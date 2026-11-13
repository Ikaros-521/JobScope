"""Collector tests driven by fake tabs: no socket, no Chrome, no DrissionPage import."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from urllib.parse import urlencode

from jobscope.reliability import collectors, local
from jobscope.reliability.models import SignalInput
from jobscope.repository import Repository, now

# 页面 HTML 均为「按实测进入方式 + 待校正解析假设」构造的回放样本：真实 DOM 以快照迭代为准。
NAME = '甲公司'
GSXT_SEARCH_URL = collectors.GSXT_ORIGIN + collectors.GSXT_SEARCH_PATH + '?' + urlencode({'tab': 'main', 'searchWord': NAME})
GSXT_DETAIL_PATH = '/corpdashboard/instrument/abc123.html'
GSXT_DETAIL_URL = collectors.GSXT_ORIGIN + GSXT_DETAIL_PATH
GSXT_ANNUAL_PATH = '/corpdashboard/annual-report/list.html'
GSXT_ANNUAL_URL = collectors.GSXT_ORIGIN + GSXT_ANNUAL_PATH
CREDIT_SEARCH_URL = collectors.CREDIT_ORIGIN + '/home?' + urlencode({'keyword': NAME})

GSXT_SEARCH_HTML = f'<a href="{GSXT_DETAIL_PATH}">{NAME}</a>'
GSXT_DETAIL_HTML = '<p>成立日期 2015-04-01</p><a href="' + GSXT_ANNUAL_PATH + '">企业年报</a>'
GSXT_ANNUAL_HTML = '<td>城镇职工基本养老保险 240人</td><td>城镇职工基本养老保险 312人</td>'
SLIDER_HTML = '<div>请拖动滑块完成拼图验证</div>'
LOGIN_HTML = '<div>请登录后查看</div>'


class FakeFlowTab:
    """按 URL 回放多页 HTML；ele() 只为裁判文书网的检索框交互服务。"""

    def __init__(self, pages=None, search_target=None, default=''):
        self.pages = pages or {}
        self.search_target = search_target
        self.visited = []
        self.closed = False
        self.current = default
        self.typed = []

    def get(self, url, timeout=None):
        self.visited.append(url)
        if url in self.pages:
            self.current = url

    @property
    def url(self):
        return self.current

    @property
    def html(self):
        return self.pages.get(self.current, '')

    def ele(self, selector, timeout=None):
        if selector.startswith('css:input') and self.search_target:
            return FakeBox(self)
        return None

    def close(self):
        self.closed = True


class FakeBox:
    def __init__(self, tab):
        self.tab = tab

    def input(self, text, clear=False):
        if text != '\n':
            self.tab.typed.append(text)
            if self.tab.search_target:
                self.tab.current = self.tab.search_target

    def click(self):
        pass


class DeadTab(FakeFlowTab):
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


def drive(monkeypatch, tab=None, *, installed=True, running=True, session_free=True, tabbed=True):
    """把一次采集接到假 tab 上，返回 (假 tab, 假浏览器, 实际等待过的秒数)。

    CHALLENGE_WAIT 置 0：质询轮询在测试里直接返回当前页，真实等待路径由常量本身约束。
    """
    tab = tab if tab is not None else FakeFlowTab({})
    browser = FakeBrowser(installed=installed, running=running, session_free=session_free)
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


# ── 公示系统：检索 → 详情 → 年报 ────────────────────────────────────────────────

def test_gsxt_full_pass_visits_search_detail_and_annual(monkeypatch):
    pages = {GSXT_SEARCH_URL: GSXT_SEARCH_HTML, GSXT_DETAIL_URL: GSXT_DETAIL_HTML, GSXT_ANNUAL_URL: GSXT_ANNUAL_HTML}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    labels = []
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, on_status=labels.append)
    assert outcome['status'] == 'completed'
    assert [item['rule'] for item in outcome['signals']] == ['company_age', 'insured_headcount_trend']
    assert outcome['signals'][0]['value']['years'] >= 1
    assert outcome['signals'][0]['observed_at'] == '2015-04-01'
    assert outcome['signals'][1]['value'] == {'before': 312, 'after': 240}
    assert tab.visited == [GSXT_SEARCH_URL, GSXT_DETAIL_URL, GSXT_ANNUAL_URL]
    assert all(item['url'] == visited for item, visited in zip(outcome['signals'], tab.visited[1:]))
    assert labels == ['检索列表', '登记信息', '企业年报']
    assert slept == [collectors.PAGE_DELAY, collectors.PAGE_DELAY]
    assert tab.closed is True


def test_gsxt_slider_page_stops_and_stays_open(monkeypatch):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: SLIDER_HTML}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_verification' and outcome['signals'] == []
    assert len(tab.visited) == 1 and tab.closed is False and slept == []


def test_gsxt_slider_on_annual_page_keeps_the_age_already_collected(monkeypatch):
    """详情页成功、年报页被滑块挡住：已取得的成立日期照常带回，页面留前台。"""
    pages = {GSXT_SEARCH_URL: GSXT_SEARCH_HTML, GSXT_DETAIL_URL: GSXT_DETAIL_HTML, GSXT_ANNUAL_URL: SLIDER_HTML}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'partial'
    assert [item['rule'] for item in outcome['signals']] == ['company_age']
    assert tab.closed is False and '人工校验或登录' in outcome['message']


def test_gsxt_without_exact_name_match_writes_a_debug_snapshot(monkeypatch, tmp_path):
    pages = {GSXT_SEARCH_URL: '<a href="/x.html">乙公司</a>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert '快照' in outcome['message']
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()
    assert tab.closed is True


def test_gsxt_detail_without_annual_link_keeps_the_age_only(monkeypatch):
    pages = {GSXT_SEARCH_URL: GSXT_SEARCH_HTML, GSXT_DETAIL_URL: '<p>成立日期 2015-04-01</p>'}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'partial'
    assert [item['rule'] for item in outcome['signals']] == ['company_age']
    assert '年报参保人数' in outcome['message']


def test_gsxt_challenge_timeout_is_reported_with_a_snapshot(monkeypatch, tmp_path):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: 'Environment Checking'}))
    outcome = collectors.collect('gsxt', NAME, browser_module=browser, data_dir=tmp_path)
    assert outcome['status'] == 'partial' and outcome['signals'] == []
    assert (tmp_path / 'debug' / 'gsxt-last.html').exists()


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

def test_an_unavailable_session_asks_for_login_instead_of_guessing(monkeypatch):
    tab, browser, slept = drive(monkeypatch, FakeFlowTab({GSXT_SEARCH_URL: GSXT_SEARCH_HTML}),
                                running=False, tabbed=False)
    outcome = collectors.collect('gsxt', NAME, browser_module=browser)
    assert outcome['status'] == 'needs_login' and outcome['signals'] == []
    assert tab.visited == [] and browser.locks == 1 and slept == []


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
    pages = {GSXT_SEARCH_URL: GSXT_SEARCH_HTML, GSXT_DETAIL_URL: GSXT_DETAIL_HTML, GSXT_ANNUAL_URL: GSXT_ANNUAL_HTML}
    tab, browser, slept = drive(monkeypatch, FakeFlowTab(pages))
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
