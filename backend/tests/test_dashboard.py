"""今日概览与管线跟进信息（2026-11-18）：/api/dashboard 聚合口径 + note/due 的存取与清理语义。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(title='Python工程师', company='甲公司', **extra):
    return dict(title=title, company=company, description='熟悉 Python 与 SQL', salary='20-30K', location='北京', **extra)


def test_pipeline_note_and_due_round_trip(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='ext-1'), 'boss')
    job_id = job['id']

    # 设状态同时给跟进信息：一并落库。
    assert client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_status': 'applied',
                                           'pipeline_note': '周三前发简历', 'pipeline_due': '2026-11-20'}).json()['updated'] == 1
    item = repo.job(job_id)
    assert item['pipeline_status'] == 'applied'
    assert item['pipeline_note'] == '周三前发简历'
    assert item['pipeline_due'] == '2026-11-20'

    # 缺省字段不动：只改 note，due 与状态保持。
    client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_note': '改约下周一'})
    item = repo.job(job_id)
    assert item['pipeline_note'] == '改约下周一'
    assert item['pipeline_due'] == '2026-11-20'
    assert item['pipeline_status'] == 'applied'

    # '' 显式清空。
    client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_note': '', 'pipeline_due': ''})
    item = repo.job(job_id)
    assert 'pipeline_note' not in item and 'pipeline_due' not in item
    assert item['pipeline_status'] == 'applied'  # 清跟进信息不动状态

    # 移出管线 / 取消收藏：跟进信息连带清掉。
    client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_status': 'interviewing', 'pipeline_note': '二面', 'pipeline_due': '2026-11-25'})
    client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_status': ''})
    item = repo.job(job_id)
    assert all(key not in item for key in ('pipeline_status', 'pipeline_note', 'pipeline_due'))


def test_pipeline_due_date_format_validated(client):
    repo = client.app.state.repo
    job_id = repo.save_job(make_job(external_id='ext-2'), 'boss')['id']
    result = client.patch('/api/jobs', json={'ids': [job_id], 'pipeline_status': 'applied', 'pipeline_due': '11/20'})
    assert result.status_code == 422


def test_pipeline_follow_info_survives_recollection(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='ext-3'), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'interviewing',
                                    'pipeline_note': '等 HR 回复', 'pipeline_due': '2026-11-30'})
    # 重新采集整包覆盖：用户意图字段幸存。
    repo.save_job(make_job(external_id='ext-3', title='Python工程师（更新）'), 'boss')
    item = repo.job(job['id'])
    assert item['pipeline_note'] == '等 HR 回复'
    assert item['pipeline_due'] == '2026-11-30'


def test_dashboard_aggregates_all_sections(client):
    repo = client.app.state.repo
    job_a = repo.save_job(make_job(external_id='da-1', title='新岗位A'), 'boss')
    repo.save_job(make_job(external_id='da-2', title='老岗位B', company='乙公司'), 'boss')
    # 老岗位B 的首见时间拨回 30 天前：不应出现在「近 7 天新入库」。
    with repo.connection() as conn:
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        row = conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.external_id')='da-2'").fetchone()
        job_b = json.loads(row['payload'])
        evidence_id = repo.job(job_b['id'])['evidence_ids'][0]
        conn.execute("UPDATE evidence SET payload=json_set(payload,'$.collected_at',?) WHERE id=?", (old, evidence_id))

    today = datetime.now(timezone.utc).date().isoformat()
    client.patch('/api/jobs', json={'ids': [job_a['id']], 'pipeline_status': 'applied',
                                    'pipeline_note': '今天投', 'pipeline_due': today})
    client.post('/api/backup')

    data = client.get('/api/dashboard').json()
    assert data['counts']['jobs'] == 2
    assert data['counts']['tracked'] == 1
    assert data['counts']['favorites'] == 1
    # 截止日=今天的待办在列，逾期标记为否。
    assert [item['id'] for item in data['due']] == [job_a['id']]
    assert data['due'][0]['note'] == '今天投' and data['due'][0]['overdue'] is False
    assert data['due'][0]['status'] == 'applied'
    # 近 7 天新入库只含岗位A。
    assert [item['title'] for item in data['new_jobs']] == ['新岗位A']
    # 备份区块给出最近备份时间。
    assert data['backup']['last_backup_at']
    assert data['model_configured'] is False


def test_dashboard_due_marks_overdue_and_skips_terminal(client):
    repo = client.app.state.repo
    overdue = repo.save_job(make_job(external_id='ov-1', title='逾期岗'), 'boss')['id']
    terminal = repo.save_job(make_job(external_id='ov-2', title='offer岗'), 'boss')['id']
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    client.patch('/api/jobs', json={'ids': [overdue], 'pipeline_status': 'applied', 'pipeline_due': yesterday})
    client.patch('/api/jobs', json={'ids': [terminal], 'pipeline_status': 'offer', 'pipeline_due': yesterday})
    data = client.get('/api/dashboard').json()
    assert [item['id'] for item in data['due']] == [overdue]
    assert data['due'][0]['overdue'] is True


def test_dashboard_briefing_counts_last_24h_only(client):
    repo = client.app.state.repo
    fresh = repo.save_job(make_job(external_id='br-1', title='简报新岗'), 'boss')
    stale = repo.save_job(make_job(external_id='br-2', title='简报老岗', company='乙公司'), 'boss')
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with repo.connection() as conn:
        # 老岗首见拨回 3 天前：不计入新入库。
        row = conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.external_id')='br-2'").fetchone()
        old_job = json.loads(row['payload'])
        conn.execute("UPDATE evidence SET payload=json_set(payload,'$.collected_at',?) WHERE id=?",
                     (three_days_ago, repo.job(old_job['id'])['evidence_ids'][0]))
        # 3 天前的旧事件：不计入推进。
        conn.execute("INSERT INTO job_pipeline_events (job_id,from_status,to_status,created_at) VALUES (?,?,?,?)",
                     (stale['id'], '', 'interested', three_days_ago))
        # 近 24h 的采集异常：计入 collect_attention。
        conn.execute("INSERT INTO tasks (id,payload) VALUES (?,?)",
                     ('br-task', json.dumps({'id': 'br-task', 'status': 'needs_login', 'source': 'boss',
                                             'query': 'Python', 'message': '', 'count': 0,
                                             'created_at': datetime.now(timezone.utc).isoformat()})))
    client.patch('/api/jobs', json={'ids': [fresh['id']], 'pipeline_status': 'applied'})

    data = client.get('/api/dashboard').json()['briefing']
    assert data['new_jobs'] == 1
    assert data['new_favorites'] == 1  # 推进状态自动收藏，starred_at 在窗口内
    assert data['moves'] == {'applied': 1}
    assert data['collect_attention'] == 1
    assert data['days_since_last_move'] == 0  # 刚推进过：不进最低功耗模式


def test_dashboard_briefing_silent_days_after_absence(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='lp-1', title='静默岗'), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'applied'})
    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with repo.connection() as conn:
        conn.execute("UPDATE job_pipeline_events SET created_at=?", (five_days_ago,))
        conn.execute("UPDATE jobs SET payload=json_set(payload,'$.starred_at',?) WHERE id=?", (five_days_ago, job['id']))
    data = client.get('/api/dashboard').json()['briefing']
    assert data['days_since_last_move'] == 5
    assert data['moves'] == {}  # 静默窗口外，增量全零
    assert data['last_move_at']
