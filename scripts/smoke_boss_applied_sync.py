"""Boss 投递记录同步入口冒烟（2026-11-23）。No mutation reaches the backend.

覆盖两处入口与结果反馈：
  1) 收藏页工具条「同步 Boss 投递」：点击 → 进行中禁用 → 轮询到终态 → 提示 → 列表与状态刷新；工具条不溢出。
  2) 岗位页「从 Boss 导入」面板「同步投递记录」：点击 → 提示；捕获篮条目带「投递记录 · Boss 已投递 时间」标记与入库计数。
两次同步的完成提示文案不同：提示会停留几秒，文案相同的话第二段的断言会命中第一段的旧提示。
Run: D:\\Miniconda\\python.exe scripts/smoke_boss_applied_sync.py --url http://127.0.0.1:8766
Requires Python playwright and Chrome; serve an isolated current-source frontend.
"""
import argparse
import pathlib
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

SYNC_MESSAGES = (
    'Boss 投递记录同步完成：共 3 条（投递时间取自 Boss 记录）；库里 2 条：新标记已投递 2 条；1 条不在库，已入捕获篮（确认入库时标记已投递）。',
    'Boss 投递记录同步完成：共 3 条（投递时间取自 Boss 记录）；库里 2 条：已是已投递或更后阶段 2 条（仅补记投递时间）。',
)


def dump_on_failure(page, label):
    """失败现场：面板按钮状态 + 截图落 output/。"""
    shot = pathlib.Path(__file__).resolve().parent.parent / 'output' / f'smoke_boss_applied_sync_{label}.png'
    try:
        page.screenshot(path=str(shot))
    except Exception:
        pass
    print(f'[debug:{label}]', page.evaluate("""() => ({
      hash: location.hash,
      dialogs: document.querySelectorAll('[role=dialog]').length,
      panelButtons: [...document.querySelectorAll('.boss-import button')].map(b => b.textContent.trim() + (b.disabled ? '(disabled)' : '')),
      favoritesButtons: [...document.querySelectorAll('.favorites-batch button')].map(b => b.textContent.trim() + (b.disabled ? '(disabled)' : '')),
      toasts: [...document.querySelectorAll('.toast')].map(t => t.textContent.trim().slice(0, 40)),
    })"""), 'screenshot:', shot)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    assert urlparse(args.url).port == 8766, 'Only isolated 8766 is allowed'
    jobs = [dict(id=f'applied-smoke-{i}', title=f'受控投递岗位{i}', company=company, location='杭州', salary='25K', url='',
                 source='boss', updated_at='', description='受控收藏', evidence_ids=[], tags=[], starred=True,
                 pipeline_status='interested', pipeline_status_changed_at='2026-11-23T00:00:00+00:00')
            for i, company in enumerate(['受控甲公司', '受控乙公司'])]
    basket_item = dict(external_id='smokeApplied01', title='受控新投递岗位', company='受控丙公司', location='杭州', salary='20K',
                       url='https://www.zhipin.com/job_detail/smokeApplied01.html', tags=[], imported_via='applied',
                       captured_at='2026-11-23T00:00:00+00:00', in_library=False, applied_at='2026-11-20T11:15:03+00:00')
    state = {'polls': 0, 'started': 0}

    def task(status):
        # 每次同步一轮轮询：第 2 次轮询到终态；提示文案按第几次同步区分。
        message = SYNC_MESSAGES[state['started'] - 1] if status == 'completed' else '投递记录同步中'
        return {'id': 'controlled-applied', 'status': status, 'message': message,
                'source': 'boss', 'query': '', 'created_at': '', 'count': 3, 'kind': 'boss_import'}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 720})

        def intercept(route):
            parsed = urlparse(route.request.url)
            path, method = parsed.path, route.request.method
            if path == '/api/jobs' and method == 'GET':
                return route.fulfill(json={'jobs': [job for job in jobs if job['starred']], 'page': 1, 'page_size': 100, 'pages': 1, 'total': 2})
            if path == '/api/import/boss/applied' and method == 'POST':
                state['started'] += 1
                state['polls'] = 0
                return route.fulfill(json={'task': task('running')})
            if path == '/api/tasks/controlled-applied' and method == 'GET':
                state['polls'] += 1
                if state['polls'] >= 2:
                    jobs[0]['pipeline_status'] = 'applied'  # 模拟后端回填：同步完成后列表应刷新出「已投递」
                    return route.fulfill(json={'task': task('completed')})
                return route.fulfill(json={'task': task('running')})
            if path == '/api/import/basket':
                return route.fulfill(json={'items': [basket_item], 'total': 1})
            if path == '/api/import/boss/tabs':
                return route.fulfill(json={'status': 'needs_browser', 'tabs': [], 'message': '受控：项目浏览器未运行'})
            if path == '/api/import/boss/browse':
                return route.fulfill(json={'status': 'idle', 'running': False, 'captured': 0, 'message': '逛即采集未开启'})
            if method not in ('GET', 'HEAD'):
                raise AssertionError(f'Unexpected mutation {method}: {path}')
            return route.continue_()

        page.route('**/api/**', intercept)
        page.goto(args.url)
        # 新档案会自动弹教程遮罩（拦截点击）：等它出现按 Esc 跳过；再展开「岗位研究」组。
        try:
            page.get_by_role('dialog', name='使用教程').wait_for(state='attached', timeout=15000)
            page.keyboard.press('Escape')
        except Exception:
            pass
        nav_toggle = page.locator('.nav-group-toggle', has_text='岗位研究')
        if nav_toggle.first.get_attribute('aria-expanded') == 'false':
            nav_toggle.first.click()
        page.locator('.sidebar').get_by_role('button', name=re.compile(r'^收藏')).click()
        page.locator('.favorites-view').wait_for(state='visible')

        # ---- 入口一：收藏页工具条 ----
        sync_button = page.locator('.favorites-batch').get_by_role('button', name='同步 Boss 投递', exact=True)
        expect(sync_button).to_be_enabled()
        expect(page.locator('.favorites-view .pipeline-select').first).to_have_value('interested')
        # 布局：工具条不横向溢出，按钮完整落在视口内，且与筛选 chips 同一行（1280 宽下不折行）。
        metrics = page.evaluate("""() => {
          const toolbar = document.querySelector('.favorites-toolbar');
          const button = document.querySelector('.favorites-batch button');
          const chip = document.querySelector('.pipeline-filter-chip');
          const r = button.getBoundingClientRect(), c = chip.getBoundingClientRect(), t = toolbar.getBoundingClientRect();
          return {overflow: toolbar.scrollWidth - toolbar.clientWidth, right: r.right, left: r.left, width: innerWidth,
                  sameRow: Math.abs((r.top + r.height / 2) - (c.top + c.height / 2)) < 6, toolbarHeight: t.height};
        }""")
        assert metrics['overflow'] <= 0 and metrics['right'] <= metrics['width'] and metrics['left'] >= 0, metrics
        assert metrics['sameRow'], f'同步按钮被挤到第二行: {metrics}'
        print('收藏页工具条布局:', metrics)

        sync_button.click()
        expect(page.locator('.favorites-batch').get_by_role('button', name='投递同步中…', exact=True)).to_be_disabled()
        expect(page.locator('.toast.notice', has_text='新标记已投递 2 条')).to_be_visible(timeout=20000)
        expect(page.locator('.toast.notice', has_text='1 条不在库，已入捕获篮')).to_be_visible()
        # 同步完成：入口恢复可用，列表已刷新出后端回填的「已投递」。
        expect(sync_button).to_be_enabled()
        expect(page.locator('.favorites-view .pipeline-select').first).to_have_value('applied')
        assert state['started'] == 1 and state['polls'] == 2, state
        print('收藏页入口：点击 → 进行中禁用 → 2 次轮询 → 提示 → 状态刷新为已投递  OK')

        # ---- 入口二：岗位页「从 Boss 导入」面板 ----
        # 导航项「岗位研究」与分组折叠钮同名：排除 .nav-group-toggle 再点（否则点到折叠钮）。
        page.locator('.sidebar button:not(.nav-group-toggle)', has_text=re.compile(r'^岗位研究')).click()
        entry = page.locator('.job-entry-actions').get_by_role('button', name='从 Boss 导入', exact=True)
        entry.wait_for(state='visible')
        page.wait_for_timeout(800)
        entry.click()
        panel = page.locator('.boss-import')
        dialog_button = panel.get_by_role('button', name='同步投递记录', exact=True)
        try:
            panel.wait_for(state='visible', timeout=10000)
            expect(dialog_button).to_be_enabled()
            # 捕获篮里的投递记录条目：通道名 + Boss 投递时间（11/20 19:15）+ 入库计数说明。
            expect(panel.get_by_text('受控新投递岗位', exact=True)).to_be_visible()
            expect(panel.get_by_text(re.compile(r'投递记录 · Boss 已投递 11/20 19:15'))).to_be_visible()
            expect(panel.get_by_text('1 条按 Boss 投递记录标记已投递')).to_be_visible()
            dialog_button.click()
            expect(page.locator('.toast.notice', has_text='仅补记投递时间')).to_be_visible(timeout=20000)
            expect(dialog_button).to_be_enabled()
        except Exception:
            dump_on_failure(page, 'dialog')
            raise
        assert state['started'] == 2, state
        print('导入面板入口：按钮 → 提示；篮子条目带投递标记与入库计数  OK')
        browser.close()
    print('SMOKE OK')


if __name__ == '__main__':
    main()
