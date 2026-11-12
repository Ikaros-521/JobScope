"""Agent 内置采集工具（company_web_lookup / collect_jobs）的范围纪律与状态透传。

不碰网络、不开浏览器：company_lookup.collect 与 connectors 模块全部打桩，
断言的是 Agent 工具层如何调用管线、如何按会话范围放行/拒绝、如何转述采集状态。
"""
import copy
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.agent_scope import ScopeBoundaryError
from jobscope.general_agent import GeneralAgent
from jobscope.main import create_app
from jobscope.repository import Repository


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr('jobscope.general_agent.model_configured', lambda: True)
    return GeneralAgent(Repository(tmp_path))


@pytest.fixture
def job(agent):
    return agent.repo.save_job(dict(title='Java 工程师', company='甲公司', description='岗位原文',
                                    salary='20-30K', external_id='own'), 'user_import')


def scope_for(kind, job=None):
    if kind == 'global':
        return dict(kind=kind)
    scope = dict(kind=kind, company='甲公司')
    if kind == 'job':
        scope['job_id'] = job['id']
    return scope


def state_for(agent, scope):
    from jobscope.extensions import fingerprint
    return dict(scope=dict(scope), scope_binding=fingerprint(scope), run=dict(scope=dict(scope)), evidence={}, metrics=None, actions=[])


def task_count(agent):
    with agent.repo.connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]


def stub_lookup(monkeypatch, outcome):
    def collect(channel, company, *, browser_module=None, data_dir=None):
        return outcome
    monkeypatch.setattr('jobscope.company_lookup.collect', collect)


CARD = dict(name='甲公司', url='https://www.tianyancha.com/company/1',
            fields={'法定代表人': '张三', '注册资本': '1000万元人民币'}, text='甲公司\n法定代表人：张三')


def test_web_lookup_saves_cards_and_returns_citable_evidence(agent, monkeypatch):
    stub_lookup(monkeypatch, dict(status='completed', message='天眼查公开页解析到 1 家公司；仅页面可见的有限字段，已按公司名归档。', cards=[CARD]))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'company_web_lookup', dict(channel='tyc', company='甲公司'))
    assert result['status'] == 'completed' and result['saved'] == 1
    assert len(result['cards']) == 1 and result['cards'][0]['fields']['法定代表人'] == '张三'
    cited = result['citable_evidence']
    assert len(cited) == 1 and cited[0]['id'].startswith('E-')
    # 登记进本轮可引用集合，且证据/任务都已真实落库
    assert cited[0]['id'] in state['evidence']
    assert agent.repo.get_evidence(cited[0]['id'])['kind'] == 'web_company_profile'
    assert agent.repo.task(result['task_id'])['status'] == 'completed'


@pytest.mark.parametrize('status,message', [
    ('needs_verification', '天眼查要求登录或人工校验；已把该页留在项目浏览器前台，请手动完成后重试。'),
    ('backoff', '另一个采集任务正占用项目浏览器，请稍后重试。'),
    ('needs_login', '请先启动项目浏览器并打开天眼查完成登录或验证。'),
])
def test_web_lookup_passes_status_through_without_saving(agent, monkeypatch, status, message):
    stub_lookup(monkeypatch, dict(status=status, message=message, cards=[]))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'company_web_lookup', dict(channel='tyc', company='甲公司'))
    assert result['status'] == status and message in result['message']
    assert result['saved'] == 0 and result['citable_evidence'] == [] and state['evidence'] == {}


def test_web_lookup_out_of_scope_company_rejected_before_task(agent, job, monkeypatch):
    stub_lookup(monkeypatch, dict(status='completed', message='不应执行', cards=[CARD]))
    state = state_for(agent, scope_for('company', job))
    with pytest.raises(ScopeBoundaryError):
        agent._builtin(state, 'company_web_lookup', dict(channel='qcc', company='乙公司'))
    assert task_count(agent) == 0


def test_collect_jobs_refused_in_job_scope_without_task(agent, job):
    state = state_for(agent, scope_for('job', job))
    result = agent._builtin(state, 'collect_jobs', dict(source='boss', query='甲公司 Java'))
    assert result['status'] == 'out_of_scope' and result['executed'] is False
    assert 'request_scope_expansion' in result['note'] and task_count(agent) == 0


def test_collect_jobs_company_scope_requires_company_in_query(agent, job):
    state = state_for(agent, scope_for('company', job))
    result = agent._builtin(state, 'collect_jobs', dict(source='liepin', query='Java'))
    assert result['status'] == 'out_of_scope' and '甲公司' in result['note'] and task_count(agent) == 0


def stub_connectors(monkeypatch, jobs):
    fake = types.ModuleType('jobscope.connectors')
    fake.BROWSER_SOURCES = ()

    def collect(source, query, location='', limit=10, cache_dir=None):
        return dict(status='completed', message=f'公开接口取得 {len(jobs)} 条岗位',
                    jobs=jobs, evidence=[])

    fake.collect = collect
    monkeypatch.setitem(sys.modules, 'jobscope.connectors', fake)


@pytest.mark.parametrize('kind', ['company', 'global'])
def test_collect_jobs_persists_and_returns_compact_summary(agent, job, monkeypatch, kind):
    stub_connectors(monkeypatch, [dict(title='Java 工程师', company='甲公司', location='杭州', salary='20-30K',
                                      url=f'https://job/{index}', external_id=str(index)) for index in range(3)])
    state = state_for(agent, scope_for(kind, job))
    result = agent._builtin(state, 'collect_jobs', dict(source='liepin', query='甲公司 Java', limit=20))
    assert result['status'] == 'completed' and result['saved'] == 3
    assert result['companies'] == ['甲公司']
    assert result['sample'][0]['title'] == 'Java 工程师'
    # 只回摘要，不把全量岗位塞进上下文
    assert all(len(job) == 4 for job in result['sample'])
    assert agent.repo.task(result['task_id'])['count'] >= 3
    assert 'project_search' in result['note']


def test_system_prompt_routes_business_facts_to_project_lookup(agent, monkeypatch):
    captured = []

    def turn(config, messages, tools):
        captured.append(copy.deepcopy(messages))
        return dict(role='assistant', content='好的'), {}

    monkeypatch.setattr('jobscope.general_agent.tool_turn', turn)
    conversation_id = agent.repo.create_conversation(dict(kind='global'), mode='general')
    agent.start('继续', [], conversation_id)
    system = captured[0][0]['content']
    assert 'company_web_lookup' in system and 'collect_jobs' in system
    assert '不要用浏览器MCP自己翻企业信息网站' in system
    # 无搜索MCP时，工商信息仍指向项目快查而不是一起判死
    assert '项目快查与采集' in system and '优先调用company_web_lookup' in system


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def configure(client):
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture')}).status_code == 200


def scripted(monkeypatch, replies):
    calls = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        calls.append(json.loads(json.dumps(kwargs['json'])))
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    return calls


def tool_reply(name, args, call_id='call1'):
    return {'choices': [{'message': {'content': None, 'tool_calls': [dict(id=call_id, type='function', function=dict(name=name, arguments=json.dumps(args)))]}}]}


def test_chat_loop_executes_web_lookup_tool(client, monkeypatch):
    configure(client)
    stub_lookup(monkeypatch, dict(status='completed', message='天眼查公开页解析到 1 家公司', cards=[CARD]))
    calls = scripted(monkeypatch, [tool_reply('company_web_lookup', dict(channel='tyc', company='甲公司')),
                                   {'choices': [{'message': {'content': '已查到甲公司。'}}]}])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '查一下甲公司是什么公司'})
    assert result.status_code == 200
    events = result.json()['trace']['events']
    assert any(event['tool'] == 'company_web_lookup' and event['status'] == 'completed' for event in events)
    tool_message = calls[1]['messages'][-1]
    assert tool_message['role'] == 'tool'
    assert json.loads(tool_message['content'])['citable_evidence']
    # 新工具已注册进模型可见的工具目录（openai wire 格式外面包一层 function）
    assert any(item['function']['name'] == 'company_web_lookup' for item in calls[0]['tools'])
    assert any(item['function']['name'] == 'collect_jobs' for item in calls[0]['tools'])
