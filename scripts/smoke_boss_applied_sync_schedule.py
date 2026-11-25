"""Boss 投递自动同步设置面板冒烟（2026-11-24）。打 8766 隔离实例的**真实**设置端点（只写定时同步设置，
结束前还原为关闭）。可选 --static <临时构建目录>：未上线时用它把静态资源指到临时构建，不碰 frontend/dist。

Run: D:\\Miniconda\\python.exe scripts/smoke_boss_applied_sync_schedule.py [--static ../output/dist-autosync-smoke]
Requires Python playwright and Chrome; serve an isolated current-source frontend.
"""
import argparse
import json
import pathlib
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright, expect

PANEL = '.system-panel[aria-label="Boss 投递自动同步"]'


def call(base, path, payload=None):
    request = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Content-Type': 'application/json'}, method='PUT' if payload is not None else 'GET')
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    parser.add_argument('--static', default='', help='临时构建目录；不给则用实例自己提供的前端')
    args = parser.parse_args()
    assert urlparse(args.url).port == 8766, 'Only isolated 8766 is allowed'
    static = pathlib.Path(args.static).resolve() if args.static else None
    assert call(args.url, '/api/settings/applied-sync')['enabled'] is False, '前置条件：测试实例的自动同步应为关闭'
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)[:200]))
            if static:
                def serve(route):
                    target = static / urlparse(route.request.url).path.lstrip('/')
                    # SPA 路由回退到入口页；带哈希的 assets 文件按原路径取。
                    route.fulfill(path=str(target if target.is_file() else static / 'index.html'))
                page.route(lambda url: not urlparse(url).path.startswith('/api'), serve)
            page.goto(args.url)
            try:
                page.get_by_role('dialog', name='使用教程').wait_for(state='attached', timeout=15000)
                page.keyboard.press('Escape')
            except Exception:
                pass
            manage = page.locator('.nav-group-toggle', has_text='系统管理').first
            if manage.get_attribute('aria-expanded') == 'false':
                manage.click()
            page.locator('.sidebar button:not(.nav-group-toggle)', has_text=re.compile('^设置中心')).click()
            page.get_by_role('tab', name='系统设置', exact=True).click()
            panel = page.locator(PANEL)
            expect(panel.get_by_role('heading', name='Boss 投递自动同步')).to_be_visible()

            # 关闭态：只有开关，没有周期 / 时间点。
            toggle = panel.get_by_role('checkbox')
            expect(toggle).not_to_be_checked()
            expect(panel.locator('#applied-sync-period')).to_have_count(0)

            # 开启：立即保存，出现周期 + 时间点（默认每天 21:30）与「下次同步」。
            toggle.check()
            expect(panel.locator('#applied-sync-period')).to_be_visible()
            expect(toggle).to_be_enabled()  # 保存完成
            expect(panel.locator('#applied-sync-period')).to_have_value('1d')
            expect(panel.locator('#applied-sync-time')).to_have_value('21:30')
            expect(panel.get_by_role('status')).to_contain_text(re.compile(r'下次同步 .+'))
            expect(panel.get_by_role('status')).to_contain_text('还没有自动同步过')
            saved = call(args.url, '/api/settings/applied-sync')
            assert saved['enabled'] is True and (saved['period'], saved['time']) == ('1d', '21:30') and saved['overdue'] is False, saved
            assert saved['next_run_at']  # 后端算出的下一次时间点

            # 周期改「每 6 小时」：时间点不适用，输入框隐藏。界面是乐观更新——等控件恢复可用（保存完成）再读接口。
            period_select = panel.locator('#applied-sync-period')
            period_select.select_option('6h')
            expect(panel.locator('#applied-sync-time')).to_have_count(0)
            expect(period_select).to_be_enabled()
            assert call(args.url, '/api/settings/applied-sync')['period'] == '6h'

            # 改回「每天」并改时间点：失焦才提交（不是每敲一下就保存）。
            period_select.select_option('1d')
            time_input = panel.locator('#applied-sync-time')
            expect(time_input).to_be_visible()
            expect(time_input).to_be_enabled()
            assert call(args.url, '/api/settings/applied-sync')['period'] == '1d'
            time_input.fill('09:15')
            assert call(args.url, '/api/settings/applied-sync')['time'] == '21:30'  # 未失焦：还没保存
            time_input.blur()
            expect(time_input).to_be_enabled()
            assert call(args.url, '/api/settings/applied-sync')['time'] == '09:15'
            expect(panel.get_by_role('status')).to_contain_text('09:15')  # 下一次落在今天或明天的 09:15

            # 布局：下拉框与时间框同一行、等高（沿用系统设置页既有的字段样式，不新增样式）。
            metrics = page.evaluate("""() => {
              const s = document.querySelector('#applied-sync-period').getBoundingClientRect();
              const t = document.querySelector('#applied-sync-time').getBoundingClientRect();
              const panel = document.querySelector('.system-panel[aria-label="Boss 投递自动同步"]');
              return {selectHeight: s.height, inputHeight: t.height, sameRow: Math.abs(s.top - t.top) < 1,
                      overflow: panel.scrollWidth - panel.clientWidth};
            }""")
            assert abs(metrics['selectHeight'] - metrics['inputHeight']) < 1 and metrics['sameRow'], metrics
            assert metrics['overflow'] <= 0, metrics
            shot = pathlib.Path(__file__).resolve().parent.parent / 'output' / 'smoke_boss_applied_sync_schedule.png'
            panel.screenshot(path=str(shot))

            # 关闭：设置落回关闭，周期 / 时间点收起。
            toggle.uncheck()
            expect(panel.locator('#applied-sync-period')).to_have_count(0)
            expect(toggle).to_be_enabled()  # 保存完成
            assert call(args.url, '/api/settings/applied-sync')['enabled'] is False
            assert not errors, errors
            browser.close()
        print('设置面板：开关 → 周期 / 时间点即时保存 → 按小时周期隐藏时间点 → 失焦提交 → 关闭还原  OK')
        print('布局:', metrics, '| 截图:', shot)
    finally:
        # 兜底还原：中途断言失败也不把测试实例留在「自动同步已开启」。
        call(args.url, '/api/settings/applied-sync', {'enabled': False})
    print('SMOKE OK')


if __name__ == '__main__':
    main()
