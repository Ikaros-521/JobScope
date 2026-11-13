"""Real JobScope reading flow against explicitly marked isolated localhost:8766.

Only unique fictitious jobs are written and deleted. Browser collection is route-mocked.
Run with .venv/Scripts/python.exe scripts/smoke_job_reading_flow.py.
"""
import argparse
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def navigate(page, name):
    nav = page.get_by_role('navigation', name='主导航')
    button = nav.get_by_role('button', name=name, exact=True)
    if not button.is_visible():
        page.get_by_role('button', name='工作区导航', exact=True).click()
    button.click()


def run_flow(browser, base, output, width):
    context = browser.new_context(viewport={'width': width, 'height': 720}, reduced_motion='reduce')
    page = context.new_page()
    page.set_default_timeout(12000)
    stamp = uuid.uuid4().hex[:8]
    title, company = f'虚构阅读验收岗位-{width}-{stamp}', f'虚构阅读验收公司-{stamp}'
    state = {'empty': True, 'collect_started': False, 'complete': False, 'polls': 0}
    task = dict(id=f'reading-mock-{stamp}', source='boss', query=title, status='running', message='受控采集中', count=0, created_at='')

    def route_api(route):
        request = route.request
        parsed = urlparse(request.url)
        assert parsed.port == 8766 and parsed.hostname in ('127.0.0.1', 'localhost')
        if parsed.path == '/api/jobs' and request.method == 'GET' and state['empty']:
            return route.fulfill(json={'jobs': [], 'page': 1, 'page_size': 20, 'pages': 0, 'total': 0})
        if parsed.path == '/api/import/job' and request.method == 'POST':
            assert request.post_data_json['title'] == title
            state['empty'] = False
            return route.continue_()
        if parsed.path == '/api/browser/collect':
            state['collect_started'] = True
            return route.fulfill(json={'task': task})
        if parsed.path == f"/api/tasks/{task['id']}":
            state['polls'] += 1
            return route.fulfill(json={'task': {**task, **({'status': 'completed', 'count': 1, 'message': '受控后台任务完成'} if state['complete'] else {})}})
        # Never start a real browser, model, collector, analysis, or save a resume.
        if request.method not in ('GET', 'HEAD'):
            assert parsed.path == '/api/import/job' or parsed.path.endswith('/manual-description'), f'Unexpected mutation: {request.method} {parsed.path}'
        return route.continue_()

    def route_network(route):
        if urlparse(route.request.url).hostname not in ('127.0.0.1', 'localhost'):
            return route.abort()
        return route.continue_()

    context.route('**/*', route_network)
    page.route('**/api/**', route_api)
    job_id = None
    results = []
    try:
        page.goto(base)
        expect(page.get_by_role('heading', name='从一个机会开始', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='粘贴已有岗位 JD', exact=True)).to_be_visible()
        expect(page.locator('.search-section')).to_be_hidden()
        page.screenshot(path=str(output / f'{width}-01-empty.png'), full_page=True)
        results.append('mocked empty library: paste and external lookup entry visible')
        page.get_by_role('button', name='查找外部岗位', exact=True).click()
        expect(page.locator('.search-section')).to_be_visible()
        page.get_by_role('button', name='粘贴已有岗位 JD', exact=True).click()
        page.locator('#import-title').fill(title)
        page.locator('#import-company').fill(company)
        page.locator('#import-body').fill('虚构岗位列表摘要，仅供隔离测试。')
        with page.expect_response(lambda response: '/api/import/job' in response.url and response.request.method == 'POST') as response:
            page.get_by_role('button', name='保存资料', exact=True).click()
        job = response.value.json()['job']
        job_id = job['id']
        expect(page.get_by_role('heading', name=title, exact=True, level=2)).to_be_visible()
        expect(page.locator('.search-section')).to_be_hidden()
        results.append('real unique job imported through UI; discovery collapsed')

        page.get_by_role('button', name='手动补录完整职责', exact=True).click()
        description = '\n'.join(f'{i:02d}、负责虚构数据平台开发与测试，记录可核对的设计和交付依据。' for i in range(1, 61))
        source = f'虚构招聘邮件 {stamp}，用户补录验收'
        page.locator(f'#manual-jd-{job_id}').fill(description)
        page.locator(f'#manual-source-{job_id}').fill(source)
        page.get_by_role('button', name='保存完整职责', exact=True).click()
        expect(page.get_by_text(re.compile('用户补录 · ' + source))).to_be_visible()
        saved = page.request.get(f'{base}/api/jobs/{job_id}').json()['job']
        assert saved['id'] == job_id and saved['description'] == description
        assert saved['description_source'] == 'user' and saved['description_source_note'] == source
        assert set(job['evidence_ids']) < set(saved['evidence_ids'])
        expect(page.get_by_role('button', name='评估适配', exact=True)).to_be_enabled()
        page.screenshot(path=str(output / f'{width}-02-manual-jd.png'), full_page=True)
        results.append('manual full JD persisted on original record with user source and old evidence')

        expand = page.get_by_role('button', name=re.compile('展开.*(?:岗位|职责|全文|描述)|查看完整'))
        if expand.count() and expand.first.is_visible():
            expand.first.click()
        if width > 800:
            page.locator('.detail-panel').evaluate('(element) => element.scrollTop = 150')
        else:
            page.locator('.detail-content').scroll_into_view_if_needed()
            page.evaluate('window.scrollBy(0, 180)')
        before = page.evaluate('({window: window.scrollY, detail: document.querySelector(".detail-panel").scrollTop})')
        assert before['window'] > 0 or before['detail'] > 0, before
        page.get_by_role('button', name='问这份岗位', exact=True).click()
        expect(page.locator('.job-chat')).to_be_visible()
        expect(page.locator('.detail-panel')).to_be_hidden()
        page.screenshot(path=str(output / f'{width}-03-job-chat.png'), full_page=True)
        page.get_by_role('button', name='返回岗位', exact=True).click()
        expect(page.locator('.detail-panel')).to_be_visible()
        expect(page.locator('.job-chat')).to_be_hidden()
        page.wait_for_timeout(150)
        after = page.evaluate('({window: window.scrollY, detail: document.querySelector(".detail-panel").scrollTop})')
        assert abs(after['detail'] - before['detail']) < 4 and abs(after['window'] - before['window']) < 4, (before, after)
        page.screenshot(path=str(output / f'{width}-04-reading-restored.png'), full_page=True)
        results.append({'job chat and return restored reading position': {'before': before, 'after': after}})

        navigate(page, '数据与采集')
        page.locator('#browser-query').fill(title)
        page.get_by_role('button', name='开始采集', exact=False).click()
        page.wait_for_function('() => document.querySelector(".toast-viewport")?.textContent.includes("后台采集")')
        assert state['collect_started']
        navigate(page, '我的简历')
        draft = f'虚构未保存简历草稿 {stamp}：这段文字必须在后台任务完成后仍可编辑。'
        editor = page.locator('#resume-text')
        editor.fill(draft)
        expect(editor).to_be_enabled()
        expect(page.get_by_role('button', name='保存到本地', exact=False)).to_be_enabled()
        state['complete'] = True
        expect(page.get_by_role('button', name='查看岗位', exact=True)).to_be_visible(timeout=10000)
        expect(editor).to_have_value(draft)
        expect(editor).to_be_enabled()
        expect(page.get_by_role('heading', name='我的简历', exact=True)).to_be_visible()
        editor.fill(draft + ' 仍可继续输入。')
        page.screenshot(path=str(output / f'{width}-05-background-resume.png'), full_page=True)
        assert state['polls'] > 0
        results.append('mock background collection completed; resume draft and view preserved, editing/save enabled')
        page.get_by_role('button', name='查看岗位', exact=True).click()
        expect(page.get_by_role('heading', name='岗位研究', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), 'horizontal overflow'
        results.append('completion action explicitly returns to job library without horizontal overflow')
        return {'width': width, 'passed': results}
    finally:
        if job_id:
            deleted = page.request.post(f'{base}/api/jobs/batch-delete', data={'ids': [job_id]})
            assert deleted.ok, deleted.text()
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    parser.add_argument('--test-token', default='61a29514')
    parser.add_argument('--output', default='output/ux-agent-validation-20261113')
    args = parser.parse_args()
    parsed = urlparse(args.url)
    assert parsed.port == 8766 and parsed.hostname in ('127.0.0.1', 'localhost'), 'Only isolated localhost:8766 is allowed'
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        request = p.request.new_context()
        health = request.get(args.url + '/api/health')
        assert health.ok and health.headers.get('x-jobscope-ux-test') == args.test_token, 'Missing isolated test instance marker'
        request.dispose()
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            results = [run_flow(browser, args.url.rstrip('/'), output, width) for width in (1280, 390)]
            report = {'status': 'passed', 'results': results}
            (output / 'reading-flow-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
