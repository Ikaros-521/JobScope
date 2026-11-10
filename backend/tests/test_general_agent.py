import json
import sys
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


def scripted(monkeypatch, replies):
    calls = []
    original = httpx.Client.post
    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        calls.append(json.loads(json.dumps(kwargs['json'])))
        body = replies.pop(0)
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    return calls


def tool(name, args, call_id='call1'):
    return {'choices': [{'message': {'content': None, 'tool_calls': [dict(id=call_id, type='function', function=dict(name=name, arguments=json.dumps(args)))]}}]}


def final(text='可以先准备一个 Agent 项目，再练习工具调用。'):
    return {'choices': [{'message': {'content': text}}]}


def test_general_chat_without_project_evidence_and_scoped_isolation(client, monkeypatch):
    configure(client)
    calls = scripted(monkeypatch, [final()])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '帮我制定 Agent 学习计划'})
    assert result.status_code == 200
    assert result.json()['mode'] == 'agent' and result.json()['trace']['strategy'] == 'agent'
    assert calls[0]['tools'] and '简历' not in calls[0]['messages'][-1]['content']
    assert client.post('/api/chat', json={'company': '甲', 'agent_mode': 'general', 'message': 'hello'}).status_code == 400


def test_project_tool_loop_returns_citations(client, monkeypatch):
    configure(client)
    job = client.post('/api/import/job', json=dict(title='Agent工程师', company='甲', description='Python RAG', salary='20-30K')).json()['job']
    eid = job['evidence_ids'][0]
    calls = scripted(monkeypatch, [tool('project_search', {'query': 'Python'}), final(f'岗位涉及 Python [证据ID：{eid}]')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': 'Python要求'}).json()
    assert result['citations'][0]['id'] == eid
    assert calls[1]['messages'][-1]['role'] == 'tool'
    assert any(e['tool'] == 'project_search' for e in result['trace']['events'])


def test_company_archive_tools_load_reference_and_evidence_on_demand(client, monkeypatch):
    configure(client)
    # 落库即登记主体 → 本地覆盖度信号 → 参考报告：档案三件套齐了。
    client.post('/api/import/job', json=dict(title='后端工程师', company='甲公司', description='Python RAG', salary='20-30K'))
    assert client.post('/api/companies/甲公司/signals/collect', json={'source': 'jobscope_jobs'}).status_code == 200
    report = client.post('/api/companies/甲公司/reliability').json()
    assert report['evidence_ids'], '本地覆盖度信号应把岗位证据带进报告引用'
    eid = report['evidence_ids'][0]
    calls = scripted(monkeypatch, [tool('company_profile', {'company': '甲公司'}),
                                   tool('company_detail', {'company': '甲公司', 'section': 'evidence'}),
                                   final(f'档案摘要已读，引用其材料 [证据ID：{eid}]')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '甲公司怎么样'}).json()
    assert result['mode'] == 'agent'
    profile = json.loads(calls[1]['messages'][-1]['content'])
    assert profile['found'] is True
    assert profile['reference']['nature'] == '主观参考' and profile['company']['name'] == '甲公司'
    assert {s['section'] for s in profile['sections']} == {'signals', 'history', 'evidence'}
    detail = json.loads(calls[2]['messages'][-1]['content'])
    assert detail['total'] >= 1 and detail['evidence'][0]['id'].startswith('E-')
    assert 'text' not in detail['evidence'][0], '证据索引只给元数据，正文按需用 project_read 读'
    # profile 把报告引用的证据静默登记进可引用集合：最终回答引用它应通过引用校验。
    assert result['citations'][0]['id'] == eid


def test_company_profile_unknown_company_reports_gap_without_writing_rows(client, monkeypatch):
    configure(client)
    calls = scripted(monkeypatch, [tool('company_profile', {'company': '不存在公司'}), final('资料库没有这家公司。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '不存在公司怎么样'}).json()
    assert result['mode'] == 'agent'
    profile = json.loads(calls[1]['messages'][-1]['content'])
    assert profile['found'] is False
    # 查询不写行：只读口径同样适用于 Agent 工具。
    assert client.get('/api/companies/不存在公司/reliability').json()['report'] is None


def test_skills_are_imported_and_loaded_on_demand(client, monkeypatch):
    text = '---\nname: interview-coach\ndescription: 面试演练与反馈\n---\n每次只问一个问题，使用 STAR 结构反馈。'
    saved = client.post('/api/agent/skills', json={'content': text}).json()
    assert saved['name'] == 'interview-coach'
    assert client.post('/api/agent/skills', json={'content': 'bad'}).status_code == 422
    configure(client)
    calls = scripted(monkeypatch, [tool('skill_read', {'skill_id': saved['id']}), final('我们从项目背景开始。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '模拟面试'}).json()
    assert result['mode'] == 'agent'
    assert 'STAR' not in calls[0]['messages'][0]['content']
    assert 'STAR' in calls[1]['messages'][-1]['content']


def test_unknown_tools_rejected_and_loop_bounded(client, monkeypatch):
    configure(client)
    calls = scripted(monkeypatch, [tool('shell_exec', {'command': 'anything'}, f'call{i}') for i in range(8)])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '测试'}).json()
    assert len(calls) <= 8 and result['trace']['status'] == 'completed'
    assert result['trace']['degraded'] and '上限' in result['answer']


def test_anthropic_native_tool_roundtrip(client, monkeypatch):
    configure(client, 'anthropic')
    calls = scripted(monkeypatch, [
        {'content': [{'type': 'tool_use', 'id': 'tool1', 'name': 'project_search', 'input': {'query': 'Python'}}], 'stop_reason': 'tool_use'},
        {'content': [{'type': 'text', 'text': '当前资料不足，可以先补充岗位。'}], 'stop_reason': 'end_turn'},
    ])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': 'Python'}).json()
    assert result['mode'] == 'agent'
    assert calls[0]['tools'][0]['input_schema']
    assert calls[1]['messages'][-1]['content'][0]['type'] == 'tool_result'


def test_mcp_config_redacts_secrets_and_requires_tested_tool_allowlist(client, monkeypatch):
    from jobscope.extensions import MCPClient
    async def inspect(config):
        return [dict(name='lookup', description='Search', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']}, annotations={'readOnlyHint': True})]
    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    saved = client.post('/api/agent/mcp', json=dict(name='资料工具', transport='http', url='http://127.0.0.1:9000/mcp', token='fixture-credential')).json()
    assert saved['token_set'] and 'fixture-credential' not in json.dumps(saved)
    tested = client.post(f"/api/agent/mcp/{saved['id']}/test").json()
    assert tested['tools'][0]['name'] == 'lookup'
    updated = client.patch(f"/api/agent/mcp/{saved['id']}", json={'enabled': True, 'allowed_tools': ['lookup'], 'auto_tools': ['lookup']})
    assert updated.status_code == 200
    assert client.patch(f"/api/agent/mcp/{saved['id']}", json={'allowed_tools': ['invented']}).status_code == 422
    assert client.patch(f"/api/agent/mcp/{saved['id']}", json={'url': 'https://another.example/mcp'}).status_code == 422


def test_mcp_tool_approval_is_single_use_and_resumable(client, monkeypatch):
    from jobscope.extensions import MCPClient
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
    from jobscope.extensions import tool_alias
    configure(client)
    scripted(monkeypatch, [tool(tool_alias(server['id'], 'lookup'), {'q': 'test'}), final('根据工具结果，可以继续核实。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '搜索外部数据'}).json()
    assert result['trace']['status'] == 'awaiting_approval' and not executed
    run_id = result['trace']['id']
    approval = {'approved': True, 'approval_id': result['pending_approval']['approval_id']}
    approved = client.post(f'/api/agent/runs/{run_id}/approval', json=approval).json()
    assert approved['mode'] == 'agent' and executed == [('lookup', {'q': 'test'})]
    assert client.post(f'/api/agent/runs/{run_id}/approval', json=approval).status_code == 409


def test_real_stdio_mcp_sdk_initialize_list_and_call():
    import anyio
    from jobscope.extensions import MCPClient
    config = dict(transport='stdio', command=sys.executable, args=[str(Path(__file__).resolve().parents[2] / 'scripts' / 'fixture_mcp.py')], env={}, token='')
    tools = anyio.run(MCPClient.discover, config)
    assert tools[0]['name'] == 'lookup_topic'
    result = anyio.run(MCPClient.call, config, 'lookup_topic', {'query': 'Agent'})
    assert not result['is_error'] and 'MCP fixture result: Agent' in result['text']


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
def test_mcp_schemas_cannot_resolve_references(keyword):
    from jobscope.extensions import validate_schema
    with pytest.raises(ValueError, match='引用'):
        validate_schema({'type': 'object', 'properties': {'x': {keyword: 'https://outside.example/schema'}}})


def test_local_schema_definitions_work_without_network():
    from jobscope.extensions import validate_schema, schema_validator
    value = validate_schema({'type': 'object', '$defs': {'item': {'type': 'string'}}, 'properties': {'q': {'$ref': '#/$defs/item'}}})
    assert not list(schema_validator(value).iter_errors({'q': 'topic'}))
    assert list(schema_validator(value).iter_errors({'q': 1}))


def test_approval_is_bound_to_each_call_and_config_changes_invalidate(client, monkeypatch):
    from jobscope.extensions import MCPClient, tool_alias
    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]
    executed = []
    async def invoke(config, name, args):
        executed.append(args)
        return {'text': 'result', 'is_error': False}
    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='逐次确认', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=['lookup']))
    configure(client)
    alias = tool_alias(server['id'], 'lookup')
    response = tool(alias, {'q': 'one'})
    response['choices'][0]['message']['tool_calls'].append(tool(alias, {'q': 'two'}, 'call2')['choices'][0]['message']['tool_calls'][0])
    scripted(monkeypatch, [response, final()])
    pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', message='lookup')).json()
    run_id = pending['trace']['id']
    first = dict(approved=True, approval_id=pending['pending_approval']['approval_id'])
    second = client.post(f'/api/agent/runs/{run_id}/approval', json=first).json()
    assert len(executed) == 1 and second['pending_approval']['arguments'] == {'q': 'two'}
    assert client.post(f'/api/agent/runs/{run_id}/approval', json=first).status_code == 409
    assert len(executed) == 1
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=False))
    result = client.post(f'/api/agent/runs/{run_id}/approval', json=dict(approved=True, approval_id=second['pending_approval']['approval_id']))
    assert result.status_code == 200 and result.json()['trace']['degraded'] and len(executed) == 1


def test_skill_and_unknown_citations_never_grant_execution_authority(client, monkeypatch):
    configure(client)
    scripted(monkeypatch, [final('事实 [E-invented]')])
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', message='回答')).json()
    assert '阻止展示' in result['answer'] and not result['citations']


def test_resume_access_taints_later_turns_even_with_auto_tool(client, monkeypatch):
    from jobscope.extensions import MCPClient, tool_alias
    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}})]
    executed = []
    async def invoke(config, name, args):
        executed.append(args)
        return dict(text='result', is_error=False)
    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='自动工具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=['lookup'], auto_tools=['lookup']))
    configure(client)
    client.put('/api/resume', json=dict(text='Python开发', format='text'))
    scripted(monkeypatch, [tool('resume_read', {}), final('你的简历包含Python开发。'), tool(tool_alias(server['id'], 'lookup'), {'q': 'Python开发'})])
    first = client.post('/api/chat', json=dict(scope='global', agent_mode='general', message='读简历')).json()
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=first['conversation_id'], message='查资料')).json()
    assert result['pending_approval'] and not executed


def test_approved_tool_failure_recovers_and_denial_does_not_execute(client, monkeypatch):
    from jobscope.extensions import MCPClient, tool_alias
    async def inspect(config):
        return [dict(name='lookup', description='Lookup', inputSchema={'type': 'object'})]
    calls = []
    async def fail(config, name, args):
        calls.append(name)
        raise ValueError('fixture-secret-error')
    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(fail))
    server = client.post('/api/agent/mcp', json=dict(name='失败工具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=['lookup']))
    configure(client)
    scripted(monkeypatch, [tool(tool_alias(server['id'], 'lookup'), {}), final('工具不可用，请稍后核实。'), tool(tool_alias(server['id'], 'lookup'), {}), final('已按你的选择继续。')])
    for approved in [True, False]:
        pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', message='测试')).json()
        result = client.post(f"/api/agent/runs/{pending['trace']['id']}/approval", json=dict(approved=approved, approval_id=pending['pending_approval']['approval_id']))
        assert result.status_code == 200 and not result.json()['pending_approval']
        assert 'fixture-secret-error' not in result.text
    assert len(calls) == 1
