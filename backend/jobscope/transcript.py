"""真实面试复盘：ASR 逐字稿导入 → 说话人标注（模型初标 + 人工核对）→ 复盘报告。

与模拟面试（interview.py）互补：那边是「AI 面试官生成对话」，这边是「真实对话的
沉淀」——外部 ASR 工具转写的纯文本进来，先按说话人切分标注（自带「候选人/面试官」
前缀的标准格式直接本地切分，否则模型初标，用户逐条核对修正后再入库），再基于确认
过的逐字稿生成结合 JD 的复盘：问答拆解 → 逐题并行点评 → 汇总（总体评分/岗位匹配/
优点问题/下次行动）。模型配置复用「分析」功能绑定。
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .interview import JD_SUMMARY_CHARS, _parse_json
from .model_transport import output_budget
from .settings import get_active_configs, use_config

logger = logging.getLogger(__name__)

# 输出预算默认值：渠道级 max_output_tokens（设置中心）优先。标注的输出 ≈ 输入文本
# 重排（保留原话），给大预算；导逐字稿上限 40000 字 ≈ 2 万 token，GLM 类模型可承载。
LABEL_MAX_TOKENS = 32000
QA_MAX_TOKENS = 16000
REVIEW_MAX_TOKENS = 16000
SUMMARY_MAX_TOKENS = 16000

MAX_SEGMENTS = 400          # 段落数封顶（软校验截断，不整条作废）
MAX_SEGMENT_CHARS = 4000    # 单段截断：ASR 碎句合并后也远小于此
MAX_QUESTIONS = 30          # 问答对封顶：真实单场面试极少超过
SPEAKER_ME = 'me'


# ---- 说话人标注：逐字保留原话，只切分与打标 ----

_LABEL_SYSTEM = '''你是面试逐字稿的说话人标注员。输入是录音转文字（ASR）工具导出的真实面试逐字稿，格式可能是：纯文本无说话人标记、「说话人1：」等说话人前缀、或带时间戳。任务：
1. 把全文切分成一段段完整的话，同一人连续说的话合并成一段（ASR 常把一句话打碎成多条）。
2. 给每段标注说话人：候选人本人（求职者，接受面试的人）标 "me"；每一位面试官按出场顺序标 "interviewer_1"、"interviewer_2"…（同一面试官全程用同一编号，最多 interviewer_5）。
3. 逐字保留原话：不改写、不总结、不删字、不纠错；去掉时间戳标记与「说话人N：」前缀本身，但前缀后面的内容完整保留。
4. 寒暄、开场白、告别等非问答内容也保留，同样标注说话人。
只输出JSON：{"segments":[{"speaker":"me|interviewer_1|...","text":"..."}]}'''


def _norm_speaker(value, seen):
    """说话人归一化：me 保留；面试官统一 interviewer_N。

    带编号（interviewer_2/面试官2/说话人2）照编号；不带的未知称呼（面试官A/考官）
    按首次出现顺序分配编号——同一称呼同一编号，避免把两位面试官合并成一位。
    """
    text = str(value or '').strip().lower()
    if text in ('me', 'candidate', '我', '候选人', '求职者', '本人'):
        return SPEAKER_ME
    match = re.search(r'(\d+)', text)
    if match and re.search(r'interviewer|面试官|说话人|speaker|考官|主持', text):
        return f'interviewer_{max(1, min(int(match.group(1)), 5))}'
    if text not in seen:
        seen[text] = f'interviewer_{min(len(seen) + 1, 5)}'
    return seen[text]


# ---- 标准格式直读：逐字稿自带「候选人/面试官」前缀时免模型标注，导入即切分（降本增效） ----

# 一行 = [可选时间戳] [可选分隔符] 说话人：原话；说话人 ≤24 字且不含冒号，防把长句子误当称呼。
_LINE_RE = re.compile(
    r'^\s*(?:\[[^\[\]]{1,16}\]|\([^\(\)]{1,16}\)|\d{1,2}:\d{2}(?::\d{2})?)?\s*'
    r'(?:[-—–]\s*)?([^:：]{1,24}?)\s*[:：]\s*(.*)$')


def parse_labeled_transcript(text):
    """标准格式直读：逐行解析「说话人：原话」，连续同人合并成段（与模型标注同契约）。

    整篇足够「格式化」才直读：≥3 行带前缀、占比 ≥60%、能认出候选人（me）且说话人
    ≥2——「说话人1/2」这类无语义前缀认不出谁是候选人，返回 [] 交模型判断。非匹配行
    视作上一段说话人的续行并入（首段前的标题/日期杂讯丢弃）；不达标返回 []。
    """
    groups, matched, total = [], 0, 0
    seen = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        total += 1
        m = _LINE_RE.match(line)
        if m is None:
            if groups:
                groups[-1][1].append(line.strip())
            continue
        matched += 1
        speaker = _norm_speaker(m.group(1), seen)
        if not groups or groups[-1][0] != speaker:
            if len(groups) >= MAX_SEGMENTS:  # 软校验截断（同流式解析器），不整条作废
                break
            groups.append([speaker, []])
        piece = m.group(2).strip()
        if piece:
            groups[-1][1].append(piece)
    if (matched < 3 or total == 0 or matched * 10 < total * 6
            or not any(speaker == SPEAKER_ME for speaker, _parts in groups)
            or len({speaker for speaker, _parts in groups}) < 2):
        return []
    return [dict(speaker=speaker, text='\n'.join(parts)[:MAX_SEGMENT_CHARS])
            for speaker, parts in groups if parts]


class _StreamLabelParser:
    """流式标注解析：模型输出分片到达，增量提取完整闭合的段对象——前端逐段点亮。

    只认平衡大括号组并按内容校验，```json 围栏与前言杂讯天然跳过；单个坏对象丢弃
    不炸全局（软校验同款哲学）。seen 跨段共享：无编号称呼（考官A/考官B）按出场顺序
    分配同一编号。顶层 {"segments":[...]} 包裹对象闭合时解析不出 speaker/text，自动忽略。
    """

    def __init__(self):
        self.buffer = ''
        self.pos = 0
        self.in_str = False
        self.esc = False
        self.starts = []   # 未闭合对象起始偏移栈（顶层包裹对象也在栈里）
        self.seen = {}
        self.segments = []

    def feed(self, text):
        self.buffer += text
        found = []
        i = self.pos
        n = len(self.buffer)
        buf = self.buffer
        while i < n:
            ch = buf[i]
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif ch == '\\':
                    self.esc = True
                elif ch == '"':
                    self.in_str = False
            elif ch == '"':
                self.in_str = True
            elif ch == '{':
                self.starts.append(i)
            elif ch == '}' and self.starts:
                start = self.starts.pop()
                item = self._coerce(buf[start:i + 1])
                if item is not None and len(self.segments) < MAX_SEGMENTS:
                    found.append(item)
            i += 1
        self.pos = n
        self.segments.extend(found)
        return found

    def _coerce(self, raw):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(item, dict):
            return None
        text = str(item.get('text') or '').strip()
        if not text:
            return None
        return dict(speaker=_norm_speaker(item.get('speaker'), self.seen), text=text[:MAX_SEGMENT_CHARS])


def label_speakers(text, *, on_event=None, should_stop=None):
    """标注：流式大预算调用（分析绑定），段对象闭合即经 on_event 产出——前端逐段点亮。

    on_event('reset') 每次尝试前通知调用方清空已发段落；on_event('segment', seg) 逐段产出。
    截断（OutputLimitError）或整流零有效段自动重试一次，两次都坏才报错；should_stop 在
    delta 间隙生效（抛 RunCancelled，底层连接随之断开）。标注失败不判死：transcript 落
    failed，用户可重标或改人工核对。
    """
    from .model_transport import OutputLimitError, stream_complete
    from .semantic import RunCancelled

    def _budget():
        return output_budget(get_config(), LABEL_MAX_TOKENS)

    for attempt in (1, 2):
        config = get_config()
        parser = _StreamLabelParser()
        if on_event:
            on_event('reset')
        state = {'pending': '', 'size': 0}

        def _flush():
            pending_text, state['pending'], state['size'] = state['pending'], '', 0
            for seg in parser.feed(pending_text):
                if on_event:
                    on_event('segment', seg)

        def _on_delta(piece):
            if should_stop is not None and should_stop():
                raise RunCancelled()
            state['pending'] += piece
            state['size'] += len(piece)
            if state['size'] >= 400:  # 合批喂解析器：40k 字流按 400 字批重扫，均摊成本可忽略
                _flush()

        try:
            content, _ = stream_complete(config, _LABEL_SYSTEM, text, temperature=.2,
                                         max_tokens=_budget(), timeout=600, on_delta=_on_delta)
        except OutputLimitError:
            logger.warning('逐字稿标注输出触顶（第 %s 次）max_tokens=%s', attempt, _budget())
            if attempt == 2:
                raise ValueError('标注被输出上限截断，请重试；反复出现时在设置中心调大渠道最大输出 tokens') from None
            continue
        _flush()
        logger.info('逐字稿标注流完成 attempt=%s 段数=%s 输出=%s 字', attempt, len(parser.segments), len(content))
        if parser.segments:
            return parser.segments
        logger.warning('逐字稿标注返回无效（第 %s 次）', attempt)
        if attempt == 2:
            raise ValueError('标注结果无效，请重试标注') from None


# ---- 问答拆解：段落编号引用，可审计 ----

_QA_SYSTEM = '''你是面试复盘的结构分析员。给你一份已标注说话人的面试逐字稿（每行 [段落编号] 说话人: 原话），请把它拆成一道道问答对。
要求：
1. 以面试官的问题为单位：同一问题主题下面试官的多次发话（含追问）与候选人的回答归为同一题；寒暄/开场/告别等非问答内容跳过。
2. 每题给出 question（面试官的问题概括，保留关键原话措辞）与 segments（该题覆盖的段落编号数组，含问题段与候选人回答段，按原文顺序）。
3. 候选人明确说「不知道/跳过」也算一题，如实记录。
只输出JSON：{"items":[{"question":"...","segments":[0,1,2]}]}'''


def _coerce_qa(raw, total):
    if not isinstance(raw, dict):
        raise ValueError('问答拆解不是 JSON 对象')
    items = []
    for item in raw.get('items') or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get('question') or '').strip()[:300]
        segments = []
        for value in item.get('segments') or []:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < total and index not in segments:
                segments.append(index)
        if question and segments:
            items.append(dict(question=question, segments=segments[:60]))
        if len(items) >= MAX_QUESTIONS:
            break
    if not items:
        raise ValueError('问答拆解缺失或全部无效')
    return items


def extract_qa(segments):
    """问答拆解：编号逐字稿 → 问答对清单（段索引引用，点评与前端都能对回原文）。"""
    from .model_transport import complete
    lines = '\n'.join(f"[{seg['index']}] {seg['speaker']}: {seg['text']}" for seg in segments)
    for attempt in (1, 2):
        config = get_config()
        content, _ = complete(config, _QA_SYSTEM, lines, temperature=.2,
                              max_tokens=output_budget(config, QA_MAX_TOKENS), timeout=300)
        try:
            return _coerce_qa(_parse_json(content), len(segments))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning('问答拆解返回无效（第 %s 次）：%s', attempt, exc)
            if attempt == 2:
                raise ValueError('问答拆解结果无效，请重试') from None


# ---- 逐题点评：真实回答（原话）+ JD 上下文 ----

_REVIEW_SYSTEM = '''你是面试教练。根据题目、候选人的实际回答（逐字稿原话，[me] 开头的是候选人）和岗位 JD 上下文，逐题点评这段真实面试表现。
要求：critique 以教练视角点评这段回答（好在哪、差在哪，引用候选人原话）；good 列亮点（至多 3 条）；bad 列问题（至多 3 条，含量化缺失/结构混乱/真实性存疑等）；suggestion 给针对本题的改进建议（下次同样的问题怎么答更好，具体到说法）。
只输出JSON：{"critique":"...","good":["..."],"bad":["..."],"suggestion":"..."}'''


def _coerce_review(raw):
    """逐题点评软校验（同 interview._coerce_review 的教训）：列表多几条/字段超长截断保住点评。"""
    if not isinstance(raw, dict):
        raise ValueError('点评不是 JSON 对象')

    def _text(value, limit):
        return str(value or '').strip()[:limit]

    critique = _text(raw.get('critique'), 2000)
    if not critique:
        raise ValueError('critique 缺失')
    good = [text for text in (_text(item, 200) for item in raw.get('good') or []) if text][:3]
    bad = [text for text in (_text(item, 200) for item in raw.get('bad') or []) if text][:3]
    return dict(critique=critique, good=good, bad=bad, suggestion=_text(raw.get('suggestion'), 1500))


def review_question(target, jd_context):
    """阶段2单题：题目 + 该题覆盖的逐字稿原话 → 逐题点评。截断/垃圾自动重试一次。"""
    from .model_transport import complete
    payload = dict(question=target['question'], transcript=target['transcript'], jd=jd_context or '（未提供 JD）')
    for attempt in (1, 2):
        config = get_config()
        content, _ = complete(config, _REVIEW_SYSTEM, json.dumps(payload, ensure_ascii=False),
                              temperature=.3, max_tokens=output_budget(config, REVIEW_MAX_TOKENS), timeout=300)
        try:
            return _coerce_review(_parse_json(content))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning('逐题点评返回无效（第 %s 次，%s…）：%s', attempt, target['question'][:20], exc)
    return dict(critique='本条点评生成失败，跳过。', good=[], bad=[], suggestion='')


# ---- 汇总：总体评分 / 岗位匹配 / 优点问题 / 下次行动 ----

_SUMMARY_SYSTEM = '''你是面试教练主管。根据逐题点评与岗位 JD，汇总这次真实面试的复盘总评。
overall_score 给整体表现 1-5；summary 一句话总评（最突出的优点 + 最致命的问题）；strengths 列整体亮点（至多 4 条）；problems 列整体问题（至多 4 条）；fit_score 给「面试表现与目标岗位要求的匹配度」1-5；jd_evidence 列出回答中支撑岗位匹配的证据（至多 5 条，引用原话）；jd_gaps 列出暴露的与岗位要求的差距（至多 5 条）；actions 给下次同类面试的行动清单（至多 5 条，具体可执行）。
未提供 JD 时 fit_score 按「面试官整体期待」给，jd_evidence/jd_gaps 给空数组。
只输出JSON：{"overall_score":3,"summary":"...","strengths":["..."],"problems":["..."],"fit_score":3,"jd_evidence":["..."],"jd_gaps":["..."],"actions":["..."]}'''


def _coerce_retro_summary(raw):
    """汇总软校验：列表多几条/字段超长截断保住整份报告；summary 缺失才作废重试。"""
    if not isinstance(raw, dict):
        raise ValueError('汇总不是 JSON 对象')

    def _text(value, limit):
        return str(value or '').strip()[:limit]

    def _score(value):
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 3

    summary = _text(raw.get('summary'), 2000)
    if not summary:
        raise ValueError('summary 缺失')

    def _list(key, limit):
        return [text for text in (_text(item, 200) for item in raw.get(key) or []) if text][:limit]

    return dict(overall_score=_score(raw.get('overall_score')), summary=summary,
                strengths=_list('strengths', 4), problems=_list('problems', 4),
                fit_score=_score(raw.get('fit_score')), jd_evidence=_list('jd_evidence', 5),
                jd_gaps=_list('jd_gaps', 5), actions=_list('actions', 5))


def get_config():
    """标注/拆解/点评/汇总统一走「分析」功能绑定（effective() 已含回落逻辑）。"""
    from .settings import get_purpose_config
    return get_purpose_config('analysis')


def generate_retro_report(segments, jd_context, job_meta, *, should_stop=None, on_progress=None):
    """复盘全流程：问答拆解 → 逐题并行（线程池）→ 汇总；取消在题间边界生效。

    on_progress 三段事件（SSE → 前端渐进视图）：start（题目清单，前端先铺骨架卡）
    → review×N（每条点评完成，带完整内容原位填充）→ summary（汇总中）。
    """
    from .semantic import RunCancelled
    if should_stop is not None and should_stop():
        raise RunCancelled()
    items = extract_qa(segments)
    numbered = {seg['index']: seg for seg in segments}
    targets = []
    for item in items:
        texts = []
        for index in item['segments']:
            seg = numbered.get(index)
            if seg:
                texts.append(f"[{seg['speaker']}] {seg['text']}")
        if texts:
            targets.append(dict(question=item['question'], transcript='\n'.join(texts)))
    if not targets:
        raise ValueError('没有拆出可点评的问答')
    if on_progress:
        on_progress(dict(stage='start', total=len(targets),
                         items=[dict(index=index, question=target['question']) for index, target in enumerate(targets)]))
    configs = get_active_configs()
    reviews = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=3) as pool:
        # contextvar 不跨线程：逐题点评跑在池子线程，use_config 挂在复盘 worker 上取不到，
        # 必须把生效全典显式带进任务重新挂（挂单 purpose 会让池内 get_purpose_config KeyError）。
        def _review(target):
            if should_stop is not None and should_stop():
                raise RunCancelled()
            with use_config(configs):
                return review_question(target, jd_context)
        futures = {pool.submit(_review, target): index for index, target in enumerate(targets)}
        # as_completed：谁先点评完先报进度，不等提交顺序；事件按完成序推，前端按题号原位填充。
        for done_count, future in enumerate(as_completed(list(futures)), start=1):
            index = futures[future]
            try:
                reviews[index] = future.result()
            except RunCancelled:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            if on_progress:
                on_progress(dict(stage='review', done=done_count, total=len(targets), index=index,
                                 question=targets[index]['question'], review=reviews[index]))
    if should_stop is not None and should_stop():
        raise RunCancelled()
    if on_progress:
        on_progress(dict(stage='summary', done=len(targets), total=len(targets)))
    payload = dict(job=job_meta, jd=jd_context or '（未提供 JD）',
                   reviews=[dict(question=target['question'], review=review) for target, review in zip(targets, reviews)])
    from .model_transport import complete
    for attempt in (1, 2):  # 软校验已兜住超长/多条；这里只剩真垃圾，自动重试一次再报错
        config = get_config()
        content, _ = complete(config, _SUMMARY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                              temperature=.3, max_tokens=output_budget(config, SUMMARY_MAX_TOKENS), timeout=300)
        try:
            summary = _coerce_retro_summary(_parse_json(content))
            break
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning('复盘总评返回无效（第 %s 次）：%s', attempt, exc)
            if attempt == 2:
                raise ValueError('复盘报告汇总无效，请重试') from None
    return dict(overall_score=summary['overall_score'], summary=summary['summary'],
                strengths=summary['strengths'], problems=summary['problems'],
                jd_match=dict(fit_score=summary['fit_score'], evidence=summary['jd_evidence'], gaps=summary['jd_gaps']),
                actions=summary['actions'],
                questions=[dict(question=target['question'], **review) for target, review in zip(targets, reviews)],
                jd_context_chars=len(jd_context or ''))
