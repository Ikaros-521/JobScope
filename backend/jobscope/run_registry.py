"""后台 run 注册表基座（2026-11-28 审计 P1-3 收敛）。

此前 6 个 run 管理器（analyze/interview/prep_plan×2/transcript/chat）各抄一份
`_runs/_lock/_register/stream`，且「查活动」与「登记」分两次拿锁——并发双击可起两个
run。本基座统一：查重、登记、起线程一次性完成，SSE 重放生成器只写一份。

- `RunRegistry.start()`：查活动（或按资源键 find_conflict）→ 登记 → 补 start 帧 →
  起线程，全程同一把锁；build()/after_register() 在锁内运行，只放快速初始化与
  「必须先于 worker 的落库」（如逐字稿翻状态）。
- `event_stream(handle)`：事件日志从头重放，无进展 15s 发 ping，done 后补 end 帧
  （与旧 6 份实现逐行同款）。chat_runs.RunManager 因压缩互斥/审批等更复杂的持锁
  语义不迁注册表，只复用本生成器；knowledge 的导入任务是任务式注册表且已整段
  持锁，不迁。
- `sse_frame` 原驻 chat_runs，现移到这里作单一来源（chat_runs 再导出保持兼容）。
"""
import json
import threading
import time


def sse_frame(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def event_stream(handle):
    """SSE 生成器：从 handle 事件日志第 0 帧重放，断线重连从头重建进度态。"""

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


class RunRegistry:
    """进程内 run 注册表：默认「全局同时一个活动 run」，传 conflict 谓词可按资源键查重。

    conflict(handle) 命中即视为冲突：start 原样返回既有 run（reused=True）供前端续接，
    不执行 build——调用方据此决定「复用」还是「抛忙碌错」。
    """

    def __init__(self, capacity=100, keep_seconds=3600):
        self._runs = {}
        self._lock = threading.RLock()
        self._capacity = capacity
        self._keep_seconds = keep_seconds

    def get(self, run_id):
        return self._runs.get(run_id)

    def find(self, predicate):
        with self._lock:
            return next((handle for handle in self._runs.values() if predicate(handle)), None)

    def active(self):
        return self.find(lambda handle: not handle.done)

    def _sweep_locked(self):
        if len(self._runs) <= self._capacity:
            return
        for run_id, stale in list(self._runs.items()):
            if stale.done and not getattr(stale, 'pending_approval', False) \
                    and time.time() - stale.created > self._keep_seconds:
                self._runs.pop(run_id, None)

    def start(self, *, build, target, args=(), conflict=None,
              start_payload=None, after_register=None):
        """查重 → build → 登记 → after_register → push start 帧 → 起线程，同一把锁内完成。

        reused=True 时 build/after_register 不会执行，返回既有 handle；调用方要抛忙碌错
        就拿 reused 自行抛（interview/transcript 的语义）。target 的第一个实参是 handle，
        其后依次是 args。
        """
        with self._lock:
            if conflict is not None:
                existing = next((handle for handle in self._runs.values() if conflict(handle)), None)
            else:
                existing = next((handle for handle in self._runs.values() if not handle.done), None)
            if existing is not None:
                return existing, True
            handle = build()
            self._sweep_locked()
            self._runs[handle.id] = handle
            if after_register is not None:
                after_register(handle)
            if start_payload is not None:
                handle.push('start', start_payload(handle))
        thread = threading.Thread(target=target, args=(handle, *args), daemon=True)
        handle.thread = thread
        thread.start()
        return handle, False

    def stream(self, run_id):
        handle = self._runs.get(run_id)
        if handle is None:
            raise ValueError('运行不存在或已结束')
        return event_stream(handle)
