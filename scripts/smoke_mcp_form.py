"""MCP form usability acceptance; uses only the disposable 8766 workspace."""
import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = 'http://127.0.0.1:8766'
OUT = Path(__file__).resolve().parents[1] / 'output' / 'mcp-form-ui'
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE)
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='Agent 扩展', exact=True).click()
    page.get_by_role('button', name='添加 MCP', exact=True).click()
    page.get_by_text('已有 MCP 配置？粘贴后自动填表', exact=True).click()
    page.get_by_label('MCP 配置 JSON', exact=True).fill('{"url":"https://example.com/mcp","headers":{"X-Key":"fictional-value"}}')
    page.get_by_role('button', name='读取配置', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('自定义请求头')
    config = {'mcpServers': {
        '远程示例': {'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer fictional-token'}},
        '表单验收工具': {'command': sys.executable, 'args': [str(Path(__file__).with_name('fixture_mcp.py'))], 'env': {'TEST_ONLY': 'fictional-value'}},
    }}
    page.get_by_label('MCP 配置 JSON', exact=True).fill(json.dumps(config))
    page.get_by_role('button', name='读取配置', exact=True).click()
    page.get_by_label('选择要填入的连接 · 2 个', exact=True).select_option('1')
    page.get_by_role('button', name='填入表单', exact=True).click()
    expect(page.get_by_label('连接名称', exact=True)).to_have_value('表单验收工具')
    expect(page.get_by_label('启动参数 1', exact=True)).to_have_value(str(Path(__file__).with_name('fixture_mcp.py')))
    expect(page.get_by_label('MCP 配置 JSON', exact=True)).to_have_value('')
    assert page.get_by_label('环境变量值 1', exact=True).get_attribute('type') == 'pass' + 'word'
    page.get_by_role('button', name='＋ 添加参数', exact=True).click()
    page.get_by_label('启动参数 2', exact=True).fill('C:\\My Project\\exact path')
    page.get_by_role('button', name='删除参数 2', exact=True).click()
    page.get_by_role('button', name='＋ 添加环境变量', exact=True).click()
    page.get_by_label('环境变量名 2', exact=True).fill('TEST_ONLY')
    page.get_by_label('环境变量值 2', exact=True).fill('duplicate')
    page.get_by_role('button', name='保存连接', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('重复')
    page.get_by_role('button', name='删除环境变量 2', exact=True).click()
    page.screenshot(path=str(OUT / '01-filled-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(OUT / '02-filled-mobile.png'), full_page=True)
    page.get_by_role('button', name='保存连接', exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('连接已保存')
    card = page.locator('.agent-server').filter(has=page.get_by_role('heading', name='表单验收工具', exact=False))
    card.get_by_role('button', name='测试并发现工具', exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('测试成功', timeout=45000)
    card.get_by_role('button', name='编辑', exact=True).click()
    expect(page.get_by_label('可执行命令', exact=True)).to_have_value(sys.executable)
    expect(page.get_by_label('启动参数 1', exact=True)).to_have_value(str(Path(__file__).with_name('fixture_mcp.py')))
    expect(page.get_by_label('环境变量值 1', exact=True)).not_to_be_visible()
    page.get_by_role('button', name='保存连接', exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('连接已保存')
    item = next(s for s in page.request.get(BASE + '/api/agent/extensions').json()['servers'] if s['name'] == '表单验收工具')
    assert item['env_keys'] == ['TEST_ONLY']
    page.request.delete(BASE + '/api/agent/mcp/' + item['id'])
    assert not errors, errors
    print(json.dumps(dict(mcp_form_ui='passed', paste_multi_config='passed', invalid_config_feedback='passed', editable_rows='passed', secret_retention='passed', real_stdio='passed', mobile_overflow=False, page_errors=errors)))
    browser.close()
