"""Commercial company-data sites (企查查/天眼查) read from the user's own project Chrome session.

与 `reliability/collectors.py` 同一通路、同一口径：完全复用 `jobscope/browser.py` 的
`status()` / `_connection()` / `session_lock()`，不新起浏览器、不做第二套 profile；
这里的产物是「页面可见的公司卡片」，不是信号——落库归端点管（存成企业信息快照证据）。

诚实状态：两家站点都对无登录会话限流，公开页只给有限字段；下面的搜索地址、链接特征与
字段正则都是**按公开页面通用写法预置的假设，尚未在真实会话里实测**。解析不到就如实报
partial/needs_verification，绝不从卡片文本里「挑一个像注册资本的」编造字段；实测结果记进
`docs/validation/jobscope-company-reliability.md`。这不是绕过登录或风控的通道：遇到校验
就把页面留在前台交给用户，不重试、不换 UA。
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# 单次快查的硬上限：卡片数与页面等待都写死在这里，调用方不接受更激进的值。
MAX_CARDS = 5
PAGE_TIMEOUT = 25
RENDER_DELAY = 1.5

# 登录墙/验证的标志文字。只在「一张卡片都没解析到」时才据此判 needs_verification——
# 两家的页头常驻「登录」按钮，全页文本匹配不能单独作为拦截依据。
VERIFY_MARKERS = ('验证码', '滑动验证', '安全校验', '安全验证', '人机验证', '请完成验证',
                  '请登录', '登录后查看', '扫码登录', '登录/注册', '请输入验证码',
                  'access denied', 'unauthorized')

# 每个字段一行「标签 + 允许的间隔 + 值」。顺序就是卡片摘要的展示顺序，取到什么写什么。
# founded 有两个标签：企查查/天眼查用「成立日期」，爱企查用「成立时间」（2026-11-15 实测）。
FIELD_PATTERNS = (
    ('legal_rep', '法定代表人', r'[^\s，,。；;：:]{2,30}'),
    ('capital', '注册资本', r'[0-9][0-9,.]{0,12}\s*[万亿]?[美元港元人民币元]{0,3}'),
    ('founded', '成立日期', r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?'),
    ('founded', '成立时间', r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?'),
    # 企业类型（如「有限责任公司(国有控股)」）：reliability 的国企性质判定只用这个字段的值。
    ('entity_kind', '企业类型', r'[^\s，,。；;]{2,50}'),
    # 人员规模（如「10000人以上」「100-499人」）：公司口径自述，进快照文本后由信号抽取
    # 与 JD 里的团队自述聚合成 team_size_statement（人员情况维度的评分依据）。
    ('staff_range', '人员规模', r'[\u4e00-\u9fa50-9]{2,15}(?:人以上)?'),
    ('status', '经营状态', r'[\u4e00-\u9fa5A-Za-z]{2,12}'),
    ('credit_code', '统一社会信用代码', r'[0-9A-Z]{18}'),
    ('phone', '电话', r'\d{3,4}-?\d{7,8}'),
    ('email', '邮箱', r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    ('authority', '登记机关', r'[^\s，,。；;：:]{4,40}'),
    ('address', '地址', r'[^\n]{4,80}'),
)
# 标签与值之间允许隔着有限个字符（兜住 <span>/<a> 之类的内联标签被抽成纯文本后的空隙）。
_LABEL_GAP = r'[^：:\n]{0,8}[：:]\s*'

# 在搜索结果页里找公司卡片：由页面自己执行，只抽「详情链接 + 所在容器的纯文本」，
# 不依赖具体卡片结构——两家站点改版时通常只是容器层级变化，锚点与文本仍在。
CARDS_JS = (
    'return (() => { const pattern = new RegExp(arguments[0]);'
    ' const seen = new Set(); const cards = [];'
    ' for (const a of document.querySelectorAll("a[href]")) {'
    '   const href = a.href || "";'
    '   if (!pattern.test(href) || seen.has(href)) continue;'
    '   seen.add(href);'
    '   let node = a;'
    '   for (let i = 0; i < 4 && node.parentElement; i++) {'
    '     node = node.parentElement;'
    '     if ((node.innerText || "").length > 40) break;'
    '   }'
    '   cards.push({href: href, title: (a.innerText || "").trim().slice(0, 120),'
    '               text: (node.innerText || "").slice(0, 1500)});'
    '   if (cards.length >= 10) break;'
    ' }'
    ' return JSON.stringify(cards); })()'
)

# 2026-11-15 实测校准（爱企查）：结果卡片是「没有 href 的 <a>」，公司 ID 在
# data-log-title="item-<pid>" 里，全名在内层锚点的 title 属性上；详情页地址由 pid 拼出。
AQC_CARDS_JS = (
    'return (() => { const seen = new Set(); const cards = [];'
    ' for (const a of document.querySelectorAll("a[data-log-title][title]")) {'
    '   const m = (a.getAttribute("data-log-title") || "").match(/^item-(\\d+)$/);'
    '   if (!m || seen.has(m[1])) continue;'
    '   seen.add(m[1]);'
    '   let node = a;'
    '   for (let i = 0; i < 5 && node.parentElement; i++) {'
    '     node = node.parentElement;'
    '     if ((node.innerText || "").length > 60) break;'
    '   }'
    '   cards.push({href: "https://aiqicha.baidu.com/company_detail_" + m[1],'
    '               title: (a.getAttribute("title") || "").trim().slice(0, 120),'
    '               text: (node.innerText || "").slice(0, 1500)});'
    '   if (cards.length >= 10) break;'
    ' }'
    ' return JSON.stringify(cards); })()'
)


def _outcome(status, message, cards=()):
    """返回键与 connectors.result() 同构（把 jobs/evidence 换成 cards），前端 Badge 不用加分支。"""
    return {'status': status, 'message': message, 'cards': list(cards)}


def _sleep(seconds):
    """渲染等待；单独抽一个函数只为让测试能断言等待确实发生，真实等待没有变化。"""
    time.sleep(seconds)


def _looks_blocked(html):
    text = str(html or '').lower()
    return any(marker.lower() in text for marker in VERIFY_MARKERS)


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
    try:
        return str(tab.html or '')
    except Exception:
        return ''


# 渠道登记表：名称、搜索地址与详情链接特征共用一份事实；link_pattern 由 CARDS_JS 编译，
# cards_js 是站点专属的卡片抽取脚本（爱企查卡片无 href，见 AQC_CARDS_JS 注释）。
SITES = {
    'qcc': dict(name='企查查',
                search=lambda keyword: 'https://www.qcc.com/web/search?' + urlencode({'key': keyword}),
                link_pattern=r'/firm/[^/?#]+'),
    'tyc': dict(name='天眼查',
                search=lambda keyword: 'https://www.tianyancha.com/search?' + urlencode({'key': keyword}),
                link_pattern=r'/(?:company|firm)/[A-Za-z0-9]+'),
    'aiqicha': dict(name='爱企查',
                    search=lambda keyword: 'https://aiqicha.baidu.com/s?' + urlencode({'q': keyword}),
                    cards_js=AQC_CARDS_JS),
}


def _parse_card(item):
    """一张卡片 → {name, url, fields, text}。字段正则全都不中就返回 None，不硬凑。"""
    if not isinstance(item, dict):
        return None
    href, body = str(item.get('href') or ''), str(item.get('text') or '')
    if not href or not body:
        return None
    fields = {}
    for key, label, pattern in FIELD_PATTERNS:
        match = re.search(re.escape(label) + _LABEL_GAP + '(' + pattern + ')', body)
        if match:
            value = match.group(1).strip()
            if value:
                fields[label] = value
    name = str(item.get('title') or '').strip() or next(iter(fields.values()), '')
    if not name:
        return None
    lines = [f'{label}：{value}' for label, value in fields.items()]
    return {'name': name[:120], 'url': href[:2000], 'fields': fields,
            'text': '\n'.join(lines) if lines else body[:600]}


def _read_cards(tab, spec, keyword):
    """打开搜索页 → 抽卡片。返回 (cards, 页面是否摆出登录/验证字样)。"""
    tab.get(spec['search'](keyword), timeout=PAGE_TIMEOUT)
    _sleep(RENDER_DELAY)
    html = _html_of(tab)
    markers = _looks_blocked(html)
    raw = tab.run_js(spec.get('cards_js') or CARDS_JS, spec.get('link_pattern', ''))
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        items = None
    if not isinstance(items, list):
        items = []
    cards = []
    for item in items:
        card = _parse_card(item)
        if card:
            cards.append(card)
        if len(cards) >= MAX_CARDS:
            break
    return cards, markers


def collect(channel, company, *, browser_module, data_dir=None):
    """One page, one pass: bounded cards, no retry, no way around a challenge."""
    spec = SITES.get(channel)
    if spec is None:
        return _outcome('unsupported', '未支持的公司快查渠道。')
    keyword = str(company or '').strip()[:120]
    if not keyword:
        return _outcome('failed', '请输入要查询的公司名称。')
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
            logger.exception('接入项目浏览器失败：%s「%s」', channel, keyword)
            return _outcome('failed', '无法接入项目浏览器会话；请确认它仍在运行后重试。')
        if tab is None:
            return _outcome('needs_login', f'请先启动项目浏览器并打开{spec["name"]}完成登录或验证。')
        try:
            cards, markers = _read_cards(tab, spec, keyword)
        except Exception:
            logger.exception('%s页面读取失败：「%s」', spec['name'], keyword)
            _close(tab)
            return _outcome('failed', f'{spec["name"]}页面读取失败；请确认会话仍在运行后重试。')
        if not cards and markers:
            # 停在验证页时把页面顶到前台交给用户处理，不重试、不绕过。
            try:
                tab.set.activate()
            except Exception:
                pass
            return _outcome('needs_verification',
                            f'{spec["name"]}要求登录或人工校验；已把该页留在项目浏览器前台，请手动完成后重试。')
        _close(tab)
        if not cards:
            return _outcome('partial',
                            f'{spec["name"]}搜索页没有解析出公司卡片；可能没有结果或页面结构已变化，未保存任何内容。')
        return _outcome('completed',
                        f'{spec["name"]}公开页解析到 {len(cards)} 家公司；仅页面可见的有限字段，已按公司名归档。',
                        cards)
