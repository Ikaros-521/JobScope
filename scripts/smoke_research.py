"""Observable agent acceptance; all mutations confined to the 8766 test service."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:8766'
OUT = Path(__file__).resolve().parents[1] / 'output' / 'research-ui'
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for title, text in [('RAG工程师', '熟悉RAG和Python，负责知识库研发'), ('Agent工程师', '熟悉Agent工具编排，负责执行状态管理')]:
        response = page.request.post(BASE + '/api/import/job', data=dict(title=title, company='研究验收公司', description=text, salary='26-35K·最高16薪', location='杭州'))
        assert response.ok
    page.goto(BASE)
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
    page.get_by_label('围绕全项目资料提问').fill('研究验收公司杭州RAG或Agent岗位工资超过25K有多少')
    page.get_by_role('button', name='发送全局问题').click()
    panel = page.locator('.global-chat-panel')
    expect(panel.locator('.chat-message.assistant')).to_have_count(1)
    expect(panel.locator('.salary-metrics')).to_contain_text('符合条件的岗位')
    expect(panel.locator('.salary-metrics strong').first).to_have_text('2')
    panel.get_by_text('查看检索过程', exact=True).click()
    expect(panel.locator('.trace-body')).to_contain_text('查询全部符合条件的岗位')
    expect(panel.locator('.trace-body code')).to_contain_text('R-')
    page.screenshot(path=str(OUT / '01-global-salary.png'), full_page=True)
    page.get_by_label('围绕全项目资料提问').fill('年薪超过50万呢')
    page.get_by_role('button', name='发送全局问题').click()
    expect(panel.locator('.chat-message.assistant')).to_have_count(2)
    expect(panel.locator('.chat-message.assistant').last).to_contain_text('招聘税前月薪')
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='数据与采集', exact=True).click()
    expect(page.get_by_role('heading', name='研究检索引擎')).to_be_visible()
    expect(page.get_by_role('button', name='构建语义向量')).to_be_disabled()
    page.get_by_role('button', name='同步索引', exact=True).click()
    expect(page.get_by_text('检索索引已更新', exact=True)).to_be_visible()
    page.screenshot(path=str(OUT / '02-index.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='全局对话', exact=True).click()
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(OUT / '03-mobile.png'), full_page=True)
    assert not errors, errors
    print(json.dumps(dict(research_ui='passed', salary_or_filter=2, trace='passed', annual_clarification='passed', index_controls='passed', mobile='passed', console_errors=errors)))
    browser.close()
