"""真实 browser.collect 的无会话分支：采集入口自动拉起项目浏览器（模块其余路径由
API 层测试整体 stub 覆盖，这里只打这一段，不起真 Chrome）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import browser


def test_collect_auto_starts_when_no_session(monkeypatch):
    """无连接时 collect 先自动 start()（用户点采集就该看到窗口）；拉不起来则透传 start 的提示。"""
    monkeypatch.setattr(browser, '_has_driver', lambda: True)
    monkeypatch.setattr(browser, '_chrome', lambda: 'C:/fake/chrome.exe')
    monkeypatch.setattr(browser, '_connection', lambda data_dir=None: None)
    started = []

    def fake_start(data_dir=None, source='boss'):
        started.append(source)
        return {'installed': True, 'running': False, 'message': '项目 Chrome 启动失败；请关闭占用同一配置目录的浏览器后重试。'}
    monkeypatch.setattr(browser, 'start', fake_start)

    out = browser.collect('boss', query='Python', limit=10)
    assert out['status'] == 'needs_login'
    assert started == ['boss']
    assert '启动失败' in out['message']


def test_collect_reports_invalid_query_before_touching_browser(monkeypatch):
    """关键词校验在浏览器探测之前：空关键词不触发自动拉起。"""
    started = []
    monkeypatch.setattr(browser, 'start', lambda data_dir=None, source='boss': started.append(source))
    out = browser.collect('boss', query='', limit=10)
    assert out['status'] == 'failed'
    assert started == []
