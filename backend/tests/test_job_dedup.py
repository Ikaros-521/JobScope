"""岗位去重（2026-11-19）：候选扫描、标记上提、列表/统计排除、拆开与删除晋升。

全部 tmp_path 隔离。
"""
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


def save(client, title, company='甲公司', **extra):
    body = dict(title=title, company=company, description='职责描述', salary='20-30K', location='北京', **extra)
    return client.post('/api/import/job', json=body).json()['job']


def test_scan_finds_same_company_title_candidates(client):
    a = save(client, 'Python后端工程师')
    b = save(client, 'Python 后端工程师（急聘）')  # 括号装饰 + 空格：归一后相等
    c = save(client, 'Go开发工程师', company='乙公司')  # 不同公司：不进候选
    data = client.get('/api/jobs/duplicates/scan').json()
    pair = next((pair for pair in data['candidates'] if {pair['a']['id'], pair['b']['id']} == {a['id'], b['id']}), None)
    assert pair is not None
    assert pair['match'] == 'exact'
    # 跨公司不成对
    assert not any({pair['a']['id'], pair['b']['id']} == {a['id'], c['id']} for pair in data['candidates'])


def test_scan_title_containment_counts_as_candidate(client):
    a = save(client, '数据分析师')
    b = save(client, '数据分析师（杭州）')
    data = client.get('/api/jobs/duplicates/scan').json()
    assert data['candidates'], '包含关系应出候选'


def test_mark_carries_intent_and_excludes_from_lists(client):
    repo = client.app.state.repo
    primary = save(client, 'Python后端工程师')
    dup = save(client, 'Python 后端工程师（急聘）')
    # 重复项带收藏与管线：标记时上提给主记录
    client.patch('/api/jobs', json={'ids': [dup['id']], 'starred': True, 'pipeline_status': 'applied',
                                    'pipeline_note': '周三投', 'pipeline_due': '2026-11-25'})
    result = client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup['id']]})
    assert result.status_code == 200
    assert result.json()['carried'].get('starred') and result.json()['carried'].get('pipeline')

    kept = repo.job(primary['id'])
    assert kept['starred'] is True
    assert kept['pipeline_status'] == 'applied'
    assert kept['pipeline_note'] == '周三投'
    dup_after = repo.job(dup['id'])
    assert dup_after['duplicate_of'] == primary['id']
    assert 'starred' not in dup_after and 'pipeline_status' not in dup_after

    # 列表与统计口径：只剩主记录；收藏/管线计数不双算
    listed = client.get('/api/jobs').json()
    assert listed['total'] == 1 and listed['jobs'][0]['id'] == primary['id']
    ids = client.get('/api/jobs/ids?starred=true').json()
    assert ids['ids'] == [primary['id']]
    review = client.get('/api/jobs/pipeline/review').json()
    assert review['totals'].get('applied') == 1
    # 全量底座（reliability/compare 用）同样排除重复项
    assert [job['id'] for job in repo.jobs()] == [primary['id']]
    # 详情仍可直读（那里能看到「与 X 视为同一岗位」）
    assert client.get(f"/api/jobs/{dup['id']}").json()['job']['duplicate_of'] == primary['id']


def test_mark_preserves_primary_intent_and_skips_already_marked(client):
    repo = client.app.state.repo
    primary = save(client, '测试开发工程师')
    client.patch('/api/jobs', json={'ids': [primary['id']], 'pipeline_status': 'interviewing'})
    dup_a = save(client, '测试开发工程师（双休）')
    dup_b = save(client, '测试开发工程师（15薪）')
    client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup_a['id']]})
    # dup_b 再次标记不影响已有成员；主记录管线不被覆盖
    client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup_b['id']]})
    kept = repo.job(primary['id'])
    assert kept['pipeline_status'] == 'interviewing'
    assert repo.job(dup_a['id'])['duplicate_of'] == primary['id']
    assert repo.job(dup_b['id'])['duplicate_of'] == primary['id']


def test_release_restores_listing(client):
    repo = client.app.state.repo
    primary = save(client, '产品经理')
    dup = save(client, '产品经理（AI方向）')
    client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup['id']]})
    result = client.post('/api/jobs/duplicates/release', json={'ids': [dup['id']]})
    assert result.json()['released'] == 1
    assert client.get('/api/jobs').json()['total'] == 2
    assert 'duplicate_of' not in repo.job(dup['id'])
    groups = client.get('/api/jobs/duplicates/scan').json()['groups']
    assert groups == []


def test_deleting_primary_promotes_duplicates(client):
    repo = client.app.state.repo
    primary = save(client, '运维工程师')
    dup = save(client, '运维工程师（外包勿投）')
    client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup['id']]})
    client.post('/api/jobs/batch-delete', json={'ids': [primary['id']]})
    # 主记录没了：重复项自动晋升回独立岗位，不悬空
    assert 'duplicate_of' not in repo.job(dup['id'])
    assert client.get('/api/jobs').json()['total'] == 1


def test_mark_missing_primary_rejected(client):
    dup = save(client, '前端工程师')
    result = client.post('/api/jobs/duplicates/mark', json={'primary_id': 'J-nonexistent', 'duplicate_ids': [dup['id']]})
    assert result.status_code == 422


def test_recollection_preserves_duplicate_mark(client):
    repo = client.app.state.repo
    primary = save(client, '算法工程师')
    dup = save(client, '算法工程师（NLP）')
    client.post('/api/jobs/duplicates/mark', json={'primary_id': primary['id'], 'duplicate_ids': [dup['id']]})
    repo.save_job(dict(title='算法工程师（NLP）', company='甲公司', description='职责描述', salary='20-30K', location='北京'), 'boss')
    assert repo.job(dup['id'])['duplicate_of'] == primary['id']
