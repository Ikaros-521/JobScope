"""会话上下文压缩的后台 run（2026-11-28 审计 P1-4：同步端点迁 run+SSE）。

摘要的常量与分段/合并实现自 main.py 原样迁入（口径逐行不变）；事件序列
start → done(result) → end，done 携带 repo.compress_conversation 的落库结果。
不做取消与流式 delta：摘要由若干次 120s 上限的 complete 调用组成，中断只会
整轮作废（repo 层 expected_ids 闸兜住历史漂移），分段流式对用户没有增益。

压缩锁跨 worker 生命周期：start() 里 begin_compression（顺带挡同会话活动
chat run），worker finally 里 end_compression——并发双压被锁挡在入口。
"""
import logging
import uuid

from .chat_runs import RunHandle
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = ('你是求职情报工作台的对话上下文压缩引擎，把早期对话提炼为高密度结构化摘要。'
                  '要求：1.保留用户意图、涉及的公司/岗位/薪资等关键事实与已得出的结论；'
                  '2.保留出现过的证据ID（形如 E-xxx）、来源链接与未完成的待办事项；'
                  '3.去除寒暄与冗余；4.用中文 Markdown 输出，以「### 📋 上下文压缩摘要」作为标题开头。')
# 单次摘要请求的字符上限（约 4–5k tokens）；超过就分段提炼再合并，绝不截断原文。
SUMMARY_CHUNK_CHARS = 16000
# 整段历史的硬上限：超过时明确拒绝，而不是悄悄丢掉后面的内容。
SUMMARY_MAX_CHARS = 240000


def _summary_chunks(messages):
    chunks, current, size = [], [], 0
    for message in messages:
        text = f"【{'用户' if message.get('role') == 'user' else '助手'}】{message.get('content', '')}"
        if current and size + len(text) > SUMMARY_CHUNK_CHARS:
            chunks.append('\n\n'.join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def summarise_messages(config, messages):
    """Summarise every message; long histories are summarised in chunks and merged (no truncation)."""
    from .model_transport import complete
    chunks = _summary_chunks(messages)
    if not chunks:
        raise ValueError('没有可压缩的消息')
    if sum(len(chunk) for chunk in chunks) > SUMMARY_MAX_CHARS:
        raise OverflowError
    if len(chunks) == 1:
        summary, _ = complete(config, SUMMARY_SYSTEM, '请压缩以下历史对话：\n\n' + chunks[0],
                              temperature=0.3, max_tokens=2048, timeout=120)
        return summary.strip()
    partials = []
    for index, chunk in enumerate(chunks, 1):
        part, _ = complete(config, SUMMARY_SYSTEM, f'这是同一段历史对话的第 {index}/{len(chunks)} 段，先提炼本段：\n\n{chunk}',
                           temperature=0.3, max_tokens=2048, timeout=120)
        partials.append(f'### 第 {index} 段\n{part.strip()}')
    merged, _ = complete(config, SUMMARY_SYSTEM,
                         '以下是同一段历史对话的分段摘要，请合并为一份不重复、不丢失关键事实的总摘要：\n\n' + '\n\n'.join(partials),
                         temperature=0.3, max_tokens=2048, timeout=120)
    return merged.strip()


class CompressManager:
    """In-process registry of compression runs; one per conversation."""

    def __init__(self, repo, chat_runs):
        self.repo = repo
        self.chat_runs = chat_runs
        self._registry = RunRegistry(capacity=20)

    def get(self, run_id):
        return self._registry.get(run_id)

    def start(self, conversation_id):
        # begin_compression 同时挡「正在生成」与「正在压缩」两种冲突（409 语义在路由转）；
        # 注册后起线程前任何异常都要把锁还回去，避免压缩闸永久关闭。
        if not self.chat_runs.begin_compression(conversation_id):
            raise ValueError('该会话正在生成或压缩上下文，请等待完成或先停止')
        try:
            def build():
                handle = RunHandle(uuid.uuid4().hex, '', None)
                handle.conversation_id = conversation_id
                handle.error = None
                return handle

            handle, reused = self._registry.start(
                build=build,
                conflict=lambda handle: getattr(handle, 'conversation_id', '') == conversation_id and not handle.done,
                start_payload=lambda handle: dict(run_id=handle.id, conversation_id=conversation_id),
                target=self._execute, args=(conversation_id,))
        except Exception:
            self.chat_runs.end_compression(conversation_id)
            raise
        if reused:
            self.chat_runs.end_compression(conversation_id)
        return handle, reused

    def _execute(self, handle, conversation_id):
        try:
            try:
                conversation = self.repo.get_conversation(conversation_id)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
            try:
                config = self.chat_runs.resolve_llm_config(conversation)['llm']
            except ValueError as exc:
                raise ValueError(str(exc)) from None
            if config.get('mode') != 'remote' or not config.get('model'):
                raise ValueError('压缩上下文需要先配置对话模型')
            messages = conversation['messages']
            keep_recent = 4
            boundary = len(messages) - keep_recent
            foldable = [m for m in messages[:boundary] if not m.get('compacted')]
            if boundary <= 0 or len(foldable) < 2:
                raise ValueError('历史消息较少，暂无需压缩')
            try:
                summary = summarise_messages(config, foldable)
            except OverflowError:
                raise ValueError('历史记录过长，单次压缩会超出模型额度；请先删除部分较早消息后重试') from None
            except Exception:
                logger.exception('上下文压缩摘要生成失败 conv=%s', conversation_id)
                raise ValueError('摘要生成失败，请检查模型服务后重试；历史未被修改') from None
            if not summary.strip():
                raise ValueError('模型返回了空摘要，历史未被修改')
            result = self.repo.compress_conversation(conversation_id, boundary, summary.strip(),
                                                     expected_ids=[m.get('id') for m in messages])
            handle.push('done', dict(result=result))
        except ValueError as exc:
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('上下文压缩异常 conv=%s', conversation_id)
            handle.error = '上下文压缩失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            self.chat_runs.end_compression(conversation_id)
            handle.finish()  # 压缩闸与 run 收尾都要执行（漏 finish 会永久「压缩中」，2026-11-28）

    def stream(self, run_id):
        return self._registry.stream(run_id)
