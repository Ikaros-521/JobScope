"""评估适配的流式运行管理：后台线程跑语义管线，SSE 重放进度与最终报告。

与 chat_runs 同一套模式但职责单一：一个 job 同时最多一个活动 run，重复发起返回
既有 run 供前端续接（切走再切回、刷新页面均适用，事件日志从头发放可重建进度态）。
事件序列 start → stage/group… → done|error|cancelled → end；done 携带完整报告
（worker 先落库再推送，前端拿到的就是已持久化的那份）。取消是合作式：管线在下个
阶段/组边界退出，进行中的单次模型调用不可打断（UI 有说明）。
"""
import logging
import threading
import time
import uuid

from .chat_runs import RunHandle, sse_frame
from .semantic import RunCancelled, analyze_pipeline

logger = logging.getLogger(__name__)


class AnalyzeManager:
    """In-process registry of analyze runs; one active run per job."""

    def __init__(self, repo):
        self.repo = repo
        self._runs = {}
        self._lock = threading.RLock()

    def get(self, run_id):
        return self._runs.get(run_id)

    def active_for_job(self, job_id):
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if handle.job_id == job_id and not handle.done:
                return handle
        return None

    def _register(self, handle):
        with self._lock:
            if len(self._runs) > 100:
                for run_id, stale in list(self._runs.items()):
                    if stale.done and time.time() - stale.created > 3600:
                        self._runs.pop(run_id, None)
            self._runs[handle.id] = handle

    def start(self, job, resume, company_evidence):
        """已有活动 run 时原样返回（reused=True），不并发起第二份。"""
        existing = self.active_for_job(job['id'])
        if existing:
            return existing, True
        handle = RunHandle(uuid.uuid4().hex, '', None)
        handle.job_id = job['id']
        handle.error = None
        self._register(handle)
        handle.push('start', dict(run_id=handle.id, job_id=job['id']))
        thread = threading.Thread(target=self._execute, args=(handle, job, resume, company_evidence), daemon=True)
        handle.thread = thread
        thread.start()
        return handle, False

    def _execute(self, handle, job, resume, company_evidence):
        started = time.time()
        try:
            # 本地注入与同步端点同一份：启用的求职偏好转已确认事实 + 该岗位的手动改判记忆。
            extra_facts = [{'id': pref['id'], 'statement': pref['statement']}
                           for pref in self.repo.job_preferences(enabled_only=True)]
            report = analyze_pipeline(job, resume, company_evidence,
                                      on_event=lambda kind, payload: handle.push(kind, payload),
                                      should_stop=handle.cancel.is_set,
                                      extra_facts=extra_facts, overrides=self.repo.list_overrides(job['id']))
            logger.info('评估适配完成 job=%s「%s」用时=%.0fs', job['id'], job['title'], time.time() - started)
            self.repo.save_report(report)
            handle.push('done', report)
        except RunCancelled:
            logger.info('评估适配已取消 job=%s「%s」用时=%.0fs', job['id'], job['title'], time.time() - started)
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            logger.warning('评估适配失败 job=%s「%s」：%s', job['id'], job['title'], exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception as exc:  # 未预期异常也要让前端解锁，不能让 run 卡在“分析中”。
            logger.exception('评估适配异常 job=%s「%s」', job['id'], job['title'])
            handle.error = '评估适配发生意外错误，详情见运行日志'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

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
        handle.cancel.set()
        return handle.snapshot()
