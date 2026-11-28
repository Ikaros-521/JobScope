"""岗位研究视图冒烟：受控 API 响应覆盖前端行为，守卫 JobResearch 深 hook 抽取。

Run: .venv\\Scripts\\python.exe scripts/smoke_jobs_view.py --url http://127.0.0.1:8766
Requires Python playwright and Chrome; serve an isolated current-source frontend.
Route-intercept 模式（同 smoke_favorites_selection）：受控端点用虚构数据应答，
其余放行到 8766 隔离后端（空库返回形状正确的空数据）；变更（PATCH/POST/DELETE）
记入 mutations 供断言——深 hook 抽取（useJobList/useJobDetail/useAnalyzeRun）
动到的正是这条状态线程。
"""
import argparse
import json
import re
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, expect

ALL_JOBS = [
    dict(id="J-1", title="验收岗位一", company="甲公司", location="杭州",
         salary="20-30K", url="", source="boss", description_status="complete", updated_at="2026-11-28T10:00:00+00:00",
         description="负责高并发服务", evidence_ids=[], tags=["Python"],
         starred=False),
    dict(id="J-2", title="验收岗位二", company="乙公司", location="上海",
         salary="25-35K", url="", source="boss", description_status="complete", updated_at="2026-11-28T10:00:00+00:00",
         description="负责数据平台", evidence_ids=[], tags=["SQL"],
         starred=True),
] + [
    dict(id=f"J-fill-{i}", title=f"填充岗位{i}", company="填充公司", location="杭州",
         salary="15K", url="", source="boss", description_status="complete", updated_at="2026-11-28T09:00:00+00:00",
         description="填充条目用于翻页", evidence_ids=[], tags=[],
         starred=False)
    for i in range(1, 19)
] + [
    dict(id="J-3", title="验收岗位三", company="丙公司", location="北京",
         salary="30-40K", url="", source="manual", description_status="complete", updated_at="2026-11-27T10:00:00+00:00",
         description="负责机器学习平台", evidence_ids=[], tags=[],
         starred=False),
]
# 有状态 fixture：PATCH 就地改写岗位数据，列表/详情重载带回新值——
# 点星后的列表重载会把乐观翻转的结果用服务端数据重画，静态 fixture 会把
# aria-pressed 压回 false（探针靠时序侥幸通过、冒烟必挂）。
IMPORT_RESULT = {
    "job": dict(id="J-new", title="导入验收岗位", company="丁公司", location="深圳",
                salary="25K", url="https://example.com/1", source="user_import",
                description_status="complete",
                updated_at="2026-11-28T12:00:00+00:00",
                description="导入的岗位描述", evidence_ids=[], tags=[]),
}
EVIDENCE = [dict(id="E-1", title="甲公司资料", company="甲公司", kind="user_material",
                 text="验收证据正文", url="", published_at="", collected_at="2026-11-28",
                 text_truncated=False, source="manual", description_status="complete", source_url="")]


def job_detail(job_id):
    job = next(j for j in ALL_JOBS if j["id"] == job_id)
    return {
        "job": dict(job, starred_at="", pipeline_status="",
                    pipeline_status_changed_at="", applied_at="", applied_source=""),
        "evidence": EVIDENCE if job_id == "J-1" else [],
        "company_evidence": [],
        "evidence_total": 1 if job_id == "J-1" else 0,
    }
IMPORT_RESULT = {
    "job": dict(id="J-new", title="导入验收岗位", company="丁公司", location="深圳",
                salary="25K", url="https://example.com/1", source="user_import",
                description_status="complete",
                updated_at="2026-11-28T12:00:00+00:00",
                description="导入的岗位描述", evidence_ids=[], tags=[]),
}

mutations = []
analyze_job = ["J-1"]  # 最近一次评估的岗位 id，done 载荷据此回填 job_id


def intercept(route):
    parsed = urlparse(route.request.url)
    path, method = parsed.path, route.request.method
    query = parse_qs(parsed.query)

    if path == "/api/dashboard" and method == "GET":
        # 今日首屏不在本冒烟范围：给最小合法载荷防 ErrorBoundary 崩整页。
        return route.fulfill(json={
            "counts": {"jobs": 0, "favorites": 0, "tracked": 0,
                       "companies": 0, "evidence": 0},
            "pipeline": {"follow_ups": [], "stale_days": 14, "totals": {}},
            "due": [], "weekly": None, "briefing": None, "prescriptions": None,
            "new_jobs": [], "backup": {"days_since": None, "last_backup_at": None},
            "recent_tasks": [], "prep_plan": None,
        })
    if path == "/api/jobs" and method == "GET":
        page_no = int(query.get("page", ["1"])[0])
        q = (query.get("q") or [""])[0]
        pool = ALL_JOBS  # 全库 21 条：页数按总数算，不是按当前页条数
        if q:
            pool = [job for job in pool
                    if q in job["title"] or q in job["description"]]
        current = pool[:20] if page_no == 1 else pool[20:]
        mutations.append(("list", dict(q=q, page=page_no)))
        pages = max(1, -(-len(pool) // 20))
        return route.fulfill(json={
            "jobs": current, "page": page_no, "page_size": 20,
            "pages": pages, "total": len(pool),
        })
    if path == "/api/jobs" and method == "PATCH":
        body = route.request.post_data_json
        mutations.append(("patch", body))
        # 就地落到 fixture：之后的列表/详情重载必须读得到这次变更。
        for job in ALL_JOBS:
            if job["id"] in body["ids"]:
                for key in ("starred", "tags", "pipeline_note"):
                    if key in body:
                        job[key] = body[key]
        return route.fulfill(json={"updated": len(body["ids"])})
    if path == "/api/jobs/batch-delete" and method == "POST":
        body = route.request.post_data_json
        mutations.append(("batch-delete", body["ids"]))
        # 不从 ALL_JOBS 摘除：后续 ⌘K 流程还要检索 J-1，前端删除提示只看响应数。
        return route.fulfill(json={"deleted": len(body["ids"])})
    if path == "/api/import/job" and method == "POST":
        body = route.request.post_data_json
        mutations.append(("import-job", body))
        ALL_JOBS.insert(0, dict(IMPORT_RESULT["job"]))
        return route.fulfill(json=IMPORT_RESULT)
    if re.fullmatch(r"/api/jobs/J-1", path) and method == "GET":
        return route.fulfill(json=job_detail("J-1"))
    if re.fullmatch(r"/api/jobs/J-1/evidence", path) and method == "GET":
        return route.fulfill(json={
            "evidence": EVIDENCE, "page": 1, "page_size": 10,
            "pages": 1, "total": 1,
        })
    if re.fullmatch(r"/api/jobs/J-1/reports", path) and method == "GET":
        return route.fulfill(json={"reports": [], "latest": None})
    if re.fullmatch(r"/api/jobs/J-1/resume-versions", path):
        return route.fulfill(json={"versions": []})
    if re.fullmatch(r"/api/jobs/J-new", path) and method == "GET":
        return route.fulfill(json=job_detail("J-new"))
    if re.fullmatch(r"/api/jobs/J-new/reports", path):
        return route.fulfill(json={"reports": [], "latest": None})
    if re.fullmatch(r"/api/jobs/J-new/resume-versions", path):
        return route.fulfill(json={"versions": []})
    if re.fullmatch(r"/api/jobs/J-new/evidence", path):
        return route.fulfill(json={"evidence": [], "page": 1, "page_size": 10,
                                   "pages": 0, "total": 0})
    if path == "/api/analyze/runs" and method == "POST":
        body = route.request.post_data_json
        mutations.append(("analyze-run", body))
        analyze_job[0] = body.get("job_id", "J-1")
        return route.fulfill(json={"run_id": "R-smoke", "reused": False})
    if "/stream" in path and "analyze" in path:
        # done 载荷必须是完整 Analysis 形状：渲染器直接读 strengths/gaps/
        # questions 等 .length，缺字段会崩整页（前端契约如此，不是冒烟宽松点）。
        job_id = analyze_job[0]
        events = [
            ("start", dict(run_id="R-smoke")),
            ("stage", dict(stage="extract", status="done", total=3)),
            ("stage", dict(stage="match", status="done", total=3)),
            ("stage", dict(stage="score", status="done")),
            ("group", dict(items=[
                dict(requirement="熟悉 Python", status="met", quote="负责高并发服务",
                     evidence_ids=["E-1"], confidence=1.0),
            ])),
            ("done", dict(
                job_id=job_id, resume_revision=1, score=82, coverage=1.0,
                method="rule",
                requirements=[dict(requirement="熟悉 Python", status="met",
                                   evidence=[], dimension="技能",
                                   jd_quote="负责高并发服务", rationale="",
                                   hard=False)],
                strengths=["经验对口"], gaps=[], questions=[],
                salary=None, company_summary=None, evidence_ids=["E-1"],
                limitations="",
            )),
        ]
        body = "".join(
            f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            for kind, payload in events
        ) + 'event: end\ndata: {"ok": true}\n\n'
        return route.fulfill(status=200, content_type="text/event-stream", body=body)
    # 其余请求（元数据/角标/公司/知识库等）放行到 8766 隔离后端——空库返回
    # 形状正确的真实空数据，避免手造 {} 破坏 App 级响应契约。
    route.continue_()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    assert urlparse(args.url).port == 8766, "Only isolated 8766 is allowed"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050},
                                reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", intercept)
        page.goto(args.url + "/#/jobs")  # 深链直达岗位研究（今日首屏不在本冒烟范围）
        # 首访教程遮罩：出现即 Esc 跳过。
        try:
            page.get_by_role("dialog", name="使用教程").wait_for(
                state="attached", timeout=15000)
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog", name="使用教程")).to_have_count(0)
        except Exception:
            pass
        toggle = page.get_by_role("button", name="求职工作台", exact=True).first
        toggle.wait_for(state="attached", timeout=10000)
        if toggle.get_attribute("aria-expanded") == "false":
            toggle.click()

        # ── 1) 列表加载与分页 ──
        expect(page.locator(".job-card")).to_have_count(20)
        expect(page.locator(".job-card").first).to_contain_text("验收岗位一")
        page.get_by_role("button", name="岗位档案下一页", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(1)
        expect(page.locator(".job-card").first).to_contain_text("验收岗位三")
        page.get_by_role("button", name="岗位档案上一页", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(20)

        # ── 2) 档案关键词检索：回车提交 → 请求带 q，结果过滤 ──
        #（「岗位关键词」是查找外部岗位折叠面板里的采集输入，默认隐藏——别选错。）
        search_input = page.get_by_placeholder("在已存档案中搜索")
        search_input.fill("数据平台")
        search_input.press("Enter")
        expect(page.locator(".job-card")).to_have_count(1)
        expect(page.locator(".job-card").first).to_contain_text("验收岗位二")
        list_mutations = [m for m in mutations if m[0] == "list"]
        assert list_mutations[-1][1]["q"] == "数据平台"
        # 清空恢复全列表
        search_input.fill("")
        search_input.press("Enter")
        expect(page.locator(".job-card")).to_have_count(20)

        # ── 3) 选中岗位 → 详情加载与页签 ──
        page.locator(".job-card").first.click()
        expect(page.get_by_role("heading", name="验收岗位一", level=2)).to_be_visible()
        page.get_by_role("tab", name="证据资料", exact=False).click()
        expect(page.locator(".evidence-card").first).to_contain_text("验收证据正文")

        # ── 4) 星标切换：PATCH starred 到达，卡内星标按钮翻 aria-pressed ──
        #（星标按钮 title 翻转后变成「取消收藏」，按 name 断言会重新解析到
        # 下一张未收藏卡片——必须锚定第一张卡内部，别用全局 .first。）
        star = page.locator(".job-card").first.locator("button.job-card-star")
        star.click()
        expect(star).to_have_attribute("aria-pressed", "true")
        star_patches = [m for m in mutations if m[0] == "patch"
                        and m[1].get("starred") is True]
        assert star_patches and star_patches[-1][1]["ids"] == ["J-1"]

        # ── 5) 跟进弹窗：保存 → PATCH pipeline_note ──
        page.get_by_role("button", name=re.compile(r"^跟进")).first.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_label("下一步行动").fill("已发打招呼消息")
        page.get_by_role("button", name="保存", exact=True).click()
        follow_patches = [m for m in mutations if m[0] == "patch"
                          and m[1].get("pipeline_note") == "已发打招呼消息"]
        assert follow_patches, "跟进保存应 PATCH pipeline_note"
        expect(page.get_by_text("跟进信息已保存", exact=True)).to_be_visible()
        # 保存成功后编辑器自行 onClose，无需再点关闭。
        expect(page.get_by_role("dialog")).to_have_count(0)

        # ── 6) 批量编辑：勾选 → 选中工具条「编辑」→ 提交 PATCH tags ──
        page.locator(".job-card").first.locator("input[type=checkbox]").check()
        selection_bar = page.locator(".selection-actions")
        selection_bar.get_by_role("button", name="编辑", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_label("个人标签（逗号分隔，可清空；平台标签保留）").fill("已核实")
        page.get_by_role("button", name="保存修改", exact=True).click()
        tag_patches = [m for m in mutations if m[0] == "patch"
                       and m[1].get("tags") == ["已核实"]]
        assert tag_patches, "批量编辑应 PATCH tags"
        expect(page.get_by_role("dialog")).to_have_count(0)

        # ── 7) 确认删除弹窗：取消不动 → 确认到达 batch-delete ──
        page.locator(".job-card").first.locator("input[type=checkbox]").check()
        selection_bar.get_by_role("button", name="删除", exact=True).click()
        confirm_dialog = page.get_by_role("dialog")
        expect(confirm_dialog).to_be_visible()
        assert "此操作不可恢复" in confirm_dialog.inner_text()
        confirm_dialog.get_by_role("button", name="取消", exact=True).click()
        assert not [m for m in mutations if m[0] == "batch-delete"]
        selection_bar.get_by_role("button", name="删除", exact=True).click()
        page.get_by_role("dialog").get_by_role(
            "button", name="确认删除", exact=True).click()
        deletes = [m for m in mutations if m[0] == "batch-delete"]
        assert deletes and deletes[-1][1] == ["J-1"]
        expect(page.get_by_text("已删除 1 份岗位档案", exact=True)).to_be_visible()

        # ── 8) 导入岗位弹窗：填写 → POST /api/import/job → 详情换装 ──
        page.get_by_role("button", name="粘贴已有岗位 JD", exact=True).click()
        page.get_by_label("岗位名称").fill("导入验收岗位")
        page.get_by_label("公司名称").fill("丁公司")
        page.get_by_label("岗位描述", exact=False).fill("导入的岗位描述")
        page.get_by_role("button", name="保存资料", exact=True).click()
        imports = [m for m in mutations if m[0] == "import-job"]
        assert imports and imports[-1][1]["title"] == "导入验收岗位"
        expect(page.get_by_role("heading", name="导入验收岗位", level=2)).to_be_visible()

        # ── 9) 评估适配 run：POST runs + SSE 事件驱动进度面板 → 报告渲染 ──
        #（导入后详情已换装为 J-new，评估跑在当前选中岗位上。）
        page.get_by_role("button", name="评估适配", exact=True).click()
        analyze_runs = [m for m in mutations if m[0] == "analyze-run"]
        assert analyze_runs and analyze_runs[-1][1]["job_id"] == "J-new"
        expect(page.get_by_text("熟悉 Python", exact=False).first).to_be_visible()

        # ── 10) ⌘K 全局搜索：键盘唤起 → 岗位结果直达详情 ──
        page.keyboard.press("Control+k")
        expect(page.get_by_role("dialog", name="全局搜索")).to_be_visible()
        page.get_by_placeholder("搜索岗位、公司、知识库…").fill("验收岗位一")
        expect(page.get_by_role("dialog", name="全局搜索")
               .get_by_text("验收岗位一", exact=True)).to_be_visible()
        page.keyboard.press("Enter")
        expect(page.get_by_role("dialog", name="全局搜索")).to_have_count(0)
        expect(page.get_by_role("heading", name="验收岗位一", level=2)).to_be_visible()

        assert not errors, f"页面脚本异常：{errors}"
        browser.close()
        print("smoke_jobs_view: 全部通过")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("== mutations ==")
        print(json.dumps(mutations, ensure_ascii=False, indent=1)[:4000])
        raise
