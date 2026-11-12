import json
import sys
import threading
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

    def close(self):
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


def join_worker(client, run_id, timeout=5):
    # 取消即时生效后工作线程可能仍在跑（僵尸直到下一个检查点才退出）；
    # 测试结束前必须等它退出，否则会吞掉下一个测试 monkeypatch 的假模型流。
    handle = client.app.state.runs.get(run_id)
    if handle and handle.thread is not None:
        handle.thread.join(timeout=timeout)


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
    started = client.post('/api/chat/runs', json={'message': '哪些岗位提到 RAG？', 'agent_mode': 'research'})
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
    assert client.post('/api/chat', json={'company': '甲', 'message': '公司问题', 'agent_mode': 'research'}).status_code == 200
    assert client.get('/api/conversations').json()['conversations'][0]['id'] == data['conversation_id']
    patched = client.patch(f"/api/conversations/{data['conversation_id']}", json={'title': '自定义标题', 'mode': 'general'})
    assert patched.status_code == 200 and patched.json()['title'] == '自定义标题'
    assert client.get('/api/conversations').json()['conversations'][0]['title'] == '自定义标题'
    assert client.delete(f"/api/conversations/{data['conversation_id']}").status_code == 200
    assert client.get('/api/conversations').json()['conversations'] == []
    assert client.get(f"/api/conversations/{data['conversation_id']}").status_code == 404


def test_job_scoped_run_continues_legacy_job_sessions(client):
    repo = client.app.state.repo
    job = repo.save_job(dict(title='Python 后端', company='甲公司', description='负责数据平台', external_id='j1', location='杭州'), 'user_import')
    assert client.post('/api/chat', json={'job_id': job['id'], 'company': job['company'], 'message': '这份岗位有哪些要求？', 'agent_mode': 'research'}).status_code == 200
    legacy = client.get('/api/conversations', params={'scope': 'job', 'job_id': job['id']}).json()['conversations']
    assert len(legacy) == 1

    started = client.post('/api/chat/runs', json={'message': '薪资口径呢？', 'job_id': job['id'], 'agent_mode': 'research'}).json()
    await_run(client, started['run_id'])
    detail = client.get(f"/api/conversations/{started['conversation_id']}").json()
    assert detail['scope'] == {'kind': 'job', 'job_id': job['id'], 'company': '甲公司'}
    assert detail['id'] != legacy[0]['id']
    continued = client.post('/api/chat/runs', json={'message': '继续追问', 'job_id': job['id'], 'conversation_id': legacy[0]['id'], 'agent_mode': 'research'}).json()
    await_run(client, continued['run_id'])
    assert len(stored_messages(client, legacy[0]['id'])) == 4
    assert len(client.get('/api/conversations', params={'scope': 'job', 'job_id': job['id']}).json()['conversations']) == 2
    assert client.get('/api/conversations').json()['conversations'] == []
    assert client.post('/api/chat/runs', json={'message': '问题', 'job_id': job['id'], 'agent_mode': 'general'}).status_code == 400
    assert client.post('/api/chat/runs', json={'message': '问题', 'job_id': 'J-missing', 'agent_mode': 'research'}).status_code == 404
    assert client.get('/api/conversations', params={'scope': 'job'}).status_code == 400


def test_company_scoped_run_keeps_sessions_isolated(client):
    repo = client.app.state.repo
    repo.save_evidence(dict(title='甲公司经营', company='甲公司', text='甲公司营业收入100万元'), 'user_import')
    repo.save_evidence(dict(title='乙公司经营', company='乙公司', text='乙公司营业收入90万元'), 'user_import')
    assert client.post('/api/chat/runs', json={'message': '问题', 'agent_mode': 'general', 'company': '甲公司'}).status_code == 400
    assert client.get('/api/conversations', params={'scope': 'company'}).status_code == 400

    started = client.post('/api/chat/runs', json={'message': '甲公司收入如何？', 'company': '甲公司', 'agent_mode': 'research'}).json()
    await_run(client, started['run_id'])
    detail = client.get(f"/api/conversations/{started['conversation_id']}").json()
    assert detail['scope'] == {'kind': 'company', 'company': '甲公司'}
    assert detail['messages'][1]['mode'] == 'evidence_search'
    assert detail['messages'][1]['trace']['scope'] == {'kind': 'company', 'company': '甲公司'}
    company_sessions = client.get('/api/conversations', params={'scope': 'company', 'company': '甲公司'}).json()['conversations']
    assert [item['id'] for item in company_sessions] == [started['conversation_id']]
    assert client.get('/api/conversations', params={'scope': 'company', 'company': '乙公司'}).json()['conversations'] == []
    assert client.get('/api/conversations').json()['conversations'] == []
    assert client.patch(f"/api/conversations/{started['conversation_id']}", json={'title': '甲公司核查'}).json()['title'] == '甲公司核查'
    assert client.delete(f"/api/conversations/{started['conversation_id']}").status_code == 200
    assert client.get('/api/conversations', params={'scope': 'company', 'company': '甲公司'}).json()['conversations'] == []


def test_edit_resend_truncates_history(client):
    first = client.post('/api/chat/runs', json={'message': '问题一', 'agent_mode': 'research'}).json()
    await_run(client, first['run_id'])
    second = client.post('/api/chat/runs', json={'message': '问题二', 'conversation_id': first['conversation_id'], 'agent_mode': 'research'}).json()
    await_run(client, second['run_id'])
    assert len(stored_messages(client, first['conversation_id'])) == 4
    third = client.post('/api/chat/runs', json={'message': '问题一（修订）', 'conversation_id': first['conversation_id'], 'edit_message_id': first['user_message_id'], 'agent_mode': 'research'}).json()
    await_run(client, third['run_id'])
    stored = stored_messages(client, first['conversation_id'])
    assert len(stored) == 2 and stored[0]['content'] == '问题一（修订）' and stored[1]['role'] == 'assistant'


def test_delete_user_message_takes_reply_with_it(client):
    run = client.post('/api/chat/runs', json={'message': '删除我', 'agent_mode': 'research'}).json()
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
    join_worker(client, run['run_id'])


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


def test_cancel_interrupts_general_run_and_discards_partial(client, monkeypatch):
    configure(client)
    stream_scripted(monkeypatch, [stream_final('已取消轮次的回答'), stream_final('接着问的回答')], delay=0.5)
    run = client.post('/api/chat/runs', json={'message': '长时间任务', 'agent_mode': 'general'}).json()
    cancelled = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    # 取消立即生效：快照直接 done，会话解锁，不再等工作线程跑到检查点。
    assert cancelled['done'] and cancelled['cancelled']
    followup = client.post('/api/chat/runs', json={'message': '接着问', 'conversation_id': run['conversation_id']})
    assert followup.status_code == 200
    join_worker(client, run['run_id'])
    await_run(client, followup.json()['run_id'])
    join_worker(client, followup.json()['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert [item['role'] for item in stored] == ['user', 'user', 'assistant']
    assert stored[1]['content'] == '接着问'
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    assert kinds.count('cancelled') == 1 and 'done' not in kinds


class SlowStreamResponse:
    """Mimics a thinking model that sits silent between chunks; close() from the
    cancelling thread must break the blocked iteration like a dropped connection
    (verified against real httpx: close() unblocks the reader with a ReadError)."""

    def __init__(self, lines, gap):
        self._lines, self._gap = lines, gap
        self.status_code = 200
        self._interrupted = threading.Event()

    def raise_for_status(self):
        return None

    def close(self):
        self._interrupted.set()

    def iter_lines(self):
        for index, line in enumerate(self._lines):
            if index and self._interrupted.wait(self._gap):
                raise httpx.ReadError('connection closed while waiting for the next chunk')
            yield line


def stream_slow(monkeypatch, lines, gap):
    original = httpx.Client.stream

    def stream(self, method, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, method, url, **kwargs)

        class _Ctx:
            def __enter__(self):
                return SlowStreamResponse(lines, gap)

            def __exit__(self, *args):
                return False

        return _Ctx()

    monkeypatch.setattr(httpx.Client, 'stream', stream)


def test_cancel_cuts_slow_model_stream_immediately(client, monkeypatch):
    configure(client)
    # First chunk (reasoning) arrives at once, then the stream goes silent for 5s.
    stream_slow(monkeypatch, [sse_chunk({'delta': {'reasoning_content': '思考中'}})] + stream_final('很慢的回答'), gap=5.0)
    run = client.post('/api/chat/runs', json={'message': '慢思考问题', 'agent_mode': 'general'}).json()
    # Wait until the worker is blocked waiting for the second chunk, then cancel.
    # (Observing the event log directly: consuming the sync SSE response would block
    # the TestClient until the run finishes naturally.)
    handle = client.app.state.runs.get(run['run_id'])
    deadline = time.time() + 5
    while time.time() < deadline:
        with handle.condition:
            if 'reasoning_delta' in [kind for kind, _ in handle.events]:
                break
        time.sleep(0.05)
    else:
        raise AssertionError('未观察到 reasoning_delta，worker 未进入流式等待')
    cancelled = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    assert cancelled['done'] and cancelled['cancelled']
    # The abort closes the model stream, so the worker exits without waiting out
    # the 5s chunk gap (the pre-abort behaviour was to coast until the next chunk).
    handle.thread.join(timeout=2.5)
    assert not handle.thread.is_alive()
    # The cut stream surfaces as cancellation, never as a broadcast error.
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    assert 'cancelled' in kinds and 'error' not in kinds


def test_mcp_session_call_aborts_on_stop_and_releases_lock():
    import asyncio

    from jobscope.extensions import _MCPSession

    session = _MCPSession('test-abort', {'transport': 'stdio', 'command': 'x', 'args': [], 'env': {}})
    session.session = object()  # skip the real stdio spawn; the coroutine ignores the session
    try:
        async def slow(_session):
            await asyncio.sleep(8)
            return 'done'

        started = time.perf_counter()
        with pytest.raises(ValueError, match='停止'):
            session.call(lambda s: slow(s), timeout=10, should_stop=lambda: True)
        assert time.perf_counter() - started < 2

        async def quick(_session):
            return 'ok'

        # Abandoning released the session lock: the next caller is not stuck behind the zombie wait.
        assert session.call(lambda s: quick(s), timeout=5) == 'ok'
    finally:
        session.close()


def test_cancel_stops_research_run_without_answer(client, monkeypatch):
    from jobscope.research.index import ResearchIndex

    def slow_sync(self):
        time.sleep(0.3)
        return dict(documents=0, chunks=0, updated=0)

    monkeypatch.setattr(ResearchIndex, 'sync', slow_sync)
    run = client.post('/api/chat/runs', json={'message': '研究一下', 'agent_mode': 'research'}).json()
    cancelled = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    assert cancelled['done'] and cancelled['cancelled']
    stored = stored_messages(client, run['conversation_id'])
    assert [item['role'] for item in stored] == ['user']
    frames = read_stream(client, run['run_id'])
    assert [kind for kind, _ in frames][-2:] == ['cancelled', 'end']
    join_worker(client, run['run_id'])


def test_cancel_during_blocking_step_is_immediate_and_seals_the_log(client, monkeypatch):
    from jobscope.research.index import ResearchIndex
    entered = threading.Event()
    release = threading.Event()

    def blocking_sync(self):
        entered.set()
        release.wait(timeout=5)
        return dict(documents=0, chunks=0, updated=0)

    monkeypatch.setattr(ResearchIndex, 'sync', blocking_sync)
    run = client.post('/api/chat/runs', json={'message': '研究一下', 'agent_mode': 'research'}).json()
    assert entered.wait(timeout=5)
    started = time.time()
    cancelled = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    # 工作线程仍阻塞在不可中断步骤里，取消也已经即时生效。
    assert time.time() - started < 1 and cancelled['done'] and cancelled['cancelled']
    followup = client.post('/api/chat/runs', json={'message': '新问题', 'conversation_id': run['conversation_id'], 'agent_mode': 'research'})
    assert followup.status_code == 200
    release.set()
    join_worker(client, run['run_id'])
    await_run(client, followup.json()['run_id'])
    join_worker(client, followup.json()['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert [item['role'] for item in stored] == ['user', 'user', 'assistant']
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    # 僵尸线程解除阻塞后的迟到输出一律被丢弃：日志封口，只保留取消。
    assert kinds.count('cancelled') == 1 and 'done' not in kinds and 'step' not in kinds


def test_research_agent_checkpoints_raise_run_cancelled(client):
    from jobscope.research.agent import ResearchAgent, RunCancelled
    repo = client.app.state.repo
    agent = ResearchAgent(repo, synthesizer=lambda *args, **kwargs: dict(answer='不该出现', citations=[], mode='model'))
    checks = {'n': 0}

    def flip():
        checks['n'] += 1
        return checks['n'] > 1  # 第一道检查放行，索引同步后第二道命中

    with pytest.raises(RunCancelled):
        agent.answer('研究一下', {'kind': 'global'}, should_stop=flip)
    with repo.connection() as c:
        payloads = [json.loads(row[0]) for row in c.execute('SELECT payload FROM research_runs').fetchall()]
    assert any(item['status'] == 'cancelled' for item in payloads)


def test_cancel_after_natural_completion_is_noop(client):
    run = client.post('/api/chat/runs', json={'message': '研究一下', 'agent_mode': 'research'}).json()
    await_run(client, run['run_id'])
    snapshot = client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()
    assert snapshot['done'] and not snapshot['cancelled']
    frames = read_stream(client, run['run_id'])
    assert 'cancelled' not in [kind for kind, _ in frames]


def test_legacy_research_mode_uses_scoped_general_and_streams_answer_deltas(client, monkeypatch):
    configure(client)
    repo = client.app.state.repo
    evidence_id = repo.save_evidence(dict(title='公司自述', company='证优客', text='证优客是一家做认证服务的公司，总部在杭州。'), 'user_import')['id']

    stream_scripted(monkeypatch, [stream_tool('project_search', {'query': '认证服务'}),
                                  stream_final(f'证优客是做认证服务的公司 [{evidence_id}]。')])
    run = client.post('/api/chat/runs', json={'message': '这是个什么公司', 'company': '证优客', 'agent_mode': 'research'}).json()
    await_run(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[-1]['role'] == 'assistant' and '认证服务' in stored[-1]['content'] and stored[-1]['mode'] == 'agent'
    assert stored[-1]['trace']['scope'] == {'kind': 'company', 'company': '证优客'}
    assert stored[-1]['citations'][0]['id'] == evidence_id
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    assert 'delta' in kinds and kinds[-2:] == ['done', 'end']
    streamed = ''.join(payload['text'] for kind, payload in frames if kind == 'delta')
    assert '认证服务' in streamed


def test_general_run_persists_ordered_timeline(client, monkeypatch):
    configure(client)
    job = client.post('/api/import/job', json=dict(title='Agent工程师', company='甲', description='Python RAG 岗位', salary='20-30K')).json()['job']
    evidence_id = job['evidence_ids'][0]
    streams = [stream_tool('project_search', {'query': 'Python'}), stream_final(f'岗位涉及 Python [证据ID：{evidence_id}]。')]
    stream_scripted(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '帮我找 Python 相关岗位', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])[1]
    timeline = stored['timeline']
    # 顺序模式分段日志：思维链被工具打断即分段，正文与 step 按发生顺序穿插。
    assert [segment['kind'] for segment in timeline] == ['reasoning', 'step', 'step', 'reasoning', 'text', 'step']
    assert timeline[0]['text'] == '先检索项目资料。'
    assert timeline[1]['event']['tool'] == 'agent_model'
    assert timeline[2]['event']['tool'] == 'project_search' and timeline[2]['event']['arguments'] == {'query': 'Python'}
    assert timeline[3]['text'] == '推理完成。'
    assert timeline[4]['text'] == f'岗位涉及 Python [证据ID：{evidence_id}]。'
    assert timeline[5]['event']['tool'] == 'agent_model'


def test_timeline_survives_approval_pause_and_resume(client, monkeypatch):
    from jobscope.extensions import MCPClient, tool_alias

    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]

    def invoke(config, name, args, should_stop=None):
        return {'text': 'external evidence', 'is_error': False}

    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='需确认工具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json={'enabled': True, 'allowed_tools': ['lookup']})
    configure(client)
    stream_scripted(monkeypatch, [stream_tool(tool_alias(server['id'], 'lookup'), {'q': 'test'}), stream_final('工具已确认执行。')])
    run = client.post('/api/chat/runs', json={'message': '搜索外部数据', 'agent_mode': 'general'}).json()
    deadline = time.time() + 10
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/chat/runs/{run['run_id']}").json()
        if status.get('awaiting_approval'):
            break
        time.sleep(0.05)
    assert status.get('awaiting_approval')
    assert client.post(f"/api/chat/runs/{run['run_id']}/approval", json={'approved': True, 'approval_id': status['pending_approval']['approval_id']}).status_code == 200
    await_run(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])[-1]
    timeline = stored['timeline']
    tools = [segment['event']['tool'] for segment in timeline if segment['kind'] == 'step']
    # 审批暂停前后同一条分段日志：tool_approval 与实际执行按真实顺序保留。
    assert tools == ['agent_model', 'tool_approval', tool_alias(server['id'], 'lookup'), 'agent_model']
    assert [segment['kind'] for segment in timeline] == ['reasoning', 'step', 'step', 'step', 'reasoning', 'text', 'step']
    assert timeline[5]['text'] == '工具已确认执行。'



    from jobscope.extensions import MCPClient, tool_alias

    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]
    executed = []

    def invoke(config, name, args, should_stop=None):
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


def test_approval_keeps_stream_open_and_continues_on_same_log(client, monkeypatch):
    # 审批挂起时流保持打开（done=False），允许后 resumed 及后续事件写入同一份日志；
    # 客户端不重连即可续传，事件序列只有一份 start，不再整段重放重建。
    from jobscope.extensions import MCPClient, tool_alias

    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]

    def invoke(config, name, args, should_stop=None):
        return {'text': 'external evidence', 'is_error': False}

    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='需确认工具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=['lookup']))
    configure(client)
    stream_scripted(monkeypatch, [stream_tool(tool_alias(server['id'], 'lookup'), {'q': 'test'}), stream_final('工具已确认执行。')])
    run = client.post('/api/chat/runs', json={'message': '搜索外部数据', 'agent_mode': 'general'}).json()
    deadline = time.time() + 10
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/chat/runs/{run['run_id']}").json()
        if status.get('awaiting_approval'):
            break
        time.sleep(0.05)
    # 挂起期间流未终结：客户端连着同一条流等待 resumed，无需重连重放。
    assert status.get('awaiting_approval') and status.get('done') is False
    approval_id = status['pending_approval']['approval_id']
    assert client.post(f"/api/chat/runs/{run['run_id']}/approval", json={'approved': True, 'approval_id': approval_id}).status_code == 200
    await_run(client, run['run_id'])
    frames = read_stream(client, run['run_id'])
    kinds = [kind for kind, _ in frames]
    assert kinds.count('start') == 1 and kinds.count('resumed') == 1
    assert kinds.index('approval_required') < kinds.index('resumed')
    assert kinds[-2:] == ['done', 'end']
