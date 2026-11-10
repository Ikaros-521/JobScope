import json
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.repository import Repository


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(title='Python工程师', company='甲公司', salary='20-30K·13薪', location='北京', source='user_import', **extra):
    return dict(title=title, company=company, description='熟悉 Python 与 SQL', salary=salary, location=location, **extra)


def test_save_job_stores_monthly_salary_numbers(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(salary='20-30K·13薪'), 'boss')
    assert job['salary_monthly_min'] == 20000
    assert job['salary_monthly_max'] == 30000
    assert job['salary_parsed'] is True
    # 面议、年薪、外币解析不出每月数值，但也要打上已解析标记，避免每次启动重复回填。
    vague = repo.save_job(make_job(title='数据专员', external_id='vague', salary='面议，年薪制'), 'boss')
    assert vague['salary_monthly_min'] is None
    assert vague['salary_monthly_max'] is None
    assert vague['salary_parsed'] is True


def test_backfill_computes_salary_numbers_for_legacy_rows(tmp_path):
    legacy = make_job(salary='15-25K')
    payload = dict(id='J-legacy', title=legacy['title'], company=legacy['company'], description=legacy['description'],
                   location=legacy['location'], salary=legacy['salary'], url='', tags=[], source='boss', updated_at='2026-01-01T00:00:00+00:00', evidence_ids=[])
    with sqlite3.connect(tmp_path / 'jobscope.sqlite3') as conn:
        conn.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)')
        conn.execute('INSERT INTO jobs VALUES (?,?,?)', ('J-legacy', json.dumps(payload, ensure_ascii=False), payload['updated_at']))
    repo = Repository(tmp_path)
    item = repo.job('J-legacy')
    assert item['salary_monthly_min'] == 15000
    assert item['salary_monthly_max'] == 25000
    assert item['salary_parsed'] is True


def test_jobs_filters_by_source_and_salary_range(client):
    repo = client.app.state.repo
    boss = repo.save_job(make_job(salary='15-25K', location='北京'), 'boss')
    zhilian = repo.save_job(make_job(title='Python后端', salary='8-12K', location='上海'), 'zhaopin')
    repo.save_job(make_job(title='海外岗位', salary='USD 3000 per month', location='Remote'), 'remotive')

    assert {j['id'] for j in client.get('/api/jobs?source=boss').json()['jobs']} == {boss['id']}
    # 区间按重叠判定：10-18K 与 15-25K、8-12K 都相交，与无法解析的外币岗不相交。
    overlapped = {j['id'] for j in client.get('/api/jobs?salary_min=10000&salary_max=18000').json()['jobs']}
    assert overlapped == {boss['id'], zhilian['id']}
    assert {j['id'] for j in client.get('/api/jobs?salary_min=20000').json()['jobs']} == {boss['id']}
    assert client.get('/api/jobs?salary_max=5000').json()['total'] == 0
    # 筛选可以与关键词、地点叠加。
    both = client.get('/api/jobs?q=Python&location=上海&source=zhaopin').json()
    assert both['total'] == 1 and both['jobs'][0]['id'] == zhilian['id']


def test_job_ids_matches_list_filters(client):
    repo = client.app.state.repo
    boss = repo.save_job(make_job(salary='15-25K'), 'boss')
    repo.save_job(make_job(title='测试工程师', salary='8-12K'), 'zhaopin')
    data = client.get('/api/jobs/ids?source=boss').json()
    assert data == {'ids': [boss['id']], 'total': 1}
    ranged = client.get('/api/jobs/ids?salary_min=13000').json()
    assert ranged['total'] == 1 and ranged['ids'] == [boss['id']]


def test_batch_update_patches_fields_and_keeps_identity(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(salary='面议', location='北京-朝阳区'), 'boss')
    evidence_before = len(repo.job(job['id'])['evidence_ids'])
    response = client.patch('/api/jobs', json={'ids': [job['id']], 'location': '北京', 'salary': '15-25K', 'tags': ['薪资已核实', '优先']})
    assert response.status_code == 200 and response.json() == {'updated': 1}
    item = repo.job(job['id'])
    assert item['id'] == job['id']
    assert item['location'] == '北京'
    assert item['salary'] == '15-25K'
    assert item['tags'] == ['薪资已核实', '优先']
    assert item['salary_monthly_min'] == 15000
    # 批量编辑不是重新采集：不应追加证据快照。
    assert len(item['evidence_ids']) == evidence_before


def test_batch_update_leaves_blank_fields_alone_and_supports_clearing(client):
    repo = client.app.state.repo
    job = repo.save_job(make_job(location='苏州', salary='10-15K'), 'boss')
    # 只改薪资：地点保持原样。
    client.patch('/api/jobs', json={'ids': [job['id']], 'salary': '18-22K'})
    item = repo.job(job['id'])
    assert item['location'] == '苏州'
    assert item['salary_monthly_max'] == 22000
    # 显式传空串是清空；tags 传空数组是清空标签。
    client.patch('/api/jobs', json={'ids': [job['id']], 'location': '', 'tags': []})
    item = repo.job(job['id'])
    assert item['location'] == ''
    assert item['tags'] == []


def test_batch_update_validates_payload(client):
    assert client.patch('/api/jobs', json={'ids': [], 'location': '北京'}).status_code == 422
    assert client.patch('/api/jobs', json={'ids': [f'J-{i}' for i in range(1001)], 'location': '北京'}).status_code == 422
    # extra=forbid：JD、链接、公司名等不在批量编辑范围内。
    assert client.patch('/api/jobs', json={'ids': ['J-x'], 'description': '改描述'}).status_code == 422
    assert client.patch('/api/jobs', json={'ids': ['J-x']}).json() == {'updated': 0}


def test_batch_delete_cascades_but_keeps_shared_evidence(client):
    repo = client.app.state.repo
    first = repo.save_job(make_job(title='Python工程师'), 'boss')
    second = repo.save_job(make_job(title='数据工程师', company='乙公司'), 'zhaopin')
    shared = repo.save_evidence(dict(title='共同公告', company='甲公司', kind='announcement', text='两家都引用的公告'), 'cninfo')
    repo.link_evidence(first['id'], shared['id'])
    repo.link_evidence(second['id'], shared['id'])
    repo.save_report(dict(job_id=first['id'], kind='analysis', findings=[]))
    kept_conversation = repo.create_conversation(dict(kind='job', job_id=second['id'], company='乙公司'))
    doomed_conversation = repo.create_conversation(dict(kind='job', job_id=first['id'], company='甲公司'))
    first_evidence = set(repo.job(first['id'])['evidence_ids'])
    assert first_evidence  # 至少含自己的 job_posting 快照

    assert client.post('/api/jobs/batch-delete', json={'ids': [first['id']]}).json() == {'deleted': 1}
    assert client.get(f"/api/jobs/{first['id']}").status_code == 404
    assert client.get(f"/api/jobs/{second['id']}").status_code == 200
    # 专属证据随之删除，共享证据保留。
    exclusive_first = first_evidence - {shared['id']}
    assert exclusive_first and all(eid not in [row['id'] for row in _rows(repo, 'evidence')] for eid in exclusive_first)
    assert shared['id'] in [row['id'] for row in _rows(repo, 'evidence')]
    assert _rows(repo, 'reports') == []
    assert [c['id'] for c in repo.list_conversations(dict(kind='job', job_id=second['id'], company='乙公司'))] == [kept_conversation]
    assert repo.list_conversations(dict(kind='job', job_id=first['id'], company='甲公司')) == []

    # 删第二家时共享证据已无人引用，一并清掉。
    assert client.post('/api/jobs/batch-delete', json={'ids': [second['id'], 'J-missing']}).json() == {'deleted': 1}
    assert shared['id'] not in [row['id'] for row in _rows(repo, 'evidence')]
    assert client.get('/api/jobs').json()['total'] == 0


def test_batch_delete_empty_and_unknown_ids(client):
    assert client.post('/api/jobs/batch-delete', json={'ids': []}).status_code == 422
    assert client.post('/api/jobs/batch-delete', json={'ids': ['J-none']}).json() == {'deleted': 0}


def _rows(repo, table):
    with sqlite3.connect(repo.path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f'SELECT id FROM {table}').fetchall()
