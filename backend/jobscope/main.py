"""Local FastAPI service. Run on 127.0.0.1; API never accepts cookies."""

import importlib
import os
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .analysis import analyze, parse_resume
from .chat import answer_question, model_configured
from .chat_runs import GLOBAL_SCOPE, RunManager
from .models import AnalyzeInput, BrowserCollectInput, ChatInput, ChatRunInput, CollectInput, ConversationPatch, DetailBatchInput, EvidenceInput, IndexInput, JobInput, ResumeInput
from .repository import Repository
from .settings import ModelSettings, ModelSettingsInput, ConnectionInput, _active


def create_app(data_dir=None):
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(data_dir or os.getenv("JOBSCOPE_DATA_DIR", str(root / ".data")))
    repo = Repository(data_dir)
    app = FastAPI(title="知根 JobScope", docs_url="/api/docs", redoc_url=None)
    app.state.repo = repo
    settings = ModelSettings(repo)
    runs = RunManager(repo)
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
            result = GeneralAgent(repo).resume(run_id, data.approved, data.approval_id)
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

    @app.get('/api/settings/channels')
    def model_channels():
        from .channels import CHANNELS
        return dict(channels=CHANNELS)

    @app.post('/api/settings/models/discover')
    def discover_models(data: ConnectionInput):
        try:
            return settings.discover(data.target, data.config)
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
            return settings.test(data.target, data.config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except ConnectionError as exc:
            raise HTTPException(502, str(exc)) from None

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
            return dict(kind='job', job_id=job['id'], company=job['company'])
        raise HTTPException(400, '暂不支持该对话范围')

    def load_module(name):
        try:
            return importlib.import_module("." + name, __package__)
        except ModuleNotFoundError as exc:
            raise HTTPException(503, "采集模块或可选依赖尚未安装") from exc

    def persist_collection(task, result):
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

    def run_collection(source, query, location, limit, use_browser=False):
        task = repo.create_task(source, query)
        try:
            connectors = load_module("connectors")
            if use_browser or source in connectors.BROWSER_SOURCES:
                result = load_module("browser").collect(source, query, location=location, limit=limit, data_dir=data_dir)
            else:
                result = connectors.collect(source, query, location=location, limit=limit, cache_dir=data_dir / "cache")
            return persist_collection(task, result)
        except Exception:
            task.update(status="failed", message="采集未完成，请检查来源状态及本地依赖后重试")
            repo.update_task(task)
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
                 cities=list(item.get('codes') or []), browser_collectable=source in collectable)
            for source, item in connectors.SOURCES.items()]}

    @app.post("/api/collect")
    def collect(data: CollectInput):
        return run_collection(data.source, data.query, data.location, data.limit)

    @app.get("/api/tasks")
    def tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.page('tasks', page, page_size)

    @app.get("/api/jobs")
    def jobs(q: str = Query(default="", max_length=300), location: str = Query(default="", max_length=300), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.page('jobs', page, page_size, q=q, location=location)

    @app.get('/api/companies')
    def companies(q: str = Query('', max_length=300), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return repo.companies_page(q, page, page_size)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = get_job(job_id)
        related = repo.page('evidence', job_id=job_id, page_size=1)
        snapshots = repo.page('evidence', linked_job_id=job_id, company=job['company'], page_size=10)
        company = repo.page('evidence', company=job['company'], include_job_postings=False, page_size=4)
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
            if model_configured():
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
        if data.agent_mode == 'general' and data.scope != 'global':
            raise HTTPException(400, '通用 Agent 仅在全局对话中可用，公司与岗位对话继续使用隔离检索')
        if data.scope == 'global' and (data.job_id or data.company):
            raise HTTPException(400, '全局对话不能同时指定岗位或公司')
        if data.scope != 'global' and not data.job_id and not data.company:
            raise HTTPException(400, "请先选择岗位或公司，限定问答资料范围")
        if data.scope == 'global':
            scope_identity = dict(kind='global')
            if data.agent_mode == 'general':
                scope_identity['agent_mode'] = 'general'
        elif data.job_id:
            job = get_job(data.job_id)
            if data.company and data.company != job["company"]:
                raise HTTPException(400, "所选岗位与公司范围不一致")
            scope_identity = dict(kind='job', job_id=job['id'], company=job['company'])
        else:
            scope_identity = dict(kind='company', company=data.company)
        try:
            conversation_id, history = repo.conversation(data.conversation_id, scope_identity)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        resume = repo.resume()
        # Send only confirmed YAML facts to the model; unconfirmed facts stay local.
        facts = parse_resume(resume["text"], resume["format"]) if resume["text"].strip() else []
        try:
            if data.agent_mode == 'general':
                from .general_agent import GeneralAgent
                result = GeneralAgent(repo).start(data.message, history, conversation_id)
                if result['trace']['status'] != 'awaiting_approval':
                    repo.append_conversation(conversation_id, data.message, result['answer'])
                return {**result, 'scope': scope_identity}
            from .research.agent import ResearchAgent
            result = ResearchAgent(repo, synthesizer=answer_question).answer(data.message, scope_identity, resume=facts, history=history)
            repo.append_conversation(conversation_id, data.message, result['answer'])
            return {**result, 'conversation_id': conversation_id, 'scope': scope_identity}
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get('/api/research/runs/{run_id}')
    def research_run(run_id: str):
        from .research.agent import ResearchAgent
        run = ResearchAgent(repo).run(run_id)
        if run is None:
            raise HTTPException(404, '研究运行不存在')
        return run

    @app.post('/api/chat/runs')
    def start_chat_run(data: ChatRunInput):
        if data.agent_mode == 'general' and not model_configured():
            raise HTTPException(400, '通用 Agent 需要先配置支持工具调用的对话模型。也可切回资料研究模式。')
        scope = chat_scope('job' if data.job_id else ('company' if data.company.strip() else 'global'),
                           data.company, data.job_id)
        if data.agent_mode == 'general' and scope['kind'] != 'global':
            raise HTTPException(400, '岗位与公司对话只检索本范围的已保存资料；通用 Agent 请在全局对话中使用。')
        try:
            return runs.start(message=data.message, conversation_id=data.conversation_id or None,
                              mode=data.agent_mode, model=data.model, edit_message_id=data.edit_message_id,
                              scope=scope)
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
            return runs.approve(run_id, data.approved, data.approval_id)
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
        return conversation

    @app.patch('/api/conversations/{conversation_id}')
    def patch_conversation(conversation_id: str, data: ConversationPatch):
        try:
            repo.update_conversation(conversation_id, title=data.title, mode=data.mode, model=data.model)
            conversation = repo.get_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        conversation.pop('messages', None)
        return conversation

    @app.delete('/api/conversations/{conversation_id}')
    def delete_conversation(conversation_id: str):
        handle = runs.active_for_conversation(conversation_id)
        if handle:
            runs.cancel(handle.id)
        try:
            repo.delete_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return dict(ok=True)

    @app.delete('/api/conversations/{conversation_id}/messages/{message_id}')
    def delete_conversation_message(conversation_id: str, message_id: str):
        if runs.active_for_conversation(conversation_id):
            raise HTTPException(409, '会话正在生成中，请等待完成或先停止')
        try:
            repo.delete_message(conversation_id, None, message_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return dict(ok=True)

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
        task = repo.create_task('boss', job['title'] + ' · 完整JD')
        output = details.collect_detail(job, data_dir=data_dir)
        saved = details.save_detail(repo, output) if output['status'] == 'completed' else job
        task.update(kind='job_details', status=output['status'], message=output['message'], count=int(output['status'] == 'completed'))
        repo.update_task(task)
        return dict(task=task, job=saved)

    @app.get('/api/browser/details/status')
    def details_status():
        details = load_module('job_details')
        jobs = [j for j in repo.jobs() if j['source'] == 'boss']
        tasks = [t for t in repo.tasks() if t.get('kind') == 'job_details' and t.get('total') is not None]
        complete = sum(j.get('description_status') == 'complete' for j in jobs)
        return dict(total=len(jobs), complete=complete, pending=len(jobs) - complete, task=tasks[0] if tasks else None)

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

    @app.post("/api/browser/collect")
    def browser_collect(data: BrowserCollectInput):
        return run_collection(data.source, data.query, data.location, data.limit, use_browser=True)

    dist = root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
