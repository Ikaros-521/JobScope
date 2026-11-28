"""空窗期叙事的后台生成（2026-11-28 审计 P1-4：同步 LLM 端点迁 run+SSE）。

事件序列 start → done(narrative) → end；done 携带已落库的叙事行（先落库再推送）。
不做取消与流式 delta：整条管线只有一次 180s 上限的模型调用（AGENTS.md run 规范
第 6 条），模型不可用即规则骨架兜底——generate 的兜底语义原样保留在 worker 内。
全局同时只允许一个活动 run（叙事是全局单例资源），重复发起原样返回。
"""
import logging
import uuid

from .chat_runs import RunHandle
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)


class GapNarrativeManager:
    """In-process registry of gap-narrative runs; one active run at a time."""

    def __init__(self, repo):
        self.repo = repo
        self._registry = RunRegistry(capacity=20)

    def get(self, run_id):
        return self._registry.get(run_id)

    def start(self, payload):
        def build():
            handle = RunHandle(uuid.uuid4().hex, '', None)
            handle.error = None
            return handle

        return self._registry.start(
            build=build,
            start_payload=lambda handle: dict(run_id=handle.id),
            target=self._execute, args=(payload,))

    def _execute(self, handle, payload):
        from .gap_narrative import generate
        try:
            narrative = generate(self.repo, job_id=payload.get('job_id', ''),
                                 jd_text=payload.get('jd_text', ''), job_title=payload.get('job_title', ''))
            handle.push('done', dict(narrative=narrative))
        except ValueError as exc:
            logger.warning('空窗期叙事生成失败：%s', exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('空窗期叙事生成异常')
            handle.error = '叙事生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    def stream(self, run_id):
        return self._registry.stream(run_id)
