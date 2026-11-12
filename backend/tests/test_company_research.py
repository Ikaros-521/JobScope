import sys
import time
import types
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


@pytest.fixture
def stub_channels(monkeypatch):
    """接口渠道：cninfo 抛错（单渠道失败样例）、liepin 正常；快查渠道 qcc 正常、tyc 抛错；浏览器渠道一律返回未启动。
    返回 {source: [limit, ...]}：各岗位渠道收到的采集上限记录，供 limit 透传断言用。"""
    limits: dict[str, list[int]] = {}

    connectors = types.ModuleType('jobscope.connectors')
    connectors.BROWSER_SOURCES = {'boss', 'zhaopin', '51job'}

    def fake_collect(source, query, location='', limit=20, cache_dir=None):
        limits.setdefault(source, []).append(limit)
        if source == 'cninfo':
            raise RuntimeError('巨潮接口不可用')
        return {'status': 'completed', 'message': f'{source} 检索完成', 'jobs': [], 'evidence': []}
    connectors.collect = fake_collect

    browser = types.ModuleType('jobscope.browser')

    def fake_browser_collect(source, query, location='', limit=20, data_dir=None, progress=None):
        limits.setdefault(source, []).append(limit)
        return {'status': 'needs_login', 'message': '请先启动项目浏览器并登录。', 'jobs': [], 'evidence': []}
    browser.collect = fake_browser_collect

    lookup = types.ModuleType('jobscope.company_lookup')

    def fake_lookup_collect(channel, company, *, browser_module, data_dir=None):
        if channel == 'tyc':
            raise RuntimeError('天眼查页面读取失败')
        return {'status': 'completed', 'message': f'{channel} 公开页解析到 1 家公司',
                'cards': [{'name': company, 'url': 'https://example.com/firm/x', 'text': '法定代表人：张三'}]}
    lookup.collect = fake_lookup_collect

    monkeypatch.setitem(sys.modules, 'jobscope.connectors', connectors)
    monkeypatch.setitem(sys.modules, 'jobscope.browser', browser)
    monkeypatch.setitem(sys.modules, 'jobscope.company_lookup', lookup)
    return limits


def wait_task(client, task_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f'/api/tasks/{task_id}').json()['task']
        if task['status'] not in ('running', 'pending'):
            return task
        time.sleep(0.2)
    return client.get(f'/api/tasks/{task_id}').json()['task']


def test_company_research_runs_all_channels_and_isolates_failures(client, stub_channels):
    started = client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司'})
    assert started.status_code == 200
    tasks = started.json()['tasks']
    assert [t['source'] for t in tasks] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin']
    # 端点异步返回：后台线程执行渠道，stub 渠道瞬时完成属正常，只验证渠道规划与隔离。

    finals = {t['source']: wait_task(client, t['id']) for t in tasks}
    # 单渠道失败被隔离：cninfo / tyc 抛错只影响自己。
    assert finals['cninfo']['status'] == 'failed'
    assert '不影响其他渠道' in finals['cninfo']['message']
    assert finals['tyc']['status'] == 'failed'
    # 快查完成的卡片归档为公司快照证据。
    assert finals['qcc']['status'] == 'completed'
    # 其余渠道各自到达终态，互不牵连。
    assert finals['liepin']['status'] == 'completed'
    for source in ('boss', 'zhaopin', '51job'):
        assert finals[source]['status'] == 'needs_login'
    evidence = client.get('/api/evidence?company=海宁辰曦科技有限公司&q=张三&page_size=5').json()
    assert evidence['total'] >= 1


def test_company_research_rejects_blank_company(client):
    assert client.post('/api/company-research', json={'company': ''}).status_code == 422
    assert client.post('/api/company-research', json={'company': '  '}).status_code == 422


def test_company_research_limit_passthrough(client, stub_channels):
    """limit 透传到全部岗位渠道（横评补数据传更大上限）；不传走默认 10（详情页行为不变）；越界拒绝。"""
    started = client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司', 'limit': 30})
    assert started.status_code == 200
    for item in started.json()['tasks']:
        wait_task(client, item['id'])
    for source in ('cninfo', 'boss', 'zhaopin', '51job', 'liepin'):
        assert stub_channels[source] == [30], source

    again = client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司'})
    assert again.status_code == 200
    for item in again.json()['tasks']:
        wait_task(client, item['id'])
    assert stub_channels['liepin'][-1] == 10

    assert client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司', 'limit': 201}).status_code == 422
    assert client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司', 'limit': 0}).status_code == 422
