"""Boss 捕获篮导入（boss_import）的解析、篮子幂等与端点契约测试。

全程 tmp_path 隔离数据库；不触碰 DrissionPage / 真实浏览器（tabs/capture 在无连接时必须优雅降级；
投递记录同步用假标签页回放预置的接口响应）。
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope import boss_import, browser, job_details
from jobscope.main import create_app
from jobscope.repository import Repository


@pytest.fixture(autouse=True)
def isolated_basket():
    """篮子是模块级分区缓存：每个用例前后清空，避免用例间串味。"""
    boss_import._baskets.clear()
    yield
    boss_import._baskets.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_parse_links_app_share_and_dedupe():
    text = ('【资深后端开发】25-40K·15薪 经验5-10年 本科 杭州云栖科技\n'
            'https://www.zhipin.com/job_detail/abc123XYZ_-9.html?securityId=xx&lid=y\n'
            '重复链接 https://www.zhipin.com/job_detail/abc123XYZ_-9.html\n'
            '另一条裸路径 /job_detail/ZZZ987yyy10.html')
    items, overflow = boss_import.parse_links(text)
    assert overflow is False
    assert [item['external_id'] for item in items] == ['abc123XYZ_-9', 'ZZZ987yyy10']
    first = items[0]
    # App 分享行的元数据顺带提取；URL 剥掉时效参数，保持 job_details 可读的干净形态。
    assert first['title'] == '资深后端开发'
    assert first['salary'] == '25-40K·15薪'
    assert first['location'] == '杭州'
    assert first['company'] == ''
    assert first['url'] == 'https://www.zhipin.com/job_detail/abc123XYZ_-9.html'
    assert first['imported_via'] == 'link'
    assert items[1]['title'].startswith('Boss岗位')


def test_parse_links_without_links_returns_empty():
    items, overflow = boss_import.parse_links('随便一段没有链接的文本，比如【岗位】20K')
    assert items == [] and overflow is False


def test_card_to_item_builds_clean_listing_and_skips_noise():
    card = {'id': 'jobid12345', 'title': '测试工程师', 'company': '某科技公司', 'salary': '15-25K',
            'area': '杭州·西湖区', 'link_text': '', 'text': '', 'tags': ['Python']}
    item = boss_import._card_to_item(card, 'current_page', 'https://www.zhipin.com/web/geek/job?query=x')
    assert item['url'] == 'https://www.zhipin.com/job_detail/jobid12345.html'
    assert item['description_status'] == 'listing'
    assert item['imported_via'] == 'current_page'
    assert '完整岗位职责尚未读取' in item['description']
    # 三字段全空的锚点不是岗位卡；非法 id 同样丢弃。
    assert boss_import._card_to_item({'id': 'okid1234', 'title': '', 'company': '', 'salary': '', 'link_text': ''}, 'current_page', 'u') is None
    assert boss_import._card_to_item({'id': '带空格的 id'}, 'current_page', 'u') is None


def test_basket_merge_keeps_first_channel_and_fills_empty_fields(tmp_path):
    sparse, _ = boss_import.parse_links('/job_detail/merge000111.html')
    boss_import.basket_add(sparse, data_dir=tmp_path)
    rich = boss_import._card_to_item({'id': 'merge000111', 'title': '后端工程师', 'company': '某公司', 'salary': '30K'}, 'current_page', 'page')
    outcome = boss_import.basket_add([rich], data_dir=tmp_path)
    assert outcome == {'added': 0, 'updated': 1}
    merged = boss_import._store(tmp_path)['merge000111']
    assert merged['imported_via'] == 'link'  # 保留首次发现通道
    assert merged['title'] == '后端工程师' and merged['salary'] == '30K'  # 非空字段更新


def test_basket_survives_restart(tmp_path):
    # P2：篮子持久化到 data_dir/boss_import_basket.json；重启（内存缓存清空）后从磁盘恢复。
    items, _ = boss_import.parse_links('/job_detail/persist00001.html')
    boss_import.basket_add(items, data_dir=tmp_path)
    assert (tmp_path / 'boss_import_basket.json').is_file()
    boss_import._baskets.clear()  # 模拟进程重启
    view = boss_import.basket_view(Repository(tmp_path))
    assert view['total'] == 1 and view['items'][0]['external_id'] == 'persist00001'
    assert view['items'][0]['in_library'] is False
    # 显式清空后磁盘同样清空。
    assert boss_import.basket_clear(data_dir=tmp_path)['cleared'] == 1
    boss_import._baskets.clear()
    assert boss_import.basket_view(Repository(tmp_path))['total'] == 0


def test_link_import_flow_idempotent_star_and_provenance(client):
    text = '【后端】20-30K https://www.zhipin.com/job_detail/aaaa1111bbbb.html'
    added = client.post('/api/import/links', json={'text': text}).json()
    assert added['added'] == 1
    basket = client.get('/api/import/basket').json()
    assert basket['total'] == 1 and basket['items'][0]['in_library'] is False

    confirmed = client.post('/api/import/confirm', json={'auto_star': True}).json()
    assert confirmed['imported'] == 1
    job_id = confirmed['jobs'][0]['id']
    listing = client.get('/api/jobs?source=boss').json()
    assert listing['total'] == 1
    assert listing['jobs'][0]['starred'] is True
    detail = client.get(f'/api/jobs/{job_id}').json()['job']
    assert detail['imported_via'] == 'link' and detail['captured_from'] == '粘贴链接'
    assert detail['description_status'] == 'listing'  # 导入只是占位，等补全详情

    # 同一批重复导入：幂等归并，不产生第二条档案，收藏幸存。
    client.post('/api/import/links', json={'text': text})
    basket = client.get('/api/import/basket').json()
    assert basket['items'][0]['in_library'] is True
    again = client.post('/api/import/confirm', json={}).json()
    assert again['imported'] == 1
    assert client.get('/api/jobs?source=boss').json()['total'] == 1
    assert client.get(f'/api/jobs/{job_id}').json()['job']['starred'] is True

    cleared = client.post('/api/import/basket/clear').json()
    assert cleared['cleared'] == 0  # 确认入库后条目已出篮
    assert client.get('/api/import/basket').json()['total'] == 0


def test_import_links_rejects_text_without_links(client):
    response = client.post('/api/import/links', json={'text': '没有链接的文本'})
    assert response.status_code == 400


def test_tabs_and_capture_degrade_without_running_browser(client, tmp_path):
    # tmp_path 下没有 connection.json：必须返回 needs_browser 状态而不是 500（测试机可能装着 DrissionPage）。
    assert browser._connection(tmp_path) is None
    tabs = client.get('/api/import/boss/tabs').json()
    assert tabs['status'] == 'needs_browser' and tabs['tabs'] == []
    capture = client.post('/api/import/boss/page', json={'tab_id': 'whatever'}).json()
    assert capture['status'] == 'needs_browser' and capture['added'] == 0


def test_normalize_detail_backfills_company_only_for_placeholder():
    job = {'id': 'J-x', 'source': 'boss', 'title': '占位', 'company': '',
           'url': 'https://www.zhipin.com/job_detail/abc12345.html', 'description': 'x'}
    snapshot = {'url': job['url'], 'title': '正式岗位名', 'description': '职责描述' * 10,
                'company': '某科技有限公司\n已上市 · 1000人以上', 'salary': '20-25K'}
    output = job_details.normalize_detail(job, snapshot)
    assert output['status'] == 'completed'
    assert output['job']['company'] == '某科技有限公司'  # 只取首行，剥离自述附文
    assert output['job']['title'] == '正式岗位名'
    # 已有列表公司名时不允许详情页名称悄悄替换（身份范围保护仍然生效）。
    kept = job_details.normalize_detail({**job, 'company': '列表公司'}, snapshot)
    assert kept['job']['company'] == '列表公司'


def test_browser_start_accepts_lookup_pages(tmp_path, monkeypatch):
    # 端点守卫必须与 browser.start 的 PAGES → LOOKUP_PAGES 落点同口径（qcc/tyc 曾被 400 拦掉）。
    # 只断言 LOOKUP_PAGES 里长期存在的 qcc；其他渠道后续合入时自然落进同一守卫。
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': True, 'message': 'stub'})
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/browser/start?source=qcc').status_code == 200
        assert client.post('/api/browser/start?source=bogus').status_code == 400


# ---- P1：逛即采集 / 一键收藏 / 在 Boss 打开 ------------------------------------------

def _wapi_payload(external_id, **fields):
    return {'code': 0, 'zpData': {'jobList': [dict(
        encryptJobId=external_id, jobName='数据工程师', brandName='某数据公司',
        cityName='北京', salaryDesc='30-45K', jobLabels=['大数据'], **fields)]}}


def test_browse_packet_absorbs_into_basket_via_search_mapping(tmp_path):
    from types import SimpleNamespace
    packet = SimpleNamespace(response=SimpleNamespace(body=__import__('json').dumps(_wapi_payload('browseJobId01'))),
                             url='https://www.zhipin.com/wapi/zpgeek/search/joblist.json?query=%E6%95%B0%E6%8D%AE&city=101010100')
    boss_import._absorb_browse_packet(packet, tmp_path)
    item = boss_import._store(tmp_path)['browseJobId01']
    assert item['imported_via'] == 'browse'
    assert '搜索「数据」' in item['captured_from']  # 溯源带上用户当时的搜索词
    assert item['title'] == '数据工程师' and item['url'].endswith('browseJobId01.html')
    # 登录/验证响应（code 7）不产垃圾条目，只更新提示。
    packet2 = SimpleNamespace(response=SimpleNamespace(body=__import__('json').dumps({'code': 7})), url='')
    boss_import._absorb_browse_packet(packet2, tmp_path)
    assert len(boss_import._store(tmp_path)) == 1
    assert '登录' in boss_import._browse_state['message']


def test_absorb_remote_rebuilds_identity_and_rejects_foreign_pages(tmp_path):
    # 油猴推送：服务端重建 id/URL，不信任客户端；非 zhipin 来源整包拒绝。
    cards = [{'id': 'remoteId12345', 'title': '测试', 'company': '某公司', 'salary': '20K', 'area': '上海',
              'url': 'https://www.zhipin.com/job_detail/remoteId12345.html?lid=evil&securityId=x'},
             {'id': 'bad id!', 'title': 'x', 'company': 'y', 'salary': 'z'}]
    outcome = boss_import.absorb_remote(cards, 'https://www.zhipin.com/web/geek/job?query=x', data_dir=tmp_path)
    assert outcome['status'] == 'ok' and outcome['added'] == 1
    item = boss_import._store(tmp_path)['remoteId12345']
    assert item['url'] == 'https://www.zhipin.com/job_detail/remoteId12345.html'  # 时效参数被剥掉
    assert item['imported_via'] == 'script' and '油猴脚本' in item['captured_from']
    assert boss_import.absorb_remote(cards, 'https://evil.example.com/job_detail/x.html', data_dir=tmp_path)['status'] == 'failed'
    # 只有 id 的卡片不是有效岗位卡。
    assert boss_import.absorb_remote([{'id': 'remoteId99999'}], 'https://www.zhipin.com/x', data_dir=tmp_path)['status'] == 'failed'


def test_remote_endpoint_flow(client):
    payload = {'page_url': 'https://www.zhipin.com/web/geek/job?query=go',
               'items': [{'id': 'remoteEp00001', 'title': 'Golang 工程师', 'company': '某公司', 'salary': '25K'}]}
    added = client.post('/api/import/basket/remote', json=payload).json()
    assert added['status'] == 'ok' and added['added'] == 1
    confirmed = client.post('/api/import/confirm', json={'auto_star': True}).json()
    assert confirmed['imported'] == 1
    job = confirmed['jobs'][0]
    assert job['imported_via'] == 'script' and job['company'] == '某公司'
    bad = client.post('/api/import/basket/remote', json={**payload, 'page_url': 'https://evil.com/x'})
    assert bad.status_code == 400


def test_browse_start_needs_browser_without_connection(tmp_path):
    outcome = boss_import.browse_start(data_dir=tmp_path)
    assert outcome['status'] == 'needs_browser' and outcome['running'] is False
    assert boss_import.browse_status()['running'] is False
    # 未运行时 stop 也安全返回。
    assert boss_import.browse_stop()['status'] == 'ok'


def test_favorites_import_without_browser_reports_need(tmp_path, monkeypatch):
    # 一键收藏会自动拉起浏览器；测试环境必须 stub 掉，绝不能真的启动 Chrome。
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': False, 'message': 'stub-start'})
    outcome = boss_import.import_favorites(repo=None, task={}, data_dir=tmp_path)
    assert outcome['status'] == 'needs_browser'
    assert outcome['message'] == 'stub-start'


def test_open_search_without_browser_reports_need(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': False, 'message': 'stub-start'})
    outcome = boss_import.open_search(data_dir=tmp_path, company='某公司')
    assert outcome['status'] == 'needs_browser'
    assert boss_import.open_search(data_dir=tmp_path, company='')['status'] == 'failed'


def test_login_wall_detection(tmp_path):
    class FakeTab:
        url = 'https://www.zhipin.com/web/user/?ka=x'
        title = '个人中心'
    assert boss_import._login_wall(FakeTab()) is False
    assert boss_import._login_wall(type('T', (), {'url': 'https://www.zhipin.com/web/common/login', 'title': ''})()) is True
    assert boss_import._login_wall(type('T', (), {'url': 'https://www.zhipin.com/safe/verify', 'title': '安全验证'})()) is True


def test_page_url_paging_keeps_other_params():
    # 收藏页实测形态（推荐中心 tab=4）：翻页只动 page，其余参数原样保留。
    url = 'https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&page=1&tag=4'
    assert boss_import._page_url(url, 2) == 'https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&page=2&tag=4'
    assert boss_import._page_url(url, 10) == 'https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&page=10&tag=4'
    # URL 不带 page 参数 → 返回空串，调用方退回滚动加载兜底。
    assert boss_import._page_url('https://www.zhipin.com/web/geek/recommend?tab=4', 2) == ''


def test_open_favorites_probes_calibrated_entry_first():
    visited = []
    page_url = boss_import.FAVORITE_PRIMARY

    class FakeTab:
        url = page_url
        title = '收藏'

        def get(self, url, timeout=20):
            visited.append(url)
            return True

        def run_js(self, script):
            return {'page_url': self.url,
                    'cards': [{'id': 'favCard00001', 'title': 'x', 'link_text': 'x', 'text': '', 'tags': []}]}

    assert boss_import._open_favorites(FakeTab()) == page_url
    # 实测入口直进：第一跳就是收藏页，不再先绕个人中心。
    assert visited == [page_url]


def test_library_detail_states_reads_payload_status(tmp_path):
    # 去重闸的数据面：库里 listing 状态查得到（要补详情），未入库的查不到（全新捕获）。
    repo = Repository(tmp_path)
    items, _ = boss_import.parse_links('/job_detail/state000001.html')
    boss_import.basket_add(items, data_dir=tmp_path)
    boss_import.confirm(repo)
    states = boss_import._library_detail_states(repo, ['state000001', 'missing000001'])
    assert states[boss_import.job_id_for('state000001')] == ('listing', '')
    assert boss_import.job_id_for('missing000001') not in states


def test_confirm_persists_company_statement_from_basket(tmp_path):
    # 详情顺带读取产出的公司自述证据随确认入库落证据表与规模自述信号（与补全详情同一通路）。
    repo = Repository(tmp_path)
    items, _ = boss_import.parse_links('/job_detail/statement01.html')
    item = dict(items[0], company='某规模公司',
                company_evidence={'title': '某规模公司 · 招聘平台公司自述', 'company': '某规模公司',
                                  'url': items[0]['url'], 'kind': 'company_statement',
                                  'text': '来源为招聘详情页的公司自述及平台展示，非独立核验。\n1000-9999人'})
    boss_import.basket_add([item], data_dir=tmp_path)
    outcome = boss_import.confirm(repo, auto_star=True)
    assert outcome['imported'] == 1
    with repo.connection() as conn:
        rows = conn.execute("SELECT payload FROM evidence WHERE kind='company_statement'").fetchall()
    assert len(rows) == 1
    evidence = __import__('json').loads(rows[0]['payload'])
    assert evidence['company'] == '某规模公司'
    # 规模自述信号由证据文本确定性解析产出。
    with repo.connection() as conn:
        signals = conn.execute("SELECT payload FROM company_signals WHERE rule='team_size_statement'").fetchall()
    assert len(signals) == 1


def test_p1_endpoints_contract(client, tmp_path, monkeypatch):
    # browse 状态端点与 open 端点：无浏览器时优雅降级，绝不 500、绝不真开 Chrome。
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': False, 'message': 'stub-start'})
    status = client.get('/api/import/boss/browse').json()
    assert status['running'] is False and 'message' in status
    opened = client.post('/api/import/boss/open', json={'company': '某数据公司'}).json()
    assert opened['status'] == 'needs_browser'
    # 2026-11-24：此前这行只是赋值没有 assert，「空公司名应 422」的校验一直空转。
    assert client.post('/api/import/boss/open', json={'company': ' '}).status_code == 422


# ---- 投递记录同步（2026-11-23）：接住页面自己的 geekDeliverList 响应 ------------------------

# 实测响应形态（output/diag_applied_net.json）：cardList 每条带 encryptJobId 与投递时刻 happenTime（毫秒）。
def _deliver_page(cards, has_more=False, code=0):
    return {'code': code, 'zpData': {'hasMore': has_more, 'cardList': cards}}


def _deliver_card(external_id, happen_ms=1795173303000, **fields):
    # 1795173303000 = 2026-11-20 19:15:03（北京时间）= 2026-11-20T11:15:03+00:00
    return dict(encryptJobId=external_id, jobName='AI Agent 工程师', brandName='某科技公司', salaryDesc='20-40K',
                jobSalary='20-40K', cityName='杭州', areaDistrict='西湖区', unifyLocationDetail='杭州·西湖区·文二路',
                jobLabels=['Agent', 'Python'], happenTime=happen_ms, **fields)


class _FakeListen:
    def __init__(self):
        self.queue, self.started, self.stopped = [], None, False

    def start(self, target):
        self.started = target

    def wait(self, count=1, timeout=2, raise_err=False):
        return self.queue.pop(0) if self.queue else False

    def stop(self):
        self.stopped = True


class _FakeAppliedTab:
    """按 URL 的 page 参数回放预置的投递记录响应（页面未发该请求的页传 None）；run_js 回放 DOM 卡片。

    tab_state 是「已投递」页签查询（_personal_tab_js）的回放值：默认 'ok'（页面已停在该页签）。
    """

    def __init__(self, pages, dom_cards=None, tab_state='ok'):
        self.pages, self.dom_cards, self.tab_state = pages, dom_cards or [], tab_state
        self.listen, self.visited, self.closed, self.url = _FakeListen(), [], False, ''

    def get(self, url, timeout=20):
        self.visited.append(url)
        self.url = url
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        body = self.pages.get(page)
        if body is not None:
            self.listen.queue.append(SimpleNamespace(
                url=f'https://www.zhipin.com/wapi/zprelation/resume/geekDeliverList?page={page}',
                response=SimpleNamespace(body=json.dumps(body))))

    def run_js(self, script):
        if 'user-jobs-tab-nav' in script:
            return self.tab_state() if callable(self.tab_state) else self.tab_state
        return {'page_url': self.url, 'cards': self.dom_cards}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_applied_browser(tmp_path, monkeypatch):
    """项目浏览器「已在运行」+ DrissionPage 换成回放桩；等待上限与翻页间隔压到 0，用例不空等。"""
    state = {'tab': None, 'fronted': []}

    def install(tab):
        state['tab'] = tab
        # Chromium(address).new_tab() 两级调用链：都换成回放桩。
        monkeypatch.setitem(sys.modules, 'DrissionPage', SimpleNamespace(
            Chromium=lambda _address: SimpleNamespace(new_tab=lambda: tab)))

    monkeypatch.setattr(browser, '_connection', lambda data_dir=None: 9222)
    monkeypatch.setattr(browser, '_front', lambda tab: state['fronted'].append(tab))
    monkeypatch.setattr(boss_import, 'APPLIED_WAIT', 0.2)
    monkeypatch.setattr(boss_import.random, 'uniform', lambda low, high: 0)
    return install, state


def test_applied_at_converts_platform_timestamp_and_rejects_bad_values():
    assert boss_import._applied_at(1795173303000) == '2026-11-20T11:15:03+00:00'
    # 与 repository.now() 同口径：UTC、带时区后缀，事件表里按字符串排序即时序。
    assert boss_import._applied_at(1795427461000).endswith('+00:00')
    # 非毫秒级（秒/脏数据）、未来时刻、非数字一律视为未知。
    assert boss_import._applied_at(1795173303) == ''
    assert boss_import._applied_at(time.time() * 1000 + 3600 * 1000) == ''
    assert boss_import._applied_at(None) == '' and boss_import._applied_at('abc') == ''


def test_record_from_deliver_builds_basket_item_with_applied_time():
    record = boss_import._record_from_deliver(_deliver_card('deliverJob001'), 'Boss 投递记录 · 页')
    assert record['external_id'] == 'deliverJob001'
    assert record['imported_via'] == 'applied' and record['applied_at'] == '2026-11-20T11:15:03+00:00'
    assert record['title'] == 'AI Agent 工程师' and record['company'] == '某科技公司'
    assert record['salary'] == '20-40K' and record['location'] == '杭州·西湖区·文二路'
    assert record['url'] == 'https://www.zhipin.com/job_detail/deliverJob001.html'
    assert record['description_status'] == 'listing'
    # 没有 unifyLocationDetail 时城市+区拼接；encryptJobId 非法整条丢弃。
    sparse = boss_import._record_from_deliver(dict(_deliver_card('deliverJob002'), unifyLocationDetail=''), '')
    assert sparse['location'] == '杭州·西湖区'
    assert boss_import._record_from_deliver(_deliver_card('带 空格的 id'), '') is None


def test_import_applied_backfills_library_and_baskets_the_rest(tmp_path, fake_applied_browser):
    repo = Repository(tmp_path)
    tracked = repo.save_job(dict(title='意向岗', company='甲公司', description='原文', external_id='libApplied01'), 'boss')
    repo.update_jobs([tracked['id']], {'pipeline_status': 'interested'})
    plain = repo.save_job(dict(title='未收藏岗', company='乙公司', description='原文', external_id='plainApplied1'), 'boss')
    tab = _FakeAppliedTab({1: _deliver_page([_deliver_card('libApplied01'), _deliver_card('plainApplied1')], has_more=True),
                           2: _deliver_page([_deliver_card('newApplied001', happen_ms=1795427461000)], has_more=False)})
    fake_applied_browser[0](tab)
    task = repo.create_task('boss', 'Boss 投递记录同步')

    outcome = boss_import.import_applied(repo, task, data_dir=tmp_path)

    assert outcome['status'] == 'completed'
    # 直进实测入口；hasMore=true 才翻下一页（URL page 参数），翻页后收停；标签页用完即关。
    assert tab.visited == [boss_import.APPLIED_PRIMARY, boss_import.APPLIED_PRIMARY.replace('page=1', 'page=2')]
    assert tab.listen.started == boss_import.APPLIED_LISTEN and tab.listen.stopped is True and tab.closed is True
    # 库内岗位：意向 → 已投递，状态变化时间与事件都是 Boss 记录的真实投递时刻（不是导入当天）。
    synced = repo.job(tracked['id'])
    assert synced['pipeline_status'] == 'applied' and synced['applied_source'] == 'boss'
    assert synced['applied_at'] == '2026-11-20T11:15:03+00:00'
    assert synced['pipeline_status_changed_at'] == synced['applied_at']
    assert synced['description'] == '原文'  # 状态回填不覆盖档案正文
    with repo.connection() as conn:
        rows = conn.execute('SELECT from_status,to_status,created_at FROM job_pipeline_events WHERE job_id=?',
                            (tracked['id'],)).fetchall()
    # 设「意向」时已落一条事件；投递事件排在末尾，from_status 承接上一状态。
    assert [(row['from_status'], row['to_status'], row['created_at']) for row in rows] == [
        ('', 'interested', rows[0]['created_at']), ('interested', 'applied', '2026-11-20T11:15:03+00:00')]
    # 未收藏的库内岗位：设状态即自动收藏（与 update_jobs 同语义）。
    starred = repo.job(plain['id'])
    assert starred['pipeline_status'] == 'applied' and starred['starred'] is True
    # 不在库的投递岗位进捕获篮待确认，带上平台投递时刻。
    basket = boss_import.basket_view(repo)
    assert basket['total'] == 1
    pending = basket['items'][0]
    assert pending['external_id'] == 'newApplied001' and pending['imported_via'] == 'applied'
    assert pending['applied_at'] == '2026-11-23T09:51:01+00:00'
    assert '共 3 条' in outcome['message'] and '库里 2 条：新标记已投递 2 条' in outcome['message']
    assert '1 条不在库' in outcome['message'] and '投递时间取自 Boss 记录' in outcome['message']
    assert outcome['truncated'] is False and '还有更多' not in outcome['message']  # hasMore=false 正常读完
    # 函数内只回写进度（终态文案由端点的 worker 回写，见端点契约用例）。
    assert task['count'] == 3 and '已读取 3 条' in task['message']


def test_import_applied_reports_truncation_when_paging_stops_early(tmp_path, fake_applied_browser):
    repo = Repository(tmp_path)
    # 第 1 页 hasMore=true，第 2 页页面没有再发投递记录请求（翻页不生效/被缓存）→ 退回 DOM 且与第 1 页同批 → 收停。
    # 已读部分照常回填，但结果必须明示「还有更多未同步」，不能让人以为投递记录就这么多。
    dom_card = {'id': 'pageOneJob001', 'title': '平台工程师', 'company': '丙公司', 'salary': '25K', 'area': '北京',
                'link_text': '', 'text': '', 'tags': []}
    tab = _FakeAppliedTab({1: _deliver_page([_deliver_card('pageOneJob001')], has_more=True), 2: None},
                          dom_cards=[dom_card])
    fake_applied_browser[0](tab)
    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)
    assert outcome['status'] == 'completed' and outcome['truncated'] is True
    assert '仅读取了前 2 页，Boss 上还有更多投递记录未同步' in outcome['message']
    assert '共 1 条' in outcome['message']
    assert boss_import.basket_view(repo)['items'][0]['applied_at'] == '2026-11-20T11:15:03+00:00'  # 第 1 页的接口数据没丢


def test_import_applied_refuses_data_unless_applied_tab_is_confirmed(tmp_path, fake_applied_browser):
    # 防污染闸：个人中心默认停在上次看的页签，深链一旦失效页面上可能是收藏/沟通过列表。
    # 页签找不到、或点了也确认不了当前页签时，一条数据都不采信——宁可同步失败，不能把别的列表标成已投递。
    install, _ = fake_applied_browser
    repo = Repository(tmp_path)
    dom_card = {'id': 'favLookAlike1', 'title': '其实是收藏岗', 'company': '丙公司', 'salary': '25K', 'area': '北京',
                'link_text': '', 'text': '', 'tags': []}
    missing = _FakeAppliedTab({1: None}, dom_cards=[dom_card], tab_state='missing')
    install(missing)
    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)
    assert outcome['status'] == 'failed' and '没有找到 Boss「已投递」页签' in outcome['message']
    assert boss_import.basket_view(repo)['total'] == 0 and missing.closed is True
    # 页签点得动但一直确认不了「当前页签是已投递」：DOM 里纵然有卡片也不采信。
    unconfirmed = _FakeAppliedTab({1: None}, dom_cards=[dom_card], tab_state='clicked')
    install(unconfirmed)
    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)
    assert outcome['status'] == 'failed' and '未能确认页面停在 Boss「已投递」页签' in outcome['message']
    assert boss_import.basket_view(repo)['total'] == 0


def test_select_personal_tab_retries_through_page_reload():
    calls = {'n': 0}

    class FlakyTab:
        def run_js(self, script):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise RuntimeError('页面被刷新，请操作前尝试等待页面刷新或加载完成。')  # DrissionPage ContextLost
            return 'ok'

    assert boss_import._select_personal_tab(FlakyTab(), 'personal_top_submitted') == 'ok'
    assert calls['n'] == 3
    assert 'personal_top_submitted' in boss_import._personal_tab_js('personal_top_submitted')


def test_open_favorites_falls_back_to_personal_center_tab(monkeypatch):
    # 收藏主入口打不开时：进个人中心点「感兴趣」页签（2026-11-24 实测可达收藏页）。
    # 点完必须确认当前页签确实是「感兴趣」才采信卡片——个人中心默认停在上次的页签。
    monkeypatch.setattr(boss_import.time, 'sleep', lambda seconds: None)  # 探针轮询不空等
    visited, tab_states = [], iter(['clicked', 'ok'])

    class FakeTab:
        url = ''
        title = 'BOSS直聘'

        def get(self, url, timeout=20):
            visited.append(url)
            self.url = url
            return True

        def run_js(self, script):
            if 'user-jobs-tab-nav' in script:
                return next(tab_states)
            # 主入口没有卡片；个人中心点完页签后出现收藏卡。
            cards = [{'id': 'fallbackFav01', 'title': 'x', 'link_text': 'x', 'text': '', 'tags': []}] \
                if self.url == boss_import.USER_CENTER else []
            return {'page_url': self.url, 'cards': cards}

    tab = FakeTab()
    assert boss_import._open_favorites(tab) == boss_import.USER_CENTER
    assert visited == [boss_import.FAVORITE_PRIMARY, boss_import.USER_CENTER]  # 主入口仍先试，再走兜底
    # 页签怎么都确认不了：不采信卡片，如实报没找到。
    tab_states = iter(['clicked', 'clicked'])
    assert boss_import._open_favorites(FakeTab()) == ''


def test_import_applied_falls_back_to_dom_cards_without_timestamp(tmp_path, fake_applied_browser):
    repo = Repository(tmp_path)
    dom_card = {'id': 'domApplied01', 'title': '平台工程师', 'company': '丙公司', 'salary': '25K', 'area': '北京',
                'link_text': '', 'text': '', 'tags': []}
    # 页面没有发投递记录接口请求（接口改版/被缓存）：退回 DOM 抽卡；第 2 页同一批卡片 → 没有新记录收停。
    tab = _FakeAppliedTab({1: None, 2: None}, dom_cards=[dom_card])
    fake_applied_browser[0](tab)

    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)

    assert outcome['status'] == 'completed' and len(tab.visited) == 2
    assert '未取到 Boss 投递时间' in outcome['message']
    item = boss_import.basket_view(repo)['items'][0]
    assert item['external_id'] == 'domApplied01' and item['imported_via'] == 'applied' and item['applied_at'] == ''


def test_import_applied_reports_login_wall_and_keeps_tab(tmp_path, fake_applied_browser):
    repo = Repository(tmp_path)
    install, state = fake_applied_browser
    # 接口回 code 7（实测登录失效码）→ needs_login，标签页留给用户登录（不关、拉到前台）。
    tab = _FakeAppliedTab({1: _deliver_page([], code=7)})
    install(tab)
    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)
    assert outcome['status'] == 'needs_login' and tab.closed is False and state['fronted'] == [tab]
    assert boss_import.basket_view(repo)['total'] == 0
    # 空列表（接口正常但没有投递）：如实报空，不产垃圾条目。
    empty = _FakeAppliedTab({1: _deliver_page([])})
    install(empty)
    outcome = boss_import.import_applied(repo, repo.create_task('boss', 'Boss 投递记录同步'), data_dir=tmp_path)
    assert outcome['status'] == 'completed' and '还没有投递记录' in outcome['message']
    assert boss_import.basket_view(repo)['total'] == 0 and empty.closed is True


def test_confirm_marks_basket_items_from_applied_records(tmp_path):
    repo = Repository(tmp_path)
    boss_import.basket_add([boss_import._record_from_deliver(_deliver_card('basketApplied1'), 'x')], data_dir=tmp_path)
    outcome = boss_import.confirm(repo)
    job = repo.job(outcome['jobs'][0]['id'])
    assert job['pipeline_status'] == 'applied' and job['starred'] is True
    assert job['applied_at'] == '2026-11-20T11:15:03+00:00'
    assert '按 Boss 投递记录标记为已投递' in outcome['message']
    # 再同步一次同一条：幂等，不重复落事件。
    again = repo.sync_applied([(job['id'], '2026-11-20T11:15:03+00:00')])
    assert again == {'changed': 0, 'kept': 1, 'archived': 0, 'events': 0}


def test_applied_endpoint_runs_as_background_task(client, tmp_path, monkeypatch):
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': False, 'message': 'stub-start'})
    started = client.post('/api/import/boss/applied').json()['task']
    for _ in range(100):
        task = client.get(f"/api/tasks/{started['id']}").json()['task']
        if task['status'] != 'running':
            break
        time.sleep(0.05)
    assert task['status'] == 'needs_browser' and task['message'] == 'stub-start'
    assert task['kind'] == 'boss_import'
