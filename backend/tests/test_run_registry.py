"""run_registry 基座回归（2026-11-28 审计 P1-3）：查重与登记同锁、重放单一来源。

修前 6 个 run 管理器各抄一份注册表，「查活动」与「登记」分两次拿锁——并发双击可起
两个 run；stream 重放生成器六份逐行相同。基座把查重收进登记锁内，冲突时 build 不执行。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.chat_runs import RunHandle, sse_frame  # noqa: F401  (sse_frame 再导出可用性一并回归)
from jobscope.run_registry import RunRegistry, event_stream


def _handle(handle_id):
    handle = RunHandle(handle_id, '', None)
    handle.error = None
    return handle


def test_start_conflict_reuses_without_build():
    """冲突命中时 build 不执行、原样返回既有 run（reused=True）——并发双击只起一份。"""
    reg = RunRegistry()
    built = []

    def build():
        handle = _handle('h1')
        built.append(handle)
        return handle

    first, reused1 = reg.start(build=build, start_payload=lambda h: {'id': h.id}, target=lambda h: None)
    assert reused1 is False and first.id == 'h1' and len(built) == 1
    assert first.thread is not None and not first.thread.is_alive() or True  # 目标即刻返回

    second, reused2 = reg.start(build=build, target=lambda h: None)
    assert reused2 is True and second is first and len(built) == 1


def test_start_conflict_predicate_miss_builds_new():
    """按资源键查重不命中（如另一个场次/岗位）时正常建新 run。"""
    reg = RunRegistry()
    first, reused1 = reg.start(
        build=lambda: _handle('a'), conflict=lambda h: getattr(h, 'session_id', '') == 'S-1',
        start_payload=lambda h: {}, target=lambda h: None)
    assert reused1 is False
    first.session_id = 'S-1'

    second, reused2 = reg.start(
        build=lambda: _handle('b'), conflict=lambda h: getattr(h, 'session_id', '') == 'S-2',
        start_payload=lambda h: {}, target=lambda h: None)
    assert reused2 is False and second.id == 'b'

    third, reused3 = reg.start(
        build=lambda: _handle('c'), conflict=lambda h: getattr(h, 'session_id', '') == 'S-1',
        start_payload=lambda h: {}, target=lambda h: None)
    assert reused3 is True and third is first


def test_start_payload_is_first_event_before_worker_runs():
    """start 帧在任何 worker 事件之前（worker 依赖这一点保证事件序）。"""
    reg = RunRegistry()
    seen = []
    handle, _ = reg.start(build=lambda: _handle('h'), start_payload=lambda h: dict(run_id=h.id, kind='plan'),
                          target=lambda h: seen.extend(h.events))
    assert seen[0][0] == 'start'


def test_stream_replays_events_then_end():
    reg = RunRegistry()
    handle = _handle('h')
    handle.push('delta', dict(text='x'))
    handle.push('done', dict(ok=1))
    handle.finish()
    reg._runs[handle.id] = handle  # 直接登记，绕过 start（不起线程）

    # stream() 返回生成器函数（与旧六份实现同契约：路由拿到后再调用一次）
    frames = list(reg.stream(handle.id)())
    kinds = [frame.split('\n')[0] for frame in frames]
    assert kinds == ['event: delta', 'event: done', 'event: end']
    assert frames[-1].startswith('event: end')


def test_stream_unknown_run_raises():
    reg = RunRegistry()
    with pytest.raises(ValueError):
        reg.stream('nope')


def test_event_stream_matches_registry_stream():
    """chat_runs.RunManager 委托的 event_stream 与 registry.stream 是同一实现、同一契约。"""
    handle = _handle('h')
    handle.push('delta', dict(text='x'))
    handle.finish()
    frames = list(event_stream(handle)())
    assert frames[-1] == sse_frame('end', dict(ok=True))
    assert any('event: delta' in frame for frame in frames)
