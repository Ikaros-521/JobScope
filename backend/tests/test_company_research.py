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
    # 真身 job_details 在 company-research 收尾步会被 import（from .connectors import plain），stub 也得供得上。
    connectors.plain = lambda value='': str(value or '').strip()

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


def test_company_research_appends_job_details_finale(client, stub_channels, monkeypatch):
    """一键补数据收尾步：该公司 boss/智联的非完整岗位追加为第 8 个任务送详情补读——
    渠道列表只存摘要，完整 JD（jd 具体度与薪资分层的材料）靠这步逐条读；没有缺口时不追加。"""
    import threading
    calls: list[dict] = []
    details = types.ModuleType('jobscope.job_details')
    details.DETAIL_SOURCES = {'boss': ('www.zhipin.com', r'/job_detail/[A-Za-z0-9_-]+\.html'),
                              'zhaopin': ('www.zhaopin.com', r'/jobdetail/[A-Za-z0-9]+\.htm')}
    details.batch_lock = threading.Lock()

    def fake_run_batch(repo, task, limit=500, ids=None, collector=None, pause=time.sleep, message=None):
        calls.append({'ids': list(ids or []), 'limit': limit, 'message': message})
        task.update(kind='job_details', status='completed', message=message or '完整JD补读完成', count=len(ids or []))
        repo.update_task(task)
    details.run_batch = fake_run_batch
    monkeypatch.setitem(sys.modules, 'jobscope.job_details', details)

    repo = client.app.state.repo
    seeded = repo.save_job(dict(title='后端工程师', company='海宁辰曦科技有限公司', location='杭州',
                                salary='15-25K', description='仅列表摘要', description_status='listing',
                                source='boss', url='https://www.zhipin.com/job_detail/abc123.html'), 'boss')
    # 别家公司的缺口不进本家收尾池。
    repo.save_job(dict(title='他司岗位', company='另一家公司', location='杭州', salary='10-15K',
                       description='仅列表摘要', description_status='listing',
                       source='boss', url='https://www.zhipin.com/job_detail/other99.html'), 'boss')

    started = client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司'})
    tasks = started.json()['tasks']
    assert [t['source'] for t in tasks] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin', 'job_details']
    final = wait_task(client, tasks[-1]['id'])
    assert final['status'] == 'completed'
    assert len(calls) == 1
    assert calls[0]['ids'] == [seeded['id']]
    assert calls[0]['limit'] == 1
    assert '海宁辰曦' in calls[0]['message']

    # 没有可补岗位的公司不追加收尾任务（仍是最初 7 渠道）。
    bare = client.post('/api/company-research', json={'company': '无岗位公司'}).json()['tasks']
    assert [t['source'] for t in bare] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin']
