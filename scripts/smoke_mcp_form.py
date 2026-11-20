"""MCP form usability acceptance; uses only the disposable 8766 workspace."""
import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = 'http://127.0.0.1:8766'
OUT = Path(__file__).resolve().parents[1] / 'output' / 'mcp-form-ui'
OUT.mkdir(parents=True, exist_ok=True)
# fixture MCP 服务器需要 mcp 包：固定用项目 .venv 解释器，而不是本脚本的运行时。
VENV_PYTHON = str(Path(__file__).resolve().parents[1] / '.venv' / 'Scripts' / 'python.exe')

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE)
    # 新档案会自动弹教程遮罩（拦截点击）：等它出现按 Esc 跳过。
    try:
        page.get_by_role('dialog', name='使用教程').wait_for(state='attached', timeout=15000)
        page.keyboard.press('Escape')
    except Exception:
        pass
    manage_toggle = page.get_by_role('navigation', name='主导航').get_by_role('button', name='系统管理', exact=True)
    if manage_toggle.get_attribute('aria-expanded') == 'false':
        manage_toggle.click()
    # Agent 扩展不再有独立导航项：从「设置中心」进入后切「Agent 扩展」页签。
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='设置中心', exact=True).click()
    page.get_by_role('tab', name='Agent 扩展', exact=True).click()
    page.get_by_role('tab', name='MCP 工具', exact=False).first.click()
    page.get_by_role('button', name='添加 MCP 连接', exact=True).click()
    page.get_by_text('已有 MCP 配置？粘贴后自动填表', exact=True).click()
    page.get_by_label('MCP 配置 JSON', exact=True).fill('{"broken"')
    page.get_by_role('button', name='读取配置', exact=True).click()
    expect(page.get_by_role('alert')).to_be_visible()
    page.get_by_label('MCP 配置 JSON', exact=True).fill('')
    config = {'mcpServers': {
        '远程示例': {'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer fictional-token'}},
        '表单验收工具': {'command': VENV_PYTHON, 'args': [str(Path(__file__).with_name('fixture_mcp.py'))], 'env': {'TEST_ONLY': 'fictional-value'}},
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
    expect(page.locator('.toast-viewport')).to_contain_text('连接已保存')
    detail = page.locator('.ext-detail')
    detail.get_by_role('button', name='测试并发现工具', exact=True).click()
    expect(page.locator('.toast-viewport')).to_contain_text('测试成功', timeout=45000)
    detail.get_by_role('button', name='编辑', exact=True).click()
    expect(page.get_by_label('可执行命令', exact=True)).to_have_value(VENV_PYTHON)
    expect(page.get_by_label('启动参数 1', exact=True)).to_have_value(str(Path(__file__).with_name('fixture_mcp.py')))
    expect(page.get_by_label('环境变量值 1', exact=True)).not_to_be_visible()
    page.get_by_role('button', name='保存连接', exact=True).click()
    expect(page.locator('.toast-viewport')).to_contain_text('连接已保存')
    item = next(s for s in page.request.get(BASE + '/api/agent/extensions').json()['servers'] if s['name'] == '表单验收工具')
    assert item['env_keys'] == ['TEST_ONLY']
    page.request.delete(BASE + '/api/agent/mcp/' + item['id'])
    assert not errors, errors
    print(json.dumps(dict(mcp_form_ui='passed', paste_multi_config='passed', invalid_config_feedback='passed', editable_rows='passed', secret_retention='passed', real_stdio='passed', mobile_overflow=False, page_errors=errors)))
    browser.close()
