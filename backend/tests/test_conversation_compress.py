import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app

from test_chat_runs import await_run, join_worker, sse_chunk, stream_scripted


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def configure(client):
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture', protocol='openai')}).status_code == 200


def fake_summary_post(monkeypatch, summary='### 📋 上下文压缩摘要\n\n用户咨询了岗位与薪资比较。', before_reply=None):
    """拦截 complete() 的非流式请求，返回固定摘要文本。"""
    captured = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        from fastapi.testclient import TestClient
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        captured.append(dict(url=str(url), headers=dict(kwargs.get('headers', {})), json=json.loads(json.dumps(kwargs['json']))))
        if before_reply:
            before_reply()
        class _Response:
            status_code = 200
            content = b'{}'
            def raise_for_status(self):
                return None
            def json(self):
                return {'choices': [{'message': {'content': summary}}]}
        return _Response()

    monkeypatch.setattr(httpx.Client, 'post', post)
    return captured


def build_history(client, rounds=3):
    """未配置模型时研究模式走本地摘录兜底，快速堆出 user/assistant 成对历史。"""
    conversation_id = None
    for index in range(rounds):
        body = {'message': f'历史问题{index + 1}'}
        if conversation_id:
            body['conversation_id'] = conversation_id
        run = client.post('/api/chat/runs', json=body).json()
        await_run(client, run['run_id'])
        conversation_id = run['conversation_id']
    return conversation_id


def test_compress_folds_old_messages_and_inserts_checkpoint(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    assert len(client.get(f'/api/conversations/{conversation_id}').json()['messages']) == 6

    configure(client)
    captured = fake_summary_post(monkeypatch)
    response = client.post(f'/api/conversations/{conversation_id}/compress')
    assert response.status_code == 200
    result = response.json()
    assert result['compacted_count'] == 2  # 保留最近 4 条，前面的 2 条被折叠

    stored = client.get(f'/api/conversations/{conversation_id}').json()['messages']
    assert len(stored) == 7
    assert stored[0]['compacted'] is True and stored[1]['compacted'] is True
    checkpoint = stored[2]
    assert checkpoint['kind'] == 'compaction' and checkpoint['compacted_count'] == 2
    assert checkpoint['content'].startswith('### 📋 上下文压缩摘要')
    assert not stored[3].get('compacted')
    # 摘要请求确实把可折叠历史转录给了模型
    transcript = captured[0]['json']['messages'][-1]['content']
    assert '历史问题1' in transcript and '历史问题3' not in transcript

    # 重复压缩：只剩检查点可折叠，数量不足直接拒绝
    again = client.post(f'/api/conversations/{conversation_id}/compress')
    assert again.status_code == 422


def test_compress_guards_unconfigured_and_short_history(client, tmp_path):
    conversation_id = build_history(client, rounds=1)
    configure(client)
    # 只有 2 条消息：无可折叠内容
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 422
    assert client.post('/api/conversations/missing/compress').status_code == 404

    # 未配置模型：独立数据目录的新实例验证 400
    with TestClient(create_app(tmp_path / 'guard-db')) as bare:
        conversation2 = build_history(bare, rounds=3)
        assert bare.post(f'/api/conversations/{conversation2}/compress').status_code == 400


def test_active_run_blocks_compress_then_unblocks(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    configure(client)
    stream_scripted(monkeypatch, [ [sse_chunk({'delta': {'reasoning_content': '推理中'}}), 'data: [DONE]'] ], delay=0.5)
    run = client.post('/api/chat/runs', json={'message': '正在生成', 'conversation_id': conversation_id, 'agent_mode': 'general'}).json()
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 409
    client.post(f"/api/chat/runs/{run['run_id']}/cancel")
    join_worker(client, run['run_id'])
    fake_summary_post(monkeypatch)
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 200


def test_compacted_history_excluded_from_model_context(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    configure(client)
    fake_summary_post(monkeypatch)
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 200

    captured = stream_scripted(monkeypatch, [[sse_chunk({'delta': {'content': '完成'}}), 'data: [DONE]']])
    run = client.post('/api/chat/runs', json={'message': '压缩后再问', 'conversation_id': conversation_id, 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    messages = captured[0]['messages']
    blob = json.dumps(messages, ensure_ascii=False)
    assert '历史问题1' not in blob  # 已折叠的旧消息不再进入模型上下文
    assert '上下文压缩摘要' in blob  # 摘要检查点保留，模型能引用
    assert '压缩后再问' in messages[-1]['content']


def test_compress_uses_conversation_channel_and_model(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    configure(client)
    saved = client.post('/api/settings/llm-channels', json=dict(
        name='压缩渠道', channel='custom', url='https://compress.example/v1', model='chan-model', key='sk-chan')).json()['channel']
    assert client.patch(f'/api/conversations/{conversation_id}', json={'llm_channel': saved['id']}).status_code == 200
    captured = fake_summary_post(monkeypatch)
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 200
    request = captured[0]
    assert request['url'] == 'https://compress.example/v1/chat/completions'
    assert request['json']['model'] == 'chan-model'
    assert request['headers'].get('Authorization') == 'Bearer sk-chan'


def test_compress_discards_summary_when_history_changes_midway(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    configure(client)
    repo = client.app.state.repo

    def mutate():
        repo.append_messages(conversation_id, [dict(role='user', content='压缩期间的新消息')])

    fake_summary_post(monkeypatch, before_reply=mutate)
    response = client.post(f'/api/conversations/{conversation_id}/compress')
    assert response.status_code == 409
    stored = client.get(f'/api/conversations/{conversation_id}').json()['messages']
    # 摘要没有落库：没有检查点，也没有任何折叠标记。
    assert not any(item.get('kind') == 'compaction' for item in stored)
    assert not any(item.get('compacted') for item in stored)
    assert stored[-1]['content'] == '压缩期间的新消息'


def test_recompression_uses_previous_summary_and_new_messages(client, monkeypatch):
    conversation_id = build_history(client, rounds=3)
    configure(client)
    fake_summary_post(monkeypatch, summary='### 📋 上下文压缩摘要\n\n第一轮摘要：用户在比较岗位薪资。')
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 200
    # 继续对话，产生摘要之后的新消息。
    for index in range(3):
        run = client.post('/api/chat/runs', json={'message': f'压缩后问题{index + 1}', 'conversation_id': conversation_id}).json()
        await_run(client, run['run_id'])

    captured = fake_summary_post(monkeypatch, summary='### 📋 上下文压缩摘要\n\n第二轮摘要。')
    assert client.post(f'/api/conversations/{conversation_id}/compress').status_code == 200
    transcript = captured[0]['json']['messages'][-1]['content']
    assert '第一轮摘要' in transcript  # 上一份摘要参与下一轮压缩，信息不丢
    assert '压缩后问题1' in transcript


def test_summary_chunks_keep_every_message_whole():
    from jobscope.main import SUMMARY_CHUNK_CHARS, _summary_chunks
    long_text = '证' * 9000
    messages = [dict(role='user', content=long_text + '甲'), dict(role='assistant', content=long_text + '乙'), dict(role='user', content='短消息')]
    chunks = _summary_chunks(messages)
    assert len(chunks) >= 2
    joined = '\n\n'.join(chunks)
    assert (long_text + '甲') in joined and (long_text + '乙') in joined and '短消息' in joined
    assert all(len(chunk) <= SUMMARY_CHUNK_CHARS + 10000 for chunk in chunks)
