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
