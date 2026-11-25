"""Boss 投递记录定时同步：设置存取 + 到点判定 + 常驻调度线程 + 端点与启停钩子。

同步动作本身在 boss_import.import_applied——人工入口（收藏页 / 导入面板的按钮）与定时入口共用同一条
通路（任务记录、状态合并规则、防污染闸完全一致）；本模块只管「什么时候跑」。
设计见 docs/design/boss-import.md §11。

调度语义（按本机本地时间）：
- 周期两类：按小时（6h / 12h，距上次尝试满 N 小时即到点）；按天（1d / 2d / 3d / 7d，落在固定时间点）。
- 按天的到点 = 时间网格上有一个「晚于上次尝试、且不晚于现在」的槽位。应用没开的那些天不会补跑多次——
  恢复后的第一次检查只跑一次，之后回到网格；睡眠 / 关机错过的槽位在恢复后补跑一次。
- 保存设置（含开启）把基准时刻记为现在：开启、改周期、改时间点都不会立刻拉起浏览器，首次运行落在下一个槽位。
- 一次尝试占一个周期：成功、需要登录、失败都算，下个周期才会再试——登录失效的窗口留给用户处理，
  不反复弹。唯一例外是「浏览器正忙」：不算尝试，5 分钟后重试，连续 3 次仍忙则放弃本周期。
- 应用不在运行时不会同步（线程随进程生死）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, time as day_time
from typing import Literal

from fastapi import HTTPException

from .models import RequestModel
from .repository import now

logger = logging.getLogger(__name__)

PERIODS = (('6h', '每 6 小时'), ('12h', '每 12 小时'), ('1d', '每天'), ('2d', '每 2 天'), ('3d', '每 3 天'), ('7d', '每周'))
DEFAULT_PERIOD, DEFAULT_TIME = '1d', '21:30'
# 测试环境总闸：pytest 里任何用例都不该有调度线程在跑（哪怕设置被打开，也不能真去拉浏览器）。
DISABLE_ENV = 'JOBSCOPE_DISABLE_AUTO_SYNC'
RETRY_SECONDS = 300   # 浏览器正忙时的重试间隔
MAX_BUSY_RETRIES = 3  # 连续忙这么多次就放弃本周期
TIME_PATTERN = re.compile(r'^(\d{1,2}):(\d{2})$')
STATE_DEFAULTS = dict(last_attempt_at='', last_status='', last_message='', defer_until='', busy_retries=0)


class AppliedSyncSettingsInput(RequestModel):
    """定时同步设置：period 见 PERIODS；time 是按天周期的时间点（HH:MM，本机本地时间）。"""
    enabled: bool
    period: Literal['6h', '12h', '1d', '2d', '3d', '7d'] = DEFAULT_PERIOD
    time: str = DEFAULT_TIME


# ---- 到点判定（纯函数，本地 naive 时间）------------------------------------------------

def parse_time(value):
    """'HH:MM' / 'H:MM' → (hour, minute)；非法抛 ValueError（保存时转 422）。"""
    match = TIME_PATTERN.match(str(value or '').strip())
    if not match:
        raise ValueError('时间点格式应为 HH:MM，例如 21:30')
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError('时间点应在 00:00–23:59 之间')
    return hour, minute


def safe_time(value):
    """读回设置时用：库里被手改成坏值就退回默认，不让一次调度检查因为时间点非法而挂掉。"""
    try:
        hour, minute = parse_time(value)
        return f'{hour:02d}:{minute:02d}'
    except ValueError:
        return DEFAULT_TIME


def _local(value):
    """库里的 ISO 时刻（UTC）→ 本地 naive；空值 / 坏值返回 None。"""
    try:
        return datetime.fromisoformat(str(value or '')).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def is_hourly(period):
    return period.endswith('h')


def next_slot(period, hhmm, anchor, reference):
    """按天周期在网格上 ≥ reference 且 ≥ anchor 的最早槽位（本地 naive）。

    anchor 是开启 / 改设置的那一刻：开启当天的时间点若已过，就从下个周期起算，不会一开就跑。
    """
    days = int(period[:-1])
    hour, minute = parse_time(hhmm)
    index = (reference.date() - anchor.date()).days // days  # 向下取整：reference 早于 anchor 时也为负
    for _ in range(3):
        candidate = datetime.combine(anchor.date() + timedelta(days=index * days), day_time(hour, minute))
        if candidate >= reference and candidate >= anchor:
            return candidate
        index += 1
    return candidate


def pending_slot(period, hhmm, anchor, last_attempt):
    """晚于上次尝试的最早槽位 = 还没被处理过的那一个（没有尝试记录时就是基准之后的第一个槽位）。"""
    reference = last_attempt or anchor
    slot = next_slot(period, hhmm, anchor, reference)
    if slot == reference:
        # 上次尝试恰好落在槽位上（准点跑了）：这个槽位已处理，看下一个。
        slot = next_slot(period, hhmm, anchor, reference + timedelta(seconds=1))
    return slot


def _boundary(settings, anchor, last_attempt):
    """下一次该跑的时刻（本地 naive）：按小时 = 距上次尝试（无则基准）满 N 小时；按天 = 未处理的最早槽位。"""
    if is_hourly(settings['period']):
        return (last_attempt or anchor) + timedelta(hours=int(settings['period'][:-1]))
    return pending_slot(settings['period'], settings['time'], anchor, last_attempt)


def upcoming(settings, state, now=None):
    """(下一次该跑的本地 ISO 时刻, 是否已到时间)。未开启或没有基准时返回 (None, False)。"""
    now = now or datetime.now()
    anchor = _local(settings.get('anchor_at'))
    if not settings.get('enabled') or anchor is None:
        return None, False
    target = _boundary(settings, anchor, _local(state.get('last_attempt_at')))
    return target.astimezone().isoformat(), target <= now


def is_due(settings, state, now=None):
    """是否该现在跑：已到时间，且不在「浏览器正忙」的延后重试窗口内。"""
    if not upcoming(settings, state, now)[1]:
        return False
    defer = _local(state.get('defer_until'))
    return not (defer and (now or datetime.now()) < defer)


# ---- 设置与运行状态 ---------------------------------------------------------------------

class SyncScheduleStore:
    """定时同步的设置与运行状态：一张键值表两个键——settings 用户可改，state 只由调度器写。"""

    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS sync_schedule(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    def _read(self, key):
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload FROM sync_schedule WHERE key=?', (key,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write(repo, key, payload):
        with repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO sync_schedule VALUES (?,?)', (key, json.dumps(payload, ensure_ascii=False)))

    def settings(self):
        saved = self._read('settings')
        return dict(enabled=bool(saved.get('enabled')),
                    period=saved.get('period') if saved.get('period') in dict(PERIODS) else DEFAULT_PERIOD,
                    time=safe_time(saved.get('time')), anchor_at=saved.get('anchor_at') or '')

    def state(self):
        return {**STATE_DEFAULTS, **{key: value for key, value in self._read('state').items() if key in STATE_DEFAULTS}}

    def save(self, data):
        """保存即重排：基准记为现在、清掉延后重试（新周期从干净状态起算）；上次尝试的历史保留。"""
        hour, minute = parse_time(data.time)
        self._write(self.repo, 'settings', dict(enabled=bool(data.enabled), period=data.period,
                                                time=f'{hour:02d}:{minute:02d}', anchor_at=now()))
        self._write(self.repo, 'state', {**self.state(), 'defer_until': '', 'busy_retries': 0})

    def record(self, outcome, started_at):
        """一次尝试的结果落状态。backoff 不算尝试：延后重试，连续 MAX_BUSY_RETRIES 次才放弃本周期。"""
        state = self.state()
        if outcome.get('status') == 'backoff' and state['busy_retries'] + 1 < MAX_BUSY_RETRIES:
            state.update(defer_until=(started_at + timedelta(seconds=RETRY_SECONDS)).isoformat(),
                         busy_retries=state['busy_retries'] + 1)
        else:
            gave_up = outcome.get('status') == 'backoff'
            state.update(last_attempt_at=started_at.isoformat(), defer_until='', busy_retries=0,
                         last_status='backoff' if gave_up else str(outcome.get('status') or 'failed'),
                         last_message='浏览器正忙，本周期已跳过；下个周期再试' if gave_up
                         else str(outcome.get('message') or ''))
        self._write(self.repo, 'state', state)

    def view(self, running=False, now=None):
        settings, state = self.settings(), self.state()
        target, overdue = upcoming(settings, state, now)
        return {**{key: settings[key] for key in ('enabled', 'period', 'time')},
                'periods': [dict(value=value, label=label, hourly=is_hourly(value)) for value, label in PERIODS],
                'next_run_at': target, 'overdue': overdue, 'running': bool(running),
                'last': dict(at=state['last_attempt_at'], status=state['last_status'], message=state['last_message'])
                if state['last_attempt_at'] else None}


# ---- 调度线程 ---------------------------------------------------------------------------

def run_sync(repo, data_dir):
    """定时入口与人工按钮走同一条同步通路，同样留任务记录（任务记录里能查到每次自动同步的结果）。"""
    from . import boss_import  # 延迟导入：浏览器可选依赖缺失时只影响执行，不影响设置读写
    task = repo.create_task('boss', 'Boss 投递记录自动同步')
    try:
        outcome = boss_import.import_applied(repo, task, data_dir=data_dir)
    except Exception:
        logger.exception('Boss 投递记录自动同步中断')
        outcome = {'status': 'failed', 'message': '投递记录自动同步中断；库里已回填的记录不受影响。'}
    task.update(kind='boss_import', status=outcome['status'], message=outcome['message'])
    repo.update_task(task)
    return outcome


class SyncScheduler:
    """常驻线程：每 interval 秒醒一次，到点且没在跑就执行一次 runner。

    同步在调度线程里原地执行（单次同步自带上限：每页等待 15s、最多 10 页），所以同一时刻至多一个同步在跑；
    关闭应用时最多等 join_timeout，等不到就随守护线程一起结束（落库按条事务，不留半截状态）。
    """

    def __init__(self, store, runner, interval=60, first_delay=60, join_timeout=5):
        self.store, self.runner = store, runner
        self.interval, self.first_delay, self.join_timeout = interval, first_delay, join_timeout
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._running = False

    @property
    def running(self):
        return self._running

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name='boss-applied-sync', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.join_timeout)

    def _loop(self):
        # 启动后先等一个周期再检查：应用刚起来时让浏览器 / 采集等先安顿，也避免与启动期的其他任务抢浏览器。
        if self.stop_event.wait(self.first_delay):
            return
        while not self.stop_event.wait(self.interval):
            try:
                self.tick()
            except Exception:
                logger.exception('定时同步的到点检查异常')  # 检查本身出错不能拖垮线程

    def tick(self, now=None):
        """到点检查一次；跑了返回结果，没跑返回 None。now 为 aware datetime（UTC），测试注入用。"""
        now = now or datetime.now().astimezone()
        if self._running or not is_due(self.store.settings(), self.store.state(), now.astimezone().replace(tzinfo=None)):
            return None
        self._running = True
        try:
            outcome = self.runner()
        except Exception:
            logger.exception('定时同步执行异常')
            outcome = {'status': 'failed', 'message': '投递记录自动同步中断；库里已回填的记录不受影响。'}
        finally:
            self._running = False
        try:
            self.store.record(outcome, started_at=now)
        except Exception:
            logger.exception('定时同步结果落库失败')
        logger.info('定时同步结束：status=%s message=%s', outcome.get('status'), str(outcome.get('message', ''))[:80])
        return outcome


# ---- 应用挂载 ---------------------------------------------------------------------------

def register(app, repo, data_dir):
    """把定时同步挂到应用上：读写端点 + 随应用启停的调度线程。main.py 只留一行调用。"""
    store = SyncScheduleStore(repo)
    # 测试环境总闸：设了就不起线程（端点照常可用，用例可以放心改设置）。
    scheduler = None if os.environ.get(DISABLE_ENV) == '1' else SyncScheduler(store, lambda: run_sync(repo, data_dir))

    def view():
        return store.view(running=bool(scheduler and scheduler.running))

    @app.get('/api/settings/applied-sync')
    def read_applied_sync():
        return view()

    @app.put('/api/settings/applied-sync')
    def save_applied_sync(data: AppliedSyncSettingsInput):
        try:
            store.save(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return view()

    if scheduler is None:
        return
    app.on_event('startup')(scheduler.start)
    app.on_event('shutdown')(scheduler.stop)
