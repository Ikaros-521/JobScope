"""Endpoint behaviour and error codes for the company reliability API."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app
from jobscope.repository import Repository

SECRET = 'sk-qcc-abcdef-123456'
REAL_POST = httpx.Client.post


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def model_on(monkeypatch):
    # 与 test_semantic.py / test_reliability_extract.py 同一套做法：走真实配置读取路径，只把 HTTP 挡掉。
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')


def mock_model(monkeypatch, content, calls=None):
    """替换 httpx.Client.post，但 TestClient 本身也继承 httpx.Client：客户端自己的请求必须原样放行。"""
    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return REAL_POST(self, url, **kwargs)
        if calls is not None:
            calls.append(url)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)


def add_signal(tmp_path, rule, value, evidence_id, source_class='official', company='甲公司'):
    """信号只能由采集或抽取产生；这条辅助函数直接按契约落库，好让端点层读到有依据的报告。"""
    from jobscope.reliability.models import SignalInput
    from jobscope.reliability.rubric import RULES
    repo = Repository(tmp_path)
    identity = repo.resolve_company(company)
    return repo.save_signal(SignalInput(company_id=identity['id'], dimension=RULES[rule].dimension, rule=rule,
                                        value=value, unit=RULES[rule].unit, source_class=source_class,
                                        evidence_id=evidence_id, observed_at='',
                                        collected_at='2026-11-09T00:00:00+00:00'))


def add_material(client, company='甲公司'):
    return client.post('/api/evidence', json=dict(title='年报摘要', company=company, kind='disclosure',
                                                  text='2024 年度报告：参保人数由 312 人变为 240 人')).json()['evidence']


def extract_tasks(client):
    return [task for task in client.get('/api/tasks').json()['tasks'] if task.get('kind') == 'reliability_extract']


def findings_of(report):
    return [item for line in report['dimensions'] for item in line['findings']]


def test_recompute_without_signals_is_200_and_still_bounded(client):
    # 不带 body 的 POST 也要能用：重算本身不需要任何参数。
    for result in (client.post('/api/companies/甲公司/reliability'),
                   client.post('/api/companies/甲公司/reliability', json=dict(credit_code=''))):
        assert result.status_code == 200
        report = result.json()
        assert report['score'] is None
        assert report['nature'] == 'subjective_reference'
        assert report['disclaimer'] and report['limitations']
        assert len(report['dimensions']) == 6
        assert all(item['score'] is None and item['missing'] for item in report['dimensions'])


def test_latest_report_is_none_until_a_run_happened(client):
    assert client.get('/api/companies/没有的公司/reliability').json() == {'report': None}
    assert client.get('/api/companies/甲公司/reliability').json()['report'] is None
    built = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    stored = client.get('/api/companies/甲公司/reliability').json()['report']
    assert stored['input_hash'] == built['input_hash'] and stored['company_id'] == built['company_id']


def test_company_path_param_is_bounded_and_blank_is_rejected(client):
    assert client.post('/api/companies/%20/reliability', json=dict()).status_code == 422
    assert client.post('/api/companies/%20/signals/collect', json=dict(source='gsxt')).status_code == 422
    assert client.post(f"/api/companies/{'甲' * 301}/reliability", json=dict()).status_code == 422
    assert client.get('/api/companies/%20/reliability').status_code == 422


def test_new_routes_do_not_shadow_the_company_list(client):
    add_material(client)
    listing = client.get('/api/companies', params=dict(q='甲')).json()
    assert listing['companies'][0]['company'] == '甲公司'
    assert client.get('/api/companies').status_code == 200


def test_free_sources_start_enabled_and_paid_start_disabled(client):
    items = client.get('/api/reliability/sources').json()['sources']
    assert items['gsxt']['tier'] == 'free' and items['gsxt']['mode'] == 'enabled'
    assert items['jobscope_jobs']['mode'] == 'enabled'
    assert items['qcc']['tier'] == 'paid' and items['qcc']['mode'] == 'disabled'
    assert items['tyc']['mode'] == 'disabled'
    assert all(not item['key_set'] for item in items.values())


def test_no_response_ever_echoes_the_key(client):
    saved = client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key=SECRET, url='https://api.example.com/qcc'))
    assert saved.status_code == 200
    assert saved.json()['key_set'] is True and saved.json()['mode'] == 'enabled'
    assert SECRET not in saved.text and '"key"' not in saved.text
    body = client.get('/api/reliability/sources').text
    assert SECRET not in body and '"key"' not in body
    assert client.get('/api/reliability/sources').json()['sources']['qcc']['mode'] == 'enabled'


def test_enabling_a_paid_source_without_a_key_is_400(client):
    result = client.put('/api/reliability/sources/qcc', json=dict(mode='enabled'))
    assert result.status_code == 400 and '密钥' in result.json()['detail']
    assert client.put('/api/reliability/sources/nobody', json=dict(mode='disabled')).status_code == 404
    # extra='forbid'：多塞字段是输入问题，不会被当成密钥字段收下
    assert client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', token=SECRET)).status_code == 422


def test_collect_refuses_sources_the_user_has_not_switched_on(client):
    paid = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc'))
    assert paid.status_code == 409 and '密钥' in paid.json()['detail']
    assert client.put('/api/reliability/sources/gsxt', json=dict(mode='disabled')).status_code == 200
    disabled = client.post('/api/companies/甲公司/signals/collect', json=dict(source='gsxt'))
    assert disabled.status_code == 409
    assert client.post('/api/companies/甲公司/signals/collect', json=dict(source='not_a_source')).status_code == 400


def test_collect_reports_an_honest_status_without_touching_the_network(client):
    browser = client.post('/api/companies/甲公司/signals/collect', json=dict(source='gsxt')).json()
    # 本机有没有装 DrissionPage / 有没有项目 Chrome 会话，两种真实结果都可能出现。
    assert browser['status'] in {'needs_login', 'unsupported'}
    assert browser['signals'] == [] and browser['message']
    # 本地来源登记着五条规则，就必须真的算出东西，不能一直退成 unsupported。
    # 这家测试公司还没有已存岗位：R1/R2 连统计量都构不成，材料计数类照常出数。
    local = client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')).json()
    assert local['status'] == 'completed'
    assert {item['rule'] for item in local['signals']} == {
        'salary_quote_coverage', 'disclosure_available', 'industry_media_count'}
    assert '不访问网络' in local['message']


def test_local_collection_is_traceable_and_never_needs_the_browser(client, tmp_path, monkeypatch):
    from jobscope.reliability import collectors

    def boom(*args, **kwargs):
        raise AssertionError('jobscope_jobs 不该走浏览器采集路径')

    monkeypatch.setattr(collectors, 'collect', boom)
    repo = Repository(tmp_path)
    repo.save_job(dict(title='后端工程师', company='甲公司', description='负责服务端开发', location='北京',
                       salary='20-35K', url='https://jobs.example.com/1'), 'test_jobs')
    repo.save_job(dict(title='架构师', company='甲公司', description='负责架构', location='北京',
                       salary='年薪 60 万元', url='https://jobs.example.com/2'), 'test_jobs')
    repo.save_evidence(dict(title='2024 年度报告', company='甲公司', kind='disclosure',
                            text='参保人数 240 人', url='https://example.com/nbgs'), 'user_import')

    def coverage(signals, rule):
        return next(item for item in signals if item['rule'] == rule)

    first = client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')).json()
    salary = coverage(first['signals'], 'salary_quote_coverage')
    assert salary['value'] == {'known': 1, 'total': 2} and salary['source_class'] == 'platform'
    stored = {item['id']: item for item in client.get('/api/evidence', params=dict(company='甲公司')).json()['evidence']}
    # 口径必须落在证据正文里，但正文不带计算时刻：证据身份含内容哈希，带上时间每次点击都会多生一份记录。
    evidence = stored[salary['evidence_id']]
    assert evidence['url'] == '' and evidence['source'] == 'jobscope_jobs'
    assert 'salary_quote_coverage' in evidence['text'] and '不访问网络' in evidence['text']
    assert salary['collected_at'] and salary['evidence_id'] == evidence['id']
    # 自己写下的口径记录不能被自己再数一遍：按钮点第二次，公告计数不许 +1，证据与信号条数也不许增长。
    before = client.get('/api/evidence', params=dict(company='甲公司')).json()['total']
    second = client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')).json()
    assert client.get('/api/evidence', params=dict(company='甲公司')).json()['total'] == before
    assert second['signals'][0]['evidence_id'] == salary['evidence_id']
    assert coverage(second['signals'], 'disclosure_available')['value'] == {'count': 1}
    assert coverage(second['signals'], 'salary_quote_coverage')['value'] == {'known': 1, 'total': 2}
    # 两条都是 material：有了 reader 也不许冒出分数。
    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    for dimension in ('pay', 'operations'):
        line = next(item for item in report['dimensions'] if item['dimension'] == dimension)
        assert line['score'] is None and line['confidence'] == 0
        assert [item['kind'] for item in line['findings']] == ['material']
        assert [item['score'] for item in line['findings']] == [None]


def test_collect_stores_the_page_the_signal_came_from(client, monkeypatch):
    """出处缺失等于法律兜底失效：落 evidence 时用的必须是采集器实际访问过的那个 URL。"""
    from jobscope.reliability import collectors
    visited = 'https://www.gsxt.gov.cn/index.html?keyword=%E7%94%B2%E5%85%AC%E5%8F%B8'

    def fake_collect(source, company, **kwargs):
        return dict(status='completed', message='采集完成。', signals=[dict(
            rule='company_age', dimension='stability', value={'years': 8.0}, unit='年',
            source_class='official', observed_at='2015-04-01', url=visited, source=source)])

    monkeypatch.setattr(collectors, 'collect', fake_collect)
    result = client.post('/api/companies/甲公司/signals/collect', json=dict(source='gsxt')).json()
    signal = result['signals'][0]
    stored = client.get('/api/evidence', params=dict(company='甲公司')).json()['evidence'][0]
    assert signal['evidence_id'] == stored['id']
    assert stored['url'] == visited
    assert stored['source'] == 'gsxt' and stored['kind'] == 'disclosure'


# 付费接口的假响应：字段名来自 sources.SIGNAL_SOURCES['qcc']['fields'] 那份待校正映射。
PAID_PAYLOAD = {'data': {'result': {'establish_date': '2015-04-01',
                                    'insured_num': 240, 'insured_num_last': 312, 'case_num': 3,
                                    'avg_salary': 20000, 'city_avg_salary': 16000,
                                    'salary_sample_num': 5, 'city_job_num': 40,
                                    'revenue_current': 120000000, 'revenue_previous': 100000000,
                                    'employee_num': 240, 'employee_num_previous': 260}}}
PAID_URL = 'https://api.vendor.example/v1/company'


def enable_paid(client, monkeypatch, payload=PAID_PAYLOAD):
    """启用付费来源并把取数接到假响应上：整个测试不落一次网络。"""
    from jobscope.reliability.sources import PaidClient

    monkeypatch.setattr(PaidClient, 'fetch', lambda self, company: payload)
    result = client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key=SECRET, url=PAID_URL))
    assert result.status_code == 200
    return result


def test_a_paid_source_without_opt_in_never_reaches_the_fetch_path(client, monkeypatch):
    from jobscope.reliability import paid

    def boom(*args, **kwargs):
        raise AssertionError('未启用的付费来源不该进入取数路径')

    monkeypatch.setattr(paid, 'collect', boom)
    assert client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc')).status_code == 409


def test_a_paid_source_marked_enabled_but_keyless_is_still_not_collected(client, tmp_path, monkeypatch):
    """库里被手工写成 enabled 却没有密钥时，闸门照样拦住：取数路径一次都不能进。"""
    from jobscope.reliability import paid
    from jobscope.reliability.sources import PaidClient

    def boom(*args, **kwargs):
        raise AssertionError('没有密钥不该发起请求')

    monkeypatch.setattr(PaidClient, 'fetch', boom)
    monkeypatch.setattr(paid, 'collect', boom)
    from jobscope.reliability.sources import SignalSourceStore
    store = SignalSourceStore(Repository(tmp_path))  # 建表；写入用原始 SQL，绕开 save() 的密钥校验
    with store.repo.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO signal_sources VALUES (?,?)',
                     ('qcc', json.dumps(dict(mode='enabled', tier='paid', url=PAID_URL))))
    result = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc'))
    assert result.status_code == 409 and '密钥' in result.json()['detail']


def test_an_enabled_paid_source_collects_and_never_stores_the_key(client, monkeypatch, tmp_path):
    from jobscope.reliability import collectors

    def no_browser(*args, **kwargs):
        raise AssertionError('付费来源不该走浏览器采集')

    monkeypatch.setattr(collectors, 'collect', no_browser)
    enable_paid(client, monkeypatch)
    result = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc'))
    assert result.status_code == 200
    body = result.json()
    assert body['status'] == 'completed'
    assert {item['rule'] for item in body['signals']} == {
        'company_age', 'insured_headcount_trend', 'judicial_case_ratio',
        'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'}
    assert all(item['source_class'] == 'paid_api' for item in body['signals'])
    assert SECRET not in result.text
    stored = client.get('/api/evidence', params=dict(company='甲公司'))
    assert SECRET not in stored.text
    for item in stored.json()['evidence']:
        # 出处是配置里的接口地址本身：密钥走请求头，绝不进 URL、也不进证据正文。
        assert item['url'] == PAID_URL and item['source'] == 'qcc'
        assert item['kind'] == 'paid_api_record'
    with Repository(tmp_path).connection() as conn:
        rows = [row[0] for row in conn.execute('SELECT payload FROM evidence').fetchall()]
    assert all(SECRET not in row for row in rows)


def test_paid_signals_are_the_first_thing_that_can_actually_score_stability(client, monkeypatch):
    enable_paid(client, monkeypatch)
    client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc'))
    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    stability = next(item for item in report['dimensions'] if item['dimension'] == 'stability')
    assert stability['score'] is not None and report['score'] is not None
    assert {item['source_class'] for item in stability['findings']} == {'paid_api'}
    assert report['evidence_ids'] and stability['evidence_ids']


def test_recollecting_the_same_paid_payload_does_not_grow_the_evidence_trail(client, monkeypatch):
    enable_paid(client, monkeypatch)
    first = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc')).json()
    total = client.get('/api/evidence', params=dict(company='甲公司')).json()['total']
    second = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc')).json()
    assert client.get('/api/evidence', params=dict(company='甲公司')).json()['total'] == total
    assert [item['evidence_id'] for item in second['signals']] == [item['evidence_id'] for item in first['signals']]


def test_a_paid_response_that_is_not_an_object_is_reported_without_any_signal(client, monkeypatch):
    enable_paid(client, monkeypatch, payload='网关返回了一段 HTML')
    result = client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc')).json()
    assert result['status'] == 'unsupported' and result['signals'] == []
    assert result['message']
    assert client.get('/api/evidence', params=dict(company='甲公司')).json()['total'] == 0


def test_paid_records_are_not_fed_back_into_the_model_as_material(client, monkeypatch):
    """付费接口原文是取数结果，不是给抽取器的材料：否则厂商字段会第二轮变成「证据」。"""
    from jobscope.reliability import extract

    def no_model(*args, **kwargs):
        raise AssertionError('没有可抽取材料时不该调用模型')

    monkeypatch.setattr(extract, 'extract', no_model)
    enable_paid(client, monkeypatch)
    client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc'))
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'skipped' and result['evidence'] == 0



def test_twelve_local_jobs_score_pay_and_professional_offline(client, tmp_path):
    """P1 验收：12 条已存岗位（≥3 完整 JD、≥8 同城可解析报价），不联网、不配模型，
    只跑本地采集与重算，pay 与 professional 必须出分，综合覆盖度显著高于 P0 的 0.22。"""
    full_jd = ('岗位职责：\n负责核心服务开发，支撑日活 500 万的业务\n'
               '任职要求：\n3 年以上 Python 后端经验，熟悉 MySQL、Redis 与 Kubernetes')
    repo = Repository(tmp_path)
    for index in range(8):
        repo.save_job(dict(title=f'后端工程师{index}', company='甲公司', location='北京',
                           salary='35-45K', description=full_jd, url=f'https://jobs.example.com/{index}',
                           description_status='complete'), 'test_jobs')
    repo.save_job(dict(title='架构师', company='甲公司', location='北京', salary='年薪 80 万元',
                       description=full_jd, url='https://jobs.example.com/9', description_status='complete'), 'test_jobs')
    for index in range(3):
        repo.save_job(dict(title=f'产品经理{index}', company='甲公司', location='北京', salary='面议',
                           description=full_jd, url=f'https://jobs.example.com/p{index}',
                           description_status='listing'), 'test_jobs')
    for index in range(7):
        repo.save_job(dict(title=f'测试工程师{index}', company='乙公司', location='北京',
                           salary='15-25K', description='负责测试', url=f'https://jobs.example.com/t{index}',
                           description_status='listing'), 'test_jobs')
    for index in range(7):
        repo.save_job(dict(title=f'运维工程师{index}', company='乙公司', location='北京',
                           salary='25-35K', description='负责运维', url=f'https://jobs.example.com/o{index}',
                           description_status='listing'), 'test_jobs')

    collected = client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')).json()
    assert collected['status'] == 'completed'
    assert {item['rule'] for item in collected['signals']} == {
        'salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median', 'jd_specificity',
        'industry_media_count'}
    pay_signal = next(item for item in collected['signals'] if item['rule'] == 'pay_vs_city_median')
    assert pay_signal['value'] == {'company_median': 40000.0, 'city_median': 30000.0, 'ratio': 1.333,
                                   'company_jobs': 8, 'city_jobs': 22}

    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    dimensions = {item['dimension']: item for item in report['dimensions']}
    assert dimensions['pay']['score'] == 95.0          # 比值 1.333 ≥ 1.2
    assert dimensions['professional']['score'] is not None
    for name in ('stability', 'people', 'operations', 'industry'):
        assert dimensions[name]['score'] is None, name
    assert dimensions['industry']['findings'][0]['rule'] == 'industry_media_count'
    assert dimensions['industry']['findings'][0]['kind'] == 'material'
    # 覆盖度 = pay(0.20) + professional(0.12) 的权重和，显著高于 P0 只有 stability 时的 0.22。
    assert report['confidence'] == 0.32 and report['score'] is not None
    assert '"score":null' in client.get('/api/companies/甲公司/reliability').text
    # 幂等：同一输入重复重算 input_hash 不变（P0 修过的行为不许退化）。
    first_hash = report['input_hash']
    assert client.post('/api/companies/甲公司/reliability', json=dict()).json()['input_hash'] == first_hash
    again = client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')).json()
    assert [item['evidence_id'] for item in again['signals']] == [item['evidence_id'] for item in collected['signals']]


def test_history_endpoint_serves_one_company_trend_only(client, tmp_path):
    """单公司历史趋势：diff 只归因到这家公司自己两次快照之间变了的规则，读路径不写任何行。"""
    assert client.get('/api/companies/甲公司/reliability/history').json() == {'history': None}
    add_signal(tmp_path, 'company_age', {'years': 8}, 'E-' + 'a' * 24)
    client.post('/api/companies/甲公司/reliability', json=dict())
    add_signal(tmp_path, 'pay_vs_city_median', {'company_median': 20000, 'city_median': 18000, 'ratio': 1.11,
                                                'company_jobs': 5, 'city_jobs': 30}, 'E-' + 'b' * 24)
    add_signal(tmp_path, 'disclosure_available', {'count': 3}, 'E-' + 'c' * 24, source_class='official')
    client.post('/api/companies/甲公司/reliability', json=dict())
    history = client.get('/api/companies/甲公司/reliability/history').json()['history']
    assert history['company'] == '甲公司' and len(history['items']) == 2
    # assessment 的分数变化归因到规则；material 只报登记出现，不带任何数字。
    diff = history['diffs'][0]
    assert [item['rule'] for item in diff['rules']] == ['pay_vs_city_median']
    assert diff['rules'][0]['before'] is None and diff['rules'][0]['after'] == 80.0
    assert [item['rule'] for item in diff['materials']] == ['disclosure_available']
    assert diff['materials'][0]['appeared'] is True
    # 再加一家公司：乙的任何东西都不出现在甲的趋势里——趋势只对比同一家公司自己。
    add_signal(tmp_path, 'company_age', {'years': 2}, 'E-' + 'd' * 24, company='乙公司')
    client.post('/api/companies/乙公司/reliability', json=dict())
    other = client.get('/api/companies/乙公司/reliability/history').json()['history']
    assert other['company'] == '乙公司' and len(other['items']) == 1 and other['diffs'] == []
    assert '乙' not in json.dumps(history, ensure_ascii=False)
    # 读路径不写行：两次 GET 之后 companies 表里只有那两家真实重算过的公司。
    assert client.get('/api/companies/没有的公司/reliability/history').json() == {'history': None}
    with Repository(tmp_path).connection() as conn:
        assert conn.execute('SELECT count(*) FROM companies').fetchone()[0] == 2


def test_extracted_team_size_statements_aggregate_into_one_signal(client, tmp_path, monkeypatch):
    """模型逐条材料交原始数字，聚合在代码里完成：两条相容自述 → 一条 median/statements/compatible 信号。"""
    model_on(monkeypatch)
    first = client.post('/api/evidence', json=dict(title='岗位描述', company='甲公司', kind='company_statement',
                                                   text='团队规模 30 人，后端组 12 人')).json()['evidence']
    second = client.post('/api/evidence', json=dict(title='公司介绍', company='甲公司', kind='company_statement',
                                                    text='我们是一支约 45 人的团队')).json()['evidence']
    content = json.dumps(dict(signals=[
        dict(rule='team_size_statement', dimension='people', value={'size': 30}, evidence_id=first['id']),
        dict(rule='team_size_statement', dimension='people', value={'size': 45}, evidence_id=second['id']),
    ]), ensure_ascii=False)
    mock_model(monkeypatch, content)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'completed' and result['accepted'] == 1
    saved = result['signals'][0]
    assert saved['value'] == {'median_size': 37.5, 'statements': 2, 'compatible': 2}
    # 锚条与参与编号都留在任务说明里：出处链查得回去。
    assert first['id'] in result['message'] and second['id'] in result['message']
    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    people = next(item for item in report['dimensions'] if item['dimension'] == 'people')
    assert people['score'] == 90.0 and people['findings'][0]['evidence_id'] in (first['id'], second['id'])
    assert '相互兼容' in people['findings'][0]['text']


def test_complete_job_descriptions_enter_the_extractor_but_summaries_do_not(client, tmp_path, monkeypatch):
    """R3 的「团队 N 人」写在岗位描述里：完整 JD 的岗位证据进材料，列表摘要仍然不进。"""
    model_on(monkeypatch)
    repo = Repository(tmp_path)
    repo.save_job(dict(title='后端工程师', company='甲公司', description='岗位职责：\n负责核心服务开发，团队 500 人',
                       location='北京', salary='20-35K', url='https://jobs.example.com/1',
                       description_status='complete'), 'test_jobs')
    repo.save_job(dict(title='客服专员', company='甲公司', description='客服',
                       location='北京', salary='8-12K', url='https://jobs.example.com/2',
                       description_status='listing'), 'test_jobs')
    jd_evidence = next(item for item in repo.evidence(include_job_postings=True)
                       if item['kind'] == 'job_posting' and '团队 500 人' in item['text'])
    summary_evidence = next(item for item in repo.evidence(include_job_postings=True)
                            if item['kind'] == 'job_posting' and '团队 500 人' not in item['text'])
    content = json.dumps(dict(signals=[
        dict(rule='team_size_statement', dimension='people', value={'size': 500}, evidence_id=jd_evidence['id']),
        dict(rule='team_size_statement', dimension='people', value={'size': 3}, evidence_id=summary_evidence['id']),
    ]), ensure_ascii=False)
    mock_model(monkeypatch, content)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['evidence'] == 1 and result['status'] == 'completed'
    # 摘要那一条被整批校验拒掉：evidence_id 不在传入材料里；完整 JD 那条只有一份自述，不成聚合、原样入库。
    assert '不在传入的证据里' in result['message']
    assert result['accepted'] == 1
    saved = result['signals'][0]
    assert saved['value'] == {'size': 500}
    assert saved['source_class'] == 'platform' and saved['evidence_id'] == jd_evidence['id']
def test_reading_a_report_does_not_leave_a_company_row_behind(client, tmp_path):
    """GET 是读操作：查一个拼错的公司名必须在 companies 表里什么都不留。"""
    def rows():
        with Repository(tmp_path).connection() as conn:
            return conn.execute('SELECT count(*) FROM companies').fetchone()[0]

    assert rows() == 0
    for name in ('拼错了的公搜', '甲公司'):
        assert client.get(f'/api/companies/{name}/reliability').json() == {'report': None}
    assert client.get('/api/companies/甲公司/reliability', params=dict(credit_code='91110000MA01ABCD7X')).json() == {'report': None}
    assert rows() == 0
    # 空白名同样只是 422，不写行。
    assert client.get('/api/companies/%20/reliability').status_code == 422
    assert rows() == 0
    # POST 重算仍然创建实体，这是对的。
    built = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    assert rows() == 1
    assert client.get('/api/companies/甲公司/reliability').json()['report']['input_hash'] == built['input_hash']
    assert rows() == 1


def test_extract_without_a_model_skips_and_still_records_the_run(client):
    evidence = add_material(client)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict())
    assert result.status_code == 200
    body = result.json()
    assert body['status'] == 'skipped' and body['accepted'] == 0
    assert body['evidence'] == 1 and body['signals'] == [] and body['message']
    task = extract_tasks(client)[0]
    assert task['source'] == 'reliability_extract' and task['query'] == '甲公司'
    assert task['status'] == 'skipped' and task['count'] == 0 and task['message']
    assert evidence['id']


def test_extract_records_degraded_status_and_keeps_the_raw(client, monkeypatch):
    model_on(monkeypatch)
    mock_model(monkeypatch, '这家公司整体不错，员工评价也挺好')
    add_material(client)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'degraded' and result['accepted'] == 0 and result['signals'] == []
    task = extract_tasks(client)[0]
    assert task['status'] == 'degraded' and task['count'] == 0
    assert '这家公司整体不错' in task['raw'] and len(task['raw']) <= 4000


def test_extract_accepted_signal_carries_the_evidence_kind_source_class(client, monkeypatch):
    model_on(monkeypatch)
    evidence = add_material(client)
    content = json.dumps(dict(signals=[dict(rule='disclosure_available', dimension='operations',
                                            value={'count': 3}, evidence_id=evidence['id'])]), ensure_ascii=False)
    mock_model(monkeypatch, content)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'completed' and result['accepted'] == 1
    saved = result['signals'][0]
    assert saved['rule'] == 'disclosure_available' and saved['value'] == {'count': 3}
    assert saved['evidence_id'] == evidence['id'] and saved['source_class'] == 'official'
    assert extract_tasks(client)[0]['status'] == 'completed'
    # 抽到的信号必须真的进得了报告：material 类只出文字不出数字。
    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    operations = next(item for item in report['dimensions'] if item['dimension'] == 'operations')
    assert operations['findings'][0]['kind'] == 'material'
    assert operations['findings'][0]['score'] is None
    assert operations['findings'][0]['evidence_id'] == evidence['id']


def test_extract_returns_every_rejection_reason(client, monkeypatch):
    model_on(monkeypatch)
    evidence = add_material(client)
    content = json.dumps(dict(signals=[
        dict(rule='company_age', dimension='stability', value={'years': 5}, evidence_id='E-forged'),
        dict(rule='is_it_a_good_company', dimension='stability', value={'x': 1}, evidence_id=evidence['id']),
    ]), ensure_ascii=False)
    mock_model(monkeypatch, content)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'completed' and result['accepted'] == 0 and result['signals'] == []
    assert 'evidence_id' in result['message'] and '未注册' in result['message']


def test_extract_with_no_material_skips_without_calling_the_model(client, monkeypatch):
    model_on(monkeypatch)
    calls = []
    mock_model(monkeypatch, '{"signals":[]}', calls)
    result = client.post('/api/companies/甲公司/signals/extract', json=dict()).json()
    assert result['status'] == 'skipped' and result['accepted'] == 0 and result['evidence'] == 0
    assert calls == [] and extract_tasks(client)[0]['status'] == 'skipped'


def test_every_persisted_finding_cites_evidence_and_material_has_no_number(client, tmp_path):
    add_signal(tmp_path, 'company_age', {'years': 8}, 'E-' + 'a' * 24)
    add_signal(tmp_path, 'disclosure_available', {'count': 3}, 'E-' + 'b' * 24)
    add_signal(tmp_path, 'employee_review_sample', {'samples': 3, 'negative': 3}, 'E-' + 'c' * 24, 'employee')
    built = client.post('/api/companies/甲公司/reliability', json=dict())
    assert built.status_code == 200
    # 序列化结果里必须是 null，不能是 0 或字段消失。
    assert '"score":null' in built.text
    stored = client.get('/api/companies/甲公司/reliability').json()['report']
    for report in (built.json(), stored):
        items = findings_of(report)
        assert len(items) == 3
        assert all(item['evidence_id'] for item in items)
        assert {item['kind'] for item in items} == {'assessment', 'material'}
        assert [item['score'] for item in items if item['kind'] == 'material'] == [None, None]
        assert next(item['score'] for item in items if item['kind'] == 'assessment') is not None
