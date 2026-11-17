"""求职参考与采集编排的路由层（2026-11-17 自 main.py 拆出的第一刀）。

这一簇共用同一批依赖（repo、data_dir、collect_service 编排）与同一批产品概念：
一键完备（company-research）、求职参考报告、信号采集/抽取、横评对照、工资基线、来源健康。
main.py 只保留设置、岗位档案、导入、对话等其余端点，并在装配时调用本模块的 register()。

拆分纪律：
- 端点行为、路径、状态码与拆分前完全一致；865 个既有测试就是行为快照，不许漂移。
- reliability 子模块保持函数级 import（包 __init__ 零导入），浏览器与模型依赖不进无关请求路径。
- 这里不做任何 HTTP 之外的新逻辑；要改管线语义去 collect_service / reliability 层。
"""
import logging
import threading

from fastapi import HTTPException, Query
from fastapi import Path as PathParam

from . import collect_service
from .models import (CompanyResearchInput, ReliabilityInput, SignalCollectInput, SignalExtractInput,
                     WageBaselineImportInput)
from .repository import Repository, now

logger = logging.getLogger(__name__)


def register(app, repo, data_dir):
    """把本簇端点挂到 app 上；依赖经 create_app 闭包传入，与 main.py 原装配一致。"""

    def execute_lookup(source, company, task):
        """企查查/天眼查公开页快查：管线在 collect_service（与 Agent 的 company_web_lookup 工具共用）。"""
        outcome, _ = collect_service.execute_lookup(repo, data_dir, source, company, task)
        return outcome

    def execute_collection(source, query, task, location='', limit=10):
        return collect_service.execute_collection(repo, data_dir, source, query, task, location, limit)

    @app.post("/api/company-research")
    def company_research(data: CompanyResearchInput):
        from .reliability.collection import SourceUnavailable, collect_source, validate_source
        from .reliability.sources import SIGNAL_SOURCES, SignalSourceStore
        company = data.company.strip()
        store = SignalSourceStore(repo)
        # Freeze source IDs before creating any task/thread. Paid sources require explicit selection;
        # unverified GSXT remains a manual option rather than silently running on every research.
        selected = data.signal_sources
        if selected is None:
            # manual_only 来源（AI 行业判断，消耗模型额度）与付费源一样不进默认选择。
            selected = [source for source, spec in SIGNAL_SOURCES.items()
                        if spec['tier'] == 'free' and spec['implemented'] and spec['status'] == 'available'
                        and not spec.get('manual_only') and store.ready(source)]
        if len(set(selected)) != len(selected):
            raise HTTPException(400, '信号来源不能重复')
        try:
            for source in selected:
                validate_source(store, source)
        except SourceUnavailable as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        # 一键完备（2026-11-17）：默认研究自动追加 AI 行业判断（manual_only 的额度成本由用户
        # 显式关闭 with_industry_llm 或在设置里禁用来源来豁免）。显式 signal_sources 是用户的
        # 指名选择（含空列表），完全尊重、绝不追加。只在来源当前可用时排队；执行时
        # collect_source 会再校验一次，禁用/未配置模型的任务收尾为 skipped，不阻塞全链。
        if data.signal_sources is None and data.with_industry_llm and store.ready('llm_industry'):
            selected = (*selected, 'llm_industry')
        selected = tuple(sorted(selected, key=lambda source: SIGNAL_SOURCES[source]['transport'] == 'local'))
        # Keep existing detail supplementation before signals and local statistics.
        detail_ids = [job['id'] for job in collect_service.company_pending_details(repo, company)]
        plan = ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin']
        channel_tasks = [repo.create_task(source, company) for source in plan]
        tasks = list(channel_tasks)
        detail_task = repo.create_task('job_details', company) if detail_ids else None
        if detail_task is not None:
            tasks.append(detail_task)
        signal_tasks = [(source, repo.create_task('signal_' + source, company)) for source in selected]
        tasks.extend(task for _, task in signal_tasks)
        # 材料抽取排在信号采集之后、重算之前：本轮快查卡片与公告要喂给抽取器出 team_size 等
        # 信号，抽取完再重算，报告一次到位（单岗完整路径不再要用户手动点「提取资料数字」）。
        extract_task = repo.create_task('reliability_extract', company) if data.with_extract else None
        if extract_task is not None:
            tasks.append(extract_task)
        compute_task = repo.create_task('reliability_compute', company)
        tasks.append(compute_task)

        def fail(task, message):
            task.update(status='failed', message=message, updated_at=now())
            repo.update_task(task)

        def worker():
            logger.info('一键调研开始：「%s」渠道=%s 信号源=%s 待补JD=%s',
                        company, ','.join(plan), ','.join(selected) or '无', len(detail_ids) if detail_task else 0)
            try:
                for source, task in zip(plan, channel_tasks):
                    try:
                        if source in ('qcc', 'tyc'):
                            execute_lookup(source, company, task)
                        else:
                            execute_collection(source, company, task, limit=data.limit)
                    except Exception:
                        logger.exception('渠道检索失败：%s「%s」', source, company)
                        fail(task, '该渠道检索失败，已跳过；不影响其他渠道')
                if detail_task is not None:
                    try:
                        collect_service.run_company_details(repo, company, detail_task, detail_ids)
                    except Exception:
                        logger.exception('完整JD补读失败：「%s」', company)
                        fail(detail_task, '完整JD补读失败，可在岗位档案里单独重试')
                for source, task in signal_tasks:
                    try:
                        # collect_source rechecks current settings and reuses this queued task.
                        collect_source(repo, data_dir, company, source, task=task)
                    except Exception:
                        logger.exception('信号来源采集失败：%s「%s」', source, company)
                        fail(task, '该信号来源采集失败，已跳过；不影响其他来源')
                if extract_task is not None:
                    try:
                        from .reliability.service import run_signal_extract
                        # run_signal_extract reuses this queued task; skipped（无可抽材料）属正常终态。
                        run_signal_extract(repo, company, task=extract_task)
                    except Exception:
                        logger.exception('材料抽取失败：「%s」', company)
                        fail(extract_task, '材料抽取失败，可单独重试；不影响已采集的信号')
                try:
                    from .reliability.service import ReliabilityService
                    ReliabilityService(repo).recompute(company, refresh_local=False)
                    compute_task.update(status='completed', message='已按当前保存的材料重算求职参考报告',
                                        count=1, updated_at=now())
                    repo.update_task(compute_task)
                except Exception:
                    logger.exception('求职参考报告重算失败：「%s」', company)
                    fail(compute_task, '求职参考报告重算失败，可单独重新计算')
                logger.info('一键调研完成：「%s」', company)
            finally:
                # Even a helper returning early must not leave a permanently running task.
                for task in tasks:
                    if task['status'] in ('running', 'pending'):
                        fail(task, '该步骤未完成，可单独重试')

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            for task in tasks:
                fail(task, '后台任务未能启动，请重试')
        return {"tasks": tasks}

    # 来源健康度（2026-11-17）：任务状态分三桶，口径与页面 Badge 一致——completed/success 算成功；
    # needs_login / needs_verification / backoff / partial / skipped / degraded / unsupported 是
    # 「未完成」（要用户动手或来源整体不可用）；其余（failed）算失败。消息原样引用任务记录，
    # 这里不产出任何新的诊断结论，也不猜失败原因。
    HEALTH_OK = {'completed', 'success'}
    HEALTH_ATTENTION = {'needs_login', 'needs_verification', 'backoff', 'partial', 'skipped', 'degraded', 'unsupported'}

    @app.get('/api/sources/health')
    def sources_health():
        """按最近 100 次任务聚合各来源的成功/未完成/失败分布与最近结果。

        没有任务记录的来源不出现（没跑过就不装作健康或异常）；stats 按「最近一次任务时间」倒序，
        最近成功时间取窗口内最后一次成功任务。"""
        stats: dict[str, dict] = {}
        for task in reversed(repo.tasks()):
            item = stats.setdefault(task['source'], dict(source=task['source'], total=0, ok=0, attention=0,
                                                         failed=0, last_run_at='', last_ok_at='',
                                                         last_status='', last_message=''))
            item['total'] += 1
            item['last_run_at'] = task.get('created_at', '')
            item['last_status'] = task.get('status', '')
            item['last_message'] = task.get('message', '')
            status = task.get('status', '')
            if status in HEALTH_OK:
                item['ok'] += 1
                item['last_ok_at'] = task.get('created_at', '')
            elif status in HEALTH_ATTENTION:
                item['attention'] += 1
            else:
                item['failed'] += 1
        result = sorted(stats.values(), key=lambda item: item['last_run_at'], reverse=True)
        return {'window': '最近 100 次任务', 'sources': result}

    # 求职参考分析。路径挂在 /api/companies/{company}/... 与 /api/reliability/... 之下，
    # 与 /api/companies 列表端点按完整路径匹配，不受注册顺序影响。
    # 参数化路径一律带长度上限，percent-decode 由 Starlette 完成，取值先过实体归一的规范化再使用：
    # 写路径（POST 重算、POST 采集）用 resolve_company 确保实体存在，读路径（GET）用 find_company
    # 同口径查找、查不到就是没有，绝不因为一次拼错的查询留下 companies 行；
    # 出站 URL 由 collectors 用 urlencode 拼接，这里不存在原样插值。
    # reliability 子模块全部走函数级 import：包的 __init__ 保持零导入，浏览器与模型依赖
    # 就不会被打进不需要它们的请求路径。

    @app.post('/api/companies/{company}/reliability')
    def recompute_reliability(company: str = PathParam(min_length=1, max_length=300), data: ReliabilityInput | None = None):
        from .reliability.service import ReliabilityService
        try:
            return ReliabilityService(repo).recompute(company, data.credit_code if data else '')
        except ValueError as exc:
            # reliability 层抛的 ValueError 都是写给用户看的中文说明（空公司名、措辞命中禁用词）。
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/companies/{company}/reliability')
    def read_reliability(company: str = PathParam(min_length=1, max_length=300), credit_code: str = Query('', max_length=40)):
        from .reliability.service import ReliabilityService
        try:
            # peek 而不是 latest：GET 是读操作，查一个拼错的名字不该写出 companies 行。
            return {'report': ReliabilityService(repo).peek(company, credit_code)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/companies/{company}/reliability/history')
    def company_reliability_history(company: str = PathParam(min_length=1, max_length=300), credit_code: str = Query('', max_length=40)):
        """单公司的历史快照与相邻差分。路径只收一个公司：趋势永远只对比这家公司自己的历史，
        没有任何跨公司对比、排名或榜单的入口（红线测试钉住这一点）。"""
        from .reliability.service import ReliabilityService
        try:
            return {'history': ReliabilityService(repo).history(company, credit_code)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/api/companies/{company}/signals/collect')
    def collect_company_signals(data: SignalCollectInput, company: str = PathParam(min_length=1, max_length=300)):
        from .reliability.collection import SourceUnavailable, collect_source
        try:
            return collect_source(repo, data_dir, company, data.source, data.credit_code)
        except SourceUnavailable as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/companies/{company}/signals/extract')
    def extract_company_signals(data: SignalExtractInput, company: str = PathParam(min_length=1, max_length=300)):
        """把已存材料喂给抽取器：手动导入的公告与材料不会自己变成信号。

        管线在 reliability.service.run_signal_extract（与 Agent 的 signal_extract 工具共用），
        这里只做 HTTP 包装；identity 是管线给工具层的内部信息，不进响应。"""
        from .reliability.service import run_signal_extract
        try:
            result = run_signal_extract(repo, company, data.credit_code, limit=data.limit, max_tokens=data.max_tokens)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result.pop('identity', None)
        return result

    @app.get('/api/reliability/sources')
    def reliability_sources():
        from .reliability.sources import SignalSourceStore
        return {'sources': SignalSourceStore(repo).public()}

    # ── 工资基线（2026-11-17）：人社局工资指导价位的手动导入，pay 维度的同城对照兜底 ──

    @app.get('/api/wage-baselines')
    def list_wage_baselines():
        return {'baselines': repo.wage_baselines()}

    @app.post('/api/wage-baselines/import')
    def import_wage_baselines(data: WageBaselineImportInput):
        from .reliability.wage_baseline import parse
        rows, errors = parse(data.csv)
        saved = repo.save_wage_baselines(rows) if rows else 0
        return {'saved': saved, 'errors': errors}

    @app.delete('/api/wage-baselines/{baseline_id}')
    def delete_wage_baseline(baseline_id: str = PathParam(min_length=1, max_length=40)):
        if not repo.delete_wage_baseline(baseline_id):
            raise HTTPException(404, '工资基线不存在或已删除')
        return {'deleted': baseline_id}

    @app.get('/api/reliability/compare')
    def reliability_compare(q: str = Query('', max_length=300), page_size: int = Query(50, ge=1, le=100),
                            companies: str = Query('', max_length=2000),
                            own_min: int = Query(0, ge=0, le=10), city_min: int = Query(0, ge=0, le=10)):
        """横评页的只读数据源，两种取法共用一条路径（可靠性面保持在登记过的七条上）：

        - 不带 companies：候选列表。实体表全量（含未算过的公司），分数照实 null，按名称排序。
        - 带 companies（'|' 分隔，≤8 家）：各家的岗位分层概要（职类构成 + 职类×经验×学历逐层
          报价对照）。薪资依赖岗位构成，公司级中位数跨公司不可比——分层口径与 job_stats.pay_analysis
          完全同源（同层同城、本公司 ≥2 条且同层 ≥5 条才给中位数），页面数字与报告口径一致。
          每家按实体口径（名称+别名）聚岗位，与候选列表的 jobs_count 同口径——主体归并后
          别名名下的岗位立即计入分层。
          own_min/city_min 是横评页「对比口径」下拉的入图门槛（0=用默认值）：只放宽哪些层
          出中位数，数据与报告同源，**不影响任何报告数字与分数**。
        """
        if companies.strip():
            from .reliability import job_stats
            names = [name.strip() for name in companies.split('|') if name.strip()][:8]
            all_jobs = repo.jobs()
            rows = []
            for name in names:
                identity = repo.find_company(name)
                if identity:
                    keys = {Repository.normalise_name(value) for value
                            in [identity['name'], *(identity.get('aliases') or [])] if str(value or '').strip()}
                else:
                    keys = {Repository.normalise_name(name)}
                breakdown = dict(own_min=own_min, city_min=city_min) if own_min > 0 or city_min > 0 else {}
                rows.append(dict(company=name, **job_stats.stratum_breakdown(all_jobs, keys, **breakdown)))
            return {'companies': rows}
        return repo.compare_page(q, page_size)
