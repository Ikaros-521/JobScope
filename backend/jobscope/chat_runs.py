"""Run-based global chat: agents execute in worker threads while clients observe SSE event logs.

Runs survive page reloads and session switches (reattach via the event log) but not process restarts.

Cancellation contract: cancel() acknowledges instantly (pushes 'cancelled' and finishes the run
under the handle lock) so the client and the conversation unlock immediately. The worker thread
keeps running until its next should_stop checkpoint; blocking calls it cannot interrupt are already
timeout-bounded. Once cancelled the handle is closed: late pushes are dropped and _settle never
persists, so no answer can appear after a stop.
"""
import logging
import threading
import time
import uuid

from .analysis import parse_resume
from .chat import answer_question
from .extensions import ExtensionStore
from .general_agent import GeneralAgent, RunHooks
from .research.agent import RunCancelled
from .run_registry import event_stream, sse_frame  # noqa: F401  (sse_frame 再导出：analyze/interview/transcript/prep_plan 由此取)
from .settings import LlmChannelStore, ModelSettings, SystemSettingsStore, use_config

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = dict(kind='global')


class RunHandle:
    def __init__(self, run_id, conversation_id, config, scope=GLOBAL_SCOPE):
        self.id = run_id
        self.conversation_id = conversation_id
        self.scope = scope
        self.config = config
        self.created = time.time()
        self.events = []
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.done = False
        self.closed = False
        self.pending_approval = False
        self.pending_payload = None
        self.thread = None
        # 活动的底层连接关闭钩子（当前模型流）；cancel 时在独立线程里执行，
        # 让等待下一个 chunk 的 worker 立刻收到断流而不是拖到 chunk 间隔结束。
        self.aborters = []

    def push(self, kind, payload):
        with self.condition:
            if self.closed:
                # Cancelled runs are sealed: late worker output never reaches clients.
                return
            self.events.append((kind, payload))
            self.condition.notify_all()

    def finish(self):
        with self.condition:
            self.done = True
            self.condition.notify_all()

    def reopen(self):
        with self.condition:
            self.done = False
            self.closed = False
            self.condition.notify_all()

    def add_abort(self, fn):
        """Register a connection-close hook fired by cancel(); returns its unregister."""
        with self.condition:
            if self.closed:
                fn()  # Race: cancelled between the check and the registration — close now.
                return lambda: None
            self.aborters.append(fn)

        def unregister():
            with self.condition:
                try:
                    self.aborters.remove(fn)
                except ValueError:
                    pass
        return unregister

    def snapshot(self):
        with self.condition:
            return dict(run_id=self.id, conversation_id=self.conversation_id, done=self.done,
                        cancelled=self.cancel.is_set(), awaiting_approval=self.pending_approval,
                        pending_approval=self.pending_payload)


class RunManager:
    """In-process registry of chat runs. One active run per conversation; cancellable between steps."""

    def __init__(self, repo):
        self.repo = repo
        self._runs = {}
        self._lock = threading.RLock()
        # 正在做上下文压缩的会话：压缩期间禁止发送、编辑、删除与再次压缩。
        self._compressing = set()

    def get(self, run_id):
        return self._runs.get(run_id)

    def active_for_conversation(self, conversation_id):
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if handle.conversation_id != conversation_id:
                continue
            # Registration reserves the source even before its worker starts.
            running = not handle.done
            if running or handle.pending_approval:
                return handle
        return None

    def _register(self, handle):
        with self._lock:
            if len(self._runs) > 200:
                for run_id, stale in list(self._runs.items()):
                    if stale.done and not stale.pending_approval and time.time() - stale.created > 3600:
                        self._runs.pop(run_id, None)
            self._runs[handle.id] = handle

    def resolve_llm_config(self, conversation=None, model=None, llm_channel=None):
        """Resolve the effective LLM config for a run.

        None means "inherit the persisted conversation"; '' means "reset" (channel default /
        global default). Explicit values win, so a deleted channel surfaces as a clear error.
        """
        config = ModelSettings(self.repo).effective()
        channel_id = llm_channel
        model_name = model
        if conversation is not None:
            if channel_id is None:
                channel_id = conversation.get('llm_channel', '')
            if model_name is None:
                model_name = conversation.get('model', '')
        channel_id = channel_id or ''
        model_name = model_name or ''
        if channel_id:
            # 会话级渠道覆盖：整组替换连接信息；模型缺省取渠道默认模型。
            payload = LlmChannelStore(self.repo).get(channel_id)
            override = {**config['llm'], 'mode': 'remote', 'channel': payload.get('channel', 'custom'),
                        'protocol': payload.get('protocol', 'openai'), 'url': payload['url'],
                        'key': payload.get('key', ''), 'local_model': '', 'model': model_name or payload.get('model', '')}
            if payload.get('context_tokens'):
                override['context_tokens'] = payload['context_tokens']
            return {**config, 'llm': override}
        if model_name:
            return {**config, 'llm': {**config['llm'], 'model': model_name}}
        return config

    def begin_compression(self, conversation_id):
        with self._lock:
            if conversation_id in self._compressing or self.active_for_conversation(conversation_id):
                return False
            self._compressing.add(conversation_id)
            return True

    def end_compression(self, conversation_id):
        with self._lock:
            self._compressing.discard(conversation_id)

    def is_compressing(self, conversation_id):
        with self._lock:
            return conversation_id in self._compressing

    def start(self, *, message, conversation_id=None, mode='general', model=None, edit_message_id=None, scope=None, llm_channel=None, permission_mode=None, background=True):
        scope = scope or GLOBAL_SCOPE
        title = None
        # Reserve history edits, appends and registration against compression and cloning.
        with self._lock:
            conversation = None
            if conversation_id:
                if self.is_compressing(conversation_id):
                    raise ValueError('会话正在压缩上下文，请稍后重试')
                if self.active_for_conversation(conversation_id):
                    raise ValueError('当前会话正在生成回答，请等待完成或先停止')
                conversation = self.repo.get_conversation(conversation_id, scope)
            elif edit_message_id:
                raise ValueError('新对话不能编辑历史消息')
            config = self.resolve_llm_config(conversation, model, llm_channel)
            configured = config['llm'].get('mode') == 'remote' and bool(config['llm'].get('model'))
            if mode == 'general' and not configured:
                raise ValueError('通用 Agent 需要先配置支持工具调用的对话模型')
            # Old research clients share the scoped runtime when a model is enabled.
            mode = 'general' if configured else 'research'
            effective_channel = (llm_channel if llm_channel is not None else (conversation or {}).get('llm_channel', '')) or ''
            effective_model = (model if model is not None else (conversation or {}).get('model', '')) or ''
            # 备用渠道（529 连续过载时切换，2026-11-25）：默认空=不降级；解析失败/渠道缺失静默跳过——
            # 备用渠道是韧性增强，它的坏配置不能把正常发起的对话一起拦下来。
            fallback_llm = None
            if mode == 'general':
                try:
                    fallback_channel = SystemSettingsStore(self.repo).effective().get('agent_fallback_channel') or ''
                    if fallback_channel and fallback_channel != effective_channel:
                        fallback_llm = self.resolve_llm_config(None, '', fallback_channel)['llm']
                        if fallback_llm.get('mode') != 'remote' or not fallback_llm.get('model'):
                            fallback_llm = None
                except Exception:
                    # 备用渠道是韧性增强：它的坏配置不能把正常发起的对话一起拦下来，降级为本轮不启用。
                    logger.info('对话备用渠道解析失败，本轮不启用降级 conv=%s', conversation_id)
            # 权限档位同模型语义：None=沿用会话已存值；显式值随本轮写回会话。
            effective_permission = permission_mode if permission_mode is not None else (conversation or {}).get('permission_mode', 'autonomous')
            if conversation is None:
                conversation_id = self.repo.create_conversation(scope, mode=mode, model=effective_model, llm_channel=effective_channel, permission_mode=effective_permission)
                history = []
                title = message[:30]
            else:
                if edit_message_id:
                    conversation_id, history = self.repo.branch_for_edit(conversation_id, scope, edit_message_id)
                else:
                    history = conversation['messages']
                self.repo.update_conversation(conversation_id, scope, mode=mode, permission_mode=effective_permission)
            # Keep original turns for deterministic conversation preferences.
            # Model adapters independently select their compacted/recent context.
            history = list(history)
            handle = RunHandle('R-' + uuid.uuid4().hex, conversation_id, config, scope)
            handle.streaming = background
            # 备用渠道 config 挂 handle：worker 线程启动后经 start(fallback_config=…) 传给 Agent。
            handle.fallback_llm = fallback_llm
            user_message = self.repo.append_messages(conversation_id, [dict(role='user', content=message)])[0]
            if title:
                self.repo.ensure_title(conversation_id, scope, title)
            self._register(handle)
            handle.push('start', dict(run_id=handle.id, conversation_id=conversation_id, user_message_id=user_message['id'], mode=mode))
            logger.info('对话运行开始 run=%s conv=%s mode=%s 权限=%s', handle.id, conversation_id, mode, effective_permission)
            if background:
                handle.thread = threading.Thread(target=self._execute, args=(handle, message, history, mode, effective_permission), daemon=True)
                handle.thread.start()
            else:
                handle.thread = threading.current_thread()
        if not background:
            try:
                self._execute(handle, message, history, mode, effective_permission)
            finally:
                # FastAPI reuses its worker thread; it is not this run's live worker.
                handle.thread = None
            if hasattr(handle, 'error'):
                raise ValueError(handle.error)
            return handle.result
        return dict(run_id=handle.id, conversation_id=conversation_id, user_message_id=user_message['id'], title=title)

    def stream(self, run_id):
        handle = self._runs.get(run_id)
        if handle is None:
            raise ValueError('运行不存在或已结束')
        # 重放生成器与 analyze/interview/prep/transcript 同款，单一来源在 run_registry。
        return event_stream(handle)

    def cancel(self, run_id):
        handle = self._runs.get(run_id)
        if handle is None:
            raise ValueError('运行不存在或已结束')
        # The whole sequence is atomic against _settle: either the run already
        # finished naturally, or 'cancelled' is acknowledged now and everything
        # the worker thread produces afterwards is discarded.
        with handle.condition:
            if handle.done and not handle.pending_approval:
                return handle.snapshot()
            handle.cancel.set()
            logger.info('对话运行取消 run=%s conv=%s（等待审批=%s）', run_id, handle.conversation_id, handle.pending_approval)
            aborters, handle.aborters = handle.aborters, []
            if aborters:
                # 关连接可能在网络栈里稍作等待；放到旁路线程执行，cancel 应答保持即时。
                threading.Thread(target=lambda: [fn() for fn in aborters], daemon=True).start()
            if handle.pending_approval:
                handle.pending_approval = False
                handle.pending_payload = None
                ExtensionStore(self.repo).finish_pending(run_id)
                handle.push('cancelled', dict(message='已放弃本次工具确认'))
            else:
                handle.push('cancelled', dict(message='已停止本轮生成'))
            handle.closed = True
            handle.finish()
            return handle.snapshot()

    def approve(self, run_id, approved, approval_id, grant=False):
        with self._lock:
            return self._approve(run_id, approved, approval_id, grant)

    def _approve(self, run_id, approved, approval_id, grant=False):
        handle = self._runs.get(run_id)
        if handle is None or (handle.done and not handle.pending_approval):
            raise ValueError('运行不存在或已结束')
        if not handle.pending_approval:
            raise ValueError('该运行没有等待确认的工具调用')
        if (handle.pending_payload or {}).get('approval_id') != approval_id:
            raise ValueError('工具确认标识已失效，请使用当前确认请求')
        if handle.thread is not None and handle.thread.is_alive():
            raise ValueError('该运行仍在进行中')
        handle.pending_approval = False
        handle.pending_payload = None
        handle.cancel.clear()
        handle.reopen()
        handle.push('resumed', dict(run_id=handle.id))
        handle.thread = threading.Thread(target=self._resume, args=(handle, approved, approval_id, grant), daemon=True)
        handle.thread.start()
        return handle.snapshot()

    def _hooks(self, handle):
        if not getattr(handle, 'streaming', True):
            return None
        return RunHooks(
            on_event=lambda item: handle.push('step', item),
            on_delta=lambda piece: handle.push('delta', dict(text=piece)),
            on_reasoning=lambda piece: handle.push('reasoning_delta', dict(text=piece)),
            should_stop=handle.cancel.is_set,
            register_abort=handle.add_abort,
            # 输出截断升级重试前作废已流出的半截：SSE 重发 start（带同一 run_id），前端实时气泡整体重置。
            on_restart=lambda: handle.push('start', dict(run_id=handle.id, restart=True)),
        )

    def _execute(self, handle, message, history, mode, permission_mode='autonomous'):
        try:
            with use_config(handle.config):
                if mode == 'general':
                    result = GeneralAgent(self.repo).start(message, history, handle.conversation_id, self._hooks(handle), run_id=handle.id, scope=handle.scope, permission_mode=permission_mode, fallback_config=getattr(handle, 'fallback_llm', None))
                else:
                    from .research.agent import ResearchAgent
                    saved = self.repo.resume()
                    facts = parse_resume(saved['text'], saved['format']) if saved['text'].strip() else []
                    result = ResearchAgent(self.repo, synthesizer=answer_question).answer(
                        message, handle.scope, resume=facts, history=history,
                        on_event=lambda item: handle.push('step', item),
                        should_stop=handle.cancel.is_set,
                        on_delta=(lambda piece: handle.push('delta', dict(text=piece))) if handle.streaming else None)
                    result.setdefault('conversation_id', handle.conversation_id)
            self._settle(handle, result, mode)
            # 对话记忆提取（2026-11-25，s09 借鉴）：done 已推送、回答已落库之后同步执行——
            # 用户已解锁不阻塞；同一 worker 线程内 use_config 上下文现成；失败静默不影响本轮。
            if mode == 'general':
                self._maybe_extract_memories(handle, result)
        except RunCancelled:
            # Already acknowledged by cancel(); the worker just reached its checkpoint.
            return
        except ValueError as exc:
            if handle.cancel.is_set():
                # 取消线程关闭模型流/MCP 会话引发的断流：停止已被确认，不广播错误。
                return
            logger.warning('对话生成失败 run=%s：%s', handle.id, exc)
            handle.error = str(exc)
            handle.push('error', dict(message=handle.error))
            handle.finish()
        except Exception:
            if handle.cancel.is_set():
                return
            logger.exception('对话生成失败 run=%s', handle.id)
            handle.error = '生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
            handle.finish()

    def _maybe_extract_memories(self, handle, result):
        """跨会话记忆提取的接入点：仅成功完成的 general 轮；开关关闭或任何异常都静默跳过。"""
        if result.get('failed') or result.get('interrupted') or result.get('pending_approval'):
            return
        try:
            if not SystemSettingsStore(self.repo).effective().get('chat_memory_enabled', True):
                return
        except Exception:
            return
        try:
            from .chat_memory import extract_memories
            trace = result.get('trace') or {}
            steps_summary = '；'.join(
                f"{event.get('label') or event.get('tool')}({event.get('status') or 'ok'})"
                for event in (trace.get('events') or [])[-12:]
                if event.get('tool') not in ('agent_model', 'model_retry', 'loop_warning', 'context_compact', 'index_sync', 'query_plan'))
            added = extract_memories(self.repo, handle.config['llm'], handle.conversation_id,
                                     trace.get('question') or '', result.get('answer') or '', steps_summary)
            if added:
                logger.info('对话记忆已沉淀 run=%s conv=%s 新增=%s条', handle.id, handle.conversation_id, added)
        except Exception as exc:
            logger.warning('对话记忆提取异常 run=%s：%s', handle.id, type(exc).__name__)

    def _resume(self, handle, approved, approval_id, grant=False):
        try:
            with use_config(handle.config):
                result = GeneralAgent(self.repo).resume(handle.id, approved, approval_id, self._hooks(handle), grant=grant)
            self._settle(handle, result, 'general')
        except ValueError as exc:
            if handle.cancel.is_set():
                return
            logger.warning('工具确认后续跑失败 run=%s：%s', handle.id, exc)
            handle.error = str(exc)
            handle.push('error', dict(message=handle.error))
            handle.finish()
        except Exception:
            if handle.cancel.is_set():
                return
            logger.exception('工具确认后续跑失败 run=%s', handle.id)
            handle.error = '生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
            handle.finish()

    def _settle(self, handle, result, mode):
        # Atomic against cancel(): whoever takes the condition first wins. If the
        # stop was acknowledged first, everything produced here is discarded;
        # otherwise the answer is persisted and 'done' precedes any 'cancelled'.
        with handle.condition:
            if handle.cancel.is_set():
                return
            result.setdefault('conversation_id', handle.conversation_id)
            handle.result = result
            if result.get('pending_approval'):
                handle.pending_approval = True
                handle.pending_payload = dict(run_id=result['trace']['id'], **result['pending_approval'])
                # 保持流打开：审批通过后 resumed 及后续事件沿同一条 SSE 续传，
                # 客户端无需重连重放整份事件日志（重放会闪现已消费的审批卡并清空实时气泡）。
                handle.push('approval_required', handle.pending_payload)
                return
            stored = self._persist(handle, result)[0]
            if result.get('failed'):
                # 先落库再广播 error：前端收到 error 即重载会话，落库后的失败消息（含执行过程）才能顶上来。
                message = result.get('failure') or '生成失败，请稍后重试'
                handle.error = message
                handle.push('error', dict(message=message, message_id=stored['id']))
                handle.finish()
                logger.warning('对话生成失败已落库 run=%s conv=%s：%s', handle.id, handle.conversation_id, message)
                return
            handle.push('done', dict(message_id=stored['id'], answer=result['answer'], mode=result.get('mode', 'agent'),
                                     interrupted=result.get('interrupted', False)))
            handle.finish()
            trace = result.get('trace') or {}
            logger.info('对话运行完成 run=%s mode=%s 时长=%sms 引用=%s',
                        handle.id, result.get('mode', 'agent'), trace.get('duration_ms', '?'),
                        len(result.get('citations') or []))

    def _persist(self, handle, result):
        entry = dict(role='assistant', content=result['answer'], mode=result.get('mode', 'agent'))
        if result.get('failed'):
            # 失败的回复也落库：执行过程（trace/timeline）必须能在历史里复查，而不是随 error 事件被清空。
            entry['failed'] = True
            entry['failure'] = result.get('failure', '')
        if result.get('reasoning'):
            entry['reasoning'] = result['reasoning']
        if result.get('citations'):
            entry['citations'] = result['citations']
        if result.get('trace'):
            entry['trace'] = result['trace']
        if result.get('timeline'):
            entry['timeline'] = result['timeline']
        if result.get('metrics'):
            entry['metrics'] = result['metrics']
        if result.get('actions'):
            entry['actions'] = result['actions']
        if result.get('interrupted'):
            entry['interrupted'] = True
        return self.repo.append_messages(handle.conversation_id, [entry])
