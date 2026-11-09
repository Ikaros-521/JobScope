"""Verify browser collection feedback with a separate UI test database."""

import argparse
import json

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1050})
        page.goto(args.url)
        # A real saved record verifies that successful collection opens its dossier.
        response = page.request.post(args.url + '/api/import/job', data={
            'title': '虚构采集反馈验收岗位', 'company': '虚构反馈验收公司',
            'description': 'Python 后端开发，仅供隔离测试库验收。',
        })
        assert response.ok
        job = response.json()['job']
        page.reload()
        page.get_by_role('navigation').get_by_role('button', name='数据与采集', exact=True).click()
        page.get_by_label('BOSS 岗位关键词', exact=True).fill('Python')

        def success(route):
            expect(page.get_by_role('status')).to_contain_text('正在读取搜索结果')
            route.fulfill(content_type='application/json', body=json.dumps({
                'task': {'status': 'completed', 'count': 1, 'message': '列表摘要，完整职责尚未读取'},
                'jobs': [job],
            }))

        page.route('**/api/browser/collect', success)
        collect = page.get_by_role('button', name='从已登录浏览器采集', exact=False)
        collect.click()
        expect(page.get_by_role('heading', name='岗位研究', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name=job['title'], level=2)).to_be_visible()
        expect(page.get_by_role('status')).to_contain_text('已保存或更新 1 条岗位')
        expect(page.get_by_role('status')).to_contain_text('完整职责尚未读取')
        page.unroute('**/api/browser/collect')

        for status, message in [('needs_verification', '请完成人工验证'), ('completed', '没有匹配的岗位')]:
            page.get_by_role('navigation').get_by_role('button', name='数据与采集', exact=True).click()
            page.route('**/api/browser/collect', lambda route, request, state=status, text=message: route.fulfill(
                content_type='application/json', body=json.dumps({
                    'task': {'status': state, 'count': 0, 'message': text}, 'jobs': [],
                })))
            collect.click()
            expect(page.get_by_role('heading', name='数据与采集', exact=True)).to_be_visible()
            expect(page.get_by_text(message, exact=False)).to_be_visible()
            expect(collect).to_be_enabled()
            page.unroute('**/api/browser/collect')
        print(json.dumps({'collection_feedback': 'passed', 'success_opens_dossier': True,
                          'progress': True, 'verification_and_empty_stay_in_sources': True}))
        browser.close()


if __name__ == '__main__':
    main()
