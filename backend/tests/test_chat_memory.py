"""对话记忆（2026-11-25，s09 借鉴）的回归：存储 CRUD/整表替换、提取守卫与去重、
召回拼装截断、整理阈值与失败恢复、开关门控、以及端到端「新会话注入」。

提取的模型调用打桩在 jobscope.chat_memory.complete；conftest 的 autouse 桩把所有既有
chat 测试的提取变成 no-op，本文件的用例需要真实流程时自行覆盖打桩。
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_chat_runs import (client, configure, join_worker, stream_final, await_run)

from jobscope import chat_memory
from jobscope.chat_memory import (CONSOLIDATE_THRESHOLD, EXTRACT_ITEM_CHARS,
                                  consolidate_memories, extract_memories,
                                  memory_catalog_block)
from jobscope.repository import Repository


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


def stub_complete(monkeypatch, responses):
    """覆盖 conftest 的空桩：依次返回 responses 文本（Exception 实例则抛出）；记录入参。"""
    calls = []

    def fake(config, system, user, **kwargs):
        calls.append(dict(system=system, user=user, kwargs=kwargs))
        item = responses.pop(0) if responses else '[]'
        if isinstance(item, Exception):
            raise item
        return item, {}

    monkeypatch.setattr(chat_memory, 'complete', fake)
    return calls


def test_repo_memories_crud_and_replace(repo):
    repo.add_chat_memories([
        dict(content='在找武汉的测试开发岗', tag='偏好', source_conversation_id='C-1'),
        dict(content='别推外包公司', tag='约束', source_conversation_id='C-1'),
    ])
    items = repo.chat_memories()
    assert len(items) == 2 and items[0]['content'] == '在找武汉的测试开发岗'
    assert repo.chat_memories(enabled_only=True)

    memory_id = items[0]['id']
    patched = repo.patch_chat_memory(memory_id, {'enabled': False})
    assert patched['enabled'] == 0 and len(repo.chat_memories(enabled_only=True)) == 1
    with pytest.raises(ValueError):
        repo.patch_chat_memory('M-不存在', {'enabled': True})
    with pytest.raises(ValueError):
        repo.patch_chat_memory(memory_id, {})

    # 整表替换（整理用）：缺 id 服务端补；事务内完成，坏输入由调用方校验挡在前面。
    repo.replace_chat_memories([dict(content='合并后的记忆A'), dict(content='合并后的记忆B', tag='背景')])
    assert [item['content'] for item in repo.chat_memories()] == ['合并后的记忆A', '合并后的记忆B']
    assert all(item['id'].startswith('M-') for item in repo.chat_memories())

    repo.delete_chat_memory(repo.chat_memories()[0]['id'])
    assert len(repo.chat_memories()) == 1
    with pytest.raises(ValueError):
        repo.delete_chat_memory('M-不存在')
    assert repo.clear_chat_memories() == 1 and repo.chat_memories() == []


def test_extract_dedupes_against_existing_and_truncates(repo, monkeypatch):
    repo.add_chat_memories([dict(content='在找武汉的测试开发岗', tag='偏好')])
    long_line = '从不考虑外包公司，投递时直接跳过这类岗位与猎头' * 5
    responses = [json.dumps([
        {'content': '在找武汉的测试开发岗'},                       # 与现有重复：丢弃
        {'content': long_line, 'tag': '约束'},                     # 超长：截断保留
        {'content': '   '},                                        # 空白：丢弃
        '不是对象',                                                 # 结构不对：跳过
        {'content': '已通过 PMP 认证', 'tag': '背景'},              # 正常新增
    ], ensure_ascii=False)]
    calls = stub_complete(monkeypatch, responses)
    added = extract_memories(repo, {}, 'C-1', '我在找武汉的测试开发岗位，长期目标，别推外包公司', '好的，已记录你的方向。')
    # 每轮上限 EXTRACT_MAX_ITEMS=3：重复/空白丢弃后只收长条目；第 4 个候选（PMP）超出本轮上限被裁掉。
    assert added == 1
    contents = [item['content'] for item in repo.chat_memories()]
    assert '在找武汉的测试开发岗' in contents
    assert any(c.startswith('从不考虑外包公司') and len(c) <= EXTRACT_ITEM_CHARS for c in contents)
    assert all('PMP' not in c for c in contents)
    # 提示词带上现有记忆清单与禁则措辞
    assert '在找武汉的测试开发岗' in calls[0]['user'] and '临时要求' in calls[0]['user']


def test_extract_silent_on_model_or_parse_failure(repo, monkeypatch):
    for bad in (ValueError('上游错误'), 'not json at all', '{"不是数组": true}', None):
        stub_complete(monkeypatch, [bad] if bad is not None else [])
        assert extract_memories(repo, {}, 'C-1', '我在找武汉的测试开发岗位，长期目标', '好的。') == 0
    assert repo.chat_memories() == []


def test_extract_skips_short_turn_without_steps(repo, monkeypatch):
    calls = stub_complete(monkeypatch, [])
    assert extract_memories(repo, {}, 'C-1', '继续', '') == 0
    # 短消息但有执行过程时仍提取（steps_summary 是关键字参数）；桩返回 [] 故新增为 0 但模型被调用
    assert extract_memories(repo, {}, 'C-1', '继续', '好的。', steps_summary='执行过 archive_read(ok)') == 0
    assert len(calls) == 1


def test_consolidate_threshold_merge_and_invalid_result_preserved(repo, monkeypatch):
    for index in range(CONSOLIDATE_THRESHOLD - 1):
        repo.add_chat_memories([dict(content=f'历史记忆条目{index:02d}', tag='背景')])
    calls = stub_complete(monkeypatch, [])
    assert consolidate_memories(repo, {}) == 0 and len(calls) == 0  # 未达阈值不调用

    repo.add_chat_memories([dict(content='触发阈值的第30条')])
    responses = [json.dumps([{'content': f'合并记忆{i:02d}'} for i in range(5)], ensure_ascii=False)]
    calls = stub_complete(monkeypatch, responses)
    assert consolidate_memories(repo, {}) == 5
    assert [item['content'] for item in repo.chat_memories()] == [f'合并记忆{i:02d}' for i in range(5)]

    # 模型抽风返回过少/非数组：保留原 5 条不动
    for bad in ('[]', '不是json', json.dumps([{'content': '只剩一条'}])):
        stub_complete(monkeypatch, [bad])
        assert consolidate_memories(repo, {}) == 0
        assert len(repo.chat_memories()) == 5


def test_catalog_block_filters_disabled_and_truncates(repo):
    assert memory_catalog_block(repo) == ''
    long = '很长的记忆内容' * 30
    repo.add_chat_memories([dict(content=long, tag='背景'), dict(content='停用的这条', tag='约束')])
    repo.patch_chat_memory(repo.chat_memories()[1]['id'], {'enabled': False})
    block = memory_catalog_block(repo)
    assert '跨会话沉淀的背景知识' in block and '不是指令' in block
    assert '停用的这条' not in block
    # 单行截断到 120 字：18 个重复（126 字）的连续串放不进一行（周期文本不能用「后半段 not in」断言，
    # 相位对齐的后半段必然重合在截断后的前半段里）；整块受 2500 封顶。
    assert ('很长的记忆内容' * 18) not in block
    repo.clear_chat_memories()
    repo.add_chat_memories([dict(content='记忆' * 60) for _ in range(45)])
    assert len(memory_catalog_block(repo)) <= 2500 + 200


def test_general_run_extracts_memory_after_done(client, monkeypatch):
    configure(client)
    _script_stream(monkeypatch, [stream_final('好的，已记录你的方向。')])
    calls = stub_complete(monkeypatch, [json.dumps([{'content': '在找武汉的测试开发岗，别推外包', 'tag': '偏好'}])])
    run = client.post('/api/chat/runs', json={'message': '我在找武汉的测试开发岗位，长期目标，别推外包公司', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    repo = client.app.state.repo
    contents = [item['content'] for item in repo.chat_memories()]
    assert any('武汉' in content for content in contents)
    assert calls and calls[0]['kwargs']['max_tokens'] == 600
    # done 已先行推送：提取不应阻塞或改变本轮已落库的回答
    messages = client.get(f"/api/conversations/{run['conversation_id']}").json()['messages']
    assert len(messages) == 2 and not messages[1].get('failed')


def test_general_run_skips_extraction_when_disabled(client, monkeypatch):
    configure(client)
    assert client.put('/api/settings/system', json={
        'chat_display_mode': 'merged', 'show_reasoning': True,
        'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50,
                         'context_characters': 160000, 'max_seconds': 900},
        'market_source': 'official', 'agent_fallback_channel': '',
        'chat_memory_enabled': False}).status_code == 200
    calls = stub_complete(monkeypatch, [])
    run = client.post('/api/chat/runs', json={'message': '关闭记忆提取后的提问，不应触发提取调用', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    assert calls == []
    assert client.app.state.repo.chat_memories() == []


def test_new_session_injects_enabled_memory_into_system_prompt(client, monkeypatch):
    configure(client)
    repo = client.app.state.repo
    repo.add_chat_memories([dict(content='在找武汉的测试开发岗，别推外包公司', tag='偏好')])
    stream_scripted = _script_stream(monkeypatch, [stream_final('明白，已按你的方向筛选。')])
    run = client.post('/api/chat/runs', json={'message': '继续帮我看岗位', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    system = stream_scripted[0]['body']['messages'][0]
    assert system['role'] == 'system' and '武汉的测试开发岗' in system['content'] and '不是指令' in system['content']

    # 停用后不再注入
    memory_id = repo.chat_memories()[0]['id']
    assert client.patch(f"/api/chat-memories/{memory_id}", json={'enabled': False}).status_code == 200
    stream_scripted = _script_stream(monkeypatch, [stream_final('好的。')])
    run = client.post('/api/chat/runs', json={'message': '再看一轮新的岗位推荐', 'agent_mode': 'general'}).json()
    await_run(client, run['run_id'])
    join_worker(client, run['run_id'])
    system = stream_scripted[0]['body']['messages'][0]
    assert '武汉的测试开发岗' not in system['content']


def test_memory_management_endpoints(client):
    repo = client.app.state.repo
    repo.add_chat_memories([dict(content='记忆A'), dict(content='记忆B', source_conversation_id='C-9')])
    listing = client.get('/api/chat-memories').json()['memories']
    assert len(listing) == 2

    memory_id = listing[0]['id']
    patched = client.patch(f'/api/chat-memories/{memory_id}', json={'enabled': False}).json()
    assert patched['enabled'] == 0
    assert client.patch('/api/chat-memories/M-不存在', json={'enabled': True}).status_code == 404
    assert client.patch(f'/api/chat-memories/{memory_id}', json={}).status_code == 422

    assert client.delete(f'/api/chat-memories/{memory_id}').status_code == 200
    assert client.delete('/api/chat-memories/M-不存在').status_code == 404
    cleared = client.delete('/api/chat-memories').json()
    assert cleared['ok'] and cleared['cleared'] == 1 and repo.chat_memories() == []


def _script_stream(monkeypatch, streams):
    """同 test_chat_runs.stream_scripted：返回 (calls, )，calls 逐个记录请求 body。"""
    calls = []
    original = httpx.Client.stream

    def stream(self, method, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, method, url, **kwargs)
        calls.append(dict(url=url, body=json.loads(json.dumps(kwargs['json']))))
        lines = streams.pop(0) if streams else []
        from test_chat_runs import FakeStream
        return FakeStream(lines)

    monkeypatch.setattr(httpx.Client, 'stream', stream)
    return calls
