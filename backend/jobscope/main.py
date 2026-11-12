"""Local FastAPI service. Run on 127.0.0.1; API never accepts cookies."""

import importlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
# 名字避让：上面的 Path 是 pathlib.Path，路径参数用别名取，两者不能共用一个名字。
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .analysis import analyze, parse_resume
from .chat import answer_question, model_configured
from .chat_runs import GLOBAL_SCOPE, RunManager
from . import collect_service
from .bindings import ModelBindings, normalise_binding
from .models import AnalyzeInput, BrowserCollectInput, ChatInput, ChatRunInput, CollectInput, CompanyLookupInput, CompanyResearchInput, ConversationPatch, DetailBatchInput, EvidenceInput, IndexInput, JobBatchDelete, JobBatchUpdate, JobInput, ModelBindingInput, ReliabilityInput, ResumeInput, ScopeExpansionInput, SignalCollectInput, SignalExtractInput, SignalSourceInput
from .repository import Repository, now
from .scope_expansion import ScopeExpansionStore
from .settings import LlmChannelInput, LlmChannelStore, ModelSettings, ModelSettingsInput, SystemSettingsInput, SystemSettingsStore, ConnectionInput, _active, get_model_config, purpose_configured


SUMMARY_SYSTEM = ('你是求职情报工作台的对话上下文压缩引擎，把早期对话提炼为高密度结构化摘要。'
                  '要求：1.保留用户意图、涉及的公司/岗位/薪资等关键事实与已得出的结论；'
                  '2.保留出现过的证据ID（形如 E-xxx）、来源链接与未完成的待办事项；'
                  '3.去除寒暄与冗余；4.用中文 Markdown 输出，以「### 📋 上下文压缩摘要」作为标题开头。')
# 单次摘要请求的字符上限（约 4–5k tokens）；超过就分段提炼再合并，绝不截断原文。
SUMMARY_CHUNK_CHARS = 16000
# 整段历史的硬上限：超过时明确拒绝，而不是悄悄丢掉后面的内容。
SUMMARY_MAX_CHARS = 240000


def _summary_chunks(messages):
    chunks, current, size = [], [], 0
    for message in messages:
        text = f"【{'用户' if message.get('role') == 'user' else '助手'}】{message.get('content', '')}"
        if current and size + len(text) > SUMMARY_CHUNK_CHARS:
            chunks.append('\n\n'.join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def summarise_messages(config, messages):
    """Summarise every message; long histories are summarised in chunks and merged (no truncation)."""
    from .model_transport import complete
    chunks = _summary_chunks(messages)
    if not chunks:
        raise ValueError('没有可压缩的消息')
    if sum(len(chunk) for chunk in chunks) > SUMMARY_MAX_CHARS:
        raise OverflowError
    if len(chunks) == 1:
        summary, _ = complete(config, SUMMARY_SYSTEM, '请压缩以下历史对话：\n\n' + chunks[0],
                              temperature=0.3, max_tokens=2048, timeout=120)
        return summary.strip()
    partials = []
    for index, chunk in enumerate(chunks, 1):
        part, _ = complete(config, SUMMARY_SYSTEM, f'这是同一段历史对话的第 {index}/{len(chunks)} 段，先提炼本段：\n\n{chunk}',
                           temperature=0.3, max_tokens=2048, timeout=120)
        partials.append(f'### 第 {index} 段\n{part.strip()}')
    merged, _ = complete(config, SUMMARY_SYSTEM,
                         '以下是同一段历史对话的分段摘要，请合并为一份不重复、不丢失关键事实的总摘要：\n\n' + '\n\n'.join(partials),
                         temperature=0.3, max_tokens=2048, timeout=120)
    return merged.strip()


def create_app(data_dir=None):
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(data_dir or os.getenv("JOBSCOPE_DATA_DIR", str(root / ".data")))
    repo = Repository(data_dir)
    app = FastAPI(title="知根 JobScope", docs_url="/api/docs", redoc_url=None)
    app.state.repo = repo
    settings = ModelSettings(repo)
    llm_channels = LlmChannelStore(repo)
    bindings = ModelBindings(repo)
    bindings.migrate_legacy_activation(settings)
    system_settings = SystemSettingsStore(repo)
    runs = RunManager(repo)
    scope_expansions = ScopeExpansionStore(repo)
    app.state.runs = runs  # exposed for tests: join worker threads to avoid cross-test leaks
    from .extensions import ExtensionStore, MCPInput, MCPPatch, SkillInput, SkillFilesInput, SkillResourceInput, OnlineSkillInput, ToggleInput, ApprovalInput
    extensions = ExtensionStore(repo)

    @app.get('/api/agent/extensions')
    def agent_extensions():
        return extensions.public()

    @app.post('/api/agent/mcp')
    def add_mcp(data: MCPInput):
        try:
            return extensions.save_server(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put('/api/agent/mcp/{server_id}')
    def replace_mcp(server_id: str, data: MCPInput):
        try:
            return extensions.save_server(data, server_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.patch('/api/agent/mcp/{server_id}')
    def update_mcp(server_id: str, data: MCPPatch):
        try:
            return extensions.patch_server(server_id, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/agent/mcp/{server_id}/test')
    async def test_mcp(server_id: str):
        try:
            return await extensions.test_server(server_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete('/api/agent/mcp/{server_id}')
    def delete_mcp(server_id: str):
        extensions.delete('mcp', server_id)
        return dict(ok=True)

    @app.post('/api/agent/skills')
    def add_skill(data: SkillInput):
        try:
            return extensions.save_skill(data.content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/agent/skills/files')
    def add_skill_files(data: SkillFilesInput):
        try:
            return extensions.save_skill_files([item.model_dump() for item in data.files])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/agent/skills/archive')
    async def add_skill_archive(request: Request, filename: str = Query(min_length=1, max_length=255)):
        raw = await request.body()
        if not filename.lower().endswith('.zip') or len(raw) == 0:
            raise HTTPException(422, '请上传 .zip 压缩包')
        try:
            return await run_in_threadpool(extensions.save_skill_archive, raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/agent/skills/install')
    def install_online_skill(data: OnlineSkillInput):
        from .market import install_online_skill as install
        try:
            return install(extensions, data.repo, data.path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/agent/skills/{skill_id}/export')
    def export_skill(skill_id: str):
        try:
            content, name = extensions.export_skill(skill_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        quoted = quote(f'{name}.zip')
        return Response(content=content, media_type='application/zip', headers={'Content-Disposition': f"attachment; filename=skill.zip; filename*=utf-8''{quoted}"})

    @app.get('/api/agent/market')
    def agent_market():
        from .market import payload
        return payload(extensions)

    @app.get('/api/agent/market/search')
    async def search_market(q: str = Query(min_length=1, max_length=50)):
        from .market import search_online
        try:
            return await run_in_threadpool(search_online, q)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post('/api/agent/market/refresh')
    async def refresh_market():
        from .market import refresh_online, payload
        try:
            result = await run_in_threadpool(refresh_online)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
        extensions.save_market_cache(result)
        return payload(extensions)

    @app.patch('/api/agent/skills/{skill_id}')
    def toggle_skill(skill_id: str, data: ToggleInput):
        try:
            return extensions.toggle_skill(skill_id, data.enabled)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/agent/skills/{skill_id}/resource')
    def read_skill_resource(skill_id: str, data: SkillResourceInput):
        try:
            return extensions.read_resource(skill_id, data.path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete('/api/agent/skills/{skill_id}')
    def delete_skill(skill_id: str):
        extensions.delete('skills', skill_id)
        return dict(ok=True)

    @app.post('/api/agent/runs/{run_id}/approval')
    def agent_approval(run_id: str, data: ApprovalInput):
        from .general_agent import GeneralAgent
        try:
            handle = runs.get(run_id)
            if handle is not None:
                runs.approve(run_id, data.approved, data.approval_id, data.grant)
                handle.thread.join()
                if hasattr(handle, 'error'):
                    raise ValueError(handle.error)
                return handle.result
            result = GeneralAgent(repo).resume(run_id, data.approved, data.approval_id, grant=data.grant)
            if result['trace']['status'] != 'awaiting_approval':
                repo.append_conversation(result['conversation_id'], result['trace']['question'], result['answer'])
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.middleware('http')
    async def model_configuration(request: Request, call_next):
        token = _active.set(settings.effective())
        try:
            return await call_next(request)
        finally:
            _active.reset(token)

    @app.get('/api/settings/models')
    def read_model_settings():
        return settings.public(settings.effective())

    @app.get('/api/settings/system')
    def read_system_settings():
        return system_settings.effective()

    @app.put('/api/settings/system')
    def save_system_settings(data: SystemSettingsInput):
        try:
            return system_settings.save(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/settings/channels')
    def model_channels():
        from .channels import CHANNELS
        return dict(channels=CHANNELS)

    @app.post('/api/settings/models/discover')
    def discover_models(data: ConnectionInput):
        try:
            return settings.discover(data.target, data.config, data.channel_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except ConnectionError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.put('/api/settings/models')
    def save_model_settings(data: ModelSettingsInput):
        try:
            return settings.save(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/settings/models/test')
    def test_model_settings(data: ConnectionInput):
        try:
            return settings.test(data.target, data.config, data.channel_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except ConnectionError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get('/api/settings/llm-channels')
    def list_llm_channels():
        return dict(channels=llm_channels.public(llm_channels.list()))

    def read_bindings_payload():
        """Bindings plus the actually-effective runtime summary per purpose; keys are never echoed."""
        from .bindings import PURPOSES
        effective = settings.effective()
        catalog = {item['id']: item for item in llm_channels.list()}
        result = {}
        for purpose in PURPOSES:
            binding = bindings.get(purpose)
            row = effective['llm' if purpose == 'agent' else purpose]
            entry = dict(binding=binding, inherited=not binding or binding.get('kind') == 'inherit', resolved=dict(
                configured=row.get('mode') == 'remote', mode=row.get('mode', 'disabled'),
                model=row.get('model', ''), context_tokens=row.get('context_tokens')))
            channel_id = (binding or {}).get('channel_id')
            if channel_id:
                channel = catalog.get(channel_id)
                entry['channel_name'] = channel['name'] if channel else '渠道已删除'
                entry['channel_missing'] = channel is None
            result[purpose] = entry
        return result

    @app.get('/api/settings/bindings')
    def read_model_bindings():
        return read_bindings_payload()

    @app.put('/api/settings/bindings')
    def save_model_binding(data: ModelBindingInput):
        purpose = data.purpose
        if data.kind == 'inherit':
            if purpose not in {'extraction', 'analysis'}:
                raise HTTPException(422, '该功能必须显式选择渠道与模型')
            bindings.clear(purpose)
            return read_bindings_payload()
        payload = {k: v for k, v in dict(kind=data.kind, channel_id=data.channel_id, model=data.model, local_model=data.local_model).items() if v is not None}
        try:
            normalised = normalise_binding(purpose, payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if normalised['kind'] == 'channel':
            try:
                channel = llm_channels.get(normalised['channel_id'])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            if purpose == 'embedding' and channel.get('protocol') == 'anthropic':
                raise HTTPException(422, 'Anthropic 协议渠道不提供向量模型，请选择其他渠道')
        bindings.save(purpose, normalised)
        return read_bindings_payload()

    @app.post('/api/settings/llm-channels')
    def save_llm_channel(data: LlmChannelInput):
        try:
            channel = llm_channels.public([llm_channels.save(data)])[0]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return dict(channel=channel, channels=llm_channels.public(llm_channels.list()))

    @app.delete('/api/settings/llm-channels/{channel_id}')
    def delete_llm_channel(channel_id: str):
        references = bindings.channel_references(channel_id)
        if references:
            names = {'agent': '对话 Agent', 'embedding': '语义检索', 'extraction': '信号抽取', 'analysis': '简历适配分析'}
            raise HTTPException(422, '该渠道正被以下功能使用：' + '、'.join(names.get(item, item) for item in references) + '。请先在「功能与模型」中改绑或关闭。')
        try:
            llm_channels.delete(channel_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return dict(ok=True, channels=llm_channels.public(llm_channels.list()), bindings=read_bindings_payload())

    @app.post('/api/settings/llm-channels/{channel_id}/activate')
    def activate_llm_channel(channel_id: str):
        """设为对话默认：写一条指向该渠道的 agent 绑定。"""
        try:
            llm_channels.activate(channel_id, bindings)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return dict(settings=settings.public(settings.effective()), bindings=read_bindings_payload())

    @app.middleware("http")
    async def local_write_protection(request: Request, call_next):
        try:
            host = urlsplit("//" + request.headers.get("host", ""))
            # Parse bracketed IPv6 correctly; splitting on ':' would reject ::1.
            if host.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"} or "@" in host.netloc or host.path or host.query or host.fragment:
                return JSONResponse(status_code=400, content={"detail": "不允许的服务主机名"})
            _ = host.port
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "服务主机名无效"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            length = request.headers.get("content-length")
            importing = request.method == 'POST' and request.url.path == '/api/resume/import'
            maximum = 10 * 1024 * 1024 if importing else 600000
            if length and (not length.isdigit() or int(length) > maximum):
                return JSONResponse(status_code=413, content={"detail": '文件超过 10 MB，请压缩或缩减后导入' if importing else '请求超过大小限制'})
            # No CORS permission is granted. Also reject cross-site form/JS writes.
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "拒绝跨来源写入，请从本地应用访问"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors normally echo input; suppress potentially sensitive bodies.
        return JSONResponse(status_code=422, content={"detail": "输入格式、长度或字段无效"})

    def get_job(job_id):
        job = repo.job(job_id)
        if job is None:
            raise HTTPException(404, "岗位不存在")
        return job

    def chat_scope(kind, company='', job_id=None):
        """Conversation scopes match the legacy /api/chat encoding, so history carries over."""
        if kind == 'global':
            return GLOBAL_SCOPE
        if kind == 'company':
            if not company.strip():
                raise HTTPException(400, '公司对话需要先指定精确公司名称')
            return dict(kind='company', company=company.strip())
        if kind == 'job':
            if not job_id:
                raise HTTPException(400, '岗位对话需要先指定岗位')
            job = get_job(job_id)
            if company.strip() and company.strip() != job['company']:
                raise HTTPException(400, '所选岗位与公司范围不一致')
            return dict(kind='job', job_id=job['id'], company=job['company'])
        raise HTTPException(400, '暂不支持该对话范围')

    def load_module(name):
        try:
            return importlib.import_module("." + name, __package__)
        except ModuleNotFoundError as exc:
            raise HTTPException(503, "采集模块或可选依赖尚未安装") from exc

    # 采集编排已下沉 collect_service（页面端点与 Agent 内置工具共用一条管线），这里只保留端点签名。
    def persist_collection(task, result):
        return collect_service.persist_collection(repo, task, result)

    def execute_collection(source, query, task, location='', limit=10, use_browser=False):
        return collect_service.execute_collection(repo, data_dir, source, query, task, location, limit, use_browser)

    def run_collection(source, query, location, limit, use_browser=False):
        return collect_service.run_collection(repo, data_dir, source, query, location, limit, use_browser)

    @app.post("/api/company-research")
    def company_research(data: CompanyResearchInput):
        # 多平台公司检索：公告（接口）与国内岗位源（浏览器）逐渠道串行跑；渠道各自独立 try，
        # 单渠道失败/需要登录只反映在该渠道任务上，不影响其余渠道，前端按任务逐个提示。
        company = data.company.strip()
        # 渠道编排：公告（接口）→ 企查查/天眼查（公开页快查）→ 国内岗位源（浏览器/接口）。
        # liepin 是接口源（connectors），browser 渠道由 BROWSER_SOURCES 判定。
        plan = ['cninfo', 'qcc', 'tyc', 'boss', 'zhaopin', '51job', 'liepin']
        tasks = [repo.create_task(source, company) for source in plan]

        def worker():
            for source, task in zip(plan, tasks):
                try:
                    if source in ('qcc', 'tyc'):
                        execute_lookup(source, company, task)
                    else:
                        execute_collection(source, company, task, limit=10)
                except Exception:
                    task.update(status="failed", message="该渠道检索失败，已跳过；不影响其他渠道")
                    repo.update_task(task)

        threading.Thread(target=worker, daemon=True).start()
        return {"tasks": tasks}

    @app.post("/api/browser/collect")
    def browser_collect(data: BrowserCollectInput):
        # 大数量翻页（如智联 500 条）耗时远超浏览器请求超时，采集放后台线程，前端轮询任务状态。
        task = repo.create_task(data.source, data.query)

        def report(items, pages):
            task.update(message=f"已读取 {items} 条岗位（第 {pages} 页），读完后自动保存")
            repo.update_task(task)

        def worker():
            try:
                result = load_module("browser").collect(data.source, data.query, location=data.location,
                                                        limit=data.limit, data_dir=data_dir, progress=report)
                persist_collection(task, result)
            except Exception:
                task.update(status="failed", message="采集未完成，请检查来源状态及本地依赖后重试")
                repo.update_task(task)

        threading.Thread(target=worker, daemon=True).start()
        return {"task": task, "jobs": []}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_configured": model_configured()}

    @app.get("/api/sources")
    def sources():
        connectors = load_module("connectors")
        try:
            collectable = set(load_module("browser").PAGES)
        except HTTPException:
            collectable = set()
        return {"sources": [
            dict(id=source, name=item['name'], kind=item['kind'], transport_mode=item['transport'],
                 status=item['status'], description=item['description'],
                 cities=list(item.get('codes') or []), hot=list(item.get('hot') or []),
                 browser_collectable=source in collectable)
            for source, item in connectors.SOURCES.items()]}

    @app.get('/api/cities')
    def cities():
        # 拼音是城市名的属性，不是来源的属性，所以整表共用一份。
        return {'pinyin': getattr(load_module('connectors'), 'CITY_PINYIN', {})}

    @app.post("/api/collect")
    def collect(data: CollectInput):
        return run_collection(data.source, data.query, data.location, data.limit)

    def execute_lookup(channel, company, task):
        """企查查/天眼查公开页快查：管线在 collect_service（与 Agent 的 company_web_lookup 工具共用）。"""
        outcome, _ = collect_service.execute_lookup(repo, data_dir, channel, company, task)
        return outcome

    @app.post("/api/company/lookup")
    def company_lookup(data: CompanyLookupInput):
        # 公开网页快查：单页同步执行（FastAPI 线程池承载，页加载 + 渲染等待约半分钟内）。
        task = repo.create_task(data.channel, data.company)
        outcome = execute_lookup(data.channel, data.company, task)
        return {"task": task, "cards": outcome.get('cards', [])}

    @app.get("/api/tasks")
    def tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.page('tasks', page, page_size)

    @app.get("/api/tasks/{task_id}")
    def read_task(task_id: str):
        task = repo.task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return {"task": task}

    @app.get("/api/jobs")
    def jobs(q: str = Query(default="", max_length=300), location: str = Query(default="", max_length=300), source: str = Query(default="", max_length=100),
             salary_min: float | None = Query(default=None, ge=0), salary_max: float | None = Query(default=None, ge=0),
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.page('jobs', page, page_size, q=q, location=location, source=source, salary_min=salary_min, salary_max=salary_max)

    @app.get("/api/jobs/ids")
    def job_ids(q: str = Query(default="", max_length=300), location: str = Query(default="", max_length=300), source: str = Query(default="", max_length=100),
                salary_min: float | None = Query(default=None, ge=0), salary_max: float | None = Query(default=None, ge=0)):
        ids, total = repo.job_ids(q=q, location=location, source=source, salary_min=salary_min, salary_max=salary_max)
        return {"ids": ids, "total": total}

    @app.patch("/api/jobs")
    def batch_update_jobs(data: JobBatchUpdate):
        patch = {key: value for key, value in data.model_dump(exclude_none=True).items() if key != 'ids'}
        return {"updated": repo.update_jobs(data.ids, patch)}

    @app.post("/api/jobs/batch-delete")
    def batch_delete_jobs(data: JobBatchDelete):
        return {"deleted": repo.delete_jobs(data.ids)}

    @app.get('/api/companies')
    def companies(q: str = Query('', max_length=300), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.companies_page(q, page, page_size)

    # 求职参考分析。路径挂在 /api/companies/{company}/... 与 /api/reliability/... 之下，
    # 与上面的 /api/companies 列表端点按完整路径匹配，不受注册顺序影响。
    # 参数化路径一律带长度上限，percent-decode 由 Starlette 完成，取值先过实体归一的规范化再使用：
    # 写路径（POST 重算、POST 采集）用 resolve_company 确保实体存在，读路径（GET）用 find_company
    # 同口径查找、查不到就是没有，绝不因为一次拼错的查询留下 companies 行；
    # 出站 URL 由 collectors 用 urlencode 拼接，这里不存在原样插值。
    # reliability 子模块全部走函数级 import：包的 __init__ 保持零导入，浏览器与模型依赖
    # 就不会被打进不需要它们的请求路径。

    def company_materials(identity):
        """该公司已保存的非岗位材料。名称按实体规范化比对，否则 '甲 公司' 存的证据永远配不上 '甲公司'。

        两类「自己写下的记录」都不算材料：本地计算记录会让计数随点击自增，付费接口原文则是
        取数结果而不是证据来源，喂给抽取器等于让厂商字段第二轮变成「材料」。
        """
        from .reliability.local import DERIVED_KIND
        from .reliability.paid import PAID_KIND
        known = {identity['name'], *(identity.get('aliases') or [])}
        return [item for item in repo.evidence(include_job_postings=False)
                if Repository.normalise_name(item['company']) in known
                and item['kind'] not in (DERIVED_KIND, PAID_KIND)]

    def company_jd_materials(identity):
        """完整 JD 的岗位证据（P1 起 team_size_statement 要从岗位描述里抽「团队 N 人」）。

        只有 description_status == 'complete' 的岗位进材料：半截 JD 不能用来评估，这是 P0
        在 /api/analyze 里就立过的规矩。证据行仍是 save_job 落的那条真实 job_posting 记录，
        evidence_id 可回查原文；列表摘要与别家公司照样不进。
        """
        known = {identity['name'], *(identity.get('aliases') or [])}
        complete = [item for item in repo.jobs()
                    if Repository.normalise_name(item['company']) in known
                    and item.get('description_status') == 'complete']
        wanted = {evidence_id for job in complete for evidence_id in job.get('evidence_ids', [])}
        return [item for item in repo.evidence(include_job_postings=True)
                if item['kind'] == 'job_posting' and item['id'] in wanted and item['text'].strip()]

    def company_identity(name, credit_code=''):
        """实体归一走 repository 的强键口径；service.company() 只接名称，这里还要带上信用代码。"""
        try:
            return repo.resolve_company(name, credit_code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
        from .reliability import collectors, local, paid
        from .reliability.sources import SIGNAL_SOURCES, SignalSourceStore
        if data.source not in SIGNAL_SOURCES:
            raise HTTPException(400, '未登记的信号来源')
        store = SignalSourceStore(repo)
        # 闸门语义取 sources.enabled：免费来源被手动关闭同样算关，付费来源还要存有密钥。
        if not store.enabled(data.source):
            if SIGNAL_SOURCES[data.source]['tier'] == 'paid':
                raise HTTPException(409, '该付费来源尚未启用；请先在设置里填入你自己的密钥，JobScope 不代持密钥。')
            raise HTTPException(409, '该来源已在设置里关闭，请重新启用后再采集。')
        identity = company_identity(company, data.credit_code)
        # 分派只看登记表里的 transport，三条取数通路互不越界：
        # browser_page → 项目浏览器读公开页面；local → 只读本项目已存数据；
        # http_api（只有付费来源用得上）→ 按声明式字段映射取数。
        # READERS 就是「由项目浏览器采集」的那一组来源；其余非本地来源连浏览器模块都不必导入，
        # 由 collectors 直接退成 unsupported，浏览器依赖因此不会进入不需要它的路径。
        spec = SIGNAL_SOURCES[data.source]
        from_local = spec['transport'] == 'local'
        from_paid = spec['transport'] == 'http_api' and spec['tier'] == 'paid'
        if from_local:
            # 本地来源反过来完全不碰浏览器：它只读本项目的已存数据，也不限页数、不等间隔。
            outcome = local.collect(data.source, identity['name'], repo=repo, identity=identity)
        elif from_paid:
            # client 由 store 把关：没启用或没密钥就是 None，paid 会退成 unsupported 而不发起请求。
            outcome = paid.collect(data.source, identity['name'], client=store.client(data.source))
        else:
            browser_module = load_module('browser') if data.source in collectors.READERS else None
            outcome = collectors.collect(data.source, identity['name'], browser_module=browser_module, data_dir=data_dir)
        # 每条信号先落成一条可溯源证据，再落成信号记录：证据仍是唯一的原始层。
        # url 必须取信号实际出自的那个地址：浏览器/付费接口用各自出处，本地计算没有出处就留空。
        kind = local.DERIVED_KIND if from_local else (paid.PAID_KIND if from_paid else 'disclosure')
        from .reliability.service import persist_outcome
        saved = persist_outcome(repo, identity, data.source, outcome, kind)
        return dict(status=outcome['status'], message=outcome['message'], signals=saved)

    @app.post('/api/companies/{company}/signals/extract')
    def extract_company_signals(data: SignalExtractInput, company: str = PathParam(min_length=1, max_length=300)):
        """把已存材料喂给抽取器：手动导入的公告与材料不会自己变成信号。"""
        from .reliability import extract
        from .reliability.models import SignalInput
        from .reliability.rubric import RULES
        identity = company_identity(company, data.credit_code)
        # 非岗位材料 + 完整 JD 的岗位证据，按证据编号去重后一起进抽取器；只有完整职责可被评估。
        materials = {item['id']: item for item in [*company_materials(identity), *company_jd_materials(identity)]
                     if item['text'].strip()}
        materials = list(materials.values())[:data.limit]
        outcome = extract.extract(identity['name'], materials, max_tokens=data.max_tokens) if materials \
            else dict(status='skipped', message='该公司还没有可抽取的非岗位材料。', signals=[], raw='')
        task = repo.create_task('reliability_extract', identity['name'])
        kinds = {item['id']: item['kind'] for item in materials}
        # team_size_statement 的多条原始自述在这里聚合成一条信号：中位数与相容数由代码算，
        # 模型只交原始数字；repository.signals() 每条规则只留最新一条，聚合必须发生在落库前。
        from .reliability.extract import aggregate_team_size
        size_signals = [item for item in outcome['signals'] if item.rule == 'team_size_statement']
        other_signals = [item for item in outcome['signals'] if item.rule != 'team_size_statement']
        merged, participants = aggregate_team_size(size_signals)
        aggregation_note = ''
        if merged is not None:
            aggregation_note = (f'team_size_statement 由 {len(participants)} 条自述聚合（中位数与相容数由代码计算，'
                                f'锚条编号 {merged.evidence_id}，参与编号：{"、".join(participants)}）。')
        saved = []
        # 聚合成功 → 只落合并后的一条；不足两条时原始自述照常落库（闸门会在报告里解释为什么不出分）。
        for item in ([*other_signals, merged] if merged is not None else outcome['signals']):
            spec = RULES[item.rule]
            saved.append(repo.save_signal(SignalInput(
                company_id=identity['id'], dimension=item.dimension, rule=item.rule, value=item.value,
                unit=spec.unit, source_class=extract.KIND_TO_SOURCE_CLASS.get(kinds.get(item.evidence_id, ''), 'user_material'),
                evidence_id=item.evidence_id, observed_at='', collected_at=now())))
        # 结构化状态与 raw 原文一起进 tasks，前端才显示得出真实状态、事后也查得到模型说了什么。
        # 聚合说明也写进任务：未成为锚条的参与编号在这里留档，出处链才查得回去。
        message = outcome['message'] + aggregation_note
        task.update(kind='reliability_extract', status=outcome['status'], message=message,
                    count=len(saved), raw=str(outcome['raw'])[:4000])
        repo.update_task(task)
        return dict(status=outcome['status'], message=message, accepted=len(saved),
                    signals=saved, evidence=len(materials), task=task)

    @app.get('/api/reliability/sources')
    def reliability_sources():
        from .reliability.sources import SignalSourceStore
        return {'sources': SignalSourceStore(repo).public()}

    @app.get('/api/reliability/compare')
    def reliability_compare(q: str = Query('', max_length=300), page_size: int = Query(50, ge=1, le=100)):
        """横评候选列表：只读。实体表全量（含未算过的公司），分数照实 null，按名称排序。"""
        return repo.compare_page(q, page_size)


    @app.put('/api/reliability/sources/{source_id}')
    def save_reliability_source(data: SignalSourceInput, source_id: str = PathParam(min_length=2, max_length=40)):
        from .reliability.sources import SIGNAL_SOURCES, SignalSourceInput as SourceSettings, SignalSourceStore
        if source_id not in SIGNAL_SOURCES:
            raise HTTPException(404, '未登记的信号来源')
        try:
            # 公开契约收下密钥，转成 reliability 层的设置对象；store.public() 只回 key_set 布尔。
            return SignalSourceStore(repo).save(source_id, SourceSettings(mode=data.mode, key=data.key, url=data.url))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/locations')
    def locations(limit: int = Query(40, ge=1, le=100)):
        return {'locations': repo.locations(limit)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = get_job(job_id)
        related = repo.page('evidence', job_id=job_id, page_size=1)
        snapshots = repo.page('evidence', linked_job_id=job_id, company=job['company'], page_size=10)
        company = repo.page('evidence', company=job['company'], include_job_postings=False, page_size=12)
        return {"job": job, "evidence": snapshots['evidence'], "company_evidence": company['evidence'],
                'evidence_total': related['total']}

    @app.get('/api/jobs/{job_id}/evidence')
    def job_evidence_page(job_id: str, page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
        get_job(job_id)
        return repo.page('evidence', page, page_size, job_id=job_id)

    @app.post("/api/import/job")
    def import_job(data: JobInput):
        return {"job": repo.save_job(data.model_dump(), "user_import")}

    @app.get("/api/evidence")
    def evidence(company: str | None = Query(default=None, max_length=300), q: str = Query(default="", max_length=300), page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
        return repo.page('evidence', page, page_size, q=q, company=company)

    @app.post("/api/evidence")
    def import_evidence(data: EvidenceInput):
        return {"evidence": repo.save_evidence(data.model_dump(), "user_import")}

    @app.post("/api/evidence/{evidence_id}/extract")
    def extract_evidence(evidence_id: str):
        original = repo.get_evidence(evidence_id)
        if original is None:
            raise HTTPException(404, "证据不存在")
        if original["source"] != "cninfo" or original["kind"] != "disclosure_metadata":
            raise HTTPException(400, "仅支持提取已采集的巨潮公告正文")
        try:
            documents = importlib.import_module(".documents", __package__)
            result = documents.extract_announcement(original["url"])
        except ImportError as exc:
            raise HTTPException(503, "公告正文提取需要安装 PyMuPDF") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "公告文件请求失败，请稍后重试") from exc
        saved = repo.save_evidence(dict(title=original["title"] + " 正文", company=original["company"], kind="disclosure", text=result["text"], url=original["url"], published_at=original.get("published_at")), "cninfo_pdf")
        return {"evidence": saved, "pages": result["pages"], "total_pages": result["total_pages"], "truncated": result["truncated"]}

    @app.get("/api/resume")
    def resume():
        return repo.resume()

    @app.post('/api/resume/import')
    async def import_resume(request: Request, filename: str = Query(min_length=1, max_length=255)):
        from .resume_import import extract_resume, MAX_FILE_BYTES
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_FILE_BYTES:
                raise HTTPException(413, '文件超过 10 MB，请压缩或缩减后导入')
            content.extend(chunk)
        try:
            return await run_in_threadpool(extract_resume, filename, bytes(content))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/resume")
    def save_resume(data: ResumeInput):
        try:
            parse_resume(data.text, data.format)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, "简历格式无效，请检查 facts.yaml 结构或改用纯文本") from exc
        return repo.save_resume(data.text, data.format)

    @app.post("/api/analyze")
    def analysis(data: AnalyzeInput):
        job = get_job(data.job_id)
        # 早期保存的 BOSS 岗位还没有正文标记，一律按列表摘要处理，避免用半截职责评估适配。
        status = job.get('description_status')
        if status == 'listing' or (job['source'] == 'boss' and status != 'complete'):
            raise HTTPException(409, '当前只有搜索列表摘要，请先取得完整岗位职责后再评估适配')
        resume = repo.resume()
        if not resume["text"].strip():
            raise HTTPException(400, "请先保存简历或 facts.yaml")
        company_evidence = repo.evidence(company=job["company"], include_job_postings=False)
        try:
            if purpose_configured('analysis'):
                semantic = importlib.import_module(".semantic", __package__)
                report = semantic.analyze_semantically(job, resume, company_evidence)
            else:
                report = analyze(job, resume, company_evidence)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        repo.save_report(report)
        return report

    @app.post("/api/chat")
    def chat(data: ChatInput):
        if data.scope == 'global' and (data.job_id or data.company):
            raise HTTPException(400, '全局对话不能同时指定岗位或公司')
        if data.scope != 'global' and not data.job_id and not data.company:
            raise HTTPException(400, "请先选择岗位或公司，限定问答资料范围")
        if data.conversation_id and runs.is_compressing(data.conversation_id):
            raise HTTPException(409, '会话正在压缩上下文，请稍后重试')
        if data.scope == 'global':
            scope_identity = dict(kind='global')
        elif data.job_id:
            job = get_job(data.job_id)
            if data.company and data.company != job["company"]:
                raise HTTPException(400, "所选岗位与公司范围不一致")
            scope_identity = dict(kind='job', job_id=job['id'], company=job['company'])
        else:
            scope_identity = dict(kind='company', company=data.company)
        try:
            # Validate before entering the synchronous adapter, just like /chat/runs.
            conversation = repo.get_conversation(data.conversation_id, scope_identity) if data.conversation_id else None
            config = runs.resolve_llm_config(conversation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if data.agent_mode == 'general' and (config['llm'].get('mode') != 'remote' or not config['llm'].get('model')):
            raise HTTPException(400, '通用 Agent 需要先配置支持工具调用的对话模型')
        try:
            result = runs.start(message=data.message, conversation_id=data.conversation_id,
                                mode=data.agent_mode, scope=scope_identity, background=False)
            return {**result, 'scope': scope_identity}
        except ValueError as exc:
            status = 409 if '会话' in str(exc) else 502
            raise HTTPException(status, str(exc)) from None

    @app.get('/api/research/runs/{run_id}')
    def research_run(run_id: str):
        from .research.agent import ResearchAgent
        run = ResearchAgent(repo).run(run_id)
        if run is None:
            raise HTTPException(404, '研究运行不存在')
        return run

    @app.post('/api/chat/runs')
    def start_chat_run(data: ChatRunInput):
        if data.agent_mode == 'general':
            # 以“这次实际会用的模型”判断：会话已保存的渠道/模型也算配置完成。
            try:
                conversation = repo.get_conversation(data.conversation_id) if data.conversation_id else None
                config = runs.resolve_llm_config(conversation, data.model, data.llm_channel)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if config['llm'].get('mode') != 'remote' or not config['llm'].get('model'):
                raise HTTPException(400, '通用 Agent 需要先配置支持工具调用的对话模型。也可切回资料研究模式。')
        scope = chat_scope('job' if data.job_id else ('company' if data.company.strip() else 'global'),
                           data.company, data.job_id)
        try:
            return runs.start(message=data.message, conversation_id=data.conversation_id or None,
                              mode=data.agent_mode, model=data.model, llm_channel=data.llm_channel,
                              permission_mode=data.permission_mode,
                              edit_message_id=data.edit_message_id, scope=scope)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/chat/runs/{run_id}')
    def chat_run_status(run_id: str):
        handle = runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get('/api/chat/runs/{run_id}/stream')
    def chat_run_stream(run_id: str):
        try:
            generator = runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/chat/runs/{run_id}/cancel')
    def cancel_chat_run(run_id: str):
        try:
            return runs.cancel(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post('/api/chat/runs/{run_id}/approval')
    def approve_chat_run(run_id: str, data: ApprovalInput):
        try:
            return runs.approve(run_id, data.approved, data.approval_id, data.grant)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/conversations')
    def list_conversations(scope: str = 'global', company: str = Query('', max_length=300), job_id: str | None = Query(default=None, max_length=100)):
        return dict(conversations=repo.list_conversations(chat_scope(scope, company, job_id)))

    @app.get('/api/conversations/{conversation_id}')
    def read_conversation(conversation_id: str):
        try:
            conversation = repo.get_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        handle = runs.active_for_conversation(conversation_id)
        conversation['active_run'] = handle.snapshot() if handle else None
        actions = scope_expansions.list(conversation_id)
        conversation['scope_expansions'] = actions
        conversation['lineage'] = scope_expansions.lineage(conversation_id)
        # Restore the current card status without rewriting any source history.
        by_id = scope_expansions.actions_for_messages(conversation['messages'])
        for message in conversation['messages']:
            if message.get('actions'):
                message['actions'] = [by_id.get(action.get('id'), action) for action in message['actions']]
        return conversation

    @app.post('/api/conversations/{conversation_id}/scope-expansions')
    def propose_scope_expansion(conversation_id: str, data: ScopeExpansionInput):
        try:
            return scope_expansions.propose(conversation_id, data.target, data.reason)
        except ValueError as exc:
            raise HTTPException(404 if '不存在' in str(exc) else 400, str(exc)) from None

    @app.post('/api/conversations/{conversation_id}/scope-expansions/{action_id}/accept')
    def accept_scope_expansion(conversation_id: str, action_id: str):
        with runs._lock:
            if runs.active_for_conversation(conversation_id) or runs.is_compressing(conversation_id):
                raise HTTPException(409, '会话正在生成、等待工具确认或压缩上下文，请完成或停止后再扩展')
            try:
                return scope_expansions.accept(conversation_id, action_id)
            except ValueError as exc:
                raise HTTPException(404 if '不存在' in str(exc) else 409, str(exc)) from None

    @app.patch('/api/conversations/{conversation_id}')
    def patch_conversation(conversation_id: str, data: ConversationPatch):
        try:
            repo.update_conversation(conversation_id, title=data.title, mode=data.mode, model=data.model, llm_channel=data.llm_channel, permission_mode=data.permission_mode)
            conversation = repo.get_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        conversation.pop('messages', None)
        return conversation

    @app.delete('/api/conversations/{conversation_id}')
    def delete_conversation(conversation_id: str):
        if runs.is_compressing(conversation_id):
            raise HTTPException(409, '会话正在压缩上下文，请稍后重试')
        handle = runs.active_for_conversation(conversation_id)
        if handle:
            runs.cancel(handle.id)
        try:
            repo.delete_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        extensions.clear_grants(conversation_id)
        return dict(ok=True)

    @app.delete('/api/conversations/{conversation_id}/messages/{message_id}')
    def delete_conversation_message(conversation_id: str, message_id: str):
        if runs.active_for_conversation(conversation_id):
            raise HTTPException(409, '会话正在生成中，请等待完成或先停止')
        if runs.is_compressing(conversation_id):
            raise HTTPException(409, '会话正在压缩上下文，请稍后重试')
        try:
            repo.delete_message(conversation_id, None, message_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return dict(ok=True)

    @app.post('/api/conversations/{conversation_id}/compress')
    def compress_conversation(conversation_id: str):
        """一键压缩上下文：旧消息打标折叠（不删除），边界处插入模型生成的摘要检查点。"""
        if runs.active_for_conversation(conversation_id):
            raise HTTPException(409, '会话正在生成中，请等待完成或先停止')
        if not runs.begin_compression(conversation_id):
            raise HTTPException(409, '该会话正在压缩上下文，请稍后重试')
        try:
            try:
                conversation = repo.get_conversation(conversation_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            try:
                config = runs.resolve_llm_config(conversation)['llm']
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if config.get('mode') != 'remote' or not config.get('model'):
                raise HTTPException(400, '压缩上下文需要先配置对话模型')
            messages = conversation['messages']
            keep_recent = 4
            boundary = len(messages) - keep_recent
            foldable = [m for m in messages[:boundary] if not m.get('compacted')]
            if boundary <= 0 or len(foldable) < 2:
                raise HTTPException(422, '历史消息较少，暂无需压缩')
            try:
                summary = summarise_messages(config, foldable)
            except OverflowError:
                raise HTTPException(422, '历史记录过长，单次压缩会超出模型额度；请先删除部分较早消息后重试') from None
            except Exception:
                raise HTTPException(502, '摘要生成失败，请检查模型服务后重试；历史未被修改') from None
            if not summary.strip():
                raise HTTPException(502, '模型返回了空摘要，历史未被修改')
            try:
                return repo.compress_conversation(conversation_id, boundary, summary.strip(),
                                                  expected_ids=[m.get('id') for m in messages])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        finally:
            runs.end_compression(conversation_id)

    @app.get('/api/research/index')
    def research_index_status():
        from .research.index import ResearchIndex
        from .research.providers import EmbeddingProvider
        index = ResearchIndex(repo)
        status = index.sync()
        provider = EmbeddingProvider()
        return dict(**status, embedding_configured=provider.configured, embedding_local=bool(provider.local_model), model_configured=model_configured(),
                    dense_engine='exact_cosine', lexical_engine='fts5_bm25')

    @app.post('/api/research/index')
    def research_index_update(data: IndexInput):
        from .research.index import ResearchIndex
        from .research.providers import EmbeddingProvider
        index = ResearchIndex(repo)
        if data.operation == 'embed':
            provider = EmbeddingProvider()
            if not provider.configured:
                raise HTTPException(409, '请先配置Embedding服务和模型，再构建语义向量')
            try:
                return index.embed(provider)
            except ValueError as exc:
                raise HTTPException(502, str(exc)) from exc
        return index.sync(rebuild=data.operation == 'rebuild')

    @app.get("/api/browser/status")
    def browser_status():
        try:
            return load_module("browser").status(data_dir=data_dir)
        except HTTPException:
            return dict(installed=False, running=False, message="尚未安装浏览器采集模块或 DrissionPage")

    @app.post('/api/jobs/{job_id}/detail')
    def read_job_detail(job_id: str):
        job = get_job(job_id)
        details = load_module('job_details')
        if details.batch_lock.locked():
            raise HTTPException(409, '批量JD补采正在运行，请等待完成')
        task = repo.create_task(job['source'], job['title'] + ' · 完整JD')
        output = details.collect_detail(job, data_dir=data_dir)
        saved = details.save_detail(repo, output) if output['status'] == 'completed' else job
        task.update(kind='job_details', status=output['status'], message=output['message'], count=int(output['status'] == 'completed'))
        repo.update_task(task)
        return dict(task=task, job=saved)

    @app.get('/api/browser/details/status')
    def details_status():
        details = load_module('job_details')
        jobs = [j for j in repo.jobs() if j['source'] in details.DETAIL_SOURCES]
        tasks = [t for t in repo.tasks() if t.get('kind') == 'job_details' and t.get('total') is not None]
        pending = len(details.pending_jobs(repo))
        return dict(total=len(jobs), complete=len(jobs) - pending, pending=pending, task=tasks[0] if tasks else None)

    @app.post('/api/browser/details')
    def start_details(data: DetailBatchInput):
        details = load_module('job_details')
        if not details.batch_lock.acquire(blocking=False):
            raise HTTPException(409, '完整JD补采正在运行')
        task = repo.create_task('boss', '完整JD批量补采')
        task.update(kind='job_details', total=min(data.limit, len(details.pending_jobs(repo))), processed=0, failed=0)
        repo.update_task(task)

        def worker():
            try:
                details.run_batch(repo, task, limit=data.limit)
            finally:
                details.batch_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return dict(task=task)

    @app.post("/api/browser/start")
    def browser_start(source: str = Query(default="boss", max_length=30)):
        module = load_module("browser")
        if source not in module.PAGES:
            raise HTTPException(400, "该来源不由项目浏览器采集")
        try:
            return module.start(data_dir=data_dir, source=source)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "浏览器启动失败，请检查 Chrome 和可选依赖") from exc

    dist = root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    @app.middleware('http')
    async def no_cache_shell(request, call_next):
        # 入口页每次都向服务器校验版本，避免旧前端长期驻留；带哈希的静态资源不受影响。
        response = await call_next(request)
        if request.url.path in ('/', '/index.html'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.on_event('shutdown')
    def close_mcp_sessions():
        # 常驻 MCP 会话（stdio 子进程/浏览器）随应用退出优雅回收。
        from .extensions import SESSION_POOL
        SESSION_POOL.shutdown_all()

    return app


app = create_app()
