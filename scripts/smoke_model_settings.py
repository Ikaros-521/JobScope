"""Model settings acceptance against an isolated app and local fixture provider."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

OUT = Path(__file__).resolve().parents[1] / 'output' / 'model-settings-ui'
OUT.mkdir(parents=True, exist_ok=True)
calls = []


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': 'fixture-model'}, {'id': 'fixture-updated'}, {'id': 'other-model'}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(body)
        if self.path.endswith('embeddings'):
            result = {'data': [{'index': i, 'embedding': [1., .5]} for i, _ in enumerate(body['input'])]}
        else:
            content = 'OK'
            if len(body['messages']) > 1 or body.get('system'):
                data = json.loads(body['messages'][-1]['content'])
                if 'baseline' in data:
                    content = json.dumps(data['baseline'])
                elif 'evidence' in data:
                    eid = data['evidence'][0]['id']
                    content = json.dumps({'answer': f'本地验收模型回答：岗位需要Python [{eid}]', 'evidence_ids': [eid]}, ensure_ascii=False)
            result = {'content': [{'type': 'text', 'text': content}]} if self.path.endswith('/messages') else {'choices': [{'message': {'content': content}}]}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:8766'
endpoint = f'http://127.0.0.1:{server.server_port}'
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.request.put(BASE + '/api/settings/models', data={'llm': {'mode': 'disabled', 'clear_key': True}, 'embedding': {'mode': 'disabled', 'clear_key': True}})
        page.request.post(BASE + '/api/import/job', data={'title': 'Python开发', 'company': '设置验收公司', 'description': '负责Python开发与测试'})
        page.goto(BASE)
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='模型设置', exact=True).click()
        llm = page.get_by_role('form', name='对话与分析模型', exact=True)
        llm.get_by_label('连接方式').select_option('remote')
        llm.get_by_role('button', name='DeepSeek', exact=True).click()
        expect(llm.get_by_label('服务地址', exact=True)).to_have_value('https://api.deepseek.com/v1')
        expect(llm.get_by_label('接口协议', exact=True)).to_have_value('openai')
        llm.get_by_role('button', name='Anthropic', exact=True).click()
        expect(llm.get_by_label('接口协议', exact=True)).to_have_value('anthropic')
        llm.get_by_role('button', name='自定义渠道', exact=True).click()
        llm.get_by_label('服务地址', exact=True).fill(endpoint + '/v1')
        llm.get_by_role('button', name='获取模型列表', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('3 个模型')
        llm.get_by_label('搜索模型或输入 ID', exact=True).fill('fixture-m')
        expect(llm.get_by_role('group', name='模型搜索结果').get_by_role('button')).to_have_count(1)
        llm.get_by_role('group', name='模型搜索结果').get_by_role('button', name='fixture-model').click()
        assert llm.get_by_label('API 密钥', exact=False).get_attribute('type') == 'pass' + 'word'
        llm.get_by_label('API 密钥', exact=False).fill('ui-fixture-credential')
        llm.get_by_role('button', name='测试连接', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('连接成功')
        assert page.request.get(BASE + '/api/health').json()['model_configured'] is False
        llm.get_by_role('button', name='保存设置', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('已保存')
        expect(llm.get_by_label('API 密钥', exact=False)).to_have_value('')
        assert 'ui-fixture-credential' not in page.request.get(BASE + '/api/settings/models').text()
        assert page.request.get(BASE + '/api/health').json()['model_configured'] is True
        # Same-address edits retain the key; a provider change requires explicit key handling.
        llm.get_by_label('搜索模型或输入 ID', exact=True).fill('fixture-updated')
        llm.get_by_role('button', name='保存设置', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('已保存')
        llm.get_by_label('服务地址', exact=True).fill(endpoint + '/changed')
        expect(llm.get_by_role('button', name='测试连接', exact=True)).to_be_disabled()
        expect(llm.get_by_text('已更换服务地址', exact=False)).to_be_visible()
        llm.get_by_label('服务地址', exact=True).fill(endpoint + '/v1/chat/completions')
        embedding = page.get_by_role('form', name='语义检索模型', exact=True)
        embedding.get_by_label('连接方式').select_option('remote')
        embedding.get_by_role('button', name='自定义渠道', exact=True).click()
        embedding.get_by_label('服务地址', exact=True).fill(endpoint + '/v1/embeddings')
        embedding.get_by_label('搜索模型或输入 ID', exact=True).fill('fixture-vector')
        embedding.get_by_role('button', name='测试连接', exact=True).click()
        expect(embedding.get_by_role('status')).to_contain_text('2 维')
        page.screenshot(path=str(OUT / '01-settings-desktop.png'), full_page=True)
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        page.get_by_label('围绕全项目资料提问').fill('Python岗位要求')
        page.get_by_role('button', name='发送全局问题').click()
        try:
            expect(page.locator('.global-chat-panel .chat-message.assistant')).to_contain_text('本地验收模型回答', timeout=15000)
        except AssertionError:
            print(page.locator('.global-chat-panel').inner_text())
            print('Fixture calls:', len(calls))
            page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
            raise
        assert any(call.get('model') == 'fixture-updated' for call in calls)
        page.reload()
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='模型设置', exact=True).click()
        llm = page.get_by_role('form', name='对话与分析模型', exact=True)
        expect(llm.get_by_label('搜索模型或输入 ID', exact=True)).to_have_value('fixture-updated')
        expect(llm.get_by_label('API 密钥', exact=False)).to_have_value('')
        llm.get_by_label('接口协议', exact=True).select_option('anthropic')
        expect(llm.get_by_role('button', name='测试连接', exact=True)).to_be_disabled()
        llm.get_by_label('API 密钥', exact=False).fill('ui-fixture-credential')
        llm.get_by_role('button', name='测试连接', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('连接成功')
        llm.get_by_role('button', name='保存设置', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('已保存')
        assert page.request.get(BASE + '/api/settings/models').json()['llm']['protocol'] == 'anthropic'
        llm.get_by_label('清除已保存密钥').check()
        llm.get_by_role('button', name='保存设置', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('已保存')
        assert not page.request.get(BASE + '/api/settings/models').json()['llm']['key_set']
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(OUT / '02-settings-mobile.png'), full_page=True)
        llm.get_by_label('连接方式').select_option('disabled')
        llm.get_by_role('button', name='保存设置', exact=True).click()
        expect(llm.get_by_role('status')).to_contain_text('已保存')
        assert not page.request.get(BASE + '/api/health').json()['model_configured']
        assert not errors, errors
        print(json.dumps(dict(model_settings_ui='passed', draft_test='passed', saved_model_chat='passed', masked_key='passed', retain_clear_key='passed', mobile_overflow=False, console_errors=errors)))
        browser.close()
finally:
    server.shutdown()
