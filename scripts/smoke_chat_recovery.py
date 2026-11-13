"""Controlled chat UX regression; API mutations are mocked, never sent to storage.
Run: .venv\\Scripts\\python.exe scripts/smoke_chat_recovery.py --url http://127.0.0.1:8766
Requires the isolated test frontend, Python playwright and installed Chrome.
"""
import argparse
import json
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    assert urlparse(args.url).port == 8766, 'Only isolated 8766 is allowed'
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 720})
        configured = [False]
        requests = []
        detail = {'id': 'ux-fixture', 'scope': {'kind': 'global'}, 'title': '受控失败', 'mode': 'general', 'model': '', 'llm_channel': '', 'permission_mode': 'autonomous', 'messages': [], 'active_run': None, 'conditions': {'location': '杭州', 'threshold': 25000, 'operator': 'ge', 'threshold_basis': 'monthly_min', 'exclude_outsourcing': True, 'clarification': ''}}

        branches = {}

        def intercept(route):
            path = urlparse(route.request.url).path
            if path == '/api/health':
                return route.fulfill(json={'status': 'ok', 'model_configured': configured[0]})
            if path == '/api/conversations':
                return route.fulfill(json={'conversations': []})
            if path.startswith('/api/conversations/'):
                return route.fulfill(json=branches.get(path.rsplit('/', 1)[-1], detail))
            if path == '/api/chat/runs' and route.request.method == 'POST':
                body = route.request.post_data_json
                requests.append(body)
                conversation_id = 'ux-fixture' if len(requests) == 1 else 'ux-retry-branch'
                branch = json.loads(json.dumps(detail))
                branch['id'] = conversation_id
                branch['title'] = '重试分支' if len(requests) > 1 else '受控失败'
                branch['messages'] = [{'id': 'ux-question', 'role': 'user', 'content': body['message'], 'ts': ''}]
                branches[conversation_id] = branch
                return route.fulfill(json={'conversation_id': conversation_id, 'user_message_id': 'ux-question', 'run_id': 'ux-run', 'title': branch['title']})
            if path == '/api/chat/runs/ux-run/stream':
                return route.fulfill(content_type='text/event-stream', body='event: error\ndata: {"message":"受控模型失败"}\n\nevent: end\ndata: {}\n\n')
            if route.request.method not in ('GET', 'HEAD'):
                raise AssertionError(f'Unexpected mutation: {route.request.method} {path}')
            return route.continue_()

        page.route('**/api/**', intercept)
        page.goto(args.url)
        page.get_by_role('button', name='全局对话', exact=True).click()
        box = page.locator('.chat-composer textarea')
        expect(box).to_be_enabled()
        box.fill('杭州至少25K，不考虑外包')
        expect(page.get_by_role('button', name='发送消息', exact=True)).to_be_disabled()
        page.get_by_role('button', name='配置模型后发送（草稿已保存）', exact=True).click()
        page.get_by_role('button', name='返回原工作区', exact=True).click()
        expect(box).to_have_value('杭州至少25K，不考虑外包')
        page.reload()
        page.get_by_role('button', name='全局对话', exact=True).click()
        expect(box).to_have_value('杭州至少25K，不考虑外包')
        configured[0] = True
        page.reload()
        page.get_by_role('button', name='全局对话', exact=True).click()
        page.get_by_role('button', name='发送消息', exact=True).click()
        expect(page.get_by_text('受控模型失败', exact=True)).to_be_visible()
        expect(page.locator('.chat-state')).to_have_text('空闲')
        expect(page.locator('.chat-bubble.user')).to_have_count(1)
        page.get_by_role('button', name='重试本轮问题', exact=True).click()
        expect(page.get_by_text('受控模型失败', exact=True)).to_be_visible()
        assert len(requests) == 2 and requests[1]['edit_message_id'] == 'ux-question'
        assert requests[1]['conversation_id'] == 'ux-fixture'
        expect(page.locator('.chat-main-title h2')).to_have_text('重试分支')
        assert page.evaluate("sessionStorage.getItem('jobscope-conversation:global')") == 'ux-retry-branch'
        assert branches['ux-fixture']['id'] == 'ux-fixture' and len(branches['ux-fixture']['messages']) == 1
        expect(page.locator('.chat-bubble.user')).to_have_count(1)
        box.fill('普通草稿')
        page.locator('.msg-edit-toggle').click()
        page.locator('.msg-editing textarea').fill('历史编辑不应覆盖草稿')
        page.locator('.msg-editing').get_by_role('button', name='取消', exact=True).click()
        expect(box).to_have_value('普通草稿')
        page.get_by_role('button', name='取消薪资', exact=True).click()
        expect(box).to_have_value('取消薪资限制')
        for width in (1280, 390):
            page.set_viewport_size({'width': width, 'height': 720})
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Overflow at {width}'
        browser.close()
        print('PASS: editable unconfigured composer, draft restoration, terminal failure, branch retry, edit isolation, visible conditions, viewport overflow')


if __name__ == '__main__':
    main()
