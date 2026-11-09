"""Project-owned Chrome session, browser-triggered BOSS response collection."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from .connectors import CITY_CODES, DEFAULT_DATA, map_boss, result

_lock = threading.Lock()


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
    message = '项目 Chrome 会话可连接，请确保已正常登录 BOSS。' if running else '尚未启动项目 Chrome。'
    if not installed:
        message = '需要本机 Chrome 和可选依赖 DrissionPage；请按 README 安装浏览器依赖。'
    return {'installed': installed, 'running': running, 'message': message}


def start(data_dir=None):
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
            tab.get('https://www.zhipin.com/web/geek/job', timeout=20)
            return {'installed': True, 'running': True, 'message': '项目 Chrome 已启动。请在该窗口手动登录并完成验证，然后回到应用采集。'}
    except Exception:
        return {'installed': True, 'running': False, 'message': '项目 Chrome 启动失败；请关闭占用同一配置目录的浏览器后重试。'}
    finally:
        _lock.release()


def collect(query, location='', limit=20, data_dir=None):
    if not _has_driver() or not _chrome():
        return result('unsupported', status(data_dir)['message'])
    if not query or len(str(query)) > 200:
        return result('failed', '请输入不超过200字的关键词。')
    if location and location not in CITY_CODES:
        return result('unsupported', '浏览器采集暂支持常用城市名或留空，请使用北京、上海、广州、深圳、杭州、成都等。')
    port = _connection(data_dir)
    if not port:
        return result('needs_login', '请先启动项目浏览器并手动登录。')
    if not _lock.acquire(blocking=False):
        return result('backoff', '浏览器任务正在运行，请等待完成。')
    tab = None
    try:
        from DrissionPage import Chromium
        with session_lock(data_dir) as acquired:
            if not acquired:
                return result('backoff', '另一个进程正在使用项目浏览器。')
            browser = Chromium(f'127.0.0.1:{port}')
            tab = browser.new_tab()
            tab.listen.start('wapi/zpgeek/search/joblist.json')
            url = 'https://www.zhipin.com/web/geek/job?' + urlencode({'query': str(query), 'city': CITY_CODES.get(location, '100010000')})
            tab.get(url, timeout=25)
            packet = tab.listen.wait(timeout=20, raise_err=False)
            if not packet:
                return result('needs_verification', '未收到岗位响应。请查看项目浏览器是否需要登录或人工验证；不会自动反复请求。')
            data = packet.response.body
            if isinstance(data, str):
                if len(data) > 12 * 1024 * 1024:
                    return result('failed', '岗位响应超过大小上限。')
                data = json.loads(data)
            if not isinstance(data, dict):
                return result('failed', '浏览器未收到有效岗位 JSON。')
            output = map_boss(data, max(1, min(int(limit), 30)))
            output['message'] = '项目浏览器响应采集：' + output['message']
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
