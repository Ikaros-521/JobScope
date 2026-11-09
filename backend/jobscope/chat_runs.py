"""Run-based global chat: agents execute in worker threads while clients observe SSE event logs.

Runs survive page reloads and session switches (reattach via the event log) but not process restarts.
"""
import json
import threading
import time
import uuid

from .analysis import parse_resume
from .chat import answer_question
from .extensions import ExtensionStore
from .general_agent import GeneralAgent, RunHooks
from .settings import ModelSettings, use_config

GLOBAL_SCOPE = dict(kind='global')


def sse_frame(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


class RunHandle:
    def __init__(self, run_id, conversation_id, config):
        self.id = run_id
        self.conversation_id = conversation_id
        self.config = config
        self.created = time.time()
        self.events = []
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.done = False
        self.pending_approval = False
        self.pending_payload = None
        self.thread = None

    def push(self, kind, payload):
        with self.condition:
            self.events.append((kind, payload))
            self.condition.notify_all()

    def finish(self):
        with self.condition:
            self.done = True
            self.condition.notify_all()

    def reopen(self):
        with self.condition:
            self.done = False
            self.condition.notify_all()

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

    def get(self, run_id):
        return self._runs.get(run_id)

    def active_for_conversation(self, conversation_id):
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if handle.conversation_id != conversation_id:
                continue
            running = handle.thread is not None and handle.thread.is_alive() and not handle.done
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

    def start(self, *, message, conversation_id=None, mode='research', model='', edit_message_id=None):
        # The full settings map is required: worker threads read both llm and embedding targets from it.
        config = ModelSettings(self.repo).effective()
        if model:
            config = {**config, 'llm': {**config['llm'], 'model': model}}
        title = None
        if conversation_id:
            conversation = self.repo.get_conversation(conversation_id, GLOBAL_SCOPE)
            history = self.repo.truncate_for_edit(conversation_id, GLOBAL_SCOPE, edit_message_id) if edit_message_id else conversation['messages']
        else:
            if edit_message_id:
                raise ValueError('新对话不能编辑历史消息')
            conversation_id = self.repo.create_conversation(GLOBAL_SCOPE, mode=mode, model=model)
            history = []
            title = message[:30]
        handle = RunHandle('R-' + uuid.uuid4().hex, conversation_id, config)
        with self._lock:
            blocker = self.active_for_conversation(conversation_id)
            if blocker:
                raise ValueError('当前会话正在生成回答，请等待完成或先停止')
            self._runs[handle.id] = handle
        user_message = self.repo.append_messages(conversation_id, [dict(role='user', content=message)])[0]
        if title:
            self.repo.ensure_title(conversation_id, GLOBAL_SCOPE, title)
        handle.thread = threading.Thread(target=self._execute, args=(handle, message, history, mode), daemon=True)
        handle.push('start', dict(run_id=handle.id, conversation_id=conversation_id, user_message_id=user_message['id'], mode=mode))
        handle.thread.start()
        return dict(run_id=handle.id, conversation_id=conversation_id, user_message_id=user_message['id'], title=title)

    def stream(self, run_id):
        handle = self._runs.get(run_id)
        if handle is None:
            raise ValueError('运行不存在或已结束')

        def generator():
            index = 0
            while True:
                with handle.condition:
                    if index >= len(handle.events) and not handle.done:
                        handle.condition.wait(timeout=15)
                    frames = handle.events[index:]
                    index = len(handle.events)
                    done = handle.done
                if not frames and not done:
                    yield ': ping\n\n'
                    continue
                for kind, payload in frames:
                    yield sse_frame(kind, payload)
                if done:
                    yield sse_frame('end', dict(ok=True))
                    return
        return generator

    def cancel(self, run_id):
        handle = self._runs.get(run_id)
        if handle is None:
            raise ValueError('运行不存在或已结束')
        if handle.pending_approval:
            handle.pending_approval = False
            handle.pending_payload = None
            ExtensionStore(self.repo).finish_pending(run_id)
            handle.push('cancelled', dict(message='已放弃本次工具确认'))
            handle.finish()
            return handle.snapshot()
        handle.cancel.set()
        return handle.snapshot()

    def approve(self, run_id, approved, approval_id):
        handle = self._runs.get(run_id)
        if handle is None or (handle.done and not handle.pending_approval):
            raise ValueError('运行不存在或已结束')
        if not handle.pending_approval:
            raise ValueError('该运行没有等待确认的工具调用')
        if handle.thread is not None and handle.thread.is_alive():
            raise ValueError('该运行仍在进行中')
        handle.pending_approval = False
        handle.pending_payload = None
        handle.cancel.clear()
        handle.reopen()
        handle.push('resumed', dict(run_id=handle.id))
        handle.thread = threading.Thread(target=self._resume, args=(handle, approved, approval_id), daemon=True)
        handle.thread.start()
        return handle.snapshot()

    def _hooks(self, handle):
        return RunHooks(
            on_event=lambda item: handle.push('step', item),
            on_delta=lambda piece: handle.push('delta', dict(text=piece)),
            on_reasoning=lambda piece: handle.push('reasoning_delta', dict(text=piece)),
            should_stop=handle.cancel.is_set,
        )

    def _execute(self, handle, message, history, mode):
        try:
            with use_config(handle.config):
                if mode == 'general':
                    result = GeneralAgent(self.repo).start(message, history, handle.conversation_id, self._hooks(handle), run_id=handle.id)
                else:
                    from .research.agent import ResearchAgent
                    saved = self.repo.resume()
                    facts = parse_resume(saved['text'], saved['format']) if saved['text'].strip() else []
                    result = ResearchAgent(self.repo, synthesizer=answer_question).answer(
                        message, GLOBAL_SCOPE, resume=facts, history=history,
                        on_event=lambda item: handle.push('step', item))
                    result.setdefault('conversation_id', handle.conversation_id)
            self._settle(handle, result, mode)
        except ValueError as exc:
            handle.push('error', dict(message=str(exc)))
            handle.finish()
        except Exception:
            handle.push('error', dict(message='生成失败，请稍后重试'))
            handle.finish()

    def _resume(self, handle, approved, approval_id):
        try:
            with use_config(handle.config):
                result = GeneralAgent(self.repo).resume(handle.id, approved, approval_id, self._hooks(handle))
            self._settle(handle, result, 'general')
        except ValueError as exc:
            handle.push('error', dict(message=str(exc)))
            handle.finish()
        except Exception:
            handle.push('error', dict(message='生成失败，请稍后重试'))
            handle.finish()

    def _settle(self, handle, result, mode):
        if result.get('pending_approval'):
            handle.pending_approval = True
            handle.pending_payload = dict(run_id=result['trace']['id'], **result['pending_approval'])
            handle.push('approval_required', handle.pending_payload)
            handle.finish()
            return
        cancelled = handle.cancel.is_set()
        if cancelled and mode != 'general' and not result.get('interrupted'):
            # Research runs have no partial text to keep; drop the finished answer when stopped.
            handle.push('cancelled', dict(message='已停止本轮生成'))
            handle.finish()
            return
        if cancelled and not result.get('interrupted'):
            result['interrupted'] = True
        stored = self._persist(handle, result)[0]
        handle.push('done', dict(message_id=stored['id'], answer=result['answer'], mode=result.get('mode', 'agent'),
                                 interrupted=result.get('interrupted', False)))
        handle.finish()

    def _persist(self, handle, result):
        entry = dict(role='assistant', content=result['answer'], mode=result.get('mode', 'agent'))
        if result.get('reasoning'):
            entry['reasoning'] = result['reasoning']
        if result.get('citations'):
            entry['citations'] = result['citations']
        if result.get('trace'):
            entry['trace'] = result['trace']
        if result.get('metrics'):
            entry['metrics'] = result['metrics']
        if result.get('interrupted'):
            entry['interrupted'] = True
        return self.repo.append_messages(handle.conversation_id, [entry])
