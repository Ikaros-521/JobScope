"""Read official public registries through the user's own project Chrome session.

浏览器层完全复用 `jobscope/browser.py`：同一个 profile 目录、同一份端口记录、同一把 OS 会话锁，
分别走它的 `status()` / `_connection()` / `session_lock()`。这里不新起浏览器、不管端口、
也不做第二套 profile；DrissionPage 只在真的要拿标签页时才导入。

诚实状态：下面两个来源页的 URL 与正则都是**按公开页面结构写的假设，尚未在真实会话里实测**。
实测跑通之前，`sources.py` 里对应来源的 `status` 必须保持 `needs_verification`，
实测结果（环境、请求序列、实际返回）记进 `docs/validation/jobscope-company-reliability.md`。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .rubric import RULES

# 单次任务的硬上限：页面上限与页间等待都写死在这里，调用方不接受更激进的值。
MAX_PAGES = 5
PAGE_DELAY = 2.5
PAGE_TIMEOUT = 25

# 验证码/风控/登录墙的标志文字。命中即停在当前页交给用户，不绕、不重试、不换 UA。
# 这份词表同样没实测过，实测时按来源实际返回补齐（例如公示系统的滑块提示与登录提示）。
VERIFY_MARKERS = ('验证码', '滑动验证', '安全校验', '请完成验证', 'access denied', 'unauthorized',
                  '请登录后查询', '登录后可查看', '扫码登录后查询')

GSXT_ORIGIN = 'https://www.gsxt.gov.cn'
# 假设 1：企业库检索页把「成立日期」以 `2015-04-01` / `2015年04月01日` 两种写法之一呈现，
# 且标签与数字之间只有若干标签/空白（由 [^0-9]{0,12} 兜住）。
REGISTRATION_PATH = '/index.html'
# 假设 2：年报页把历年社保信息平铺在同一张表里，且列表由新到旧排列。
ANNUAL_REPORT_PATH = '/nbgs.html'


def _looks_blocked(html):
    text = str(html or '').lower()
    return any(marker.lower() in text for marker in VERIFY_MARKERS)


def _sleep(seconds):
    """页间停手；单独抽一个函数只为让测试能断言间隔确实发生，真实等待没有变化。"""
    time.sleep(seconds)


def _signal(rule, value, observed_at='', url=''):
    """维度与单位取自 rubric，避免采集器和规则表两处漂移；字段名对齐 models.SignalInput。

    `url` 是这条信号的出处，命名沿用证据层的同名字段：法律红线要求「每条依据都标出来源与
    抓取时间」，所以读不到实际访问地址的信号一律不许带空出处往下落库。
    """
    spec = RULES.get(rule)
    if spec is None or not isinstance(value, dict) or not value:
        return None
    return dict(rule=rule, dimension=spec.dimension, value=value, unit=spec.unit,
                source_class='official', observed_at=observed_at, url=url)


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


def _read_registration(tab, company):
    """成立日期 → company_age（年）。页面没有这个数字就什么都不返回。"""
    url = GSXT_ORIGIN + REGISTRATION_PATH + '?' + urlencode({'keyword': company})
    tab.get(url, timeout=PAGE_TIMEOUT)
    html = tab.html or ''
    if _looks_blocked(html):
        return None
    match = re.search(r'成立日期[^0-9]{0,12}(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', html)
    if not match:
        return None
    try:
        founded = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
    except ValueError:
        # 数字明显不是合法日期：页面结构和假设不一样，不能拿它换算年限。
        return None
    years = round((datetime.now(timezone.utc) - founded).days / 365.25, 1)
    return _signal('company_age', {'years': years}, founded.date().isoformat(), url)


def _read_annual_report(tab, company):
    """年报参保人数 → insured_headcount_trend 的两端。少于两期就无法构成趋势。"""
    url = GSXT_ORIGIN + ANNUAL_REPORT_PATH + '?' + urlencode({'keyword': company})
    tab.get(url, timeout=PAGE_TIMEOUT)
    html = tab.html or ''
    if _looks_blocked(html):
        return None
    counts = [int(item) for item in re.findall(r'城镇职工基本养老保险[^0-9]{0,20}(\d+)\s*人', html)]
    if len(counts) < 2:
        return None
    # 假设 3（最需要实测校正的一条）：公示系统把年报由新到旧排列，所以 counts[0] 是最近一期（after）、
    # counts[1] 是上一期（before）。实测时若页面其实由旧到新，必须交换两端——否则趋势会把扩员读成裁员。
    # observed_at 留空：本期还没有把握把页面上的「XXXX年度报告」和这两个数字逐行配对，不猜。
    return _signal('insured_headcount_trend', {'before': counts[1], 'after': counts[0]}, url=url)


# 来源键与 `reliability/sources.py` 的 SIGNAL_SOURCES 对齐；这里刻意不导入那个模块，
# 免得采集路径依赖还没落地的文件。未登记的来源（含 qcc/tyc 这类付费源）一律 unsupported。
READERS = {
    'gsxt': [('company_age', _read_registration), ('insured_headcount_trend', _read_annual_report)],
}


def collect(source_id, company, *, browser_module, data_dir=None, on_status=None):
    """One sequential pass: bounded pages, fixed spacing, no retry, no way around a challenge."""
    readers = READERS.get(source_id)
    if not readers:
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
        signals, blocked = [], False
        for index, (rule, reader) in enumerate(readers[:MAX_PAGES]):
            if index:
                _sleep(PAGE_DELAY)
            if on_status:
                on_status(rule)
            try:
                item = reader(tab, keyword)
            except Exception:
                item = None
            if item is None:
                # 读不到数字就停手，不反复刷新同一个页面；只有页面确实摆出校验/登录墙才算 needs_verification。
                blocked = _looks_blocked(_html_of(tab))
                break
            item['source'] = source_id
            # 出处 URL 原样透传给调用方：端点落证据时用的就是这个值，这里不兜底成空串。
            signals.append(item)
        if not blocked:
            # 干净收尾才关标签页；停在验证页时把页面留在前台交给用户处理。
            _close(tab)
        if blocked:
            return _outcome('needs_verification', '官方公示页面要求人工校验或登录；已把该页留在项目浏览器前台，请手动处理后重试。', signals)
        got = {item['rule'] for item in signals}
        if len(got) == len(readers):
            return _outcome('completed', f'从 {source_id} 采集到 {len(signals)} 类信号。', signals)
        missing = '、'.join(rule for rule, _ in readers if rule not in got)
        message = f'从 {source_id} 采集到 {len(signals)} 类信号（未取到：{missing}）。' if signals \
            else f'{source_id} 页面没有出现可解析的数字，本轮未采集到信号（未取到：{missing}）。'
        return _outcome('partial', message, signals)
