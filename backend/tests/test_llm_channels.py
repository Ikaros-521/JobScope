import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app

from test_chat_runs import await_run, join_worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def test_channel_crud_key_retention_and_activate(client):
    saved = client.post('/api/settings/llm-channels', json=dict(
        name='工作渠道', channel='deepseek', protocol='openai', url='https://api.deepseek.com/v1',
        model='deepseek-chat', models=['deepseek-chat', 'deepseek-reasoner'], key='sk-work-1'))
    assert saved.status_code == 200
    channel = saved.json()['channel']
    # URL 按协议归一成完整端点；公钥字段永不回读。
    assert channel['url'] == 'https://api.deepseek.com/v1/chat/completions'
    assert 'key' not in channel and channel['key_set'] is True
    assert channel['context_tokens'] is None
    assert [item['name'] for item in saved.json()['channels']] == ['工作渠道']

    # 同名再保存 = 编辑；留空保留旧密钥，附带的上下文窗口一并落库。
    updated = client.post('/api/settings/llm-channels', json=dict(
        name='工作渠道', channel='deepseek', protocol='openai', url='https://api.deepseek.com/v1',
        model='deepseek-reasoner', context_tokens=64000)).json()['channel']
    assert updated['id'] == channel['id'] and updated['key_set'] is True and updated['context_tokens'] == 64000

    # 勾选清除密钥后才真正清空。
    cleared = client.post('/api/settings/llm-channels', json=dict(
        name='工作渠道', channel='deepseek', protocol='openai', url='https://api.deepseek.com/v1',
        model='deepseek-chat', clear_key=True)).json()['channel']
    assert cleared['key_set'] is False

    activated = client.post(f"/api/settings/llm-channels/{channel['id']}/activate")
    assert activated.status_code == 200
    payload = activated.json()
    # 激活现在是“设为对话默认”：写指向渠道的绑定，不复制配置。
    llm = payload['settings']['llm']
    assert llm['mode'] == 'remote' and llm['channel'] == 'deepseek' and llm['url'] == 'https://api.deepseek.com/v1/chat/completions'
    assert llm['key_set'] is False  # 上一步已清除
    assert payload['bindings']['agent']['binding'] == dict(kind='channel', channel_id=channel['id'], model='deepseek-chat')

    # 被功能引用的渠道不能直接删除；改绑后才可删。
    assert client.delete(f"/api/settings/llm-channels/{channel['id']}").status_code == 422
    other = client.post('/api/settings/llm-channels', json=dict(
        name='备用渠道', channel='custom', protocol='openai', url='https://other.example/v1', model='other-model', key='sk-other')).json()['channel']
    assert client.put('/api/settings/bindings', json=dict(purpose='agent', kind='channel', channel_id=other['id'], model='other-model')).status_code == 200
    assert client.delete(f"/api/settings/llm-channels/{channel['id']}").status_code == 200
    remaining = client.get('/api/settings/llm-channels').json()['channels']
    assert [item['id'] for item in remaining] == [other['id']]
    assert client.delete(f"/api/settings/llm-channels/{channel['id']}").status_code == 404
    assert client.post(f"/api/settings/llm-channels/{channel['id']}/activate").status_code == 404


def test_channel_validation_rejects_bad_input(client):
    assert client.post('/api/settings/llm-channels', json=dict(name='无地址', channel='custom', url='', model='')).status_code == 422
    assert client.post('/api/settings/llm-channels', json=dict(name='坏地址', channel='custom', url='ftp://x', model='')).status_code == 422
    assert client.post('/api/settings/llm-channels', json=dict(name='坏渠道', channel='nope', url='https://a.example/v1')).status_code == 422
    assert client.post('/api/settings/llm-channels', json=dict(name='', channel='custom', url='https://a.example/v1')).status_code == 422
    # 远程 HTTP 明文地址拒绝，本机 HTTP 放行。
    assert client.post('/api/settings/llm-channels', json=dict(name='明文', channel='custom', url='http://relay.example/v1')).status_code == 422
    local = client.post('/api/settings/llm-channels', json=dict(name='本机', channel='custom', url='http://127.0.0.1:8000/v1', model='qwen'))
    assert local.status_code == 200 and local.json()['channel']['url'] == 'http://127.0.0.1:8000/v1/chat/completions'


def test_channel_rejects_url_change_without_new_key(client):
    saved = client.post('/api/settings/llm-channels', json=dict(
        name='绑定渠道', channel='custom', url='https://a.example/v1', model='m', key='sk-a')).json()['channel']
    # 换地址但没重填密钥：拒绝，避免把 A 的密钥发给 B。
    rejected = client.post('/api/settings/llm-channels', json=dict(
        name='绑定渠道', channel='custom', url='https://b.example/v1', model='m'))
    assert rejected.status_code == 422 and '密钥' in rejected.json()['detail']
    # 换协议同样拒绝。
    assert client.post('/api/settings/llm-channels', json=dict(
        name='绑定渠道', channel='custom', protocol='anthropic', url='https://a.example/v1', model='m')).status_code == 422
    # 显式清除密钥或重填后允许。
    assert client.post('/api/settings/llm-channels', json=dict(
        name='绑定渠道', channel='custom', url='https://b.example/v1', model='m', key='sk-b')).status_code == 200
    updated = client.get('/api/settings/llm-channels').json()['channels'][0]
    assert updated['id'] == saved['id'] and updated['url'] == 'https://b.example/v1/chat/completions'


def test_channel_test_and_discover_use_saved_credential(client, monkeypatch):
    saved = client.post('/api/settings/llm-channels', json=dict(
        name='凭证渠道', channel='custom', url='https://cred.example/v1', model='m', key='sk-saved')).json()['channel']
    captured = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        from fastapi.testclient import TestClient
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        captured.append(dict(url=str(url), headers=dict(kwargs.get('headers', {}))))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    response = client.post('/api/settings/models/test', json=dict(
        target='llm', channel_id=saved['id'],
        config=dict(mode='remote', channel='custom', protocol='openai', url='https://cred.example/v1', model='m')))
    assert response.status_code == 200
    assert captured[-1]['headers'].get('Authorization') == 'Bearer sk-saved'


def test_run_uses_saved_channel_connection_and_model(client, monkeypatch):
    saved = client.post('/api/settings/llm-channels', json=dict(
        name='备份渠道', channel='custom', protocol='openai', url='https://chan-b.example/v1',
        model='b-default', key='sk-b')).json()['channel']
    # 全局配置做对照组：渠道覆盖必须整体替换地址、密钥与模型
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://global.example/v1', model='g-model', protocol='openai', key='sk-global')}).status_code == 200

    captured = []
    original = httpx.Client.stream

    def stream(self, method, url, **kwargs):
        from fastapi.testclient import TestClient
        if isinstance(self, TestClient):
            return original(self, method, url, **kwargs)
        captured.append(dict(url=str(url), json=json.loads(json.dumps(kwargs['json'])), headers=dict(kwargs.get('headers', {}))))
        lines = ['data: ' + json.dumps({'choices': [{'delta': {'content': '完成'}}]}), 'data: [DONE]']

        class _Response:
            status_code = 200
            def raise_for_status(self):
                return None
            def iter_lines(self):
                return iter(lines)

        class _Stream:
            def __enter__(self):
                return _Response()
            def __exit__(self, *args):
                return False
        return _Stream()

    monkeypatch.setattr(httpx.Client, 'stream', stream)

    # 会话级覆盖：PATCH 保存渠道 + 模型；general 模式走该渠道的地址与密钥。
    first = client.post('/api/chat/runs', json={'message': '建立会话', 'agent_mode': 'general'}).json()
    await_run(client, first['run_id'])
    patched = client.patch(f"/api/conversations/{first['conversation_id']}", json={'llm_channel': saved['id'], 'model': 'b-custom'})
    assert patched.status_code == 200 and patched.json()['llm_channel'] == saved['id']

    run = client.post('/api/chat/runs', json={'message': '按渠道回答', 'conversation_id': first['conversation_id'], 'agent_mode': 'general', 'llm_channel': saved['id'], 'model': 'b-custom'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    assert captured, '渠道覆盖未生效，没有任何上游请求'
    request = captured[-1]
    assert request['url'] == 'https://chan-b.example/v1/chat/completions'
    assert request['json']['model'] == 'b-custom'
    assert request['headers'].get('Authorization') == 'Bearer sk-b'
    assert '按渠道回答' in request['json']['messages'][-1]['content']

    # 只带渠道、显式空模型：落到该渠道的默认模型
    default_run = client.post('/api/chat/runs', json={'message': '渠道默认模型', 'conversation_id': first['conversation_id'], 'agent_mode': 'general', 'llm_channel': saved['id'], 'model': ''}).json()
    await_run(client, default_run['run_id'])
    join_worker(client, default_run['run_id'])
    assert captured[-1]['url'] == 'https://chan-b.example/v1/chat/completions'
    assert captured[-1]['json']['model'] == 'b-default'

    # 两者都省略：沿用会话已保存的渠道与模型
    inherit_run = client.post('/api/chat/runs', json={'message': '沿用会话配置', 'conversation_id': first['conversation_id'], 'agent_mode': 'general'}).json()
    await_run(client, inherit_run['run_id'])
    join_worker(client, inherit_run['run_id'])
    assert captured[-1]['url'] == 'https://chan-b.example/v1/chat/completions'
    assert captured[-1]['json']['model'] == 'b-custom'

    # 显式重置两者：跟随全局配置
    followup = client.post('/api/chat/runs', json={'message': '回到全局', 'conversation_id': first['conversation_id'], 'agent_mode': 'general', 'model': '', 'llm_channel': ''}).json()
    await_run(client, followup['run_id'])
    join_worker(client, followup['run_id'])
    fallback = captured[-1]
    assert fallback['url'] == 'https://global.example/v1/chat/completions'
    assert fallback['json']['model'] == 'g-model'
    assert fallback['headers'].get('Authorization') == 'Bearer sk-global'
