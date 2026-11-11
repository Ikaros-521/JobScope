"""Company web lookup tests driven by a fake tab: no socket, no Chrome, no DrissionPage import."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager

from fastapi.testclient import TestClient

from jobscope import company_lookup
from jobscope.main import create_app
from jobscope.repository import Repository

# 真实的卡片抽取发生在页面自己的 DOM 里（CARDS_JS 由浏览器执行）；测试在 FakeTab.run_js 处接上，
# 直接回放「锚点 + 容器文本」的中间结果，等价于浏览器端抽出的数据。Python 侧的字段正则照常全跑。
FULL_TEXT = ('甲科技有限公司'
             '法定代表人：张三  注册资本：1000万人民币  成立日期：2015-04-01'
             ' 经营状态：存续 统一社会信用代码：91110000123456789X 电话：010-12345678')
MINIMAL_TEXT = '乙网络股份有限公司 注册资本：500万元'
NO_FIELD_TEXT = '只有一段没有可识别字段的介绍文字'

# 卡片名在锚点文本里；字段在容器文本里，两者由 CARDS_JS 分别带出。
CARDS = [
    dict(href='https://www.tianyancha.com/company/1', title='甲科技有限公司', text=FULL_TEXT),
    dict(href='https://www.tianyancha.com/company/2', title='乙网络股份有限公司', text=MINIMAL_TEXT),
    dict(href='https://www.tianyancha.com/company/3', title='丙公司', text=NO_FIELD_TEXT),
]


class FakeTab:
    """回放一段页面文本与一张卡片清单，不发任何网络请求；closed 记录是否被收尾。"""

    def __init__(self, cards, html=''):
        self._cards = cards
        self._html = html
        self.visited = []
        self.closed = False

    def get(self, url, timeout=None):
        self.visited.append(url)
        return True

    @property
    def html(self):
        return self._html

    def run_js(self, script, *args):
        # 真实实现里 script 是 CARDS_JS、args[0] 是链接特征；这里只回放数据。
        return json.dumps(self._cards)

    def close(self):
        self.closed = True


class DeadTab(FakeTab):
    """会话中途失效：run_js 抛错不该把异常抛进请求路径。"""

    def run_js(self, script, *args):
        raise RuntimeError('tab is gone')


class FakeBrowser:
    """只提供 company_lookup 用到的 status / session_lock / _connection，不碰真浏览器。"""

    def __init__(self, installed=True, running=True, session_free=True):
        self.installed = installed
        self.running = running
        self.session_free = session_free
        self.locks = 0

    def status(self, data_dir=None):
        return {'installed': self.installed, 'running': self.running,
                'message': '会话可连接。' if self.running else '尚未启动项目 Chrome。'}

    @contextmanager
    def session_lock(self, data_dir=None):
        self.locks += 1
        yield self.session_free

    def _connection(self, data_dir=None):
        return 9333 if self.running else None


def drive(monkeypatch, cards=CARDS, html='', *, installed=True, running=True, session_free=True, tabbed=True):
    """把一次快查接到假 tab 上，返回 (假 tab, 假浏览器, 实际等待过的秒数)。

    tabbed=False 时保留 company_lookup._open_tab 的真实实现：没有可用会话时它应当在导入
    DrissionPage 之前就返回 None，正好由假浏览器验证这条路径。
    """
    tab = FakeTab(cards, html)
    browser = FakeBrowser(installed=installed, running=running, session_free=session_free)
    slept = []
    if tabbed:
        monkeypatch.setattr(company_lookup, '_open_tab', lambda browser_module, data_dir: tab)
    monkeypatch.setattr(company_lookup, '_sleep', lambda seconds: slept.append(seconds))
    return tab, browser, slept


def test_parse_card_maps_only_visible_fields():
    card = company_lookup._parse_card(CARDS[0])
    assert card['name'] == '甲科技有限公司'
    assert card['url'] == 'https://www.tianyancha.com/company/1'
    assert card['fields']['法定代表人'] == '张三'
    assert card['fields']['注册资本'] == '1000万人民币'
    assert card['fields']['成立日期'] == '2015-04-01'
    # 状态后缀的括号说明不属于状态本身，只取页面状态词。
    assert card['fields']['经营状态'] == '存续'
    assert card['fields']['统一社会信用代码'] == '91110000123456789X'
    assert card['fields']['电话'] == '010-12345678'
    lines = card['text'].splitlines()
    assert lines[0] == '法定代表人：张三'


def test_parse_card_without_recognizable_fields_keeps_raw_text():
    card = company_lookup._parse_card(CARDS[2])
    assert card['name'] == '丙公司'
    assert card['fields'] == {}
    assert card['text'] == NO_FIELD_TEXT
    assert company_lookup._parse_card({'href': '', 'text': FULL_TEXT}) is None


def test_a_full_pass_visits_one_page_and_closes_the_tab(monkeypatch):
    tab, browser, slept = drive(monkeypatch)
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'completed'
    assert [card['name'] for card in outcome['cards']] == ['甲科技有限公司', '乙网络股份有限公司', '丙公司']
    assert len(tab.visited) == 1 and 'key=' in tab.visited[0]
    assert slept == [company_lookup.RENDER_DELAY]
    assert tab.closed is True
    assert browser.locks == 1


def test_cards_are_capped_at_max_cards(monkeypatch):
    many = [dict(href=f'https://www.tianyancha.com/company/{index}', title=f'公司{index}', text=MINIMAL_TEXT)
            for index in range(10)]
    tab, browser, _ = drive(monkeypatch, cards=many)
    outcome = company_lookup.collect('qcc', '公司', browser_module=browser)
    assert outcome['status'] == 'completed'
    assert len(outcome['cards']) == company_lookup.MAX_CARDS


def test_login_wall_without_cards_is_left_open(monkeypatch):
    tab, browser, _ = drive(monkeypatch, cards=[], html='<div>请登录后查看更多公司信息</div><div>安全校验</div>')
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'needs_verification'
    assert outcome['cards'] == []
    # 停在验证页时把页面留在前台交给用户处理。
    assert tab.closed is False


def test_markers_do_not_block_when_cards_were_parsed(monkeypatch):
    # 两家站点的页头常驻「登录」按钮：只要解析出卡片，登录字样不算拦截。
    tab, browser, _ = drive(monkeypatch, html='<span>请登录</span>')
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'completed'
    assert tab.closed is True


def test_no_cards_without_markers_is_an_honest_partial(monkeypatch):
    tab, browser, _ = drive(monkeypatch, cards=[], html='<div>没有找到相关公司</div>')
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'partial'
    assert outcome['cards'] == []
    assert tab.closed is True


def test_dead_session_is_failed_not_crashed(monkeypatch):
    tab, browser, _ = drive(monkeypatch)
    monkeypatch.setattr(company_lookup, '_open_tab', lambda browser_module, data_dir: DeadTab([], html=''))
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'failed'
    assert outcome['cards'] == []


def test_without_a_running_session_it_asks_for_login_first(monkeypatch):
    _, browser, _ = drive(monkeypatch, running=False, tabbed=False)
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'needs_login'


def test_without_an_installed_driver_it_is_unsupported(monkeypatch):
    _, browser, _ = drive(monkeypatch, installed=False)
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'unsupported'


def test_a_busy_session_backs_off_without_queueing(monkeypatch):
    _, browser, _ = drive(monkeypatch, session_free=False)
    outcome = company_lookup.collect('tyc', '甲科技', browser_module=browser)
    assert outcome['status'] == 'backoff'


def test_unknown_channel_and_empty_company_are_rejected():
    browser = FakeBrowser()
    assert company_lookup.collect('foo', '甲科技', browser_module=browser)['status'] == 'unsupported'
    assert company_lookup.collect('tyc', '   ', browser_module=browser)['status'] == 'failed'


def completed(cards):
    return dict(status='completed', message=f'解析到 {len(cards)} 家公司。', cards=cards)


def test_endpoint_persists_snapshots_and_dedupes_repeats(tmp_path, monkeypatch):
    cards = [dict(name='甲科技有限公司', url='https://www.tianyancha.com/company/1',
                  fields={'法定代表人': '张三'}, text='法定代表人：张三')]
    monkeypatch.setattr(company_lookup, 'collect',
                        lambda channel, company, *, browser_module, data_dir=None: completed(cards))
    with TestClient(create_app(tmp_path)) as client:
        first = client.post('/api/company/lookup', json={'channel': 'tyc', 'company': '甲科技'}).json()
        assert first['task']['status'] == 'completed' and first['task']['count'] == 1
        assert first['cards'][0]['name'] == '甲科技有限公司'
        # 第二次查询走同一条 identity 去重，不产生重复证据。
        second = client.post('/api/company/lookup', json={'channel': 'tyc', 'company': ' 甲科技 '}).json()
        assert second['task']['count'] == 1
    with Repository(tmp_path).connection() as conn:
        rows = conn.execute("SELECT payload FROM evidence WHERE company='甲科技有限公司'").fetchall()
    assert len(rows) == 1
    item = json.loads(rows[0]['payload'])
    assert item['kind'] == 'web_company_profile' and item['source'] == 'tyc'
    assert item['url'] == 'https://www.tianyancha.com/company/1'


def test_endpoint_keeps_failed_statuses_in_the_task_record(tmp_path, monkeypatch):
    outcome = dict(status='needs_verification', message='要求人工校验。', cards=[])
    monkeypatch.setattr(company_lookup, 'collect',
                        lambda channel, company, *, browser_module, data_dir=None: outcome)
    with TestClient(create_app(tmp_path)) as client:
        data = client.post('/api/company/lookup', json={'channel': 'qcc', 'company': '甲科技'}).json()
    assert data['task']['status'] == 'needs_verification' and data['task']['count'] == 0
    assert data['cards'] == []


def test_endpoint_rejects_unknown_channel(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/company/lookup', json={'channel': 'foo', 'company': '甲'})
    assert response.status_code == 422


def test_sources_register_company_web_channels(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        sources = {item['id']: item for item in client.get('/api/sources').json()['sources']}
    for channel in ('qcc', 'tyc'):
        assert sources[channel]['kind'] == 'company'
        assert sources[channel]['transport_mode'] == 'company_web'
        # 公司快查不走岗位采集路由，也不出现在岗位浏览器采集的下拉里。
        assert sources[channel]['browser_collectable'] is False
