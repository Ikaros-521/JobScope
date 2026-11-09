"""Headless lifecycle check; no third-party navigation or personal profile access."""
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from jobscope import browser


def main():
    from DrissionPage import Chromium, ChromiumOptions
    root = Path(__file__).resolve().parents[1] / '.data' / 'browser-smoke'
    state = browser._root(root)
    state.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    options = ChromiumOptions().set_browser_path(str(browser._chrome())).set_user_data_path(str(state / 'profile')).set_local_port(port).headless()
    options.set_argument('--no-first-run')
    chrome = Chromium(options)
    try:
        (state / 'connection.json').write_text(json.dumps({'port': port}), encoding='utf-8')
        tab = chrome.new_tab('data:text/html,<title>JobScope lifecycle</title><h1>Local check</h1>')
        assert tab.title == 'JobScope lifecycle'
        assert browser.status(root)['running'] is True
        print(json.dumps({'chrome_lifecycle': 'passed', 'profile': 'project-only', 'external_navigation': False}))
    finally:
        chrome.quit()
    assert browser.status(root)['running'] is False


if __name__ == '__main__':
    main()
