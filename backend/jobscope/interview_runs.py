"""模拟面试的运行管理：出题/对话/复盘三类后台 run 共用一个注册表，一场次同时一个。

与 analyze_runs 同一套模式：RunHandle 事件日志 + SSE 重放（断线重连从头重建状态），
取消是合作式——流式对话在 chunk 间退出（register_abort 关底层连接即时打断），
复盘的逐题调用在题间边界退出。事件序列 start → delta/reasoning_delta… → done|error|cancelled → end；
done 携带完整场次快照（worker 先落库再推送）。
"""
import json
import logging
import threading
import time
import uuid

from .chat_runs import RunHandle, sse_frame
from .interview import ADVANCE_TOOL, build_interviewer_system, build_persona, build_question_plan, generate_report
from .repository import now
from .semantic import RunCancelled
from .model_transport import stream_turn
from .settings import ModelSettings, use_config

logger = logging.getLogger(__name__)

INTERVIEW_SCOPE_PREFIX = 'interview'


def interview_scope(session_id):
    """面试对话挂在独立 scope 下，不混入岗位/公司对话列表。"""
    return dict(kind='interview', session_id=session_id)


def new_session(*, persona_id, round_id, intensity, question_count, material,
                company_style='generic', job_title='', company_name=''):
    """建场次：画像编译成三段式快照存进 payload（预设后续升级不影响已开场次）。

    岗位锚点优先级：材料（岗位库/粘贴JD）> 入参 job_title > 画像岗位类型。
    """
    persona = build_persona(persona_id, round_id, intensity, company_style=company_style,
                            job_title=job_title or material.get('job_title', ''),
                            company_name=company_name or material.get('company', ''))
    session_id = 'I-' + uuid.uuid4().hex
    label = ' · '.join(part for part in (material.get('company') or persona['company_name'],
                                         material.get('job_title') or persona['job_title']) if part)
    return dict(
        id=session_id, title=f'{label or persona["name"]} 模拟面试',
        job_id=material.get('job_id', ''), company=material.get('company', ''), job_title=material.get('job_title', ''),
        persona=persona, material=material,
        config=dict(question_count=question_count, intensity=intensity, preset_id=persona_id, company_style=company_style),
        question_plan=None, progress=dict(phase='opening', current_index=0, segments=[], rounds=0),
        status='planning', report=None, conversation_id='', created_at=now(),
    )


class InterviewManager:
    """In-process registry of interview runs; one active run per session."""

    def __init__(self, repo):
        self.repo = repo
        self._runs = {}
        self._lock = threading.RLock()

    def get(self, run_id):
        return self._runs.get(run_id)

    def active_for_session(self, session_id):
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if getattr(handle, 'session_id', '') == session_id and not handle.done:
                return handle
        return None

    def _register(self, handle):
        with self._lock:
            if len(self._runs) > 100:
                for run_id, stale in list(self._runs.items()):
                    if stale.done and time.time() - stale.created > 3600:
                        self._runs.pop(run_id, None)
            self._runs[handle.id] = handle

    # ---- 三类入口：出题（建场后自动）/ 对话轮 / 复盘 ----

    def start_plan(self, session):
        handle = self._new_handle(session)
        handle.push('start', dict(run_id=handle.id, session_id=session['id'], kind='plan'))
        handle.thread = threading.Thread(target=self._plan_execute, args=(handle, session), daemon=True)
        handle.thread.start()
        return handle

    def start_turn(self, session, answer=''):
        """追加一轮对话。answer 为空表示开场（面试官自我介绍+第一题）。"""
        conversation_id = session['conversation_id']
        if answer.strip():
            self.repo.append_messages(conversation_id, [dict(role='user', content=answer.strip())])
            self._record_answer(session, answer.strip())
        handle = self._new_handle(session)
        handle.push('start', dict(run_id=handle.id, session_id=session['id'], kind='turn'))
        handle.thread = threading.Thread(target=self._turn_execute, args=(handle, session), daemon=True)
        handle.thread.start()
        return handle

    def start_report(self, session):
        session['status'] = 'reporting'
        session = self.repo.save_interview_session(session)
        handle = self._new_handle(session)
        handle.push('start', dict(run_id=handle.id, session_id=session['id'], kind='report'))
        handle.thread = threading.Thread(target=self._report_execute, args=(handle, session), daemon=True)
        handle.thread.start()
        return handle

    def _new_handle(self, session):
        existing = self.active_for_session(session['id'])
        if existing:
            raise ValueError('当前场次正在生成，请等待完成或先停止')
        handle = RunHandle(uuid.uuid4().hex, '', None)
        handle.session_id = session['id']
        handle.error = None
        self._register(handle)
        return handle

    # ---- worker：出题 ----

    def _plan_execute(self, handle, session):
        started = time.time()
        try:
            persona, material, config = session['persona'], session['material'], session['config']
            # worker 线程收不到请求中间件挂的线程级模型配置（contextvar 不跨线程）：
            # 必须显式组装生效配置（含「分析」绑定与对话模型回落），否则回落环境变量 → 地址为空。
            with use_config(ModelSettings(self.repo).effective()):
                plan = build_question_plan(persona, material, question_count=config.get('question_count', 8))
            session['question_plan'] = plan
            session['status'] = 'ready'
            conversation_id = self.repo.create_conversation(interview_scope(session['id']), mode='interview')
            session['conversation_id'] = conversation_id
            session = self.repo.save_interview_session(session)
            logger.info('模拟面试出题完成 session=%s 题数=%s 用时=%.0fs', session['id'], len(plan['questions']), time.time() - started)
            handle.push('done', dict(kind='plan', session=session))
        except RunCancelled:
            session['status'] = 'ready' if session.get('question_plan') else 'failed'
            self.repo.save_interview_session(session)
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            logger.warning('模拟面试出题失败 session=%s：%s', session['id'], exc)
            session['status'] = 'failed'
            self.repo.save_interview_session(session)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('模拟面试出题异常 session=%s', session['id'])
            session['status'] = 'failed'
            self.repo.save_interview_session(session)
            handle.error = '出题失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    # ---- worker：对话轮 ----

    def _turn_execute(self, handle, session):
        started = time.time()
        try:
            conversation_id = session['conversation_id']
            history = self.repo.get_conversation(conversation_id)['messages']
            phase = session['progress'].get('phase', 'opening')
            system = build_interviewer_system(session['persona'], session['material'], session['question_plan'],
                                              phase=phase, progress=session['progress'])
            messages = [dict(role='system', content=system)]
            messages += [dict(role=item['role'], content=item['content']) for item in history if item.get('role') in ('user', 'assistant')]
            if not messages[-1]['role'] == 'user':
                # 开场轮：对话历史为空，给一句只有本轮可见的触发语，不落库。
                messages.append(dict(role='user', content='（候选人已就座，请开始面试）'))
            from .interview import get_config
            # 同出题：线程级配置必须显式挂到当前 worker（含「分析」绑定与对话模型回落）。
            with use_config(ModelSettings(self.repo).effective()):
                assistant, _usage, reasoning = stream_turn(
                    get_config(), messages, [ADVANCE_TOOL],
                    on_delta=lambda piece: handle.push('delta', dict(text=piece)),
                    on_reasoning=lambda piece: handle.push('reasoning_delta', dict(text=piece)),
                    should_stop=handle.cancel.is_set, register_abort=handle.add_abort, timeout=120)
            verdict = self._apply_verdict(session, assistant)
            entry = dict(role='assistant', content=assistant.get('content') or '')
            if reasoning:
                entry['reasoning'] = reasoning
            stored = self.repo.append_messages(conversation_id, [entry])[0]
            session['status'] = 'live'
            if session['progress'].get('phase') == 'opening' and verdict != 'wrapup':
                session['progress']['phase'] = 'questioning'
            if verdict == 'wrapup':
                session['progress']['phase'] = 'wrapup'
            session = self.repo.save_interview_session(session)
            logger.info('模拟面试对话轮完成 session=%s verdict=%s 用时=%.0fs', session['id'], verdict, time.time() - started)
            handle.push('done', dict(kind='turn', verdict=verdict, message_id=stored['id'], session=session))
        except RunCancelled:
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            if handle.cancel.is_set():
                return
            logger.warning('模拟面试对话轮失败 session=%s：%s', session['id'], exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            if handle.cancel.is_set():
                return
            logger.exception('模拟面试对话轮异常 session=%s', session['id'])
            handle.error = '面试官掉线了，请重试本轮'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    @staticmethod
    def _verdict_of(assistant):
        for call in assistant.get('tool_calls') or []:
            if (call.get('function') or {}).get('name') != 'advance':
                continue
            try:
                payload = json.loads(call['function'].get('arguments') or '{}')
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get('verdict'):
                return payload
        return None

    def _apply_verdict(self, session, assistant):
        """把 advance 调度决策写进场次进度：题号推进、弱点信号与内心评价按题归档。"""
        progress = session['progress']
        payload = self._verdict_of(assistant)
        if payload is None:
            return 'followup'  # 模型没上报按继续追问处理，不阻断对话
        segments = progress.setdefault('segments', [])
        note = str(payload.get('note') or '')[:300]
        weakness = [str(item)[:40] for item in (payload.get('weakness') or []) if str(item).strip()]
        if segments:
            current = segments[-1]
            if note:
                current.setdefault('notes', []).append(note)
            current.setdefault('signals', []).extend(weakness)
        verdict = payload.get('verdict', 'followup')
        index = payload.get('question_index')
        questions = (session.get('question_plan') or {}).get('questions') or []
        if verdict == 'next':
            # 不设上限截断：越过最后一题即视为进入反问收尾（phase=wrapup）。
            progress['current_index'] = max(int(index or 0) + 1, 0)
            progress['rounds'] = progress.get('rounds', 0) + 1
            if progress['current_index'] < len(questions):
                segments.append(self._new_segment(progress['current_index'], questions))
            else:
                progress['phase'] = 'wrapup'
        elif verdict == 'wrapup':
            progress['phase'] = 'wrapup'
            # 反问环节的回答不参与逐题点评：单开一个不可点评的 wrap 片段承接。
            if segments and segments[-1].get('reviewable', False):
                segments.append(dict(index=-1, question={}, answers=[], notes=[], signals=[], reviewable=False))
        return verdict

    @staticmethod
    def _new_segment(index, questions):
        item = questions[index] if 0 <= index < len(questions) else {}
        return dict(index=index, question=dict(topic=item.get('topic', ''), question=item.get('question', ''),
                                               focus=item.get('focus', ''), followups=item.get('followups', [])),
                    answers=[], notes=[], signals=[], reviewable=bool(item))

    def _record_answer(self, session, answer):
        """候选人的回答归档到当前题片段（复盘逐题对账的依据）；反问环节的落 wrap 片段。"""
        progress = session['progress']
        segments = progress.setdefault('segments', [])
        questions = (session.get('question_plan') or {}).get('questions') or []
        if not segments:
            segments.append(self._new_segment(progress.get('current_index', 0), questions))
        segments[-1].setdefault('answers', []).append(answer[:4000])

    # ---- worker：复盘 ----

    def _report_execute(self, handle, session):
        started = time.time()
        try:
            segments = [seg for seg in session['progress'].get('segments', []) if seg.get('reviewable') and seg.get('answers')]
            if not segments:
                raise ValueError('本场还没有可复盘的问答')
            # 同出题：线程级配置必须显式挂到当前 worker（含「分析」绑定与对话模型回落）。
            with use_config(ModelSettings(self.repo).effective()):
                report = generate_report(session['persona'], segments, session['material'],
                                         should_stop=handle.cancel.is_set,
                                         on_progress=lambda data: handle.push('progress', data))
            session['report'] = dict(**report, reviewed=len(segments), generated_at=now())
            session['status'] = 'done'
            session = self.repo.save_interview_session(session)
            logger.info('模拟面试复盘完成 session=%s 题数=%s 用时=%.0fs', session['id'], len(segments), time.time() - started)
            handle.push('done', dict(kind='report', session=session))
        except RunCancelled:
            session['status'] = 'live'
            self.repo.save_interview_session(session)
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            session['status'] = 'live'
            self.repo.save_interview_session(session)
            logger.warning('模拟面试复盘失败 session=%s：%s', session['id'], exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('模拟面试复盘异常 session=%s', session['id'])
            session['status'] = 'live'
            self.repo.save_interview_session(session)
            handle.error = '复盘生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    # ---- SSE / 取消：与 analyze_runs 同款 ----

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
        aborters = handle.aborters
        if aborters:
            threading.Thread(target=lambda: [fn() for fn in aborters], daemon=True).start()
        return handle.snapshot()

