from jobscope.research.query import plan_query
from jobscope.research.index import ResearchIndex
from jobscope.research.tools import salary_query
from jobscope.repository import Repository


def test_city_salary_and_unsupported_locations(tmp_path):
    repo = Repository(tmp_path)
    for city, salary in [('西安', '12-18K'), ('杭州', '30-35K')]:
        repo.save_job(dict(company='甲公司', title='研发', location=city, salary=salary, description='Python', external_id=city), 'user_import')
    scope = dict(kind='company', company='甲公司')
    for city, expected in [('西安', 0), ('杭州', 1)]:
        plan = plan_query(f'{city}工资至少20K', [], scope)
        assert not plan.clarification
        metrics = salary_query(ResearchIndex(repo), plan, scope)
        assert metrics['matched_jobs'] == expected
        assert metrics['location'] == city
    for query in ['在火星工资至少20K', '杭州或西安工资至少20K', '只看火星市的岗位工资至少20K']:
        assert plan_query(query, [], scope).clarification


def test_conditions_survive_unrelated_turns_and_can_be_removed():
    from jobscope.research.query import conversation_conditions
    scope = dict(kind='company', company='甲公司')
    history = [dict(role='user', content='只看杭州，月薪至少25K，不考虑外包')]
    history += [dict(role='user', content='帮我写一个面试自我介绍') for _ in range(5)]
    plan = plan_query('工资有多少岗位', history, scope)
    assert plan.location == '杭州' and plan.threshold == 25000
    assert plan.exclude_outsourcing and plan.clarification
    history += [dict(role='user', content='改成西安'), dict(role='user', content='取消薪资限制，取消外包限制')]
    conditions = conversation_conditions(history)
    assert conditions['location'] == '西安' and conditions['threshold'] is None
    assert not conditions['exclude_outsourcing']
    assert conversation_conditions([])['location'] == ''
    assert conversation_conditions([dict(role='user', content='写一段杭州旅游广告，预算至少25K')])['location'] == ''
    assert not plan_query('取消薪资限制', [dict(role='user', content='年薪至少50万')], scope).clarification
    assert not plan_query('那取消薪资限制呢', [dict(role='user', content='年薪至少50万')], scope).clarification
    assert not plan_query('改成杭州', [dict(role='user', content='杭州或西安工资至少20K')], scope).clarification


def test_tool_queries_keep_user_conditions_and_timeline_validation(tmp_path):
    import time
    from jobscope.general_agent import GeneralAgent
    from jobscope.extensions import fingerprint
    repo = Repository(tmp_path)
    for city, salary in [('西安', '12-18K'), ('杭州', '30-35K')]:
        repo.save_job(dict(company='甲公司', title='研发', location=city, salary=salary, description='Python', external_id=city), 'user_import')
    agent = GeneralAgent(repo)
    scope = dict(kind='company', company='甲公司')
    state = dict(scope=scope, scope_binding=fingerprint(scope), run=dict(id='R-test', scope=scope, created_at='test', degraded=[]),
                 condition_history=[dict(role='user', content='西安工资至少20K')], evidence={}, metrics=None, actions=[],
                 conversation_id='test', started=time.time(), context_chars=0,
                 timeline=[dict(kind='text', text='引用[E-fake]'), dict(kind='step', event=dict(tool='project_search'))])
    result = agent._builtin(state, 'project_salary', dict(query='杭州工资至少1K'))
    assert result['location'] == '西安' and result['threshold'] == 20000
    assert result['matched_jobs'] == 0
    result = agent._result(state, '引用[E-fake]')
    assert result['citation_validation']['invalid_ids'] == ['E-fake']
    assert 'E-fake 未读取' in result['answer']
    assert 'E-fake 未读取' in result['timeline'][0]['text']
    assert result['timeline'][1]['kind'] == 'step'
    state['condition_history'] = [dict(role='user', content='你好')]
    result = agent._builtin(state, 'project_salary', dict(query='杭州工资至少20K'))
    assert result['location'] == '杭州' and result['threshold'] == 20000
    assert result['matched_jobs'] == 1


def test_search_obeys_city_and_salary_filters(tmp_path):
    from jobscope.research.retrieval import retrieve
    repo = Repository(tmp_path)
    for city, salary in [('西安', '12-18K'), ('杭州', '30-35K')]:
        repo.save_job(dict(company='甲公司', title='Python研发', location=city, salary=salary, description='Python', external_id=city), 'user_import')
    scope = dict(kind='company', company='甲公司')
    plan = plan_query('西安Python岗位月薪至少20K', [], scope)
    assert retrieve(ResearchIndex(repo), plan, scope)['evidence'] == []


def test_saved_locations_unknown_slots_and_company_names(tmp_path):
    from jobscope.research.query import condition_context, conversation_conditions
    repo = Repository(tmp_path)
    company = '北京星辰公司'
    scope = dict(kind='company', company=company)
    for city in ['杭州', '台北', '新加坡', 'Köln', '外地']:
        repo.save_job(dict(company=company, title='研发', location=city, salary='30-35K', description='Python', external_id=city), 'user_import')
    context = condition_context(repo, scope)
    for city in ['台北', '新加坡', 'Köln', '外地']:
        plan = plan_query(f'{company}在{city}的岗位工资至少20K', [], scope, [company], known_locations=context['known_locations'])
        assert not plan.clarification
        metrics = salary_query(ResearchIndex(repo), plan, scope)
        assert metrics['location'] == city and metrics['matched_jobs'] == 1
    plan = plan_query(f'{company}在杭州的岗位工资至少20K', [], scope, [company], known_locations=context['known_locations'])
    assert plan.location == '杭州' and not plan.clarification
    for query in [f'{company}在火星的岗位工资至少20K', '只看火星工资至少20K']:
        assert plan_query(query, [], scope, [company], known_locations=context['known_locations']).clarification
    conditions = conversation_conditions([dict(role='user', content='杭州岗位至少20K'), dict(role='user', content='也看看上海')], **context)
    assert conditions['clarification']
