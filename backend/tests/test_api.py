import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_end_to_end_import_resume_analyze_chat_and_scope(client):
    assert client.get("/api/health").json()["model_configured"] is False


def test_index_shell_is_served_with_no_cache(client):
    shell = client.get('/')
    assert shell.status_code == 200
    assert shell.headers.get('Cache-Control') == 'no-cache'
    assert client.get('/api/health').headers.get('Cache-Control') is None
    payload = dict(title="Python工程师", company="甲公司", description="熟悉 Python、SQL、Redis", salary="20-30K·最高16薪", location="北京")
    job = client.post("/api/import/job", json=payload).json()["job"]
    assert job["source"] == "user_import"
    assert len(client.get("/api/jobs?q=Python").json()["jobs"]) == 1
    client.post("/api/evidence", json=dict(title="甲公司资料", company="甲公司", text="2025年营收增长10%", kind="user_material"))
    foreign = client.post("/api/evidence", json=dict(title="其他公司资料", company="甲公司子公司", text="利润下降", kind="user_material")).json()["evidence"]
    saved = client.put("/api/resume", json=dict(text="我使用 Python、SQL 开发项目", format="text"))
    assert saved.status_code == 200
    result = client.post("/api/analyze", json=dict(job_id=job["id"]))
    assert result.status_code == 200
    assert result.json()["coverage"] == pytest.approx(2 / 3, abs=0.01)
    response = client.post("/api/chat", json=dict(message="营收情况", job_id=job["id"]))
    assert response.status_code == 200
    assert response.json()["mode"] == "evidence_search"
    assert all(c["id"] != foreign["id"] for c in response.json()["citations"])
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert len(detail["evidence"]) == 1
    assert len(detail["company_evidence"]) == 1


def test_missing_resume_invalid_yaml_and_oversize_are_clear(client):
    job = client.post("/api/import/job", json=dict(title="开发", company="甲", description="Python")).json()["job"]
    assert client.post("/api/analyze", json=dict(job_id=job["id"])).status_code == 400
    assert client.put("/api/resume", json=dict(text="facts: [", format="facts_yaml")).status_code == 400
    assert client.post("/api/chat", json=dict(message="x" * 12001)).status_code == 422
    assert client.get("/api/jobs/unknown").status_code == 404


def test_import_does_not_fetch_user_url_and_rejects_javascript_link(client):
    result = client.post("/api/import/job", json=dict(title="开发", company="甲", description="Python", url="javascript:alert(1)"))
    assert result.status_code == 422


def test_resume_and_job_persist_across_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as first:
        first.put("/api/resume", json=dict(text="Python", format="text"))
        first.post("/api/import/job", json=dict(title="开发", company="甲", description="Python"))
    with TestClient(create_app(tmp_path)) as second:
        assert second.get("/api/resume").json()["text"] == "Python"
        assert len(second.get("/api/jobs").json()["jobs"]) == 1


def test_chat_requires_an_explicit_scope(client):
    result = client.post("/api/chat", json=dict(message="所有公司的情况"))
    assert result.status_code == 400


def test_extract_only_uses_saved_cninfo_metadata(client, monkeypatch):
    import sys
    import types
    repo = client.app.state.repo
    metadata = repo.save_evidence(dict(title="年度报告", company="甲", kind="disclosure_metadata", text="公告元信息", url="https://static.cninfo.com.cn/finalpage/2026-01-01/1.PDF"), "cninfo")
    module = types.ModuleType("jobscope.documents")
    module.extract_announcement = lambda url: dict(text="[第1页]\n营业收入100万元", pages=1, total_pages=2, truncated=True)
    monkeypatch.setitem(sys.modules, "jobscope.documents", module)
    response = client.post(f"/api/evidence/{metadata['id']}/extract")
    assert response.status_code == 200
    assert response.json()["evidence"]["source"] == "cninfo_pdf"
    assert response.json()["evidence"]["kind"] == "disclosure"
    assert "第1页" in response.json()["evidence"]["text"]
    user_evidence = client.post("/api/evidence", json=dict(title="资料", company="甲", text="内容", kind="user_material")).json()["evidence"]
    assert client.post(f"/api/evidence/{user_evidence['id']}/extract").status_code == 400
    assert client.post("/api/evidence/unknown/extract").status_code == 404


def wait_task(client, task_id):
    for _ in range(200):
        task = client.get(f"/api/tasks/{task_id}").json()["task"]
        if task["status"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError("任务未在预期时间内结束")


def test_browser_explicit_data_dir_and_collection_state(client, monkeypatch):
    import sys
    import types
    module = types.ModuleType("jobscope.browser")
    seen = []
    module.status = lambda data_dir: dict(installed=True, running=False, message=str(data_dir))
    module.start = lambda data_dir: dict(installed=True, running=True, message="已启动")
    def collect(source, query, location, limit, data_dir, progress=None):
        seen.append((source, query, data_dir))
        return dict(status="needs_login", message="请登录", jobs=[], evidence=[])
    module.collect = collect
    monkeypatch.setitem(sys.modules, "jobscope.browser", module)
    assert client.get("/api/browser/status").json()["installed"] is True
    response = client.post("/api/browser/collect", json=dict(query="Python")).json()
    task = wait_task(client, response["task"]["id"])
    assert task["status"] == "needs_login"
    assert seen[0][0] == "boss" and seen[0][2] == client.app.state.repo.data_dir


def test_browser_collect_runs_in_background_and_reports_progress(client, monkeypatch):
    import sys
    import types
    module = types.ModuleType("jobscope.browser")
    def collect(source, query, location, limit, data_dir, progress=None):
        if progress:
            progress(1, 1)
        return dict(status="completed", message="智联已读取", jobs=[dict(
            title="Python工程师", company="甲有限公司", location="北京", external_id="CCL1234567890J12345678901",
            url="https://www.zhaopin.com/jobdetail/CCL1234567890J12345678901.htm",
            description="一、岗位职责", description_status="complete")], evidence=[])
    module.collect = collect
    monkeypatch.setitem(sys.modules, "jobscope.browser", module)
    started = client.post("/api/browser/collect", json=dict(source="zhaopin", query="Python", location="北京", limit=50)).json()
    assert started["task"]["status"] == "running"
    task = wait_task(client, started["task"]["id"])
    assert task["status"] == "completed" and task["count"] == 1
    assert client.get("/api/jobs").json()["jobs"][0]["external_id"] == "CCL1234567890J12345678901"
    assert client.get(f"/api/tasks/{started['task']['id']}").json()["task"]["status"] == "completed"
    assert client.get("/api/tasks/no-such-id").status_code == 404


def test_cross_origin_write_is_blocked(client):
    response = client.put("/api/resume", json=dict(text="Python", format="text"), headers={"origin": "https://evil.example"})
    assert response.status_code == 403


def test_collection_persists_jobs_and_issuer_identity_without_query_alias(client, monkeypatch):
    import sys
    import types
    module = types.ModuleType("jobscope.connectors")
    module.BROWSER_SOURCES = ('zhaopin', '51job')
    def collect(source, query, location, limit, *, cache_dir):
        if source == "cninfo":
            return dict(status="completed", message="公告元信息", jobs=[], evidence=[dict(title="年报", company="甲简称", text="公告索引", kind="disclosure_metadata", url="https://static.cninfo.com.cn/1.PDF")])
        return dict(status="completed", message="查询完成", jobs=[dict(title="开发", company="甲法定名称", description="Python", external_id="123", url="https://example.com/123")], evidence=[])
    module.collect = collect
    monkeypatch.setitem(sys.modules, "jobscope.connectors", module)
    first = client.post("/api/collect", json=dict(source="remotive", query="Python"))
    assert first.json()["task"]["status"] == "completed"
    assert first.json()["jobs"][0]["source"] == "remotive"
    cninfo = client.post("/api/collect", json=dict(source="cninfo", query="甲法定名称"))
    assert cninfo.json()["task"]["count"] == 1
    assert client.get("/api/evidence?company=甲法定名称").json()["evidence"][0]["kind"] == "job_posting"
    issuer = client.get("/api/evidence?company=甲简称").json()["evidence"]
    assert issuer[0]["kind"] == "disclosure_metadata"
    assert issuer[0]["source"] == "cninfo"


def test_job_scope_does_not_accept_other_company(client):
    job = client.post("/api/import/job", json=dict(title="开发", company="甲", description="Python")).json()["job"]
    assert client.post("/api/chat", json=dict(job_id=job["id"], company="乙", message="情况")).status_code == 400


def test_analyze_selects_semantic_when_configured_and_surfaces_error(client, monkeypatch):
    import sys
    import types
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    job = client.post("/api/import/job", json=dict(title="开发", company="甲", description="Python")).json()["job"]
    client.put("/api/resume", json=dict(text="Python", format="text"))
    module = types.ModuleType("jobscope.semantic")
    module.analyze_semantically = lambda job, resume, company_evidence: dict(job_id=job["id"], method="model_assisted", score=80, coverage=1)
    monkeypatch.setitem(sys.modules, "jobscope.semantic", module)
    response = client.post("/api/analyze", json=dict(job_id=job["id"]))
    assert response.json()["method"] == "model_assisted"
    def failed(*args):
        raise ValueError("模型引用无效")
    module.analyze_semantically = failed
    response = client.post("/api/analyze", json=dict(job_id=job["id"]))
    assert response.status_code == 502
    assert "引用" in response.json()["detail"]


def test_collector_notes_preserved_and_cache_uses_data_dir(client, monkeypatch):
    import sys
    import types
    module = types.ModuleType("jobscope.connectors")
    module.BROWSER_SOURCES = ('zhaopin', '51job')
    def collect(source, query, location, limit, *, cache_dir):
        assert cache_dir == client.app.state.repo.data_dir / "cache"
        return dict(status="completed", message="完成", jobs=[dict(title="开发", company="甲品牌", description="Python", url="https://example.com/j1")], evidence=[dict(title="开发", company="甲品牌", kind="job_posting", text="公司字段为招聘品牌，法定主体未核验", url="https://example.com/j1")])
    module.collect = collect
    monkeypatch.setitem(sys.modules, "jobscope.connectors", module)
    result = client.post("/api/collect", json=dict(source="boss", query="Python")).json()
    assert result["task"]["status"] == "completed"
    detail = client.get("/api/jobs/" + result["jobs"][0]["id"]).json()
    assert any("法定主体未核验" in e["text"] for e in detail["evidence"])


def test_unknown_host_is_rejected_even_when_origin_matches(client):
    assert client.get("/api/resume", headers={"host": "attacker.example"}).status_code == 400
    assert client.put("/api/resume", json=dict(text="Python", format="text"), headers={"host": "attacker.example", "origin": "http://attacker.example"}).status_code == 400
    assert client.get("/api/health", headers={"host": "localhost:5173"}).status_code == 200
    assert client.get("/api/health", headers={"host": "[::1]:8000"}).status_code == 200


def test_sources_endpoint_describes_the_domestic_job_platforms(client):
    sources = {item['id']: item for item in client.get('/api/sources').json()['sources']}
    assert set(sources) >= {'boss', 'zhaopin', '51job', 'liepin', 'remotive', 'cninfo', 'user_import'}
    assert sources['zhaopin']['transport_mode'] == 'browser_page'
    assert sources['zhaopin']['browser_collectable'] is True
    assert sources['51job']['browser_collectable'] is True
    assert sources['liepin']['transport_mode'] == 'http_api'
    assert sources['liepin']['browser_collectable'] is False
    assert {'北京', '深圳', '西安'} <= set(sources['liepin']['cities'])
    assert sources['cninfo']['kind'] == 'evidence' and sources['cninfo']['cities'] == []
    assert len(sources['boss']['cities']) > 300, '地点提示要靠接口给出的城市字典，前端不该自带名单'
    assert set(sources['boss']['hot']) <= set(sources['boss']['cities']) and sources['boss']['hot']
    assert all(item['name'] and item['description'] for item in sources.values())


def test_cities_endpoint_serves_the_shared_pinyin_table(client):
    pinyin = client.get('/api/cities').json()['pinyin']
    assert pinyin['杭州'] == ['hangzhou', 'hz'] and len(pinyin) > 300


def test_collect_routes_browser_only_sources_into_the_page_session(client, monkeypatch):
    import sys
    import types
    browser = types.ModuleType('jobscope.browser')
    seen = []
    def collect(source, query, location, limit, data_dir):
        seen.append((source, query, location))
        return dict(status='completed', message='智联搜索页已读取岗位', jobs=[dict(
            title='Python工程师', company='甲有限公司', location='北京 海淀', external_id='CCL1234567890J1234567890',
            url='https://www.zhaopin.com/jobdetail/CCL1234567890J1234567890.htm',
            description='一、岗位职责', description_status='complete')], evidence=[])
    browser.collect = collect
    monkeypatch.setitem(sys.modules, 'jobscope.browser', browser)
    result = client.post('/api/collect', json=dict(source='zhaopin', query='Python', location='北京')).json()
    assert seen == [('zhaopin', 'Python', '北京')]
    assert result['task']['status'] == 'completed'
    job = result['jobs'][0]
    assert job['source'] == 'zhaopin' and job['description_status'] == 'complete'
    client.put('/api/resume', json=dict(text='一、岗位职责 Python', format='text'))
    assert client.post('/api/analyze', json=dict(job_id=job['id'])).status_code == 200


def test_browser_source_without_a_session_reports_login_not_zero_results(client):
    result = client.post('/api/collect', json=dict(source='51job', query='Python')).json()
    assert result['task']['status'] in ('needs_login', 'unsupported')
    assert '浏览器' in result['task']['message']
    assert result['jobs'] == []
    assert client.get('/api/jobs').json()['total'] == 0


def test_liepin_listing_waits_for_full_duties_before_analysis(client, monkeypatch):
    import sys
    import types
    module = types.ModuleType('jobscope.connectors')
    module.BROWSER_SOURCES = ('zhaopin', '51job')
    def collect(source, query, location, limit, *, cache_dir):
        return dict(status='completed', message='已读取猎聘检索列表', jobs=[dict(
            title='后端工程师', company='甲公司', location='北京-昌平区', salary='15-25k',
            external_id='86374907', url='https://www.liepin.com/job/1986374907.shtml',
            description='仅搜索列表字段；完整岗位职责尚未读取。', description_status='listing')], evidence=[])
    module.collect = collect
    monkeypatch.setitem(sys.modules, 'jobscope.connectors', module)
    job = client.post('/api/collect', json=dict(source='liepin', query='Python')).json()['jobs'][0]
    client.put('/api/resume', json=dict(text='Python 后端项目', format='text'))
    blocked = client.post('/api/analyze', json=dict(job_id=job['id']))
    assert blocked.status_code == 409 and '完整岗位' in blocked.json()['detail']


def test_browser_start_only_accepts_page_session_sources(client):
    assert client.post('/api/browser/start?source=nobody').status_code == 400


def test_locations_lists_saved_places_by_job_count(client):
    for index in range(2):
        client.post('/api/import/job', json=dict(title=f'后端工程师{index}', company='甲公司', description='Python', location='杭州'))
    client.post('/api/import/job', json=dict(title='前端工程师', company='乙公司', description='React', location='北京-昌平区'))
    client.post('/api/import/job', json=dict(title='无地点岗位', company='丙公司', description='Go', location=''))
    items = client.get('/api/locations').json()['locations']
    assert [(item['location'], item['jobs']) for item in items] == [('杭州', 2), ('北京-昌平区', 1)]
    assert client.get('/api/locations?limit=1').json()['locations'] == [dict(location='杭州', jobs=2)]
    assert client.get('/api/locations?limit=0').status_code == 422
