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
    # 信号采集（collect_source）会 import reliability.local → job_stats（from ..connectors import
    # CITY_CODES），一键完备起信号源在本文件独立运行时也会真的被 import，stub 同样供得上。
    connectors.CITY_CODES = {}

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
    # 一键完备（2026-11-17）：默认研究含 AI 行业判断（llm 信号源先于本地统计）与材料抽取，
    # 两者排在重算之前——抽取出的信号要进同一次报告。
    assert [t['source'] for t in tasks] == ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin',
                                               'signal_llm_industry', 'signal_jobscope_jobs',
                                               'reliability_extract', 'reliability_compute']
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
    # 未配置模型时 AI 行业判断与材料抽取如实收尾为 skipped，不阻塞重算。
    assert finals['signal_llm_industry']['status'] == 'skipped'
    assert finals['reliability_extract']['status'] == 'skipped'
    assert finals['reliability_compute']['status'] == 'completed'
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
                                               'job_details', 'signal_llm_industry', 'signal_jobscope_jobs',
                                               'reliability_extract', 'reliability_compute']
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
                                               'signal_llm_industry', 'signal_jobscope_jobs',
                                               'reliability_extract', 'reliability_compute']
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
    # 一键完备默认追加 AI 行业判断（免费、模型判断），本地统计仍垫底。
    assert [task['source'] for task in tasks if task['source'].startswith('signal_')] == ['signal_llm_industry', 'signal_jobscope_jobs']
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
    """显式选择合法、执行走 collectors.collect（三个浏览器来源 2026-11-15 起暂停；本用例测
    选择与执行链路，不测暂停，临时摘掉两者的 suspended 标记）。"""
    from jobscope.reliability import collectors
    from jobscope.reliability.sources import SIGNAL_SOURCES
    for source in ('credit_china', 'court_records'):
        monkeypatch.delitem(SIGNAL_SOURCES[source], 'suspended')
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
            # 默认选择路径下把两个免费信号源都关掉：一键完备的默认追加尊重设置状态，不再排队。
            assert client.put('/api/reliability/sources/jobscope_jobs', json={'mode': 'disabled'}).status_code == 200
            assert client.put('/api/reliability/sources/llm_industry', json={'mode': 'disabled'}).status_code == 200
        tasks = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=selection)).json()['tasks']
        assert not any(task['source'].startswith('signal_') for task in tasks)
        assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    result = client.post('/api/company-research', json=dict(company='甲公司', signal_sources=['jobscope_jobs']))
    assert result.status_code == 409


def test_research_explicit_paid_and_browser_sources_run_last_before_local(client, stub_channels, monkeypatch):
    """付费 API 与浏览器来源跟岗位渠道分开执行，本地统计垫底（三个浏览器来源 2026-11-15 起暂停；
    本用例测执行顺序，不测暂停，临时摘掉信用中国的 suspended 标记让它代表浏览器来源）。"""
    from jobscope.reliability import collectors, local
    from jobscope.reliability.sources import PaidClient, SIGNAL_SOURCES
    monkeypatch.delitem(SIGNAL_SOURCES['credit_china'], 'suspended')
    events = []
    original_local = local.collect

    def paid_fetch(self, company):
        events.append(('paid', company))
        return {'establish_date': '2015-04-01'}

    def credit_collect(source, company, **kwargs):
        events.append(('credit', company))
        return dict(status='needs_verification', message='请核对页面', signals=[])

    def local_collect(source, company, **kwargs):
        events.append(('local', company))
        assert 'liepin' in stub_channels  # job channels have finished before local statistics
        return original_local(source, company, **kwargs)

    monkeypatch.setattr(PaidClient, 'fetch', paid_fetch)
    monkeypatch.setattr(collectors, 'collect', credit_collect)
    monkeypatch.setattr(local, 'collect', local_collect)
    assert client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key='vendor-key',
                      url='http://localhost:9999/company')).status_code == 200
    tasks = client.post('/api/company-research', json=dict(company='甲 公司',
                        signal_sources=['jobscope_jobs', 'qcc', 'credit_china'])).json()['tasks']
    signals = [task for task in tasks if task['source'].startswith('signal_')]
    assert [task['source'] for task in signals] == ['signal_qcc', 'signal_credit_china', 'signal_jobscope_jobs']
    assert wait_task(client, tasks[-1]['id'])['status'] == 'completed'
    assert events == [('paid', '甲公司'), ('credit', '甲公司'), ('local', '甲公司')]
    finals = {task['source']: wait_task(client, task['id']) for task in tasks}
    assert finals['qcc']['status'] == 'completed'  # public web lookup is distinct
    assert finals['signal_qcc']['status'] == 'partial' and finals['signal_qcc']['count'] == 1
    assert finals['signal_credit_china']['status'] == 'needs_verification'
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


def test_sources_health_buckets_and_empty_window(client, stub_channels):
    """来源健康度：空库返回空表；跑完一轮一键调研后，成败分桶与任务记录口径一致——
    单渠道失败只计入自己，needs_login 类状态计「未完成」而非失败。"""
    assert client.get('/api/sources/health').json()['sources'] == []
    started = client.post('/api/company-research', json={'company': '海宁辰曦科技有限公司'})
    for item in started.json()['tasks']:
        wait_task(client, item['id'])
    data = client.get('/api/sources/health').json()
    assert data['window'] == '最近 100 次任务'
    by_source = {item['source']: item for item in data['sources']}
    assert by_source['cninfo']['failed'] >= 1 and by_source['cninfo']['ok'] == 0
    assert by_source['tyc']['failed'] >= 1
    assert by_source['qcc']['ok'] >= 1 and by_source['liepin']['ok'] >= 1
    for source in ('boss', 'zhaopin', '51job'):
        assert by_source[source]['attention'] >= 1 and by_source[source]['failed'] == 0
    # 最近成功时间与消息都引用任务记录；排序按最近一次任务时间倒序（同秒任务并列不破坏单调性）。
    runs = [item['last_run_at'] for item in data['sources']]
    assert runs == sorted(runs, reverse=True)
    assert all(item['last_message'] for item in data['sources'])


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



def test_sources_health_counts_running_and_cancelled_as_attention(client):
    """回归：running（进行中）与 cancelled（用户取消）进「未完成」桶——落失败桶会让
    一次日常采集或手动停止就把来源显示成挂了。"""
    repo = client.app.state.repo
    task = repo.create_task('demo_channel', '某公司')
    item = next(s for s in client.get('/api/sources/health').json()['sources'] if s['source'] == 'demo_channel')
    assert item['attention'] == 1 and item['failed'] == 0 and item['ok'] == 0
    task.update(status='cancelled', message='手动停止')
    repo.update_task(task)
    item = next(s for s in client.get('/api/sources/health').json()['sources'] if s['source'] == 'demo_channel')
    assert item['attention'] == 1 and item['failed'] == 0
