import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.bindings import ModelBindings
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_URL', 'EMBEDDING_MODEL', 'EMBEDDING_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def make_channel(client, name, url, model, key='sk-bound'):
    saved = client.post('/api/settings/llm-channels', json=dict(
        name=name, channel='custom', protocol='openai', url=url, model=model, key=key,
        embeddings=['embed-x'] if 'emb' in url else []))
    assert saved.status_code == 200, saved.text
    return saved.json()['channel']


def put_binding(client, purpose, **payload):
    return client.put('/api/settings/bindings', json=dict(purpose=purpose, **payload))


def test_agent_binding_points_at_channel_and_flips_health(client):
    assert client.get('/api/health').json()['model_configured'] is False
    channel = make_channel(client, '绑定渠道', 'https://bind.example/v1', 'bind-model', 'sk-bind')
    assert put_binding(client, 'agent', kind='channel', channel_id=channel['id'], model='bind-model').status_code == 200
    payload = client.get('/api/settings/bindings').json()
    assert payload['agent']['binding'] == dict(kind='channel', channel_id=channel['id'], model='bind-model')
    assert payload['agent']['channel_name'] == '绑定渠道'
    assert payload['agent']['resolved']['configured'] is True and payload['agent']['resolved']['model'] == 'bind-model'
    llm = client.get('/api/settings/models').json()['llm']
    # 渠道是唯一事实源：解析后的 llm 行指向渠道地址并带 saved_channel_id，密钥不复制到全局行之外。
    assert llm['url'] == 'https://bind.example/v1/chat/completions' and llm['saved_channel_id'] == channel['id']
    assert llm['key_set'] is True and 'key' not in llm
    assert client.get('/api/health').json()['model_configured'] is True


def test_extraction_and_analysis_follow_own_bindings(client, monkeypatch):
    extract_channel = make_channel(client, '抽取渠道', 'https://extract.example/v1', 'extract-model', 'sk-extract')
    analyze_channel = make_channel(client, '分析渠道', 'https://analyze.example/v1', 'analyze-model', 'sk-analyze')
    repo = client.app.state.repo
    repo.save_evidence(dict(title='甲公司经营', company='甲公司', text='甲公司注册资本1000万元'), 'user_import')
    job = repo.save_job(dict(title='Python 工程师', company='甲公司', description='负责数据平台', external_id='bind-job'), 'user_import')
    assert client.put('/api/resume', json=dict(text='三年 Python 经验', format='text')).status_code == 200
    assert put_binding(client, 'extraction', kind='channel', channel_id=extract_channel['id'], model='extract-model').status_code == 200
    assert put_binding(client, 'analysis', kind='channel', channel_id=analyze_channel['id'], model='analyze-model').status_code == 200

    captured = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        from fastapi.testclient import TestClient
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        captured.append(dict(url=str(url), headers=dict(kwargs.get('headers', {})), json=json.loads(json.dumps(kwargs['json']))))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'not-json'}}], 'finish_reason': 'stop'},
                              request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    extracted = client.post('/api/companies/甲公司/signals/extract', json=dict(limit=5))
    assert extracted.status_code == 200
    request = captured[-1]
    assert request['url'] == 'https://extract.example/v1/chat/completions'
    assert request['json']['model'] == 'extract-model'
    assert request['headers'].get('Authorization') == 'Bearer sk-extract'

    analyzed = client.post('/api/analyze', json=dict(job_id=job['id']))
    request = captured[-1]
    assert request['url'] == 'https://analyze.example/v1/chat/completions'
    assert request['json']['model'] == 'analyze-model'
    assert analyzed.status_code == 502  # 模型返回了不可解析内容；这里只验证调用到达了分析绑定


def test_extraction_disabled_skips_without_model_call(client, monkeypatch):
    repo = client.app.state.repo
    repo.save_evidence(dict(title='甲公司经营', company='甲公司', text='甲公司注册资本1000万元'), 'user_import')
    assert put_binding(client, 'extraction', kind='disabled').status_code == 200
    calls = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        from fastapi.testclient import TestClient
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        calls.append(str(url))
        return original(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, 'post', post)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict(limit=5)).json()
    assert result['status'] == 'skipped' and calls == []


def test_embedding_binding_channel_local_and_disabled(client):
    channel = make_channel(client, '向量渠道', 'https://emb.example/v1', 'chat-model')
    assert put_binding(client, 'embedding', kind='channel', channel_id=channel['id'], model='embed-x').status_code == 200
    status = client.get('/api/research/index').json()
    assert status['embedding_configured'] is True and status['embedding_local'] is False
    assert put_binding(client, 'embedding', kind='local', local_model='BAAI/bge-tiny').status_code == 200
    status = client.get('/api/research/index').json()
    assert status['embedding_configured'] is True and status['embedding_local'] is True
    assert put_binding(client, 'embedding', kind='disabled').status_code == 200
    assert client.get('/api/research/index').json()['embedding_configured'] is False
    # Anthropic 协议渠道没有向量接口
    saved = client.post('/api/settings/llm-channels', json=dict(name='无向量渠道', channel='anthropic', protocol='anthropic',
                                                                url='https://anth.example/v1', model='claude'))
    assert saved.status_code == 200
    anthropic_id = saved.json()['channel']['id']
    assert put_binding(client, 'embedding', kind='channel', channel_id=anthropic_id, model='embed-x').status_code == 422


def test_binding_validation_rules(client):
    channel = make_channel(client, '校验渠道', 'https://valid.example/v1', 'valid-model')
    assert put_binding(client, 'agent', kind='disabled').status_code == 422      # 对话默认不允许关闭
    assert put_binding(client, 'agent', kind='inherit').status_code == 422       # 对话默认必须显式选渠道
    assert put_binding(client, 'agent', kind='channel', channel_id=channel['id']).status_code == 422  # 缺模型
    assert put_binding(client, 'agent', kind='channel', channel_id='LC-missing', model='m').status_code == 422
    assert put_binding(client, 'analysis', kind='inherit').status_code == 200    # 抽取/分析可回到跟随对话默认
    payload = client.get('/api/settings/bindings').json()
    # 无全局模型时“跟随对话默认”的真实生效就是 disabled；binding 为空即表示跟随默认。
    assert payload['analysis']['binding'] is None and payload['analysis']['inherited'] is True
    assert payload['analysis']['resolved']['mode'] == 'disabled'


def test_delete_guard_requires_unbinding_first(client):
    first = make_channel(client, '占用渠道', 'https://occupied.example/v1', 'occupied-model')
    second = make_channel(client, '备用渠道', 'https://spare.example/v1', 'spare-model')
    assert put_binding(client, 'agent', kind='channel', channel_id=first['id'], model='occupied-model').status_code == 200
    rejected = client.delete(f"/api/settings/llm-channels/{first['id']}")
    assert rejected.status_code == 422 and '对话 Agent' in rejected.json()['detail']
    assert put_binding(client, 'agent', kind='channel', channel_id=second['id'], model='spare-model').status_code == 200
    assert client.delete(f"/api/settings/llm-channels/{first['id']}").status_code == 200
    payload = client.get('/api/settings/bindings').json()
    assert payload['agent']['binding']['channel_id'] == second['id']


def test_bindings_override_legacy_row_and_inherit_follows_it(client):
    # 旧接口写入的全局行在未绑定时仍然生效
    legacy = client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://legacy.example/v1', model='legacy-model', protocol='openai', key='sk-legacy')}).json()['llm']
    assert legacy['url'] == 'https://legacy.example/v1/chat/completions'
    assert client.get('/api/settings/bindings').json()['agent']['binding'] is None
    channel = make_channel(client, '覆盖渠道', 'https://override.example/v1', 'override-model')
    assert put_binding(client, 'agent', kind='channel', channel_id=channel['id'], model='override-model').status_code == 200
    assert client.get('/api/settings/models').json()['llm']['url'] == 'https://override.example/v1/chat/completions'
    # extraction 绑定渠道后，inherit 会清掉绑定、重新跟随对话默认
    assert put_binding(client, 'extraction', kind='channel', channel_id=channel['id'], model='override-model').status_code == 200
    assert client.get('/api/settings/bindings').json()['extraction']['resolved']['model'] == 'override-model'
    assert put_binding(client, 'extraction', kind='inherit').status_code == 200
    assert client.get('/api/settings/bindings').json()['extraction']['resolved']['model'] == 'override-model'  # 跟随对话默认


def test_missing_channel_falls_back_to_legacy_row(client):
    legacy = client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://legacy.example/v1', model='legacy-model', protocol='openai', key='sk-legacy')}).json()['llm']
    channel = make_channel(client, '短命渠道', 'https://short.example/v1', 'short-model')
    assert put_binding(client, 'agent', kind='channel', channel_id=channel['id'], model='short-model').status_code == 200
    client.put('/api/settings/bindings', json=dict(purpose='extraction', kind='channel', channel_id=channel['id'], model='short-model'))
    # 绕过删除守卫直接移除渠道行，验证解析层的回落
    with client.app.state.repo.connection() as conn:
        conn.execute('DELETE FROM llm_channels WHERE id=?', (channel['id'],))
    llm = client.get('/api/settings/models').json()['llm']
    assert llm['url'] == legacy['url'] and llm['model'] == 'legacy-model'
    payload = client.get('/api/settings/bindings').json()
    assert payload['agent']['channel_missing'] is True
    assert payload['extraction']['resolved']['model'] == 'legacy-model'


def test_migration_promotes_legacy_activation_marker(client):
    repo = client.app.state.repo
    channel = make_channel(client, '迁移渠道', 'https://migrate.example/v1', 'migrate-model')
    with repo.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO model_settings VALUES (?,?)', ('llm', json.dumps(dict(
            mode='remote', channel='custom', protocol='openai', url='https://migrate.example/v1/chat/completions',
            model='migrate-model', local_model='', key='sk-migrate', clear_key=False, context_tokens=None,
            saved_channel_id=channel['id']), ensure_ascii=False)))
    # create_app 没把 settings 挂到 state；直接构造等价调用
    from jobscope.settings import ModelSettings
    settings = ModelSettings(repo)
    assert ModelBindings(repo).migrate_legacy_activation(settings) == dict(kind='channel', channel_id=channel['id'], model='migrate-model')
    llm = client.get('/api/settings/models').json()['llm']
    assert llm['url'] == 'https://migrate.example/v1/chat/completions' and llm['saved_channel_id'] == channel['id']
    # 再次迁移是幂等空操作
    assert ModelBindings(repo).migrate_legacy_activation(settings) is None
