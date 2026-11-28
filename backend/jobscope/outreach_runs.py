"""触达话术的后台生成（2026-11-28 审计 P1-4：同步 LLM 端点迁 run+SSE）。

事件序列 start → done(version) → end；done 携带已落库的版本行（先落库再推送）。
不做取消与流式 delta：整条管线只有一次 60s 上限的抽取调用（AGENTS.md run 规范
第 6 条），且话术上限 150 字、模型不可用即规则兜底——流式展示没有增益。
一个岗位同时只允许一个活动 run（greeting/followup 共用闸门），重复发起原样返回。
"""
import logging
import uuid

from .chat_runs import RunHandle
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)


class OutreachManager:
    """In-process registry of outreach runs; one active run per job."""

    def __init__(self, repo):
        self.repo = repo
        self._registry = RunRegistry()

    def get(self, run_id):
        return self._registry.get(run_id)

    def start(self, job_id, kind):
        def build():
            handle = RunHandle(uuid.uuid4().hex, '', None)
            handle.job_id = job_id
            handle.error = None
            return handle

        return self._registry.start(
            build=build,
            conflict=lambda handle: getattr(handle, 'job_id', '') == job_id and not handle.done,
            start_payload=lambda handle: dict(run_id=handle.id, job_id=job_id, kind=kind),
            target=self._execute, args=(kind,))

    def _execute(self, handle, kind):
        from .outreach import generate
        try:
            version = generate(self.repo, handle.job_id, kind)
            handle.push('done', dict(version=version))
        except ValueError as exc:
            logger.warning('触达话术生成失败 job=%s：%s', handle.job_id, exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('触达话术生成异常 job=%s', handle.job_id)
            handle.error = '话术生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    def stream(self, run_id):
        return self._registry.stream(run_id)
