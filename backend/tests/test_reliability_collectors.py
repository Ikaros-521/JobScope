"""Collector tests driven by a fake tab: no socket, no Chrome, no DrissionPage import."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager

from jobscope.reliability import collectors
from jobscope.reliability.models import SignalInput

# 两个来源页各自能解析出数字的 HTML；真实页面结构未实测，正则假设写在 collectors 的注释里。
REGISTRATION_HTML = '<td>成立日期 2015-04-01</td><td>注册资本 1000万元人民币</td>'
REPORTS_HTML = '<td>2024年度报告</td><td>城镇职工基本养老保险 240人</td><td>城镇职工基本养老保险 312人</td>'
BOTH_HTML = '<td>成立日期 2015-04-01</td>' + REPORTS_HTML
CAPTCHA_HTML = '<div>请输入验证码通过校验</div>'
EMPTY_HTML = '<div>企业概要</div><td>注册资本 1000万元人民币</td>'


class FakeTab:
    """只回放一段固定 HTML，不发任何网络请求；visited 记录请求序列，closed 记录是否被收尾。"""

    def __init__(self, html):
        self._html = html
        self.visited = []
        self.closed = False

    def get(self, url, timeout=None):
        self.visited.append(url)
        return True

    @property
    def html(self):
        return self._html

    def close(self):
        self.closed = True


class DeadTab(FakeTab):
    """校验跳转中标签页可能失效：读取与关闭抛错都不该把异常抛进请求路径。"""

    @property
    def html(self):
        raise RuntimeError('tab is gone')

    def close(self):
        raise RuntimeError('tab is gone')


class FakeBrowser:
    """只提供 collectors 用到的 status / session_lock / _connection，全部走构造参数，不碰真浏览器。"""

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


def drive(monkeypatch, html=None, *, installed=True, running=True, session_free=True, tabbed=True, tab=None):
    """把一次采集接到假 tab 上，返回 (假 tab, 假浏览器, 实际等待过的秒数)。

    tabbed=False 时保留 collectors._open_tab 的真实实现：没有可用会话时它应当在导入
    DrissionPage 之前就返回 None，正好由假浏览器验证这条路径。
    """
    tab = tab if tab is not None else FakeTab(html)
    browser = FakeBrowser(installed=installed, running=running, session_free=session_free)
    slept = []
    if tabbed:
        monkeypatch.setattr(collectors, '_open_tab', lambda browser_module, data_dir: tab)
    monkeypatch.setattr(collectors, '_sleep', lambda seconds: slept.append(seconds))
    return tab, browser, slept


def test_registration_page_yields_company_age_without_guessing():
    tab = FakeTab(REGISTRATION_HTML)
    result = collectors._read_registration(tab, '甲公司')
    assert result['rule'] == 'company_age' and result['value']['years'] >= 1
    assert result['source_class'] == 'official'
    assert result['dimension'] == 'stability' and result['unit'] == '年'
    # 成立日期是页面上唯一明确的数字，observed_at 只能由它得到。
    assert result['observed_at'] == '2015-04-01'
    assert len(tab.visited) == 1


def test_annual_report_yields_insured_headcount():
    tab = FakeTab(REPORTS_HTML)
    result = collectors._read_annual_report(tab, '甲公司')
    assert result['rule'] == 'insured_headcount_trend'
    assert result['value'] == {'before': 312, 'after': 240}
    assert result['unit'] == '人' and result['source_class'] == 'official'


def test_a_page_without_the_number_yields_no_signal_at_all():
    """解析不到就返回 None：宁可不入库，也不补默认值。"""
    assert collectors._read_registration(FakeTab(EMPTY_HTML), '甲公司') is None
    assert collectors._read_annual_report(FakeTab(EMPTY_HTML), '甲公司') is None
    assert collectors._read_annual_report(FakeTab('<td>城镇职工基本养老保险 240人</td>'), '甲公司') is None


def test_captcha_page_stops_without_retrying():
    tab = FakeTab(CAPTCHA_HTML)
    assert collectors._read_registration(tab, '甲公司') is None
    assert len(tab.visited) == 1


def test_collection_is_bounded_by_pages_and_never_replays_requests():
    assert collectors.MAX_PAGES <= 5 and collectors.PAGE_DELAY >= 2


def test_a_full_pass_visits_one_page_per_rule_spaces_them_and_closes_the_tab(monkeypatch):
    tab, browser, slept = drive(monkeypatch, BOTH_HTML)
    rules = []
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser, on_status=rules.append)
    assert outcome['status'] == 'completed'
    assert [item['rule'] for item in outcome['signals']] == ['company_age', 'insured_headcount_trend']
    assert len(tab.visited) == 2 and len(set(tab.visited)) == 2
    # 间隔只在第二页之前发生一次，且不少于 PAGE_DELAY。
    assert slept == [collectors.PAGE_DELAY]
    assert rules == ['company_age', 'insured_headcount_trend']
    assert tab.closed is True


def test_verification_page_stops_after_one_page_and_is_left_open(monkeypatch):
    tab, browser, slept = drive(monkeypatch, CAPTCHA_HTML)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    assert len(tab.visited) == 1
    # 停在验证页时不许关标签页，用户得能在前台处理。
    assert tab.closed is False


def test_signals_feed_the_stored_signal_contract(monkeypatch):
    """采集结果的字段名必须原样喂进 SignalInput，Task 10 的端点才不会拼不出信号。"""
    tab, browser, slept = drive(monkeypatch, BOTH_HTML)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    for item in outcome['signals']:
        signal = SignalInput(company_id='CO-0123456789abcdef', evidence_id='E-1',
                             collected_at='2026-11-09T00:00:00+00:00',
                             **{key: item[key] for key in
                                ('rule', 'dimension', 'value', 'unit', 'source_class', 'observed_at')})
        assert signal.rule == item['rule']


def test_a_page_that_simply_has_no_number_is_reported_as_partial_not_verification(monkeypatch):
    tab, browser, slept = drive(monkeypatch, EMPTY_HTML)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert len(tab.visited) == 1 and tab.closed is True


def test_an_unavailable_session_asks_for_login_instead_of_guessing(monkeypatch):
    tab, browser, slept = drive(monkeypatch, BOTH_HTML, running=False, tabbed=False)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert tab.visited == [] and browser.locks == 1 and slept == []


def test_an_occupied_session_backs_off_instead_of_queueing(monkeypatch):
    tab, browser, slept = drive(monkeypatch, BOTH_HTML, session_free=False)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'backoff' and outcome['signals'] == []
    assert tab.visited == [] and tab.closed is False


def test_a_source_without_a_chrome_dependency_is_not_collected(monkeypatch):
    tab, browser, slept = drive(monkeypatch, BOTH_HTML, installed=False, tabbed=False)
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'unsupported' and tab.visited == []
    assert browser.locks == 0


def test_unknown_source_is_unsupported(monkeypatch):
    tab, browser, slept = drive(monkeypatch, BOTH_HTML)
    outcome = collectors.collect('qcc', '甲公司', browser_module=browser)
    assert outcome['status'] == 'unsupported' and outcome['signals'] == []
    assert tab.visited == [] and browser.locks == 0


def test_a_tab_that_dies_mid_collection_is_reported_not_raised(monkeypatch):
    """标签页失效（跳转/被回收）时只退化状态，异常不该冒出到端点那一层。"""
    tab, browser, slept = drive(monkeypatch, tab=DeadTab(''))
    outcome = collectors.collect('gsxt', '甲公司', browser_module=browser)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert len(tab.visited) == 1
