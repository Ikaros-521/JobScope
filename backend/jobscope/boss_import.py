"""Boss 岗位「捕获篮」导入：当前页抽取 + 链接粘贴，只读用户会话里已渲染的页面。

设计见 docs/design/boss-import.md。三条纪律：
1. 只读不仿——抽取仅读已渲染 DOM / 解析用户粘贴的文本，绝不代替用户向 Boss 发请求；
2. 幂等——落库走 save_job(source='boss')，与搜索采集按 encryptJobId 归并为同一条岗位；
3. 校准——DOM 类名是启发式的，抽不到就如实报错并引导走链接粘贴，不硬造数据。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from . import browser
from .connectors import CITY_CODES, DEFAULT_DATA, loads_payload, map_boss, plain
from .job_details import persist_company_statement, read_detail_on_tab
from .repository import digest, now

logger = logging.getLogger(__name__)

# 岗位在 Boss 站内的唯一标识；与 connectors.map_boss 同一口径校验。
ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{4,60}')
LINK_PATTERN = re.compile(r'/job_detail/([A-Za-z0-9_-]{4,60})\.html')
SALARY_PATTERN = re.compile(r'\d{1,4}[-–~]\d{1,4}[Kk](?:·\d{1,2}薪)?|\d{1,4}[Kk](?:以下|以上)?(?:·\d{1,2}薪)?')
TITLE_PATTERN = re.compile(r'[【「\[]([^】」\]]{2,80})[】」\]]')
# 单次粘贴上限：分享文本可能整页粘贴，超出的链接提示分批。
MAX_LINKS = 200

# 列表页卡片抽取：锚点定位卡片容器，按类名启发式取字段。已按 2026-11-20 实测校准两点：
# 1) 推荐中心（收藏页）右侧有平台推送的 sider-recommend 栏，卡片是 <a> 包 <li> 的异形结构，
#    必须整栏排除，否则平台推荐岗会混进用户的收藏/搜索结果（数据污染）；
# 2) 收藏卡是 li.item-boss，干净字段在 .job-name-text（岗位名）、.company-info b（公司名）、
#    .location（地点），启发式匹配不到时按旧正则兜底。Boss 改版时类名漂移，抽不到字段就少填、
#    Python 侧按城市字典补地点；整体抽不到卡片时明确报错并指引用链接粘贴。
CARDS_JS = r'''return (() => {
  const clsOf = (el) => { const c = el && el.className; return typeof c === 'string' ? c : (c && c.baseVal) || ''; };
  const pick = (root, rx) => {
    for (const el of root.querySelectorAll('[class]')) {
      if (rx.test(clsOf(el))) return (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    }
    return '';
  };
  const one = (root, sel) => {
    const el = root.querySelector(sel);
    return el ? (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160) : '';
  };
  const grab = (root) => ({
    title: one(root, '[class*="job-name-text"]') || pick(root, /(^|[\s-])(job-name|job-title)/),
    // 收藏卡的薪资在 p.gray 里（与公司信息行同名，类名抓不到），退而取卡片文本里第一段 K 型薪资；
    // 「1000-9999人」这类区间不带 K 不会误中。
    salary: pick(root, /salary/) || ((root.textContent || '').match(/\d{1,4}[-–~]\d{1,4}[Kk](?:·\d{1,2}薪)?/) || [''])[0],
    company: one(root, '.company-info b') || one(root, '[class*="company-name"]') || pick(root, /company-name|company-info/),
    area: pick(root, /job-area|job-city|job-location|(^|[\s-])location/).replace(/[\[\]【】]/g, ''),
  });
  const cards = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/job_detail/"]')) {
    const match = (a.getAttribute('href') || '').match(/\/job_detail\/([A-Za-z0-9_-]+)\.html/);
    if (!match || seen.has(match[1])) continue;
    if (a.closest('[class*="sider-recommend"]')) continue;
    seen.add(match[1]);
    // 卡片容器：锚点在 li 里就用该 li（搜索页 li.job-card-wrapper、收藏页 li.item-boss 都是一卡一 li），
    // 绝不能再向上走——把列表容器当卡片会让第一张卡的字段错配给所有卡（2026-11-20 实测踩坑）；
    // 锚点不在 li 里的异形卡片才走旧的三层上溯。
    let card = a.closest('li') || a;
    if (card === a) {
      for (let i = 0; i < 3; i++) {
        const parent = card.parentElement;
        if (!parent) break;
        card = parent;
        if (/job-card|job-box|search-job|job-list/i.test(clsOf(card)) || card.tagName === 'LI') break;
      }
    }
    let fields = grab(card);
    if (!fields.salary && !fields.title && card.parentElement) {
      // 类名没匹配上（改版/异形卡片）：再上溯一层重试一次，容器更大通常就能罩住整卡。
      fields = grab(card.parentElement);
    }
    const tags = [...card.querySelectorAll('[class*="tag"]')]
      .map(e => (e.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(t => t && t.length <= 20).slice(0, 8);
    cards.push({id: match[1], link_text: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
      text: (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300), tags, ...fields});
  }
  return {page_url: location.href, page_title: document.title.slice(0, 200), cards};
})()'''

# 捕获篮按数据目录分区：内存为缓存（速度），data_dir 下 basket.json 为持久层（跨重启保留，
# 服务重启不清空；「清空篮子」才是显式清除）。所有访问持 _basket_lock，落盘走原子替换。
_baskets: dict[str, dict] = {}
_basket_lock = threading.Lock()
_save_lock = threading.Lock()
BASKET_FILE = 'boss_import_basket.json'


def _basket_key(data_dir=None):
    return str(Path(data_dir or DEFAULT_DATA).resolve())


def _basket_path(data_dir=None):
    return Path(data_dir or DEFAULT_DATA) / BASKET_FILE


def _store(data_dir=None):
    """取该数据目录的篮子分区；首次接触时从磁盘懒加载（多 app 实例/测试互不串味）。"""
    key = _basket_key(data_dir)
    if key not in _baskets:
        try:
            data = json.loads(_basket_path(data_dir).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = None
        items = data.get('items') if isinstance(data, dict) else None
        _baskets[key] = {str(entry['external_id']): dict(entry) for entry in items
                         if isinstance(entry, dict) and str(entry.get('external_id') or '')} if isinstance(items, list) else {}
    return _baskets[key]


def _save_basket(data_dir=None):
    """变更后落盘；失败不阻塞主流程（篮子仍在内存可用，下次变更会再尝试）。"""
    with _save_lock:
        try:
            path = _basket_path(data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({'items': list(_store(data_dir).values()), 'saved_at': now()}, ensure_ascii=False)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(payload, encoding='utf-8')
            temporary.replace(path)
        except OSError:
            pass


def job_id_for(external_id):
    """与 repository.save_job 同一口径的岗位主键，用于篮子的「已在库」标记。"""
    return 'J-' + digest('boss:' + str(external_id))[:24]


def _canonical_url(external_id):
    # 不带任何 query：job_details.valid_detail_url 要求详情链接干净（无 securityId/lid 等时效参数）。
    return f'https://www.zhipin.com/job_detail/{external_id}.html'


def _listing_description(title, company, location, salary, tags):
    lines = [title]
    if company:
        lines.append(f'公司/招聘品牌：{company}')
    if location:
        lines.append(f'地点：{location}')
    if salary:
        lines.append(f'招聘薪资：{salary}')
    if tags:
        lines.append('、'.join(tags))
    lines.append('从 Boss 页面/链接捕获的列表摘要；完整岗位职责尚未读取，可「补全详情」。')
    return '\n'.join(lines)


def _card_to_item(card, imported_via, captured_from):
    """DOM 卡片 → 篮子条目。字段缺失就留空（补全详情会修），不猜值。"""
    identifier = str(card.get('id') or '')
    if not ID_PATTERN.fullmatch(identifier):
        return None
    title = (plain(card.get('title')) or plain(card.get('link_text')))[:300]
    company = plain(card.get('company'))[:300]
    salary = plain(card.get('salary'))[:300]
    if not title and not company and not salary:
        # 三个字段全空的锚点不是岗位卡（页脚/隐藏模板之类），跳过。
        return None
    tags = [plain(t) for t in card.get('tags', []) if isinstance(t, str) and plain(t)][:8]
    location = plain(card.get('area')) or _city_in(str(card.get('text') or '') + str(card.get('area') or ''))
    fallback_title = f'Boss岗位 {identifier[:8]}'
    return {
        'external_id': identifier,
        'title': title or fallback_title,
        'company': company,
        'location': location[:300],
        'salary': salary,
        'url': _canonical_url(identifier),
        'tags': tags,
        'imported_via': imported_via,
        'captured_from': captured_from[:500],
        'captured_at': now(),
        'description': _listing_description(title or fallback_title, company, location, salary, tags),
        'description_status': 'listing',
    }


def _city_in(text):
    """按内置城市字典从卡片文本里找地名；字典顺序即核实优先级。"""
    return next((name for name in CITY_CODES if name in text), '')


def parse_links(text):
    """从粘贴文本解析 job_detail 链接；App 分享行的【岗位】/薪资/城市顺带提取。"""
    text = text or ''
    items, seen, overflow = [], set(), False
    previous_end = 0
    for match in LINK_PATTERN.finditer(text):
        identifier = match.group(1)
        # 元数据段以「前一条链接」或 200 字符为界：App 分享常是元数据行+链接行（允许跨行），
        # 但绝不能越过上一条链接把它的【标题】串味到下一条。
        boundary = max(match.start() - 200, previous_end)
        prefix = text[boundary:match.start()]
        previous_end = match.end()
        if identifier in seen:
            continue
        seen.add(identifier)
        if len(items) >= MAX_LINKS:
            overflow = True
            break
        title_match = TITLE_PATTERN.search(prefix)
        title = plain(title_match.group(1)) if title_match else ''
        salary_match = SALARY_PATTERN.search(prefix)
        salary = salary_match.group(0) if salary_match else ''
        city = _city_in(prefix)
        items.append({
            'external_id': identifier,
            'title': title[:300] or f'Boss岗位 {identifier[:8]}',
            'company': '',
            'location': city,
            'salary': salary,
            'url': _canonical_url(identifier),
            'tags': [],
            'imported_via': 'link',
            'captured_from': '粘贴链接',
            'captured_at': now(),
            'description': _listing_description(title or f'Boss岗位 {identifier[:8]}', '', city, salary, []),
            'description_status': 'listing',
        })
    return items, overflow


def absorb_remote(cards, page_url, data_dir=None):
    """油猴脚本推送入口：清洗任意浏览器送来的卡片并入篮（imported_via='script'）。

    信任边界：external_id 与 URL 一律服务端重建（canonical 无参链接），文本字段走 plain()
    与长度截断；page_url 必须是 zhipin.com 页面，否则整包拒绝——防止脚本被塞进别站页面
    后把无关数据推进资料库。
    """
    try:
        host = urlsplit(str(page_url or '')).hostname or ''
    except ValueError:
        host = ''
    if host not in ('www.zhipin.com', 'zhipin.com') and not host.endswith('.zhipin.com'):
        return {'status': 'failed', 'added': 0, 'updated': 0, 'message': '仅接受来自 zhipin.com 页面的推送。'}
    captured_from = f'油猴脚本 · {str(page_url)[:400]}'
    items = [item for item in (_card_to_item(card, 'script', captured_from) for card in cards if isinstance(card, dict)) if item]
    if not items:
        return {'status': 'failed', 'added': 0, 'updated': 0, 'message': '推送内容中没有有效的岗位卡片。'}
    outcome = basket_add(items, data_dir)
    with _basket_lock:
        total = len(_store(data_dir))
    return {'status': 'ok', 'added': outcome['added'], 'updated': outcome['updated'], 'total': total,
            'message': f'已入篮：新增 {outcome["added"]}、合并 {outcome["updated"]}；篮子共 {total} 条待确认。'}


def basket_add(items, data_dir=None):
    """入篮合并：新捕获的非空字段覆盖旧值，imported_via 保留首次发现通道，条目按捕获顺序排列。"""
    added = updated = 0
    with _basket_lock:
        store = _store(data_dir)
        for item in items:
            key = item['external_id']
            if key in store:
                previous = store[key]
                merged = {**previous, **{k: v for k, v in item.items() if v}}
                merged['imported_via'] = previous['imported_via']
                store[key] = merged
                updated += 1
            else:
                store[key] = dict(item)
                added += 1
    _save_basket(data_dir)
    return {'added': added, 'updated': updated}


def _library_ids(repo, job_ids):
    """批量查这批岗位主键里哪些已在库（单连接分批，替代逐条 repo.job() 的 N 次连接）。"""
    wanted = list(dict.fromkeys(job_ids))
    known = set()
    if wanted:
        with repo.connection() as conn:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                placeholders = ','.join('?' * len(chunk))
                known.update(row['id'] for row in conn.execute(f'SELECT id FROM jobs WHERE id IN ({placeholders})', chunk))
    return known


def basket_view(repo):
    with _basket_lock:
        items = [dict(entry) for entry in _store(repo.data_dir).values()]
    if items:
        # 「已在库」标记：逛即采集轮询会高频调用，必须走批量查询。
        known = _library_ids(repo, [job_id_for(item['external_id']) for item in items])
        for item in items:
            item['in_library'] = job_id_for(item['external_id']) in known
    return {'items': items, 'total': len(items)}


def basket_clear(data_dir=None):
    with _basket_lock:
        store = _store(data_dir)
        removed = len(store)
        store.clear()
    _save_basket(data_dir)
    return {'cleared': removed}


def _to_job(item):
    """篮子条目 → save_job 载荷：来源恒为 boss（与搜索采集归并），导入溯源留在 payload。"""
    return {k: item[k] for k in ('external_id', 'title', 'company', 'location', 'salary', 'url',
                                 'description', 'description_status', 'tags', 'imported_via',
                                 'captured_from', 'captured_at')}


def confirm(repo, ids=None, auto_star=False):
    """确认入库：逐条 save_job(source='boss') 幂等落库；可选中加星；入库条目移出篮子。

    导入时顺带读取过详情的条目带公司自述证据，入库时与「补全详情」同一通路落证据与规模信号。
    投递记录通道的条目带 applied_at：入库后直接回填「已投递」（真实投递时刻），不必再手工标。
    """
    wanted = set(ids) if ids else None
    with _basket_lock:
        store = _store(repo.data_dir)
        selected = {key: dict(entry) for key, entry in store.items() if wanted is None or key in wanted}
    if not selected:
        return {'status': 'empty', 'message': '捕获篮为空或没有选中的条目。', 'imported': 0, 'jobs': []}
    saved = [repo.save_job(_to_job(entry), 'boss') for entry in selected.values()]
    for entry, job in zip(selected.values(), saved):
        persist_company_statement(repo, job, entry.get('company_evidence'))
    if auto_star:
        repo.update_jobs([job['id'] for job in saved], {'starred': True})
    applied = sum(1 for entry in selected.values() if 'applied_at' in entry)
    if applied:
        repo.sync_applied([(job['id'], entry.get('applied_at') or '')
                           for entry, job in zip(selected.values(), saved) if 'applied_at' in entry])
    with _basket_lock:
        store = _store(repo.data_dir)
        for key in selected:
            store.pop(key, None)
    _save_basket(repo.data_dir)
    complete = sum(1 for entry in selected.values() if entry.get('description_status') == 'complete')
    logger.info('捕获篮确认入库：%s 条（加星=%s 详情完整=%s 投递记录=%s）',
                len(saved), bool(auto_star), complete, applied)
    tail = '详情已随导入读取，可直接评估' if complete else '可继续「补全详情」读取完整 JD'
    applied_note = f'，其中 {applied} 条按 Boss 投递记录标记为已投递' if applied else ''
    return {'status': 'ok', 'imported': len(saved), 'jobs': saved,
            'message': f'已导入 {len(saved)} 条 Boss 岗位{applied_note}；重复岗位自动归并，{tail}。'}


def list_tabs(data_dir=None):
    """枚举项目浏览器里打开的 Boss 标签页（按 URL 过滤），供「捕获当前页」选择。"""
    port = browser._connection(data_dir)
    if not port:
        return {'status': 'needs_browser', 'tabs': [], 'message': '项目浏览器未运行；请先在「数据与采集」打开专用浏览器并登录 Boss。'}
    if not browser._lock.acquire(blocking=False):
        return {'status': 'backoff', 'tabs': [], 'message': '浏览器任务正在运行，请稍后重试。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'tabs': [], 'message': '另一个进程正在使用项目浏览器，请稍后重试。'}
            from DrissionPage import Chromium
            tabs = []
            for tab in Chromium(f'127.0.0.1:{port}').get_tabs():
                url = str(getattr(tab, 'url', '') or '')
                if 'zhipin.com' not in url:
                    continue
                tabs.append({'id': str(getattr(tab, 'tab_id', '') or ''),
                             'url': url[:500], 'title': str(getattr(tab, 'title', '') or '')[:200]})
            return {'status': 'ok', 'tabs': tabs,
                    'message': '' if tabs else '项目浏览器里没有打开的 Boss 页面；先在浏览器中打开 zhipin.com 的岗位列表或收藏页。'}
    except Exception:
        return {'status': 'failed', 'tabs': [], 'message': '读取浏览器标签页失败；请确认项目会话仍在运行。'}
    finally:
        browser._lock.release()


def capture_page(data_dir=None, tab_id=''):
    """读取所选标签页当前渲染的岗位卡片并入篮。只读 DOM，不导航、不刷新、不打扰用户浏览。"""
    port = browser._connection(data_dir)
    if not port:
        return {'status': 'needs_browser', 'added': 0, 'message': '项目浏览器未运行；请先打开专用浏览器并登录 Boss。'}
    if not browser._lock.acquire(blocking=False):
        return {'status': 'backoff', 'added': 0, 'message': '浏览器任务正在运行，请稍后重试。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'added': 0, 'message': '另一个进程正在使用项目浏览器，请稍后重试。'}
            from DrissionPage import Chromium
            target = None
            for tab in Chromium(f'127.0.0.1:{port}').get_tabs():
                if str(getattr(tab, 'tab_id', '') or '') == tab_id and 'zhipin.com' in str(getattr(tab, 'url', '') or ''):
                    target = tab
                    break
            if target is None:
                return {'status': 'failed', 'added': 0, 'message': '未找到该 Boss 标签页（可能已关闭）；请刷新标签页列表重试。'}
            snapshot = target.run_js(CARDS_JS)
            cards = snapshot.get('cards') if isinstance(snapshot, dict) else None
            if not isinstance(cards, list) or not cards:
                return {'status': 'failed', 'added': 0,
                        'message': '该页面没有解析到岗位卡片；可能不在岗位列表页或页面结构已变化，可用「粘贴链接」兜底。'}
            page_url = str(snapshot.get('page_url') or '')
            items = [item for item in (_card_to_item(card, 'current_page', page_url) for card in cards if isinstance(card, dict)) if item]
            if not items:
                return {'status': 'failed', 'added': 0,
                        'message': '解析到链接但没有可用的岗位字段；页面结构可能已变化，可用「粘贴链接」兜底。'}
            outcome = basket_add(items, data_dir)
            logger.info('捕获当前页完成：卡片=%s 有效=%s 新增=%s 合并=%s', len(cards), len(items), outcome['added'], outcome['updated'])
            note = '；存在链接但未解析出有效卡片，可能混入非岗位链接' if len(items) < len(cards) else ''
            return {'status': 'ok', 'added': outcome['added'], 'updated': outcome['updated'],
                    'message': f'本页捕获 {len(items)} 条岗位：新增 {outcome["added"]}、合并 {outcome["updated"]}{note}。'}
    except Exception:
        logger.exception('捕获当前页失败 tab=%s', tab_id)
        return {'status': 'failed', 'added': 0, 'message': '读取页面失败；请确认项目浏览器仍在运行。'}
    finally:
        browser._lock.release()


# ---- P1：逛即采集（浏览的副产品）------------------------------------------------
# 现有 listen 模式的常驻化：只接住用户浏览 tab 自己发出的 joblist.json 响应（带着用户的
# 定位/筛选/排序），逐包解析入篮。不持锁收包——listen 队列只属于这些 tab，采集/详情任务
# 用各自的新 tab，互不干扰；只在枚举 tab 与启停 listen 时短暂拿 browser._lock。

BROWSE_LISTEN = 'wapi/zpgeek/search/joblist.json'

_browse_lock = threading.Lock()
_browse_stop = threading.Event()
_browse_thread: threading.Thread | None = None
_browse_state = {'running': False, 'captured': 0, 'message': ''}


def _set_browse(**fields):
    with _browse_lock:
        _browse_state.update(fields)


def _item_from_job(job, via, captured_from):
    """map_boss 的 wapi 岗位 → 篮子条目：逛即采集与搜索采集共用同一套字段映射。"""
    identifier = str(job.get('external_id') or '')
    if not ID_PATTERN.fullmatch(identifier):
        return None
    title = str(job.get('title') or '')[:300] or f'Boss岗位 {identifier[:8]}'
    return {
        'external_id': identifier,
        'title': title,
        'company': str(job.get('company') or '')[:300],
        'location': str(job.get('location') or '')[:300],
        'salary': str(job.get('salary') or '')[:300],
        'url': _canonical_url(identifier),
        'tags': [t for t in (job.get('tags') or []) if isinstance(t, str)][:8],
        'imported_via': via,
        'captured_from': captured_from[:500],
        'captured_at': now(),
        'description': str(job.get('description') or ''),
        'description_status': 'listing',
    }


def _absorb_browse_packet(packet, data_dir=None):
    data = loads_payload(packet.response.body) if getattr(packet, 'response', None) is not None else None
    if not isinstance(data, dict):
        return
    outcome = map_boss(data, limit=30, city='')
    if outcome.get('status') != 'completed':
        # 登录失效/验证页如实记录；包级噪音不刷掉上一条有用提示。
        message = outcome.get('message') or ''
        if message and message != _browse_state.get('message'):
            _set_browse(message=message)
        return
    captured_from = '逛即采集'
    try:
        query = (parse_qs(urlsplit(str(getattr(packet, 'url', '') or '')).query).get('query') or [''])[0]
        if query:
            captured_from = f'逛即采集：搜索「{query[:60]}」'
    except Exception:
        pass
    items = [item for item in (_item_from_job(job, 'browse', captured_from) for job in outcome.get('jobs') or []) if item]
    if not items:
        return
    added = basket_add(items, data_dir)
    _set_browse(captured=_browse_state['captured'] + added['added'] + added['updated'])


def _browse_worker(data_dir):
    stop = _browse_stop
    live_tabs = []
    try:
        while not stop.is_set():
            port = browser._connection(data_dir)
            if not port:
                _set_browse(running=False, message='项目浏览器已关闭，逛即采集停止；重新打开并登录 Boss 后可再次开启。')
                return
            tabs = []
            with browser._lock:
                with browser.session_lock(data_dir) as acquired:
                    if acquired:
                        try:
                            from DrissionPage import Chromium
                            tabs = [tab for tab in Chromium(f'127.0.0.1:{port}').get_tabs()
                                    if 'zhipin.com' in str(getattr(tab, 'url', '') or '')]
                            for tab in tabs:
                                try:
                                    tab.listen.start(BROWSE_LISTEN)
                                except Exception:
                                    pass
                        except Exception:
                            tabs = []
            if stop.is_set():
                break
            if not tabs:
                _set_browse(message='项目浏览器里没有打开的 Boss 页面；在浏览器打开 zhipin.com 岗位列表后自动开始捕获。')
                stop.wait(5)
                continue
            live_tabs = tabs
            for tab in tabs:
                if stop.is_set():
                    break
                try:
                    packet = tab.listen.wait(count=1, timeout=3, raise_err=False)
                except Exception:
                    packet = None
                for part in (packet if isinstance(packet, list) else [packet]):
                    if part:
                        try:
                            _absorb_browse_packet(part, data_dir)
                        except Exception:
                            pass
    finally:
        with browser._lock:
            for tab in live_tabs:
                try:
                    tab.listen.stop()
                except Exception:
                    pass
        _set_browse(running=False)


def browse_start(data_dir=None):
    global _browse_thread
    with _browse_lock:
        if _browse_state['running'] and _browse_thread and _browse_thread.is_alive():
            return {**_browse_state, 'status': 'ok', 'message': '逛即采集已在运行。'}
        # 逛即采集的前提是「有浏览器可逛」；不自动拉起——用户接下来本来就要亲自去浏览。
        if not browser._connection(data_dir):
            return {'status': 'needs_browser', 'running': False, 'captured': _browse_state['captured'],
                    'message': '项目浏览器未运行；请先打开专用浏览器并登录 Boss，再开启逛即采集。'}
        _browse_stop.clear()
        _browse_state.update(running=True,
                             message='逛即采集已开启：在项目浏览器里浏览 Boss 岗位列表会自动入篮（只接住页面自己发出的响应，不代替你发请求）。')
        _browse_thread = threading.Thread(target=_browse_worker, args=(data_dir,), daemon=True)
        _browse_thread.start()
    return {**_browse_state, 'status': 'ok'}


def browse_stop():
    global _browse_thread
    _browse_stop.set()
    thread = _browse_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=5)
    with _browse_lock:
        return {**_browse_state, 'status': 'ok', 'message': '逛即采集已停止；已捕获的条目保留在篮子里。'}


def browse_status():
    with _browse_lock:
        state = dict(_browse_state)
    state['status'] = 'ok' if state['running'] else 'idle'
    if not state['running'] and not state['message']:
        state['message'] = '逛即采集未开启；开启后浏览 Boss 列表会自动入篮。'
    return state


# ---- P1：一键收藏导入-------------------------------------------------------------
# 收藏页 = 推荐中心（web/geek/recommend）的第 4 个页签，2026-11-20 首跑实测校准：
# 直接进 FAVORITE_PRIMARY；打不开再回个人中心点「感兴趣」页签，最后才做链接发现与历史候选兜底。
# 入口再改版时把实测 URL 换进 FAVORITE_PRIMARY 即可。
#
# 个人中心（2026-11-24 受控实验校准，output/diag_personal_tab_state.py）：web/geek/recommend?tab=2，
# 顶部页签是 JS 切换的 span（ka=personal_top_added 沟通过 / personal_top_submitted 已投递 /
# personal_top_interview 面试 / personal_top_interest 感兴趣），不是链接。实测三条：
# 1) 旧个人中心地址 /web/user/ 现已跳首页，不再是可用入口（旧兜底因此一直是死路）；
# 2) URL 的 tag 只对部分页签有效：tag=1 → 已投递是可靠深链（能覆盖「上次停留的页签」）；
#    tag=2 / tag=4 不切页签，落到上次停留的那个——不能当深链用；
# 3) 点「感兴趣」页签会跳到 tab=4&sub=1&tag=4（即 FAVORITE_PRIMARY）。所以兜底与同步都按 ka 点页签。

USER_CENTER = 'https://www.zhipin.com/web/geek/recommend?tab=2&page=1'
FAVORITE_PRIMARY = 'https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&page=1&tag=4'
FAVORITE_CANDIDATES = (
    FAVORITE_PRIMARY,
    'https://www.zhipin.com/web/user/collect.html',
    'https://www.zhipin.com/web/user/favorite',
    'https://www.zhipin.com/web/geek/favorite',
)
LOGIN_MARKS = ('/login', '/passport', 'login.zhipin')
FAVORITE_LINK_JS = r'''return (() => {
  const links = [...document.querySelectorAll('a[href]')];
  const hit = links.find(a => /收藏|感兴趣/.test(a.textContent || ''));
  return hit ? hit.href : '';
})()'''
SCROLL_BOTTOM_JS = 'window.scrollTo(0, document.documentElement.scrollHeight)'


def _login_wall(tab):
    url = str(getattr(tab, 'url', '') or '')
    title = str(getattr(tab, 'title', '') or '')
    if any(mark in url for mark in LOGIN_MARKS):
        return True
    return '验证' in title or 'Verification' in title


def _snapshot_cards(tab, tries=5, interval=2):
    """Boss 是 SPA：导航完成后卡片要约 2-3 秒才异步渲染，一进页面就读 DOM 只会拿到空。

    轮询读 CARDS_JS 直到出现卡片（首读 + 4 次×2s，上限 ~8s）；超时如实返回最后的空快照，
    由调用方按「没解析到」处理，不硬造数据。
    """
    snapshot = {}
    for attempt in range(tries):
        try:
            snapshot = tab.run_js(CARDS_JS)
        except Exception:
            snapshot = {}
        if isinstance(snapshot, dict) and snapshot.get('cards'):
            return snapshot
        if attempt < tries - 1:
            time.sleep(interval)
    return snapshot if isinstance(snapshot, dict) else {}


def _personal_tab_js(ka):
    """个人中心顶部页签是 span（非链接），按 ka 点选：'ok'=已是当前页签，'clicked'=已点，'missing'=页面上没有。"""
    return ('const cur = document.querySelector(\'.user-jobs-tab-nav span.current\'); '
            f'if (cur && cur.getAttribute("ka") === "{ka}") return "ok"; '
            f'const target = document.querySelector(\'.user-jobs-tab-nav span[ka="{ka}"]\'); '
            'if (!target) return "missing"; target.click(); return "clicked";')


def _select_personal_tab(tab, ka, tries=5):
    """确保个人中心停在指定页签；返回 'ok' / 'clicked'，''=重试后页面上始终没有该页签。

    导航完成瞬间页面常在二次跳转（run_js 抛 ContextLost），页签栏也要等异步渲染——所以带重试。
    个人中心默认停在上次看的页签，深链失效时只能靠这一步把页面带到正确的列表。
    """
    for attempt in range(tries):
        try:
            state = tab.run_js(_personal_tab_js(ka))
        except Exception:
            state = ''
        if state in ('ok', 'clicked'):
            return state
        if attempt < tries - 1:
            time.sleep(2)
    return ''


def _open_favorites(tab):
    """定位收藏列表页并确认有岗位卡片；返回实际页面 URL，''=没找到，'login'=被登录墙拦住。"""
    def probe(candidate):
        try:
            tab.get(candidate, timeout=20)
        except Exception:
            return ''
        if _login_wall(tab):
            return 'login'
        snapshot = _snapshot_cards(tab)
        if snapshot.get('cards'):
            return str(snapshot.get('page_url') or candidate)
        return ''

    # 实测收藏入口直进（推荐中心 tab=4）；打不开再回个人中心点「感兴趣」页签，最后做链接发现与历史候选兜底。
    outcome = probe(FAVORITE_PRIMARY)
    if outcome:
        return outcome
    # 兜底一：进个人中心点「感兴趣」页签（2026-11-24 实测可达收藏页）；点完必须确认当前页签
    # 确实是「感兴趣」才采信卡片——个人中心默认停在上次的页签（已投递/沟通过），误采会把别的列表当收藏入篮。
    tab.get(USER_CENTER, timeout=20)
    if _login_wall(tab):
        return 'login'
    if _select_personal_tab(tab, 'personal_top_interest'):
        snapshot = _snapshot_cards(tab)
        if snapshot.get('cards') and _select_personal_tab(tab, 'personal_top_interest') == 'ok':
            return str(snapshot.get('page_url') or USER_CENTER)
    # 兜底二：个人中心链接发现与历史候选。
    for candidate in [str(tab.run_js(FAVORITE_LINK_JS) or '')] + list(FAVORITE_CANDIDATES[1:]):
        if not candidate.startswith('https://www.zhipin.com'):
            continue
        outcome = probe(candidate)
        if outcome:
            return outcome
    return ''


def _page_url(url, page):
    """把列表页 URL 的 page 参数换到指定页；URL 不带 page 参数返回 ''（调用方退回滚动加载）。"""
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if 'page' not in query:
            return ''
        query['page'] = [str(page)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
    except ValueError:
        return ''


def _library_detail_states(repo, external_ids):
    """批量查岗位在库里的详情状态：返回 {job_id: (description_status, detail_transport)}。

    去重闸的数据面：库里已有完整详情（browser_dom）的岗位，导入时整条跳过，不再入篮重读。
    """
    states = {}
    wanted = [job_id_for(x) for x in external_ids]
    if not wanted:
        return states
    with repo.connection() as conn:
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            placeholders = ','.join('?' * len(chunk))
            rows = conn.execute(f'SELECT payload FROM jobs WHERE id IN ({placeholders})', chunk)
            for row in rows:
                data = json.loads(row['payload'])
                states[data['id']] = (data.get('description_status') or '', data.get('detail_transport') or '')
    return states


def import_favorites(repo, task, data_dir=None, max_pages=10):
    """一键收藏导入：定位收藏页 → 逐页抽卡入篮 → page 参数翻页 → 顺带读取岗位详情。

    详情顺带读取（2026-11-20）：新开 tab 逐条走 job_details 通路（校验/登录墙/下架识别同款），
    完整 JD 与公司自述随捕获一起进篮，确认入库即落库，无需再跑「补全详情」。
    去重两道闸：库里已有完整详情（browser_dom）的岗位整条跳过（不入篮不重读）；
    篮子里已带完整详情的（上次中断遗留）跳过详情读取。下架岗位保留列表摘要并计数。
    """
    port = browser._connection(data_dir)
    if not port:
        # 一键承诺：浏览器没开就自动拉起（与采集入口同一模式）。
        started = browser.start(data_dir, 'boss')
        port = browser._connection(data_dir)
        if not port:
            return {'status': 'needs_browser', 'message': started.get('message') or '请先启动项目浏览器并登录 Boss。'}
    if not browser._lock.acquire(blocking=False):
        return {'status': 'backoff', 'message': '浏览器任务正在运行，请等待完成。'}
    tab = None
    detail_tab = None
    keep_detail_tab = False
    outcome = {'status': 'failed', 'message': '收藏导入未完成。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'message': '另一个进程正在使用项目浏览器。'}
            from DrissionPage import Chromium
            chromium = Chromium(f'127.0.0.1:{port}')
            tab = chromium.new_tab()
            url = _open_favorites(tab)
            if url == 'login':
                browser._front(tab)
                outcome = {'status': 'needs_login', 'message': '收藏页要求登录；请在项目浏览器完成登录后重试，或手动打开收藏页用「捕获当前页」。'}
                return outcome
            if not url:
                outcome = {'status': 'failed',
                           'message': f'没有找到收藏页入口：请在项目浏览器手动打开 {FAVORITE_PRIMARY}，然后用「捕获当前页」导入（收藏夹也可能是空的）。'}
                return outcome
            seen: set[str] = set()
            skipped = 0
            captured: list[dict] = []
            pages = 0
            filled = 0
            stall_note = ''
            current = url
            while pages < max_pages:
                tab.run_js(SCROLL_BOTTOM_JS)
                snapshot = _snapshot_cards(tab)
                cards = snapshot.get('cards')
                page_items = [item for item in (_card_to_item(card, 'favorite', str(snapshot.get('page_url') or current))
                                                for card in (cards or []) if isinstance(card, dict)) if item]
                fresh = [item for item in page_items if item['external_id'] not in seen]
                seen.update(item['external_id'] for item in fresh)
                if fresh:
                    # 去重第一道闸：库里已有完整详情、或篮子里已带完整详情（上次中断遗留）的
                    # 岗位整条跳过——收藏越攒越多时，重复导入不再把时间花在重复提取上。
                    states = _library_detail_states(repo, [item['external_id'] for item in fresh])
                    store = _store(data_dir)
                    todo = []
                    for item in fresh:
                        state = states.get(job_id_for(item['external_id']))
                        existing = store.get(item['external_id'])
                        if ((state and state[0] == 'complete' and state[1] == 'browser_dom')
                                or (existing and existing.get('description_status') == 'complete')):
                            skipped += 1
                        else:
                            todo.append(item)
                    basket_add(todo, data_dir=data_dir)
                    captured.extend(todo)
                    filled += 1
                pages += 1
                logger.info('收藏导入进度：第 %s 页 新捕获=%s 跳过已在库=%s 累计=%s',
                            pages, len(fresh), skipped, len(seen))
                task.update(count=len(seen) - skipped,
                            message=f'收藏导入进行中：已捕获 {len(seen) - skipped} 条、跳过已在库 {skipped} 条（第 {pages} 页）')
                repo.update_task(task)
                if not fresh or pages >= max_pages or _login_wall(tab):
                    if not fresh and pages > 1:
                        stall_note = f'；第 {pages} 页后没有新岗位，已停止翻页'
                    break
                next_url = _page_url(current, pages + 1)
                if next_url:
                    # 收藏页是 URL 翻页（page 参数）：直接打开下一页；空页下一轮按 stall 收停。
                    try:
                        tab.get(next_url, timeout=20)
                    except Exception:
                        stall_note = f'；第 {pages + 1} 页打开失败，已停止翻页'
                        break
                    current = next_url
                    time.sleep(random.uniform(2, 4))
                else:
                    # URL 不带 page 参数的列表页：维持滚动加载节奏（旧兜底路径）。
                    time.sleep(random.uniform(4, 6))
            if _login_wall(tab):
                if seen:
                    outcome = {'status': 'needs_login',
                               'message': f'已捕获 {len(seen)} 条后遇到登录/验证页，已暂停翻页；请在项目浏览器完成登录后重新执行（已捕获内容保留在篮子）。'}
                else:
                    outcome = {'status': 'needs_login', 'message': '收藏页要求登录；请在项目浏览器完成登录后重试。'}
                return outcome
            if not seen:
                outcome = {'status': 'failed',
                           'message': '没有解析到收藏岗位：收藏夹可能是空的，或页面结构已变化；可在收藏页手动用「捕获当前页」验证。'}
                return outcome
            if not captured:
                outcome = {'status': 'completed',
                           'message': f'收藏夹里没有新岗位：{len(seen)} 条全部已在库且详情完整，无需重复提取。'}
                return outcome
            # 详情顺带读取：专 tab 逐条读，逐条落篮（中断后重跑会被去重闸跳过，不重复提取）。
            detail_tab = chromium.new_tab()
            detailed = 0
            unavailable = 0
            for index, item in enumerate(captured):
                task.update(count=len(seen) - skipped,
                            message=f'正在读取详情 {index + 1}/{len(captured)}：{item["title"]}')
                repo.update_task(task)
                output = read_detail_on_tab({**item, 'source': 'boss'}, detail_tab)
                if output['status'] == 'completed':
                    updated = output['job']
                    with _basket_lock:
                        entry = _store(data_dir).get(item['external_id'])
                        if entry:
                            entry.update({k: updated[k] for k in ('title', 'salary', 'description', 'description_status')
                                          if updated.get(k)})
                            if output.get('company_evidence'):
                                entry['company_evidence'] = output['company_evidence']
                    _save_basket(data_dir)
                    detailed += 1
                elif output['status'] in ('needs_login', 'needs_verification'):
                    keep_detail_tab = True
                    browser._front(detail_tab)
                    outcome = {'status': 'needs_login',
                               'message': f'已捕获 {len(seen) - skipped} 条、读取详情 {detailed} 条后遇到登录/验证页；'
                                          '请在项目浏览器完成登录后重新执行（已捕获内容保留在篮子，已完整的不会重读）。'}
                    return outcome
                else:
                    # 下架/结构变化：保留列表摘要，如实计数。
                    unavailable += 1
                time.sleep(random.uniform(1.5, 2.5))
            logger.info('收藏导入完成：捕获=%s 跳过已在库=%s 详情完整=%s 未取到=%s',
                        len(seen) - skipped, skipped, detailed, unavailable)
            unavailable_note = f'、{unavailable} 条未取到详情（可能已下架）' if unavailable else ''
            outcome = {'status': 'completed',
                       'message': f'收藏导入完成：捕获 {len(seen) - skipped} 条（跳过已在库 {skipped} 条），'
                                  f'详情完整 {detailed} 条{unavailable_note}{stall_note}；请到捕获篮确认入库。'}
            return outcome
    except Exception:
        logger.exception('收藏导入中断')
        outcome = {'status': 'failed', 'message': '收藏导入中断；已捕获的条目保留在捕获篮。'}
        return outcome
    finally:
        wall = outcome['status'] in ('needs_login', 'needs_verification')
        # 详情页撞墙时把详情 tab 留在前台；列表页撞墙时沿用旧行为留列表 tab。
        if detail_tab is not None and not (wall and keep_detail_tab):
            try:
                detail_tab.close()
            except Exception:
                pass
        if tab is not None and not (wall and not keep_detail_tab):
            try:
                tab.close()
            except Exception:
                pass
        browser._lock.release()


# ---- P1：投递记录同步（2026-11-23）--------------------------------------------------
# 「已投递」是个人中心（web/geek/recommend，tab=2）顶部的页签之一，2026-11-23 实测直达
# tab=2&page=1&tag=1（同栏：沟通过 tag=3 / 感兴趣 tag=4 / 面试）。列表数据由页面自己请求
# wapi/zprelation/resume/geekDeliverList?page=N 取回（zpData.cardList + hasMore），这里只被动接住
# 该响应——与逛即采集同一纪律，不代发请求。选接口而不是 DOM 的原因：接口带平台记录的投递时刻
# happenTime（毫秒，实测与页面 actionDateDesc 逐条吻合），卡片 DOM 里没有时间；而漏斗的推进天数、
# 投递批次、周几投递都按事件时间算，没有真实时刻就只能全记成「导入当天」。
# 接不到响应（接口改版/被缓存）时退回 DOM 抽卡（与收藏卡同为 li.item-boss），时刻按同步时间记。

APPLIED_PRIMARY = 'https://www.zhipin.com/web/geek/recommend?tab=2&page=1&tag=1'
APPLIED_LISTEN = 'wapi/zprelation/resume/geekDeliverList'
APPLIED_WAIT = 15  # 秒：进页后等投递记录响应的上限（SPA 异步渲染约 2-4s）


def _applied_at(happen_time):
    """平台投递时刻（毫秒时间戳）→ 与 repository.now() 同口径的 ISO；无效或在未来返回 ''。"""
    try:
        value = int(happen_time)
    except (TypeError, ValueError):
        return ''
    if value < 10 ** 11:  # 毫秒级时间戳不可能早于 2001 年
        return ''
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ''
    if moment > datetime.now(timezone.utc) + timedelta(minutes=5):
        return ''
    return moment.isoformat()


def _record_from_deliver(card, captured_from=''):
    """geekDeliverList 的 cardList 条目 → 投递记录（篮子条目字段 + applied_at）。字段缺失就留空，不猜值。"""
    identifier = plain(card.get('encryptJobId'))
    if not ID_PATTERN.fullmatch(identifier):
        return None
    title = plain(card.get('jobName'))[:300] or f'Boss岗位 {identifier[:8]}'
    company = plain(card.get('brandName'))[:300]
    salary = (plain(card.get('salaryDesc')) or plain(card.get('jobSalary')))[:300]
    location = plain(card.get('unifyLocationDetail')) or '·'.join(
        part for part in (plain(card.get('cityName')), plain(card.get('areaDistrict'))) if part)
    tags = [plain(t) for t in (card.get('jobLabels') or []) if isinstance(t, str) and plain(t)][:8]
    return {
        'external_id': identifier,
        'title': title,
        'company': company,
        'location': location[:300],
        'salary': salary,
        'url': _canonical_url(identifier),
        'tags': tags,
        'imported_via': 'applied',
        'captured_from': captured_from[:500],
        'captured_at': now(),
        'description': _listing_description(title, company, location, salary, tags),
        'description_status': 'listing',
        'applied_at': _applied_at(card.get('happenTime')),
    }


def _drain_deliver(tab):
    """等并收当前页的投递记录接口响应：({external_id: record}, has_more, codes)。只被动接页面自己发的请求。"""
    cards, codes, has_more = {}, [], False
    deadline = time.monotonic() + APPLIED_WAIT
    while time.monotonic() < deadline:
        try:
            packet = tab.listen.wait(count=1, timeout=2, raise_err=False)
        except Exception:
            packet = False
        if not packet:
            time.sleep(0.05)  # wait 异常/提前返回时不空转吃满一个核
        for part in (packet if isinstance(packet, list) else [packet]):
            if not part:
                continue
            try:
                data = loads_payload(part.response.body) if getattr(part, 'response', None) is not None else None
            except Exception:
                data = None
            if not isinstance(data, dict):
                continue
            codes.append(data.get('code'))
            payload = data.get('zpData')
            if data.get('code') != 0 or not isinstance(payload, dict):
                continue
            has_more = has_more or bool(payload.get('hasMore'))
            for card in payload.get('cardList') or []:
                record = _record_from_deliver(card, 'Boss 投递记录') if isinstance(card, dict) else None
                if record:
                    cards[record['external_id']] = record
            deadline = min(deadline, time.monotonic() + 1.5)  # 收到有效响应后再等一小会儿收尾即返回
    return cards, has_more, codes


def _read_applied_page(tab, attempts=2):
    """读当前页的投递记录：(records, has_more, state)。

    state：'network' 接口响应（带投递时刻，has_more 可信）/ 'dom' 退回 DOM 抽卡（时刻未知）/
    'needs_login' / 'needs_verification' / 'unverified' 'missing' 页签无法确认、页面数据不采信 /
    'empty' 两路都没读到。
    DOM 兜底只在确认当前页签是「已投递」时采信：个人中心默认停在上次看的页签，深链一旦失效，
    页面上可能是收藏/沟通过列表，照单全收会把它们全标成已投递。
    """
    for attempt in range(attempts):
        cards, has_more, codes = _drain_deliver(tab)
        if any(code == 0 for code in codes):
            return list(cards.values()), has_more, 'network'
        if codes and codes[-1] in (7, 8, 9):
            return [], False, 'needs_login'
        if codes and codes[-1] in (37, 35):
            return [], False, 'needs_verification'
        # 接口没来：页面可能没停在「已投递」。点中页签后页面会重发投递记录请求，再等一轮。
        state = _select_personal_tab(tab, 'personal_top_submitted')
        if state == 'clicked' and attempt < attempts - 1:
            continue
        break
    if state != 'ok':
        return [], False, 'missing' if not state else 'unverified'
    snapshot = _snapshot_cards(tab)
    page_url = str(snapshot.get('page_url') or '')
    items = [item for item in (_card_to_item(card, 'applied', f'Boss 投递记录 · {page_url}')
                               for card in (snapshot.get('cards') or []) if isinstance(card, dict)) if item]
    for item in items:
        item['applied_at'] = ''
    return items, False, 'dom' if items else 'empty'


def import_applied(repo, task, data_dir=None, max_pages=10):
    """Boss 投递记录同步：「已投递」逐页读取 → 库里的岗位回填「已投递」（真实投递时刻）→ 不在库的入篮。

    库内回填走 repository.sync_applied（状态只升不降 + 事件回溯），不经捕获篮——平台说「简历已发送」
    是既成事实，不需要再确认一次；不在库的岗位进捕获篮（imported_via='applied'），确认入库时落库并标记。
    """
    port = browser._connection(data_dir)
    if not port:
        started = browser.start(data_dir, 'boss')
        port = browser._connection(data_dir)
        if not port:
            return {'status': 'needs_browser', 'message': started.get('message') or '请先启动项目浏览器并登录 Boss。'}
    if not browser._lock.acquire(blocking=False):
        return {'status': 'backoff', 'message': '浏览器任务正在运行，请等待完成。'}
    tab = None
    outcome = {'status': 'failed', 'message': '投递记录同步未完成。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'message': '另一个进程正在使用项目浏览器。'}
            from DrissionPage import Chromium
            tab = Chromium(f'127.0.0.1:{port}').new_tab()
            tab.listen.start(APPLIED_LISTEN)
            records: dict[str, dict] = {}
            transport = 'network'
            known_more = False  # 最近一次接口响应说「还有下一页」：循环结束后仍为真 = 本轮没读完
            pages = 0
            current = APPLIED_PRIMARY
            while pages < max_pages:
                tab.get(current, timeout=20)
                if _login_wall(tab):
                    browser._front(tab)
                    outcome = {'status': 'needs_login',
                               'message': '投递记录页要求登录；请在项目浏览器完成登录后重试。'}
                    return outcome
                # 个人中心默认停在上次看的页签：深链 tag=1 实测可靠，但仍按 ka 把页面钉到「已投递」，
                # 深链哪天失效也不会读到别的列表。
                if not _select_personal_tab(tab, 'personal_top_submitted'):
                    outcome = {'status': 'failed',
                               'message': '没有找到 Boss「已投递」页签：页面结构可能已变化，本次未读取任何投递记录。'}
                    return outcome
                page_records, has_more, state = _read_applied_page(tab)
                if state in ('needs_login', 'needs_verification'):
                    browser._front(tab)
                    outcome = {'status': state,
                               'message': f'已读取 {len(records)} 条后 Boss 要求登录/验证；请在项目浏览器处理后重新执行（已回填的不受影响）。'}
                    return outcome
                if state in ('unverified', 'missing'):
                    outcome = {'status': 'failed',
                               'message': '未能确认页面停在 Boss「已投递」页签，为避免把别的列表误标为已投递，本次未采信页面数据。'}
                    return outcome
                if state == 'network':
                    known_more = has_more
                fresh = [record for record in page_records if record['external_id'] not in records]
                for record in fresh:
                    records[record['external_id']] = record
                if state == 'dom':
                    transport = 'dom'
                pages += 1
                logger.info('投递记录同步进度：第 %s 页 读取=%s 新增=%s 累计=%s 通道=%s 还有更多=%s',
                            pages, len(page_records), len(fresh), len(records), state, known_more)
                task.update(count=len(records),
                            message=f'投递记录同步中：已读取 {len(records)} 条（第 {pages} 页）')
                repo.update_task(task)
                # 收停四条件：到页数上限；两路都没读到；接口通道 hasMore=false；翻到没有新记录的一页
                # （URL 的 page 参数不被响应时每页都是同一批，DOM 通道也靠这条收停）。
                if (pages >= max_pages or state == 'empty' or (state == 'network' and not has_more)
                        or (pages > 1 and not fresh)):
                    break
                next_url = _page_url(current, pages + 1)
                if not next_url:
                    break
                current = next_url
                time.sleep(random.uniform(2, 4))
            if not records:
                outcome = {'status': 'completed', 'message': 'Boss「已投递」列表里还没有投递记录。'}
                return outcome
            # 截断如实报告：接口说还有下一页，本轮却因页数上限 / 翻页不生效 / URL 不能翻页而停了。
            # 不报的话用户会以为「已投递」就这么多，后面的记录永远同步不到。
            truncated_note = f'；仅读取了前 {pages} 页，Boss 上还有更多投递记录未同步' if known_more else ''
            known = _library_ids(repo, [job_id_for(record['external_id']) for record in records.values()])
            in_library = [record for record in records.values() if job_id_for(record['external_id']) in known]
            fresh = [record for record in records.values() if job_id_for(record['external_id']) not in known]
            result = repo.sync_applied([(job_id_for(record['external_id']), record['applied_at']) for record in in_library])
            added = basket_add(fresh, data_dir=data_dir)['added'] if fresh else 0
            source_note = '投递时间取自 Boss 记录' if all(record['applied_at'] for record in records.values()) \
                else '未取到 Boss 投递时间的按同步时间记'
            parts = []
            if in_library:
                detail = f'新标记已投递 {result["changed"]} 条'
                if result['kept']:
                    detail += f'、已是已投递或更后阶段 {result["kept"]} 条（仅补记投递时间）'
                if result['archived']:
                    detail += f'、已归档 {result["archived"]} 条（状态未改）'
                parts.append(f'库里 {len(in_library)} 条：{detail}')
            if fresh:
                parts.append(f'{len(fresh)} 条不在库，已入捕获篮（确认入库时标记已投递）')
            logger.info('投递记录同步完成：共=%s 库内=%s 回填=%s 入篮=%s 通道=%s 截断=%s',
                        len(records), len(in_library), result, added, transport, known_more)
            outcome = {'status': 'completed', 'truncated': known_more,
                       'message': f'Boss 投递记录同步完成：共 {len(records)} 条（{source_note}）；'
                                  + '；'.join(parts) + truncated_note + '。'}
            return outcome
    except Exception:
        logger.exception('投递记录同步中断')
        outcome = {'status': 'failed', 'message': '投递记录同步中断；库里已回填的记录不受影响。'}
        return outcome
    finally:
        wall = outcome['status'] in ('needs_login', 'needs_verification')
        if tab is not None:
            try:
                tab.listen.stop()
            except Exception:
                pass
            if not wall:
                try:
                    tab.close()
                except Exception:
                    pass
        browser._lock.release()


# ---- P1：在 Boss 打开公司检索（公司快查页联动入口）---------------------------------

def open_search(data_dir=None, company=''):
    company = str(company or '').strip()[:120]
    if not company:
        return {'status': 'failed', 'message': '请提供公司名称。'}
    port = browser._connection(data_dir)
    if not port:
        started = browser.start(data_dir, 'boss')
        port = browser._connection(data_dir)
        if not port:
            return {'status': 'needs_browser', 'message': started.get('message') or '请先启动项目浏览器并登录 Boss。'}
    if not browser._lock.acquire(blocking=False):
        return {'status': 'backoff', 'message': '浏览器任务正在运行，请稍后重试。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'message': '另一个进程正在使用项目浏览器。'}
            from DrissionPage import Chromium
            tab = Chromium(f'127.0.0.1:{port}').new_tab()
            tab.get('https://www.zhipin.com/web/geek/job?' + urlencode({'query': company}), timeout=20)
            return {'status': 'ok',
                    'message': f'已在项目浏览器打开 Boss 检索「{company}」：自己筛到满意后，回岗位页「从 Boss 导入」捕获本页即可。'}
    except Exception:
        return {'status': 'failed', 'message': '打开 Boss 检索失败；请确认项目浏览器仍在运行。'}
    finally:
        browser._lock.release()
