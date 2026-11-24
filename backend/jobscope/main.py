"""Local FastAPI service. Run on 127.0.0.1; API never accepts cookies."""

import importlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Literal
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
from .analyze_runs import AnalyzeManager
from .backup import BackupError, BackupStore
from .chat import answer_question, model_configured
from .chat_runs import GLOBAL_SCOPE, RunManager
from . import collect_service
from . import company_merge
from . import drill
from . import resume_workshop as workshop
from .bindings import ModelBindings, normalise_binding
from .interview import assemble_material, company_brief_from, persona_cards
from .interview_runs import InterviewManager, interview_scope, new_session
from .prep_plan_runs import PrepAdjustManager, PrepPlanManager
from .transcript import parse_labeled_transcript
from .transcript_runs import TranscriptManager, new_transcript
from .knowledge import KnowledgeImportManager, KnowledgeStore
from .logs import setup as setup_logs
from .models import AnalysisStatusInput, AnalyzeInput, AttributionInput, BossOpenInput, BossTabPageInput, BrowserCollectInput, ChatInput, ChatRunInput, CollectInput, CompanyLookupInput, ConversationPatch, DetailBatchInput, EvidenceInput, GapNarrativeInput, ImportConfirmInput, ImportLinksInput, IndexInput, InterviewCreateInput, InterviewTurnInput, JdAssessmentInput, JobBatchDelete, JobBatchUpdate, JobDedupMark, JobDedupRelease, JobInput, KnowledgeImportInput, KnowledgeInput, KnowledgePatch, KnowledgeSettingsInput, ManualDescriptionInput, MergeAcceptInput, MergeInput, ModelBindingInput, OptimizeResumeInput, OutreachInput, PrepAdjustInput, PrepPlanGenerateInput, PrepTaskNoteInput, ProfileEntryInput, ProfileEntryPatch, RemoteBasketInput, ResumeInput, ResumePreviewInput, ResumeVersionInput, ResumeVersionPatch, ResumeVersionPinInput, RunwayInput, ScopeExpansionInput, SignalSourceInput, SplitInput, StyleAssessmentInput, TranscriptImportInput, TranscriptSegmentsInput
from .repository import Repository, now
from .scope_expansion import ScopeExpansionStore
from .semantic import aggregate_requirements, apply_overrides, quote_digest
from .settings import LlmChannelInput, LlmChannelStore, ModelSettings, ModelSettingsInput, SystemSettingsInput, SystemSettingsStore, ConnectionInput, _active, get_model_config, purpose_configured, use_config


setup_logs()
logger = logging.getLogger(__name__)


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
    logger.info('JobScope 服务启动：数据目录=%s', data_dir)
    repo = Repository(data_dir)
    app = FastAPI(title="知根 JobScope", docs_url="/api/docs", redoc_url=None)
    app.state.repo = repo
    settings = ModelSettings(repo)
    llm_channels = LlmChannelStore(repo)
    bindings = ModelBindings(repo)
    system_settings = SystemSettingsStore(repo)
    bindings.migrate_legacy_activation(settings)
    runs = RunManager(repo)
    analyze_runs = AnalyzeManager(repo)
    interview_runs = InterviewManager(repo)
    transcript_runs = TranscriptManager(repo)
    prep_plan_runs = PrepPlanManager(repo)
    prep_adjust_runs = PrepAdjustManager(repo)
    scope_expansions = ScopeExpansionStore(repo)
    knowledge = KnowledgeStore(repo)
    knowledge_imports = KnowledgeImportManager(knowledge, repo)
    app.state.knowledge_imports = knowledge_imports  # exposed for tests: 并发上传要走同一个锁
    app.state.runs = runs  # exposed for tests: join worker threads to avoid cross-test leaks
    app.state.analyze_runs = analyze_runs  # exposed for tests: join worker threads to avoid cross-test leaks
    app.state.interview_runs = interview_runs  # exposed for tests: join worker threads to avoid cross-test leaks
    app.state.transcript_runs = transcript_runs  # exposed for tests: join worker threads to avoid cross-test leaks
    app.state.prep_plan_runs = prep_plan_runs  # exposed for tests: join worker threads to avoid cross-test leaks
    app.state.prep_adjust_runs = prep_adjust_runs  # exposed for tests: join worker threads to avoid cross-test leaks
    from .extensions import ExtensionStore, MCPInput, MCPPatch, SkillInput, SkillFilesInput, SkillResourceInput, OnlineSkillInput, ToggleInput, ApprovalInput
    extensions = ExtensionStore(repo)
    # 数据备份（2026-11-18）：`.data/` 是生产数据，备份/恢复能力是一等公民而非运维纪律。
    backups = BackupStore(repo, data_dir)

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
            return install(extensions, data.repo, data.path, system_settings.effective()['market_source'])
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
        source = system_settings.effective()['market_source']
        try:
            return await run_in_threadpool(search_online, q, source)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post('/api/agent/market/refresh')
    async def refresh_market():
        from .market import refresh_online, payload
        source = system_settings.effective()['market_source']
        try:
            result = await run_in_threadpool(refresh_online, source)
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
        # 静态资源与 /api 无关：不组装模型配置（effective 含多次库读），contextvar 落到默认兜底。
        if not request.url.path.startswith('/api/'):
            return await call_next(request)
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
        from .channels import CHANNELS, MODEL_METADATA
        return dict(channels=CHANNELS, model_metadata=MODEL_METADATA)

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

    @app.post('/api/settings/models/test-purpose/{purpose}')
    def test_model_purpose(purpose: str):
        """按功能当前生效配置实测（渠道 + 模型 + 密钥全部来自绑定，不回显给前端）。"""
        try:
            return settings.test_purpose(purpose)
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
            raise HTTPException(422, str(exc)) from exc
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
                logger.exception('浏览器采集失败：%s「%s」', data.source, data.query)
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
                 login=item.get('login', 'none'),
                 cities=list(item.get('codes') or []), hot=list(item.get('hot') or []),
                 browser_collectable=source in collectable)
            for source, item in connectors.SOURCES.items()]}

    # 来源健康度已在 api_reliability.register 挂载（8252698 拆分）；这里不再重复注册同一路径，
    # FastAPI 按注册顺序取第一个，残留的旧副本会把 api_reliability 里的改动整个架空。
    @app.get('/api/logs')
    def read_logs(level: str = Query('', pattern='^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$'),
                  q: str = Query('', max_length=200),
                  after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=2000),
                  source: str = Query('', pattern='^(app|access)$')):
        """运行日志环形缓冲：前端「运行日志」页按 after 轮询增量。

        只读进程内存（logs._RING），重启即清空、不落盘；uvicorn 访问行里对 /api/logs
        自身的轮询已在 handler 里滤掉，这里不会再收到自己的请求记录。source 留空=
        全部，'app'=业务日志（前端默认），'access'=请求日志（uvicorn 访问行 + httpx
        出站请求行）。
        """
        from .logs import recent_logs
        return recent_logs(after=after, level=level, q=q, limit=limit, source=source)

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

    # ── 数据备份与恢复（2026-11-18）：SQLite 在线快照打 zip 存 .data/backups，恢复先做安全备份 ──

    @app.get('/api/backup')
    def backup_status():
        return backups.status()

    @app.post('/api/backup')
    def create_backup():
        try:
            return {'backup': backups.create()}
        except BackupError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/backup/{backup_id}/download')
    def download_backup(backup_id: str = PathParam(min_length=6, max_length=80)):
        try:
            content = backups.read(backup_id)
        except BackupError as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(content=content, media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="{backup_id}.zip"'})

    @app.delete('/api/backup/{backup_id}')
    def delete_backup(backup_id: str = PathParam(min_length=6, max_length=80)):
        try:
            backups.delete(backup_id)
        except BackupError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'ok': True}

    @app.post('/api/backup/restore')
    async def restore_backup(request: Request):
        raw = await request.body()
        if not raw:
            raise HTTPException(422, '请选择要恢复的备份包（.zip）')
        try:
            # 恢复会整体替换活库内容；内存里的会话/任务状态仍是旧数据，页面刷新后一致。
            return await run_in_threadpool(backups.restore, raw)
        except BackupError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/jobs")
    def jobs(q: str = Query(default="", max_length=300), location: str = Query(default="", max_length=300), source: str = Query(default="", max_length=100),
             salary_min: float | None = Query(default=None, ge=0), salary_max: float | None = Query(default=None, ge=0),
             starred: bool = Query(default=False), pipeline_status: str = Query(default="", max_length=20),
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        # 列表卡片只渲染摘要：description 裁剪后整页响应从 MB 级降到几十 KB，全文走 /jobs/{id}。
        return repo.page('jobs', page, page_size, q=q, location=location, source=source, salary_min=salary_min,
                         salary_max=salary_max, starred=starred, pipeline_status=pipeline_status, trim_description=600)

    # 机会管线回顾：纯本地统计。路径含两段（/jobs/pipeline/review），不会撞上单段的 /jobs/{job_id}。
    PIPELINE_ACTIVE = {'interested', 'applied', 'interviewing'}
    PIPELINE_REVIEW_NEW_DAYS = 7
    PIPELINE_REVIEW_STALE_DAYS = 14
    PIPELINE_REVIEW_CAP = 8

    def _pipeline_review_payload():
        """收藏篮子的回顾摘要：状态计数、本周新增收藏、本周状态变化、超期未跟进。

        跟进口径只看本项目已存数据：活跃状态（意向/已投递/面试中）距最近一次状态变化超过
        14 天就提醒看一眼进展；offer 与归档不算待跟进。列表各截前 8 条，按时间排序。"""
        from datetime import datetime, timezone
        from .prescription import build as build_prescriptions
        reference = datetime.now(timezone.utc)
        items = []
        for job in repo.jobs():
            status = job.get('pipeline_status')
            if not status:
                continue
            changed = job.get('pipeline_status_changed_at') or ''
            starred_at = job.get('starred_at') or ''
            def age(value):
                try:
                    return (reference - datetime.fromisoformat(str(value))).total_seconds()
                except (TypeError, ValueError):
                    return None
            items.append(dict(id=job['id'], title=job.get('title', ''), company=job.get('company', ''),
                              status=status, changed_at=changed, starred_at=starred_at,
                              changed_age=age(changed), starred_age=age(starred_at)))
        totals: dict[str, int] = {}
        for item in items:
            totals[item['status']] = totals.get(item['status'], 0) + 1
        week = PIPELINE_REVIEW_NEW_DAYS * 86400
        stale_after = PIPELINE_REVIEW_STALE_DAYS * 86400
        new_favorites = sorted([item for item in items if item['starred_age'] is not None and item['starred_age'] <= week],
                               key=lambda item: item['starred_age'])[:PIPELINE_REVIEW_CAP]
        recent_changes = sorted([item for item in items if item['changed_age'] is not None and item['changed_age'] <= week],
                                key=lambda item: item['changed_age'])[:PIPELINE_REVIEW_CAP]
        follow_ups = sorted([item for item in items if item['status'] in PIPELINE_ACTIVE
                             and item['changed_age'] is not None and item['changed_age'] > stale_after],
                            key=lambda item: -item['changed_age'])[:PIPELINE_REVIEW_CAP]
        def strip(item):
            return {key: value for key, value in item.items() if key not in ('changed_age', 'starred_age')}
        return {'totals': totals, 'stale_days': PIPELINE_REVIEW_STALE_DAYS,
                'new_favorites': [strip(item) for item in new_favorites],
                'recent_changes': [strip(item) for item in recent_changes],
                'follow_ups': [strip(item) for item in follow_ups],
                # 漏斗转化与归因（2026-11-22）：曾到达口径 + 来源/定制简历/可靠度分带切片。
                'funnel': repo.pipeline_funnel(),
                # Offer 对比（2026-11-22 P1）：Offer 状态岗位并排，基线差值只对可比岗位计算。
                'offers': repo.offer_comparison(),
                # 行动处方（2026-11-22 P0）：漏斗数据 → 处方卡，收藏页回顾区直接消费。
                'prescriptions': build_prescriptions(repo)}

    @app.get('/api/jobs/pipeline/review')
    def pipeline_review():
        return _pipeline_review_payload()

    # ── 行动处方引擎（2026-11-22）：漏斗数据 → 可执行动作，规则版无 LLM、确定性可测。 ──

    @app.get('/api/jobs/pipeline/prescriptions')
    def pipeline_prescriptions():
        from .prescription import build
        return build(repo)

    # ── 面试备战计划（2026-11-24）：模型分析短板与练法 → 每日任务 → 打卡；模型不可用退规则。 ──

    @app.get('/api/prep-plan')
    def prep_plan_snapshot():
        from .prep_plan import snapshot
        active = prep_plan_runs.active()
        adjusting = prep_adjust_runs.active()
        return dict(snapshot(repo), model_configured=purpose_configured('analysis'),
                    active_run=active.snapshot() if active else None,
                    active_adjust=adjusting.snapshot() if adjusting else None)

    @app.post('/api/prep-plan')
    def prep_plan_generate(data: PrepPlanGenerateInput | None = None):
        from .prep_plan import generate, normalize_prefs, snapshot
        prefs = normalize_prefs(data.model_dump() if data else None)
        plan = generate(repo, data.days if data else 14, prefs)
        return dict(snapshot(repo), plan=plan, model_configured=purpose_configured('analysis'))

    # ---- 流式生成：后台 run + SSE（同 analyze/interview 模式）。同步入口保留给规则快路径。 ----

    @app.post('/api/prep-plan/runs')
    def prep_plan_run_start(data: PrepPlanGenerateInput | None = None):
        from .prep_plan import generate, idempotent_plan, normalize_prefs, snapshot
        prefs = normalize_prefs(data.model_dump() if data else None)
        days = data.days if data else 14
        force = bool(data.force) if data else False
        if force:
            # 整轮重排继承当前计划已调整过的 prefs（对话 set_prefs 后的最新意图），
            # 请求体里没带显式参数时不回落缺省值把用户的调整洗掉。
            stored = repo.active_prep_plan()
            if stored and (stored.get('prefs') or {}).get('minutes_per_day'):
                prefs = normalize_prefs(stored.get('prefs'))
        if not force:
            existing = idempotent_plan(repo, prefs)
            if existing is not None:
                # 同日同数据已生成：直接回快照，不起线程也不发 SSE。
                return dict(run_id=None, reused=True, plan=existing,
                            snapshot=dict(snapshot(repo), model_configured=purpose_configured('analysis')))
        if not (prefs['use_llm'] and purpose_configured('analysis')):
            # 规则快路径秒级完成：同步生成即可，不值得起 run。
            plan = generate(repo, days, prefs, force=force)
            return dict(run_id=None, reused=False, plan=plan,
                        snapshot=dict(snapshot(repo), model_configured=purpose_configured('analysis')))
        handle, reused = prep_plan_runs.start(days, prefs, force=force)
        return dict(run_id=handle.id, reused=reused)

    @app.get('/api/prep-plan/runs/{run_id}')
    def prep_plan_run_status(run_id: str = PathParam(max_length=100)):
        handle = prep_plan_runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get('/api/prep-plan/runs/{run_id}/stream')
    def prep_plan_run_stream(run_id: str = PathParam(max_length=100)):
        try:
            generator = prep_plan_runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    # ---- 对话式调整：自然语言说改哪，模型给结构化指令，规则层护栏落库（同 run + SSE 模式）。 ----

    @app.post('/api/prep-plan/adjust')
    def prep_plan_adjust_start(data: PrepAdjustInput):
        if repo.active_prep_plan() is None:
            raise HTTPException(404, '当前没有进行中的备战计划')
        if not purpose_configured('analysis'):
            raise HTTPException(400, '调整对话需要先在设置中心绑定分析模型')
        if prep_plan_runs.active() or prep_adjust_runs.active():
            raise HTTPException(409, '计划正在生成或调整中，请等它完成')
        handle, reused = prep_adjust_runs.start(data.message)
        return dict(run_id=handle.id, reused=reused)

    @app.get('/api/prep-plan/adjust/conversation')
    def prep_plan_adjust_conversation():
        """调整对话历史（含已执行摘要 meta），前端刷新后重建气泡。"""
        plan = repo.active_prep_plan()
        if plan is None or not plan.get('conversation_id'):
            return dict(conversation_id=None, messages=[])
        scope = dict(kind='prep-plan', plan_id=plan['id'])
        try:
            stored = repo.get_conversation(plan['conversation_id'], scope)
        except ValueError:
            return dict(conversation_id=None, messages=[])
        messages = [dict(id=item.get('id'), role=item.get('role'), content=item.get('content', ''),
                         changes=item.get('changes'), rebuild_pending=item.get('rebuild_pending'))
                    for item in stored['messages'] if item.get('role') in ('user', 'assistant')]
        return dict(conversation_id=stored['id'], messages=messages)

    @app.get('/api/prep-plan/adjust/runs/{run_id}')
    def prep_plan_adjust_status(run_id: str = PathParam(max_length=100)):
        handle = prep_adjust_runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get('/api/prep-plan/adjust/runs/{run_id}/stream')
    def prep_plan_adjust_stream(run_id: str = PathParam(max_length=100)):
        try:
            generator = prep_adjust_runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/prep-plan/tasks/{task_id}/done')
    def prep_task_done(task_id: str, data: PrepTaskNoteInput | None = None):
        from .prep_plan import set_task_status, snapshot
        task = set_task_status(repo, task_id, 'done', data.note if data else '')
        if task is None:
            raise HTTPException(404, '备战任务不存在')
        return dict(snapshot(repo), task=task)

    @app.post('/api/prep-plan/tasks/{task_id}/skip')
    def prep_task_skip(task_id: str):
        from .prep_plan import set_task_status, snapshot
        task = set_task_status(repo, task_id, 'skipped')
        if task is None:
            raise HTTPException(404, '备战任务不存在')
        return dict(snapshot(repo), task=task)

    @app.post('/api/prep-plan/tasks/{task_id}/reopen')
    def prep_task_reopen(task_id: str):
        from .prep_plan import set_task_status, snapshot
        task = set_task_status(repo, task_id, 'pending')
        if task is None:
            raise HTTPException(404, '备战任务不存在')
        return dict(snapshot(repo), task=task)

    @app.delete('/api/prep-plan/{plan_id}')
    def prep_plan_archive(plan_id: str):
        from .prep_plan import archive
        if not archive(repo, plan_id):
            raise HTTPException(404, '计划不存在或已归档')
        return {'archived': True}

    # ── 触达生成器（2026-11-22）：打招呼/跟进话术。模型不可用走规则兜底，生成即落版本。 ──

    @app.post('/api/jobs/{job_id}/outreach')
    def generate_outreach(job_id: str, data: OutreachInput):
        from .outreach import generate
        try:
            return generate(repo, job_id, data.kind)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/jobs/{job_id}/outreach')
    def outreach_versions(job_id: str):
        return {'versions': repo.outreach_versions(job_id)}

    @app.post('/api/jobs/{job_id}/outreach/{version_id}/used')
    def outreach_used(job_id: str, version_id: str):
        if not repo.mark_outreach_used(job_id, version_id):
            raise HTTPException(404, '话术版本不存在或已标记过')
        return {'marked': True}

    # ── 岗位去重（2026-11-19）：同公司+标题归一匹配置信候选，人工确认标记主次，统计不再双算。 ──

    @app.get('/api/jobs/duplicates/scan')
    def duplicates_scan():
        """规则扫描候选对（只读不写）+ 现有重复组；标记永远由人工确认。"""
        return {**repo.duplicate_candidates(), **repo.duplicate_groups()}

    @app.post('/api/jobs/duplicates/mark')
    def mark_duplicates(data: JobDedupMark):
        try:
            return repo.mark_duplicates(data.primary_id, data.duplicate_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/jobs/duplicates/release')
    def release_duplicates(data: JobDedupRelease):
        return {'released': repo.release_duplicates(data.ids)}

    @app.get('/api/dashboard')
    def dashboard():
        """今日概览（2026-11-18）：打开工作区的第一屏——待办、近况、数据规模与备份提醒。

        全部只读本库与本地备份目录，不发起联网采集；各区块的详细操作仍落在对应工作区
        （收藏/管线在收藏夹，采集在数据与采集，备份在设置中心）。"""
        from .prescription import build as build_prescriptions
        from .prep_plan import snapshot as prep_snapshot
        prep = prep_snapshot(repo)
        return {
            'date': now()[:10],
            'counts': repo.overview_counts(),
            'pipeline': _pipeline_review_payload(),
            'due': repo.pipeline_due_items(),
            # 本周节奏（2026-11-22 P1）：按 ISO 周聚合事件流与新收藏，末两周环比在前端算。
            'weekly': repo.pipeline_weekly(),
            # 晨间简报（2026-11-23）：近 24 小时增量计数，首屏单行条——安静时前端整条隐藏。
            'briefing': repo.daily_briefing(),
            # 行动处方（2026-11-22）：漏斗数据 → 今日行动清单 + 处方卡，今日页直接消费。
            'prescriptions': build_prescriptions(repo),
            # 面试备战计划（2026-11-24）：今日页只摆今天的任务与连续天数，完整计划在工作区。
            'prep_plan': dict(plan=prep['plan'], today=prep['today'], progress=prep['progress']) if prep['plan'] else None,
            'new_jobs': repo.recent_first_seen(),
            'backup': backups.status(),
            'recent_tasks': repo.tasks()[:5],
            'model_configured': model_configured(),
        }

    @app.get("/api/jobs/ids")
    def job_ids(q: str = Query(default="", max_length=300), location: str = Query(default="", max_length=300), source: str = Query(default="", max_length=100),
                salary_min: float | None = Query(default=None, ge=0), salary_max: float | None = Query(default=None, ge=0),
                starred: bool = Query(default=False), pipeline_status: str = Query(default="", max_length=20)):
        ids, total = repo.job_ids(q=q, location=location, source=source, salary_min=salary_min, salary_max=salary_max,
                                  starred=starred, pipeline_status=pipeline_status)
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

    # 求职参考与采集编排簇（一键完备/报告/信号/横评/工资基线/来源健康）已拆至 api_reliability，
    # 行为快照由既有测试钉住；要改管线语义去 collect_service / reliability 层，不在端点层。
    from .api_reliability import register as register_reliability
    register_reliability(app, repo, data_dir)

    # ── 主体归并：规则+LLM 扫描产候选；人工确认/手动合并/拆分是最终事实源。 ──

    def _merge_overview_payload():
        return dict(entities=repo.list_merge_entities(), proposals=repo.list_merge_proposals())

    def _resolve_pair(name_a, name_b):
        a, b = repo.find_company(name_a), repo.find_company(name_b)
        if a is None or b is None:
            missing = name_a if a is None else name_b
            raise HTTPException(422, f'主体「{missing}」不存在或已被合并')
        return a, b

    def _recompute(company_name):
        from .reliability.service import ReliabilityService
        return ReliabilityService(repo).recompute(company_name)

    @app.get('/api/companies/merge/overview')
    def merge_overview():
        return _merge_overview_payload()

    @app.post('/api/companies/merge/scan')
    def merge_scan():
        # LLM 判定在一批候选上只调一次模型，放后台线程；规则候选本身不依赖模型。
        task = repo.create_task('company_merge', '主体归并扫描')

        def worker():
            try:
                with use_config(settings.effective()):
                    summary = company_merge.scan(repo)
                task.update(status='completed', message=summary['message'], count=summary.get('merged', 0))
            except Exception as exc:
                logger.exception('主体归并扫描失败：%s', exc)
                task.update(status='failed', message=f'归并扫描失败：{exc}')
            repo.update_task(task)

        threading.Thread(target=worker, daemon=True).start()
        return dict(task=task)

    @app.post('/api/companies/merge')
    def merge_companies_manual(data: MergeInput):
        a, b = _resolve_pair(data.company_a, data.company_b)
        primary, secondary = (a, b) if data.primary == 'a' else (b, a)
        try:
            entity = repo.merge_companies(primary['id'], secondary['id'])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # 合并后数据面变了（别名岗位并入），本地重算毫秒级，让报告立即反映合并结果。
        _recompute(entity['name'])
        return dict(company=entity, **_merge_overview_payload())

    @app.post('/api/companies/merge/proposals/{proposal_id}/accept')
    def accept_merge_proposal(data: MergeAcceptInput, proposal_id: str = PathParam(min_length=3, max_length=40)):
        try:
            proposal = repo.resolve_merge_proposal(proposal_id, 'accepted')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        a, b = _resolve_pair(proposal['company_a'], proposal['company_b'])
        if data.primary == 'auto':
            jobs = {item['name']: item['jobs_count'] for item in repo.list_merge_entities()}
            keep = 'a' if jobs.get(a['name'], 0) >= jobs.get(b['name'], 0) else 'b'
        else:
            keep = data.primary
        primary, secondary = (a, b) if keep == 'a' else (b, a)
        try:
            entity = repo.merge_companies(primary['id'], secondary['id'])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        _recompute(entity['name'])
        return dict(company=entity, proposal=proposal, **_merge_overview_payload())

    @app.post('/api/companies/merge/proposals/{proposal_id}/dismiss')
    def dismiss_merge_proposal(proposal_id: str = PathParam(min_length=3, max_length=40)):
        try:
            proposal = repo.resolve_merge_proposal(proposal_id, 'dismissed')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return dict(proposal=proposal, **_merge_overview_payload())

    @app.post('/api/companies/{company_id}/split')
    def split_company_alias(data: SplitInput, company_id: str = PathParam(min_length=3, max_length=40)):
        try:
            entity = repo.split_alias(company_id, data.alias)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # 主体少了别名口径的岗位，重算让报告立即收窄；拆出的新实体没有历史报告，按需再生成。
        host = repo.find_company(company_id)
        if host:
            _recompute(host['name'])
        return dict(company=entity, **_merge_overview_payload())


    @app.put('/api/reliability/sources/{source_id}')
    def save_reliability_source(data: SignalSourceInput, source_id: str = PathParam(min_length=2, max_length=40)):
        from .reliability.sources import SIGNAL_SOURCES, SignalSourceStore
        if source_id not in SIGNAL_SOURCES:
            raise HTTPException(404, '未登记的信号来源')
        try:
            # The public model inherits the internal contract, including optional advanced fields.
            return SignalSourceStore(repo).save(source_id, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/locations')
    def locations(limit: int = Query(40, ge=1, le=100)):
        return {'locations': repo.locations(limit)}

    @app.post('/api/jobs/{job_id}/manual-description')
    def supplement_job_description(job_id: str, data: ManualDescriptionInput):
        if not data.description.strip() or not data.source_note.strip():
            raise HTTPException(status_code=422, detail='职责与来源说明不能为空')
        try:
            return repo.supplement_job_description(job_id, data.description.strip(), data.source_note.strip())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = get_job(job_id)
        related = repo.page('evidence', job_id=job_id, page_size=1)
        # 关联证据列表同样裁正文：详情页一次带 22 条卡片，全文字段会把响应撑到 MB 级。
        snapshots = repo.page('evidence', linked_job_id=job_id, company=job['company'], page_size=10, trim_text=600)
        company = repo.page('evidence', company=job['company'], include_job_postings=False, page_size=12, trim_text=600)
        return {"job": job, "evidence": snapshots['evidence'], "company_evidence": company['evidence'],
                'evidence_total': related['total']}

    @app.get('/api/jobs/{job_id}/evidence')
    def job_evidence_page(job_id: str, page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
        get_job(job_id)
        return repo.page('evidence', page, page_size, job_id=job_id, trim_text=600)

    @app.post("/api/import/job")
    def import_job(data: JobInput):
        return {"job": repo.save_job(data.model_dump(), "user_import")}

    # ---- Boss 捕获篮导入（docs/design/boss-import.md）：当前页捕获 + 链接粘贴 → 篮子 → 确认入库。
    # 篮子是 boss_import 模块的进程内单例；load_module 走 importlib 缓存，状态跨请求保留。

    @app.post("/api/import/links")
    def import_links(data: ImportLinksInput):
        module = load_module("boss_import")
        items, overflow = module.parse_links(data.text)
        if not items:
            raise HTTPException(400, "没有解析到 Boss 岗位链接；请粘贴含 zhipin.com/job_detail/ 链接的文本")
        outcome = module.basket_add(items, data_dir=data_dir)
        note = '；链接较多已截断，请分批粘贴' if overflow else ''
        return {"added": outcome['added'], "updated": outcome['updated'],
                "message": f"解析到 {len(items)} 条链接：新增 {outcome['added']}、合并 {outcome['updated']}{note}。"}

    @app.get("/api/import/boss/tabs")
    def boss_import_tabs():
        return load_module("boss_import").list_tabs(data_dir=data_dir)

    @app.post("/api/import/boss/page")
    def boss_import_page(data: BossTabPageInput):
        return load_module("boss_import").capture_page(data_dir=data_dir, tab_id=data.tab_id)

    @app.get("/api/import/basket")
    def import_basket():
        return load_module("boss_import").basket_view(repo)

    @app.post("/api/import/basket/clear")
    def import_basket_clear():
        return load_module("boss_import").basket_clear(data_dir=data_dir)

    @app.post("/api/import/basket/remote")
    def import_basket_remote(data: RemoteBasketInput):
        # 油猴脚本推送入口：任意浏览器（日常 Chrome）的 Boss 页面 → 本地 JobScope。
        # 服务端重建 id/URL 并限 zhipin.com 来源；走 GM_xmlhttpRequest，不需要 CORS。
        outcome = load_module("boss_import").absorb_remote(data.items, data.page_url, data_dir=data_dir)
        if outcome['status'] != 'ok':
            raise HTTPException(400, outcome['message'])
        return outcome

    @app.post("/api/import/confirm")
    def import_confirm(data: ImportConfirmInput):
        module = load_module("boss_import")
        summary = module.confirm(repo, data.ids, data.auto_star)
        # 导入也留任务记录：与采集同口径，任务记录保留真实状态。
        task = repo.create_task('boss', 'Boss 捕获篮导入')
        task.update(kind='boss_import', status='completed' if summary['imported'] else 'skipped',
                    count=summary['imported'], message=summary['message'])
        repo.update_task(task)
        return summary

    @app.post("/api/import/boss/favorites")
    def boss_import_favorites():
        # 自动翻页 + 页面渲染等待可能超过一分钟：后台线程 + 任务轮询，与 /api/browser/collect 同模式。
        module = load_module("boss_import")
        task = repo.create_task('boss', 'Boss 收藏导入')

        def worker():
            try:
                outcome = module.import_favorites(repo, task, data_dir=data_dir)
            except Exception:
                logger.exception('Boss 收藏导入中断')
                outcome = {'status': 'failed', 'message': '收藏导入中断；已捕获的条目保留在捕获篮。'}
            task.update(kind='boss_import', status=outcome['status'], message=outcome['message'])
            repo.update_task(task)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            task.update(status='failed', message='后台任务未能启动，请重试')
            repo.update_task(task)
        return {"task": task}

    @app.post("/api/import/boss/applied")
    def boss_import_applied():
        # Boss「已投递」同步：接住页面自己的投递记录响应（带真实投递时刻）→ 回填管线状态与事件流，
        # 不在库的入捕获篮。后台线程 + 任务轮询，与收藏导入同模式。
        module = load_module("boss_import")
        task = repo.create_task('boss', 'Boss 投递记录同步')

        def worker():
            try:
                outcome = module.import_applied(repo, task, data_dir=data_dir)
            except Exception:
                logger.exception('Boss 投递记录同步中断')
                outcome = {'status': 'failed', 'message': '投递记录同步中断；库里已回填的记录不受影响。'}
            task.update(kind='boss_import', status=outcome['status'], message=outcome['message'])
            repo.update_task(task)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            task.update(status='failed', message='后台任务未能启动，请重试')
            repo.update_task(task)
        return {"task": task}

    @app.post("/api/import/boss/browse/start")
    def boss_browse_start():
        return load_module("boss_import").browse_start(data_dir=data_dir)

    @app.post("/api/import/boss/browse/stop")
    def boss_browse_stop():
        return load_module("boss_import").browse_stop()

    @app.get("/api/import/boss/browse")
    def boss_browse_status():
        return load_module("boss_import").browse_status()

    @app.post("/api/import/boss/open")
    def boss_open_search(data: BossOpenInput):
        return load_module("boss_import").open_search(data_dir=data_dir, company=data.company)

    @app.get("/api/evidence")
    def evidence(company: str | None = Query(default=None, max_length=300), q: str = Query(default="", max_length=300), page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
        # 50 条/页的公司资料库：正文裁剪 + 命中窗口，展开全文时前端按 id 走下面的详情端点。
        return repo.page('evidence', page, page_size, q=q, company=company, trim_text=600)

    @app.get('/api/evidence/{evidence_id}')
    def read_evidence(evidence_id: str):
        item = repo.get_evidence(evidence_id)
        if item is None:
            raise HTTPException(404, "证据不存在")
        return {"evidence": item}

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

    @app.get('/api/knowledge')
    def list_knowledge(q: str = Query('', max_length=300), tag: str = Query('', max_length=30),
                       page: int = Query(1, ge=1, le=10000), page_size: int = Query(20, ge=1, le=100)):
        return knowledge.list(q=q, tag=tag, page=page, page_size=page_size)

    @app.post('/api/knowledge')
    def create_knowledge(data: KnowledgeInput):
        return knowledge.save(data.model_dump())

    # 字面路由必须在 {entry_id} 之前注册，否则 "settings"/"reembed" 会被当成条目 ID。
    @app.get('/api/knowledge/settings')
    def knowledge_settings():
        return dict(settings=knowledge.settings(), **knowledge.status())

    @app.put('/api/knowledge/settings')
    def save_knowledge_settings(data: KnowledgeSettingsInput):
        return knowledge.save_settings(data.model_dump())

    @app.post('/api/knowledge/reembed')
    def reembed_knowledge():
        try:
            return knowledge.reembed()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    # ---- 知识库批量导入任务：登记→逐文件暂存→后台导入→可取消 ----
    @app.post('/api/knowledge/imports')
    def create_knowledge_import(data: KnowledgeImportInput):
        try:
            return knowledge_imports.create(data.names)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/knowledge/imports/latest')
    def latest_knowledge_import():
        return knowledge_imports.latest()

    @app.get('/api/knowledge/imports/{task_id}')
    def get_knowledge_import(task_id: str = PathParam(min_length=1, max_length=100)):
        task = knowledge_imports.get(task_id)
        if task is None:
            raise HTTPException(404, '导入任务不存在')
        return task

    @app.post('/api/knowledge/imports/{task_id}/files')
    async def stage_knowledge_import_file(request: Request, task_id: str = PathParam(min_length=1, max_length=100),
                                          index: int = Query(ge=0, le=KnowledgeImportManager.MAX_FILES - 1), filename: str = Query(min_length=1, max_length=255)):
        from .kb_import import KB_MAX_FILE_BYTES
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > KB_MAX_FILE_BYTES:
                raise HTTPException(413, f'文件超过 {KB_MAX_FILE_BYTES // (1024 * 1024)} MB，请压缩或拆分后导入')
            content.extend(chunk)
        try:
            return await run_in_threadpool(knowledge_imports.stage, task_id, index, filename, bytes(content))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/knowledge/imports/{task_id}/start')
    def start_knowledge_import(task_id: str = PathParam(min_length=1, max_length=100)):
        try:
            return knowledge_imports.start(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/knowledge/imports/{task_id}/cancel')
    def cancel_knowledge_import(task_id: str = PathParam(min_length=1, max_length=100)):
        try:
            return knowledge_imports.cancel(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/knowledge/upload')
    async def upload_knowledge(request: Request, filename: str = Query(min_length=1, max_length=255)):
        from .kb_import import KB_MAX_FILE_BYTES
        limit_mb = KB_MAX_FILE_BYTES // (1024 * 1024)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > KB_MAX_FILE_BYTES:
                raise HTTPException(413, f'文件超过 {limit_mb} MB，请压缩或拆分后导入')
            content.extend(chunk)
        try:
            result = await run_in_threadpool(knowledge.import_file, filename, bytes(content))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return result

    @app.get('/api/knowledge/{entry_id}')
    def get_knowledge(entry_id: str = PathParam(max_length=100)):
        entry = knowledge.get(entry_id)
        if entry is None:
            raise HTTPException(404, '知识条目不存在')
        return entry

    @app.get('/api/knowledge/{entry_id}/chunks')
    def knowledge_chunks(entry_id: str = PathParam(max_length=100)):
        if knowledge.get(entry_id) is None:
            raise HTTPException(404, '知识条目不存在')
        return dict(chunks=knowledge.doc_chunks(entry_id))

    @app.put('/api/knowledge/{entry_id}')
    def update_knowledge(data: KnowledgePatch, entry_id: str = PathParam(max_length=100)):
        # data 为部分更新，缺省字段沿用现值；content 变更按该文档原策略重切。
        entry = knowledge.get(entry_id)
        if entry is None:
            raise HTTPException(404, '知识条目不存在')
        patch = data.model_dump(exclude_none=True)
        return knowledge.save({**entry, **patch, 'id': entry_id})

    @app.delete('/api/knowledge/{entry_id}')
    def delete_knowledge(entry_id: str):
        if not knowledge.delete(entry_id):
            raise HTTPException(404, '知识条目不存在')
        return dict(deleted=True)

    def analyze_preconditions(job_id):
        """评估适配共用闸门：完整JD、已有简历；返回 (job, resume, company_evidence)。

        简历解析顺序：岗位绑定的「评估用」存档（工坊版本 markdown 作全文）→ 全局我的简历。
        label 随报告落库，前端可见本次评估用的到底是哪份。
        """
        job = get_job(job_id)
        # 早期保存的 BOSS 岗位还没有正文标记，一律按列表摘要处理，避免用半截职责评估适配。
        status = job.get('description_status')
        if status == 'listing' or (job['source'] == 'boss' and status != 'complete'):
            raise HTTPException(409, '当前只有搜索列表摘要，请先取得完整岗位职责后再评估适配')
        pinned = repo.pinned_resume_version(job_id)
        if pinned and pinned['markdown'].strip():
            resume = dict(text=pinned['markdown'], format='text', revision=pinned['id'],
                          label=f"存档《{pinned['title']}》")
        else:
            resume = repo.resume()
        if not resume["text"].strip():
            raise HTTPException(400, "请先保存简历或 facts.yaml")
        company_evidence = repo.evidence(company=job["company"], include_job_postings=False)
        return job, resume, company_evidence

    def analysis_extras(job_id):
        """评估管线的本地注入：启用的求职偏好（category='preference'）转已确认事实 + 该岗位的手动改判记忆。"""
        return repo.preference_statements(), repo.list_overrides(job_id)

    @app.post("/api/analyze")
    def analysis(data: AnalyzeInput):
        job, resume, company_evidence = analyze_preconditions(data.job_id)
        try:
            if purpose_configured('analysis'):
                semantic = importlib.import_module(".semantic", __package__)
                extra_facts, overrides = analysis_extras(job['id'])
                report = semantic.analyze_semantically(job, resume, company_evidence, extra_facts=extra_facts, overrides=overrides)
            else:
                report = analyze(job, resume, company_evidence)
        except ValueError as exc:
            logger.exception('评估适配失败 job=%s「%s」：%s', job['id'], job['title'], exc)
            raise HTTPException(502, str(exc)) from exc
        repo.save_report(report)
        return report

    # 求职偏好已并入「个人资料库」category='preference'：CRUD 走 /api/profile-entries，
    # 评估注入走 repo.preference_statements()（原 /api/job-preferences 端点随表退役删除）。

    @app.put('/api/jobs/{job_id}/analysis/status')
    def update_analysis_status(data: AnalysisStatusInput, job_id: str = PathParam(max_length=100)):
        """需求行手动改判：改判记忆按岗位保存（重评沿用），并同步重算所有已存报告。"""
        get_job(job_id)
        repo.save_override(job_id, quote_digest(data.jd_quote), data.status, data.note)
        overrides = repo.list_overrides(job_id)
        summaries, _latest = repo.list_reports(job_id)
        latest_report = None
        for summary in summaries:
            report = repo.get_report(job_id, summary['id'])
            if report is None:
                continue
            rows, _count = apply_overrides(report.get('requirements', []), overrides)
            confirmed = sum(1 for row in rows if row.get('overridden'))
            if rows == report.get('requirements', []) and not confirmed:
                continue  # 该报告没有可改判的行（如规则报告），原样保留
            report['requirements'] = rows
            withhold = any('模型未完整映射此岗位原文' in row.get('rationale', '') for row in rows)
            report.update(aggregate_requirements(rows, withhold_score=withhold))
            limitations = [line for line in report.get('limitations', []) if '条要求按你的手动确认计分' not in line]
            if confirmed:
                limitations.append(f'{confirmed} 条要求按你的手动确认计分，重新评估时自动沿用。')
            report['limitations'] = limitations
            repo.update_report(summary['id'], report)
            if summary['id'] == summaries[0]['id']:
                latest_report = report
        if latest_report is None:
            return dict(report=None, message='改判已记录，重新评估时生效')
        return dict(report=latest_report, message=None)

    # ---- 评估适配流式运行：拆解/分组匹配/计分三阶段进度经 SSE 推送 ----

    @app.post("/api/analyze/runs")
    def start_analyze_run(data: AnalyzeInput):
        job, resume, company_evidence = analyze_preconditions(data.job_id)
        if not purpose_configured('analysis'):
            # 未绑定分析模型：与同步端点一致回退纯规则分析，立即返回（无需流式）。
            report = analyze(job, resume, company_evidence)
            repo.save_report(report)
            return dict(run_id=None, reused=False, report=report)
        handle, reused = analyze_runs.start(job, resume, company_evidence)
        return dict(run_id=handle.id, reused=reused)

    @app.get("/api/analyze/runs/{run_id}")
    def analyze_run_status(run_id: str = PathParam(max_length=100)):
        handle = analyze_runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get("/api/analyze/runs/{run_id}/stream")
    def analyze_run_stream(run_id: str = PathParam(max_length=100)):
        try:
            generator = analyze_runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post("/api/analyze/runs/{run_id}/cancel")
    def cancel_analyze_run(run_id: str = PathParam(max_length=100)):
        try:
            return analyze_runs.cancel(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    # ---- 模拟面试：画像卡片 → 出题 → 逐题对话 → 复盘报告（后台 run + SSE，同 analyze 模式） ----
    # 出题/面试官/复盘统一走「分析」功能绑定（effective() 已含回落全局对话模型的逻辑）。

    @app.get('/api/interview/personas')
    def interview_personas():
        cards, styles = persona_cards()
        return dict(cards=cards, styles=styles)

    def interview_material(data):
        """建场次的候选材料包：岗位（评估缺口+公司情报）> 粘贴 JD > 纯自由练。

        公司情报来自公司资料库证据（非岗位 postings），面试官据此结合公司业务提问。
        """
        if data.job_id:
            job = get_job(data.job_id)
            if job['source'] == 'boss' and job.get('description_status') != 'complete':
                raise HTTPException(409, '当前只有搜索列表摘要，请先取得完整岗位职责后再模拟面试')
            pinned = repo.pinned_resume_version(data.job_id)
            if pinned and pinned['markdown'].strip():
                resume = dict(text=pinned['markdown'], format='text', label=f"存档《{pinned['title']}》")
            else:
                resume = repo.resume()
            _summaries, report = repo.list_reports(data.job_id)
            material = assemble_material(job, resume, report, company_brief=company_brief_from(repo, job['company']))
        elif data.jd_text.strip():
            material = assemble_material(dict(id='', company=data.company.strip(), title=data.job_title.strip() or '自定义 JD',
                                              description=data.jd_text.strip(), source='user_import'),
                                         repo.resume(), company_brief=company_brief_from(repo, data.company))
        else:
            material = assemble_material(None, repo.resume(), company_brief=company_brief_from(repo, data.company))
        if data.persona_id == 'gap':
            # 空窗期专题卡：把资料库启用的空窗经历条目注入材料，出题与面试官都以它为核实基准。
            from .gap_narrative import entries_block
            material['gap_digest'] = entries_block(repo)
        # 备战计划的当日专项（weak_focus）：出题时定向覆盖，第三层闭环在此接上。
        if (data.focus or '').strip():
            material['weak_focus'] = data.focus.strip()[:120]
        if data.drill:
            # 错题强化：引用解析成重练目标快照落进材料（来源记录之后被删也不影响本场），并带上跨场通病。
            prepared = drill.prepare(repo, data.drill)
            if not prepared['drills']:
                raise ValueError('所选错题已不在复盘记录里（记录被删除或下标失效），请重新选择')
            material.update(drills=prepared['drills'], habit_note=prepared['habit_note'])
        return material

    @app.get('/api/interview/weak-points')
    def interview_weak_points():
        """错题本：既往复盘（模拟 + 真实）里被点了失分点的题，跨场合并、带重练状态。规则统计，不调模型。"""
        return drill.weak_points(repo)

    @app.post('/api/interview/sessions')
    def create_interview_session(data: InterviewCreateInput):
        if not purpose_configured('analysis'):
            raise HTTPException(400, '模拟面试需要先在设置中心绑定分析模型')
        if data.persona_id == 'custom' and not (data.job_id or data.jd_text.strip() or data.job_title.strip()):
            raise HTTPException(400, '自定义岗位需要先指明目标岗位：从岗位库选择、粘贴 JD 或填写岗位名称')
        if data.persona_id == 'reinforce' and not data.drill:
            raise HTTPException(400, '错题强化需要先选择要重练的错题')
        if data.drill and data.persona_id != 'reinforce':
            raise HTTPException(400, '错题只能在「错题强化官」下重练')
        try:
            material = interview_material(data)
            session = repo.save_interview_session(new_session(
                persona_id=data.persona_id, round_id=data.round, intensity=data.intensity,
                question_count=data.question_count, material=material,
                company_style=data.company_style, job_title=data.job_title, company_name=data.company))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        handle = interview_runs.start_plan(session)
        return dict(session_id=session['id'], run_id=handle.id)

    @app.get('/api/interview/sessions')
    def list_interview_sessions():
        return dict(sessions=repo.interview_sessions())

    def get_session_or_404(session_id):
        session = repo.get_interview_session(session_id)
        if session is None:
            raise HTTPException(404, '面试场次不存在')
        return session

    @app.get('/api/interview/sessions/{session_id}')
    def read_interview_session(session_id: str = PathParam(max_length=100)):
        session = get_session_or_404(session_id)
        handle = interview_runs.active_for_session(session_id)
        return dict(session=session, active_run=handle.snapshot() if handle else None)

    @app.get('/api/interview/sessions/{session_id}/messages')
    def interview_session_messages(session_id: str = PathParam(max_length=100)):
        session = get_session_or_404(session_id)
        if not session.get('conversation_id'):
            return dict(messages=[])
        conversation = repo.get_conversation(session['conversation_id'], interview_scope(session_id))
        return dict(messages=conversation['messages'])

    @app.post('/api/interview/sessions/{session_id}/runs')
    def start_interview_turn(data: InterviewTurnInput, session_id: str = PathParam(max_length=100)):
        session = get_session_or_404(session_id)
        if session['status'] not in ('ready', 'live') or not session.get('question_plan'):
            raise HTTPException(409, '该场次还未准备好（出题中 / 复盘生成中 / 已结束）')
        try:
            handle = interview_runs.start_turn(session, data.answer)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return dict(run_id=handle.id, session_id=session_id)

    @app.post('/api/interview/sessions/{session_id}/finish')
    def finish_interview_session(session_id: str = PathParam(max_length=100)):
        session = get_session_or_404(session_id)
        # done 也放行：从既有片段重跑复盘（重新生成）。占位点评/弱总评不满意时无需重开一场。
        if session['status'] not in ('ready', 'live', 'done'):
            raise HTTPException(409, '当前状态不能生成复盘（出题中或已在复盘）')
        handle = interview_runs.start_report(session)
        return dict(run_id=handle.id)

    @app.delete('/api/interview/sessions/{session_id}')
    def delete_interview_session(session_id: str = PathParam(max_length=100)):
        session = get_session_or_404(session_id)
        handle = interview_runs.active_for_session(session_id)
        if handle:
            raise HTTPException(409, '场次正在生成中，请先停止再删除')
        if session.get('conversation_id'):
            try:
                repo.delete_conversation(session['conversation_id'], interview_scope(session_id))
            except ValueError:
                pass  # 对话行已不在：删除会话仍要继续
        repo.delete_interview_session(session_id)
        repo.delete_interview_attribution(session_id)  # 失败博物馆归档随场次级联删除
        return dict(ok=True)

    @app.get('/api/interview/runs/{run_id}')
    def interview_run_status(run_id: str = PathParam(max_length=100)):
        handle = interview_runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get('/api/interview/runs/{run_id}/stream')
    def interview_run_stream(run_id: str = PathParam(max_length=100)):
        try:
            generator = interview_runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/interview/runs/{run_id}/cancel')
    def cancel_interview_run(run_id: str = PathParam(max_length=100)):
        try:
            return interview_runs.cancel(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    # ---- 真实面试复盘：ASR 逐字稿导入 → 说话人标注（模型初标+人工核对）→ 复盘报告 ----

    @app.post('/api/interview/transcripts')
    def import_interview_transcript(data: TranscriptImportInput):
        text = data.text.strip()
        if len(text) < 50:
            raise HTTPException(400, '逐字稿太短（至少 50 字），请确认粘贴的是面试转写文本')
        text = text[:40000]
        # 标准格式直读：自带「候选人/面试官」前缀的逐字稿本地切分直接入库，免模型标注（降本增效）。
        # 只有需要模型标注的稿子才要求绑定分析模型；岗位锚点两条路径都要校验。
        segments = parse_labeled_transcript(text)
        if not segments and not purpose_configured('analysis'):
            raise HTTPException(400, '导入复盘需要先在设置中心绑定分析模型')
        job = get_job(data.job_id)
        if segments:
            transcript = new_transcript(
                job_id=job['id'], job_title=job.get('title', ''), company=job.get('company', ''), text=text)
            transcript['segments'] = [dict(index=index, **seg) for index, seg in enumerate(segments)]
            transcript['status'] = 'labeled'
            transcript['labeling_meta'] = dict(labeled_at=now(), segment_count=len(segments), method='format')
            transcript = repo.save_interview_transcript(transcript)
            return dict(transcript_id=transcript['id'], run_id='', direct=True)
        transcript = repo.save_interview_transcript(new_transcript(
            job_id=job['id'], job_title=job.get('title', ''), company=job.get('company', ''), text=text))
        try:
            handle = transcript_runs.start_label(transcript)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return dict(transcript_id=transcript['id'], run_id=handle.id)

    @app.get('/api/interview/transcripts')
    def list_interview_transcripts():
        return dict(transcripts=repo.interview_transcripts())

    def get_transcript_or_404(transcript_id):
        transcript = repo.get_interview_transcript(transcript_id)
        if transcript is None:
            raise HTTPException(404, '逐字稿不存在')
        return transcript

    @app.get('/api/interview/transcripts/{transcript_id}')
    def read_interview_transcript(transcript_id: str = PathParam(max_length=100)):
        transcript = get_transcript_or_404(transcript_id)
        handle = transcript_runs.active_for_transcript(transcript_id)
        return dict(transcript=transcript, active_run=handle.snapshot() if handle else None)

    @app.patch('/api/interview/transcripts/{transcript_id}/segments')
    def save_transcript_segments(data: TranscriptSegmentsInput, transcript_id: str = PathParam(max_length=100)):
        transcript = get_transcript_or_404(transcript_id)
        if transcript_runs.active_for_transcript(transcript_id):
            raise HTTPException(409, '逐字稿正在生成中，请等待完成后再修改')
        if transcript['status'] not in ('labeled', 'done'):
            raise HTTPException(409, '当前状态不能修改标注')
        # 全量按序替换：index 服务端重编，防止跳号/重复；done 上改段落后报告视为过期（回 labeled 促使重生成）。
        segments = []
        for item in data.segments:
            text = item.text.strip()
            if text:
                segments.append(dict(index=len(segments), speaker=item.speaker, text=text[:4000]))
        if not segments:
            raise HTTPException(400, '至少保留一个非空段落')
        transcript['segments'] = segments
        transcript['status'] = 'labeled'
        transcript = repo.save_interview_transcript(transcript)
        return dict(transcript=transcript)

    @app.post('/api/interview/transcripts/{transcript_id}/report')
    def generate_transcript_report(transcript_id: str = PathParam(max_length=100)):
        transcript = get_transcript_or_404(transcript_id)
        # done 也放行：从既有段落重跑复盘（重新生成）。报告不满意或改过标注时无需重新导入。
        if transcript['status'] not in ('labeled', 'done') or not (transcript.get('segments') or []):
            raise HTTPException(409, '当前状态不能生成复盘（标注中或还没有标注段落）')
        try:
            handle = transcript_runs.start_report(transcript)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return dict(run_id=handle.id)

    @app.post('/api/interview/transcripts/{transcript_id}/relabel')
    def relabel_interview_transcript(transcript_id: str = PathParam(max_length=100)):
        transcript = get_transcript_or_404(transcript_id)
        if transcript['status'] not in ('imported', 'labeled', 'failed'):
            raise HTTPException(409, '当前状态不能重新标注')
        try:
            handle = transcript_runs.start_label(transcript)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return dict(run_id=handle.id)

    @app.delete('/api/interview/transcripts/{transcript_id}')
    def delete_interview_transcript(transcript_id: str = PathParam(max_length=100)):
        get_transcript_or_404(transcript_id)
        handle = transcript_runs.active_for_transcript(transcript_id)
        if handle:
            raise HTTPException(409, '逐字稿正在生成中，请先停止再删除')
        repo.delete_interview_transcript(transcript_id)
        repo.delete_interview_attribution(transcript_id)  # 失败博物馆归档随记录级联删除
        return dict(ok=True)

    @app.get('/api/interview/transcript-runs/{run_id}')
    def transcript_run_status(run_id: str = PathParam(max_length=100)):
        handle = transcript_runs.get(run_id)
        if handle is None:
            raise HTTPException(404, '运行不存在或已结束')
        return handle.snapshot()

    @app.get('/api/interview/transcript-runs/{run_id}/stream')
    def transcript_run_stream(run_id: str = PathParam(max_length=100)):
        try:
            generator = transcript_runs.stream(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return StreamingResponse(generator(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/interview/transcript-runs/{run_id}/cancel')
    def cancel_transcript_run(run_id: str = PathParam(max_length=100)):
        try:
            return transcript_runs.cancel(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get('/api/jobs/{job_id}/reports')
    def job_reports(job_id: str = PathParam(max_length=100)):
        get_job(job_id)  # 未知岗位 404
        reports, latest = repo.list_reports(job_id)
        return dict(reports=reports, latest=latest)

    @app.get('/api/jobs/{job_id}/reports/{report_id}')
    def job_report_detail(job_id: str = PathParam(max_length=100), report_id: str = PathParam(max_length=64)):
        report = repo.get_report(job_id, report_id)
        if report is None:
            raise HTTPException(404, '评估报告不存在')
        return report

    # ---- 简历工坊：模板预览、JD 定向优化、版本二次编辑与多格式导出 ----

    @app.get("/api/resume-templates")
    def resume_templates():
        return {"templates": workshop.template_summary()}

    @app.post("/api/resume-templates/preview")
    def preview_resume_template(data: ResumePreviewInput):
        if data.template_id not in workshop.TEMPLATES:
            raise HTTPException(400, "未知简历模板，请从内置模板中选择")
        return {"html": workshop.render_document(data.markdown, data.template_id, typography=data.typography)}

    @app.get("/api/resume-versions")
    def resume_versions(job_id: str = Query('', max_length=100)):
        return {"versions": repo.list_resume_versions(job_id=job_id or None)}

    @app.post("/api/resume-versions")
    def create_resume_version(data: ResumeVersionInput):
        if data.template_id not in workshop.TEMPLATES:
            raise HTTPException(400, "未知简历模板，请从内置模板中选择")
        payload = data.model_dump()
        payload['typography'] = workshop.sanitize_typography(payload.get('typography') or {})
        return repo.save_resume_version(payload)

    @app.get("/api/resume-versions/{version_id}")
    def resume_version_detail(version_id: int):
        version = repo.resume_version(version_id)
        if version is None:
            raise HTTPException(404, "简历版本不存在")
        return version

    @app.put("/api/resume-versions/{version_id}")
    def update_resume_version(version_id: int, data: ResumeVersionPatch):
        if repo.resume_version(version_id) is None:
            raise HTTPException(404, "简历版本不存在")
        if data.template_id is not None and data.template_id not in workshop.TEMPLATES:
            raise HTTPException(400, "未知简历模板，请从内置模板中选择")
        patch = {key: value for key, value in data.model_dump().items() if value is not None}
        if 'typography' in patch:
            patch['typography'] = workshop.sanitize_typography(patch['typography'] or {})
        return repo.save_resume_version(dict(id=version_id, **patch))

    @app.delete("/api/resume-versions/{version_id}")
    def delete_resume_version(version_id: int):
        if not repo.delete_resume_version(version_id):
            raise HTTPException(404, "简历版本不存在")
        return {"ok": True}

    @app.post("/api/resume-versions/{version_id}/pin")
    def pin_resume_version(version_id: int, data: ResumeVersionPinInput):
        """岗位绑定「评估用」存档：pin 互斥同岗位旧 pin；评估适配自动取用。"""
        version = repo.resume_version(version_id)
        if version is None:
            raise HTTPException(404, "简历版本不存在")
        if data.pinned and not version["job_id"]:
            raise HTTPException(422, "未绑定岗位的存档不能设为评估用；请先在工坊里绑定目标岗位")
        return repo.set_pinned_resume_version(version_id, data.pinned)

    # ── 个人资料库（2026-11-17）：完整版个人材料 + 求职偏好申报，简历工坊优化时按需取材 ──
    # 条目是用户手写的第一手事实；「我的简历」保持可为适配篇幅而删减的版本，两者分离。
    # preference 类（求职偏好）注入方向不同：启用即进评估事实池（见 preference_statements），
    # 不作工坊扩写素材。

    PROFILE_CATEGORIES = ('project', 'work', 'gap', 'education', 'skill', 'cert', 'award', 'preference', 'other')
    PROFILE_CATEGORIES_HEADER = {'project': '项目经历', 'work': '工作经历', 'gap': '空窗经历', 'education': '教育背景',
                                 'skill': '专业技能', 'cert': '证书', 'award': '奖项荣誉',
                                 'preference': '求职偏好', 'other': '其他材料'}

    def profile_block(entry_ids):
        """按选中条目拼「个人资料库摘录」文本块；未选中/已停用/不存在/偏好类条目静默跳过。"""
        wanted = list(dict.fromkeys(entry_ids or []))
        entries = {item['id']: item for item in repo.profile_entries()}
        blocks = []
        for entry_id in wanted:
            item = entries.get(entry_id)
            if item is None or not item['enabled'] or item['category'] == 'preference':
                continue
            header = '｜'.join(filter(None, (item['title'], item['org'], item['period'])))
            blocks.append(f"【{PROFILE_CATEGORIES_HEADER.get(item['category'], '其他')}】{header}\n{item['content']}")
        return '\n\n'.join(blocks)

    @app.get('/api/profile-entries')
    def list_profile_entries(enabled_only: bool = Query(default=False)):
        return {'entries': repo.profile_entries(enabled_only=enabled_only)}

    @app.get('/api/profile-entries/{entry_id}')
    def read_profile_entry(entry_id: str = PathParam(min_length=1, max_length=40)):
        item = repo.profile_entry(entry_id)
        if item is None:
            raise HTTPException(404, '资料条目不存在')
        return item

    @app.post('/api/profile-entries')
    def create_profile_entry(data: ProfileEntryInput):
        return repo.save_profile_entry(data.model_dump())

    @app.put('/api/profile-entries/{entry_id}')
    def replace_profile_entry(data: ProfileEntryInput, entry_id: str = PathParam(min_length=1, max_length=40)):
        if repo.profile_entry(entry_id) is None:
            raise HTTPException(404, '资料条目不存在')
        return repo.save_profile_entry({**data.model_dump(), 'id': entry_id})

    @app.patch('/api/profile-entries/{entry_id}')
    def patch_profile_entry(data: ProfileEntryPatch, entry_id: str = PathParam(min_length=1, max_length=40)):
        patch = {key: value for key, value in data.model_dump(exclude_none=True).items()}
        item = repo.patch_profile_entry(entry_id, patch)
        if item is None:
            raise HTTPException(404, '资料条目不存在')
        return item

    @app.delete('/api/profile-entries/{entry_id}')
    def delete_profile_entry(entry_id: str = PathParam(min_length=1, max_length=40)):
        if not repo.delete_profile_entry(entry_id):
            raise HTTPException(404, '资料条目不存在')
        return {'deleted': entry_id}

    # ── 空窗期叙事（2026-11-22）：gap 条目 → 简历段落/面试应答/电梯陈述。模型不可用退规则骨架。 ──

    @app.post('/api/profile/gap-narrative')
    def generate_gap_narrative(data: GapNarrativeInput):
        from .gap_narrative import generate
        try:
            return generate(repo, job_id=data.job_id, jd_text=data.jd_text, job_title=data.job_title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/profile/gap-narratives')
    def list_gap_narratives():
        return {'narratives': repo.gap_narratives()}

    # ── 反向JD测评（2026-11-22；2026-11-24 改 best-worst 选题）：每题 4 段真实 JD 片段点「最想投」「最不想投」
    # → 隐性筛选条件报告。纯规则，无 LLM。同页还有第二种考卷「工作风格卷」（见下），两卷同表分卷别存储。 ──

    @app.get('/api/jd-assessment/trials')
    def jd_assessment_trials(seed: int | None = None):
        from .jd_assessment import trial_list
        return trial_list(seed)

    @app.get('/api/jd-assessment')
    def list_jd_assessments(kind: Literal['bws', 'style'] | None = None):
        return {'assessments': repo.jd_assessments(kind=kind)}

    @app.post('/api/jd-assessment')
    def submit_jd_assessment(data: JdAssessmentInput):
        from .jd_assessment import score_responses
        responses = [item.model_dump() for item in data.responses]
        try:
            report = score_responses(responses, data.seed)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return repo.save_jd_assessment({'kind': 'bws', 'responses': responses, 'report': report, 'seed': data.seed})

    # ── 工作风格卷（2026-11-24，第二种考卷）：陈述句 + 五级同意度（MBTI 式自陈题）→ 工作风格报告。
    # 纯规则，无 LLM；测「你适合怎么工作」，与取舍卷的「你要什么」互补。 ──

    @app.get('/api/jd-assessment/style/trials')
    def work_style_trials(seed: int | None = None):
        from .work_style import paper
        return paper(seed)

    @app.post('/api/jd-assessment/style')
    def submit_work_style(data: StyleAssessmentInput):
        from .work_style import score_style
        responses = [item.model_dump() for item in data.responses]
        try:
            report = score_style(responses, data.seed)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return repo.save_jd_assessment({'kind': 'style', 'responses': responses, 'report': report, 'seed': data.seed})

    @app.get('/api/jd-assessment/cross')
    def jd_assessment_cross():
        """两卷各自最新一份报告的互证：读时计算、不回写旧报告，所以谁后做都能立刻互证。
        missing 列出还没做过的卷；legacy 列出仍是旧版题型报告的卷（改 best-worst 之前的取舍卷报告没有判定字段）。"""
        from .work_style import cross_report
        latest = {}
        for item in repo.jd_assessments():
            payload = item['payload']
            latest.setdefault(payload.get('kind') or 'bws', payload.get('report') or {})
        cross = cross_report(latest.get('style'), latest.get('bws'))
        missing = [kind for kind in ('bws', 'style') if kind not in latest]
        # 两卷都在却算不出互证 → 取舍卷那份是旧版（两张岗位卡二选一）报告，没有判定字段。
        legacy = ['bws'] if cross is None and not missing else []
        return {'cross': cross, 'missing': missing, 'legacy': legacy}

    # ── 生存跑道（2026-11-22 P2）：月支出/存款/社保/失业金 → 决策参考。 ──
    # 隐私硬底线：只存本机单行表、绝不进任何模型素材路径；跨页只出推导值。

    @app.get('/api/runway')
    def read_runway():
        from .runway import derive
        profile = repo.runway_profile()
        return {'profile': profile, 'derived': derive(profile) if profile else None}

    @app.put('/api/runway')
    def save_runway(data: RunwayInput):
        from .runway import clean_profile, derive
        profile = repo.save_runway_profile(clean_profile(data.model_dump()))
        return {'profile': profile, 'derived': derive(profile)}

    # ── 失败博物馆（2026-11-23）：面试可控/不可控结构化归因 + 跨场失分模式报告。纯规则，无 LLM。 ──

    @app.post('/api/interview/attributions')
    def save_interview_attribution(data: AttributionInput):
        from .failure_museum import normalize_tags
        tags = [item.model_dump() for item in data.tags]
        try:
            tags = normalize_tags(tags)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        # company/job_title/job_id 从原场记录快照：博物馆列表不依赖原场次存活，删场后归档仍在。
        if data.source == 'mock':
            session = repo.get_interview_session(data.session_id)
            if session is None:
                raise HTTPException(404, '模拟面试场次不存在，无法归档')
            company, job_title, job_id = session.get('company', ''), session.get('job_title', ''), session.get('job_id', '')
        else:
            transcript = repo.get_interview_transcript(data.session_id)
            if transcript is None:
                raise HTTPException(404, '真实复盘记录不存在，无法归档')
            company, job_title, job_id = transcript.get('company', ''), transcript.get('job_title', ''), transcript.get('job_id', '')
        return repo.save_interview_attribution(dict(
            session_id=data.session_id, source=data.source, job_id=job_id, company=company,
            job_title=job_title, outcome=data.outcome, tags=tags, note=data.note.strip()))

    @app.get('/api/interview/attributions/{session_id}')
    def read_interview_attribution(session_id: str = PathParam(max_length=100)):
        item = repo.get_interview_attribution(session_id)
        if item is None:
            raise HTTPException(404, '该场次还没有归因标注')
        return item

    @app.delete('/api/interview/attributions/{session_id}')
    def remove_interview_attribution(session_id: str = PathParam(max_length=100)):
        if not repo.delete_interview_attribution(session_id):
            raise HTTPException(404, '该场次还没有归因标注')
        return dict(ok=True)

    @app.get('/api/interview/failure-museum')
    def failure_museum_report():
        from . import failure_museum
        records = repo.interview_attributions()
        return {'records': records, 'report': failure_museum.build_pattern_report(records),
                'controlled': failure_museum.CONTROLLED_TAGS, 'uncontrolled': failure_museum.UNCONTROLLED_TAGS}

    @app.post("/api/resume-versions/optimize")
    def optimize_resume_version(data: OptimizeResumeInput):
        base_text = data.resume_text.strip() or repo.resume()["text"]
        if not base_text.strip():
            raise HTTPException(400, "请先在「我的简历」保存基础简历，或在优化表单粘贴简历文本")
        jd_text = data.jd_text.strip()
        company, job_title = data.company.strip(), data.job_title.strip()
        if data.job_id:
            job = get_job(data.job_id)
            jd_text = jd_text or job["description"]
            company = company or job["company"]
            job_title = job_title or job["title"]
        if not jd_text:
            raise HTTPException(400, "请选择目标岗位或粘贴岗位要求（JD）后再优化")
        if not purpose_configured('analysis'):
            raise HTTPException(400, "尚未配置分析模型，请先在「模型设置 → 功能绑定」为「分析」用途绑定模型渠道，再进行简历优化")
        # 个人资料库摘录：完整版材料（项目经历等），供模型把基础简历里的阉割版内容扩写充实。
        profile_materials = profile_block(data.profile_entry_ids or [])
        try:
            markdown_text, notes = workshop.optimize_resume(base_text, jd_text, company=company, job_title=job_title,
                                                            instructions=data.instructions, profile_materials=profile_materials)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from None
        version = repo.save_resume_version(dict(
            title=workshop.compose_title(job_title, company), markdown=markdown_text, template_id="classic",
            job_id=data.job_id, company=company, job_title=job_title,
            jd_digest=jd_text[:2000], base_text=base_text, notes=notes))
        if data.job_id:
            # 优化产出即该岗位的最新投递简历：自动接管「评估用」标记，同岗位旧 pin 被替代。
            repo.set_pinned_resume_version(version['id'], True)
        return version

    @app.get("/api/resume-versions/{version_id}/export")
    def export_resume_version(version_id: int, format: str = Query("pdf", pattern="^(pdf|docx|html|md)$")):
        version = repo.resume_version(version_id)
        if version is None:
            raise HTTPException(404, "简历版本不存在")
        media_type, content = workshop.export_version(version, format)
        quoted = quote(f"{version['title']}.{format}")
        return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename=resume.{format}; filename*=utf-8''{quoted}"})

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
        from .research.query import condition_context, conversation_conditions
        conversation['conditions'] = conversation_conditions(
            conversation['messages'], **condition_context(repo, conversation['scope']))
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
                logger.exception('上下文压缩摘要生成失败 conv=%s', conversation_id)
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
        # 稳态（无新证据、无岗位变更）只读返回，不进 sync 的写事务，避免与后台采集抢锁。
        status = index.sync() if index.needs_sync() else dict(updated=0, **index.status())
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
        # 一次全量岗位同时供平台过滤与 pending 计数（原先 repo.jobs() 跑两遍）。
        all_jobs = repo.jobs()
        jobs = [j for j in all_jobs if j['source'] in details.DETAIL_SOURCES]
        tasks = [t for t in repo.tasks() if t.get('kind') == 'job_details' and t.get('total') is not None]
        pending = len(details.pending_jobs(repo, jobs=all_jobs))
        return dict(total=len(jobs), complete=len(jobs) - pending, pending=pending, task=tasks[0] if tasks else None)

    @app.post('/api/browser/details')
    def start_details(data: DetailBatchInput):
        details = load_module('job_details')
        if not details.batch_lock.acquire(blocking=False):
            raise HTTPException(409, '完整JD补采正在运行')
        # 反查指定岗位与补采共用同一任务管道；标题与初始 total 按模式区分，run_batch 随即按实际池子覆盖。
        task = repo.create_task('boss', '收藏岗位反查' if data.ids else '完整JD批量补采')
        initial_total = min(data.limit, len(data.ids)) if data.ids else min(data.limit, len(details.pending_jobs(repo)))
        task.update(kind='job_details', total=initial_total, processed=0, failed=0)
        repo.update_task(task)

        def worker():
            try:
                details.run_batch(repo, task, limit=data.limit, ids=data.ids)
            finally:
                details.batch_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return dict(task=task)

    @app.post("/api/browser/start")
    def browser_start(source: str = Query(default="boss", max_length=30)):
        module = load_module("browser")
        # start() 落点按 PAGES → LOOKUP_PAGES 顺序解析（公司快查面板传 qcc/tyc 等也走同一入口），
        # 守卫必须与它同口径，否则快查渠道会被这里 400 拦掉。
        if source not in module.PAGES and source not in module.LOOKUP_PAGES:
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
