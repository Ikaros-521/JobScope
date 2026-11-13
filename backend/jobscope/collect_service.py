"""采集管线编排：页面端点与 Agent 内置工具共用同一条通路。

main.py 的 /api/collect、/api/company/lookup、/api/company-research、/api/browser/collect
与 general_agent 的 company_web_lookup / collect_jobs 工具都委托到这里，采集路由、落库
口径、任务状态机只有这一份事实。模块按名懒加载（importlib 每次调用时查 sys.modules，
测试替身依旧有效）；这里不感知 HTTP，缺依赖抛 ModuleNotFoundError 由调用方翻译。
"""
import importlib


def _module(name):
    return importlib.import_module("." + name, __package__)


def execute_lookup(repo, data_dir, channel, company, task=None):
    """企查查/天眼查公开页快查：卡片按解析出的精确主体名归档成证据（identity 去重，重复查询幂等）。

    返回 (outcome, saved)：outcome 是 company_lookup.collect 的原始结果（状态与卡片），
    saved 是落库后的证据 payload，调用方按会话范围决定哪些可引用。
    """
    if task is None:
        task = repo.create_task(channel, company)
    outcome = _module('company_lookup').collect(channel, company, browser_module=_module('browser'), data_dir=data_dir)
    saved = []
    for card in outcome.get('cards', []):
        saved.append(repo.save_evidence(dict(title=card['name'], company=card['name'], kind='web_company_profile',
                                             text=card['text'], url=card.get('url', '')), channel))
    task.update(status=outcome.get('status', 'failed'), message=outcome.get('message', '快查未完成'),
                count=len(saved))
    repo.update_task(task)
    return outcome, saved


def persist_collection(repo, task, result):
    jobs = [repo.save_job(item, task["source"]) for item in result.get("jobs", [])]
    evidence_count = 0
    for item in result.get("evidence", []):
        evidence = repo.save_evidence(item, task["source"])
        if item.get("kind") != "job_posting":
            evidence_count += 1
        for job in jobs:
            if item.get("url") and item.get("url") == job["url"] and item.get("company") == job["company"]:
                repo.link_evidence(job["id"], evidence["id"])
    task.update(status=result.get("status", "failed"), message=result.get("message", "采集完成"), count=len(jobs) + evidence_count)
    repo.update_task(task)
    return {"task": task, "jobs": [repo.job(job["id"]) for job in jobs]}


def execute_collection(repo, data_dir, source, query, task, location='', limit=10, use_browser=False):
    connectors = _module("connectors")
    if use_browser or source in connectors.BROWSER_SOURCES:
        result = _module("browser").collect(source, query, location=location, limit=limit, data_dir=data_dir)
    else:
        result = connectors.collect(source, query, location=location, limit=limit, cache_dir=data_dir / "cache")
    return persist_collection(repo, task, result)


def run_collection(repo, data_dir, source, query, location, limit, use_browser=False):
    task = repo.create_task(source, query)
    try:
        return execute_collection(repo, data_dir, source, query, task, location, limit, use_browser)
    except Exception:
        task.update(status="failed", message="采集未完成，请检查来源状态及本地依赖后重试")
        repo.update_task(task)
        return {"task": task, "jobs": []}


# 一键补数据收尾步的每家详情上限：jd 具体度闸门要 3 条完整 JD，留足余量又控制时长（每条 2s 间隔）。
DETAIL_BATCH_LIMIT = 10


def company_pending_details(repo, company):
    """该公司实体（名+别名）名下、支持详情读取（boss/智联）且尚未完整的岗位。

    company-research 的岗位渠道只存列表摘要；jd_specificity 与薪资分层要的完整 JD 得逐条读
    详情页。实体口径与 reliability/local.py 一致：别名并集规范化精确比对，绝不模糊匹配。"""
    details = _module('job_details')
    entity = repo.resolve_company(company)
    raw = [entity['name'], *(entity.get('aliases') or [])]
    names = {repo.normalise_name(item) for item in raw if str(item or '').strip()}
    return [job for job in repo.jobs()
            if repo.normalise_name(job.get('company', '')) in names
            and job.get('source', '') in details.DETAIL_SOURCES
            and job.get('description_status') != 'complete'][:DETAIL_BATCH_LIMIT]


def run_company_details(repo, company, task, ids):
    """company-research 的收尾步：渠道串行跑完后，把该公司待补岗位逐条读成完整 JD。

    与岗位档案的批量详情共用 batch_lock：锁被占说明另一路详情在跑，本家跳过不阻塞，
    渠道结果不受影响（漏掉的完整 JD 可在岗位档案里单独补）。"""
    details = _module('job_details')
    if not details.batch_lock.acquire(blocking=False):
        task.update(status='failed', message='完整JD批量补采正在运行，本家详情补读已跳过；可在岗位档案里单独补采')
        repo.update_task(task)
        return
    try:
        details.run_batch(repo, task, limit=len(ids), ids=ids,
                          message=f'正在补读「{company}」完整JD，逐条保存，可在中断后继续')
    finally:
        details.batch_lock.release()
