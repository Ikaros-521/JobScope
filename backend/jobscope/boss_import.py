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
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from . import browser
from .connectors import CITY_CODES, DEFAULT_DATA, loads_payload, map_boss, plain
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
    salary: pick(root, /salary/),
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
    // 卡片容器：优先锚点所在 li（搜索页 li.job-card-wrapper、收藏页 li.item-boss 都适用）；
    // 不在 li 里的异形卡片再上溯三层找容器。
    let card = a.closest('li') || a;
    for (let i = 0; i < 3; i++) {
      const parent = card.parentElement;
      if (!parent) break;
      card = parent;
      if (/job-card|job-box|search-job|job-list/i.test(clsOf(card)) || card.tagName === 'LI') break;
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


def basket_view(repo):
    with _basket_lock:
        items = [dict(entry) for entry in _store(repo.data_dir).values()]
    if items:
        # 单连接分批查「已在库」，替代逐条 repo.job() 的 N 次连接；逛即采集轮询会高频调用。
        known = set()
        with repo.connection() as conn:
            for start in range(0, len(items), 500):
                chunk = [job_id_for(item['external_id']) for item in items[start:start + 500]]
                placeholders = ','.join('?' * len(chunk))
                known.update(row['id'] for row in conn.execute(f'SELECT id FROM jobs WHERE id IN ({placeholders})', chunk))
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
    """确认入库：逐条 save_job(source='boss') 幂等落库；可选中加星；入库条目移出篮子。"""
    wanted = set(ids) if ids else None
    with _basket_lock:
        store = _store(repo.data_dir)
        selected = {key: dict(entry) for key, entry in store.items() if wanted is None or key in wanted}
    if not selected:
        return {'status': 'empty', 'message': '捕获篮为空或没有选中的条目。', 'imported': 0, 'jobs': []}
    saved = [repo.save_job(_to_job(entry), 'boss') for entry in selected.values()]
    if auto_star:
        repo.update_jobs([job['id'] for job in saved], {'starred': True})
    with _basket_lock:
        store = _store(repo.data_dir)
        for key in selected:
            store.pop(key, None)
    _save_basket(repo.data_dir)
    logger.info('捕获篮确认入库：%s 条（加星=%s）', len(saved), bool(auto_star))
    return {'status': 'ok', 'imported': len(saved), 'jobs': saved,
            'message': f'已导入 {len(saved)} 条 Boss 岗位；重复岗位自动归并，可继续「补全详情」。'}


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
# 直接进 FAVORITE_PRIMARY；打不开再回个人中心做「收藏/感兴趣」链接发现与历史候选兜底。
# 入口再改版时把实测 URL 换进 FAVORITE_PRIMARY 即可。

USER_CENTER = 'https://www.zhipin.com/web/user/'
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

    # 实测收藏入口直进（推荐中心 tab=4）；打不开再回个人中心做链接发现与历史候选兜底。
    outcome = probe(FAVORITE_PRIMARY)
    if outcome:
        return outcome
    tab.get(USER_CENTER, timeout=20)
    if _login_wall(tab):
        return 'login'
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


def import_favorites(repo, task, data_dir=None, max_pages=10):
    """一键收藏导入：开新 tab 进收藏页（推荐中心 tab=4）→ 每页抽卡入篮 → page 参数逐页翻页
    （2-4s 间隔，上限 10 页；URL 无 page 参数的列表页退回滚动加载）。

    逐页落任务进度；任何一页失败都保留已捕获内容。登录/验证墙把 tab 留在前台交人工处理。
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
    outcome = {'status': 'failed', 'message': '收藏导入未完成。'}
    try:
        with browser.session_lock(data_dir) as acquired:
            if not acquired:
                return {'status': 'backoff', 'message': '另一个进程正在使用项目浏览器。'}
            from DrissionPage import Chromium
            tab = Chromium(f'127.0.0.1:{port}').new_tab()
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
                basket_add(fresh, data_dir=data_dir)
                pages += 1
                if fresh:
                    filled += 1
                logger.info('收藏导入进度：第 %s 页 新捕获=%s 累计=%s', pages, len(fresh), len(seen))
                task.update(count=len(seen), message=f'收藏导入进行中：已捕获 {len(seen)} 条（第 {pages} 页）')
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
            if seen:
                outcome = {'status': 'completed',
                           'message': f'收藏导入完成：共捕获 {len(seen)} 条（{filled} 页有新岗位）{stall_note}；请到捕获篮确认入库。'}
            else:
                outcome = {'status': 'failed',
                           'message': '没有解析到收藏岗位：收藏夹可能是空的，或页面结构已变化；可在收藏页手动用「捕获当前页」验证。'}
            return outcome
    except Exception:
        logger.exception('收藏导入中断')
        outcome = {'status': 'failed', 'message': '收藏导入中断；已捕获的条目保留在捕获篮。'}
        return outcome
    finally:
        if tab is not None:
            if outcome['status'] in ('needs_login', 'needs_verification'):
                pass  # 留在前台等人工处理
            else:
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
