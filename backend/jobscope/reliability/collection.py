"""Shared signal collection and task persistence, independent of HTTP endpoints."""
import importlib

from ..repository import now
from .sources import SIGNAL_SOURCES, SignalSourceStore, contains_key


class SourceUnavailable(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def validate_source(store, source_id):
    """Validate before scheduling; revalidate at execution time to respect disable actions."""
    if source_id not in SIGNAL_SOURCES:
        raise SourceUnavailable('未登记的信号来源', 400)
    reason = store.unavailable_reason(source_id)
    if reason:
        raise SourceUnavailable(reason)


def collect_source(repo, data_dir, company, source_id, credit_code='', *, task=None):
    """Collect once, save canonical evidence/signals, and finalize exactly one task.

    Invalid manual requests raise before creating a task. A research task already queued
    when its source is disabled is finalized as skipped without calling the collector.
    Exceptions never copy SDK/HTTP messages, headers, or credentials into task records.
    """
    store = SignalSourceStore(repo)
    try:
        validate_source(store, source_id)
    except SourceUnavailable as exc:
        if task is None:
            raise
        task.update(status='skipped', message=str(exc), count=0, updated_at=now())
        repo.update_task(task)
        return dict(status=task['status'], message=task['message'], signals=[], task=task)

    if task is None:
        identity = repo.resolve_company(company, credit_code)
        task = repo.create_task('signal_' + source_id, identity['name'])
    else:
        identity = None
    saved = []
    status, message = 'failed', '信号采集失败，请检查来源配置与本地依赖后重试。'
    try:
        from . import collectors, local, paid
        from .service import persist_outcome
        identity = identity or repo.resolve_company(company, credit_code)
        spec = SIGNAL_SOURCES[source_id]
        from_local = spec['transport'] == 'local'
        from_paid = spec['transport'] == 'http_api' and spec['tier'] == 'paid'
        if from_local:
            outcome = local.collect(source_id, identity['name'], repo=repo, identity=identity)
        elif from_paid:
            outcome = paid.collect(source_id, identity['name'], client=store.client(source_id))
        else:
            browser_module = importlib.import_module('..browser', __package__) if source_id in collectors.FLOWS else None
            outcome = collectors.collect(source_id, identity['name'], browser_module=browser_module, data_dir=data_dir,
                                         repo=repo, identity=identity)
        key = store.stored(source_id).get('key', '') if from_paid else ''
        # Inspect nested strings before persistence, including credentials with JSON escapes.
        if key and contains_key(outcome, key):
            raise ValueError('unsafe source response')
        status = outcome['status']
        if status not in {'completed', 'partial', 'needs_login', 'needs_verification', 'backoff', 'unsupported', 'failed', 'skipped'}:
            raise ValueError('invalid collector status')
        message = str(outcome['message'])
        kind = local.DERIVED_KIND if from_local else (paid.PAID_KIND if from_paid else 'disclosure')
        # Keep successful records if one later signal cannot be saved; count only saved signals.
        for signal in outcome['signals']:
            saved.extend(persist_outcome(repo, identity, source_id, {**outcome, 'signals': [signal]}, kind))
    except ModuleNotFoundError:
        status, message = 'unsupported', '采集模块或可选依赖尚未安装，本轮未完成信号采集。'
    except Exception:
        status = 'partial' if saved else 'failed'
        message = ('部分信号已保存，其余信号采集或保存失败，请检查来源后重试。' if saved
                   else '信号采集失败，请检查来源配置与本地依赖后重试。')
    finally:
        task.update(status=status, message=message, count=len(saved), updated_at=now())
        repo.update_task(task)
    return dict(status=status, message=message, signals=saved, task=task)
