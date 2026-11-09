"""Full global Agent UI workflow against an isolated app and local fixtures."""
import json
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
        result = dict(choices=[dict(message=dict(role='assistant', content=None if calls else answer, **({'tool_calls': calls} if calls else {})))])
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:8766'
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
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        page.get_by_label('对话模式', exact=True).select_option('general')
        page.get_by_role('button', name='管理 MCP 与 Skills', exact=False).click()
        expect(page.get_by_role('heading', name='Agent 扩展', exact=True)).to_be_visible()
        expect(page.get_by_label('连接名称', exact=True)).not_to_be_visible()
        page.get_by_role('button', name='添加 MCP', exact=True).click()
        page.get_by_role('button', name='取消', exact=True).click()
        expect(page.get_by_label('连接名称', exact=True)).not_to_be_visible()
        page.get_by_role('button', name='添加 MCP', exact=True).click()
        page.get_by_label('连接名称', exact=True).fill('验收 MCP')
        page.get_by_label('连接方式', exact=True).select_option('stdio')
        page.get_by_label('可执行命令', exact=True).fill(sys.executable)
        page.get_by_role('button', name='＋ 添加参数', exact=True).click()
        page.get_by_label('启动参数 1', exact=True).fill(str(Path(__file__).with_name('fixture_mcp.py')))
        page.get_by_role('button', name='保存连接', exact=True).click()
        expect(page.get_by_role('status')).to_contain_text('连接已保存')
        page.get_by_role('button', name='测试并发现工具', exact=True).click()
        expect(page.get_by_role('status')).to_contain_text('测试成功', timeout=45000)
        page.get_by_label('允许调用', exact=True).check()
        expect(page.get_by_role('status')).to_contain_text('权限已更新')
        page.get_by_label('启用连接', exact=True).check()
        expect(page.get_by_role('status')).to_contain_text('权限已更新')
        page.get_by_label('搜索扩展', exact=True).fill('nothing-matches')
        expect(page.get_by_text('没有匹配的连接', exact=True)).to_be_visible()
        page.get_by_label('搜索扩展', exact=True).fill('')
        expect(page.get_by_text('可供 Agent 使用', exact=True)).to_be_visible()
        page.screenshot(path=str(OUT / '05-extensions-mcp.png'), full_page=True)
        page.get_by_role('group', name='扩展类型').get_by_role('button', name='Skills', exact=False).click()
        page.get_by_role('button', name='导入 Skill', exact=True).click()
        page.get_by_label('SKILL.md 内容', exact=True).fill('---\nname: interview-coach\ndescription: 面试演练与反馈\n---\n每次只问一个问题，按 STAR 结构反馈。')
        page.get_by_role('button', name='确认导入', exact=True).click()
        expect(page.get_by_role('status')).to_contain_text('Skill 已导入')
        page.screenshot(path=str(OUT / '04-extensions-skills.png'), full_page=True)
        page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
        expect(page.get_by_label('对话模式', exact=True)).to_have_value('general')
        page.get_by_label('围绕全项目资料提问', exact=True).fill('帮我制定学习计划')
        page.get_by_role('button', name='发送全局问题').click()
        expect(page.locator('.global-chat-panel .chat-message.assistant').last).to_contain_text('先学习工具调用')
        page.get_by_label('围绕全项目资料提问', exact=True).fill('测试MCP')
        page.get_by_role('button', name='发送全局问题').click()
        approval = page.get_by_role('region', name='待确认工具调用')
        expect(approval).to_contain_text('lookup_topic')
        expect(approval).to_contain_text('Agent工具')
        page.screenshot(path=str(OUT / '01-tool-approval.png'), full_page=True)
        page.get_by_role('button', name='确认执行', exact=True).click()
        expect(page.locator('.global-chat-panel .chat-message.assistant').last).to_contain_text('MCP fixture result', timeout=45000)
        page.get_by_label('围绕全项目资料提问', exact=True).fill('面试演练')
        page.get_by_role('button', name='发送全局问题').click()
        expect(page.locator('.global-chat-panel .chat-message.assistant').last).to_contain_text('STAR')
        page.locator('.global-chat-panel .chat-message.assistant').last.get_by_text('查看 Agent 执行过程', exact=True).click()
        expect(page.locator('.global-chat-panel .chat-message.assistant').last).to_contain_text('加载 Skill 指令')
        page.screenshot(path=str(OUT / '02-tool-and-skill-result.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(OUT / '03-mobile.png'), full_page=True)
        assert not errors, errors
        print(json.dumps(dict(general_agent_ui='passed', mcp_stdio='passed', tool_approval_resume='passed', skills='passed', mobile_overflow=False, page_errors=errors)))
        browser.close()
finally:
    server.shutdown()
