"""Read official public registries through the user's own project Chrome session.

浏览器层完全复用 `jobscope/browser.py`：同一个 profile 目录、同一份端口记录、同一把 OS 会话锁，
分别走它的 `status()` / `_connection()` / `session_lock()`，无会话时调它的 `start()` 自动拉起
项目浏览器（与岗位采集入口同一行为、同一套文案）。这里不另起浏览器进程、不管端口、也不做第二
套 profile；DrissionPage 只在真的要拿标签页时才导入。

诚实状态（2026-11-15 在已实名登录的会话里逐页复测后的结论）：
- 公示系统：检索/详情通路已按真实 DOM 校准（详见 GSXT_SEARCH_URL 处注释与测试回放样本）；
  仍待校正的只剩年报参保数字——当晚实测年报页签的表渲染为空（接口 count:0），数字版式未见过。
- 信用中国未登录直接 302 到 auth.creditchina.gov.cn 登录页；裁判文书网检索需要登录。这两家的
  **结果页 DOM 仍未实测**，`sources.py` 里对应来源的 `status` 必须保持 `needs_verification`。
- 解析失败时把页面快照写到 `data_dir/debug/<source>-last.html`（只保留最后一次），供人工
  校正正则；快照属于诊断产物，跟数据目录一起清理，不含密钥。
- 涉诉类来源（信用中国/裁判文书网）只能拿到「案件条数」这一个数字；每万人比率的分母是参保
  人数，只可能来自公示系统年报或付费接口。落库前查已存参保信号合并换算；库里没有分母就
  **不落信号**（避免一条 cases-only 信号把已出分的比率顶掉——repository.signals 每规则只留
  最新一条），并在结果消息里说明先采什么。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .rubric import RULES
from .sources import SIGNAL_SOURCES

logger = logging.getLogger(__name__)

# 单次任务的硬上限：页面上限与页间等待都写死在这里，调用方不接受更激进的值。
MAX_PAGES = 5
PAGE_DELAY = 2.5
PAGE_TIMEOUT = 25
# 加速乐/环境质询自解的等待上限：真实 Chrome 通常 1-3 秒内自动跳转，超时说明被风控，交给人工。
CHALLENGE_WAIT = 15
CHALLENGE_POLL = 1.0
# 页面"渲染完成"的最小体积：质询页只有几百字节，结果页至少几 KB。
READY_MIN_LENGTH = 2500

# 验证码/风控/登录墙的标志文字。命中即停在当前页交给用户，不绕、不重试、不换 UA。
VERIFY_MARKERS = ('验证码', '滑块', '拖动', '安全校验', '请完成验证', '拼图', '异常流量',
                  'access denied', 'unauthorized')
LOGIN_MARKERS = ('请登录', '扫码登录', '立即登录', '用户登录', '系统登录', '登录后可查看', '请先登录')
# 加速乐一类的环境质询页：不是人工校验，等它自解即可，超时才算失败。
CHALLENGE_MARKERS = ('Environment Checking', '请开启JavaScript', '请开启 JavaScript', 'checking your browser')
# 2026-11-14 实测（快照见 _capture）：加速乐的混淆壳页没有上面的标志文字，页面唯一的内容
# 是一段 _0x 十六进制数组混淆的 JS，可见文本近零。它体积远超 READY_MIN_LENGTH，不归入质询
# 就会被当成「无结果的搜索页」提前放行——这正是浏览器已启动时公示系统首轮采不到东西的原因。
CHALLENGE_OBFUSCATED = re.compile(r'var\s+_0x[a-f0-9]{2,}\s*=\s*\[')
# 2026-11-14 实测同日第二轮：加速乐 404/错误插页（iframe 指向 404.jiasule.com，正文只有
# notice_404 一个容器）。既不是质询也不是登录，是这次请求本身没被受理；按「读取未完成」上报
# 交给快照与人工重试，不许糊弄成「检索结果里没有一致条目」。
ERROR_PAGE_MARKERS = ('404.jiasule.com', 'notice_404')

# 2026-11-15 实测校准（快照 output/gsxt-search-result.html、gsxt-detail.html）：
# - 旧口径 /corpdashboard/pc-search.html 从来不存在，直开就是 404（用户当晚报告的正是它）。
# - 真实检索在实名子系统 shiming.gsxt.gov.cn 的 corp-query-search-1.html：表单 POST 且必须带
#   极验 v4 票据（captcha_output/pass_token/lot_number…），拼地址或裸 POST 一律被弹掉；
#   未实名登录时壳页没有检索框。唯一通路 = 开壳页 → 填 #keyword → 点 #btn_query → 人过极验
#   → 表单自动提交出结果。
# - 结果条目 a.search_list_item 的 href 是一次性加密跳转，裸导航会被加速乐拦（实测），
#   只能在结果页里点击条目、等 target=_blank 的新标签页。
# - 详情页正文含「请登录人民银行征信中心…」一类提示文字，登录墙标记对它必误伤：gsxt 页面
#   一律 login_wall=False，登录态改由「检索框是否存在」判断。
GSXT_SEARCH_URL = 'https://shiming.gsxt.gov.cn/corp-query-search-1.html'
# 点「查询」后极验弹层由人完成，通过后表单自动提交：这里轮询等条目渲染，窗口给足人工时间。
HUMAN_SOLVE_WAIT = 120
HUMAN_SOLVE_POLL = 2.0
CREDIT_ORIGIN = 'https://public.creditchina.gov.cn'
CREDIT_LOGIN_HOST = 'auth.creditchina.gov.cn'
WENSHU_URL = 'https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html'

# 每万人比率唯一的换算常量：paid.py 从这里导入，两条通路一把尺子（paid 依赖本模块的 years_since，
# 导入方向只能是 collectors → paid，不能反过来）。
PER_WAN = 10000


class _Blocked(Exception):
    """页面被人工校验挡住：collect() 把状态置为 needs_verification 并把页面留在前台。"""


class _LoginWall(Exception):
    """页面要求登录：collect() 把状态置为 needs_login 并把页面留在前台。"""


def _looks_blocked(html):
    text = str(html or '').lower()
    return any(marker.lower() in text for marker in VERIFY_MARKERS)


def _login_wall(html, url=''):
    text = str(html or '')
    if CREDIT_LOGIN_HOST in str(url or ''):
        return True
    return any(marker in text for marker in LOGIN_MARKERS)


def _challenge(html):
    text = str(html or '')
    return (any(marker in text for marker in CHALLENGE_MARKERS)
            or bool(CHALLENGE_OBFUSCATED.search(text)))


def years_since(founded):
    """成立日期 → 多少年。全项目只有这一把尺子：`paid.py` 也算年数，用的就是这个函数。

    写成 `round(days / 365.25, 1)` 而不是按年份相减：跨闰年的误差由 365.25 吸收，
    而「今年减出生年」会把 2015-12-31 成立的公司在 2026-01-01 报成 11 年。
    """
    return round((datetime.now(timezone.utc) - founded).days / 365.25, 1)


def _sleep(seconds):
    """页间停手；单独抽一个函数只为让测试能断言间隔确实发生，真实等待没有变化。"""
    time.sleep(seconds)


def _signal(rule, value, observed_at='', url='', text=''):
    """维度与单位取自 rubric，避免采集器和规则表两处漂移；字段名对齐 models.SignalInput。

    `url` 是这条信号的出处，命名沿用证据层的同名字段：法律红线要求「每条依据都标出来源与
    抓取时间」，所以读不到实际访问地址的信号一律不许带空出处往下落库。
    `text` 会成为证据正文（persist_outcome 消费），口径说明写在这里而不是靠猜。
    """
    spec = RULES.get(rule)
    if spec is None or not isinstance(value, dict) or not value:
        return None
    return dict(rule=rule, dimension=spec.dimension, value=value, unit=spec.unit,
                source_class='official', observed_at=observed_at, url=url, text=text)


def _outcome(status, message, signals=()):
    """返回键与 connectors.result() 同构（把 jobs/evidence 换成 signals），前端 Badge 不用加分支。"""
    return {'status': status, 'message': message, 'signals': list(signals)}


def _open_tab(browser_module, data_dir):
    """取会话的方式只出现在这里：端口只取 browser.py 记录的本机回环端口。"""
    port = browser_module._connection(data_dir)
    if not port:
        return None
    from DrissionPage import Chromium
    return Chromium(f'127.0.0.1:{port}').new_tab()


def _close(tab):
    try:
        tab.close()
    except Exception:
        pass


def _html_of(tab):
    """标签页可能在校验跳转中失效；读不到就当没有正文，让状态判定退成 partial。"""
    try:
        return str(tab.html or '')
    except Exception:
        return ''


def _wait_ready(tab, min_length=READY_MIN_LENGTH, seconds=None):
    """轮询等质询页自解；真实浏览器 1-3 秒内会自动跳转，超时就按当前内容继续判定。"""
    wait = CHALLENGE_WAIT if seconds is None else seconds
    deadline = time.monotonic() + wait
    html = _html_of(tab)
    while time.monotonic() < deadline:
        if len(html) >= min_length and not _challenge(html):
            return html
        _sleep(CHALLENGE_POLL)
        html = _html_of(tab)
    return html


def _gate(html, url='', login_wall=True):
    """统一的闸门：登录墙优先于验证码（信用中国登录页同时含两样），质询页算未就绪。

    gsxt 页面传 login_wall=False：其实页正文里就有「请登录人民银行征信中心…」一类提示文字，
    按标记判墙必误伤；gsxt 的登录态由检索框是否存在来判断（见 _gsxt_search）。
    """
    if not str(html or '').strip():
        # 读不到正文（标签页失效/跳转中）：如实报读取未完成，不猜成登录或无结果。
        raise TimeoutError('页面为空，读取未完成')
    if _challenge(html):
        raise TimeoutError('环境质询页未在时限内通过')
    if any(marker in str(html or '') for marker in ERROR_PAGE_MARKERS):
        raise TimeoutError('来源返回了加速乐错误页，本轮请求未被受理')
    if login_wall and _login_wall(html, url):
        raise _LoginWall()
    if _looks_blocked(html):
        raise _Blocked()


def _capture(data_dir, source_id, tab):
    """解析失败时把页面快照存到 data_dir/debug/<source>-last.html，只保留最后一次。"""
    if not data_dir:
        return ''
    try:
        root = Path(str(data_dir)) / 'debug'
        root.mkdir(parents=True, exist_ok=True)
        path = root / f'{source_id}-last.html'
        path.write_text(_html_of(tab)[:500000], encoding='utf-8')
        return str(path)
    except Exception:
        return ''


# ── 公示系统：检索（实名子系统 + 极验）→ 详情（成立日期）→ 年报（参保人数） ──────

def _search_box(tab):
    """实名会话有效时壳页才有 #keyword；没有就按需登录处理。

    DrissionPage 的 ele() 找不到时返回 NoneElement，访问其属性才抛错：用一次属性读取
    区分「找到了」和「没有」，不依赖版本对假值判断的细节。
    """
    try:
        box = tab.ele('css:#keyword', timeout=5)
        box.attr('id')
        return box
    except Exception:
        return None


def _gsxt_items(html):
    """[(href, 纯名称)]：条目是 a.search_list_item，名称在 h1 里且被 <font> 高亮拆开，先去标签。"""
    items = []
    for match in re.finditer(
            r'<a\b[^>]*?class="[^"]*search_list_item[^"]*"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>',
            str(html or ''), re.S | re.I):
        inner = match.group(2)
        heading = re.search(r'<h1[^>]*>(.*?)</h1>', inner, re.S | re.I)
        items.append((match.group(1), re.sub(r'<[^>]+>|\s+', '',
                                             heading.group(1) if heading else inner)))
    return items


def _gsxt_open_detail(tab, href):
    """在结果页点击条目（target=_blank），等新标签页出现并返回它。

    加密链接只能这样有机进入：实测对它裸 tab.get 会被加速乐拦成错误插页。
    """
    try:
        before = {str(item.url or '') for item in tab.browser.get_tabs()}
        tab.ele(f'css:a[href="{href}"]', timeout=5).click()
    except Exception:
        return None
    deadline = time.monotonic() + PAGE_TIMEOUT
    while time.monotonic() < deadline:
        for item in tab.browser.get_tabs():
            url = str(item.url or '')
            if url and url not in before and 'gsxt.gov.cn' in url:
                return item
        _sleep(1)
    return None


def _gsxt_annual_counts(detail_tab):
    """点详情页「企业年报信息」页签，等 ajax 出表后抽养老保险参保数。

    2026-11-15 实测：页签是 [data-tag-name="annualreport"]，表格列「报送年度/公示日期/详情」；
    当晚该表渲染为空（接口 count:0），参保数字的最终版式仍未校正——抽不到就如实报未取到。
    """
    try:
        detail_tab.ele('css:[data-tag-name="annualreport"]', timeout=5).click()
    except Exception:
        return []
    _sleep(PAGE_DELAY)
    # 数字与标签词之间可能隔着单元格/样式标签：先去标签再配对。
    plain = re.sub(r'<[^>]+>', '', _html_of(detail_tab))
    return [int(item) for item in re.findall(
        r'城镇职工基本养老保险[^0-9]{0,30}(\d+)\s*人', plain)]


def _gsxt_search(tab, keyword):
    """2026-11-15 实测校准的检索通路：开壳页 → 填词 → 点「查询」→ 轮询等结果条目。

    极验点选弹层由人完成，通过后表单自动提交、条目渲染出来；轮询窗口给足人工时间，
    超时按需人工校验上报（弹层仍停在前台，完成后重新发起采集即可继续）。
    """
    tab.get(GSXT_SEARCH_URL, timeout=PAGE_TIMEOUT)
    html = _wait_ready(tab)
    _gate(html, str(tab.url or ''), login_wall=False)
    box = _search_box(tab)
    if box is None:
        raise _LoginWall()
    try:
        box.input(keyword, clear=True)
        tab.ele('css:#btn_query', timeout=5).click()
    except Exception:
        return [], str(tab.url or GSXT_SEARCH_URL)
    deadline = time.monotonic() + HUMAN_SOLVE_WAIT
    html = _html_of(tab)
    while time.monotonic() < deadline:
        items = _gsxt_items(html)
        if items:
            return items, str(tab.url or GSXT_SEARCH_URL)
        _sleep(HUMAN_SOLVE_POLL)
        html = _html_of(tab)
    raise _Blocked()


def _collect_gsxt(tab, keyword, on_status=None, data_dir=None):
    drafts = []
    try:
        return _gsxt_pass(tab, keyword, on_status, data_dir, drafts)
    except (_Blocked, _LoginWall):
        if not drafts:
            raise
        # 前几页已解析出的数字照常带回去；页面留前台，用户处理后重新采集补齐其余信号。
        return drafts, 'partial', (f'已取到 {len(drafts)} 类信号，但下一页要求人工校验或登录；'
                                   '页面已留在项目浏览器前台，处理后重新发起采集可补齐其余信号。'), True


def _gsxt_pass(tab, keyword, on_status, data_dir, drafts):
    if on_status:
        on_status('检索列表')
    items, _ = _gsxt_search(tab, keyword)
    href = next((link for link, name in items if name == keyword), None)
    if href is None:
        snapshot = _capture(data_dir, 'gsxt', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', (f'公示系统检索结果里没有与「{keyword}」完全一致的条目'
                               f'（结果共 {len(items)} 条），本轮未采集{note}。'), False
    if on_status:
        on_status('登记信息')
    detail = _gsxt_open_detail(tab, href)
    if detail is None:
        snapshot = _capture(data_dir, 'gsxt', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return drafts, 'partial', f'公示系统详情页未能打开（加密链接点击后未出新标签页）{note}。', False
    try:
        html = _wait_ready(detail)
        _gate(html, str(detail.url or ''), login_wall=False)
        # 2026-11-15 实测：真实详情页里「成立日期：」与日期之间隔着一整段样式标签（类名还带
        # 数字，如 fa-info-circle1），跨标签匹配必须先去标签再对齐。
        plain = re.sub(r'<[^>]+>', '', html)
        match = re.search(r'成立日期[^0-9]{0,30}(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})', plain)
        if match:
            try:
                founded = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
            except ValueError:
                founded = None
            if founded is not None:
                drafts.append(_signal('company_age', {'years': years_since(founded)},
                                      founded.date().isoformat(), str(detail.url or ''),
                                      f'成立日期取自公示系统登记信息页（{founded.date().isoformat()}），'
                                      f'按同一把算法换算 {years_since(founded)} 年。'))
        if on_status:
            on_status('企业年报')
        counts = _gsxt_annual_counts(detail)
        # 假设（未实测校正）：年报由新到旧排列，counts[0] 是最近一期。实测若相反必须交换两端，
        # 否则趋势会把扩员读成裁员。observed_at 留空：还没把握把「XXXX年度报告」与数字逐行配对。
        if len(counts) >= 2:
            drafts.append(_signal('insured_headcount_trend', {'before': counts[1], 'after': counts[0]},
                                  '', str(detail.url or ''),
                                  '参保人数取自公示系统年报社保信息（城镇职工基本养老保险），页面排列由新到旧的'
                                  '假设未实测校正；人数变动有多种成因，本条只登记数字。'))
    finally:
        _close(detail)
    missing = [label for label, got in (('成立日期', drafts[:1]), ('年报参保人数', counts)) if not got]
    if len(drafts) == 2:
        return drafts, 'completed', '从公示系统采集到成立日期与年报参保人数。', False
    snapshot = _capture(data_dir, 'gsxt', tab)
    note = f'（页面快照：{snapshot}）' if snapshot else ''
    return drafts, 'partial', f'从公示系统采集到 {len(drafts)} 类信号（未取到：{"、".join(missing) or "无"}）{note}。', False


# ── 信用中国：检索页（需登录）→ 失信记录条数 ────────────────────────────────────

CREDIT_COUNT_PATTERNS = (
    r'失信被执行人[^0-9<]{0,30}(\d+)\s*条',
    r'失信被执行人[（(]\s*(\d+)\s*[）)]',
    r'严重失信[^0-9<]{0,30}(\d+)\s*条',
)
CREDIT_EMPTY_MARKERS = ('没有找到', '暂无', '未检索到', '查询结果为空', '无记录')


def _collect_creditchina(tab, keyword, on_status=None, data_dir=None):
    if on_status:
        on_status('信用信息')
    tab.get(CREDIT_ORIGIN + '/home?' + urlencode({'keyword': keyword}), timeout=PAGE_TIMEOUT)
    html = _wait_ready(tab, min_length=3000)
    _gate(html, str(tab.url or ''))
    if keyword not in re.sub(r'\s+', '', html):
        snapshot = _capture(data_dir, 'credit_china', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', f'信用中国结果页没有出现「{keyword}」{note}。', False
    cases = None
    for pattern in CREDIT_COUNT_PATTERNS:
        match = re.search(pattern, html)
        if match:
            cases = int(match[1])
            break
    if cases is None and any(marker in html for marker in CREDIT_EMPTY_MARKERS):
        cases = 0
    if cases is None:
        snapshot = _capture(data_dir, 'credit_china', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', f'信用中国页面没有出现可解析的失信记录计数{note}。', False
    draft = _signal('judicial_case_ratio', {'cases': cases}, '', str(tab.url or ''),
                    f'信用中国口径：失信记录 {cases} 条（严重失信主体名单检索命中，非全部涉诉案件）。'
                    '每万人比率的分母是参保人数，采集时与已存参保信号合并换算。')
    draft['needs_denominator'] = True
    return [draft], 'completed', f'从信用中国读取到 {cases} 条失信记录。', False


# ── 裁判文书网：检索（需登录）→ 文书命中数量 ────────────────────────────────────

def _fill_and_search(tab, keyword):
    """DOM 未实测：能找到文本框就填词并点搜索/回车，找不到如实返回 False 交给快照。"""
    try:
        box = tab.ele('css:input[type=text]', timeout=5)
        if box is None:
            return False
        box.input(keyword, clear=True)
        button = tab.ele('text:搜索', timeout=3) or tab.ele('text:检索', timeout=3)
        if button is not None:
            button.click()
        else:
            box.input('\n')
        return True
    except Exception:
        return False


def _collect_court(tab, keyword, on_status=None, data_dir=None):
    if on_status:
        on_status('文书检索')
    tab.get(WENSHU_URL, timeout=PAGE_TIMEOUT)
    html = _wait_ready(tab, min_length=3000)
    # 裁判文书网的登录墙在检索时弹出；页面壳本身没有登录字样判定不了，先试检索再判。
    if not _fill_and_search(tab, keyword):
        snapshot = _capture(data_dir, 'court_records', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        if _login_wall(html):
            raise _LoginWall()
        return [], 'partial', f'裁判文书网页面没有找到可用的检索框{note}。', False
    _sleep(PAGE_DELAY)
    html = _wait_ready(tab, min_length=3000)
    if _login_wall(html, str(tab.url or '')):
        raise _LoginWall()
    _gate(html, str(tab.url or ''))
    match = re.search(r'共\s*([\d,]+)\s*篇', html)
    if match is None:
        snapshot = _capture(data_dir, 'court_records', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', f'裁判文书网没有出现可解析的文书计数{note}。', False
    cases = int(match[1].replace(',', ''))
    draft = _signal('judicial_case_ratio', {'cases': cases}, '', str(tab.url or ''),
                    f'裁判文书网口径：检索命中 {cases} 篇文书（含判决书之外的各类文书，仅作涉诉数量口径）。'
                    '每万人比率的分母是参保人数，采集时与已存参保信号合并换算。')
    draft['needs_denominator'] = True
    return [draft], 'completed', f'从裁判文书网读取到 {cases} 篇命中文书。', False


# ── 商业站点公开页（企查查/天眼查/爱企查）：检索卡片（成立日期）→ 详情页（参保/涉诉） ──
#
# 与官方登记站点不同，这三家不需要接口密钥也不强制登录：完全复用 `company_lookup.py` 的
# 公开搜索页通路（同一套 SITES/卡片抽取），取到什么登记什么。诚实边界与快查同一口径：
# 搜索卡片与详情页的字段正则均未在真实会话里逐站实测，解析不到就如实报缺 + 快照待校正；
# 未登录会话两家商业站会遮蔽部分数字（如「登录后查看」），那类值抽不出来、也不猜。

# 详情页要抽的数字（对去标签后的纯文本配对）。
INSURED_RE = re.compile(r'参保人数[^0-9]{0,12}(\d+)\s*人?')
CASES_RE = re.compile(r'(?:司法案件|裁判文书|开庭公告)[^0-9<]{0,10}(\d+)')
DATE_RE = re.compile(r'(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})日?')
# 参保数字的人力合理区间：全国最大单一雇主参保约 230 万，上限放到 500 万；区间外一律不认。
# 防的正是 2026-11-15 实测的事故：去标签后相邻数字粘连（「参保人数 51843」+「2025 年报」
# 被并成 518432025），以及分支机构参保数被当成主体数——宁缺勿猜。
INSURED_MIN, INSURED_MAX = 1, 5_000_000
# 天眼查把参保数嵌在页面 JSON 里：num=主体参保、branchNum=分支机构、year=年报年份。
# 渲染文本不可信（就是上面粘连事故的来源），tyc 直接从原始 html 的 JSON 里取。
TYC_INSURED_JSON = re.compile(r'"num":(\d{1,7}),"branchNum":\d{1,7},"year":(\d{4})')
# 信号来源键 → company_lookup.SITES 渠道键。
COMMERCIAL_SITES = {'qcc_web': 'qcc', 'tyc_web': 'tyc', 'aiqicha_web': 'aiqicha'}
# 详情页的参保/司法区块是异步渲染的（2026-11-15 实测：固定等 1.5 秒会读早）：轮询等到
# 关键字出现或超时，读到的就是已渲染的区块，不会多发任何请求。
DETAIL_SECTION_WAIT = 6


def _commercial_founded(card):
    """卡片字段里的成立日期 → datetime；没有或不成日期就 None，不硬凑。

    企查查/天眼查的标签是「成立日期」，爱企查是「成立时间」（2026-11-15 实测），两边都认。
    """
    fields = card.get('fields') or {}
    match = DATE_RE.search(str(fields.get('成立日期') or fields.get('成立时间') or ''))
    if match is None:
        return None
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
    except ValueError:
        return None


# 国企性质判定只认「企业类型」的值，不扫全页正文——页面里出现的「国有企业」新闻字样
# 不构成这家公司性质的依据。命中任一关键词 → 1；字段明确存在但无命中 → 0；没字段 → None。
SOE_MARKERS = ('国有', '全民所有制', '集体所有制', '国家出资')
# 类型值必须像登记类型（2026-11-15 实测：爱企查「企业类型」标签后会跟「变更查看动态…」
# 变更流水，企查查卡片会跟「从"X"变更为"Y"」——没结构词的值一律当脏数据弃用，不猜）。
ENTITY_TYPE_WORDS = ('有限公司', '有限责任公司', '合伙', '独资', '所有制', '出资', '股份', '个体')
ENTITY_TYPE_DIRTY = ('变更', '新增', '动态', '查看', '国标行业')
# 天眼查把企业类型嵌在页面 JSON 里（干净，无粘连）：优先于渲染文本。
TYC_ORG_TYPE_JSON = re.compile(r'"companyOrgType":"([^"]{2,50})"')


def _entity_type(card, plain, html=''):
    """(类型原文或 None)：天眼查 JSON > 卡片字段 > 文本行；值必须过结构词校验。"""
    candidates = []
    json_match = TYC_ORG_TYPE_JSON.search(str(html or ''))
    if json_match:
        candidates.append(json_match[1])
    fields = card.get('fields') or {}
    if fields.get('企业类型'):
        candidates.append(str(fields['企业类型']))
    text_match = re.search(r'企业类型[（(]?[^：:\s]{0,10}[：:]?\s*([^\s，,。；;（）()]{2,40})', plain)
    if text_match:
        candidates.append(text_match[1])
    for value in candidates:
        if (any(word in value for word in ENTITY_TYPE_WORDS)
                and not any(word in value for word in ENTITY_TYPE_DIRTY)):
            return value
    return None


def _soe_flag(card, plain, html=''):
    """(flag, 企业类型原文)；未取得可信类型返回 (None, '')。确定性关键词判定，不猜。"""
    value = _entity_type(card, plain, html)
    if value is None:
        return None, ''
    return (1 if any(marker in value for marker in SOE_MARKERS) else 0), value


# 人员规模（工商照面登记口径，天眼查详情文本行）：「10000人以上」「100-499人」「58435人」。
# 这是 company_size 的一条公司口径自述，与 JD 里的团队自述一起进 team_size_statement 聚合。
STAFF_RANGE_RE = re.compile(r'人员规模\s*([^\s，,。；;：:]{1,15})')


def _staff_range_value(text):
    """「10000人以上」→ {'size': 10000}；「100-499人」→ {'size': 100, 'size_max': 499}；解析不出 → None。

    「以上」取下界当规模点值：证据正文写明口径是登记下界，不冒充精确值。
    """
    match = STAFF_RANGE_RE.search(str(text or ''))
    if match is None:
        return None
    value = match[1]
    interval = re.search(r'(\d[\d,]*)\s*[-~至]\s*(\d[\d,]*)', value)
    if interval:
        low = int(interval[1].replace(',', ''))
        return {'size': low, 'size_max': int(interval[2].replace(',', ''))} if low > 0 else None
    floor = re.search(r'(\d[\d,]*)', value)
    return {'size': int(floor[1].replace(',', ''))} if floor and int(floor[1].replace(',', '')) > 0 else None


def _commercial_insured(source_id, html, plain):
    """[最近一期, 上一期]（不足两期就短列表）：天眼查走页面 JSON，其余走标签配对 + 范围闸门。"""
    if source_id == 'tyc_web':
        by_year = {}
        for number, year in TYC_INSURED_JSON.findall(str(html or '')):
            value, period = int(number), int(year)
            if INSURED_MIN <= value <= INSURED_MAX and 1990 <= period <= 2035:
                by_year[period] = value  # 同一年份重复出现时以最后一条为准
        return [by_year[period] for period in sorted(by_year, reverse=True)[:2]]
    return [int(item) for item in INSURED_RE.findall(plain)
            if INSURED_MIN <= int(item) <= INSURED_MAX]


# 爱企查年报（2026-11-15 实测校准）：公司页年报表的行有直达链接 /annualreport?pid=..&year=YYYY，
# 公开可访问、无需登录。逐年页面公示「从业人数」（企业可选不公示）与「社保信息」
# （城镇职工基本养老保险参保数，实测恒公示）；「主营业务收入/营业收入」企业可选不公示。
AQC_ORIGIN = 'https://aiqicha.baidu.com'
# DOM 序列化里 & 会写成 &amp;，两种都收；收获后统一归一化成 & 再拼地址。
AQC_ANNUAL_LINK = re.compile(r'href="(/annualreport\?pid=\d+&(?:amp;)?year=\d{4})"')
AQC_STAFF_RE = re.compile(r'从业人数\s*([^\s，,。；;]{1,15})')
AQC_PENSION_RE = re.compile(r'城镇职工基本养老保险\s*(\d+)\s*人')
AQC_REVENUE_RE = re.compile(r'(?:主营业务收入|营业收入)\s*([^\s，,。；;]{1,20})')
AMOUNT_UNITS = {'万': 10000, '亿': 100000000}


def _aqc_amount(text):
    """「1.2亿」「3000万」「120000」→ 元；「不公示」/无数字 → None。"""
    match = re.search(r'([\d,]+(?:\.\d+)?)\s*([万亿]?)', str(text or ''))
    if match is None or not match[1]:
        return None
    try:
        value = float(match[1].replace(',', ''))
    except ValueError:
        return None
    return value * AMOUNT_UNITS.get(match[2], 1)


def _aqc_read_annual(tab, detail_html, on_status):
    """读爱企查最近两期年报 → [(年份, {staff, pension, revenue}, 口径纯文本), …]。

    只取年报表里链接直达的最近两年（每期一次页面访问，有界）；年报表要点「企业年报」页签
    才渲染，当前 DOM 收不够链接时先点页签再收一次。读取失败的字段留 None，由调用方决定
    口径是否成立——同一口径两期都成立才出同比，宁缺勿猜。
    """
    from .. import company_lookup

    def harvest(html_text):
        found = {href.replace('&amp;', '&') for href in AQC_ANNUAL_LINK.findall(str(html_text or ''))}
        return sorted(found, key=lambda href: int(href[-4:]), reverse=True)

    links = harvest(detail_html)[:2]
    if len(links) < 2:
        try:
            tab.ele('css:#convenient-tab-annual', timeout=3).click()
        except Exception:
            try:
                tab.ele('css:a.subtab-annual', timeout=3).click()
            except Exception:
                return []
        _sleep(company_lookup.RENDER_DELAY)
        links = harvest(_html_of(tab))[:2]
    if len(links) < 2:
        return []
    rows = []
    for href in links:
        _sleep(PAGE_DELAY)
        tab.get(AQC_ORIGIN + href, timeout=PAGE_TIMEOUT)
        _sleep(company_lookup.RENDER_DELAY)
        plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', _html_of(tab)))
        staff = AQC_STAFF_RE.search(plain)
        pension = AQC_PENSION_RE.search(plain)
        revenue = AQC_REVENUE_RE.search(plain)
        # 从业人数的值可能带「人」等后缀或写「不公示」：只认捕获值里成串的数字。
        staff_digits = re.search(r'\d[\d,]*', staff[1]) if staff else None
        rows.append((href[-4:],
                     dict(staff=int(staff_digits[0].replace(',', '')) if staff_digits else None,
                          pension=int(pension[1]) if pension else None,
                          revenue=_aqc_amount(revenue[1]) if revenue else None),
                     plain))
    if on_status:
        on_status(f'年报({rows[-1][0]}-{rows[0][0]})')
    return rows


def _commercial_pass(tab, keyword, source_id, on_status, data_dir, drafts):
    from .. import company_lookup
    spec = company_lookup.SITES[COMMERCIAL_SITES[source_id]]
    if on_status:
        on_status('检索卡片')
    cards, markers = company_lookup._read_cards(tab, spec, keyword)
    if not cards and markers:
        # 一张卡片都没有且页面摆出登录/验证字样：停在前台交给用户，不重试、不绕过。
        raise _Blocked()
    if not cards:
        snapshot = _capture(data_dir, source_id, tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', f'{spec["name"]}搜索页没有解析出公司卡片{note}。', False
    card = next((item for item in cards if item['name'] == keyword), None)
    if card is None:
        card = next((item for item in cards if item['name'].startswith(keyword)), None)
    if card is None:
        snapshot = _capture(data_dir, source_id, tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', (f'{spec["name"]}检索结果里没有与「{keyword}」一致的条目'
                               f'（共 {len(cards)} 条）{note}。'), False
    got = []
    founded = _commercial_founded(card)
    if founded is not None:
        drafts.append(_signal('company_age', {'years': years_since(founded)},
                              founded.date().isoformat(), str(card.get('url') or ''),
                              f'成立日期取自{spec["name"]}公开搜索卡片（{founded.date().isoformat()}），'
                              f'按同一把算法换算 {years_since(founded)} 年。'))
        got.append('成立日期')
    if on_status:
        on_status('公司详情')
    detail_url = str(card.get('url') or '')
    _sleep(PAGE_DELAY)
    tab.get(detail_url, timeout=PAGE_TIMEOUT)
    try:
        # 后台标签会被节流、懒加载区块不渲染：把自己的标签顶到前台再等区块出来。
        tab.set.activate()
    except Exception:
        pass
    try:
        tab.scroll.to_bottom()
    except Exception:
        pass
    html = _html_of(tab)
    deadline = time.monotonic() + DETAIL_SECTION_WAIT
    # 基础信息区的「参保人数」来得快，「司法案件」区块更慢：等两者都渲染或超时再读。
    while ('参保人数' not in html or '司法案件' not in html) and time.monotonic() < deadline:
        company_lookup._sleep(company_lookup.RENDER_DELAY)
        html = _html_of(tab)
    # 2026-11-15 实测：爱企查标签与数值之间隔着大段空白换行（20+ 字符），先归一化空白再配对。
    plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', html))
    # 参保趋势按来源取数：tyc 从页面 JSON 取（渲染文本会粘连数字），其余标签配对 + 范围闸门。
    # 页面排列由新到旧的假设仅适用于非 tyc 来源；tyc 按年份排序，[0] 是最近一期。
    insured = _commercial_insured(source_id, html, plain)
    # 爱企查年报先读（导航离开详情页，必须在 plain 已提取之后）：公司页参保文本同样会与年份
    # 粘连（2026-11-15 实测 24033+2024），年报页逐年养老参保数才是干净口径——有年报就用它。
    annual = _aqc_read_annual(tab, html, on_status) if source_id == 'aiqicha_web' else []
    insured_basis = 'page'
    if len(annual) == 2 and annual[0][1]['pension'] and annual[1][1]['pension']:
        insured = [annual[0][1]['pension'], annual[1][1]['pension']]
        insured_basis = 'annual'
    if len(insured) >= 2:
        insured_text = (f'参保人数取自{spec["name"]}公开公司页（城镇职工口径未必一致，'
                        f'页面排列由新到旧的假设未实测校正）' if insured_basis == 'page' else
                        f'参保人数取自爱企查年报序列（{annual[1][0]}→{annual[0][0]} 年度报告的'
                        f'城镇职工基本养老保险参保数）')
        drafts.append(_signal('insured_headcount_trend', {'before': insured[1], 'after': insured[0]},
                              '', str(tab.url or detail_url),
                              insured_text + '；人数变动有多种成因，本条只登记数字。'))
        got.append('参保人数趋势')
    cases = CASES_RE.search(plain)
    if cases is not None:
        draft = _signal('judicial_case_ratio', {'cases': int(cases[1])}, '', str(tab.url or detail_url),
                        f'{spec["name"]}公开页口径：司法案件/裁判文书计数 {cases[1]}（公开页可见口径，'
                        '非全部涉诉）。每万人比率的分母是参保人数，采集时与已存参保信号合并换算。')
        draft['needs_denominator'] = True
        drafts.append(draft)
        got.append('涉诉计数')
    flag, entity_kind = _soe_flag(card, plain, html)
    if flag is not None:
        nature = '国有性质' if flag else '非国有'
        drafts.append(_signal('soe_status', {'flag': flag}, '', str(card.get('url') or detail_url),
                              f'登记的企业类型为「{entity_kind}」（取自{spec["name"]}公开页），'
                              f'按关键词确定性判定为{nature}；性质只是稳定性的一个侧面，不推断经营好坏。'))
        got.append('国企性质')
    # 人员规模（工商照面登记口径）：一条「公司口径自述」，与 JD 团队自述一起进
    # team_size_statement 聚合（聚合发生在提取端点，会把这条已存原始形态纳入）。
    staff = _staff_range_value(plain)
    if staff:
        raw = STAFF_RANGE_RE.search(plain)[0]
        suffix = f"（登记区间 {staff['size']}-{staff['size_max']} 人）" if 'size_max' in staff else ''
        drafts.append(_signal('team_size_statement', staff, '', str(card.get('url') or detail_url),
                              f'人员规模取自{spec["name"]}公开页工商照面信息（{raw}）{suffix}；'
                              '这是登记口径的规模自述，与岗位描述里的团队规模自述一起衡量口径一致性，'
                              '不评价规模好坏。'))
        got.append('人员规模')
    if source_id == 'aiqicha_web' and len(annual) == 2:
        # 爱企查年报序列：operations 维度的人数/营收同比（相邻年度、同一来源序列，
        # 口径写进证据正文；两期同一口径都成立才出信号）。年报已在上面读取复用。
        (year_new, row_new, _), (year_old, row_old, _) = annual
        basis, field = None, None
        if row_new['staff'] and row_old['staff']:
            basis, field = '从业人数（企业年报公示口径）', 'staff'
        elif row_new['pension'] and row_old['pension']:
            basis, field = '城镇职工基本养老保险参保数（从业人数未公示时的代理口径）', 'pension'
        if field:
            drafts.append(_signal('disclosure_headcount_trend',
                                  {'current': row_new[field], 'previous': row_old[field]}, '',
                                  str(tab.url or detail_url),
                                  f'人数取自爱企查年报序列（{year_old}→{year_new} 年度报告，口径：{basis}）；'
                                  '同比由固定规则换算，人数变动有多种成因，本条只登记数字，不推断经营好坏。'))
            got.append('年报人数同比')
        if row_new['revenue'] and row_old['revenue']:
            drafts.append(_signal('disclosure_revenue_trend',
                                  {'current': row_new['revenue'], 'previous': row_old['revenue']}, '',
                                  str(tab.url or detail_url),
                                  f'营收取自爱企查年报序列（{year_old}→{year_new} 年度报告的年度营业收入，'
                                  '企业公示口径）；同比由固定规则换算，营收变动有多种成因，本条只登记数字。'))
            got.append('年报营收同比')
    if not drafts:
        snapshot = _capture(data_dir, source_id, tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', (f'{spec["name"]}公开页没有取到可登记的数字字段'
                               f'（成立日期/参保人数/涉诉计数均未见）{note}。'), False
    return drafts, 'completed', f'从{spec["name"]}公开页取到：{"、".join(got)}。', False


def _collect_commercial(source_id):
    def flow(tab, keyword, on_status=None, data_dir=None):
        return _commercial_pass(tab, keyword, source_id, on_status, data_dir, [])
    return flow


# 来源键与 `reliability/sources.py` 的 SIGNAL_SOURCES 对齐。未登记的来源（含 qcc/tyc 付费源）一律 unsupported。
FLOWS = {
    'gsxt': _collect_gsxt,
    'credit_china': _collect_creditchina,
    'court_records': _collect_court,
    **{source: _collect_commercial(source) for source in COMMERCIAL_SITES},
}


def _stored_insured(repo, identity):
    """已存的最新参保人数（repo.signals 每规则最新一条，顺序即新→旧）；没有就 None。"""
    if repo is None or identity is None:
        return None
    try:
        rows = repo.signals(identity['id'])
    except Exception:
        return None
    for item in rows:
        if item.get('rule') != 'insured_headcount_trend':
            continue
        value = item.get('value') or {}
        after = value.get('after')
        if isinstance(after, (int, float)) and not isinstance(after, bool) and after > 0:
            return dict(insured=int(after), collected_at=str(item.get('collected_at') or ''),
                        source_class=str(item.get('source_class') or ''))
    return None


def _merge_denominator(drafts, repo, identity):
    """涉诉计数 × 已存参保人数 → 每万人比率。没有分母的计数不落库（防顶掉已出分的比率信号）。"""
    merged, dropped = [], 0
    for item in drafts:
        if not item.pop('needs_denominator', False):
            merged.append(item)
            continue
        denom = _stored_insured(repo, identity)
        cases = item['value'].get('cases')
        if denom is None or not isinstance(cases, (int, float)) or isinstance(cases, bool):
            dropped += 1
            continue
        insured = denom['insured']
        ratio = round(cases * PER_WAN / insured, 2) if insured > 0 else 0
        item['value'] = {'ratio': ratio, 'cases': cases, 'insured': insured}
        stamp = denom['collected_at'][:10] or '时间未知'
        item['text'] += (f'分母取已存参保信号（{stamp} 采集，{insured} 人，来源类别 '
                         f'{denom["source_class"] or "未知"}），按 件÷人×{PER_WAN} 换算 {ratio} 件/万人；'
                         '分母时点与分子不同步，比率只作量级参考。')
        merged.append(item)
    return merged, dropped


def collect(source_id, company, *, browser_module, data_dir=None, on_status=None, repo=None, identity=None):
    """One sequential pass: bounded pages, fixed spacing, no retry, no way around a challenge."""
    flow = FLOWS.get(source_id)
    if not flow:
        return _outcome('unsupported', '该来源不由项目浏览器采集。')
    keyword = str(company or '').strip()[:120]
    if not keyword:
        return _outcome('failed', '请先确定要查询的公司名称。')
    logger.info('信号采集开始：%s「%s」', source_id, keyword)
    state = browser_module.status(data_dir)
    if not state.get('installed'):
        return _outcome('unsupported', state.get('message', ''))
    if not browser_module._connection(data_dir):
        # 无会话时自动拉起项目浏览器，与岗位采集入口（browser.collect）同一行为：点「采集」的
        # 预期就是看到浏览器窗口，不再要求先去别处手动启动。start() 自带锁与连接探测，且它自己
        # 会拿会话锁，所以必须在进锁之前调用；拉不起来（依赖缺失、配置目录被占）就按它的结果提示。
        started = browser_module.start(data_dir, source_id)
        if not browser_module._connection(data_dir):
            return _outcome('needs_login', started.get('message') or
                            '请先启动项目浏览器并在来源页面完成登录或验证。')
    with browser_module.session_lock(data_dir) as acquired:
        if not acquired:
            # 会话被占用就退让，不排队、不抢占——同一时刻只允许一个浏览器任务。
            return _outcome('backoff', '另一个采集任务正占用项目浏览器，请稍后重试。')
        try:
            tab = _open_tab(browser_module, data_dir)
        except Exception:
            logger.exception('信号采集接入项目浏览器失败：%s「%s」', source_id, keyword)
            return _outcome('failed', '无法接入项目浏览器会话；请确认它仍在运行后重试。')
        if tab is None:
            # 自动拉起之后仍拿不到会话，只剩「拉起成功到开标签页之间浏览器退出」的窄窗口：
            # 如实报中断让用户重试，不猜成需要登录。
            return _outcome('needs_login', '项目浏览器会话已中断，请重新发起采集。')
        try:
            drafts, status, message, keep_open = flow(tab, keyword, on_status=on_status, data_dir=data_dir)
        except _LoginWall:
            return _outcome('needs_login', f'{SIGNAL_SOURCES[source_id]["name"]}需要登录；'
                            '已把登录页留在项目浏览器前台，请登录后重新发起本来源采集。')
        except _Blocked:
            return _outcome('needs_verification', f'{SIGNAL_SOURCES[source_id]["name"]}要求人工校验或登录；'
                            '已把该页留在项目浏览器前台，请手动处理后重试。')
        except TimeoutError as exc:
            # 质询页/错误页在时限内没让路（2026-11-14 实测：防护限流时首页长时间白屏，
            # 质询脚本本身也被拒）。把具体原因与白屏解释带给用户，别只说「读取未完成」。
            snapshot = _capture(data_dir, source_id, tab)
            note = f'页面快照已存 {snapshot}。' if snapshot else ''
            drafts, status, message, keep_open = [], 'partial', (
                f'{source_id} {str(exc) or "页面读取未完成"}，本轮未采集到信号。'
                f'若项目浏览器里该站持续白屏，多为防护限流，请稍后重试。{note}'), False
        except Exception:
            logger.exception('信号采集页面读取失败：%s「%s」（快照见 data_dir/debug）', source_id, keyword)
            snapshot = _capture(data_dir, source_id, tab)
            note = f'页面快照已存 {snapshot}。' if snapshot else ''
            drafts, status, message, keep_open = [], 'partial', f'{source_id} 页面读取未完成，本轮未采集到信号。{note}', False
        if not keep_open:
            # 干净收尾才关标签页；停在验证/登录页时把页面留在前台交给用户处理。
            _close(tab)
        drafts, dropped = _merge_denominator(drafts, repo, identity)
        if dropped:
            status = 'partial'
            message += (f'有 {dropped} 条涉诉计数因缺少参保人数分母未落库；'
                        '可先采集公示系统参保数（或配置付费接口）后重试本来源。')
        logger.info('信号采集结束：%s「%s」状态=%s 信号=%s', source_id, keyword, status, len(drafts))
        return _outcome(status, message, drafts)
