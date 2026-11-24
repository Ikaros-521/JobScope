"""面试备战计划的个性化分析层（2026-11-24）：LLM 出诊断与练法，规则层只负责排期。

设计取舍：让模型生成整份 14 天 ×2–3 条任务 JSON 既费 token 又容易结构出错，
所以这里只产出「诊断 + 3–6 个针对性短板 + 每个短板的具体练法」，再由
prep_plan._tasks_for 按既有的 mock + 一件专项 + 每周回看节奏展开成任务。
这样模型负责真正需要理解力的部分，打卡闭环与日期规则保持确定性。

模型不可用或输出不合格时返回 None，由调用方回退规则版；绝不阻断计划生成。
"""
import json
import logging
import re

from . import failure_museum, gap_narrative

logger = logging.getLogger(__name__)

DIAGNOSIS_MAX = 500
TOPIC_MAX = 60
DETAIL_MAX = 300
DRILL_MAX = 300
SOURCE_MAX = 200
FOCUS_MIN = 2
FOCUS_MAX = 6

_SYSTEM = '''你是求职面试备战教练。根据候选人的模拟面试复盘、失败归因、目标岗位 JD、评估缺口、简历与本人填写的备战重点，做一次针对性诊断，给出最该优先补的短板，以及每个短板"今天就能做"的具体练法。

要求：
1. diagnosis 用 2–4 句中文写清最关键的 1–2 个问题模式，必须引用材料里的具体证据（哪场面试、哪个维度、哪条缺口、简历里的哪个项目），不要说"加强沟通能力"这类空话。材料不足时直说依据有限，不要编造。
2. focus 给 3–6 项，按优先级从高到低。topic 是具体的短板名（不超过 30 字），detail 说明为什么它是短板（引用证据），source 写清依据来自哪份材料的哪一条，drill 是一项可以在 15 分钟内完成的具体练习——写清"做什么、产出什么"，比如"画一张链路图，标注三段串行/并行，再对着图讲 90 秒"，不要"多练习""系统学习"。
3. kind：需要补事实/概念/知识的用 study，需要改表达/叙事/答题结构的用 drill。
4. weight：3=反复出现的硬伤，2=单场面试暴露的明确问题，1=待核实或次要缺口。
5. goal 是候选人自己填的备战重点，要靠前满足；minutes_per_day 是每天可用时间，练法不要超出它。
6. 只输出 JSON，不要解释或 Markdown 围栏：
{"diagnosis":"...","focus":[{"topic":"...","detail":"...","source":"...","drill":"...","kind":"drill|study","weight":2}]}'''


def _parse_json(content):
    content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
    candidates = [content]
    match = re.search(r'\{.*\}', content, re.S)
    if match:
        candidates.append(match.group(0))
    for text in candidates:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    raise ValueError('模型未返回可解析 JSON')


_PROBE_LIMIT = 2000
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', '"': '"', '\\': '\\', '/': '/'}


class DiagnosisProbe:
    """从流式 JSON 文本里渐进摘出 diagnosis 的字符串值，让诊断边生成边可显示。

    只服务于实时展示：转义解码宽容（解不出的原样跳过）、截断有护栏，
    最终内容仍以完整 JSON 的解析结果为准，这里解错一个字符不影响正式结果。
    """

    def __init__(self):
        self._buffer = ''
        self._state = 'seek'  # seek → colon → open → text → done
        self._visible = ''
        self._sent = 0

    def feed(self, piece):
        """喂入一段模型输出，返回本次新解锁的可见诊断文本（可能为空串）。"""
        self._buffer += piece
        self._advance()
        delta = self._visible[self._sent:]
        self._sent = len(self._visible)
        return delta

    def _advance(self):
        while True:
            if self._state == 'seek':
                index = self._buffer.find('"diagnosis"')
                if index < 0:
                    return
                self._buffer = self._buffer[index + len('"diagnosis"'):]
                self._state = 'colon'
            elif self._state == 'colon':
                stripped = self._buffer.lstrip()
                if not stripped:
                    return
                if stripped[0] == ':':
                    self._buffer = stripped[1:]
                    self._state = 'open'
                else:  # diagnosis 键后面不是冒号：异常输出，放弃探测。
                    self._state = 'done'
            elif self._state == 'open':
                stripped = self._buffer.lstrip()
                if not stripped:
                    return
                if stripped[0] == '"':
                    self._buffer = stripped[1:]
                    self._state = 'text'
                else:
                    self._state = 'done'
            elif self._state == 'text':
                if len(self._visible) >= _PROBE_LIMIT:  # 异常长输出：停止探测，等完整解析兜底。
                    self._state = 'done'
                    return
                before = len(self._buffer)
                self._scan_text()
                if self._state != 'text':
                    return
                # 缓冲耗尽，或只剩一个等下一块的半个转义（无法推进）：返回，别空转。
                if not self._buffer or len(self._buffer) == before:
                    return
            else:
                return

    def _scan_text(self):
        """消费缓冲：解码到结束引号或第一个不完整转义为止；已消费部分移出缓冲。"""
        buffer = self._buffer
        index = 0
        while index < len(buffer):
            char = buffer[index]
            if char == '"':
                self._emit(self._decode(buffer[:index]))
                self._buffer = buffer[index + 1:]
                self._state = 'done'
                return
            if char == '\\':
                if index + 1 >= len(buffer):
                    break  # 转义不完整，连同后面的字符留到下一块
                if buffer[index + 1] == 'u' and index + 6 > len(buffer):
                    break
                index += 6 if buffer[index + 1] == 'u' else 2
                continue
            index += 1
        self._emit(self._decode(buffer[:index]))
        self._buffer = buffer[index:]

    @staticmethod
    def _decode(region):
        out = []
        index = 0
        while index < len(region):
            char = region[index]
            if char == '\\' and index + 1 < len(region):
                escape = region[index + 1]
                if escape == 'u' and index + 6 <= len(region):
                    try:
                        out.append(chr(int(region[index + 2:index + 6], 16)))
                    except ValueError:
                        pass
                    index += 6
                    continue
                out.append(_ESCAPES.get(escape, escape))
                index += 2
                continue
            out.append(char)
            index += 1
        return ''.join(out)

    def _emit(self, text):
        if text:
            self._visible += text


def _coerce(raw):
    """软校验：坏行丢掉、超长截断；只有整体结构不对才判失败触发重试。"""
    if not isinstance(raw, dict):
        raise ValueError('返回不是 JSON 对象')
    diagnosis = str(raw.get('diagnosis') or '').strip()[:DIAGNOSIS_MAX]
    items = []
    for row in raw.get('focus') or []:
        if not isinstance(row, dict):
            continue
        topic = str(row.get('topic') or '').strip()
        if len(topic) < 2:
            continue
        kind = str(row.get('kind') or 'drill').strip()
        if kind not in ('study', 'drill'):
            kind = 'drill'
        try:
            weight = int(row.get('weight') or 1)
        except (TypeError, ValueError):
            weight = 1
        items.append(dict(
            topic=topic[:TOPIC_MAX], detail=str(row.get('detail') or '').strip()[:DETAIL_MAX],
            source=str(row.get('source') or '').strip()[:SOURCE_MAX],
            drill=str(row.get('drill') or '').strip()[:DRILL_MAX],
            kind=kind, weight=max(0, min(3, weight)), session_id='', job_id=''))
        if len(items) >= FOCUS_MAX:
            break
    if len(items) < FOCUS_MIN:
        raise ValueError(f'有效短板不足 {FOCUS_MIN} 项')
    return dict(diagnosis=diagnosis, focus=items)


def prep_material(repo, focus, prefs=None):
    """把本机材料压成一份给模型读的紧凑 payload；不上传，只发给已绑定的模型。"""
    prefs = prefs or {}
    job_id = str(prefs.get('job_id') or '')
    job = repo.job(job_id) if job_id else None
    jd = str((job or {}).get('description') or '')[:3000]
    gaps = []
    if job:
        _, report = repo.list_reports(job_id)
        gaps = [str(item)[:200] for item in ((report or {}).get('gaps') or [])[:6]]

    recent = []
    for summary in repo.interview_sessions(limit=12):
        if not summary.get('has_report'):
            continue
        session = repo.get_interview_session(summary['id']) or {}
        report = session.get('report') or {}
        if not report:
            continue
        low = [dict(name=item.get('name'), score=item.get('score'),
                    comment=str(item.get('comment') or '')[:200])
               for item in (report.get('dimensions') or [])
               if item.get('score') is not None and float(item['score'] or 5) <= 3][:4]
        recent.append(dict(
            title=str(session.get('job_title') or summary.get('title') or '模拟面试')[:80],
            summary=str(report.get('summary') or '')[:600],
            low_dimensions=low,
            risks=[str(item)[:200] for item in (report.get('risk_list') or [])[:4]]))
        if len(recent) >= 2:
            break

    patterns = failure_museum.build_pattern_report(repo.interview_attributions()).get('recurring') or []
    return dict(
        goal=str(prefs.get('goal') or '').strip()[:300],
        minutes_per_day=prefs.get('minutes_per_day') or 20,
        target_job=(dict(id=job_id, title=(job or {}).get('title', ''),
                         company=(job or {}).get('company', '')) if job else None),
        jd_summary=jd,
        jd_gaps=gaps,
        recent_interviews=recent,
        failure_patterns=[dict(label=item.get('label'), count=item.get('count'),
                               advice=str(item.get('advice') or '')[:200]) for item in patterns[:5]],
        rule_focus=[dict(topic=item.get('topic', ''), detail=str(item.get('detail') or '')[:250],
                         source=str(item.get('source') or '')[:200]) for item in (focus or [])[:6]],
        gap_entries=gap_narrative.entries_block(repo)[:4000],
        resume=str((repo.resume().get('text') or ''))[:3000],
    )


def analyze(repo, focus, prefs=None, on_delta=None):
    """返回 dict(diagnosis, focus) 供生成使用；模型未配置/失败/输出无效返回 None。

    on_delta 提供时改走流式调用：诊断文本经 DiagnosisProbe 边生成边解码，
    增量回调给调用方（探测只影响实时展示，正式内容仍由完整 JSON 解析产生）。
    """
    from .model_transport import complete, output_budget, stream_complete
    from .settings import get_purpose_config, purpose_configured

    if not purpose_configured('analysis'):
        return None
    payload = prep_material(repo, focus, prefs)
    for attempt in (1, 2):
        probe = DiagnosisProbe() if on_delta else None

        def push(piece, _probe=probe):
            if _probe is None:
                return
            try:
                on_delta(_probe.feed(piece))
            except Exception:
                pass  # 展示层异常绝不打断模型调用

        try:
            config = get_purpose_config('analysis')
            body = json.dumps(payload, ensure_ascii=False)
            kwargs = dict(temperature=.3, max_tokens=output_budget(config, 6000), timeout=240)
            if probe is not None:
                content, _ = stream_complete(config, _SYSTEM, body, on_delta=push, **kwargs)
            else:
                content, _ = complete(config, _SYSTEM, body, **kwargs)
            return _coerce(_parse_json(content))
        except Exception as exc:
            logger.warning('备战计划个性化分析失败（第 %s 次）：%s', attempt, exc)
    return None
