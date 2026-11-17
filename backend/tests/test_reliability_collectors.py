"""Collector tests driven by fake tabs: no socket, no Chrome, no DrissionPage import."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from urllib.parse import urlencode, urljoin

from jobscope.reliability import collectors, local
from jobscope.reliability.models import SignalInput
from jobscope.repository import Repository, now

# 页面 HTML 均为按 2026-11-15 实测 DOM 构造的回放样本（真实快照见 output/gsxt-*.html）。
NAME = '甲公司'
GSXT_SEARCH_URL = collectors.GSXT_SEARCH_URL
GSXT_DETAIL_PATH = '/corp-query-detail-abc123.html'
GSXT_DETAIL_URL = urljoin(GSXT_SEARCH_URL, GSXT_DETAIL_PATH)
CREDIT_SEARCH_URL = collectors.CREDIT_ORIGIN + '/home?' + urlencode({'keyword': NAME})

# 真实壳页/详情页远大于就绪门槛，回放样本补齐体量。
PAD = '<!-- ' + 'x' * 3000 + ' -->'
GSXT_SHELL_HTML = '<div><input id="keyword" name="searchword"></div>' + PAD
GSXT_RESULTS_HTML = (
    f'<a class="search_list_item db" target="_blank" href="{GSXT_DETAIL_PATH}">'
    f'<h1 class="f20"><font color="red">{NAME[0]}</font>{NAME[1:]}</h1></a>'
    '<a class="search_list_item db" target="_blank" href="/corp-query-detail-other.html">'
    '<h1 class="f20">乙公司</h1></a>')
GSXT_DETAIL_HTML = ('<span class="regNum_inner">成立日期：<span class="nameBoxColor">2015年04月01日</span></span>'
                    + PAD)
GSXT_ANNUAL_HTML = ('<td>城镇职工基本养老保险 240人</td><td>城镇职工基本养老保险 312人</td>' + PAD)
SLIDER_HTML = '<div>请拖动滑块完成拼图验证</div>'
LOGIN_HTML = '<div>请登录后查看</div>' + PAD


class FakeTabs:
    """collectors 用 tab.browser.get_tabs() 找 target=_blank 开出的详情标签页；测试用本登记表回放。"""

    def __init__(self):
        self.tabs = []

    def get_tabs(self):
        return list(self.tabs)


class FakeFlowTab:
    """按 URL 回放多页 HTML；ele() 服务于公示系统检索框/查询按钮/结果条目/年报页签与裁判文书网检索框。

    公示系统的回放语义：壳页（has_search_box=True 时有 #keyword）→ 点 #btn_query 视为极验
    已过、表单已提交（submitted=True），html 随之切到 results_html；点结果条目按 target=_blank
    开新标签页（FakeAnchor）；详情页里点年报页签切换到 annual_html。
    """

    def __init__(self, pages=None, search_target=None, default='', registry=None,
                 has_search_box=False, results_html=None, detail_pages=None, detail_annual=None,
                 annual_html=None, cards_json='[]'):
        self.pages = pages or {}
        self.search_target = search_target
        self.visited = []
        self.closed = False
        self.current = default
        self.typed = []
        self.browser = registry
        self.has_search_box = has_search_box
        self.results_html = results_html
        self.detail_pages = detail_pages or {}   # 绝对 URL → 详情页 HTML
        self.detail_annual = detail_annual or {}  # 绝对 URL → 点年报页签后的 HTML
        self.annual_html = annual_html
        self.cards_json = cards_json  # 商业站点检索：CARDS_JS 的回放返回值
        self.submitted = False
        self.annual_open = False
        if registry is not None:
            registry.tabs.append(self)

    def get(self, url, timeout=None):
        self.visited.append(url)
        if url in self.pages:
            self.current = url

    @property
    def url(self):
        return self.current

    @property
    def html(self):
        if self.annual_open and self.annual_html is not None:
            return self.annual_html
        if self.submitted and self.results_html is not None:
            return self.results_html
        return self.pages.get(self.current, '')

    def ele(self, selector, timeout=None):
        if selector == 'css:#keyword':
            return FakeBox(self) if self.has_search_box else None
        if selector == 'css:#btn_query':
            return FakeQueryButton(self) if self.has_search_box else None
        if selector == 'css:[data-tag-name="annualreport"]':
            return FakeAnnualButton(self)
        if selector.startswith('css:a[href="') and selector.endswith('"]'):
            return FakeAnchor(self, selector[len('css:a[href="'):-2])
        if selector.startswith('css:input') and self.search_target:
            return FakeBox(self)
        return None

    def run_js(self, script, *args):
        return self.cards_json

    def close(self):
        self.closed = True


class FakeBox:
    def __init__(self, tab):
        self.tab = tab

    def attr(self, name):
        return 'keyword' if name == 'id' else None

    def input(self, text, clear=False):
        if text != '\n':
            self.tab.typed.append(text)
            if self.tab.search_target:
                self.tab.current = self.tab.search_target

    def click(self):
        pass


class FakeQueryButton:
    """点「查询」= 极验已过的等价物：表单提交，结果条目随 html 出现。"""

    def __init__(self, tab):
        self.tab = tab

    def click(self):
        self.tab.submitted = True


class FakeAnnualButton:
    """详情页的「企业年报信息」页签：点击后详情页 html 切到年报版式。"""

    def __init__(self, tab):
        self.tab = tab

    def click(self):
        self.tab.annual_open = True


class FakeAnchor:
    """点击结果条目：按 target=_blank 语义开新标签页，落在 detail_pages 里该 URL 的回放页上。"""

    def __init__(self, tab, href):
        self.tab = tab
        self.href = href

    def click(self):
        url = urljoin(str(self.tab.url or ''), self.href)
        FakeFlowTab(pages={url: self.tab.detail_pages[url]}, default=url,
                    registry=self.tab.browser, annual_html=self.tab.detail_annual.get(url))


class DeadTab(FakeFlowTab):
    """校验跳转中标签页可能失效：读取与关闭抛错都不该把异常抛进请求路径。"""

    @property
    def html(self):
        raise RuntimeError('tab is gone')

    def close(self):
        raise RuntimeError('tab is gone')


class FakeBrowser:
    """只提供 collectors 用到的 status / session_lock / _connection / start，全部走构造参数，不碰真浏览器。"""

    def __init__(self, installed=True, running=True, session_free=True, launch_ok=False):
        self.installed = installed
        self.running = running
        self.session_free = session_free
        self.launch_ok = launch_ok
        self.started = []
        self.locks = 0

    def status(self, data_dir=None):
        return {'installed': self.installed, 'running': self.running,
                'message': '会话可连接。' if self.running else '尚未启动项目 Chrome。'}

    def start(self, data_dir=None, source='boss'):
        """无会话时 collectors 会调 start 自动拉起：记录调用，按 launch_ok 决定是否拉起成功。"""
        self.started.append((data_dir, source))
        if self.launch_ok:
            self.running = True
            return {'installed': True, 'running': True, 'message': '项目 Chrome 已启动。'}
        return {'installed': True, 'running': False,
                'message': '项目 Chrome 启动失败；请关闭占用同一配置目录的浏览器后重试。'}

    @contextmanager
    def session_lock(self, data_dir=None):
        self.locks += 1
        yield self.session_free

    def _connection(self, data_dir=None):
        return 9333 if self.running else None


def drive(monkeypatch, tab=None, *, installed=True, running=True, session_free=True, tabbed=True,
          launch_ok=False):
    """把一次采集接到假 tab 上，返回 (假 tab, 假浏览器, 实际等待过的秒数)。

    CHALLENGE_WAIT 置 0：质询轮询在测试里直接返回当前页，真实等待路径由常量本身约束。
    """
    tab = tab if tab is not None else FakeFlowTab({})
    browser = FakeBrowser(installed=installed, running=running, session_free=session_free,
                          launch_ok=launch_ok)
    slept = []
    if tabbed:
        monkeypatch.setattr(collectors, '_open_tab', lambda browser_module, data_dir: tab)
    monkeypatch.setattr(collectors, '_sleep', lambda seconds: slept.append(seconds))
    monkeypatch.setattr(collectors, 'CHALLENGE_WAIT', 0)
    return tab, browser, slept


def insured_store(tmp_path, after=6000):
    repo = Repository(tmp_path)
    identity = repo.resolve_company(NAME)
    repo.save_signal(SignalInput(company_id=identity['id'], dimension='stability', rule='insured_headcount_trend',
                                 value={'before': 5000, 'after': after}, unit='人', source_class='official',
                                 evidence_id='E-1', observed_at='', collected_at=now()))
    return repo, identity


# ── 公示系统：检索（实名子系统 + 极验）→ 详情 → 年报 ────────────────────────────

def gsxt_replay(*, shell=GSXT_SHELL_HTML, results_html=GSXT_RESULTS_HTML,
                detail_pages=None, detail_annual=None, has_search_box=True):
    """一把梭构造「壳页 → 提交 → 结果 → 详情 → 年报」的回放标签页，返回 (登记表, 检索标签页)。"""
    registry = FakeTabs()
    tab = FakeFlowTab(pages={GSXT_SEARCH_URL: shell}, default=GSXT_SEARCH_URL, registry=registry,
                      has_search_box=has_search_box, results_html=results_html,
                      detail_pages={GSXT_DETAIL_URL: GSXT_DETAIL_HTML} if detail_pages is None else detail_pages,
                      detail_annual={GSXT_DETAIL_URL: GSXT_ANNUAL_HTML} if detail_annual is None else detail_annual)
    return registry, tab


def test_gsxt_full_pass_searches_submits_and_reads_the_detail_tab(monkeypatch):
    registry, tab = gsxt_replay()
    tab, browser, slept = drive(monkeypatch, tab)
    labels = []
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, on_status=labels.append)
    assert outcome['status'] == 'completed'
    assert [item['rule'] for item in outcome['signals']] == ['company_age', 'insured_headcount_trend']
    assert outcome['signals'][0]['value']['years'] >= 1
    assert outcome['signals'][0]['observed_at'] == '2015-04-01'
    assert outcome['signals'][1]['value'] == {'before': 312, 'after': 240}
    # 两类信号的出处都是详情页：成立日期与年报页签同在那把一次性加密地址下。
    assert [item['url'] for item in outcome['signals']] == [GSXT_DETAIL_URL, GSXT_DETAIL_URL]
    assert tab.visited == [GSXT_SEARCH_URL] and tab.typed == [NAME] and tab.submitted
    detail = next(item for item in registry.tabs if item is not tab)
    assert detail.annual_open is True and detail.closed is True
    assert labels == ['检索列表', '登记信息', '企业年报']
    # 新通路只有年报页签那一次固定停手。
    assert slept == [collectors.PAGE_DELAY]
    assert tab.closed is True


def test_gsxt_without_a_realname_session_asks_for_login(monkeypatch):
    """未实名登录：壳页没有检索框（登录态由结构判断，登录墙标记对 gsxt 一律关闭）。"""
    registry, tab = gsxt_replay(shell=LOGIN_HTML, has_search_box=False)
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert '需要登录' in outcome['message']
    assert tab.visited == [GSXT_SEARCH_URL] and tab.typed == [] and tab.closed is False


def test_gsxt_unsolved_captcha_leaves_the_page_in_the_foreground(monkeypatch):
    """点了「查询」但极验一直没人过：按需人工校验上报，弹层留在前台等用户完成后重试。"""
    registry, tab = gsxt_replay(results_html=None)
    tab, browser, slept = drive(monkeypatch, tab)
    monkeypatch.setattr(collectors, 'HUMAN_SOLVE_WAIT', 0.1)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    assert tab.submitted and tab.closed is False and '前台' in outcome['message']


def test_gsxt_without_exact_name_match_writes_a_debug_snapshot(monkeypatch, tmp_path):
    registry, tab = gsxt_replay(results_html=(
        '<a class="search_list_item db" target="_blank" href="/corp-query-detail-other.html">'
        '<h1 class="f20">乙公司</h1></a>'))
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '完全一致' in outcome['message'] and '共 1 条' in outcome['message']
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()
    assert tab.closed is True


def test_gsxt_slider_on_the_search_page_stops_and_stays_open(monkeypatch):
    registry, tab = gsxt_replay(shell=SLIDER_HTML + PAD, has_search_box=False)
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    assert tab.visited == [GSXT_SEARCH_URL] and tab.closed is False and slept == []


def test_gsxt_slider_on_the_detail_page_closes_it_and_keeps_search_open(monkeypatch):
    """详情页被滑块挡住：详情标签页收掉，检索页留前台交给用户处理。"""
    registry, tab = gsxt_replay(detail_pages={GSXT_DETAIL_URL: SLIDER_HTML + PAD})
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    detail = next(item for item in registry.tabs if item is not tab)
    assert detail.closed is True and tab.closed is False


def test_gsxt_detail_without_annual_numbers_keeps_the_age_only(monkeypatch, tmp_path):
    """2026-11-15 实测情形：年报页签的表是空的（接口 count:0）——成立日期照带，年报如实报未取到。"""
    registry, tab = gsxt_replay(detail_annual={})
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial'
    assert [item['rule'] for item in outcome['signals']] == ['company_age']
    assert '年报参保人数' in outcome['message']
    assert tab.closed is True


def test_gsxt_challenge_timeout_is_reported_with_a_snapshot(monkeypatch, tmp_path):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: 'Environment Checking'}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()


# 2026-11-14 实测回放：加速乐混淆壳页（快照 gsxt-last.html，可见文本 40 字符、全是 _0x 数组
# 混淆 JS、体积 19KB）。它没有 CHALLENGE_MARKERS 里的标志文字，必须按混淆特征归入「未就绪」。
JIASULE_SHELL = "<html><script>var _0x31b7=['ECTCs2s=','ZErCv8OT','dsKvZRk='];</script></html>"


class ResolvingTab(FakeFlowTab):
    """加速乐壳页自解：首次读到混淆壳，之后（cookie 落地、自动跳转）返回真实壳页。"""

    def __init__(self, pages, first_shell, **kwargs):
        super().__init__(pages=pages, **kwargs)
        self.first_shell = first_shell
        self.reads = 0

    @property
    def html(self):
        self.reads += 1
        return self.first_shell if self.reads == 1 else super().html


def test_a_jiasule_shell_is_polled_until_it_self_resolves(monkeypatch):
    registry = FakeTabs()
    tab = ResolvingTab({GSXT_SEARCH_URL: GSXT_SHELL_HTML}, JIASULE_SHELL, registry=registry,
                       has_search_box=True, results_html=GSXT_RESULTS_HTML,
                       detail_pages={GSXT_DETAIL_URL: GSXT_DETAIL_HTML},
                       detail_annual={GSXT_DETAIL_URL: GSXT_ANNUAL_HTML}, default=GSXT_SEARCH_URL)
    tab, browser, slept = drive(monkeypatch, tab)
    monkeypatch.setattr(collectors, 'CHALLENGE_WAIT', 5)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'completed' and len(outcome['signals']) == 2
    # 壳页没有被当成结果页放行：至少轮询到第二次读取才继续。
    assert tab.reads >= 2 and collectors.PAGE_DELAY in slept


def test_a_stuck_jiasule_shell_reports_the_challenge_timeout_with_a_hint(monkeypatch, tmp_path):
    """壳页不自解时带具体原因与白屏提示上报；不许糊弄成「检索结果里没有一致条目」。"""
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: JIASULE_SHELL}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '环境质询页未在时限内通过' in outcome['message'] and '白屏' in outcome['message']
    assert '完全一致' not in outcome['message']
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()


JIASULE_404 = ('<!DOCTYPE html><html><body><div class="main-content" id="notice_404">'
               '<iframe id="iframe_ad" src="//404.jiasule.com/static/html/404_ad_new.html">'
               '</iframe></div></body></html>')


def test_a_jiasule_404_interstitial_is_an_unfinished_read_not_empty_results(monkeypatch, tmp_path):
    """2026-11-14 实测回放：加速乐 404 插页 = 请求未被受理，不是「检索无结果」。"""
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: JIASULE_404}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '加速乐错误页' in outcome['message'] and '完全一致' not in outcome['message']
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()


# ── 商业站点公开页（企查查/天眼查/爱企查）：检索卡片 → 详情页 ────────────────────

CARD_HREF = 'https://www.qcc.com/firm/abc.html'
CARD = {'href': CARD_HREF, 'title': NAME,
        'text': '法定代表人：张三\n注册资本：100万元\n成立日期：2015-04-01\n经营状态：存续'}
DETAIL_HTML = ('<span>参保人数：240人</span><span>参保人数：312人</span>'
               '<a>司法案件 12</a>' + PAD)


def commercial_replay(source_id, cards, detail_pages=None):
    """按来源构造「搜索页（卡片回放）→ 公司详情页」的标签页，返回 (登记表, 标签页)。"""
    from jobscope import company_lookup
    spec = company_lookup.SITES[collectors.COMMERCIAL_SITES[source_id]]
    search_url = spec['search'](NAME)
    registry = FakeTabs()
    tab = FakeFlowTab(pages={search_url: '<div>search</div>', **(detail_pages or {})},
                      default=search_url, registry=registry,
                      cards_json=json.dumps(cards, ensure_ascii=False))
    return registry, tab


def quiet_lookup(monkeypatch):
    """company_lookup 的渲染等待是真实 time.sleep；测试里换成记录，不真等。
    详情区块轮询的时限一并置零：回放页缺关键词时不空转到 6 秒超时。"""
    from jobscope import company_lookup
    waited = []
    monkeypatch.setattr(company_lookup, '_sleep', lambda seconds: waited.append(seconds))
    monkeypatch.setattr(collectors, 'DETAIL_SECTION_WAIT', 0)
    return waited


# tyc_web 不在此列：它只认页面 JSON（见下方专项回放），通用标签配对不适用。
@pytest.mark.parametrize('source_id', ['qcc_web', 'aiqicha_web'])
def test_commercial_web_full_pass_collects_age_trend_and_case_count(monkeypatch, tmp_path, source_id):
    registry, tab = commercial_replay(source_id, [CARD], {CARD_HREF: DETAIL_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    lookup_waits = quiet_lookup(monkeypatch)
    repo, identity = insured_store(tmp_path)
    outcome = collectors.collect(source_id, NAME, browser_module=browser, repo=repo, identity=identity,
                                 data_dir=tmp_path)
    # 成立日期 + 参保趋势 + 涉诉计数×已存分母 → 三条信号全部落位。
    assert outcome['status'] == 'completed', outcome['message']
    rules = {item['rule']: item['value'] for item in outcome['signals']}
    assert rules['company_age']['years'] >= 1
    assert rules['insured_headcount_trend'] == {'before': 312, 'after': 240}
    assert rules['judicial_case_ratio']['cases'] == 12 and rules['judicial_case_ratio']['insured'] == 6000
    assert [item['url'] for item in outcome['signals']] == [CARD_HREF, CARD_HREF, CARD_HREF]
    assert tab.closed is True and lookup_waits  # 卡片与详情两次渲染等待都真实发生（这里只记录）


def test_commercial_web_without_a_stored_denominator_keeps_the_count_honest(monkeypatch, tmp_path):
    """涉诉计数没有参保分母就不落库（防止顶掉已出分的比率信号），其余照常。"""
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: DETAIL_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    repo = Repository(tmp_path)
    identity = repo.resolve_company(NAME)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, repo=repo, identity=identity,
                                 data_dir=tmp_path)
    assert outcome['status'] == 'partial' and '缺少参保人数分母' in outcome['message']
    assert {item['rule'] for item in outcome['signals']} == {'company_age', 'insured_headcount_trend'}


def test_commercial_web_blocked_search_stays_in_the_foreground(monkeypatch):
    """一张卡片都没有且页面摆出登录/验证字样：needs_verification，页面留前台。"""
    registry, tab = commercial_replay('tyc_web', [])
    tab.pages = {tab.current: '<div>请登录后查看</div>'}
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('tyc_web', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    assert tab.closed is False


def test_commercial_web_without_an_exact_match_writes_a_snapshot(monkeypatch, tmp_path):
    other = {'href': 'https://www.qcc.com/firm/other.html', 'title': '乙公司',
             'text': '法定代表人：李四\n成立日期：2001-01-01'}
    registry, tab = commercial_replay('qcc_web', [other])
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '一致的条目' in outcome['message'] and '共 1 条' in outcome['message']
    assert (tmp_path / 'debug' / 'qcc_web-last.html').exists()


def test_commercial_web_detail_without_numbers_reports_nothing_found(monkeypatch, tmp_path):
    """详情页数字都被遮蔽（未登录常见）：如实报「未见可登记字段」，不硬凑。"""
    registry, tab = commercial_replay('aiqicha_web', [CARD],
                                      {CARD_HREF: '<span>成立日期由卡片登记</span>' + PAD})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    monkeypatch.setattr(collectors, 'DETAIL_SECTION_WAIT', 0)  # 没有关键区块：不空转等超时
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    # 卡片里有成立日期 → 至少一条；详情页没有参保/涉诉数字 → 不产趋势与计数。
    assert outcome['status'] == 'completed' and len(outcome['signals']) == 1
    assert outcome['signals'][0]['rule'] == 'company_age'


def test_commercial_flows_are_registered_for_every_declared_site(monkeypatch):
    assert set(collectors.COMMERCIAL_SITES) == {'qcc_web', 'tyc_web', 'aiqicha_web'}
    assert all(source in collectors.FLOWS for source in collectors.COMMERCIAL_SITES)


# 2026-11-15 实测事故回放：天眼查渲染文本里「参保人数 51843」与「2025 年报」去标签后粘连成
# 518432025，分支机构参保 24028 又被当成主体数，两家公司的参保趋势全成了脏数据。
TYC_DETAIL_HTML = ('<span>参保人数518432025年报</span>'
                   '<script>{"text":"参保人数综合计算得出。","num":51843,"branchNum":24028,"year":2025}</script>'
                   '<script>{"text":"同上","num":58435,"branchNum":21782,"year":2024}</script>' + PAD)


def test_tyc_insured_numbers_come_from_the_page_json_not_glued_text(monkeypatch, tmp_path):
    """tyc 参保趋势只认主体 num（按年份取最近两期），粘连文本与 branchNum 一律不进。"""
    registry, tab = commercial_replay('tyc_web', [CARD], {CARD_HREF: TYC_DETAIL_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('tyc_web', NAME, browser_module=browser, data_dir=tmp_path)
    trend = next(item for item in outcome['signals'] if item['rule'] == 'insured_headcount_trend')
    assert trend['value'] == {'before': 58435, 'after': 51843}


def test_insured_numbers_outside_human_range_are_rejected(monkeypatch, tmp_path):
    """范围闸门：去标签后粘连出的亿级大数不是参保人数，宁可不出趋势也不落库。"""
    glued = '<span>参保人数7612025年报</span><span>参保人数0</span>' + PAD
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: glued})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert all(item['rule'] != 'insured_headcount_trend' for item in outcome['signals'])
    assert outcome['signals'][0]['rule'] == 'company_age'


def test_commercial_web_classifies_soe_from_the_card_field(monkeypatch, tmp_path):
    """国企性质（2026-11-15）：卡片「企业类型」字段命中关键词 → flag=1，原文进证据。"""
    soe_card = {'href': CARD_HREF, 'title': NAME,
                'text': '法定代表人：张三\n成立日期：2015-04-01\n企业类型：有限责任公司（国有控股）'}
    registry, tab = commercial_replay('qcc_web', [soe_card])
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    soe = next(item for item in outcome['signals'] if item['rule'] == 'soe_status')
    assert soe['value'] == {'flag': 1} and '国有控股' in soe['text']


def test_commercial_web_classifies_non_soe_from_the_detail_page(monkeypatch, tmp_path):
    """详情页「企业类型」明确为非国有类型 → flag=0（明确登记，同样是一条信号）。"""
    detail = '<span>企业类型：有限责任公司（自然人投资或控股）</span>' + PAD
    registry, tab = commercial_replay('tyc_web', [CARD], {CARD_HREF: detail})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('tyc_web', NAME, browser_module=browser, data_dir=tmp_path)
    soe = next(item for item in outcome['signals'] if item['rule'] == 'soe_status')
    assert soe['value'] == {'flag': 0} and '非国有' in soe['text']


def test_commercial_web_without_entity_kind_emits_no_soe_signal(monkeypatch, tmp_path):
    """没有「企业类型」字段就不判定：宁缺勿猜，不拿页面新闻字样当性质依据。"""
    registry, tab = commercial_replay('aiqicha_web', [CARD])
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert all(item['rule'] != 'soe_status' for item in outcome['signals'])


# 2026-11-15 实测回放：爱企查年报页（/annualreport?pid=..&year=YYYY）逐年公示社保参保数
# （恒公示），从业人数/营收企业可选不公示（华为全不公示）。两期同一口径成立才出同比。
AQC_ANNUAL_2024 = ('<title>2024年报</title>从业人数 不公示 社保信息 城镇职工基本养老保险58422人 '
                   '失业保险58425人 主营业务收入 不公示' + PAD)
AQC_ANNUAL_2023 = ('<title>2023年报</title>从业人数 不公示 社保信息 城镇职工基本养老保险61341人 '
                   '失业保险61339人 主营业务收入 不公示' + PAD)
AQC_DETAIL_WITH_ANNUAL = ('<span>成立日期：2015-04-01</span>'
                          '<a href="/annualreport?pid=1&amp;year=2024">2024年度报告</a>'
                          '<a href="/annualreport?pid=1&amp;year=2023">2023年度报告</a>' + PAD)


def aqc_annual_replay(detail_html=AQC_DETAIL_WITH_ANNUAL, pages_2024=AQC_ANNUAL_2024, pages_2023=AQC_ANNUAL_2023):
    return commercial_replay('aiqicha_web', [CARD], {
        CARD_HREF: detail_html,
        'https://aiqicha.baidu.com/annualreport?pid=1&year=2024': pages_2024,
        'https://aiqicha.baidu.com/annualreport?pid=1&year=2023': pages_2023,
    })


def test_aiqicha_annual_report_feeds_the_operations_dimension(monkeypatch, tmp_path):
    """华为实测回放：从业人数不公示 → 社保养老参保数作代理口径，出年报人数同比。"""
    registry, tab = aqc_annual_replay()
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    headcount = next(item for item in outcome['signals'] if item['rule'] == 'disclosure_headcount_trend')
    assert headcount['value'] == {'current': 58422, 'previous': 61341}
    assert '养老保险参保数' in headcount['text'] and '2023→2024' in headcount['text']
    # 营收不公示 → 不出营收同比，也不许拿别处数字凑。
    assert all(item['rule'] != 'disclosure_revenue_trend' for item in outcome['signals'])


def test_aiqicha_insured_trend_prefers_annual_pension_over_glued_page_text(monkeypatch, tmp_path):
    """公司页参保文本与年份粘连（24033+2024 同款事故）：有年报就改用逐年养老参保数。"""
    glued_detail = ('<span>参保人数24033 2024年年报</span>'
                    '<a href="/annualreport?pid=1&amp;year=2024">2024年度报告</a>'
                    '<a href="/annualreport?pid=1&amp;year=2023">2023年度报告</a>' + PAD)
    registry, tab = aqc_annual_replay(detail_html=glued_detail)
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    insured = next(item for item in outcome['signals'] if item['rule'] == 'insured_headcount_trend')
    assert insured['value'] == {'before': 61341, 'after': 58422}
    assert '年报序列' in insured['text']


def test_aiqicha_annual_report_uses_staff_counts_and_revenue_when_disclosed(monkeypatch, tmp_path):
    """从业人数与营收都公示时优先直取（不再用社保代理），两条同比都落。"""
    y2024 = '从业人数 12000人 社保信息 城镇职工基本养老保险11800人 主营业务收入 3.2亿' + PAD
    y2023 = '从业人数 10000人 社保信息 城镇职工基本养老保险9800人 主营业务收入 28000万' + PAD
    registry, tab = aqc_annual_replay(pages_2024=y2024, pages_2023=y2023)
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    headcount = next(item for item in outcome['signals'] if item['rule'] == 'disclosure_headcount_trend')
    revenue = next(item for item in outcome['signals'] if item['rule'] == 'disclosure_revenue_trend')
    assert headcount['value'] == {'current': 12000, 'previous': 10000}
    assert '从业人数' in headcount['text']
    assert revenue['value'] == {'current': 320000000.0, 'previous': 280000000.0}


def test_aiqicha_without_two_annual_reports_skips_the_trend(monkeypatch, tmp_path):
    """新公司不足两期年报：如实跳过同比，不猜。"""
    detail = '<span>成立日期：2026-01-14</span><a href="/annualreport?pid=1&amp;year=2026">2026年度报告</a>' + PAD
    registry, tab = commercial_replay('aiqicha_web', [CARD], {CARD_HREF: detail,
                                      'https://aiqicha.baidu.com/annualreport?pid=1&year=2026': AQC_ANNUAL_2024})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert all(item['rule'] not in ('disclosure_headcount_trend', 'disclosure_revenue_trend')
               for item in outcome['signals'])


# 2026-11-15 实测回放：爱企查/企查查卡片「企业类型」后跟的是变更流水（「变更查看动态…」
# 「从X变更为Y」），没有结构词的值是脏数据；天眼查 JSON 的 companyOrgType 才是干净口径。
DIRTY_TYPE_HTML = ('<span>企业类型 变更查看动态2026-09-10新增</span>' + PAD)
TYC_TYPE_DETAIL = ('<script>{"companyOrgType":"有限责任公司（法人独资）","scale":"大型"}</script>'
                   '<span>人员规模10000人以上</span>' + PAD)


def test_dirty_entity_type_is_discarded_not_guessed(monkeypatch, tmp_path):
    """卡片/文本里的企业类型值没有结构词（全是变更流水）→ 不判定，宁缺勿猜。"""
    dirty_card = {'href': CARD_HREF, 'title': NAME, 'fields': {'企业类型': '态2026-09-10新增'},
                  'text': '企业类型：态2026-09-10新增'}
    registry, tab = commercial_replay('aiqicha_web', [dirty_card], {CARD_HREF: DIRTY_TYPE_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert all(item['rule'] != 'soe_status' for item in outcome['signals'])


def test_tyc_org_type_comes_from_the_page_json_and_staff_range_becomes_a_statement(monkeypatch, tmp_path):
    """天眼查：企业类型优先取 JSON（干净），人员规模文本行 → team_size_statement 原始形态。"""
    registry, tab = commercial_replay('tyc_web', [CARD], {CARD_HREF: TYC_TYPE_DETAIL})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('tyc_web', NAME, browser_module=browser, data_dir=tmp_path)
    soe = next(item for item in outcome['signals'] if item['rule'] == 'soe_status')
    assert soe['value'] == {'flag': 0} and '法人独资' in soe['text']
    staff = next(item for item in outcome['signals'] if item['rule'] == 'team_size_statement')
    assert staff['value'] == {'size': 10000}
    assert staff['source_class'] == 'official' and '人员规模' in staff['text']


def test_staff_range_interval_keeps_both_ends(monkeypatch, tmp_path):
    """区间口径「100-499人」：size 取下界、size_max 保留，证据写明区间。"""
    detail = '<span>人员规模100-499人</span><span>企业类型：有限责任公司（自然人投资或控股）</span>' + PAD
    registry, tab = commercial_replay('tyc_web', [CARD], {CARD_HREF: detail})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('tyc_web', NAME, browser_module=browser, data_dir=tmp_path)
    staff = next(item for item in outcome['signals'] if item['rule'] == 'team_size_statement')
    assert staff['value'] == {'size': 100, 'size_max': 499}


def test_aiqicha_cards_come_from_the_pid_extraction_and_founded_uses_both_labels(monkeypatch):
    """2026-11-15 实测校准：爱企查卡片无 href（pid 在 data-log-title 里），标签用「成立时间」。"""
    from jobscope import company_lookup
    spec = company_lookup.SITES['aiqicha']
    assert 'data-log-title' in spec['cards_js'] and 'link_pattern' not in spec
    raw_card = {'href': CARD_HREF, 'title': NAME,
                'text': '法定代表人：张鹏欣\n成立时间：2026-01-14\n注册资本：100万(元)'}
    card = company_lookup._parse_card(raw_card)
    assert card['fields']['成立时间'] == '2026-01-14'
    # 采集侧对两个标签同一口径：成立时间同样换算 company_age。
    registry, tab = commercial_replay('aiqicha_web', [raw_card])
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    monkeypatch.setattr(collectors, 'DETAIL_SECTION_WAIT', 0)  # 无详情页回放：不空转等超时
    outcome = collectors.collect('aiqicha_web', NAME, browser_module=browser)
    age = next(item for item in outcome['signals'] if item['rule'] == 'company_age')
    assert age['observed_at'] == '2026-01-14' and age['value']['years'] >= 0


# ── 信用中国：登录墙 + 失信计数 × 已存参保分母 ─────────────────────────────────

def test_credit_china_login_wall_stays_open(monkeypatch):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({CREDIT_SEARCH_URL: '<html>系统登录 验证码</html>'}))
    outcome = collectors.collect('credit_china', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert tab.closed is False and '登录' in outcome['message']


def test_credit_china_merges_the_stored_insured_denominator(monkeypatch, tmp_path):
    repo, identity = insured_store(tmp_path)
    pages = {CREDIT_SEARCH_URL: f'<div>{NAME}</div><span>失信被执行人 12 条</span>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('credit_china', NAME, browser_module=browser, repo=repo, identity=identity)
    assert outcome['status'] == 'completed' and len(outcome['signals']) == 1
    signal = outcome['signals'][0]
    assert signal['value'] == {'ratio': 20.0, 'cases': 12, 'insured': 6000}
    assert '分母取已存参保信号' in signal['text']
    assert signal['url'] == CREDIT_SEARCH_URL and signal['source_class'] == 'official'
    assert tab.closed is True


def test_credit_china_without_a_denominator_drops_the_signal_and_explains(monkeypatch, tmp_path):
    repo = Repository(tmp_path)
    identity = repo.resolve_company(NAME)
    pages = {CREDIT_SEARCH_URL: f'<div>{NAME}</div><span>失信被执行人 12 条</span>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('credit_china', NAME, browser_module=browser, repo=repo, identity=identity)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '缺少参保人数分母' in outcome['message']


def test_credit_china_zero_records_still_lands_with_a_stored_denominator(monkeypatch, tmp_path):
    repo, identity = insured_store(tmp_path)
    pages = {CREDIT_SEARCH_URL: f'<div>{NAME}</div><p>没有找到相关记录</p>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('credit_china', NAME, browser_module=browser, repo=repo, identity=identity)
    assert outcome['status'] == 'completed'
    assert outcome['signals'][0]['value']['ratio'] == 0


def test_credit_china_unparsable_page_is_partial_with_snapshot(monkeypatch, tmp_path):
    repo, identity = insured_store(tmp_path)
    pages = {CREDIT_SEARCH_URL: f'<div>{NAME}</div><p>其它内容</p>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('credit_china', NAME, browser_module=browser, repo=repo, identity=identity,
                                 data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert (tmp_path / 'debug' / 'credit_china-last.html').exists()


# ── 裁判文书网：登录墙 + 文书计数 ───────────────────────────────────────────────

WENSHU_SHELL = '<input type="text" placeholder="检索"><div>文书列表</div>'
WENSHU_RESULTS = '<p>共 1,234 篇文书</p>'
WENSHU_LOGIN = '<div>请登录后查看检索结果</div>'


def test_court_records_login_wall_after_search(monkeypatch):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(
        {collectors.WENSHU_URL: WENSHU_SHELL, 'results': WENSHU_LOGIN}, search_target='results'))
    outcome = collectors.collect('court_records', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert tab.closed is False and tab.typed == [NAME]


def test_court_records_counts_documents_and_merges_denominator(monkeypatch, tmp_path):
    repo, identity = insured_store(tmp_path)
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(
        {collectors.WENSHU_URL: WENSHU_SHELL, 'results': WENSHU_RESULTS}, search_target='results'))
    outcome = collectors.collect('court_records', NAME, browser_module=browser, repo=repo, identity=identity)
    assert outcome['status'] == 'completed'
    signal = outcome['signals'][0]
    assert signal['value']['cases'] == 1234
    assert signal['value']['ratio'] == round(1234 * collectors.PER_WAN / 6000, 2)
    assert '分母取已存参保信号' in signal['text']


def test_court_records_without_a_search_box_writes_a_snapshot(monkeypatch, tmp_path):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({collectors.WENSHU_URL: WENSHU_SHELL}))
    outcome = collectors.collect('court_records', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '检索框' in outcome['message']
    assert (tmp_path / 'debug' / 'court_records-last.html').exists()


# ── 通用闸门：会话状态、未知来源、失效标签页 ────────────────────────────────────

def test_a_launch_failure_is_reported_with_the_start_result(monkeypatch):
    """无会话时先自动拉起；拉不起来就按 start() 的结果提示，不开标签页、不进会话锁、不装作采集过。"""
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: GSXT_SHELL_HTML}),
                                running=False, tabbed=False)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert '启动失败' in outcome['message']
    assert tab.visited == [] and browser.locks == 0 and slept == []
    assert browser.started == [(None, 'gsxt')]


def test_a_missing_session_is_auto_launched_and_the_collection_proceeds(monkeypatch):
    """自动拉起成功后照常采集：与岗位采集入口同一行为，用户不必先去别处手动启动浏览器。"""
    registry, tab = gsxt_replay()
    tab, browser, slept = drive(monkeypatch, tab, running=False, launch_ok=True)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir='wherever')
    assert outcome['status'] == 'completed' and len(outcome['signals']) == 2
    assert browser.started == [('wherever', 'gsxt')] and browser.locks == 1


def test_an_occupied_session_backs_off_instead_of_queueing(monkeypatch):
    tab, browser, slept = drive(monkeypatch, session_free=False)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'backoff' and outcome['signals'] == []
    assert tab.visited == [] and tab.closed is False


def test_a_source_without_a_chrome_dependency_is_not_collected(monkeypatch):
    tab, browser, slept = drive(monkeypatch, installed=False, tabbed=False)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'unsupported' and tab.visited == []
    assert browser.locks == 0


def test_unknown_and_paid_sources_are_unsupported(monkeypatch):
    tab, browser, slept = drive(monkeypatch)
    for source in ('qcc', 'nope'):
        outcome = collectors.collect(source, NAME, browser_module=browser)
        assert outcome['status'] == 'unsupported' and outcome['signals'] == []
    assert tab.visited == [] and browser.locks == 0


def test_a_tab_that_dies_mid_collection_is_reported_not_raised(monkeypatch):
    """标签页失效（跳转/被回收）时只退化状态，异常不该冒出到端点那一层。"""
    tab, browser, slept = drive(monkeypatch, tab=DeadTab({GSXT_SEARCH_URL: ''}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert len(tab.visited) == 1


def test_collection_is_bounded_by_pages_and_never_replays_requests():
    assert collectors.MAX_PAGES <= 5 and collectors.PAGE_DELAY >= 2


def test_signals_feed_the_stored_signal_contract(monkeypatch):
    """采集结果的字段名必须原样喂进 SignalInput，端点才不会拼不出信号。"""
    registry, tab = gsxt_replay()
    tab, browser, slept = drive(monkeypatch, tab)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    for item in outcome['signals']:
        signal = SignalInput(company_id='CO-0123456789abcdef', evidence_id='E-1',
                             collected_at='2026-11-09T00:00:00+00:00',
                             **{key: item[key] for key in
                                ('rule', 'dimension', 'value', 'unit', 'source_class', 'observed_at')})
        assert signal.rule == item['rule']


class ForbiddenBrowser:
    """本地来源的哨兵：碰一下浏览器模块就立刻失败，连属性名都不许读。"""

    def __getattr__(self, name):
        raise AssertionError(f'本地来源不该触碰浏览器模块：{name}')


def store(tmp_path, jobs=(), materials=(), company='甲公司'):
    """按现有仓储契约塞几条已存数据，让本地来源有东西可算。"""
    repo = Repository(tmp_path)
    for index, salary in enumerate(jobs):
        repo.save_job(dict(title=f'岗位{index}', company=company, description='负责服务端开发',
                           location='北京', salary=salary, url=f'https://jobs.example.com/{index}'), 'test_jobs')
    for index, kind in enumerate(materials):
        repo.save_evidence(dict(title=f'材料{index}', company=company, kind=kind,
                                text='2024 年度报告：参保人数由 312 人变为 240 人'), 'user_import')
    repo.save_job(dict(title='别家的岗位', company='乙公司', description='负责别的', location='上海',
                       salary='30k-50k', url='https://jobs.example.com/other'), 'test_jobs')
    return repo, repo.resolve_company(company)


def test_the_local_reader_counts_saved_jobs_and_their_parsable_salary_quotes(tmp_path):
    # 三条不计入已知的报价各走一条 parse_salary 的退出口径：年薪、外币、时薪。
    repo, identity = store(tmp_path, jobs=['20-35K'] * 9 + ['3-5万/年', 'USD 4-6k', '150-200元/小时'])
    outcome = local.collect('jobscope_jobs', identity['name'], repo=repo, identity=identity)
    assert outcome['status'] == 'completed'
    coverage = next(item for item in outcome['signals'] if item['rule'] == 'salary_quote_coverage')
    # 年薪、时薪与外币报价在 parse_salary 里 currency 不是 CNY，按口径不计入已知。
    assert coverage['value'] == {'known': 9, 'total': 12}
    assert coverage['dimension'] == 'pay' and coverage['unit'] == '%'


def test_the_local_reader_is_honest_about_being_local(tmp_path):
    repo, identity = store(tmp_path, jobs=['20-35K'])
    outcome = local.collect('jobscope_jobs', identity['name'], repo=repo, identity=identity)
    assert '由本项目已保存数据计算' in outcome['message'] and '不访问网络' in outcome['message']
    for item in outcome['signals']:
        # platform 类、没有外部出处、没有观察时间：这三样凑在一起才是「不是外部来源」的诚实标记。
        assert item['source_class'] == 'platform'
        assert item['url'] == '' and item['observed_at'] == ''
        assert item['collected_at'] and item['source'] == 'jobscope_jobs'


def test_a_company_with_nothing_stored_computes_zeros_instead_of_nothing(tmp_path):
    repo = Repository(tmp_path)
    identity = repo.resolve_company('甲公司')
    outcome = local.collect('jobscope_jobs', identity['name'], repo=repo, identity=identity)
    assert outcome['status'] == 'completed'
    # 没有岗位就没有 R1/R2 的统计量（pay/jd 不产出信号）；材料计数类照常出全零。
    assert {item['rule']: item['value'] for item in outcome['signals']} == {
        'salary_quote_coverage': {'known': 0, 'total': 0}, 'disclosure_available': {'count': 0},
        'industry_media_count': {'count': 0}}


def test_the_local_reader_counts_only_announcement_like_evidence(tmp_path):
    repo, identity = store(tmp_path, materials=['disclosure', 'disclosure_metadata', 'announcement',
                                                'employee_statement', 'media_report', 'user_material'])
    disclosure = next(item for item in local.collect('jobscope_jobs', identity['name'], repo=repo,
                                                     identity=identity)['signals']
                      if item['rule'] == 'disclosure_available')
    # 三条公告类证据；岗位报价的证据是 job_posting 类，同样不算公告。
    assert disclosure['value'] == {'count': 3}
    assert disclosure['dimension'] == 'operations' and disclosure['unit'] == '份'


def test_the_industry_counter_only_takes_media_and_announcement_kinds(tmp_path):
    # R6 是 industry 维度唯一的材料登记：公司自己的披露（disclosure）不算行业媒体面。
    repo, identity = store(tmp_path, materials=['media_report', 'announcement', 'announcement', 'disclosure'])
    media = next(item for item in local.collect('jobscope_jobs', identity['name'], repo=repo,
                                                identity=identity)['signals']
                 if item['rule'] == 'industry_media_count')
    assert media['value'] == {'count': 3}
    assert media['dimension'] == 'industry' and media['source_class'] == 'platform'


def test_the_pay_rule_carries_both_sides_of_the_city_sample(tmp_path):
    # 甲 4 条北京「技术·3-5年·本科」报价，只与同城同层对比；乙的岗位在上海，不进北京样本——
    # 城市归一宁可漏并不可错并，分层标注缺失的岗位同样不硬算。
    repo = Repository(tmp_path)
    desc = '负责服务端开发\n经验：3-5年\n学历：本科'
    for index in range(4):
        repo.save_job(dict(title=f'后端工程师{index}', company='甲公司', description=desc,
                           location='北京', salary='20-35K', url=f'https://jobs.example.com/{index}'), 'test_jobs')
    repo.save_job(dict(title='别家的岗位', company='乙公司', description=desc, location='上海',
                       salary='30k-50k', url='https://jobs.example.com/other'), 'test_jobs')
    # 12 条同城同层对照：过统计层的 ≥10 闸门，但合计 <20，rubric 的闸门照样拦——计数如实落库。
    for index in range(12):
        repo.save_job(dict(title=f'对照工程师{index}', company='乙公司', description=desc,
                           location='北京', salary='20-35K', url=f'https://jobs.example.com/c{index}'), 'test_jobs')
    identity = repo.resolve_company('甲公司')
    outcome = local.collect('jobscope_jobs', identity['name'], repo=repo, identity=identity)
    pay = next(item for item in outcome['signals'] if item['rule'] == 'pay_vs_city_median')
    assert pay['dimension'] == 'pay' and pay['unit'] == '元'
    assert pay['value']['company_jobs'] == 4 and pay['value']['city_jobs'] == 16
    assert pay['value']['company_median'] == 27500.0 and pay['value']['ratio'] == 1.0
    # 样本闸门不在采集层判：计数如实落库，出不出分由 rubric 说清原因。
    assert pay['value']['city_jobs'] < 20


def test_the_local_reader_never_touches_the_browser_module(tmp_path):
    repo, identity = store(tmp_path, jobs=['20-35K'])
    outcome = local.collect('jobscope_jobs', identity['name'], repo=repo, identity=identity,
                            browser_module=ForbiddenBrowser(), data_dir=tmp_path)
    # 覆盖度两条 + 同城报价对比 + 描述具体程度 + 行业媒体计数：全部纯本地，浏览器模块碰都不许碰。
    assert outcome['status'] == 'completed' and len(outcome['signals']) == 5
    assert {item['rule'] for item in outcome['signals']} == {
        'salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median', 'jd_specificity',
        'industry_media_count'}


def test_a_source_without_a_local_reader_is_still_reported_as_unsupported(tmp_path):
    repo, identity = store(tmp_path, jobs=['20-35K'])
    for source in ('gsxt', 'credit_china', 'qcc'):
        outcome = local.collect(source, identity['name'], repo=repo, identity=identity)
        assert outcome['status'] == 'unsupported' and outcome['signals'] == []


# 2026-11-17 企查查真实页回放（海康威视详情页）：参保区是「参保人数 11616 (2025年报)
# 分支机构参保人数 5510 (2025年报)」——主体+分支机构同年两个数，旧逻辑把它们配成
# before=5510/after=11616 的假趋势；风险区块首格「司法案件 361」，另有「历史裁判文书 64」
# 等历史口径行。三条回放钉住修正后的口径。
QCC_YEAR_HTML = ('<span>纳税人资质 一般纳税人 参保人数 11616 (2025年报) '
                 '分支机构参保人数 5510 (2025年报)</span>'
                 '<a>司法案件 361</a><span>失信被执行人 0 被执行人 0 终本案件 0 '
                 '历史裁判文书 64 历史开庭公告 1</span>' + PAD)


def test_qcc_same_year_main_and_branch_counts_are_not_a_trend(monkeypatch, tmp_path):
    """同年主体+分支两个参保数：不出趋势；涉诉计数就地用同页主体参保作分母落库。"""
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: QCC_YEAR_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    repo = Repository(tmp_path)
    identity = repo.resolve_company(NAME)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, repo=repo, identity=identity,
                                 data_dir=tmp_path)
    rules = {item['rule']: item for item in outcome['signals']}
    assert 'insured_headcount_trend' not in rules  # 同年两个数不构成趋势
    ratio_signal = rules['judicial_case_ratio']
    # 361 × 10000 ÷ 11616 ≈ 310.78；分母是同页主体口径，不是分支 5510。
    assert ratio_signal['value'] == {'ratio': 310.78, 'cases': 361, 'insured': 11616}
    assert '同页参保人数' in ratio_signal['text']
    assert '单期' in outcome['message']
    assert not any(item.get('needs_denominator') for item in outcome['signals'])


def test_qcc_year_tagged_insured_builds_trend_by_year(monkeypatch, tmp_path):
    """两个年份的主体参保：按年份由新到旧出趋势，口径说明写「页面带年报年份标注」。"""
    html = ('<span>参保人数 12000 (2025年报)</span><span>参保人数 11000 (2024年报)</span>'
            '<a>司法案件 12</a>' + PAD)
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: html})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    repo, identity = insured_store(tmp_path)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, repo=repo, identity=identity,
                                 data_dir=tmp_path)
    rules = {item['rule']: item['value'] for item in outcome['signals']}
    assert rules['insured_headcount_trend'] == {'before': 11000, 'after': 12000}
    trend = next(item for item in outcome['signals'] if item['rule'] == 'insured_headcount_trend')
    assert '页面带年报年份标注' in trend['text']


def test_historical_case_rows_never_pass_for_current_counts(monkeypatch, tmp_path):
    """「历史裁判文书 64」不进现行计数：优先「司法案件 N」，其次非历史的裁判文书/开庭公告。"""
    assert collectors._judicial_cases('司法案件 361 裁判文书 285')[1] == '361'
    assert collectors._judicial_cases('失信被执行人 0 裁判文书 285 立案信息 205')[1] == '285'
    assert collectors._judicial_cases('历史裁判文书 64 裁判文书 285')[1] == '285'
    assert collectors._judicial_cases('历史开庭公告 1 历史裁判文书 1') is None
    html = ('<span>参保人数 11616 (2025年报) 分支机构参保人数 5510 (2025年报)</span>'
            '<span>失信被执行人 0 历史裁判文书 64 历史开庭公告 1</span>' + PAD)
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: html})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    # 页面只有历史口径的涉诉行：不落任何涉诉信号，参保单期只进消息。
    assert all(item['rule'] != 'judicial_case_ratio' for item in outcome['signals'])
    assert '单期' in outcome['message']


# rubric-9 字段扩展回放（qcc 2026-11-17 实测）：登记状态行 + 风险区格四行 + 行政处罚行。
QCC_FULL_HTML = ('<span>登记状态 存续 成立日期 2001-11-30</span>\n'
                 '<span>参保人数 11616 (2025年报) 分支机构参保人数 5510 (2025年报)</span>\n'
                 '<a>司法案件 361</a><span>失信被执行人 0 被执行人 0 限制高消费 0 终本案件 0 '
                 '裁判文书 285 立案信息 205 开庭公告 307 法院公告 45 送达公告 8 '
                 '强制清算与破产案件 0 股权冻结 0 司法拍卖 1 '
                 '行政处罚 0 经营异常 0 严重违法 0 环保处罚 0 税务非正常户 0 欠税公告 0 '
                 '历史裁判文书 64 历史行政处罚 3 历史经营异常 1 历史被执行人 7</span>' + PAD)


def test_qcc_credit_risk_registration_and_penalty_from_risk_grid(monkeypatch, tmp_path):
    """登记状态/信用风险/行政处罚三条新信号：正常状态 flag=1，四行全零 flag=0，
    历史行（历史行政处罚 3 等）不参与任何现行计数。"""
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: QCC_FULL_HTML})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    rules = {item['rule']: item['value'] for item in outcome['signals']}
    assert rules['registration_status'] == {'flag': 1}
    assert rules['credit_risk'] == {'flag': 0}
    assert rules['admin_penalty'] == {'count': 0}
    assert 'insured_headcount_trend' not in rules  # 单期参保不出趋势（既有口径）
    assert '登记状态' in outcome['message'] and '信用风险名单' in outcome['message']


def test_qcc_credit_risk_requires_all_four_rows_visible(monkeypatch, tmp_path):
    """四行缺任何一行＝覆盖不全：不出 credit_risk，其余照常。"""
    partial = ('<span>登记状态 存续</span>\n'
               '<span>参保人数 11616 (2025年报) 分支机构参保人数 5510 (2025年报)</span>\n'
               '<span>失信被执行人 0 被执行人 0 经营异常 0</span>' + PAD)  # 严重违法行缺失
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: partial})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    assert all(item['rule'] != 'credit_risk' for item in outcome['signals'])
    assert any(item['rule'] == 'registration_status' for item in outcome['signals'])


def test_qcc_credit_risk_flags_on_any_hit_and_closed_status_scores_low(monkeypatch, tmp_path):
    """任一名单命中 → flag=1；登记状态为吊销 → registration_status flag=0（终止经营）。"""
    html = ('<span>登记状态 吊销</span>\n'
            '<span>参保人数 500 (2024年报) 分支机构参保人数 120 (2024年报)</span>\n'
            '<span>失信被执行人 2 被执行人 9 经营异常 1 严重违法 0 '
            '行政处罚 5 历史行政处罚 9</span>' + PAD)
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: html})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    rules = {item['rule']: item['value'] for item in outcome['signals']}
    assert rules['registration_status'] == {'flag': 0}
    assert rules['credit_risk'] == {'flag': 1}
    assert rules['admin_penalty'] == {'count': 5}  # 历史行政处罚 9 不参与
    from jobscope.reliability import rubric
    assert rubric.RULES['credit_risk'].scorer({'flag': 1}) == 0.1
    assert rubric.RULES['credit_risk'].scorer({'flag': 0}) == 0.85
    assert rubric.RULES['admin_penalty'].scorer({'count': 0}) == 0.8
    assert rubric.RULES['admin_penalty'].scorer({'count': 12}) == 0.25
    assert rubric.RULES['registration_status'].scorer({'flag': 0}) == 0.1


def test_qcc_registration_status_survives_glued_insured_field(monkeypatch, tmp_path):
    """回归：登记状态与参保字段去标签后粘连（「存续参保人数11616」）时，截断词表必须
    含「参保人数」，否则捕获值判定失败、登记状态信号静默丢失（修复前注释举的例子没被覆盖）。"""
    glued = QCC_FULL_HTML.replace('<span>登记状态 存续 成立日期 2001-11-30</span>',
                                  '<span>登记状态存续参保人数11616</span>')
    assert glued != QCC_FULL_HTML  # 防替换串失效导致假通过
    registry, tab = commercial_replay('qcc_web', [CARD], {CARD_HREF: glued})
    tab, browser, slept = drive(monkeypatch, tab)
    quiet_lookup(monkeypatch)
    outcome = collectors.collect('qcc_web', NAME, browser_module=browser, data_dir=tmp_path)
    rules = {item['rule']: item['value'] for item in outcome['signals']}
    assert rules['registration_status'] == {'flag': 1}
    assert '登记状态' in outcome['message']
    # 粘连修复不波及同页其余信号
    assert rules['credit_risk'] == {'flag': 0} and rules['admin_penalty'] == {'count': 0}
