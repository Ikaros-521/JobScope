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
