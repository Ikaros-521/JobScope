"""Controlled cross-page favorites smoke. No mutation reaches the backend.
Run: .venv\\Scripts\\python.exe scripts/smoke_favorites_selection.py --url http://127.0.0.1:8766
Requires Python playwright and Chrome; serve an isolated current-source frontend.
"""
import argparse
import re
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    assert urlparse(args.url).port == 8766, 'Only isolated 8766 is allowed'
    jobs = [dict(id=f'favorite-{i}', title=f'受控岗位{i}', company=company, location='杭州', salary='25K', url='', source='manual', updated_at='', description='受控收藏', evidence_ids=[], tags=[], starred=True) for i, company in enumerate(['受控甲公司', '受控乙公司', '受控甲公司'])]
    mutations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 720})

        def intercept(route):
            parsed = urlparse(route.request.url)
            if parsed.path == '/api/jobs' and route.request.method == 'GET':
                query = parse_qs(parsed.query)
                page_no = int(query.get('page', ['1'])[0])
                # Two first-page jobs from one company exercise company deduplication.
                if page_no == 1:
                    current = [jobs[0], jobs[2]]
                else:
                    current = [jobs[1]]
                current = [job for job in current if job['starred']]
                if query.get('q'):
                    current = [job for job in current if query['q'][0] in job['company']]
                return route.fulfill(json={'jobs': current, 'page': page_no, 'page_size': 2, 'pages': 2, 'total': 3})
            if parsed.path == '/api/jobs' and route.request.method == 'PATCH':
                body = route.request.post_data_json
                mutations.append(('unstar', body['ids']))
                for job in jobs:
                    if job['id'] in body['ids']:
                        job['starred'] = False
                return route.fulfill(json={'updated': len(body['ids'])})
            if parsed.path == '/api/browser/details':
                body = route.request.post_data_json
                mutations.append(('recheck', body['ids']))
                return route.fulfill(json={'task': {'id': 'controlled-recheck', 'status': 'completed', 'message': '受控反查', 'source': '', 'query': '', 'created_at': '', 'count': len(body['ids'])}})
            if route.request.method not in ('GET', 'HEAD'):
                raise AssertionError(f'Unexpected mutation {route.request.method}: {parsed.path}')
            return route.continue_()

        page.route('**/api/**', intercept)
        page.goto(args.url)
        page.locator('.sidebar').get_by_role('button', name=re.compile(r'^收藏')).click()
        page.get_by_label('选择岗位：受控岗位0 · 受控甲公司', exact=True).check()
        page.get_by_label('选择岗位：受控岗位2 · 受控甲公司', exact=True).check()
        page.get_by_role('button', name='收藏夹下一页', exact=True).click()
        page.get_by_label('选择岗位：受控岗位1 · 受控乙公司', exact=True).check()
        expect(page.get_by_text('跨页已选 3 项（本页 1 项）', exact=True)).to_be_visible()
        page.get_by_role('button', name='横评选中公司', exact=True).click()
        page.get_by_role('button', name=re.compile(r'^数据分析')).click()
        expect(page.locator('.compare-chip')).to_have_count(2)
        expect(page.locator('.compare-chip').filter(has_text='受控甲公司')).to_have_count(1)
        expect(page.locator('.compare-chip').filter(has_text='受控乙公司')).to_have_count(1)
        page.locator('.sidebar').get_by_role('button', name=re.compile(r'^收藏')).click()
        # Selection is scoped to this favorites mount; reselect to verify bulk request IDs.
        page.get_by_label('选择岗位：受控岗位0 · 受控甲公司', exact=True).check()
        page.get_by_role('button', name='收藏夹下一页', exact=True).click()
        page.get_by_label('选择岗位：受控岗位1 · 受控乙公司', exact=True).check()
        page.get_by_role('button', name='反查选中', exact=True).click()
        expect(page.get_by_role('button', name='反查选中', exact=True)).to_be_enabled()
        assert mutations[-1] == ('recheck', ['favorite-0', 'favorite-1'])
        page.locator('.favorites-batch').get_by_role('button', name='取消收藏', exact=True).click()
        expect(page.get_by_role('button', name='横评选中公司', exact=True)).to_have_count(0)
        assert mutations[-1] == ('unstar', ['favorite-0', 'favorite-1'])
        page.get_by_label('选择岗位：受控岗位2 · 受控甲公司', exact=True).check()
        page.get_by_role('textbox', name='收藏内搜索', exact=True).fill('甲公司')
        page.get_by_role('button', name='筛选', exact=True).click()
        expect(page.get_by_text('筛选已变更，已清空此前跨页选择。', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='横评选中公司', exact=True)).to_have_count(0)
        browser.close()
        print('PASS: cross-page selection, company deduplication, compare UI, bulk IDs and filter reset')


if __name__ == '__main__':
    main()



