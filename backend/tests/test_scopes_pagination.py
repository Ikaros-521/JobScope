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


def seed(client, n=26):
    repo = client.app.state.repo
    for i in range(n):
        repo.save_job(dict(title=f'Python {i:02d}', company='甲公司' if i % 2 == 0 else '甲公司子公司',
                           description=f'Python岗位职责{i}', external_id=str(i), location='杭州'), 'user_import')
        repo.create_task('boss', str(i))
    return repo


def test_paginated_lists_and_filters(client):
    seed(client)
    for endpoint, key in [('jobs', 'jobs'), ('evidence', 'evidence'), ('tasks', 'tasks')]:
        first = client.get(f'/api/{endpoint}?page=1&page_size=10').json()
        second = client.get(f'/api/{endpoint}?page=2&page_size=10').json()
        assert first['total'] == 26
        assert first['pages'] == 3
        assert len(first[key]) == len(second[key]) == 10
        assert not {x['id'] for x in first[key]} & {x['id'] for x in second[key]}
        assert client.get(f'/api/{endpoint}?page=99&page_size=10').json()['page'] == 3
        assert client.get(f'/api/{endpoint}?page_size=501').status_code == 422
    filtered = client.get('/api/evidence?company=甲公司&page_size=10').json()
    assert filtered['total'] == 13
    assert all(e['company'] == '甲公司' for e in filtered['evidence'])
    assert client.get('/api/jobs?q=NOTFOUND').json()['total'] == 0
    companies = client.get('/api/companies?q=甲公司&page_size=10').json()
    assert companies['total'] == 2


def test_company_and_global_chat_and_bound_history(client, monkeypatch):
    repo = seed(client, 2)
    a = repo.save_evidence(dict(title='甲经营', company='甲公司', text='甲公司营业收入100，薪资10K'), 'user_import')
    b = repo.save_evidence(dict(title='乙经营', company='乙公司', text='乙公司营业收入200，薪资20K'), 'user_import')
    company = client.post('/api/chat', json={'company': '甲公司', 'message': '营业收入和薪资'}).json()
    assert company['scope']['kind'] == 'company'
    assert {c['company'] for c in company['citations']} == {'甲公司'}
    session = company['conversation_id']
    assert client.post('/api/chat', json={'company': '乙公司', 'message': '情况', 'conversation_id': session}).status_code == 409
    assert client.post('/api/chat', json={'scope': 'global', 'message': '情况', 'conversation_id': session}).status_code == 409
    global_chat = client.post('/api/chat', json={'scope': 'global', 'message': '营业收入和薪资'}).json()
    assert {a['id'], b['id']} <= {c['id'] for c in global_chat['citations']}
    assert global_chat['scope']['kind'] == 'global'
    assert client.post('/api/chat', json={'scope': 'global', 'company': '甲公司', 'message': '情况'}).status_code == 400

    captured = []
    def answer(message, evidence, resume, history):
        captured.append(history)
        assert all(item['company'] == '甲公司' for item in evidence)
        return {'answer': '甲公司资料', 'citations': [], 'mode': 'evidence_search'}
    monkeypatch.setattr('jobscope.main.answer_question', answer)
    client.post('/api/chat', json={'company': '甲公司', 'message': '继续', 'conversation_id': session,
                                'history': [{'role': 'assistant', 'content': '乙公司的隐私历史'}]})
    assert captured[0]
    assert '乙公司的隐私历史' not in str(captured)
    assert '营业收入和薪资' in str(captured)


def test_job_evidence_paging_deduplicates_and_excludes_other_company(client):
    repo = seed(client, 1)
    job = repo.jobs()[0]
    for i in range(12):
        repo.save_evidence(dict(title=f'证据{i}', company='甲公司', text=str(i)), 'user_import')
    foreign = repo.save_evidence(dict(title='外部证据', company='乙公司', text='不能出现'), 'user_import')
    repo.link_evidence(job['id'], foreign['id'])
    result = client.get(f"/api/jobs/{job['id']}/evidence?page=1&page_size=10").json()
    assert result['total'] == 13
    assert all(e['company'] == '甲公司' for e in result['evidence'])
    assert len(result['evidence']) == 10


def test_job_overview_bounds_evidence_and_reports_full_count(client):
    repo = seed(client, 1)
    job = repo.jobs()[0]
    for i in range(25):
        repo.save_evidence(dict(title=f'资料{i}', company=job['company'], text='公司来源原文'), 'user_import')
    result = client.get(f"/api/jobs/{job['id']}").json()
    assert result['evidence_total'] == 26
    assert len(result['evidence']) <= 10
    assert len(result['company_evidence']) == 4
    assert client.get(f"/api/jobs/{job['id']}/evidence?page=3").json()['total'] == 26


def test_global_search_ranks_explicit_company_even_when_job_title_omits_it(client):
    repo = client.app.state.repo
    repo.save_evidence(dict(title='开发工程师', company='杭州目标技术', text='Python 数据平台职责'), 'user_import')
    for i in range(10):
        repo.save_evidence(dict(title=f'工程师{i}', company='其他技术公司', text='Python 数据平台职责'), 'user_import')
    result = client.post('/api/chat', json={'scope': 'global', 'message': '杭州目标技术的岗位职责'}).json()
    assert result['citations'][0]['company'] == '杭州目标技术'
