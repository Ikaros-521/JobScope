"""真实面试逐字稿的运行管理：标注 / 复盘两类后台 run 共用一个注册表，一份逐字稿同时一个。

与 interview_runs 同一套模式：RunHandle 事件日志 + SSE 重放（断线重连从头重建状态），
取消是合作式——标注是单次大调用无法中途打断（取消后丢弃结果回滚状态），复盘的逐题
调用在题间边界退出。事件序列 start → progress… → done|error|cancelled → end；
done 携带完整逐字稿快照（worker 先落库再推送）。
"""
import logging
import threading
import time
import uuid

from .chat_runs import RunHandle, sse_frame
from .interview import JD_SUMMARY_CHARS
from .repository import now
from .semantic import RunCancelled
from .settings import ModelSettings, use_config
from .transcript import generate_retro_report, label_speakers

logger = logging.getLogger(__name__)


def new_transcript(*, job_id, job_title, company, text):
    """建逐字稿：岗位锚点与原文快照存 payload（岗位后续删除不影响已有文本）。"""
    return dict(
        id='T-' + uuid.uuid4().hex,
        job_id=job_id, job_title=job_title, company=company,
        raw_text=text, status='imported', error='',
        segments=[], labeling_meta=None, report=None,
        created_at=now(),
    )


class TranscriptManager:
    """In-process registry of transcript runs; one active run per transcript."""

    def __init__(self, repo):
        self.repo = repo
        self._runs = {}
        self._lock = threading.RLock()
        self._sweep_stale()

    def _sweep_stale(self):
        """启动清扫：上一进程遗留的 labeling/reporting 状态没有活 run 兜着（run 在进程内，
        服务重启即消失），一律回滚到可操作状态——关页/刷新不受此影响（run 在服务端继续跑），
        这里只修「服务重启杀死了 run」的存档：标注中断有部分段落则保留成 labeled（可核对或重标），
        否则回 imported；复盘中断与报告 worker 的失败回滚同语义（有旧报告回 done，否则回 labeled）。"""
        for item in self.repo.interview_transcripts(limit=200):
            if item['status'] not in ('labeling', 'reporting'):
                continue
            transcript = self.repo.get_interview_transcript(item['id'])
            if transcript is None:
                continue
            if item['status'] == 'labeling':
                if transcript.get('segments'):
                    transcript['status'] = 'labeled'
                    transcript['error'] = '服务重启打断了标注，已保留部分结果；可核对修改或重新标注'
                else:
                    transcript['status'] = 'imported'
            else:
                transcript['status'] = 'done' if transcript.get('report') else 'labeled'
            self.repo.save_interview_transcript(transcript)

    def get(self, run_id):
        return self._runs.get(run_id)

    def active_for_transcript(self, transcript_id):
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if getattr(handle, 'transcript_id', '') == transcript_id and not handle.done:
                return handle
        return None

    def _register(self, handle):
        with self._lock:
            if len(self._runs) > 100:
                for run_id, stale in list(self._runs.items()):
                    if stale.done and time.time() - stale.created > 3600:
                        self._runs.pop(run_id, None)
            self._runs[handle.id] = handle

    # ---- 两类入口：标注（导入/重标后自动）/ 复盘报告 ----

    def start_label(self, transcript):
        handle = self._new_handle(transcript)  # 忙碌检查在前：翻状态落库后抛错会把状态毒留在 labeling
        transcript['status'] = 'labeling'
        transcript = self.repo.save_interview_transcript(transcript)
        handle.push('start', dict(run_id=handle.id, transcript_id=transcript['id'], kind='label'))
        handle.thread = threading.Thread(target=self._label_execute, args=(handle, transcript), daemon=True)
        handle.thread.start()
        return handle

    def start_report(self, transcript):
        handle = self._new_handle(transcript)
        transcript['status'] = 'reporting'
        transcript = self.repo.save_interview_transcript(transcript)
        handle.push('start', dict(run_id=handle.id, transcript_id=transcript['id'], kind='report'))
        handle.thread = threading.Thread(target=self._report_execute, args=(handle, transcript), daemon=True)
        handle.thread.start()
        return handle

    def _new_handle(self, transcript):
        existing = self.active_for_transcript(transcript['id'])
        if existing:
            raise ValueError('当前逐字稿正在生成，请等待完成或先停止')
        handle = RunHandle(uuid.uuid4().hex, '', None)
        handle.transcript_id = transcript['id']
        handle.error = None
        self._register(handle)
        return handle

    # ---- worker：标注 ----

    def _label_execute(self, handle, transcript):
        started = time.time()
        # 取消/失败回滚目标：带旧段落的重标回 labeled，首次标注回 imported（可重试或删除）。
        restore = 'labeled' if transcript.get('segments') else 'imported'
        partial = []

        def _on_event(kind, data=None):
            if kind == 'segment':
                partial.append(data)
                handle.push('progress', dict(stage='label_segment', segment=data, done=len(partial)))
                if len(partial) % 20 == 0:
                    # 流式节流落库（每 20 段）：服务重启也能保住已标段落，回来可核对或重标。
                    transcript['segments'] = [dict(index=index, **seg) for index, seg in enumerate(partial)]
                    self.repo.save_interview_transcript(transcript)
            else:
                partial.clear()
                handle.push('progress', dict(stage='label_reset'))

        try:
            # worker 线程收不到请求中间件挂的线程级模型配置（contextvar 不跨线程）：
            # 必须显式组装生效配置（含「分析」绑定与对话模型回落），否则回落环境变量 → 地址为空。
            with use_config(ModelSettings(self.repo).effective()):
                segments = label_speakers(transcript['raw_text'], on_event=_on_event,
                                          should_stop=handle.cancel.is_set)
            if handle.cancel.is_set():
                # 取消：流已在 delta 间隙打断，丢弃结果，状态回滚不落新段落。
                transcript['status'] = restore
                self.repo.save_interview_transcript(transcript)
                handle.push('cancelled', dict(run_id=handle.id))
                return
            transcript['segments'] = [dict(index=index, **seg) for index, seg in enumerate(segments)]
            transcript['status'] = 'labeled'
            transcript['error'] = ''
            transcript['labeling_meta'] = dict(labeled_at=now(), segment_count=len(transcript['segments']))
            transcript = self.repo.save_interview_transcript(transcript)
            logger.info('逐字稿标注完成 id=%s 段数=%s 用时=%.0fs', transcript['id'],
                        len(transcript['segments']), time.time() - started)
            handle.push('done', dict(kind='label', transcript=transcript))
        except RunCancelled:
            transcript['status'] = restore
            self.repo.save_interview_transcript(transcript)
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            logger.warning('逐字稿标注失败 id=%s：%s', transcript['id'], exc)
            transcript['status'] = 'failed'
            transcript['error'] = str(exc)
            self.repo.save_interview_transcript(transcript)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('逐字稿标注异常 id=%s', transcript['id'])
            transcript['status'] = 'failed'
            transcript['error'] = '标注失败，请稍后重试'
            self.repo.save_interview_transcript(transcript)
            handle.error = transcript['error']
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    # ---- worker：复盘报告 ----

    def _report_execute(self, handle, transcript):
        started = time.time()
        # 失败回滚目标：带旧报告的重生成回 done，首生成分回 labeled。
        restore = 'done' if transcript.get('report') else 'labeled'
        try:
            segments = transcript.get('segments') or []
            if not segments:
                raise ValueError('还没有可复盘的逐字稿段落')
            # 岗位锚点现读：导入后 JD 若有更新，复盘用最新版；岗位被删则降级为无 JD 复盘。
            job = self.repo.job(transcript['job_id']) if transcript.get('job_id') else None
            limitations = []
            jd_context = ''
            job_meta = dict(title=transcript.get('job_title', ''), company=transcript.get('company', ''))
            if job:
                jd_context = (job.get('description') or '').strip()[:JD_SUMMARY_CHARS]
                job_meta = dict(title=job.get('title', ''), company=job.get('company', ''))
            else:
                limitations.append('绑定岗位已删除或不存在，本报告未结合 JD')
            # 同标注：线程级配置必须显式挂到当前 worker（含「分析」绑定与对话模型回落）。
            with use_config(ModelSettings(self.repo).effective()):
                report = generate_retro_report(segments, jd_context, job_meta,
                                               should_stop=handle.cancel.is_set,
                                               on_progress=lambda data: handle.push('progress', data))
            if limitations:
                report['limitations'] = limitations
            transcript['report'] = dict(**report, reviewed=len(report['questions']), generated_at=now())
            transcript['status'] = 'done'
            transcript['error'] = ''
            transcript = self.repo.save_interview_transcript(transcript)
            logger.info('逐字稿复盘完成 id=%s 题数=%s 用时=%.0fs', transcript['id'],
                        len(report['questions']), time.time() - started)
            handle.push('done', dict(kind='report', transcript=transcript))
        except RunCancelled:
            transcript['status'] = restore
            self.repo.save_interview_transcript(transcript)
            handle.push('cancelled', dict(run_id=handle.id))
        except ValueError as exc:
            transcript['status'] = restore
            self.repo.save_interview_transcript(transcript)
            logger.warning('逐字稿复盘失败 id=%s：%s', transcript['id'], exc)
            handle.error = str(exc)
            handle.push('error', dict(message=str(exc)))
        except Exception:
            logger.exception('逐字稿复盘异常 id=%s', transcript['id'])
            transcript['status'] = restore
            self.repo.save_interview_transcript(transcript)
            handle.error = '复盘生成失败，请稍后重试'
            handle.push('error', dict(message=handle.error))
        finally:
            handle.finish()

    # ---- SSE / 取消：与 interview_runs 同款 ----

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
