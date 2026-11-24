"""备战计划的流式生成运行：后台线程跑「取材 → 模型诊断（流式）→ 排期」，SSE 重放进度。

与 analyze_runs 同一套模式：RunHandle 事件日志 + SSE 重放（断线重连从头重建状态）。
备战计划是全局单例，同时只允许一个活动 run，重复发起原样返回既有 run 供前端续接。
事件序列：start → focus → model_start → diagnosis_delta… → model_done|model_fallback
→ focus_item… → scheduled → done|error → end；done 携带完整快照（worker 先落库再
推送，前端拿到的就是已持久化的那份）。不做取消：整条管线只有一次模型调用且有
超时上限，渐进事件本身已给足反馈，中途放弃反而留不下任何计划。
"""
import logging
import threading
import time
import uuid

from .chat_runs import RunHandle, sse_frame
from .settings import ModelSettings, purpose_configured, use_config

logger = logging.getLogger(__name__)


class PrepPlanManager:
    """In-process registry of prep-plan runs; one active run at a time."""

    def __init__(self, repo):
        self.repo = repo
        self._runs = {}
        self._lock = threading.RLock()

    def get(self, run_id):
        return self._runs.get(run_id)

    def active(self):
        with self._lock:
            for handle in self._runs.values():
                if not handle.done:
                    return handle
        return None

    def _register(self, handle):
        with self._lock:
            if len(self._runs) > 20:
                for run_id, stale in list(self._runs.items()):
                    if stale.done and time.time() - stale.created > 3600:
                        self._runs.pop(run_id, None)
            self._runs[handle.id] = handle

    def start(self, days, prefs):
        """已有活动 run 时原样返回（reused=True），不并发起第二份。"""
        existing = self.active()
        if existing:
            return existing, True
        handle = RunHandle(uuid.uuid4().hex, '', None)
        self._register(handle)
        handle.push('start', dict(run_id=handle.id, days=days))
        thread = threading.Thread(target=self._execute, args=(handle, days, prefs), daemon=True)
        handle.thread = thread
        thread.start()
        return handle, False

    def _execute(self, handle, days, prefs):
        from .prep_plan import generate, snapshot
        started = time.time()
        try:
            # worker 线程收不到请求中间件设置的线程级模型配置（contextvar 不跨线程），
            # 必须显式组装生效配置（含「分析」功能绑定），否则回落环境变量 → 地址为空。
            with use_config(ModelSettings(self.repo).effective()):
                generate(self.repo, days, prefs, on_event=lambda kind, payload: handle.push(kind, payload))
            logger.info('备战计划流式生成完成 用时=%.0fs origin=%s',
                        time.time() - started, (self.repo.active_prep_plan() or {}).get('origin', '?'))
            handle.push('done', dict(snapshot(self.repo), model_configured=purpose_configured('analysis')))
        except Exception:
            logger.exception('备战计划流式生成失败')
            handle.error = '备战计划生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    # ---- SSE：与 analyze_runs 同款 ----

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
