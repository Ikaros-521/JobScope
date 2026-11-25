"""Boss 投递记录定时同步（sync_schedule）：到点判定、设置存储、调度 tick、端点契约与线程启停。

全程 tmp_path 隔离，不碰浏览器。conftest 已设 JOBSCOPE_DISABLE_AUTO_SYNC=1：端点用例里打开开关
也不会起线程；调度逻辑用注入时钟 + 假执行器直接测。
"""
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope import boss_import, sync_schedule
from jobscope.main import create_app
from jobscope.repository import Repository

# 测试里的时刻一律用本地 naive：库里存的是不带时区的 ISO，_local 按本机时间读回，墙钟不变。
ANCHOR = datetime(2026, 11, 20, 9, 0)  # 周五 09:00，开启自动同步


def settings(enabled=True, period='1d', time='21:30', anchor=ANCHOR):
    return dict(enabled=enabled, period=period, time=time, anchor_at=anchor.isoformat() if anchor else '')


def state(**fields):
    return {**sync_schedule.STATE_DEFAULTS, **fields}


def attempted(moment):
    return state(last_attempt_at=moment.isoformat(), last_status='completed', last_message='同步完成')


# ---- 到点判定（纯函数）----------------------------------------------------------------

def test_parse_time_normalizes_and_rejects_bad_values():
    assert sync_schedule.parse_time('21:30') == (21, 30)
    assert sync_schedule.parse_time(' 9:05 ') == (9, 5)  # RequestModel 会去首尾空白，这里也容许
    for bad in ('24:00', '21:60', 'abc', '', '9', '21:3'):
        with pytest.raises(ValueError):
            sync_schedule.parse_time(bad)
    # 库里被手改成坏值：读回时退回默认，不让调度检查挂掉。
    assert sync_schedule.safe_time('25:99') == sync_schedule.DEFAULT_TIME
    assert sync_schedule.safe_time('9:05') == '09:05'


def test_daily_slot_runs_at_time_point_and_catches_up_once():
    # 周五 09:00 开启、每天 21:30：当天 21:30 到点；准点跑过后要等下一天。
    assert sync_schedule.upcoming(settings(), state(), datetime(2026, 11, 20, 21, 29)) == (
        datetime(2026, 11, 20, 21, 30).astimezone().isoformat(), False)
    assert sync_schedule.is_due(settings(), state(), datetime(2026, 11, 20, 21, 30)) is True
    ran_at = datetime(2026, 11, 20, 21, 30, 5)
    assert sync_schedule.is_due(settings(), attempted(ran_at), datetime(2026, 11, 20, 21, 35)) is False
    assert sync_schedule.upcoming(settings(), attempted(ran_at), datetime(2026, 11, 20, 21, 35))[0] == \
        datetime(2026, 11, 21, 21, 30).astimezone().isoformat()
    # 应用关了两天（周六、周日的槽位都错过）：周日上午恢复只补跑一次，之后回到网格。
    assert sync_schedule.is_due(settings(), attempted(ran_at), datetime(2026, 11, 22, 10, 0)) is True
    catch_up = datetime(2026, 11, 22, 10, 0, 3)
    assert sync_schedule.is_due(settings(), attempted(catch_up), datetime(2026, 11, 22, 10, 5)) is False
    assert sync_schedule.is_due(settings(), attempted(catch_up), datetime(2026, 11, 22, 21, 30)) is True


def test_enabling_after_todays_slot_waits_for_next_cycle():
    # 22:40 才开启（当天 21:30 已过）：不会一开就跑，首次落在明天 21:30。
    late = settings(anchor=datetime(2026, 11, 20, 22, 40))
    assert sync_schedule.is_due(late, state(), datetime(2026, 11, 20, 22, 41)) is False
    assert sync_schedule.upcoming(late, state(), datetime(2026, 11, 20, 22, 41))[0] == \
        datetime(2026, 11, 21, 21, 30).astimezone().isoformat()


def test_multi_day_grid_is_anchored_on_enable_day():
    # 周一 11-23 开启、每 3 天：槽位是 23 / 26 / 29 日。
    weekly = settings(period='3d', anchor=datetime(2026, 11, 23, 9, 0))
    assert sync_schedule.upcoming(weekly, state(), datetime(2026, 11, 23, 12, 0))[0] == \
        datetime(2026, 11, 23, 21, 30).astimezone().isoformat()  # 当天的槽位还没到，首跑就在今天
    # 周一准点跑过：周三不是槽位，要到周四 21:30。
    monday_run = attempted(datetime(2026, 11, 23, 21, 30, 5))
    assert sync_schedule.is_due(weekly, monday_run, datetime(2026, 11, 25, 21, 30)) is False
    assert sync_schedule.upcoming(weekly, monday_run, datetime(2026, 11, 25, 21, 30))[0] == \
        datetime(2026, 11, 26, 21, 30).astimezone().isoformat()
    assert sync_schedule.is_due(weekly, monday_run, datetime(2026, 11, 26, 21, 30)) is True


def test_hourly_period_counts_from_last_attempt():
    # 每 6 小时：距上次尝试（没有就是基准）满 6 小时即到点。
    six = settings(period='6h')
    assert sync_schedule.is_due(six, state(), datetime(2026, 11, 20, 14, 59)) is False
    assert sync_schedule.is_due(six, state(), datetime(2026, 11, 20, 15, 0)) is True
    ran_at = datetime(2026, 11, 20, 15, 2)
    assert sync_schedule.is_due(six, attempted(ran_at), datetime(2026, 11, 20, 21, 1)) is False
    assert sync_schedule.is_due(six, attempted(ran_at), datetime(2026, 11, 20, 21, 2)) is True


def test_disabled_or_anchorless_schedule_never_runs():
    assert sync_schedule.upcoming(settings(enabled=False), state(), datetime(2026, 11, 20, 21, 30)) == (None, False)
    assert sync_schedule.is_due(settings(anchor=None), state(), datetime(2026, 11, 20, 21, 30)) is False


def test_busy_defer_window_blocks_execution_but_not_the_displayed_state():
    # 浏览器正忙后的 5 分钟内不执行；界面仍显示「已到时间」，窗口过后照常到点。
    now = datetime(2026, 11, 20, 21, 30)
    deferred = state(defer_until=(now + timedelta(minutes=4)).isoformat(), busy_retries=1)
    assert sync_schedule.is_due(settings(), deferred, now + timedelta(minutes=1)) is False
    assert sync_schedule.upcoming(settings(), deferred, now + timedelta(minutes=1))[1] is True
    assert sync_schedule.is_due(settings(), deferred, now + timedelta(minutes=5)) is True


# ---- 设置与运行状态 -------------------------------------------------------------------

def test_store_defaults_save_and_sanitizes_hand_edited_rows(tmp_path):
    repo = Repository(tmp_path)
    store = sync_schedule.SyncScheduleStore(repo)
    assert store.settings() == dict(enabled=False, period='1d', time='21:30', anchor_at='')
    store.save(sync_schedule.AppliedSyncSettingsInput(enabled=True, period='2d', time='9:05'))
    saved = store.settings()
    assert (saved['enabled'], saved['period'], saved['time']) == (True, '2d', '09:05')
    assert saved['anchor_at']  # 保存即记基准
    with pytest.raises(ValueError):
        store.save(sync_schedule.AppliedSyncSettingsInput(enabled=True, time='25:00'))
    # 库里的行被手改成坏值：读回默认值，而不是让调度检查抛异常。
    sync_schedule.SyncScheduleStore._write(repo, 'settings', dict(enabled=True, period='5d', time='99:99', anchor_at='x'))
    assert store.settings() == dict(enabled=True, period='1d', time='21:30', anchor_at='x')


def test_store_record_defers_busy_retries_then_gives_up(tmp_path):
    repo = Repository(tmp_path)
    store = sync_schedule.SyncScheduleStore(repo)
    started = datetime(2026, 11, 20, 21, 30).astimezone()
    busy = {'status': 'backoff', 'message': '浏览器任务正在运行，请等待完成。'}
    store.record(busy, started)
    store.record(busy, started + timedelta(minutes=6))
    deferred = store.state()
    assert deferred['last_attempt_at'] == '' and deferred['busy_retries'] == 2  # 忙不算尝试
    assert deferred['defer_until']  # 延后重试
    # 第 3 次仍忙：放弃本周期，记为一次尝试（下个周期才会再试）。
    store.record(busy, started + timedelta(minutes=12))
    gave_up = store.state()
    assert gave_up['last_status'] == 'backoff' and '本周期已跳过' in gave_up['last_message']
    assert gave_up['busy_retries'] == 0 and gave_up['defer_until'] == ''
    store.record({'status': 'completed', 'message': '共 6 条'}, started + timedelta(days=1))
    view = store.view()
    assert view['last']['status'] == 'completed' and view['last']['message'] == '共 6 条'
    assert [item['value'] for item in view['periods']] == [value for value, _ in sync_schedule.PERIODS]
    assert view['running'] is False


# ---- 调度线程 ---------------------------------------------------------------------------

class FakeScheduler(sync_schedule.SyncScheduler):
    """不碰线程，只测 tick：按顺序弹出预设结果并计数。"""

    def __init__(self, store, outcomes=()):
        self.outcomes, self.calls = list(outcomes), 0
        super().__init__(store, self._next)

    def _next(self):
        self.calls += 1
        return self.outcomes.pop(0)


def scheduled(path, **fields):
    """独立数据目录 + 预置设置；同一用例里要两套状态时各建各的目录，别共用一个库。"""
    store = sync_schedule.SyncScheduleStore(Repository(path))
    sync_schedule.SyncScheduleStore._write(store.repo, 'settings', settings(**fields))
    return store


def test_tick_runs_once_when_due_and_records_the_outcome(tmp_path):
    store = scheduled(tmp_path)
    scheduler = FakeScheduler(store, [{'status': 'completed', 'message': '共 6 条'}])
    now = datetime(2026, 11, 20, 21, 30).astimezone()
    assert scheduler.tick(now)['status'] == 'completed' and scheduler.calls == 1
    assert store.state()['last_status'] == 'completed' and store.state()['last_message'] == '共 6 条'
    # 同一个槽位不重复跑；下一天的时间点才再跑。
    assert scheduler.tick(now + timedelta(minutes=1)) is None and scheduler.calls == 1
    assert scheduler.tick(now + timedelta(days=1)) is not None and scheduler.calls == 2


def test_tick_never_runs_when_disabled_or_before_the_slot(tmp_path):
    off = scheduled(tmp_path, enabled=False)
    assert FakeScheduler(off, []).tick(datetime(2026, 11, 20, 21, 30).astimezone()) is None
    early = scheduled(tmp_path)
    assert FakeScheduler(early, []).tick(datetime(2026, 11, 20, 21, 29).astimezone()) is None


def test_tick_records_failures_and_turns_exceptions_into_failed(tmp_path):
    store = scheduled(tmp_path)
    scheduler = FakeScheduler(store, [{'status': 'needs_login', 'message': '请在项目浏览器完成登录'}])
    assert scheduler.tick(datetime(2026, 11, 20, 21, 30).astimezone())['status'] == 'needs_login'
    assert store.state()['last_status'] == 'needs_login'  # 需要登录也算一次尝试：不反复弹窗

    def explode():
        raise RuntimeError('boom')

    store2 = scheduled(tmp_path / 'explode')
    scheduler2 = sync_schedule.SyncScheduler(store2, explode)
    outcome = scheduler2.tick(datetime(2026, 11, 20, 21, 30).astimezone())
    assert outcome['status'] == 'failed' and store2.state()['last_status'] == 'failed'


def test_tick_busy_retries_are_bounded_and_never_overlap(tmp_path):
    store = scheduled(tmp_path)
    busy = {'status': 'backoff', 'message': '浏览器任务正在运行，请等待完成。'}
    scheduler = sync_schedule.SyncScheduler(store, lambda: busy)
    start = datetime(2026, 11, 20, 21, 30).astimezone()
    assert scheduler.tick(start)['status'] == 'backoff' and store.state()['last_attempt_at'] == ''
    assert scheduler.tick(start + timedelta(minutes=1)) is None  # 5 分钟延后窗口内不再试
    assert scheduler.tick(start + timedelta(minutes=6)) is not None
    assert scheduler.tick(start + timedelta(minutes=12)) is not None
    assert store.state()['last_status'] == 'backoff'  # 连续 3 次仍忙：放弃本周期
    assert scheduler.tick(start + timedelta(minutes=20)) is None
    # 同步进行中再次到点检查：不叠加第二个同步。
    overlap = scheduled(tmp_path / 'overlap')
    seen = []
    scheduler2 = sync_schedule.SyncScheduler(overlap, lambda: seen.append(scheduler2.tick(start)) or {'status': 'completed', 'message': 'ok'})
    scheduler2.tick(start)
    assert seen == [None] and overlap.state()['last_status'] == 'completed'


# ---- 同步动作胶水与端点 ------------------------------------------------------------------

def test_run_sync_leaves_a_task_record(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    monkeypatch.setattr(boss_import, 'import_applied', lambda repo, task, data_dir: {'status': 'completed', 'message': '共 6 条'})
    assert sync_schedule.run_sync(repo, tmp_path)['status'] == 'completed'
    task = repo.tasks()[0]
    assert (task['query'], task['status'], task['message'], task['kind']) == ('Boss 投递记录自动同步', 'completed', '共 6 条', 'boss_import')

    def explode(repo, task, data_dir):
        raise RuntimeError('boom')

    monkeypatch.setattr(boss_import, 'import_applied', explode)
    assert sync_schedule.run_sync(repo, tmp_path)['status'] == 'failed'
    assert repo.tasks()[0]['status'] == 'failed'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_applied_sync_settings_round_trip(client):
    before = client.get('/api/settings/applied-sync').json()
    assert before['enabled'] is False and before['next_run_at'] is None and before['last'] is None
    assert [item['label'] for item in before['periods']][:3] == ['每 6 小时', '每 12 小时', '每天']
    # 每 6 小时：保证用例运行期间不会到点。
    saved = client.put('/api/settings/applied-sync', json={'enabled': True, 'period': '6h'}).json()
    assert saved['enabled'] is True and saved['period'] == '6h' and saved['time'] == '21:30'
    assert saved['next_run_at'] and saved['overdue'] is False and saved['running'] is False
    assert client.get('/api/settings/applied-sync').json()['period'] == '6h'
    assert client.put('/api/settings/applied-sync', json={'enabled': True, 'time': '25:00'}).status_code == 422
    assert client.put('/api/settings/applied-sync', json={'enabled': True, 'period': '5d'}).status_code == 422
    client.put('/api/settings/applied-sync', json={'enabled': False})
    assert client.get('/api/settings/applied-sync').json()['enabled'] is False


def test_scheduler_thread_follows_app_lifecycle(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    monkeypatch.delenv(sync_schedule.DISABLE_ENV, raising=False)  # 本用例专门验证真实启停
    app = create_app(tmp_path)
    with TestClient(app):
        assert [thread.name for thread in threading.enumerate() if thread.name == 'boss-applied-sync'] == ['boss-applied-sync']
    assert not [thread for thread in threading.enumerate() if thread.name == 'boss-applied-sync']


def test_scheduler_thread_stays_off_behind_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    monkeypatch.setenv(sync_schedule.DISABLE_ENV, '1')
    with TestClient(create_app(tmp_path)) as client:
        assert not [thread for thread in threading.enumerate() if thread.name == 'boss-applied-sync']
        assert client.get('/api/settings/applied-sync').json()['enabled'] is False  # 端点照常可用
