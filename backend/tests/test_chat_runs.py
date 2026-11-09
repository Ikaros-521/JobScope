import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def configure(client, protocol='openai'):
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture', protocol=protocol)}).status_code == 200


def sse_chunk(delta):
    return 'data: ' + json.dumps({'choices': [delta]})


def sse_lines(*payloads):
    return ['data: [DONE]' if payload == '[DONE]' else 'data: ' + json.dumps(payload, ensure_ascii=False) for payload in payloads]


def stream_tool(name, args, call_id='call1', reasoning='先检索项目资料。'):
    return [
        sse_chunk({'delta': {'reasoning_content': reasoning}}),
        sse_chunk({'delta': {'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}}),
        'data: [DONE]',
    ]


def stream_final(text, reasoning='推理完成。'):
    parts = [sse_chunk({'delta': {'reasoning_content': reasoning}})]
    step = max(1, len(text) // 4)
    parts += [sse_chunk({'delta': {'content': text[i:i + step]}}) for i in range(0, len(text), step)]
    parts.append('data: [DONE]')
    return parts


class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_lines(self):
        return iter(self._lines)


class FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return FakeStreamResponse(self._lines)

    def __exit__(self, *args):
        return False


def stream_scripted(monkeypatch, streams, delay=0.0):
    calls = []
    original = httpx.Client.stream

    def stream(self, method, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, method, url, **kwargs)
        calls.append(json.loads(json.dumps(kwargs['json'])))
        if delay:
            time.sleep(delay)
        return FakeStream(streams.pop(0) if streams else [])

    monkeypatch.setattr(httpx.Client, 'stream', stream)
    return calls


def await_run(client, run_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f'/api/chat/runs/{run_id}').json()
        if status.get('done'):
            return status
        time.sleep(0.05)
    raise AssertionError('运行超时未完成')


def stored_messages(client, conversation_id):
    return client.get(f'/api/conversations/{conversation_id}').json()['messages']


def read_stream(client, run_id):
    frames = []
    with client.stream('GET', f'/api/chat/runs/{run_id}/stream') as response:
        assert response.status_code == 200
        event = None
        for line in response.iter_lines():
            if line.startswith('event: '):
                event = line[7:].strip()
            elif line.startswith('data: '):
                frames.append((event, json.loads(line[6:])))
    return frames


def test_run_lifecycle_and_conversation_crud_without_model(client):
    assert client.post('/api/chat/runs', json={'message': '开始', 'agent_mode': 'general'}).status_code == 400
    started = client.post('/api/chat/runs', json={'message': '哪些岗位提到 RAG？'})
    assert started.status_code == 200
    data = started.json()
    assert data['title'].startswith('哪些岗位') and data['user_message_id']
    await_run(client, data['run_id'])
    listing = client.get('/api/conversations').json()['conversations']
    assert len(listing) == 1 and listing[0]['id'] == data['conversation_id'] and listing[0]['message_count'] == 2
    detail = client.get(f"/api/conversations/{data['conversation_id']}").json()
    stored = detail['messages']
    assert stored[0]['role'] == 'user' and stored[0]['id'] == data['user_message_id']
    assert stored[1]['role'] == 'assistant' and stored[1]['mode'] == 'evidence_search'
    # Scoped company conversations never leak into the global list.
    assert client.post('/api/chat', json={'company': '甲', 'message': '公司问题'}).status_code == 200
    assert client.get('/api/conversations').json()['conversations'][0]['id'] == data['conversation_id']
    patched = client.patch(f"/api/conversations/{data['conversation_id']}", json={'title': '自定义标题', 'mode': 'general'})
    assert patched.status_code == 200 and patched.json()['title'] == '自定义标题'
    assert client.get('/api/conversations').json()['conversations'][0]['title'] == '自定义标题'
    assert client.delete(f"/api/conversations/{data['conversation_id']}").status_code == 200
    assert client.get('/api/conversations').json()['conversations'] == []
    assert client.get(f"/api/conversations/{data['conversation_id']}").status_code == 404


def test_edit_resend_truncates_history(client):
    first = client.post('/api/chat/runs', json={'message': '问题一'}).json()
    await_run(client, first['run_id'])
    second = client.post('/api/chat/runs', json={'message': '问题二', 'conversation_id': first['conversation_id']}).json()
    await_run(client, second['run_id'])
    assert len(stored_messages(client, first['conversation_id'])) == 4
    third = client.post('/api/chat/runs', json={'message': '问题一（修订）', 'conversation_id': first['conversation_id'], 'edit_message_id': first['user_message_id']}).json()
    await_run(client, third['run_id'])
    stored = stored_messages(client, first['conversation_id'])
    assert len(stored) == 2 and stored[0]['content'] == '问题一（修订）' and stored[1]['role'] == 'assistant'


def test_delete_user_message_takes_reply_with_it(client):
    run = client.post('/api/chat/runs', json={'message': '删除我'}).json()
    await_run(client, run['run_id'])
    conversation_id = run['conversation_id']
    assert len(stored_messages(client, conversation_id)) == 2
    assert client.delete(f"/api/conversations/{conversation_id}/messages/{run['user_message_id']}").status_code == 200
    assert stored_messages(client, conversation_id) == []


def test_concurrent_runs_on_same_conversation_are_rejected(client, monkeypatch):
    configure(client)
    stream_scripted(monkeypatch, [stream_tool('project_search', {'query': 'Python'})], delay=0.5)
    run = client.post('/api/chat/runs', json={'message': '第一个', 'agent_mode': 'general'}).json()
    rejected = client.post('/api/chat/runs', json={'message': '第二个', 'conversation_id': run['conversation_id'], 'agent_mode': 'general'})
    assert rejected.status_code == 409
    client.post(f"/api/chat/runs/{run['run_id']}/cancel")
    await_run(client, run['run_id'])


def test_general_run_streams_reasoning_and_tool_events(client, monkeypatch):
    configure(client)
    job = client.post('/api/import/job', json=dict(title='Agent工程师', company='甲', description='Python RAG 岗位', salary='20-30K')).json()['job']
    evidence_id = job['evidence_ids'][0]
    streams = [stream_tool('project_search', {'query': 'Python'}), stream_final(f'岗位涉及 Python [证据ID：{evidence_id}]。')]
    calls = stream_scripted(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '帮我找 Python 相关岗位', 'agent_mode': 'general'}).json()
    status = await_run(client, run['run_id'])
    assert not status.get('awaiting_approval')
    stored = stored_messages(client, run['conversation_id'])
    assert stored[1]['role'] == 'assistant' and stored[1].get('reasoning') == '推理完成。'
    tool_events = [event for event in stored[1]['trace']['events'] if event['tool'] == 'project_search']
    assert tool_events and tool_events[0]['arguments'] == {'query': 'Python'} and tool_events[0]['result_preview']
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    assert kinds[0] == 'start' and kinds[-1] == 'end'
    assert 'reasoning_delta' in kinds and 'delta' in kinds and 'step' in kinds
    done = dict(frames)['done']
    assert done['message_id'] == stored[1]['id'] and not done['interrupted']
    assert calls[0]['stream'] is True
    # The streamed assistant tool_calls must be complete OpenAI tool definitions on the follow-up request.
    follow_up_assistant = calls[1]['messages'][-2]
    assert follow_up_assistant['tool_calls'][0]['type'] == 'function'


def test_cancel_interrupts_general_run_and_marks_partial(client, monkeypatch):
    configure(client)
    stream_scripted(monkeypatch, [stream_tool('project_search', {'query': 'Python'})], delay=0.5)
    run = client.post('/api/chat/runs', json={'message': '长时间任务', 'agent_mode': 'general'}).json()
    cancelled = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    assert not cancelled['done']
    await_run(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[-1]['role'] == 'assistant' and stored[-1].get('interrupted') is True


def test_cancel_stops_research_run_without_answer(client):
    run = client.post('/api/chat/runs', json={'message': '研究一下'}).json()
    client.post(f"/api/chat/runs/{run['run_id']}/cancel")
    await_run(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert [item['role'] for item in stored] == ['user']


def test_run_approval_resumes_in_background_and_is_single_use(client, monkeypatch):
    from jobscope.extensions import MCPClient, tool_alias

    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]
    executed = []

    async def invoke(config, name, args):
        executed.append((name, args))
        return {'text': 'external evidence', 'is_error': False}

    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='需确认工具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=['lookup']))
    configure(client)
    streams = [stream_tool(tool_alias(server['id'], 'lookup'), {'q': 'test'}), stream_final('根据工具结果，可以继续核实。')]
    stream_scripted(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '搜索外部数据', 'agent_mode': 'general'}).json()
    deadline = time.time() + 10
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/chat/runs/{run['run_id']}").json()
        if status.get('awaiting_approval'):
            break
        time.sleep(0.05)
    assert status.get('awaiting_approval') and not executed
    approval_id = status['pending_approval']['approval_id']
    resumed = client.post(f"/api/chat/runs/{run['run_id']}/approval", json={'approved': True, 'approval_id': approval_id})
    assert resumed.status_code == 200
    await_run(client, run['run_id'])
    assert executed == [('lookup', {'q': 'test'})]
    stored = stored_messages(client, run['conversation_id'])
    assert stored[-1]['role'] == 'assistant' and stored[-1]['content'] == '根据工具结果，可以继续核实。'
    assert client.post(f"/api/chat/runs/{run['run_id']}/approval", json={'approved': True, 'approval_id': approval_id}).status_code == 409
