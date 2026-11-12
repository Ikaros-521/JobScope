"""Project tool isolation tests; all data lives in per-test SQLite repositories."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.agent_scope import AgentScope, ScopeBoundaryError
from jobscope.extensions import fingerprint, tool_alias
from jobscope.general_agent import BUILTINS, GeneralAgent, RunHooks
from jobscope.repository import Repository, now
from jobscope.scope_expansion import ScopeExpansionStore


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr('jobscope.general_agent.model_configured', lambda: True)
    return GeneralAgent(Repository(tmp_path))


@pytest.fixture
def saved(agent):
    repo = agent.repo
    jobs = {}
    for key, company in [('own', '甲公司'), ('sibling', '甲公司'), ('foreign', '乙公司'), ('subsidiary', '甲公司子公司')]:
        jobs[key] = repo.save_job(dict(title=f'Python {key}', company=company, description=f'Python {key} private posting',
                                       salary='20-30K', external_id=key), 'user_import')
    shared = repo.save_evidence(dict(company='甲公司', title='Python 公司公告', kind='disclosure', text='Python shared company evidence'))
    broad = repo.save_evidence(dict(company='甲公司', title='jd_specificity', kind='local_computation', text='Python company-wide JD analysis'))
    cross = repo.save_evidence(dict(company='甲公司', title='pay_vs_city_median', kind='local_computation', text='Python cross-company city median private stats'))
    from jobscope.reliability.models import SignalInput
    company = repo.find_company('甲公司')
    for rule, evidence in [('jd_specificity', broad), ('pay_vs_city_median', cross)]:
        repo.save_signal(SignalInput(company_id=company['id'], dimension='professional', rule=rule,
                                     evidence_id=evidence['id'], value=dict(count=2), source_class='platform', collected_at=now()))
    agent.index.sync()
    return dict(jobs=jobs, shared=shared, broad=broad, cross=cross)


def scope_for(saved, kind):
    if kind == 'global':
        return dict(kind=kind)
    scope = dict(kind=kind, company='甲公司')
    if kind == 'job':
        scope['job_id'] = saved['jobs']['own']['id']
    return scope


def state_for(agent, scope):
    return dict(scope=dict(scope), scope_binding=fingerprint(scope), run=dict(scope=dict(scope)), evidence={}, metrics=None, actions=[])


def run_script(agent, monkeypatch, conversation_id, replies, **kwargs):
    captured = []
    def turn(config, messages, tools):
        captured.append(copy.deepcopy(messages))
        assert replies, 'Unexpected additional model turn'
        return replies.pop(0), {}
    monkeypatch.setattr('jobscope.general_agent.tool_turn', turn)
    return agent.start('继续', kwargs.pop('history', []), conversation_id, **kwargs), captured


def call(name, args, identifier='c1'):
    return dict(id=identifier, type='function', function=dict(name=name, arguments=json.dumps(args)))


def tools(*calls):
    return dict(role='assistant', content='', tool_calls=list(calls))


def answer(text='资料已核实。'):
    return dict(role='assistant', content=text)


@pytest.mark.parametrize('kind', ['global', 'company', 'job'])
def test_search_and_salary_use_conversation_boundary(agent, saved, kind):
    state = state_for(agent, scope_for(saved, kind))
    result = agent._builtin(state, 'project_search', dict(query='Python'))
    ids = {item['id'] for item in result['evidence']}
    own, sibling, foreign, subsidiary = (saved['jobs'][key]['evidence_ids'][0] for key in ['own', 'sibling', 'foreign', 'subsidiary'])
    assert own in ids
    if kind == 'global':
        assert {foreign, subsidiary} <= ids
    else:
        assert foreign not in ids and subsidiary not in ids and saved['cross']['id'] not in ids
        assert saved['shared']['id'] in ids
    if kind == 'job':
        assert sibling not in ids and saved['broad']['id'] not in ids
    if kind == 'company':
        assert sibling in ids
    metrics = agent._builtin(state, 'project_salary', dict(query='Python 薪资'))
    assert metrics['total_jobs'] == {'global': 4, 'company': 2, 'job': 1}[kind]
    assert {sample['job_id'] for sample in metrics['samples']} == {
        job['id'] for job in saved['jobs'].values()
        if kind == 'global' or (job['company'] == '甲公司' and (kind != 'job' or job['id'] == saved['jobs']['own']['id']))}


@pytest.mark.parametrize('kind', ['company', 'job'])
def test_foreign_direct_ids_and_profiles_are_denied(agent, saved, kind):
    state = state_for(agent, scope_for(saved, kind))
    for key in ['foreign', 'subsidiary']:
        with pytest.raises(ScopeBoundaryError, match='request_scope_expansion'):
            agent._builtin(state, 'project_read', dict(evidence_id=saved['jobs'][key]['evidence_ids'][0]))
        for tool in ['company_profile', 'company_detail']:
            with pytest.raises(ScopeBoundaryError, match='当前会话范围'):
                agent._builtin(state, tool, dict(company=saved['jobs'][key]['company'], section='evidence'))
    assert not state['evidence']


def test_unbound_local_computation_text_cannot_grant_company_access(agent, saved):
    forged = agent.repo.save_evidence(dict(company='甲公司', kind='local_computation', title='jd_specificity',
                                          text='jd_specificity Python: city_jobs=999 跨公司统计'))
    state = state_for(agent, scope_for(saved, 'company'))
    with pytest.raises(ScopeBoundaryError, match='request_scope_expansion'):
        agent._builtin(state, 'project_read', dict(evidence_id=forged['id']))
    global_state = state_for(agent, dict(kind='global'))
    assert agent._builtin(global_state, 'project_read', dict(evidence_id=forged['id']))['id'] == forged['id']


def test_job_postings_use_current_mapping_and_allow_shared_sources(agent, saved):
    state = state_for(agent, scope_for(saved, 'job'))
    repo = agent.repo
    # An arbitrary historical link never grants access to a sibling posting.
    sibling = saved['jobs']['sibling']['evidence_ids'][0]
    repo.link_evidence(saved['jobs']['own']['id'], sibling)
    with pytest.raises(ScopeBoundaryError):
        agent._builtin(state, 'project_read', dict(evidence_id=sibling))
    assert agent._builtin(state, 'project_read', dict(evidence_id=saved['shared']['id']))['id'] == saved['shared']['id']
    with pytest.raises(ScopeBoundaryError):
        agent._builtin(state, 'project_read', dict(evidence_id=saved['broad']['id']))
    # Two job IDs can legitimately share one deduplicated current posting.
    duplicate = repo.save_job({**saved['jobs']['own'], 'external_id': 'duplicate'}, 'user_import')
    state = state_for(agent, dict(kind='job', company='甲公司', job_id=duplicate['id']))
    assert duplicate['id'] != saved['jobs']['own']['id']
    eid = saved['jobs']['own']['evidence_ids'][0]
    assert agent._builtin(state, 'project_read', dict(evidence_id=eid))['id'] == eid
    old = eid
    repo.save_job({**duplicate, 'external_id': 'duplicate', 'description': 'new Python requirements'}, 'user_import')
    with pytest.raises(ScopeBoundaryError):
        agent._builtin(state, 'project_read', dict(evidence_id=old))


@pytest.mark.parametrize('kind', ['company', 'job'])
def test_metadata_pagination_filters_before_totals(agent, saved, kind):
    repo = agent.repo
    for i in range(13):
        repo.save_evidence(dict(company='甲公司', title=f'公告{i}', kind='disclosure', text=f'safe {i}'))
    # Aliases are descriptive metadata, not an additional evidence boundary.
    original = repo.find_company('甲公司')
    repo.resolve_company('甲公司', aliases=['甲公司子公司'])
    state = state_for(agent, scope_for(saved, kind))
    first = agent._builtin(state, 'company_detail', dict(company='甲公司', section='evidence', page=1))
    second = agent._builtin(state, 'company_detail', dict(company='甲公司', section='evidence', page=99))
    expected = 17 if kind == 'company' else 15  # 13 disclosures + shared + own [+ sibling + safe company computation]
    assert first['total'] == second['total'] == expected
    assert first['page'] == 1 and second['page'] == second['pages'] == 2
    assert len(first['evidence']) == 10 and len(second['evidence']) == expected - 10
    all_items = first['evidence'] + second['evidence']
    assert len({item['id'] for item in all_items}) == expected
    assert all('text' not in item for item in all_items)
    assert saved['cross']['id'] not in {item['id'] for item in all_items}
    assert repo.find_company('甲公司')['id'] == original['id']


def report_for(agent, evidence, rule='registered_years', score=73):
    company = agent.repo.find_company('甲公司')
    return dict(company_id=company['id'], company='甲公司', score=score, confidence=.5, summary='主观参考摘要',
                computed_at=now(), input_hash=fingerprint([evidence['id'], score]), evidence_ids=[evidence['id']],
                dimensions=[dict(dimension='stability', label='稳定性', score=score, confidence=.5,
                                 evidence_ids=[evidence['id']], findings=[dict(rule=rule, evidence_id=evidence['id'])])])


@pytest.mark.parametrize('kind', ['global', 'company', 'job'])
def test_reference_and_signal_provenance_are_bounded(agent, saved, kind):
    from jobscope.reliability.models import SignalInput
    scope = scope_for(saved, kind)
    state = state_for(agent, scope)
    company = agent.repo.find_company('甲公司')
    for rule, evidence in [('registered_years', saved['shared']), ('pay_vs_city_median', saved['cross']), ('jd_specificity', saved['broad'])]:
        agent.repo.save_signal(SignalInput(company_id=company['id'], dimension='stability', rule=rule,
                                          evidence_id=evidence['id'], value=dict(count=12), source_class='platform', collected_at=now()))
    safe = report_for(agent, saved['shared'])
    unsafe = report_for(agent, saved['cross'], 'pay_vs_city_median', 99)
    agent.repo.save_reliability(safe)
    agent.repo.save_reliability(unsafe)
    profile = agent._builtin(state, 'company_profile', dict(company='甲公司'))
    history = agent._builtin(state, 'company_detail', dict(company='甲公司', section='history'))
    signals = agent._builtin(state, 'company_detail', dict(company='甲公司', section='signals'))
    if kind == 'global':
        assert profile['reference']['score'] == 99
        assert len(history['history']) == 2
        assert 'pay_vs_city_median' in {item['rule'] for item in signals['signals']}
    else:
        assert profile['reference'] is None and 'request_scope_expansion' in profile['note']
        assert [item['score'] for item in history['history']] == [73]
        assert 'pay_vs_city_median' not in {item['rule'] for item in signals['signals']}
        assert saved['cross']['id'] not in state['evidence']
    if kind == 'job':
        assert {item['rule'] for item in signals['signals']} == {'registered_years'}
    # Safe standalone company evidence remains a subjective reference in every scope.
    newer = report_for(agent, saved['shared'], score=74)
    agent.repo.save_reliability(newer)
    profile = agent._builtin(state, 'company_profile', dict(company='甲公司'))
    assert profile['reference']['nature'] == '主观参考' and profile['reference']['score'] == 74


@pytest.mark.parametrize('tool', ['project_search', 'project_salary'])
@pytest.mark.parametrize('kind', ['global', 'company', 'job'])
def test_plan_clarification_never_executes_query(agent, saved, monkeypatch, tool, kind):
    def forbidden(*args, **kwargs):
        pytest.fail('A clarification must not execute retrieval or aggregation')
    monkeypatch.setattr('jobscope.general_agent.retrieve', forbidden)
    monkeypatch.setattr('jobscope.general_agent.salary_query', forbidden)
    state = state_for(agent, scope_for(saved, kind))
    result = agent._builtin(state, tool, dict(query='年薪超过50万'))
    assert result['clarification'] and result['executed'] is False
    assert state['metrics'] is None and not state['evidence']
    if kind == 'global':
        result = agent._builtin(state, tool, dict(query='不存在公司薪资'))
        assert result['clarification'] and result['executed'] is False


@pytest.mark.parametrize('kind', ['global', 'company', 'job'])
def test_start_derives_scope_keeps_pinned_summary_and_does_not_mutate_scope(agent, saved, monkeypatch, kind):
    scope = scope_for(saved, kind)
    stored = {**scope, 'agent_mode': 'general'} if kind == 'global' else scope
    cid = agent.repo.create_conversation(stored, mode='general')
    history = [dict(role='assistant', kind='compaction', content='早期结论：已选择当前岗位。')]
    history += [dict(role='user', content=f'recent {i}') for i in range(8)]
    result, captured = run_script(agent, monkeypatch, cid, [answer()], history=history)
    assert result['trace']['scope'] == scope
    assert json.dumps(scope, ensure_ascii=False) in captured[0][0]['content']
    assert captured[0][1]['content'] == history[0]['content']
    assert len(captured[0]) == 9 and 'recent 0' not in str(captured[0][2:])
    assert agent.repo.get_conversation(cid)['scope'] == stored
    with pytest.raises(ValueError, match='范围不匹配'):
        agent.start('test', [], cid, scope=dict(kind='company', company='乙公司'))
    assert result['actions'] == []


def test_default_without_conversation_is_global_and_explicit_scope_is_copied(agent, saved, monkeypatch):
    result, _ = run_script(agent, monkeypatch, None, [answer()])
    assert result['trace']['scope'] == dict(kind='global')
    scope = scope_for(saved, 'job')
    result, _ = run_script(agent, monkeypatch, None, [answer()], scope=scope)
    scope['company'] = '乙公司'
    assert result['trace']['scope']['company'] == '甲公司'
    with pytest.raises(ValueError, match='范围不匹配'):
        agent.start('test', [], None, scope=scope)


@pytest.mark.parametrize('target', ['company', 'global'])
def test_proposal_is_durable_emits_card_and_stops_without_expanded_execution(agent, saved, monkeypatch, target):
    scope = scope_for(saved, 'job')
    cid = agent.repo.create_conversation(scope, mode='general')
    agent.repo.append_messages(cid, [dict(role='user', content='原始消息')])
    before = agent.repo.get_conversation(cid)
    events = []
    response = tools(call('request_scope_expansion', dict(target=target, reason='需要比较岗位')),
                     call('project_read', dict(evidence_id=saved['jobs']['foreign']['evidence_ids'][0]), 'c2'))
    result, captured = run_script(agent, monkeypatch, cid, [response], hooks=RunHooks(on_event=events.append))
    assert len(captured) == 1 and result['trace']['status'] == 'completed'
    action = result['actions'][0]
    assert action['type'] == 'scope_expansion' and action['source_scope'] == scope
    assert action['target_scope'] == (dict(kind='company', company='甲公司') if target == 'company' else dict(kind='global'))
    assert action['target_conversation_id'] is None
    assert ScopeExpansionStore(agent.repo).list(cid) == [action]
    assert agent.repo.get_conversation(cid) == before
    with agent.repo.connection() as conn:
        assert conn.execute('SELECT count(*) FROM conversations').fetchone()[0] == 1
    assert [event['action'] for event in events if event['tool'] == 'request_scope_expansion'] == [action]
    assert not any(event['tool'] == 'project_read' for event in events)
    spec = next(item for item in BUILTINS if item['name'] == 'request_scope_expansion')
    assert spec['parameters']['properties']['target']['enum'] == ['company', 'global']
    assert set(spec['parameters']['properties']) == {'target', 'reason'}


def mcp_fixture(agent, monkeypatch):
    from jobscope.extensions import MCPClient
    spec = dict(name='lookup', description='外部查询', inputSchema=dict(type='object', properties={}))
    server = dict(id='m1', name='fixture', enabled=True, tested=True, tools=[spec], allowed_tools=['lookup'], auto_tools=[], token='', env={})
    agent.extensions._put('agent_mcp', server)
    executed = []
    def invoke(config, name, args):
        executed.append(args)
        return dict(text='external result', is_error=False)
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    return tool_alias('m1', 'lookup'), executed


@pytest.mark.parametrize('change', ['scope', 'deleted', 'state'])
def test_pending_resume_revalidates_scope_before_any_external_call(agent, saved, monkeypatch, change):
    alias, executed = mcp_fixture(agent, monkeypatch)
    scope = scope_for(saved, 'job')
    cid = agent.repo.create_conversation(scope, mode='general')
    pending, _ = run_script(agent, monkeypatch, cid, [tools(call(alias, {}))])
    assert pending['pending_approval'] and not executed
    with agent.repo.connection() as conn:
        if change == 'scope':
            conn.execute('UPDATE conversations SET scope=? WHERE id=?', (json.dumps(dict(kind='global')), cid))
        elif change == 'deleted':
            conn.execute('DELETE FROM conversations WHERE id=?', (cid,))
        else:
            row = conn.execute('SELECT payload FROM agent_pending WHERE run_id=?', (pending['trace']['id'],)).fetchone()
            state = json.loads(row[0])
            state['scope'] = dict(kind='global')
            conn.execute('UPDATE agent_pending SET payload=? WHERE run_id=?', (json.dumps(state), pending['trace']['id']))
    with pytest.raises(ValueError):
        agent.resume(pending['trace']['id'], True, pending['pending_approval']['approval_id'])
    assert not executed


def test_scoped_mcp_still_requires_approval_and_resume_keeps_boundary(agent, saved, monkeypatch):
    alias, executed = mcp_fixture(agent, monkeypatch)
    scope = scope_for(saved, 'job')
    cid = agent.repo.create_conversation(scope, mode='general')
    replies = [tools(call(alias, {})), answer('外部查询已完成。')]
    pending, _ = run_script(agent, monkeypatch, cid, replies)
    assert pending['pending_approval'] and not executed
    result = agent.resume(pending['trace']['id'], True, pending['pending_approval']['approval_id'])
    assert executed == [{}] and result['trace']['scope'] == scope and not result['pending_approval']


def test_denied_read_returns_boundary_to_model_without_foreign_body(agent, saved, monkeypatch):
    cid = agent.repo.create_conversation(scope_for(saved, 'job'), mode='general')
    eid = saved['jobs']['foreign']['evidence_ids'][0]
    result, captured = run_script(agent, monkeypatch, cid, [tools(call('project_read', dict(evidence_id=eid))), answer('当前范围无法读取。')])
    error = json.loads(captured[1][-1]['content'])['error']
    assert 'request_scope_expansion' in error and '当前会话范围' in error
    assert 'foreign private posting' not in str(captured)
    assert result['citations'] == []


def test_scoped_skills_and_resume_keep_existing_privacy_controls(agent, saved, monkeypatch):
    alias, executed = mcp_fixture(agent, monkeypatch)
    server = agent.extensions.mcp('m1')
    server['auto_tools'] = ['lookup']
    agent.extensions._put('agent_mcp', server)
    skill = agent.extensions.save_skill('---\nname: interview\ndescription: 面试准备\n---\n每次只问一个问题。')
    agent.repo.save_resume('Python开发，三年工作经验', 'text')
    scope = scope_for(saved, 'job')
    cid = agent.repo.create_conversation(scope, mode='general')
    pending, captured = run_script(agent, monkeypatch, cid, [
        tools(call('skill_read', dict(skill_id=skill['id']))),
        tools(call('resume_read', {})), tools(call(alias, {}))])
    assert '每次只问一个问题' in captured[1][-1]['content']
    assert 'Python' in captured[2][-1]['content']
    assert pending['pending_approval'] and not executed
    assert agent.extensions.is_private(cid)
    assert pending['trace']['scope'] == scope


def test_proposal_schema_cannot_supply_foreign_company_or_scope(agent, saved, monkeypatch):
    cid = agent.repo.create_conversation(scope_for(saved, 'job'), mode='general')
    result, captured = run_script(agent, monkeypatch, cid, [tools(call('request_scope_expansion',
        dict(target='company', company='乙公司', reason='扩大范围'))), answer('请选择允许的扩展范围。')])
    assert '结构校验' in captured[1][-1]['content']
    assert result['actions'] == [] and ScopeExpansionStore(agent.repo).list(cid) == []


def test_scope_policy_is_immutable_and_state_binding_rejects_changes(agent, saved):
    from dataclasses import FrozenInstanceError
    policy = AgentScope.parse(scope_for(saved, 'job'))
    with pytest.raises(FrozenInstanceError):
        policy.kind = 'global'
    state = state_for(agent, policy.as_dict())
    state['run']['scope'] = dict(kind='global')
    with pytest.raises(ValueError, match='范围已改变'):
        agent._builtin(state, 'project_salary', dict(query='薪资'))
