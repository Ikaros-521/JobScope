import sys
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


def make_job(title='Python工程师', company='甲公司', salary='20-30K', location='北京', source='boss', description='熟悉 Python 与 SQL', **extra):
    return dict(title=title, company=company, description=description, salary=salary, location=location, **extra)


def test_new_job_has_no_starred_and_recollection_preserves_it(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='ext-1'), 'boss')
    assert 'starred' not in job

    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': True})
    starred = repo.job(job['id'])
    assert starred['starred'] is True and starred['starred_at']

    # 重采集整包覆盖 payload：收藏标记是用户意图，必须幸存（含时间戳）。
    recollected = repo.save_job(make_job(external_id='ext-1', salary='25-35K'), 'boss')
    assert recollected['id'] == job['id']
    assert recollected['starred'] is True
    assert recollected['starred_at'] == starred['starred_at']
    assert recollected['salary_monthly_min'] == 25000  # 采集数据照常更新

    # 未收藏过的岗位重采集不会凭空长出字段。
    plain = repo.save_job(make_job(title='另一个岗位', external_id='ext-2'), 'boss')
    repo.save_job(make_job(title='另一个岗位', external_id='ext-2'), 'boss')
    assert 'starred' not in repo.job(plain['id'])


def test_patch_starred_toggle_and_clear_timestamp(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': True})
    on = repo.job(job['id'])
    assert on['starred'] is True and on['starred_at']

    # 显式 false 是取消收藏：标记落 false，时间戳清掉；缺省不带 starred 时不碰收藏。
    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': False, 'location': '上海'})
    off = repo.job(job['id'])
    assert off['starred'] is False and 'starred_at' not in off
    assert off['location'] == '上海'

    # 取消后再收藏，时间戳重新盖上。
    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': True})
    assert repo.job(job['id'])['starred_at']


def test_jobs_and_ids_filter_starred_only(client):
    repo = client.app.state.repo
    kept = repo.save_job(make_job(salary='15-25K'), 'boss')
    other = repo.save_job(make_job(title='测试工程师', salary='8-12K', location='上海'), 'zhaopin')
    client.patch('/api/jobs', json={'ids': [kept['id']], 'starred': True})

    only = client.get('/api/jobs?starred=true').json()
    assert only['total'] == 1 and only['jobs'][0]['id'] == kept['id']
    # 不带 starred 参数时不过滤：收藏与未收藏都在。
    assert client.get('/api/jobs').json()['total'] == 2
    # 与来源筛选叠加。
    both = client.get('/api/jobs?starred=true&source=zhaopin').json()
    assert both['total'] == 0
    ids = client.get('/api/jobs/ids?starred=true').json()
    assert ids == {'ids': [kept['id']], 'total': 1}

    # 取消收藏后过滤自然落空。
    client.patch('/api/jobs', json={'ids': [kept['id']], 'starred': False})
    assert client.get('/api/jobs?starred=true').json()['total'] == 0
    assert other['id']  # 占位避免未用变量告警


def test_run_batch_ids_pool_reads_complete_jobs_too(client):
    repo = client.app.state.repo
    first = repo.save_job(make_job(external_id='a', description='旧的描述'), 'boss')
    second = repo.save_job(make_job(title='另一个岗位', external_id='b'), 'boss')
    # 两份都已「完整」：补采池（pending_jobs）为空，反查必须仍能按 ids 重读。
    for job in (first, second):
        repo.save_job(make_job(title=job['title'], external_id=job.get('external_id'), description='完整JD',
                               description_status='complete', detail_collected_at='2026-01-01T00:00:00+00:00'), 'boss')
    assert repo_pending(repo) == []

    calls = []

    def collector(job, data_dir=None):
        calls.append(job['id'])
        return dict(status='completed', message='ok',
                    job={**job, 'description': job['description'] + '（反查更新）', 'detail_collected_at': '2026-01-02T00:00:00+00:00'})

    task = repo.create_task('boss', '收藏岗位反查')
    from jobscope.job_details import run_batch
    run_batch(repo, task, ids=[first['id'], second['id']], collector=collector, pause=lambda seconds: None)
    # 同秒内 updated_at 并列时池子顺序不保证按 ids 传入序：只断言集合与数量。
    assert sorted(calls) == sorted([first['id'], second['id']])
    assert task['total'] == 2 and task['count'] == 2 and task['status'] == 'completed'
    assert repo.job(first['id'])['description'].endswith('（反查更新）')
    assert repo.job(first['id'])['detail_collected_at'] == '2026-01-02T00:00:00+00:00'


def test_run_batch_ids_reports_missing_and_caps_limit(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(), 'boss')

    def collector(job, data_dir=None):
        return dict(status='completed', message='ok', job=job)

    task = repo.create_task('boss', '收藏岗位反查')
    from jobscope.job_details import run_batch
    # ids 里混入不存在的岗位：池子按实际存在裁剪；limit 生效。
    run_batch(repo, task, limit=1, ids=[job['id'], 'J-missing'], collector=collector, pause=lambda seconds: None)
    assert task['total'] == 1 and task['count'] == 1


def test_detail_batch_endpoint_validates_ids(client):
    # 空列表拒绝：反查至少要指明一个岗位；缺省 ids 才是补采语义。
    assert client.post('/api/browser/details', json={'ids': []}).status_code == 422
    assert client.post('/api/browser/details', json={'ids': ['x' * 101]}).status_code == 422
    assert client.post('/api/browser/details', json={'ids': [f'J-{i}' for i in range(201)]}).status_code == 422


def repo_pending(repo):
    from jobscope.job_details import pending_jobs
    return pending_jobs(repo)


def test_pipeline_status_auto_stars_and_filters(client):
    """机会管线：给未收藏岗位设状态自动收藏；'' 显式移出管线但保留收藏；筛选参数可用。"""
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='pipe-1'), 'boss')

    # 设状态即入篮：自动收藏，状态与变化时间落库。
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'applied'})
    item = repo.job(job['id'])
    assert item['pipeline_status'] == 'applied' and item['pipeline_status_changed_at']
    assert item['starred'] is True and item['starred_at']

    # 收藏筛选 + 管线筛选（单状态与 tracked）。
    assert client.get('/api/jobs', params={'starred': 'true'}).json()['total'] == 1
    assert client.get('/api/jobs', params={'pipeline_status': 'applied'}).json()['total'] == 1
    assert client.get('/api/jobs', params={'pipeline_status': 'tracked'}).json()['total'] == 1
    assert client.get('/api/jobs', params={'pipeline_status': 'offer'}).json()['total'] == 0

    # 状态迁移盖新时间戳；'' 移出管线但收藏保留。
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'interviewing'})
    moved = repo.job(job['id'])
    assert moved['pipeline_status'] == 'interviewing'
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': ''})
    cleared = repo.job(job['id'])
    assert 'pipeline_status' not in cleared and 'pipeline_status_changed_at' not in cleared
    assert cleared['starred'] is True

    # 非法状态名 422。
    assert client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'hired'}).status_code == 422


def test_unstarring_clears_pipeline_state(client):
    """取消收藏即移出管线：状态与变化时间一并清掉，不留下不可见的幽灵跟踪。"""
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='pipe-2'), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': True, 'pipeline_status': 'interested'})
    assert repo.job(job['id'])['pipeline_status'] == 'interested'

    client.patch('/api/jobs', json={'ids': [job['id']], 'starred': False})
    item = repo.job(job['id'])
    assert item['starred'] is False
    assert 'pipeline_status' not in item and 'pipeline_status_changed_at' not in item


def test_pipeline_recollection_preserves_state(client):
    """重采集整包覆盖 payload：管线状态是用户意图，与收藏一样必须幸存。"""
    repo = client.app.state.repo
    job = repo.save_job(make_job(external_id='pipe-3'), 'boss')
    client.patch('/api/jobs', json={'ids': [job['id']], 'pipeline_status': 'applied'})
    recollected = repo.save_job(make_job(external_id='pipe-3', salary='25-35K'), 'boss')
    assert recollected['pipeline_status'] == 'applied'
    assert recollected['pipeline_status_changed_at'] == repo.job(job['id'])['pipeline_status_changed_at']


def test_pipeline_review_counts_and_follow_ups(client, monkeypatch):
    """回顾端点：状态计数、本周新增/变化、活跃状态超 14 天进待跟进。时间偏移用可变的 now 间接控制——
    这里直接改 payload 里的时间戳更直白：早于阈值的 changed_at 由 datetime 解析得到。"""
    from datetime import datetime, timedelta, timezone
    repo = client.app.state.repo
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    stale = repo.save_job(make_job(title='旧投递', external_id='rev-1'), 'boss')
    repo.update_jobs([stale['id']], {'pipeline_status': 'applied'})
    with repo.connection() as conn:
        row = conn.execute('SELECT payload FROM jobs WHERE id=?', (stale['id'],)).fetchone()
        # 状态变化与收藏都按 30 天前算：这岗位是「早就进了篮子、一直没动静」的典型待跟进样本。
        item = {**__import__('json').loads(row['payload']), 'pipeline_status_changed_at': old, 'starred_at': old}
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', (__import__('json').dumps(item, ensure_ascii=False), stale['id']))

    recent = repo.save_job(make_job(title='新收藏', external_id='rev-2'), 'boss')
    repo.update_jobs([recent['id']], {'pipeline_status': 'interested'})

    archived = repo.save_job(make_job(title='已归档', external_id='rev-3'), 'boss')
    repo.update_jobs([archived['id']], {'pipeline_status': 'archived'})
    with repo.connection() as conn:
        row = conn.execute('SELECT payload FROM jobs WHERE id=?', (archived['id'],)).fetchone()
        item = {**__import__('json').loads(row['payload']), 'pipeline_status_changed_at': old}
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', (__import__('json').dumps(item, ensure_ascii=False), archived['id']))

    data = client.get('/api/jobs/pipeline/review').json()
    assert data['stale_days'] == 14
    assert data['totals'] == {'applied': 1, 'interested': 1, 'archived': 1}
    ids = lambda group: [item['id'] for item in data[group]]
    assert stale['id'] in ids('follow_ups')
    assert archived['id'] not in ids('follow_ups')  # 归档不算待跟进
    assert recent['id'] in ids('recent_changes') and recent['id'] in ids('new_favorites')
    assert stale['id'] not in ids('new_favorites')
