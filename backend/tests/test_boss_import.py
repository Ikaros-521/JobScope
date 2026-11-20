"""Boss 捕获篮导入（boss_import）的解析、篮子幂等与端点契约测试。

全程 tmp_path 隔离数据库；不触碰 DrissionPage / 真实浏览器（tabs/capture 在无连接时必须优雅降级）。
"""
import sys
from pathlib import Path

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


def test_p1_endpoints_contract(client, tmp_path, monkeypatch):
    # browse 状态端点与 open 端点：无浏览器时优雅降级，绝不 500、绝不真开 Chrome。
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': {'installed': True, 'running': False, 'message': 'stub-start'})
    status = client.get('/api/import/boss/browse').json()
    assert status['running'] is False and 'message' in status
    opened = client.post('/api/import/boss/open', json={'company': '某数据公司'}).json()
    assert opened['status'] == 'needs_browser'
    empty = client.post('/api/import/boss/open', json={'company': ' '}).status_code == 422
