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
    assert [t['source'] for t in tasks] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin',
                                               'signal_jobscope_jobs', 'reliability_compute']
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
    assert [t['source'] for t in tasks] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin',
                                               'job_details', 'signal_jobscope_jobs', 'reliability_compute']
    final = wait_task(client, next(task['id'] for task in tasks if task['source'] == 'job_details'))
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    assert final['status'] == 'completed'
    assert len(calls) == 1
    assert calls[0]['ids'] == [seeded['id']]
    assert calls[0]['limit'] == 1
    assert '海宁辰曦' in calls[0]['message']

    # 无待补岗位时不追加详情任务，信号与报告任务仍需执行。
    bare = client.post('/api/company-research', json={'company': '无岗位公司'}).json()['tasks']
    assert [t['source'] for t in bare] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin',
                                               'signal_jobscope_jobs', 'reliability_compute']
    assert wait_task(client, bare[-1]['id'])['status'] == 'completed'


def test_research_defaults_only_ready_available_free_and_never_paid(client, stub_channels, monkeypatch):
    from jobscope.reliability import collectors
    from jobscope.reliability.sources import PaidClient
    calls = []
    monkeypatch.setattr(PaidClient, 'fetch', lambda self, company: calls.append(company))
    monkeypatch.setattr(collectors, 'collect', lambda *args, **kwargs: pytest.fail('unverified GSXT is explicit only'))
    assert client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key='vendor-key',
                      url='http://127.0.0.1:9999/company')).status_code == 200
    tasks = client.post('/api/company-research', json={'company': '甲公司'}).json()['tasks']
    assert [task['source'] for task in tasks if task['source'].startswith('signal_')] == ['signal_jobscope_jobs']
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    local = wait_task(client, next(task['id'] for task in tasks if task['source'] == 'signal_jobscope_jobs'))
    assert local['status'] == 'completed' and local['count'] == 3
    assert calls == []
    report = client.get('/api/companies/甲公司/reliability').json()['report']
    assert report and report['evidence_ids']
    assert len([task for task in client.app.state.repo.tasks() if task['source'] == 'signal_jobscope_jobs']) == 1
    assert client.get('/api/reliability/sources').json()['sources']['jobscope_jobs']['last_run']['id'] == local['id']


@pytest.mark.parametrize('selection,code', [
    (['qcc'], 409),
    (['missing_source'], 400), (['jobscope_jobs', 'jobscope_jobs'], 400),
    (['gsxt'] * 7, 422),
])
def test_research_validates_selection_before_creating_tasks_or_threads(client, monkeypatch, selection, code):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid selection must not start a thread')
    monkeypatch.setattr('jobscope.main.threading.Thread', forbidden)
    result = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=selection))
    assert result.status_code == code
    assert client.app.state.repo.tasks() == []
    assert client.app.state.repo.find_company('甲公司') is None


def test_research_accepts_explicit_credit_and_court_selections(client, stub_channels, monkeypatch):
    """2026-11-13 接线后：信用中国/裁判文书网是合法的显式选择，执行走 collectors.collect。"""
    from jobscope.reliability import collectors
    seen = []

    def browser_collect(source, company, **kwargs):
        seen.append(source)
        return dict(status='needs_login', message='需要登录', signals=[])

    monkeypatch.setattr(collectors, 'collect', browser_collect)
    tasks = client.post('/api/company-research', json=dict(
        company='甲公司', signal_sources=['credit_china', 'court_records'])).json()['tasks']
    signals = [task for task in tasks if task['source'].startswith('signal_')]
    assert [task['source'] for task in signals] == ['signal_credit_china', 'signal_court_records']
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    finals = {task['source']: wait_task(client, task['id']) for task in signals}
    assert seen == ['credit_china', 'court_records']
    assert all(item['status'] == 'needs_login' and item['count'] == 0 for item in finals.values())


def test_research_empty_selection_and_disabled_default_do_not_collect(client, stub_channels, monkeypatch):
    from jobscope.reliability import local
    monkeypatch.setattr(local, 'collect', lambda *args, **kwargs: pytest.fail('signals not selected'))
    for selection in ([], None):
        if selection is None:
            assert client.put('/api/reliability/sources/jobscope_jobs', json={'mode': 'disabled'}).status_code == 200
        tasks = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=selection)).json()['tasks']
        assert not any(task['source'].startswith('signal_') for task in tasks)
        assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    result = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=['jobscope_jobs']))
    assert result.status_code == 409


def test_research_explicit_paid_and_gsxt_are_separate_from_web_and_local_runs_last(client, stub_channels, monkeypatch):
    from jobscope.reliability import collectors, local
    from jobscope.reliability.sources import PaidClient
    events = []
    original_local = local.collect

    def paid_fetch(self, company):
        events.append(('paid', company))
        return {'establish_date': '2015-04-01'}

    def gsxt_collect(source, company, **kwargs):
        events.append(('gsxt', company))
        return dict(status='needs_verification', message='请核对页面', signals=[])

    def local_collect(source, company, **kwargs):
        events.append(('local', company))
        assert 'liepin' in stub_channels  # job channels have finished before local statistics
        return original_local(source, company, **kwargs)

    monkeypatch.setattr(PaidClient, 'fetch', paid_fetch)
    monkeypatch.setattr(collectors, 'collect', gsxt_collect)
    monkeypatch.setattr(local, 'collect', local_collect)
    assert client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key='vendor-key',
                      url='http://localhost:9999/company')).status_code == 200
    tasks = client.post('/api/company-research', json=dict(company='甲 公司',
                        signal_sources=['jobscope_jobs', 'qcc', 'gsxt'])).json()['tasks']
    signals = [task for task in tasks if task['source'].startswith('signal_')]
    assert [task['source'] for task in signals] == ['signal_qcc', 'signal_gsxt', 'signal_jobscope_jobs']
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    assert events == [('paid', '甲公司'), ('gsxt', '甲公司'), ('local', '甲公司')]
    finals = {task['source']: wait_task(client, task['id']) for task in tasks}
    assert finals['qcc']['status'] == 'completed'  # public web lookup is distinct
    assert finals['signal_qcc']['status'] == 'partial' and finals['signal_qcc']['count'] == 1
    assert finals['signal_gsxt']['status'] == 'needs_verification'
    assert client.get('/api/companies/甲公司/reliability').json()['report']['score'] is not None
    assert len(client.app.state.repo.tasks()) == len(tasks)


def test_queued_source_is_rechecked_after_disable_without_calling_paid(client, stub_channels, monkeypatch):
    from jobscope.reliability.sources import PaidClient, SignalSourceInput, SignalSourceStore
    from jobscope import collect_service
    store = SignalSourceStore(client.app.state.repo)
    store.save('qcc', SignalSourceInput(mode='enabled', key='vendor-key', url='http://localhost:9999/company'))
    monkeypatch.setattr(PaidClient, 'fetch', lambda *args: pytest.fail('disabled paid source was called'))
    original = collect_service.execute_collection

    def disable_during_jobs(*args, **kwargs):
        store.save('qcc', SignalSourceInput(mode='disabled'))
        return original(*args, **kwargs)

    monkeypatch.setattr(collect_service, 'execute_collection', disable_during_jobs)
    tasks = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=['qcc'])).json()['tasks']
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    signal = wait_task(client, next(task['id'] for task in tasks if task['source'] == 'signal_qcc'))
    assert signal['status'] == 'skipped' and signal['count'] == 0
    assert '未启用' in signal['message']


def test_worker_signal_and_compute_failures_finalize_all_tasks(client, stub_channels, monkeypatch):
    from jobscope.reliability import local
    from jobscope.reliability.service import ReliabilityService
    secret = 'secret-must-not-be-stored'

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(local, 'collect', fail)
    monkeypatch.setattr(ReliabilityService, 'recompute', fail)
    tasks = client.post('/api/company-research', json={'company': '甲公司'}).json()['tasks']
    finals = {task['source']: wait_task(client, task['id']) for task in tasks}
    assert finals['signal_jobscope_jobs']['status'] == 'failed'
    assert finals['reliability_compute']['status'] == 'failed'
    assert not any(task['status'] in ('pending', 'running') for task in finals.values())
    assert secret not in str(finals)

