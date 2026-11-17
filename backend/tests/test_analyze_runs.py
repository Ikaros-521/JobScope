import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 配置分析模型 env，让 start 走流式 run（默认回退是规则分析，见 rule_client）。
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    with TestClient(create_app(tmp_path)) as client:
        yield client


@pytest.fixture
def rule_client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def save_job(repo, **extra):
    return repo.save_job(dict(title='Python 后端', company='甲公司', description='负责数据平台', location='杭州', **extra), 'user_import')


def save_resume(client):
    assert client.put('/api/resume', json=dict(text='使用 Python 开发服务', format='text')).status_code == 200


def read_stream(client, run_id):
    frames = []
    with client.stream('GET', f'/api/analyze/runs/{run_id}/stream') as response:
        assert response.status_code == 200
        event = None
        for line in response.iter_lines():
            if line.startswith('event: '):
                event = line[7:].strip()
            elif line.startswith('data: '):
                frames.append((event, json.loads(line[6:])))
    return frames


def await_done(client, run_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f'/api/analyze/runs/{run_id}').json().get('done'):
            return
        time.sleep(0.05)
    raise AssertionError('评估运行超时未完成')


def join_worker(client, run_id, timeout=5):
    handle = client.app.state.analyze_runs.get(run_id)
    if handle and handle.thread is not None:
        handle.thread.join(timeout=timeout)


def patch_pipeline(monkeypatch, runner):
    """把 analyze_runs 引用的管线替换为可控 fake；runner 签名 (job, resume, evidence, on_event, should_stop)。"""
    def fake(job, resume, company_evidence, on_event=None, should_stop=None, extra_facts=None, overrides=None):
        return runner(job, resume, company_evidence, on_event or (lambda kind, payload: None), should_stop or (lambda: False))
    monkeypatch.setattr('jobscope.analyze_runs.analyze_pipeline', fake)


def test_start_requires_complete_jd_and_resume(client, monkeypatch):
    repo = client.app.state.repo
    listing = repo.save_job(dict(title='BOSS 摘要岗', company='乙公司', description='半截职责',
                                 source='boss', description_status='listing'), 'boss')
    assert client.post('/api/analyze/runs', json=dict(job_id=listing['id'])).status_code == 409
    job = save_job(repo)
    assert client.post('/api/analyze/runs', json=dict(job_id=job['id'])).status_code == 400
    save_resume(client)
    patch_pipeline(monkeypatch, lambda job, resume, evidence, on_event, should_stop: dict(job_id=job['id']))
    response = client.post('/api/analyze/runs', json=dict(job_id=job['id']))
    assert response.status_code == 200
    assert response.json()['reused'] is False
    run_id = response.json()['run_id']
    await_done(client, run_id)
    join_worker(client, run_id)


def test_stream_frames_carry_report_and_persist(client, monkeypatch):
    repo = client.app.state.repo
    job = save_job(repo)
    save_resume(client)
    items = [dict(requirement='熟悉Python', jd_quote='负责数据平台', status='met', evidence=['F-1'],
                  dimension='skill', hard=False, rationale='事实明确支持')]

    def runner(job, resume, evidence, on_event, should_stop):
        on_event('stage', dict(stage='extract', status='done', total=1))
        on_event('group', dict(index=0, total=1, items=items))
        return dict(job_id=job['id'], method='model_assisted', score=80, coverage=1,
                    requirements=items, strengths=[], gaps=[], questions=[], limitations=[])

    patch_pipeline(monkeypatch, runner)
    run_id = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()['run_id']
    await_done(client, run_id)
    join_worker(client, run_id)
    frames = read_stream(client, run_id)
    kinds = [kind for kind, _ in frames]
    assert kinds[0] == 'start' and kinds[-1] == 'end'
    assert 'stage' in kinds and 'group' in kinds
    done_payload = dict(frames)['done']
    assert done_payload['job_id'] == job['id'] and done_payload['score'] == 80
    with repo.connection() as conn:  # worker 先落库再推送
        count = conn.execute('SELECT COUNT(*) FROM reports WHERE job_id = ?', (job['id'],)).fetchone()[0]
    assert count == 1


def test_duplicate_start_reuses_active_run(client, monkeypatch):
    repo = client.app.state.repo
    job = save_job(repo)
    save_resume(client)
    entered, release = threading.Event(), threading.Event()

    def runner(job, resume, evidence, on_event, should_stop):
        entered.set()
        release.wait(5)
        return dict(job_id=job['id'])

    patch_pipeline(monkeypatch, runner)
    first = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()
    assert entered.wait(5)
    second = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()
    assert second['reused'] is True and second['run_id'] == first['run_id']
    release.set()
    await_done(client, first['run_id'])
    join_worker(client, first['run_id'])


def test_cancel_pushes_cancelled_frame(client, monkeypatch):
    from jobscope.semantic import RunCancelled
    repo = client.app.state.repo
    job = save_job(repo)
    save_resume(client)

    def runner(job, resume, evidence, on_event, should_stop):
        while not should_stop():
            time.sleep(0.02)
        raise RunCancelled()

    patch_pipeline(monkeypatch, runner)
    run_id = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()['run_id']
    time.sleep(0.1)  # 让 worker 进入等待循环
    assert client.post(f'/api/analyze/runs/{run_id}/cancel').json()['cancelled'] is True
    await_done(client, run_id)
    join_worker(client, run_id)
    kinds = [kind for kind, _ in read_stream(client, run_id)]
    assert 'cancelled' in kinds and 'done' not in kinds


def test_pipeline_error_pushes_error_frame(client, monkeypatch):
    repo = client.app.state.repo
    job = save_job(repo)
    save_resume(client)
    patch_pipeline(monkeypatch, lambda job, resume, evidence, on_event, should_stop: (_ for _ in ()).throw(ValueError('适配模型请求失败：渠道 401')))
    run_id = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()['run_id']
    await_done(client, run_id)
    join_worker(client, run_id)
    frames = dict(read_stream(client, run_id))
    assert frames['error']['message'] == '适配模型请求失败：渠道 401'
    assert 'done' not in frames


def test_unknown_run_is_404(client):
    assert client.get('/api/analyze/runs/nope').status_code == 404
    assert client.get('/api/analyze/runs/nope/stream').status_code == 404
    assert client.post('/api/analyze/runs/nope/cancel').status_code == 404


def test_unbound_analysis_purpose_falls_back_to_rule_report(rule_client):
    # 未绑定分析模型：与同步端点一致回退纯规则分析，start 直接带 report、不开流式 run。
    repo = rule_client.app.state.repo
    job = save_job(repo)
    assert rule_client.put('/api/resume', json=dict(text='使用 Python 开发服务', format='text')).status_code == 200
    response = rule_client.post('/api/analyze/runs', json=dict(job_id=job['id']))
    assert response.status_code == 200
    payload = response.json()
    assert payload['run_id'] is None and payload['reused'] is False
    assert payload['report']['job_id'] == job['id'] and payload['report']['method'] == 'rule_assisted'
    with repo.connection() as conn:
        count = conn.execute('SELECT COUNT(*) FROM reports WHERE job_id = ?', (job['id'],)).fetchone()[0]
    assert count == 1


def test_reports_readback_list_detail_and_404(rule_client):
    # 回读：打开岗位即可看到历史评估（含本轮之前落库的报告），最新在前，详情可按 id 取。
    repo = rule_client.app.state.repo
    job = save_job(repo)
    assert rule_client.put('/api/resume', json=dict(text='使用 Python 开发服务', format='text')).status_code == 200
    rule_client.post('/api/analyze/runs', json=dict(job_id=job['id']))
    rule_client.post('/api/analyze/runs', json=dict(job_id=job['id']))
    listing = rule_client.get(f"/api/jobs/{job['id']}/reports")
    assert listing.status_code == 200
    payload = listing.json()
    assert len(payload['reports']) == 2
    assert payload['reports'][0]['method'] == 'rule_assisted'  # 最新在前
    assert payload['latest']['job_id'] == job['id']
    detail = rule_client.get(f"/api/jobs/{job['id']}/reports/{payload['reports'][1]['id']}")
    assert detail.status_code == 200 and detail.json()['job_id'] == job['id']
    assert rule_client.get(f"/api/jobs/{job['id']}/reports/nope").status_code == 404
    assert rule_client.get('/api/jobs/J-not-exist/reports').status_code == 404


def test_job_preferences_crud(rule_client):
    created = rule_client.post('/api/job-preferences', json=dict(dimension='preference', statement='接受做六休一，周日固定休息'))
    assert created.status_code == 200
    pref = created.json()
    assert pref['enabled'] is True
    assert any(item['id'] == pref['id'] for item in rule_client.get('/api/job-preferences?enabled_only=true').json()['preferences'])
    assert rule_client.put(f"/api/job-preferences/{pref['id']}", json=dict(enabled=False)).json()['enabled'] is False
    assert rule_client.get('/api/job-preferences?enabled_only=true').json()['preferences'] == []
    assert rule_client.delete(f"/api/job-preferences/{pref['id']}").json()['deleted'] is True
    assert rule_client.delete(f"/api/job-preferences/{pref['id']}").status_code == 404


def test_status_override_on_rule_report_records_memory_only(rule_client):
    # 规则报告的行没有 jd_quote，改判只记录记忆（重评走语义管线时生效），返回提示而不改报告。
    repo = rule_client.app.state.repo
    job = save_job(repo)
    assert rule_client.put('/api/resume', json=dict(text='Python', format='text')).status_code == 200
    assert rule_client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()['report']['method'] == 'rule_assisted'
    response = rule_client.put(f"/api/jobs/{job['id']}/analysis/status", json=dict(jd_quote='必须双休', status='met'))
    assert response.status_code == 200
    payload = response.json()
    assert payload['report'] is None and '重新评估时生效' in payload['message']


def test_status_override_updates_semantic_report(client, monkeypatch):
    repo = client.app.state.repo
    job = save_job(repo)
    save_resume(client)
    quote = '杭州线下办公，并能参加线下面试'

    def runner(job, resume, evidence, on_event, should_stop):
        return dict(job_id=job['id'], method='model_assisted', score=None, coverage=0,
                    requirements=[dict(requirement='杭州线下办公', jd_quote=quote, status='unknown', evidence=[],
                                       dimension='preference', hard=True, rationale='简历未提供所在地与可用性')],
                    strengths=[], gaps=[], questions=[], salary=None, company_summary=None,
                    evidence_ids=[], limitations=[], resume_revision=1)

    patch_pipeline(monkeypatch, runner)
    run_id = client.post('/api/analyze/runs', json=dict(job_id=job['id'])).json()['run_id']
    await_done(client, run_id)
    join_worker(client, run_id)
    response = client.put(f"/api/jobs/{job['id']}/analysis/status", json=dict(jd_quote=quote, status='met', note='可线下面试'))
    assert response.status_code == 200
    payload = response.json()
    assert payload['message'] is None
    row = payload['report']['requirements'][0]
    assert row['status'] == 'met' and row['overridden'] is True
    assert row['rationale'].startswith('用户确认：可线下面试')
    assert any('手动确认' in line for line in payload['report']['limitations'])
    # 历史报告同步改判 + 记忆持久：直接再读最新报告应已是改判后状态
    latest = client.get(f"/api/jobs/{job['id']}/reports").json()['latest']
    assert latest['requirements'][0]['status'] == 'met'
