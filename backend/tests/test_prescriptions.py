"""行动处方引擎：漏斗数据 → 处方卡 + 今日行动清单。规则版，无 LLM。

断言规则触发条件与诚实降级：样本不足不诊断、空窗就绪只提醒一次口径、
跟进积压合并逾期与停滞、今日已投数取当日事件。时间敏感断言直插事件戳
（同 test_pipeline_funnel 的直插范式）。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.prescription import build
from jobscope.repository import Repository

import pytest


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(title='Python工程师', company='甲公司', **extra):
    return dict(title=title, company=company, description='岗位原文', salary='20-30K', location='北京', **extra)


def patch_pipeline(repo, job_id, status):
    repo.update_jobs([job_id], {'pipeline_status': status})


def backdate_changed_at(repo, job_id, days):
    """状态变化时间改到 N 天前：停滞规则的时间锚（update_jobs 恒写 now，必须直改 payload）。"""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with repo.connection() as conn:
        conn.execute("UPDATE jobs SET payload=json_set(payload,'$.pipeline_status_changed_at',?) WHERE id=?",
                     (stamp, job_id))


def insert_event(repo, job_id, to_status, created_at):
    with repo.connection() as conn:
        conn.execute('INSERT INTO job_pipeline_events (job_id,from_status,to_status,created_at) VALUES (?,?,?,?)',
                     (job_id, '', to_status, created_at))


def ids_of(prescriptions):
    return {item['id'] for item in prescriptions}


def test_empty_db_small_sample_and_today_plan(repo):
    result = build(repo)
    assert ids_of(result['prescriptions']) == {'small_sample'}
    plan = result['today_plan']
    assert plan['targets']['apply'] == 3 and '小目标' in plan['targets']['apply_basis']
    assert plan['done']['apply'] == 0 and plan['follow_ups'] == 0


def test_bottleneck_points_at_worst_step(repo):
    # 6 个岗位全部推进到已投递、零约面：applied→interviewing 转化率 0 是最低环节。
    for index in range(6):
        job = repo.save_job(make_job(external_id=f'b{index}'), 'boss')
        patch_pipeline(repo, job['id'], 'applied')
    result = build(repo)
    assert 'small_sample' not in ids_of(result['prescriptions'])
    bottleneck = next(item for item in result['prescriptions'] if item['id'] == 'bottleneck')
    assert '已投递→面试中' in bottleneck['title']
    assert bottleneck['target'] == 'workshop'


def test_followup_backlog_merges_overdue_and_stale(repo):
    job = repo.save_job(make_job(), 'boss')
    repo.update_jobs([job['id']], {'pipeline_status': 'applied', 'pipeline_due': '2000-01-01', 'pipeline_note': '催结果'})
    backdate_changed_at(repo, job['id'], 20)
    stale_job = repo.save_job(make_job(company='乙公司', external_id='s1'), 'boss')
    patch_pipeline(repo, stale_job['id'], 'interviewing')
    backdate_changed_at(repo, stale_job['id'], 30)
    result = build(repo)
    followup = next(item for item in result['prescriptions'] if item['id'] == 'followup')
    assert followup['level'] == 'warn'
    assert '1 条跟进逾期' in followup['title'] and '1 条超 14 天无变化' in followup['title']
    assert followup['target'] == 'favorites'
    assert result['today_plan']['follow_ups'] == 1


def test_today_plan_counts_today_events(repo):
    job = repo.save_job(make_job(), 'boss')
    patch_pipeline(repo, job['id'], 'applied')
    # 昨天的投递事件不计入今日（直插历史时间戳）。
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    insert_event(repo, job['id'], 'applied', yesterday)
    result = build(repo)
    assert result['today_plan']['done']['apply'] == 1
    assert result['today_plan']['targets']['apply'] == 3  # 近一周仅 1 条 → 仍按小目标


def test_gap_readiness_once_entries_fill_it(repo):
    job = repo.save_job(make_job(), 'boss')
    patch_pipeline(repo, job['id'], 'interested')
    assert 'gap_readiness' in ids_of(build(repo)['prescriptions'])
    repo.save_profile_entry(dict(category='gap', title='备考', content='考取 PMP 证书'))
    assert 'gap_readiness' not in ids_of(build(repo)['prescriptions'])


def test_no_activity_week_reminder(repo):
    # 上周有投递、本周全 0：停摆提醒与小样本提示并存。空库（无历史）不提醒——那是入门引导的事。
    assert 'no_activity' not in ids_of(build(repo)['prescriptions'])
    job = repo.save_job(make_job(), 'boss')
    two_weeks_ago = (datetime.now(timezone.utc) - timedelta(weeks=2)).isoformat()
    insert_event(repo, job['id'], 'applied', two_weeks_ago)
    assert {'no_activity', 'small_sample'} <= ids_of(build(repo)['prescriptions'])


def test_prescriptions_via_api_and_dashboard(client, tmp_path):
    data = client.get('/api/jobs/pipeline/prescriptions').json()
    assert set(data) == {'prescriptions', 'today_plan', 'generated_at'}
    assert all(set(item) == {'id', 'level', 'title', 'evidence', 'action', 'target', 'cta'}
               for item in data['prescriptions'])
    dashboard = client.get('/api/dashboard').json()
    assert dashboard['prescriptions']['today_plan']['date']
