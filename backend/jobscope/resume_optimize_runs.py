"""简历定向优化的后台生成（2026-11-28 审计 P1-4：同步 LLM 端点迁 run+SSE）。

事件序列 start → done(version) → end；done 携带已落库的版本行（先落库再推送，
含「评估用」自动接管）。不做取消与流式 delta：整条管线只有一次 180s 上限的
模型调用（AGENTS.md run 规范第 6 条），失败时优化_resume 的 ValueError 文案
原样转 error 帧给前端表单。全局同时只允许一个活动 run（版本列表与 pin 是
共享资源），重复发起原样返回。
"""
import logging
import uuid

from .chat_runs import RunHandle
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)


class ResumeOptimizeManager:
    """In-process registry of resume-optimize runs; one active run at a time."""

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
        from .resume_workshop import optimize_and_save
        try:
            version = optimize_and_save(self.repo, payload)
            handle.push('done', dict(version=version))
        except ValueError as exc:
            logger.warning('简历定向优化失败：%s', exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('简历定向优化异常')
            handle.error = '简历优化失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    def stream(self, run_id):
        return self._registry.stream(run_id)
