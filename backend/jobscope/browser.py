"""Project-owned Chrome session; page-driven collection for sources that refuse direct requests."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from .connectors import (DEFAULT_DATA, SOURCES, city_hint, loads_payload, map_boss, map_job51,
                         map_zhaopin, result)

_lock = threading.Lock()

# 搜索结果页把岗位放在页面自身的数据里，或只在通过校验的会话内返回；三种取法都是实测得到的。
STATE_JS = ('return (() => { const state = window.__INITIAL_STATE__;'
            ' return state && Array.isArray(state.positionList)'
            ' ? {positionList: state.positionList, hasMore: state.hasMore,'
            ' pageIndex: state.pageIndex, pageSize: state.pageSize} : null; })()')
FETCH_JS = ("return (async () => { const r = await fetch(arguments[0],"
            " {credentials: 'include', headers: {Accept: 'application/json'}});"
            " const t = await r.text();"
            " return {ok: r.ok, status: r.status, body: t.slice(0, 600000)}; })()")
# 智联搜索页首屏岗位由 SSR 直出，后续页走页面自己的检索接口；实测在会话内
# 直接 POST /c/i/search/positions 传 pageIndex 即可翻页，无需额外令牌。
ZHAOPIN_PAGE_JS = ("return (async () => { const body = {S_SOU_FULL_INDEX: arguments[0],"
                   " S_SOU_WORK_CITY: arguments[1], order: 0, pageSize: 20, pageIndex: arguments[2],"
                   " eventScenario: 'pcSearchedSouSearch', anonymous: 0, clickFilterBlackCompany: false,"
                   " platform: 13, version: '0.0.0'};"
                   " const r = await fetch('https://fe-api.zhaopin.com/c/i/search/positions?platform=13&version=0.0.0',"
                   " {method: 'POST', credentials: 'include',"
                   " headers: {'Content-Type': 'application/json', Accept: 'application/json, text/plain, */*'},"
                   " body: JSON.stringify(body)});"
                   " const t = await r.text();"
                   " let payload = null;"
                   " try { payload = JSON.parse(t); } catch (e) { return null; }"
                   " const d = (payload && payload.data) || {};"
                   " return {positionList: Array.isArray(d.list) ? d.list : null, isEndPage: d.isEndPage,"
                   " isVerification: d.isVerification, total: d.count}; })()")

def _spec(source, **transport):
    """登记表提供名称与城市码，这里只补充该来源在浏览器里的取数方式。"""
    return dict(SOURCES[source], **transport)


PAGES = {
    'boss': _spec('boss', mode='listen', home='https://www.zhipin.com/web/geek/job',
                  listen='wapi/zpgeek/search/joblist.json', size=30, pages=34,
                  mapper=map_boss, default='100010000',
                  search=lambda query, code, size: 'https://www.zhipin.com/web/geek/job?' + urlencode({'query': query, 'city': code}),
                  blocked='未收到岗位响应。请查看项目浏览器是否需要登录或人工验证；不会自动反复请求。'),
    'zhaopin': _spec('zhaopin', mode='state', home='https://www.zhaopin.com/jobs/', size=20, pages=50,
                     mapper=map_zhaopin, default='',
                     search=lambda query, code, size: 'https://www.zhaopin.com/jobs/?' + urlencode({'kw': query, 'jl': code}),
                     blocked='搜索页未带出岗位数据；可能需要登录、人工验证，或页面结构已变化。'),
    '51job': _spec('51job', mode='fetch', home='https://we.51job.com/pc/search', size=50, pages=20,
                   mapper=map_job51, default='000000',
                   search=lambda query, code, size: 'https://we.51job.com/pc/search?' + urlencode({'keyword': query, 'searchType': '2', 'sortType': '0', 'jobArea': code}),
                   api=lambda query, code, size: 'https://we.51job.com/api/job/search-pc?' + urlencode({'keyword': query, 'searchType': '2', 'sortType': '0', 'jobArea': code, 'pageNum': '1', 'pageSize': str(size)}),
                   blocked='检索接口未在浏览器会话内返回结果；可能仍在脚本校验或需要重新登录。'),
}


# 公司快查渠道没有岗位搜索页，只要能打开站点首页让用户登录/过验证即可；start() 按
# PAGES → LOOKUP_PAGES → boss 的顺序找落点，公司快查面板的「打开专用浏览器」复用同一入口。
LOOKUP_PAGES = {
    'qcc': dict(name='企查查', home='https://www.qcc.com'),
    'tyc': dict(name='天眼查', home='https://www.tianyancha.com'),
}


@contextmanager
def session_lock(data_dir=None):
    """OS lock is released on process exit, so no stale PID-file deadlock."""
    root = _root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / 'session.lock').open('a+b')
    acquired = False
    try:
        if (root / 'session.lock').stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _has_driver():
    return importlib.util.find_spec('DrissionPage') is not None


def _root(data_dir=None):
    return Path(data_dir or DEFAULT_DATA).resolve() / 'browser'


def _chrome():
    candidates = [Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe', Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe', Path(os.environ.get('LOCALAPPDATA', 'C:/Users/Administrator/AppData/Local')) / 'Google/Chrome/Application/chrome.exe']
    return next((p for p in candidates if p.is_file()), None)


def _connection(data_dir=None):
    try:
        settings = json.loads((_root(data_dir) / 'connection.json').read_text(encoding='utf-8'))
        port = int(settings['port'])
        # Probe only the locally recorded loopback port, never caller input.
        import httpx
        data = httpx.get(f'http://127.0.0.1:{port}/json/version', timeout=1, trust_env=False).json()
        if not str(data.get('webSocketDebuggerUrl', '')).startswith(f'ws://127.0.0.1:{port}/'):
            return None
        return port
    except Exception:
        return None


def status(data_dir=None):
    installed = _has_driver() and _chrome() is not None
    running = bool(_connection(data_dir)) if installed else False
    message = '项目 Chrome 会话可连接，请确保已在该窗口登录所需平台。' if running else '尚未启动项目 Chrome。'
    if not installed:
        message = '需要本机 Chrome 和可选依赖 DrissionPage；请按 README 安装浏览器依赖。'
    return {'installed': installed, 'running': running, 'message': message}


def start(data_dir=None, source='boss'):
    spec = PAGES.get(source) or LOOKUP_PAGES.get(source) or PAGES['boss']
    if not _has_driver() or not _chrome():
        return status(data_dir)
    if not _lock.acquire(blocking=False):
        return {'installed': True, 'running': bool(_connection(data_dir)), 'message': '浏览器任务正在运行，请稍后重试。'}
    try:
        with session_lock(data_dir) as acquired:
            if not acquired:
                return {'installed': True, 'running': False, 'message': '另一个进程正在使用项目浏览器，请稍后重试。'}
            if _connection(data_dir):
                return status(data_dir)
            root = _root(data_dir)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            from DrissionPage import Chromium, ChromiumOptions
            options = ChromiumOptions().set_browser_path(str(_chrome())).set_user_data_path(str(root / 'profile')).set_local_port(port)
            options.set_argument('--no-first-run')
            options.set_argument('--remote-debugging-address', '127.0.0.1')
            browser = Chromium(options)
            (root / 'connection.json').write_text(json.dumps({'port': port}), encoding='utf-8')
            tab = browser.new_tab()
            tab.get(spec['home'], timeout=20)
            return {'installed': True, 'running': True, 'message': f'项目 Chrome 已启动，当前打开 {spec["name"]}。请在该窗口手动登录并完成验证，然后回到应用采集。'}
    except Exception:
        return {'installed': True, 'running': False, 'message': '项目 Chrome 启动失败；请关闭占用同一配置目录的浏览器后重试。'}
    finally:
        _lock.release()


def _city_code(spec, city):
    return spec['codes'].get(city, spec['default']) if city else spec['default']


def _listen(tab, spec, query, code, size):
    tab.listen.start(spec['listen'])
    tab.get(spec['search'](query, code, size), timeout=25)
    packet = tab.listen.wait(timeout=20, raise_err=False)
    if not packet:
        return None
    return loads_payload(packet.response.body)


def _job51_items(payload):
    return (((payload or {}).get('resultbody') or {}).get('job') or {}).get('items')


def _page(tab, spec, query, code, size):
    tab.get(spec['search'](query, code, size), timeout=30)
    if spec['mode'] == 'state':
        for _ in range(6):
            payload = tab.run_js(STATE_JS)
            if isinstance(payload, dict):
                return payload
            time.sleep(1)
        return None
    for _ in range(3):
        # pageSize 固定用登记值；大数量靠 pageNum 翻页，不把 limit 塞进单次请求。
        answer = tab.run_js(FETCH_JS, spec['api'](query, code, spec['size'])) or {}
        payload = loads_payload(answer.get('body'))
        # 200 且能 parse 不等于有效数据：风控校验页也可能是 JSON。必须 status=1 且带岗位列表。
        if str((payload or {}).get('status')) == '1' and isinstance(_job51_items(payload), list):
            return payload
        time.sleep(2)
    return None


def _extend_state_pages(tab, spec, payload, query, code, size, progress=None):
    """state 模式首屏只渲染一页；后续页向页面自己的检索接口按 pageIndex 续读。

    返回 (payload, 已读页数, 提示)。任何一页失败都保留已读到的内容，不算采集失败。
    """
    pages = int(spec.get('pages', 1))
    if pages <= 1 or not isinstance(payload.get('positionList'), list):
        return payload, 1, ''
    items = list(payload['positionList'])
    pages_read = 1
    note = ''
    for page in range(2, pages + 1):
        if len(items) >= size or payload.get('hasMore') is False:
            break
        # 页与页之间留出间隔，避免高频连击触发来源风控。
        time.sleep(0.5)
        more = tab.run_js(ZHAOPIN_PAGE_JS, query, code, page)
        if not isinstance(more, dict) or not isinstance(more.get('positionList'), list) or not more['positionList']:
            note = '；后续页未返回数据，已停止翻页'
            break
        if more.get('isVerification'):
            note = '；后续页要求人工验证，已停止翻页'
            break
        items.extend(more['positionList'])
        pages_read += 1
        payload = {**payload, 'positionList': items, 'hasMore': more.get('isEndPage') == 0}
        if progress:
            try:
                progress(len(items), pages_read)
            except Exception:
                pass
    return payload, pages_read, note


def _extend_listen_pages(tab, spec, payload, query, code, size, progress=None):
    """listen 模式首屏一包；BOSS 结果页向下滚动会触发下一页 joblist.json，边滚边收包合并。

    返回 (payload, 已读页数, 提示)。滚动后超时、响应异常或没有新岗位都保留已读内容，不算采集失败。
    """
    pages = int(spec.get('pages', 1))
    items = ((payload or {}).get('zpData') or {}).get('jobList')
    if pages <= 1 or not isinstance(items, list):
        return payload, 1, ''
    seen, merged = set(), list(items)
    for item in items:
        if isinstance(item, dict):
            seen.add(str(item.get('encryptJobId') or ''))
    pages_read = 1
    note = ''
    for page in range(2, pages + 1):
        if len(merged) >= size:
            break
        # 滚到底部触发下一页；响应间隔留出时间，避免高频连击触发来源风控。
        tab.run_js('window.scrollTo(0, document.documentElement.scrollHeight)')
        packet = tab.listen.wait(timeout=10, raise_err=False)
        if not packet:
            note = '；滚动后未收到下一页响应，已停止翻页'
            break
        more = loads_payload(packet.response.body)
        new_items = ((more or {}).get('zpData') or {}).get('jobList') if isinstance(more, dict) else None
        if not isinstance(new_items, list) or not new_items:
            note = '；下一页未返回岗位，已停止翻页'
            break
        fresh = 0
        for item in new_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get('encryptJobId') or '')
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            fresh += 1
        if not fresh:
            note = '；下一页没有新岗位，已停止翻页'
            break
        pages_read = page
        payload = {**payload, 'zpData': {**payload.get('zpData', {}), 'jobList': merged}}
        if progress:
            try:
                progress(len(merged), pages_read)
            except Exception:
                pass
        time.sleep(0.5)
    return payload, pages_read, note


def _extend_fetch_pages(tab, spec, payload, query, code, size, progress=None):
    """fetch 模式首屏一包；前程无忧同一检索接口按 pageNum 续读并合并。

    返回 (payload, 已读页数, 提示)。接口失败、状态异常或没有新岗位都保留已读内容，不算采集失败。
    """
    pages = int(spec.get('pages', 1))
    items = _job51_items(payload)
    if pages <= 1 or not items:
        return payload, 1, ''
    seen, merged = set(), list(items)
    for item in items:
        if isinstance(item, dict):
            seen.add(str(item.get('jobId') or ''))
    pages_read = 1
    note = ''
    base_url = spec['api'](query, code, spec['size'])
    for page in range(2, pages + 1):
        if len(merged) >= size:
            break
        # 页与页之间留出间隔，避免高频连击触发来源风控；风控校验页会挤掉个别响应，失败再补一次。
        time.sleep(0.5)
        new_items = None
        for attempt in range(2):
            if attempt:
                time.sleep(2)
            answer = tab.run_js(FETCH_JS, base_url.replace('pageNum=1', f'pageNum={page}')) or {}
            more = loads_payload(answer.get('body'))
            candidate = _job51_items(more) if isinstance(more, dict) else None
            if str((more or {}).get('status')) == '1' and isinstance(candidate, list) and candidate:
                new_items = candidate
                break
        if not new_items:
            note = '；后续页未返回岗位，已停止翻页'
            break
        fresh = 0
        for item in new_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get('jobId') or '')
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            fresh += 1
        if not fresh:
            note = '；下一页没有新岗位，已停止翻页'
            break
        pages_read = page
        payload = {**payload, 'resultbody': {**payload.get('resultbody', {}), 'job': {**(payload.get('resultbody') or {}).get('job', {}), 'items': merged}}}
        if progress:
            try:
                progress(len(merged), pages_read)
            except Exception:
                pass
    return payload, pages_read, note


def collect(source='boss', query='', location='', limit=20, data_dir=None, progress=None):
    """Collect from the platform's own page; nothing here is replayed outside the user session."""
    spec = PAGES.get(source)
    if spec is None:
        return result('unsupported', '该来源不由项目浏览器采集。')
    if not query or len(str(query)) > 200:
        return result('failed', '请输入不超过200字的关键词。')
    city = str(location or '').strip()[:40]
    if city and city not in spec['codes']:
        return result('unsupported', f'{spec["name"]}暂未映射该城市；可用{city_hint(source)}或留空。')
    try:
        size = max(1, min(int(limit), spec['size'] * int(spec.get('pages', 1))))
    except (TypeError, ValueError):
        return result('failed', '采集数量必须是整数。')
    if not _has_driver() or not _chrome():
        return result('unsupported', status(data_dir)['message'])
    port = _connection(data_dir)
    if not port:
        return result('needs_login', f'请先启动项目浏览器并登录{spec["name"]}。')
    if not _lock.acquire(blocking=False):
        return result('backoff', '浏览器任务正在运行，请等待完成。')
    tab = None
    try:
        from DrissionPage import Chromium
        with session_lock(data_dir) as acquired:
            if not acquired:
                return result('backoff', '另一个进程正在使用项目浏览器。')
            tab = Chromium(f'127.0.0.1:{port}').new_tab()
            payload = _listen(tab, spec, query, _city_code(spec, city), size) if spec['mode'] == 'listen' else _page(tab, spec, query, _city_code(spec, city), size)
            if not isinstance(payload, dict):
                return result('needs_verification', spec['blocked'])
            extend = {'state': _extend_state_pages, 'listen': _extend_listen_pages, 'fetch': _extend_fetch_pages}[spec['mode']]
            payload, pages_read, page_note = extend(tab, spec, payload, query, _city_code(spec, city), size, progress)
            output = spec['mapper'](payload, size, city)
            extras = []
            if pages_read > 1:
                extras.append(f'已翻页读取 {pages_read} 页')
            if page_note:
                extras.append(page_note.lstrip('；'))
            base = output['message'].rstrip('。')
            suffix = '；' + '；'.join(extras) + '。' if extras else '。'
            output['message'] = f'{spec["name"]}浏览器采集：' + base + suffix
            return output
    except Exception:
        return result('failed', '浏览器采集失败；请确认项目会话仍在运行并已完成登录。')
    finally:
        if tab is not None:
            try:
                tab.listen.stop()
                # Leave tab visible so the user can handle login/verification.
            except Exception:
                pass
        _lock.release()
