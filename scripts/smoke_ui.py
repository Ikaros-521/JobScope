"""UI acceptance using fictional material in a separate test database."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / 'output'
    output.mkdir(exist_ok=True)
    (output / 'unslop').mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1050}, device_scale_factor=1, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url)
        # 新档案首次进入会自动弹教程遮罩（拦截点击，加载完成后才弹）：等它出现按 Esc 跳过。
        try:
            page.get_by_role('dialog', name='使用教程').wait_for(state='attached', timeout=15000)
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog', name='使用教程')).to_have_count(0, timeout=5000)
        except Exception:
            pass
        # 教程关闭后导航组恢复折叠：先展开「岗位研究」组再点条目（条目带角标时名字含数字，不能用 exact）。
        toggle = page.get_by_role('button', name='求职工作台', exact=True).first
        if toggle.get_attribute('aria-expanded') == 'false':
            toggle.click()
        page.locator('#nav-group-research').get_by_role('button', name='岗位研究').first.click()
        expect(page.get_by_role('heading', name='岗位研究', exact=True)).to_be_visible()

        def mobile_nav(view: str) -> None:
            # 窄屏导航抽屉默认收起：先展开再点组内条目（条目名可能带角标数字，禁用 exact）。
            toggle = page.locator('.mobile-nav-toggle')
            if toggle.is_visible() and toggle.get_attribute('aria-expanded') == 'false':
                toggle.click()
            page.get_by_role('navigation').locator('.nav-group-items').get_by_role('button', name=view, exact=False).first.click()
        page.screenshot(path=str(output / 'workbench-empty.png'), full_page=True)

        page.get_by_role('button', name='粘贴已有岗位 JD', exact=True).click()
        expect(page.get_by_label('岗位名称')).to_be_focused()
        page.get_by_role('dialog').get_by_role('button', name='保存资料', exact=True).focus()
        page.keyboard.press('Tab')
        expect(page.get_by_role('button', name='关闭弹窗')).to_be_focused()
        page.keyboard.press('Shift+Tab')
        expect(page.get_by_role('dialog').get_by_role('button', name='保存资料', exact=True)).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog')).to_have_count(0)
        expect(page.get_by_role('button', name='粘贴已有岗位 JD', exact=True)).to_be_focused()
        page.get_by_role('button', name='粘贴已有岗位 JD', exact=True).click()
        page.get_by_label('岗位名称').fill('验收专用 Python 后端工程师')
        page.get_by_label('公司名称').fill('虚构验收公司')
        page.get_by_label('工作地点', exact=True).last.fill('北京')
        page.get_by_label('原始薪资描述').fill('20-30K·最高16薪')
        page.get_by_label('岗位描述', exact=False).fill('熟悉 Python、SQL、Redis\n必须本科，3年以上后端开发经验。')
        page.get_by_role('button', name='保存资料', exact=True).click()
        expect(page.get_by_role('heading', name='验收专用 Python 后端工程师', level=2)).to_be_visible()

        page.get_by_role('button', name='我的简历', exact=False).click()
        page.get_by_label('简历正文', exact=True).fill('【虚构验收材料】使用 Python 和 SQL 开发数据服务。')
        page.get_by_role('button', name='保存到本地', exact=True).click()
        expect(page.get_by_text('简历已保存在本地。岗位适配分析将使用这个版本。')).to_be_visible()
        page.locator('#nav-group-research').get_by_role('button', name='岗位研究').first.click()
        page.get_by_role('button', name='评估适配', exact=True).click()
        expect(page.get_by_text('词项核实覆盖', exact=False)).to_be_visible()
        # 薪酬口径在「薪酬与公司」子页签（2026-11 子页签改版后不再与映射表同屏）。
        page.get_by_role('tab', name='薪酬与公司', exact=True).click()
        expect(page.get_by_text('保证薪资月数', exact=True)).to_be_visible()
        page.get_by_role('tab', name='要求映射', exact=False).first.click()
        expect(page.get_by_role('columnheader', name='简历依据', exact=True)).to_be_visible()
        expect(page.get_by_text('暂不生成总分', exact=True)).to_be_visible()

        # 岗位对话按焦点挂载（工作台弹性链改版后不再常驻 DOM）：先进对话面板。
        # 未配置模型时发送键禁用（composer 门禁；配置后的发送链路由 smoke_chat_recovery 覆盖）。
        page.get_by_role('button', name='问这份岗位', exact=True).click()
        page.locator('.job-chat .chat-composer textarea').fill('这个岗位需要什么技能？')
        expect(page.locator('.job-chat').get_by_role('button', name='发送消息', exact=True)).to_be_disabled()
        page.get_by_role('button', name='返回岗位', exact=True).click()
        page.evaluate('scrollTo(0, 0)')
        page.screenshot(path=str(output / 'workbench-analysis.png'), full_page=True)

        page.get_by_role('tab', name='岗位与公司', exact=True).click()
        page.get_by_role('button', name='补充公司资料', exact=True).click()
        page.get_by_label('资料标题').fill('虚构验收资料')
        page.get_by_label('资料原文').fill('这是一份虚构验收材料。薪酬和团队情况尚待核实。')
        page.get_by_role('button', name='保存资料', exact=True).click()
        expect(page.get_by_text('资料已保存。用户提供的资料仍需结合来源核实。')).to_be_visible()
        page.get_by_role('button', name='公司资料库', exact=True).click()
        expect(page.get_by_text('虚构验收资料', exact=True).first).to_be_visible()
        page.get_by_label('公司主体（精确匹配）').fill('虚构验收公司')
        page.get_by_role('button', name='查询资料', exact=True).click()
        expect(page.get_by_text('虚构验收资料', exact=True).first).to_be_visible()
        page.get_by_role('tab', name='公司对话', exact=False).click()
        page.locator('.library-chat-panel .chat-composer textarea').fill('公司的薪酬和团队情况？')
        expect(page.locator('.library-chat-panel').get_by_role('button', name='发送消息', exact=True)).to_be_disabled()

        page.get_by_role('button', name='数据与采集', exact=True).click()
        expect(page.get_by_role('heading', name='数据与采集', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='打开专用浏览器')).to_be_visible()
        page.screenshot(path=str(output / 'sources.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        mobile_nav('岗位研究')
        page.evaluate('scrollTo(0, 0)')
        page.screenshot(path=str(output / 'mobile.png'), full_page=True)
        overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow, '窄屏存在横向溢出'
        checks = []
        for view in ['岗位研究', '我的简历', '公司资料库', '全局对话', '数据与采集']:
            mobile_nav(view)
            expect(page.get_by_role('heading', name=view, exact=True).first).to_be_visible()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'{view}窄屏存在横向溢出'
            undersized = page.evaluate('''() => [...document.querySelectorAll('body *')].filter(el => {
                const style = getComputedStyle(el);
                return el.getClientRects().length && style.visibility !== 'hidden' &&
                    [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) &&
                    parseFloat(style.fontSize) < 12 && !el.closest('.sr-only');
            }).map(el => el.textContent.slice(0, 60))''')
            assert not undersized, f'{view}存在低于12px文字：{undersized}'
            checks.append(view)
        mobile_nav('岗位研究')
        page.get_by_role('tab', name='适配分析', exact=True).click()
        expect(page.get_by_role('columnheader', name='简历依据', exact=True)).to_be_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), '分析表格窄屏存在横向溢出'
        page.screenshot(path=str(output / 'unslop' / 'mobile-analysis.png'), full_page=True)

        page.route('**/api/collect', lambda route: route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '验收：数据源暂时不可用'})))
        page.get_by_role('button', name='查找外部岗位', exact=True).click()
        page.get_by_label('岗位关键词', exact=True).fill('Python')
        page.get_by_role('button', name='检索岗位', exact=False).click()
        expect(page.get_by_role('alert')).to_contain_text('验收：数据源暂时不可用')
        expect(page.get_by_role('button', name='检索岗位', exact=False)).to_be_enabled()
        page.unroute('**/api/collect')
        assert not errors, errors
        print(json.dumps({'ui_smoke': 'passed', 'console_errors': errors, 'mobile_overflow': overflow, 'mobile_font_checks': checks, 'mobile_analysis_table': 'passed', 'modal_keyboard': 'passed', 'collection_error_recovery': 'passed', 'screenshots': str(output)}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
