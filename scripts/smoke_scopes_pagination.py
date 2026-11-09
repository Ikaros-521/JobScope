"""Acceptance against an isolated server seeded with two fictional companies."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / 'output' / 'ux-audit'
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url)
        total_jobs = page.request.get(args.url + '/api/jobs?page_size=1').json()['total']
        expect(page.locator('.job-card')).to_have_count(20)
        first_title = page.locator('.job-card h3').first.inner_text()
        page.get_by_role('button', name='岗位档案下一页', exact=True).click()
        expect(page.locator('.job-card')).to_have_count(min(20, total_jobs - 20))
        assert page.locator('.job-card h3').first.inner_text() != first_title
        page.get_by_label('岗位关键词', exact=True).fill('NOT-FOUND')
        page.get_by_role('button', name='筛选本地', exact=True).click()
        expect(page.locator('.job-card')).to_have_count(0)
        expect(page.get_by_role('button', name='岗位档案下一页', exact=True)).to_be_disabled()

        page.get_by_role('navigation', name='主导航').get_by_role('button', name='公司资料库', exact=True).click()
        expect(page.locator('.library-item')).to_have_count(10)
        first_evidence = page.locator('.library-item').first.inner_text()
        page.get_by_role('button', name='来源档案下一页', exact=True).click()
        expect(page.locator('.library-item')).to_have_count(10)
        assert page.locator('.library-item').first.inner_text() != first_evidence
        page.get_by_label('公司主体（精确匹配）').fill('验收甲公司')
        page.get_by_role('button', name='查询资料', exact=True).click()
        expect(page.locator('.library-item')).to_have_count(10)
        assert all('验收甲公司' in text for text in page.locator('.library-company').all_text_contents())
        expect(page.get_by_role('navigation', name='来源档案分页')).to_contain_text('1–10 / 26')
        page.screenshot(path=str(output / '03-after-records.png'), full_page=True)

        page.get_by_role('tab', name='来源档案', exact=False).focus()
        page.keyboard.press('ArrowRight')
        expect(page.get_by_role('tab', name='公司对话', exact=False)).to_be_focused()
        expect(page.get_by_role('tab', name='公司对话', exact=False)).to_have_attribute('aria-selected', 'true')
        page.get_by_label('围绕当前公司提问').fill('薪资和团队情况')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message.assistant')).to_be_visible()
        assert '验收乙公司' not in page.locator('.company-chat-panel .chat-message.assistant').inner_text()
        expect(page.locator('.company-chat-panel .citation')).not_to_have_count(0)
        assert all('验收甲公司' in text for text in page.locator('.company-chat-panel .citation summary').all_text_contents())
        page.get_by_role('tab', name='来源档案', exact=False).click()
        expect(page.locator('.library-item')).to_have_count(10)
        page.get_by_role('tab', name='公司对话', exact=False).click()
        expect(page.locator('.company-chat-panel .chat-message.assistant')).to_have_count(1)
        page.screenshot(path=str(output / '04-after-company-chat.png'), full_page=True)
        page.get_by_label('选择对话公司').fill('验收乙公司')
        page.get_by_role('button', name='确认公司', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message')).to_have_count(0)
        expect(page.locator('.company-chat-panel .scope-banner')).to_contain_text('验收乙公司')
        page.get_by_label('围绕当前公司提问').fill('薪资和团队情况')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message.assistant')).to_be_visible()
        assert '验收甲公司' not in page.locator('.company-chat-panel .chat-message.assistant').inner_text()

        # Hold a response until the company has changed, then deliver the old answer.
        delayed = []
        page.route('**/api/chat', lambda route: delayed.append(route))
        page.get_by_label('围绕当前公司提问').fill('等待中的乙公司问题')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-thinking')).to_be_visible()
        page.get_by_label('选择对话公司').fill('验收甲公司')
        page.get_by_role('button', name='确认公司', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message')).to_have_count(0)
        assert delayed
        delayed[0].fulfill(content_type='application/json', body=json.dumps({
            'answer': '乙公司迟到的回复不得进入甲公司', 'citations': [],
            'mode': 'evidence_search', 'conversation_id': 'C-late',
        }))
        page.unroute('**/api/chat')
        page.get_by_label('围绕当前公司提问').fill('甲公司的团队')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message.assistant')).to_have_count(1)
        assert '乙公司迟到的回复' not in page.locator('.company-chat-panel').inner_text()

        # A new conversation also discards an in-flight answer in the same scope.
        delayed.clear()
        page.route('**/api/chat', lambda route: delayed.append(route))
        page.get_by_label('围绕当前公司提问').fill('等待中的旧会话')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-thinking')).to_be_visible()
        page.locator('.company-chat-panel').get_by_role('button', name='新建对话').click()
        expect(page.locator('.company-chat-panel .chat-message')).to_have_count(0)
        assert delayed
        delayed[0].fulfill(content_type='application/json', body=json.dumps({
            'answer': '旧会话迟到的回复不得显示', 'citations': [],
            'mode': 'evidence_search', 'conversation_id': 'C-old',
        }))
        page.unroute('**/api/chat')
        page.get_by_label('围绕当前公司提问').fill('新会话薪资情况')
        page.get_by_role('button', name='发送公司问题', exact=True).click()
        expect(page.locator('.company-chat-panel .chat-message.assistant')).to_have_count(1)
        assert '旧会话迟到的回复' not in page.locator('.company-chat-panel').inner_text()

        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        page.get_by_label('围绕全项目资料提问').fill('薪资和团队资料')
        page.get_by_role('button', name='发送全局问题', exact=True).click()
        expect(page.locator('.global-chat-panel .chat-message.assistant')).to_be_visible()
        answer = page.locator('.global-chat-panel .chat-message.assistant').inner_text()
        assert '验收甲公司' in answer and '验收乙公司' in answer
        page.screenshot(path=str(output / '05-after-global-chat.png'), full_page=True)

        page.set_viewport_size({'width': 390, 'height': 844})
        for view in ['岗位研究', '我的简历', '公司资料库', '全局对话', '数据与采集']:
            page.get_by_role('navigation', name='主导航').get_by_role('button', name=view, exact=False).click()
            expect(page.get_by_role('heading', name=view, exact=True)).to_be_visible()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), view
        assert not errors, errors
        print(json.dumps({'scoped_company_chat': 'passed', 'late_response_isolation': 'passed', 'new_conversation_isolation': 'passed', 'global_chat': 'passed', 'pagination_and_filter_reset': 'passed', 'mobile_views': 5, 'console_errors': errors}))
        browser.close()


if __name__ == '__main__':
    main()
