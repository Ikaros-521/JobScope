"""2026-11-25 错误恢复分层与压缩存档的端到端回归（learn-claude-code s15 经验落地）。

覆盖：429 指数退避有界重试、529 连续过载切备用渠道（含未配置降级的失败路径）、
上游超长 400 触发响应式压缩并落档（archive_read 可取回原文）、输出截断升级预算
重试（半截先经 SSE start 重置实时气泡）、classify_transport_error 分流单元。
全部 stub 在 httpx 层，复用 test_chat_runs 的脚本化流与工具助手；退避基数打桩为 0 免等待。
"""
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_chat_runs import (FakeStream, await_run, client, configure, join_worker,
                            read_stream, sse_chunk, stored_messages, stream_final,
                            stream_parallel_tool_calls, stream_tool)

from jobscope import general_agent as agent_module
from jobscope.model_transport import (ContextOverflowError, OverloadedError, RateLimitError,
                                      TransportStatusError, classify_transport_error)


class FakeErrorResponse:
    """模拟上游错误响应：raise_for_status 抛带状态码/响应体/Retry-After 的 HTTPStatusError，
    model_transport.classify_transport_error 据此分流为 429/529/超长 400。"""

    def __init__(self, status, body='', headers=None):
        self.status_code = status
        self._body = body
        self._headers = headers or {}

    def raise_for_status(self):
        request = httpx.Request('POST', 'https://fixture.example/v1/chat/completions')
        httpx.Response(self.status_code, request=request, text=self._body, headers=self._headers).raise_for_status()

    # stream_turn 以 `with client.stream(...) as response:` 使用打桩返回值，必须可作上下文管理器。
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        return None

    def iter_lines(self):
        return iter([])


def stream_scripted_urls(monkeypatch, items):
    """同 test_chat_runs.stream_scripted，但脚本项可以是 FakeErrorResponse（非列表原样返回），
    且记录每次模型调用的 url——断言 529 降级后请求确实打到了备用渠道。"""
    calls = []
    original = httpx.Client.stream

    def stream(self, method, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, method, url, **kwargs)
        calls.append(dict(url=url, body=json.loads(json.dumps(kwargs['json']))))
        item = items.pop(0) if items else []
        if isinstance(item, list):
            return FakeStream(item)
        return item

    monkeypatch.setattr(httpx.Client, 'stream', stream)
    return calls


def save_settings(client, fallback_channel=''):
    return client.put('/api/settings/system', json={
        'chat_display_mode': 'merged', 'show_reasoning': True,
        'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50,
                         'context_characters': 160000, 'max_seconds': 900},
        'market_source': 'official', 'agent_fallback_channel': fallback_channel})


def retry_events(stored):
    return [event for event in stored[-1]['trace']['events'] if event['tool'] == 'model_retry']


def test_rate_limit_backs_off_then_completes(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    streams = [FakeErrorResponse(429, 'rate limited'),
               stream_tool('project_search', {'query': 'Python'}), stream_final('限流退避后完成。')]
    calls = stream_scripted_urls(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '限流重试', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[1]['content'] == '限流退避后完成。' and not stored[1].get('failed')
    retries = retry_events(stored)
    assert len(retries) == 1 and '429' in retries[0]['note']
    assert len(calls) == 3 and all(call['url'].startswith('https://fixture.example') for call in calls)


def test_rate_limit_exhaustion_fails_with_both_retries_logged(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    stream_scripted_urls(monkeypatch, [FakeErrorResponse(429, 'rate limited')] * 3)
    run = client.post('/api/chat/runs', json={'message': '限流耗尽', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[1]['failed'] is True
    retries = retry_events(stored)
    assert len(retries) == 2 and all('429' in event['note'] for event in retries)


def test_overload_switches_to_fallback_channel_then_completes(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    channel = client.post('/api/settings/llm-channels', json=dict(
        name='备用渠道', channel='custom', protocol='openai',
        url='https://backup.example/v1', model='backup-model', key='k2')).json()['channel']
    assert save_settings(client, fallback_channel=channel['id']).status_code == 200
    streams = [FakeErrorResponse(529, 'overloaded'), FakeErrorResponse(529, 'overloaded'),
               FakeErrorResponse(529, 'overloaded'),
               stream_tool('project_search', {'query': 'Python'}), stream_final('备用渠道完成。')]
    calls = stream_scripted_urls(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '过载降级', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[-1]['content'] == '备用渠道完成。' and not stored[-1].get('failed')
    assert any('切换备用渠道' in event['note'] for event in retry_events(stored))
    # 前 3 次退避打主渠道，切换后第 4 次起打到备用渠道。
    assert [call['url'].split('/')[2] for call in calls] == ['fixture.example'] * 3 + ['backup.example'] * 2


def test_overload_without_fallback_config_fails_after_retries(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    stream_scripted_urls(monkeypatch, [FakeErrorResponse(529, 'overloaded')] * 3)
    run = client.post('/api/chat/runs', json={'message': '过载无备用', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[1]['failed'] is True and stored[1]['failure']
    retries = retry_events(stored)
    assert len(retries) == 2 and all('529' in event['note'] for event in retries)


def parallel_reads(count, evidence_id):
    """一轮并行返回 count 个 project_read（同一证据不同 call_id）：确定性构造 >2000 字符的工具
    结果正文，让响应式压缩走到存档分支（project_search 的检索片段长度不可控）。"""
    calls = [{'index': i, 'id': f'batch{i}', 'type': 'function',
              'function': {'name': 'project_read', 'arguments': json.dumps({'evidence_id': evidence_id})}}
             for i in range(count)]
    return [sse_chunk({'delta': {'tool_calls': calls}}), 'data: [DONE]']


def test_context_overflow_compacts_archives_and_recovers_via_archive_read(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    description = '岗位职责与要求：' + '负责大模型检索增强与多轮对话系统的设计与落地。' * 120
    job = client.post('/api/import/job', json=dict(title='长文岗位', company='长文公司',
                                                   description=description, salary='20-30K')).json()['job']
    evidence_id = job['evidence_ids'][0]
    overflow = FakeErrorResponse(400, "This model's maximum context length is 8192 tokens")
    # 7 个并行 project_read：> COMPACT_KEEP=6 才有可压缩对象；单条正文 6000 字符窗口必超存档阈值。
    streams = [parallel_reads(7, evidence_id), overflow,
               stream_tool('archive_read', {'tool_call_id': 'batch0'}), stream_final('压缩取回原文后继续完成。')]
    stream_scripted_urls(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '超长压缩', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[-1]['role'] == 'assistant' and not stored[-1].get('failed')
    events = stored[-1]['trace']['events']
    assert any(event['tool'] == 'model_retry' and '超长' in event['note'] for event in events)
    assert any(event['tool'] == 'context_compact' for event in events)
    # 长正文落了存档；archive_read 从存档取回原文（result_preview 可见）。
    archived = client.app.state.repo.read_tool_archive(run['conversation_id'], 'batch0')
    assert archived and '大模型检索增强' in archived
    reads = [event for event in events if event['tool'] == 'archive_read']
    assert reads and reads[0]['status'] == 'completed' and '大模型检索增强' in (reads[0].get('result_preview') or '')
    assert any(event['tool'] == 'project_read' for event in events)


def test_output_truncation_escalates_budget_and_restarts(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(agent_module, 'MODEL_RETRY_BACKOFF_BASE', 0.0)
    truncated = [sse_chunk({'delta': {'reasoning_content': '长思考'}}),
                 sse_chunk({'delta': {'content': '半截回答'}}),
                 sse_chunk({'delta': {}, 'finish_reason': 'length'}), 'data: [DONE]']
    streams = [truncated, stream_final('升级预算后的完整回答。')]
    calls = stream_scripted_urls(monkeypatch, streams)
    run = client.post('/api/chat/runs', json={'message': '截断升级', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    stored = stored_messages(client, run['conversation_id'])
    assert stored[1]['content'] == '升级预算后的完整回答。' and not stored[1].get('failed')
    retries = retry_events(stored)
    assert len(retries) == 1 and '提升到' in retries[0]['note']
    assert calls[1]['body']['max_tokens'] == 16000
    # 已流出的半截先重置：SSE 出现第二个 start 帧（前端实时气泡整体重置，不拼接两段）。
    frames = read_stream(client, run['run_id'])
    assert [kind for kind, _ in frames].count('start') == 2


def test_transport_error_classification_unit():
    """classify_transport_error：状态码优先、body 词表兜底、Retry-After 透传、未识别保持通用。"""

    def http_error(status, body='', headers=None):
        request = httpx.Request('POST', 'https://fixture.example/v1/chat/completions')
        response = httpx.Response(status, request=request, text=body, headers=headers or {})
        return httpx.HTTPStatusError('boom', request=request, response=response)

    assert isinstance(classify_transport_error(http_error(429, 'slow down'), 'x'), RateLimitError)
    retry = classify_transport_error(http_error(429, '', headers={'retry-after': '3'}), 'x')
    assert isinstance(retry, RateLimitError) and retry.retry_after == 3.0
    assert isinstance(classify_transport_error(http_error(529, ''), 'x'), OverloadedError)
    assert isinstance(classify_transport_error(http_error(503, 'upstream overloaded'), 'x'), OverloadedError)
    assert isinstance(classify_transport_error(http_error(400, 'maximum context length exceeded'), 'x'), ContextOverflowError)
    assert isinstance(classify_transport_error(http_error(400, '上下文长度超出限制'), 'x'), ContextOverflowError)
    plain = classify_transport_error(http_error(400, 'bad request'), 'x')
    assert isinstance(plain, TransportStatusError)
    assert not isinstance(plain, (RateLimitError, OverloadedError, ContextOverflowError))
    assert classify_transport_error(ValueError('不是HTTP错误'), 'x') is None
