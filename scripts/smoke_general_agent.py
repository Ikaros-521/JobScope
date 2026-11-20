"""Full global Agent UI workflow against an isolated app and local fixtures."""
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parents[1] / 'output' / 'general-agent-ui'
OUT.mkdir(parents=True, exist_ok=True)


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body['messages']
        last = messages[-1]
        calls = None
        answer = '通用 Agent 回答：先学习工具调用，再完成一个可验证的项目。'
        if last['role'] == 'tool':
            answer = '工具调用完成：' + last['content'][:200]
        elif '测试MCP' in last['content']:
            spec = next(tool['function'] for tool in body['tools'] if tool['function']['name'].startswith('mcp_'))
            calls = [dict(id='fixture-call', type='function', function=dict(name=spec['name'], arguments=json.dumps({'query': 'Agent工具'})))]
        elif '面试演练' in last['content']:
            system = messages[0]['content']
            import re
            skill_id = re.search(r'S-[a-f0-9]{16}', system)[0]
            calls = [dict(id='fixture-skill', type='function', function=dict(name='skill_read', arguments=json.dumps({'skill_id': skill_id})))]
        # UI 路径走 stream_turn（stream=true）：用 SSE 分片回放同一份脚本化应答。
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            def emit(payload):
                self.wfile.write(b'data: ' + json.dumps(payload).encode() + b'\n\n')
            if calls:
                for index, call in enumerate(calls):
                    emit(dict(choices=[dict(delta=dict(tool_calls=[dict(index=index, id=call['id'], type='function', function=dict(name=call['function']['name'], arguments=call['function']['arguments']))]))]))
            else:
                emit(dict(choices=[dict(delta=dict(content=answer))]))
            self.wfile.write(b'data: [DONE]\n\n')
            return
        result = dict(choices=[dict(message=dict(role='assistant', content=None if calls else answer, **({'tool_calls': calls} if calls else {})))])
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:8766'
# fixture MCP 服务器需要 mcp 包：固定用项目 .venv 解释器，而不是本脚本的运行时。
VENV_PYTHON = str(Path(__file__).resolve().parents[1] / '.venv' / 'Scripts' / 'python.exe')
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.request.put(BASE + '/api/settings/models', data={'llm': dict(mode='remote', url=f'http://127.0.0.1:{server.server_port}/v1', model='fixture', clear_key=True)})
        # Clean only disposable fixture extensions in this isolated service.
        extensions = page.request.get(BASE + '/api/agent/extensions').json()
        for item in extensions['servers']:
            page.request.delete(BASE + '/api/agent/mcp/' + item['id'])
        for item in extensions['skills']:
            page.request.delete(BASE + '/api/agent/skills/' + item['id'])
        page.goto(BASE)
        # 新档案会自动弹教程遮罩（拦截点击）：等它出现按 Esc 跳过，再展开「对话与知识」组。
        try:
            page.get_by_role('dialog', name='使用教程').wait_for(state='attached', timeout=15000)
            page.keyboard.press('Escape')
        except Exception:
            pass
        nav_toggle = page.get_by_role('button', name='对话与知识', exact=True).first
        if nav_toggle.get_attribute('aria-expanded') == 'false':
            nav_toggle.click()
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        # 对话范围=视图（全局对话即通用 Agent），扩展入口在 composer 工具条。
        page.get_by_role('button', name='MCP 与 Skills', exact=True).click()
        expect(page.get_by_role('heading', name='Agent 扩展', exact=True)).to_be_visible()
        # 扩展页默认在「MCP 市场」页签：先切到「MCP 工具」（页签名带计数）。
        page.get_by_role('tab', name='MCP 工具', exact=False).first.click()
        expect(page.get_by_label('连接名称', exact=True)).not_to_be_visible()
        page.get_by_role('button', name='添加 MCP 连接', exact=True).click()
        page.get_by_role('button', name='取消', exact=True).click()
        expect(page.get_by_label('连接名称', exact=True)).not_to_be_visible()
        page.get_by_role('button', name='添加 MCP 连接', exact=True).click()
        page.get_by_label('连接名称', exact=True).fill('验收 MCP')
        page.get_by_label('连接方式', exact=True).select_option('stdio')
        page.get_by_label('可执行命令', exact=True).fill(VENV_PYTHON)
        page.get_by_role('button', name='＋ 添加参数', exact=True).click()
        page.get_by_label('启动参数 1', exact=True).fill(str(Path(__file__).with_name('fixture_mcp.py')))
        page.get_by_role('button', name='保存连接', exact=True).click()
        expect(page.locator('.toast-viewport')).to_contain_text('连接已保存')
        page.get_by_role('button', name='测试并发现工具', exact=True).click()
        expect(page.locator('.toast-viewport')).to_contain_text('测试成功', timeout=45000)
        page.get_by_role('button', name='全部允许', exact=True).click()
        expect(page.locator('.toast-viewport')).to_contain_text('权限已更新')
        page.locator('.ext-detail').get_by_text('启用连接', exact=True).click()
        expect(page.locator('.toast-viewport')).to_contain_text('权限已更新')
        page.get_by_label('搜索连接', exact=True).fill('nothing-matches')
        expect(page.get_by_text('没有匹配的连接', exact=True)).to_be_visible()
        page.get_by_label('搜索连接', exact=True).fill('')
        expect(page.get_by_text('可供 Agent 使用', exact=True)).to_be_visible()
        page.screenshot(path=str(OUT / '05-extensions-mcp.png'), full_page=True)
        page.get_by_role('tab', name=re.compile(r'^Skills \d')).click()
        page.get_by_role('button', name='导入 Skill', exact=True).click()
        page.get_by_label('SKILL.md 内容', exact=True).fill('---\nname: interview-coach\ndescription: 面试演练与反馈\n---\n每次只问一个问题，按 STAR 结构反馈。')
        page.get_by_role('button', name='粘贴导入', exact=True).click()
        expect(page.locator('.toast-viewport')).to_contain_text('Skill 已导入')
        page.screenshot(path=str(OUT / '04-extensions-skills.png'), full_page=True)
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        expect(page.locator('.chat-global')).to_be_visible()
        page.locator('.chat-global .chat-composer textarea').fill('帮我制定学习计划')
        page.locator('.chat-global').get_by_role('button', name='发送消息', exact=True).click()
        expect(page.locator('.chat-global .chat-bubble.assistant').last).to_contain_text('先学习工具调用')
        page.locator('.chat-global .chat-composer textarea').fill('测试MCP')
        page.locator('.chat-global').get_by_role('button', name='发送消息', exact=True).click()
        approval = page.get_by_role('region', name='待确认工具调用')
        expect(approval).to_contain_text('lookup_topic')
        expect(approval).to_contain_text('Agent工具')
        page.screenshot(path=str(OUT / '01-tool-approval.png'), full_page=True)
        page.get_by_role('button', name='确认执行', exact=True).click()
        expect(page.locator('.chat-global .chat-bubble.assistant').last).to_contain_text('MCP fixture result', timeout=45000)
        page.locator('.chat-global .chat-composer textarea').fill('面试演练')
        page.locator('.chat-global').get_by_role('button', name='发送消息', exact=True).click()
        expect(page.locator('.chat-global .chat-bubble.assistant').last).to_contain_text('STAR')
        # 顺序模式：工具步骤以 .tool-step 行内联呈现（步骤名即摘要），无需再展开折叠。
        expect(page.locator('.chat-global .chat-bubble.assistant').last).to_contain_text('加载 Skill 指令')
        page.screenshot(path=str(OUT / '02-tool-and-skill-result.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(OUT / '03-mobile.png'), full_page=True)
        assert not errors, errors
        print(json.dumps(dict(general_agent_ui='passed', mcp_stdio='passed', tool_approval_resume='passed', skills='passed', mobile_overflow=False, page_errors=errors)))
        browser.close()
finally:
    server.shutdown()
