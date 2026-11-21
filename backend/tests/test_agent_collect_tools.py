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


def stub_channels(monkeypatch, direct, browser_result):
    """直连 connectors 与浏览器通道一起打桩，按调用顺序记录，验证降级路由。"""
    calls = []

    fake_connectors = types.ModuleType('jobscope.connectors')
    fake_connectors.BROWSER_SOURCES = ()

    def fake_direct(source, query, location='', limit=10, cache_dir=None):
        calls.append(('direct', source, query, location, limit))
        return direct

    fake_connectors.collect = fake_direct

    fake_browser = types.ModuleType('jobscope.browser')
    fake_browser.PAGES = {'boss': dict(name='BOSS 直聘')}

    def fake_browser_collect(source, query, location='', limit=20, data_dir=None, progress=None):
        calls.append(('browser', source, query, location, limit))
        return browser_result

    fake_browser.collect = fake_browser_collect
    monkeypatch.setitem(sys.modules, 'jobscope.connectors', fake_connectors)
    monkeypatch.setitem(sys.modules, 'jobscope.browser', fake_browser)
    return calls


BOSS_BLOCKED = dict(status='needs_verification', message='BOSS 返回访问验证状态；请使用项目浏览器正常登录并完成人工验证后采集。', jobs=[], evidence=[])
BOSS_BROWSER_JOBS = [dict(title='Java 工程师', company='甲公司', location='杭州', salary='20-30K',
                          url='https://job/boss1', external_id='boss1')]


def test_collect_jobs_boss_degrades_to_browser_channel(agent, job, monkeypatch):
    """boss 直连被风控时自动降级浏览器通道并正常落库，而不是只回一句人工验证提示。"""
    calls = stub_channels(monkeypatch, BOSS_BLOCKED,
                          dict(status='completed', message='BOSS直聘浏览器采集：已读取 BOSS 搜索列表 1 条。',
                               jobs=BOSS_BROWSER_JOBS, evidence=[]))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'collect_jobs', dict(source='boss', query='甲公司 Java', limit=20))
    assert calls == [('direct', 'boss', '甲公司 Java', '', 20), ('browser', 'boss', '甲公司 Java', '', 20)]
    assert result['status'] == 'completed' and result['saved'] == 1
    assert agent.repo.task(result['task_id'])['status'] == 'completed'


@pytest.mark.parametrize('blocked_status', ['needs_verification', 'needs_login'])
def test_collect_jobs_degrade_covers_both_blocked_statuses(agent, job, monkeypatch, blocked_status):
    """直连要求登录或验证都触发降级；浏览器通道也要人工时，状态与文案原样透传。"""
    calls = stub_channels(monkeypatch, dict(status=blocked_status, message='拦截', jobs=[], evidence=[]),
                          dict(status='needs_verification',
                               message='BOSS直聘浏览器采集：未收到岗位响应。请查看项目浏览器是否需要登录或人工验证；不会自动反复请求。',
                               jobs=[], evidence=[]))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'collect_jobs', dict(source='boss', query='甲公司 Java'))
    assert len(calls) == 2 and calls[1][0] == 'browser'
    assert result['status'] == 'needs_verification' and '登录或人工验证' in result['message']
    assert result['saved'] == 0


def test_collect_jobs_direct_success_skips_browser_channel(agent, job, monkeypatch):
    """公开接口直连成功时不碰浏览器：降级只在被拦截后发生。"""
    calls = stub_channels(monkeypatch,
                          dict(status='completed', message='公开接口取得 1 条岗位', jobs=BOSS_BROWSER_JOBS, evidence=[]),
                          dict(status='failed', message='不应执行', jobs=[], evidence=[]))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'collect_jobs', dict(source='liepin', query='甲公司 Java'))
    assert calls == [('direct', 'liepin', '甲公司 Java', '', 20)]
    assert result['status'] == 'completed' and result['saved'] == 1


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
    # 补分工作流与四个新工具进了系统提示
    assert '补分工作流' in system and 'signal_collect' in system
    assert 'reliability_refresh' in system and 'job_deep_read' in system and 'signal_extract' in system


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
    assert any(item['function']['name'] == 'signal_collect' for item in calls[0]['tools'])
    assert any(item['function']['name'] == 'reliability_refresh' for item in calls[0]['tools'])


# ---- 补分工具：signal_collect / job_deep_read / signal_extract / reliability_refresh ----
# 与上方同一路数：管线全部打桩，断言工具层的范围纪律、状态透传与可引用证据登记。


def stub_collect_source(monkeypatch, outcome=None, raises=None):
    """打桩 reliability.collection.collect_source；Agent 分支在调用时才从模块取该函数。"""
    from jobscope.reliability import collection
    calls = []

    def collect_source(repo, data_dir, company, source_id, credit_code='', *, task=None):
        calls.append(dict(company=company, source=source_id))
        if raises:
            raise raises
        return outcome

    monkeypatch.setattr(collection, 'collect_source', collect_source)
    return calls


def test_signal_collect_refused_in_job_scope_without_task(agent, job, monkeypatch):
    calls = stub_collect_source(monkeypatch, raises=AssertionError('不应执行'))
    state = state_for(agent, scope_for('job', job))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='jobscope_jobs'))
    assert result['status'] == 'out_of_scope' and result['executed'] is False
    assert 'request_scope_expansion' in result['note'] and calls == [] and task_count(agent) == 0


def test_signal_collect_out_of_scope_company_rejected_before_pipeline(agent, job, monkeypatch):
    calls = stub_collect_source(monkeypatch, raises=AssertionError('不应执行'))
    state = state_for(agent, scope_for('company', job))
    with pytest.raises(ScopeBoundaryError):
        agent._builtin(state, 'signal_collect', dict(company='乙公司', source='aiqicha_web'))
    assert calls == [] and task_count(agent) == 0


def test_signal_collect_paid_source_refused(agent, monkeypatch):
    calls = stub_collect_source(monkeypatch, raises=AssertionError('不应执行'))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='qcc'))
    assert result['status'] == 'unsupported' and '付费接口' in result['message'] and calls == []


def test_signal_collect_unavailable_source_reports_skipped(agent, monkeypatch):
    from jobscope.reliability.collection import SourceUnavailable
    stub_collect_source(monkeypatch, raises=SourceUnavailable('入口已按用户要求暂停（2026-11-15）'))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='credit_china'))
    assert result['status'] == 'skipped' and '暂停' in result['message']
    # 停用/挂起来自管线内的 validate_source：不建任务、不落证据，只转述原因
    assert result['citable_evidence'] == [] and state['evidence'] == {} and task_count(agent) == 0


@pytest.mark.parametrize('status,message', [
    ('needs_login', '请先启动项目浏览器并完成登录。'),
    ('backoff', '另一个采集任务正占用项目浏览器，请稍后重试。'),
])
def test_signal_collect_passes_collector_status_through(agent, monkeypatch, status, message):
    task = agent.repo.create_task('signal_aiqicha_web', '甲公司')
    stub_collect_source(monkeypatch, dict(status=status, message=message, signals=[], task=task))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='aiqicha_web'))
    assert result['status'] == status and message in result['message']
    assert result['citable_evidence'] == [] and state['evidence'] == {}


def test_signal_collect_persists_signals_and_registers_citable_evidence(agent, monkeypatch):
    evidence = agent.repo.save_evidence(dict(title='甲公司 参保人数', company='甲公司', kind='disclosure',
                                             text='2024 年报：参保 120 人', url=''), 'aiqicha_web')
    # 管线真实契约：save_signal 落库后的 dict（model_dump 形态），不是对象——
    # 旧桩用 SimpleNamespace 编码了错误契约，掩盖了线上信号一落库就 AttributeError 的 bug。
    signal = dict(rule='insured_headcount_trend', value={'after': 120, 'before': 100},
                  evidence_id=evidence['id'])
    task = agent.repo.create_task('signal_aiqicha_web', '甲公司')
    calls = stub_collect_source(monkeypatch, dict(status='completed', message='已保存 1 条信号。',
                                                  signals=[signal], task=task))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='aiqicha_web'))
    assert result['status'] == 'completed' and result['signals'][0]['rule'] == 'insured_headcount_trend'
    assert result['citable_evidence'][0]['id'] == evidence['id'] and evidence['id'] in state['evidence']
    assert calls[0]['source'] == 'aiqicha_web'
    assert 'reliability_refresh' in result['note']


def test_signal_collect_enum_matches_free_source_registry():
    from jobscope.general_agent import BUILTINS
    from jobscope.reliability.sources import SIGNAL_SOURCES
    tool = next(item for item in BUILTINS if item['name'] == 'signal_collect')
    enum = tool['parameters']['properties']['source']['enum']
    # 枚举与登记表同源生成，付费接口（qcc/tyc http_api）不进对话工具
    assert enum == [key for key, spec in SIGNAL_SOURCES.items() if spec['tier'] == 'free']
    assert 'qcc' not in enum and 'tyc' not in enum


def boss_pending(repo, company, external_id):
    return repo.save_job(dict(title='Java 工程师', company=company, salary='20-30K', location='杭州',
                              url=f'https://www.zhipin.com/job_detail/{external_id}.html',
                              external_id=external_id, description_status='listing'), 'boss')


def stub_detail_collector(monkeypatch):
    from jobscope import job_details
    seen = []

    def collect_detail(job, data_dir):
        seen.append(job['id'])
        return dict(status='completed',
                    job={**job, 'description': '完整职责：负责服务端开发\n团队 30 人', 'description_status': 'complete'},
                    company_evidence=None)

    monkeypatch.setattr(job_details, 'collect_detail', collect_detail)
    return seen


def test_job_deep_read_refused_in_job_scope_without_task(agent, job, monkeypatch):
    seen = stub_detail_collector(monkeypatch)
    state = state_for(agent, scope_for('job', job))
    result = agent._builtin(state, 'job_deep_read', dict(company='甲公司', limit=3))
    assert result['status'] == 'out_of_scope' and result['executed'] is False
    assert 'request_scope_expansion' in result['note'] and seen == [] and task_count(agent) == 0


def test_job_deep_read_backoff_when_batch_lock_held(agent, job, monkeypatch):
    from jobscope import job_details
    seen = stub_detail_collector(monkeypatch)
    boss_pending(agent.repo, '甲公司', 'lock1')
    assert job_details.batch_lock.acquire(blocking=False)
    try:
        state = state_for(agent, scope_for('company', job))
        result = agent._builtin(state, 'job_deep_read', dict(company='甲公司', limit=3))
    finally:
        job_details.batch_lock.release()
    assert result['status'] == 'backoff' and seen == [] and task_count(agent) == 0


def test_job_deep_read_only_reads_scope_company_pending(agent, job, monkeypatch):
    own = boss_pending(agent.repo, '甲公司', 'a1')
    other = boss_pending(agent.repo, '乙公司', 'b1')
    seen = stub_detail_collector(monkeypatch)
    state = state_for(agent, scope_for('company', job))
    result = agent._builtin(state, 'job_deep_read', dict(company='甲公司', limit=5))
    assert result['status'] == 'completed' and result['saved'] == 1
    # 只有范围公司的待补岗位进了反查；乙公司的岗位不进
    assert seen == [own['id']] and other['id'] not in seen
    assert result['company_complete_jds'] == 1
    assert agent.repo.task(result['task_id'])['count'] == 1


def test_job_deep_read_skips_when_nothing_pending(agent, job, monkeypatch):
    seen = stub_detail_collector(monkeypatch)
    state = state_for(agent, scope_for('company', job))
    result = agent._builtin(state, 'job_deep_read', dict(company='甲公司'))
    assert result['status'] == 'skipped' and result['saved'] == 0 and seen == [] and task_count(agent) == 0
    assert 'collect_jobs' in result['note']


def stub_run_extract(monkeypatch, outcome):
    from jobscope.reliability import service
    calls = []

    def run_signal_extract(repo, company, credit_code='', *, limit=12, max_tokens=1200):
        calls.append(dict(company=company, limit=limit, max_tokens=max_tokens))
        return outcome

    monkeypatch.setattr(service, 'run_signal_extract', run_signal_extract)
    return calls


def test_signal_extract_refused_in_job_scope_without_task(agent, job, monkeypatch):
    calls = stub_run_extract(monkeypatch, dict())
    state = state_for(agent, scope_for('job', job))
    result = agent._builtin(state, 'signal_extract', dict(company='甲公司'))
    assert result['status'] == 'out_of_scope' and result['executed'] is False and calls == []


def test_signal_extract_returns_signals_and_citable_evidence(agent, job, monkeypatch):
    evidence = agent.repo.save_evidence(dict(title='甲公司 年报', company='甲公司', kind='disclosure',
                                             text='本期营业收入 1.2 亿，上年 1.0 亿', url=''), 'user_import')
    signal = dict(rule='disclosure_revenue_trend', value={'current': 120000000, 'previous': 100000000},
                  evidence_id=evidence['id'])
    task = agent.repo.create_task('reliability_extract', '甲公司')
    calls = stub_run_extract(monkeypatch, dict(status='completed', message='抽取 1 条。', accepted=1, evidence=1,
                                               signals=[signal], task=task, identity=None))
    state = state_for(agent, scope_for('company', job))
    result = agent._builtin(state, 'signal_extract', dict(company='甲公司', limit=20))
    assert result['status'] == 'completed' and result['accepted'] == 1
    assert result['citable_evidence'][0]['id'] == evidence['id'] and evidence['id'] in state['evidence']
    assert calls[0]['limit'] == 20


def test_reliability_refresh_refused_in_job_scope_without_task(agent, job):
    state = state_for(agent, scope_for('job', job))
    result = agent._builtin(state, 'reliability_refresh', dict(company='甲公司'))
    assert result['status'] == 'out_of_scope' and result['executed'] is False
    assert 'request_scope_expansion' in result['note']


def test_reliability_refresh_reports_dimensions_missing_and_diff(agent, job):
    from jobscope.reliability.models import SignalInput
    identity = agent.repo.resolve_company('甲公司')
    agent.repo.save_signal(SignalInput(company_id=identity['id'], dimension='stability', rule='company_age',
                                       value={'years': 6}, unit='年', source_class='official', evidence_id='E-age',
                                       observed_at='', collected_at='2026-01-01T00:00:00+00:00'))
    state = state_for(agent, scope_for('company', job))
    first = agent._builtin(state, 'reliability_refresh', dict(company='甲公司'))
    assert first['status'] == 'completed'
    stability = next(line for line in first['dimensions'] if line['dimension'] == 'stability')
    pay = next(line for line in first['dimensions'] if line['dimension'] == 'pay')
    # 有信号的维度出分，无信号的维度带缺口文案——补分工作流靠这两者定位下一步
    assert stability['score'] is not None and pay['score'] is None and pay['missing']
    assert first['diff'] is None  # 首次计算没有上一份快照
    # 换一条更新的信号后重算：差分把变化归因到具体规则（取值跨档位才看得见分数变化；
    # collected_at 错开，别让同秒并列取旧值）
    agent.repo.save_signal(SignalInput(company_id=identity['id'], dimension='stability', rule='company_age',
                                       value={'years': 12}, unit='年', source_class='official', evidence_id='E-age2',
                                       observed_at='', collected_at='2026-01-02T00:00:00+00:00'))
    second = agent._builtin(state, 'reliability_refresh', dict(company='甲公司'))
    assert second['diff'] is not None
    assert any(item['rule'] == 'company_age' for item in second['diff']['rules'])


def test_company_profile_surfaces_missing_gaps(agent, job):
    from jobscope.reliability.service import ReliabilityService
    agent.repo.resolve_company('甲公司')
    ReliabilityService(agent.repo).recompute('甲公司')
    state = state_for(agent, scope_for('company', job))
    result = agent._builtin(state, 'company_profile', dict(company='甲公司'))
    assert result['found'] and result['reference']
    pay = next(line for line in result['reference']['dimensions'] if line['dimension'] == 'pay')
    assert pay['score'] is None and pay['missing']
    assert 'reliability_refresh' in result['note']


def test_chat_loop_executes_reliability_refresh_tool(client, monkeypatch):
    configure(client)
    calls = scripted(monkeypatch, [tool_reply('reliability_refresh', dict(company='甲公司')),
                                   {'choices': [{'message': {'content': '已重算。'}}]}])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '重算甲公司的参考分'})
    assert result.status_code == 200
    events = result.json()['trace']['events']
    assert any(event['tool'] == 'reliability_refresh' and event['status'] == 'completed' for event in events)
    tool_message = json.loads(calls[1]['messages'][-1]['content'])
    assert tool_message['status'] == 'completed' and tool_message['dimensions']


def test_signal_collect_reads_pipeline_signals_as_dicts(agent, monkeypatch):
    # reliability 管线返回的 signals 是 save_signal 落库后的 dict（model_dump 形态）：
    # 曾按 RawSignal 属性访问，信号一落库就 AttributeError（2026-11-21 线上五连炸的根因）。
    saved_signal = dict(id='S-x', company_id='C-1', dimension='stability', rule='salary_quote_coverage',
                        value={'median': 20}, unit='k', source_class='derived', evidence_id='E-fake1',
                        observed_at='', collected_at='')
    monkeypatch.setattr('jobscope.reliability.collection.collect_source',
                        lambda repo, data_dir, company, source, credit_code='': dict(
                            status='completed', message='采集完成', signals=[saved_signal], task=dict(id='T-1')))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_collect', dict(company='甲公司', source='jobscope_jobs'))
    assert result['status'] == 'completed' and result['task_id'] == 'T-1'
    assert result['signals'] == [dict(rule='salary_quote_coverage', value={'median': 20})]
    # 信号引用的证据不存在时不编造可引用清单，但工具本身不再炸
    assert result['citable_evidence'] == []


def test_signal_extract_reads_pipeline_signals_as_dicts(agent, monkeypatch):
    saved_signal = dict(id='S-y', company_id='C-1', dimension='stability', rule='team_size_statement',
                        value={'size': 50}, unit='人', source_class='user_material', evidence_id='E-fake2',
                        observed_at='', collected_at='')
    monkeypatch.setattr('jobscope.reliability.service.run_signal_extract',
                        lambda repo, company, credit_code='', limit=12, max_tokens=1200: dict(
                            status='completed', message='抽取完成', accepted=1, evidence=3,
                            signals=[saved_signal], task=dict(id='T-2')))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'signal_extract', dict(company='甲公司'))
    assert result['status'] == 'completed' and result['accepted'] == 1 and result['task_id'] == 'T-2'
    assert result['signals'] == [dict(rule='team_size_statement', value={'size': 50})]
    assert result['citable_evidence'] == []
