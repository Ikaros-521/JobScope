"""漏斗转化与归因（事件流 + 转化率/归因切片）与触达话术版本。

事件流是用户意图的推进历史：只断言状态变化落事件、漏斗口径（曾到达）、归因三组切片
与可靠度分带校准，不碰模型。触达走规则兜底（测试环境未配模型），断言版本落库与使用标记。
"""
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.general_agent import GeneralAgent
from jobscope.main import create_app
from jobscope.repository import Repository

import pytest


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(title='Python工程师', company='甲公司', source='boss', salary='20-30K', location='北京', **extra):
    return dict(title=title, company=company, description='岗位原文', salary=salary, location=location, **extra)


def patch_pipeline(repo, job_id, status):
    repo.update_jobs([job_id], {'pipeline_status': status})


def events_of(repo, job_id):
    with repo.connection() as conn:
        return [dict(row) for row in conn.execute(
            'SELECT from_status,to_status,created_at FROM job_pipeline_events WHERE job_id=? ORDER BY id', (job_id,))]


def test_status_change_records_event_once(repo):
    job = repo.save_job(make_job(), 'boss')
    patch_pipeline(repo, job['id'], 'applied')
    patch_pipeline(repo, job['id'], 'interviewing')
    # 非状态字段批量更新不落事件。
    repo.update_jobs([job['id']], {'location': '上海'})
    assert [dict(from_status=e['from_status'], to_status=e['to_status']) for e in events_of(repo, job['id'])] == [
        {'from_status': '', 'to_status': 'applied'}, {'from_status': 'applied', 'to_status': 'interviewing'}]
    # 显式移出管线落离场事件。
    patch_pipeline(repo, job['id'], '')
    assert events_of(repo, job['id'])[-1]['to_status'] == ''
    # 取消收藏连带清状态，同样是一条事件。
    repo.update_jobs([job['id']], {'starred': True})
    patch_pipeline(repo, job['id'], 'applied')
    repo.update_jobs([job['id']], {'starred': False})
    assert events_of(repo, job['id'])[-1]['to_status'] == ''


def test_funnel_ever_reached_counts_and_rates(repo):
    job1 = repo.save_job(make_job(external_id='a'), 'boss')
    for status in ('interested', 'applied', 'interviewing', 'offer'):
        patch_pipeline(repo, job1['id'], status)
    job2 = repo.save_job(make_job(external_id='b'), 'boss')
    for status in ('interested', 'applied'):
        patch_pipeline(repo, job2['id'], status)
    job3 = repo.save_job(make_job(external_id='c'), 'boss')
    patch_pipeline(repo, job3['id'], 'interested')

    funnel = repo.pipeline_funnel()
    counts = {item['status']: item['count'] for item in funnel['stages']}
    assert counts == {'interested': 3, 'applied': 2, 'interviewing': 1, 'offer': 1, 'archived': 0}
    steps = {(step['from'], step['to']): step for step in funnel['steps']}
    assert steps[('interested', 'applied')]['rate'] == pytest.approx(0.667)
    assert steps[('applied', 'interviewing')]['rate'] == pytest.approx(0.5)
    assert steps[('interviewing', 'offer')]['rate'] == pytest.approx(1.0)
    # 推进周期：中位/四分位 1 条样本也算，均值随行（P2：小样本标注由前端做）。
    assert steps[('applied', 'interviewing')]['cycle_n'] == 1
    assert steps[('interviewing', 'offer')]['cycle_days'] is not None
    assert steps[('interviewing', 'offer')]['median_days'] is not None
    assert steps[('interviewing', 'offer')]['p25_days'] is not None


def test_funnel_attribution_slices(repo):
    boss_job = repo.save_job(make_job(external_id='a', source='boss'), 'boss')
    patch_pipeline(repo, boss_job['id'], 'applied')
    patch_pipeline(repo, boss_job['id'], 'interviewing')
    other_job = repo.save_job(make_job(external_id='b', source='liepin'), 'liepin')
    patch_pipeline(repo, other_job['id'], 'applied')
    # 定制简历的岗位：简历版本挂 job_id，归因进 custom 桶。
    repo.save_resume_version(dict(title='定制', job_id=boss_job['id'], markdown='# 定制'))

    funnel = repo.pipeline_funnel()
    by_source = {item['key']: item for item in funnel['attribution']['by_source']}
    assert by_source['boss']['applied'] == 1 and by_source['boss']['interviewing'] == 1
    assert by_source['liepin']['applied'] == 1 and by_source['liepin']['interviewing'] == 0
    assert by_source['liepin']['interview_rate'] == 0.0
    by_resume = {item['key']: item for item in funnel['attribution']['by_resume']}
    assert by_resume['custom']['applied'] == 1 and by_resume['general']['applied'] == 1


def test_funnel_reliability_band_is_calibration_report(repo):
    scored = repo.save_job(make_job(title='甲岗位', company='甲公司', external_id='a'), 'boss')
    patch_pipeline(repo, scored['id'], 'applied')
    unrated = repo.save_job(make_job(title='乙岗位', company='乙公司', external_id='b'), 'boss')
    patch_pipeline(repo, unrated['id'], 'applied')
    # 甲公司已评分（8+ 分带），乙公司没有——分带切片就是「评分 vs 约面」的校准报告。
    with repo.connection() as conn:
        conn.execute("INSERT INTO company_reliability (company_id,input_hash,created_at,payload) "
                     "SELECT id,'h','2026-11-22T00:00:00+00:00','{\"score\": 8.5}' FROM companies WHERE name='甲公司'")
    funnel = repo.pipeline_funnel()
    bands = {item['key']: item for item in funnel['attribution']['by_reliability']}
    assert bands['8+']['applied'] == 1
    assert bands['unassessed']['applied'] == 1


def test_funnel_legacy_archived_without_events_excluded_from_denominator(repo):
    job = repo.save_job(make_job(), 'boss')
    # 模拟事件流上线前的存量数据：直接改 payload，不经 update_jobs（不留事件）。
    with repo.connection() as conn:
        item = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job['id'],)).fetchone()['payload'])
        item['pipeline_status'] = 'archived'
        conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(item, ensure_ascii=False), job['id']))
    funnel = repo.pipeline_funnel()
    assert funnel['archived_untracked'] == 1
    counts = {item['status']: item['count'] for item in funnel['stages']}
    assert counts['archived'] == 1 and counts['applied'] == 0


def test_pipeline_review_api_includes_funnel(client):
    job = client.app.state.repo.save_job(make_job(), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'applied'})
    payload = client.get('/api/jobs/pipeline/review').json()
    assert payload['funnel']['stages'][1] == {'status': 'applied', 'count': 1}


# ── 触达话术（规则兜底口径，测试环境未配模型） ──

def test_outreach_fallback_and_versions(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(tags=['Python', 'SQL']), 'boss')

    version = client.post(f'/api/jobs/{job["id"]}/outreach', json={'kind': 'greeting'}).json()
    assert version['origin'] == 'rules' and job['title'] in version['text']

    followup = client.post(f'/api/jobs/{job["id"]}/outreach', json={'kind': 'followup'}).json()
    assert '投递' in followup['text']

    listed = client.get(f'/api/jobs/{job["id"]}/outreach').json()['versions']
    assert [item['kind'] for item in listed] == ['followup', 'greeting']

    assert client.post(f'/api/jobs/{job["id"]}/outreach/{version["id"]}/used').json() == {'marked': True}
    assert client.post(f'/api/jobs/{job["id"]}/outreach/{version["id"]}/used').status_code == 404
    # 话术版本不进管线事件流。
    assert events_of(repo, job['id']) == []


def test_outreach_unknown_job_and_kind(client):
    assert client.post('/api/jobs/J-none/outreach', json={'kind': 'greeting'}).status_code == 422
    job = client.app.state.repo.save_job(make_job(), 'boss')
    assert client.post(f'/api/jobs/{job["id"]}/outreach', json={'kind': 'other'}).status_code == 422


def test_agent_pipeline_funnel_tool_reads_real_data(tmp_path):
    from jobscope.extensions import fingerprint
    agent = GeneralAgent(Repository(tmp_path))
    job = agent.repo.save_job(make_job(), 'boss')
    agent.repo.update_jobs([job['id']], {'pipeline_status': 'applied'})
    scope = dict(kind='global')
    state = dict(scope=dict(scope), scope_binding=fingerprint(scope), run=dict(scope=dict(scope)), evidence={}, metrics=None, actions=[])
    result = agent._builtin(state, 'pipeline_funnel', {})
    counts = {item['status']: item['count'] for item in result['stages']}
    assert counts['applied'] == 1 and 'note' in result


# ── 每周节奏与 Offer 对比（P1） ──


def test_percentiles_compute_from_single_sample(repo):
    # P2 的立场：1 条样本就计算，参考价值靠 n 标注，不由统计层「等数据」。
    assert Repository._percentiles([]) == (None, None, None, 0)
    assert Repository._percentiles([5.0]) == (5.0, 5.0, 5.0, 1)
    # 极端值只污染 P75，中位数稳：[2,3,4,40] → P25=2.8 中位=3.5 P75=13.0。
    assert Repository._percentiles([2.0, 3.0, 4.0, 40.0]) == (2.8, 3.5, 13.0, 4)


def test_funnel_cohorts_and_weekday(repo):
    from datetime import datetime, timedelta, timezone
    from jobscope.repository import iso_week_start
    monday = iso_week_start(datetime.now(timezone.utc))
    job1 = repo.save_job(make_job(external_id='a'), 'boss')
    job2 = repo.save_job(make_job(external_id='b'), 'boss')
    # 直插事件+直接落 payload（不经 update_jobs，避免 now() 事件戳混入）：
    # job1 周二投、周四进面试；job2 上周日投。星期几断言与运行日期无关且互不碰撞。
    with repo.connection() as conn:
        for job_id, status in ((job1['id'], 'interviewing'), (job2['id'], 'applied')):
            item = json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()['payload'])
            item['pipeline_status'] = status
            conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), job_id))
        stamps = [
            (job1['id'], '', 'applied', (monday + timedelta(days=1, hours=10)).isoformat()),            # 本周二
            (job1['id'], 'applied', 'interviewing', (monday + timedelta(days=3, hours=10)).isoformat()),  # 本周四
            (job2['id'], '', 'applied', (monday - timedelta(days=1)).isoformat()),                       # 上周日
        ]
        for job_id, from_status, to_status, stamp in stamps:
            conn.execute('INSERT INTO job_pipeline_events (job_id,from_status,to_status,created_at) VALUES (?,?,?,?)',
                         (job_id, from_status, to_status, stamp))

    funnel = repo.pipeline_funnel()
    cohorts = {item['week_start']: item for item in funnel['cohorts']}
    this_week, last_week = monday.date().isoformat(), (monday - timedelta(weeks=1)).date().isoformat()
    assert cohorts[this_week]['applied'] == 1 and cohorts[this_week]['interviewing'] == 1 and cohorts[this_week]['active'] == 1
    assert cohorts[last_week]['applied'] == 1 and cohorts[last_week]['interviewing'] == 0 and cohorts[last_week]['active'] == 1
    by_weekday = {item['weekday']: item for item in funnel['by_weekday']}
    assert by_weekday[2]['applied'] == 1 and by_weekday[2]['interviewing'] == 1
    assert by_weekday[7]['applied'] == 1 and by_weekday[7]['interviewing'] == 0 and by_weekday[7]['rate'] == 0.0


def test_pipeline_weekly_buckets_iso_weeks(repo):
    from datetime import datetime, timedelta, timezone
    from jobscope.repository import iso_week_start
    job1 = repo.save_job(make_job(external_id='a'), 'boss')
    job2 = repo.save_job(make_job(external_id='b'), 'boss')
    this_monday = iso_week_start(datetime.now(timezone.utc))
    stamps = {
        job1['id']: (this_monday + timedelta(days=1)).isoformat(),   # 本周投递
        job2['id']: (this_monday - timedelta(days=6)).isoformat(),   # 上周投递（上周日）
    }
    with repo.connection() as conn:
        for job_id, stamp in stamps.items():
            conn.execute('INSERT INTO job_pipeline_events (job_id,from_status,to_status,created_at) VALUES (?,?,?,?)',
                         (job_id, '', 'applied', stamp))
    repo.update_jobs([job1['id']], {'starred': True})  # 新收藏落本周
    weekly = repo.pipeline_weekly()
    assert len(weekly['weeks']) == 4
    assert weekly['weeks'][-1]['applied'] == 1 and weekly['weeks'][-1]['new_favorites'] == 1
    assert weekly['weeks'][-2]['applied'] == 1


def test_offer_comparison_bands_and_baseline(repo):
    offer1 = repo.save_job(make_job(title='Python 工程师', company='甲公司', location='杭州', salary='20-30K', external_id='a'), 'boss')
    repo.update_jobs([offer1['id']], {'pipeline_status': 'offer'})
    offer2 = repo.save_job(make_job(title='运维工程师', company='乙公司', location='北京', salary='15-20K', external_id='b'), 'boss')
    repo.update_jobs([offer2['id']], {'pipeline_status': 'offer'})
    repo.save_wage_baselines([{'city': '杭州', 'role': 'Python', 'education': '本科', 'year': '2025', 'monthly_median': 20000.0}])
    with repo.connection() as conn:
        conn.execute("INSERT INTO company_reliability (company_id,input_hash,created_at,payload) "
                     "SELECT id,'h','2026-11-22T00:00:00+00:00','{\"score\": 8.5}' FROM companies WHERE name='甲公司'")
    offers = {item['id']: item for item in repo.offer_comparison()}
    one, two = offers[offer1['id']], offers[offer2['id']]
    assert one['reliability_band'] == '8+'
    assert one['baseline_median'] == 20000 and one['baseline_note'].startswith('杭州·Python')
    assert one['baseline_delta'] == 0.25  # (25000-20000)/20000
    # 城市+角色都不可比的岗位不给基线差值，不硬凑。
    assert two['baseline_median'] is None and two['baseline_delta'] is None
    assert two['reliability_band'] is None


def test_offer_comparison_api_and_dashboard_weekly(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(salary='20-30K'), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'offer'})
    review = client.get('/api/jobs/pipeline/review').json()
    assert [item['id'] for item in review['offers']] == [job['id']]
    dashboard = client.get('/api/dashboard').json()
    assert len(dashboard['weekly']['weeks']) == 4
    assert dashboard['weekly']['weeks'][-1]['new_favorites'] >= 1
