"""Read official public registries through the user's own project Chrome session.

浏览器层完全复用 `jobscope/browser.py`：同一个 profile 目录、同一份端口记录、同一把 OS 会话锁，
分别走它的 `status()` / `_connection()` / `session_lock()`。这里不新起浏览器、不管端口、
也不做第二套 profile；DrissionPage 只在真的要拿标签页时才导入。

诚实状态（2026-11-13 一次性实测后的结论）：
- 三个来源都实测确认了**进入方式**：公示系统是加速乐质询页 + 搜索滑块（真实浏览器自动过质询，
  滑块必须人工拖）；信用中国未登录直接 302 到 auth.creditchina.gov.cn 登录页；裁判文书网检索
  需要登录。**结果页的 DOM 结构尚未实测**——下方每条正则都还是待校正的假设，实测跑通之前
  `sources.py` 里对应来源的 `status` 必须保持 `needs_verification`。
- 解析失败时把页面快照写到 `data_dir/debug/<source>-last.html`（只保留最后一次），供人工
  校正正则；快照属于诊断产物，跟数据目录一起清理，不含密钥。
- 涉诉类来源（信用中国/裁判文书网）只能拿到「案件条数」这一个数字；每万人比率的分母是参保
  人数，只可能来自公示系统年报或付费接口。落库前查已存参保信号合并换算；库里没有分母就
  **不落信号**（避免一条 cases-only 信号把已出分的比率顶掉——repository.signals 每规则只留
  最新一条），并在结果消息里说明先采什么。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin

from .rubric import RULES
from .sources import SIGNAL_SOURCES

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

GSXT_ORIGIN = 'https://www.gsxt.gov.cn'
# 2026-11 实测：搜索页是 /corpdashboard/pc-search.html?tab=main&searchWord=…（直开 index.html?keyword=… 不会触发检索，
# 这正是上一版采集不到数字的原因之一）。详情/年报页地址从结果页 DOM 里提取。
GSXT_SEARCH_PATH = '/corpdashboard/pc-search.html'
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
    return any(marker in text for marker in CHALLENGE_MARKERS)


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


def _gate(html, url=''):
    """统一的闸门：登录墙优先于验证码（信用中国登录页同时含两样），质询页算未就绪。"""
    if _challenge(html):
        raise TimeoutError('环境质询页未在时限内通过')
    if _login_wall(html, url):
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


def _anchors(html):
    """[(href, 纯文本)]：DOM 结构未实测，导航一律走「正则抽 href + 直接跳转」，不依赖点击。"""
    result = []
    for match in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', str(html or ''), re.S | re.I):
        text = re.sub(r'<[^>]+>', '', match.group(2))
        result.append((match.group(1), re.sub(r'\s+', '', text)))
    return result


def _goto(tab, href):
    # 固定页间间隔：同一个公司最多三次跳转，节奏与旧版逐页读取一致，不追求速度。
    _sleep(PAGE_DELAY)
    tab.get(urljoin(str(tab.url or ''), href), timeout=PAGE_TIMEOUT)
    return _wait_ready(tab)


# ── 公示系统：检索 → 详情（成立日期）→ 年报（参保人数） ──────────────────────────

def _gsxt_search(tab, keyword):
    url = GSXT_ORIGIN + GSXT_SEARCH_PATH + '?' + urlencode({'tab': 'main', 'searchWord': keyword})
    tab.get(url, timeout=PAGE_TIMEOUT)
    html = _wait_ready(tab)
    _gate(html, str(tab.url or ''))
    for href, text in _anchors(html):
        if text == keyword and ('corpdashboard' in href or href.endswith('.html')):
            return href, url
    return None, url


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
    href, search_url = _gsxt_search(tab, keyword)
    if href is None:
        snapshot = _capture(data_dir, 'gsxt', tab)
        note = f'（页面快照：{snapshot}）' if snapshot else ''
        return [], 'partial', f'公示系统检索结果里没有与「{keyword}」完全一致的条目，本轮未采集{note}。', False
    if on_status:
        on_status('登记信息')
    html = _goto(tab, href)
    _gate(html, str(tab.url or ''))
    match = re.search(r'成立日期[^0-9]{0,15}(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})', html)
    if match:
        try:
            founded = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
        except ValueError:
            founded = None
        if founded is not None:
            drafts.append(_signal('company_age', {'years': years_since(founded)},
                                  founded.date().isoformat(), str(tab.url or ''),
                                  f'成立日期取自公示系统登记信息页（{founded.date().isoformat()}），'
                                  f'按同一把算法换算 {years_since(founded)} 年。'))
    if on_status:
        on_status('企业年报')
    annual_href = next((link for link, text in _anchors(html)
                        if 'annual' in link.lower() or '年报' in text or '企业公示' in text), None)
    counts = []
    if annual_href:
        html = _goto(tab, annual_href)
        _gate(html, str(tab.url or ''))
        # 假设（未实测校正）：年报由新到旧排列，counts[0] 是最近一期。实测若相反必须交换两端，
        # 否则趋势会把扩员读成裁员。observed_at 留空：还没把握把「XXXX年度报告」与数字逐行配对。
        counts = [int(item) for item in re.findall(r'城镇职工基本养老保险[^0-9]{0,30}(\d+)\s*人', html)]
        if len(counts) >= 2:
            drafts.append(_signal('insured_headcount_trend', {'before': counts[1], 'after': counts[0]},
                                  '', str(tab.url or ''),
                                  '参保人数取自公示系统年报社保信息（城镇职工基本养老保险），页面排列由新到旧的'
                                  '假设未实测校正；人数变动有多种成因，本条只登记数字。'))
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


# 来源键与 `reliability/sources.py` 的 SIGNAL_SOURCES 对齐。未登记的来源（含 qcc/tyc 付费源）一律 unsupported。
FLOWS = {
    'gsxt': _collect_gsxt,
    'credit_china': _collect_creditchina,
    'court_records': _collect_court,
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
    state = browser_module.status(data_dir)
    if not state.get('installed'):
        return _outcome('unsupported', state.get('message', ''))
    with browser_module.session_lock(data_dir) as acquired:
        if not acquired:
            # 会话被占用就退让，不排队、不抢占——同一时刻只允许一个浏览器任务。
            return _outcome('backoff', '另一个采集任务正占用项目浏览器，请稍后重试。')
        try:
            tab = _open_tab(browser_module, data_dir)
        except Exception:
            return _outcome('failed', '无法接入项目浏览器会话；请确认它仍在运行后重试。')
        if tab is None:
            return _outcome('needs_login', '请先启动项目浏览器并在来源页面完成登录或验证。')
        try:
            drafts, status, message, keep_open = flow(tab, keyword, on_status=on_status, data_dir=data_dir)
        except _LoginWall:
            return _outcome('needs_login', f'{SIGNAL_SOURCES[source_id]["name"]}需要登录；'
                            '已把登录页留在项目浏览器前台，请登录后重新发起本来源采集。')
        except _Blocked:
            return _outcome('needs_verification', f'{SIGNAL_SOURCES[source_id]["name"]}要求人工校验或登录；'
                            '已把该页留在项目浏览器前台，请手动处理后重试。')
        except Exception:
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
        return _outcome(status, message, drafts)
