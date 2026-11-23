"""Agent 内部功能调度工具（2026-11-23）：favorite_add/favorite_remove/match_jobs/assess_job/optimize_resume。

不碰网络、不开浏览器：直接以 _builtin/_execute 层验证
- 收藏写入与范围纪律（岗位/公司范围只动范围内岗位）；
- favorite_remove 高危二次确认：autonomous 拦截 → approved 放行 → full 档直放 → 会话授权免确认；
- match_jobs 偏好粗排（薪资/地点/词元命中）与无偏好/岗位范围拒绝；
- assess_job/optimize_resume 只生成随消息落库的卡片动作，不执行评估、不写简历版本。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.extensions import fingerprint
from jobscope.general_agent import BUILTINS, GeneralAgent
from jobscope.repository import Repository


@pytest.fixture
def agent(tmp_path):
    return GeneralAgent(Repository(tmp_path))


@pytest.fixture
def job(agent):
    return agent.repo.save_job(dict(title='Java 后端工程师', company='甲公司', description='负责 Java 微服务开发',
                                    salary='25-35K', location='杭州', external_id='own'), 'user_import')


@pytest.fixture
def other_job(agent):
    return agent.repo.save_job(dict(title='前端工程师', company='乙公司', description='React 前端开发',
                                    salary='15-25K', location='上海', external_id='own2'), 'user_import')


def scope_for(kind, job=None):
    if kind == 'global':
        return dict(kind=kind)
    scope = dict(kind=kind, company='甲公司')
    if kind == 'job':
        scope['job_id'] = job['id']
    return scope


def state_for(agent, scope, mode='autonomous'):
    # started/context_chars 是 _result 计算耗时与引用的必需字段；run.created_at 是 _event 落库所需。
    return dict(scope=dict(scope), scope_binding=fingerprint(scope),
                run=dict(id='R-t', status='running', scope=dict(scope), events=[], degraded=[],
                         created_at='2026-11-23T00:00:00'),
                evidence={}, metrics=None, actions=[], context_chars=0,
                conversation_id='C-t', permission_mode=mode, started=time.time())


def exec_state(agent, scope, mode='autonomous'):
    """_execute 层状态：需要工具目录（schema 校验）与审批通道依赖的最小字段。"""
    state = state_for(agent, scope, mode)
    state.update(tools=list(BUILTINS), registry={}, messages=[],
                 limits=dict(max_tool_calls=10, context_characters=1000000), queue=[])
    return state


def call_of(name, args):
    return dict(id='call-1', type='function', function=dict(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def test_favorite_add_stars_and_is_idempotent(agent, job):
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'favorite_add', dict(job_id=job['id']))
    assert result['status'] == 'ok' and result['executed'] is True and result['starred'] is True
    assert agent.repo.job(job['id'])['starred'] is True and agent.repo.job(job['id'])['starred_at']
    again = agent._builtin(state, 'favorite_add', dict(job_id=job['id']))
    assert again['status'] == 'noop' and again['executed'] is False


def test_favorite_remove_clears_pipeline_data(agent, job):
    agent.repo.update_jobs([job['id']], {'starred': True, 'pipeline_status': 'interested',
                                         'pipeline_note': '周五跟进', 'pipeline_due': '2026-11-30'})
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'favorite_remove', dict(job_id=job['id']))
    assert result['status'] == 'ok' and result['starred'] is False
    updated = agent.repo.job(job['id'])
    assert not updated['starred']
    for key in ('pipeline_status', 'pipeline_note', 'pipeline_due', 'starred_at'):
        assert key not in updated


def test_favorite_scope_discipline(agent, job, other_job):
    # 岗位范围只能收藏当前岗位；公司范围只能收藏本公司岗位；全局不限。
    job_state = state_for(agent, scope_for('job', job))
    assert agent._builtin(job_state, 'favorite_add', dict(job_id=job['id']))['status'] == 'ok'
    refusal = agent._builtin(job_state, 'favorite_add', dict(job_id=other_job['id']))
    assert refusal['status'] == 'out_of_scope' and refusal['executed'] is False
    company_state = state_for(agent, scope_for('company'))
    assert agent._builtin(company_state, 'favorite_add', dict(job_id=job['id']))['status'] in ('ok', 'noop')
    assert agent._builtin(company_state, 'favorite_add', dict(job_id=other_job['id']))['status'] == 'out_of_scope'


def test_favorite_missing_job_raises(agent):
    with pytest.raises(ValueError):
        agent._builtin(state_for(agent, scope_for('global')), 'favorite_add', dict(job_id='J-none'))


def test_favorite_remove_requires_confirmation_then_executes(agent, job):
    """autonomous 档 favorite_remove 先拦截为待审批；用户批准后同一次调用真正执行。"""
    agent.repo.update_jobs([job['id']], {'starred': True})
    state = exec_state(agent, scope_for('global'))
    pending = agent._execute(state, call_of('favorite_remove', dict(job_id=job['id'])))
    assert pending['kind'] == 'pending'
    assert state['run']['status'] == 'awaiting_approval'
    assert state['run']['pending']['tool'] == '取消收藏岗位'
    assert state['run']['pending']['arguments'] == dict(job_id=job['id'])
    assert 'job_id' in state['run']['pending']['approval_id'] or state['run']['pending']['approval_id']
    assert agent.repo.job(job['id'])['starred'] is True  # 拦截阶段不落任何写
    done = agent._execute(state, state['pending_call'], approved=True)
    assert done['kind'] == 'done' and done['result']['status'] == 'ok'
    assert agent.repo.job(job['id'])['starred'] is False


def test_favorite_remove_full_mode_and_grant_skip_gate(agent, job):
    # full 档直放（与 MCP destructive 行为一致）；会话授权后 autonomous 也不再逐次确认。
    full = exec_state(agent, scope_for('global'), mode='full')
    assert agent._execute(full, call_of('favorite_remove', dict(job_id=job['id'])))['kind'] == 'done'
    agent.repo.update_jobs([job['id']], {'starred': True})
    agent.extensions.grant_tool('C-t', 'builtin', 'favorite_remove')
    granted = exec_state(agent, scope_for('global'))
    assert agent._execute(granted, call_of('favorite_remove', dict(job_id=job['id'])))['kind'] == 'done'


def test_match_jobs_requires_preferences(agent, job):
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'match_jobs', {})
    assert result['status'] == 'no_preferences' and result['matches'] == []
    assert '个人资料库' in result['note']


def test_match_jobs_ranks_by_preferences(agent, job, other_job):
    agent.repo.save_profile_entry(dict(category='preference', title='薪资', content='期望薪资 20-30k',
                                       tags=[], enabled=True))
    agent.repo.save_profile_entry(dict(category='preference', title='城市', content='坐标杭州',
                                       tags=[], enabled=True))
    agent.repo.save_profile_entry(dict(category='preference', title='方向', content='后端开发，Java 技术栈',
                                       tags=[], enabled=True))
    state = state_for(agent, scope_for('global'))
    result = agent._builtin(state, 'match_jobs', {})
    assert result['status'] == 'ok' and result['preference_count'] == 3
    assert result['salary_preference']['min'] == pytest.approx(20000)
    by_id = {item['job_id']: item for item in result['matches']}
    assert job['id'] in by_id and other_job['id'] in by_id  # 零分候选也列出，说明是缺命中而非判负
    hit = by_id[job['id']]
    assert hit['score'] >= 4.0  # 薪资相容 2 + 地点命中 1 + 内容命中
    detail = ' '.join(hit['reasons'])
    assert '薪资' in detail and '地点' in detail and 'java' in detail.casefold()
    assert by_id[other_job['id']]['score'] < hit['score']


def test_match_jobs_company_scope_and_job_refusal(agent, job, other_job):
    agent.repo.save_profile_entry(dict(category='preference', title='方向', content='Java 后端', tags=[], enabled=True))
    job_state = state_for(agent, scope_for('job', job))
    assert agent._builtin(job_state, 'match_jobs', {})['status'] == 'out_of_scope'
    company_state = state_for(agent, scope_for('company'))
    result = agent._builtin(company_state, 'match_jobs', {})
    assert result['status'] == 'ok' and [item['job_id'] for item in result['matches']] == [job['id']]


def test_action_cards_append_and_close_turn(agent, job):
    state = state_for(agent, scope_for('global'))
    assess = agent._builtin(state, 'assess_job', dict(job_id=job['id'], reason='JD 与偏好匹配度高'))
    assert assess['action']['id'].startswith('AA-') and assess['action']['type'] == 'assess_job'
    assert assess['action']['reason'] == 'JD 与偏好匹配度高'
    assert state['actions'] == [assess['action']]
    resume = agent._builtin(state_for(agent, scope_for('global')), 'optimize_resume',
                            dict(job_id=job['id'], instructions='突出微服务经验'))
    assert resume['action']['id'].startswith('AR-') and resume['action']['instructions'] == '突出微服务经验'
    # 卡片收尾口径：范围扩展与评估/工坊卡共用结束信号，文案按卡片类型区分
    closing = agent._expansion_result(state)
    assert '评估卡片' in closing['answer']
    resume_state = state_for(agent, scope_for('global'))
    resume_state['actions'] = [resume['action']]
    closing_resume = agent._expansion_result(resume_state)
    assert '简历工坊' in closing_resume['answer']


def test_action_cards_missing_job_and_scope(agent, job, other_job):
    with pytest.raises(ValueError):
        agent._builtin(state_for(agent, scope_for('global')), 'assess_job', dict(job_id='J-none'))
    job_state = state_for(agent, scope_for('job', job))
    assert agent._builtin(job_state, 'optimize_resume', dict(job_id=other_job['id']))['status'] == 'out_of_scope'
    allowed = agent._builtin(state_for(agent, scope_for('job', job)), 'assess_job', dict(job_id=job['id']))
    assert allowed['action']['job_id'] == job['id'] and allowed['action']['type'] == 'assess_job'
