"""错题强化（2026-11-24）：把既往复盘里答得不好的题（错题）捞出来，定向重考。

与备战计划的「主题级专项」互补：那边是一个短板主题 → weak_focus → 题单里至少
1/3 的题围绕它；这里是「题目级」——每道错题出一道变式题，面试官按「上次失分点」
追问，复盘逐题对照上次。模拟面试复盘与真实面试复盘一视同仁，同一道题跨场合并。

纯确定性模块（无 LLM、不改任何报告）：
- 错题 = 规则抽取 + 跨场合并 + 失分要素标签。「答得不好」只认复盘里的结构化证据
  （模拟面试的 missing、真实面试的 bad），不读点评正文做情绪判断。
- 重练结果只做计数对照（缺失要素 上次 N 条 → 本次 M 条），不下「已掌握」的结论：
  点评模型对几乎每个回答都会点出可改进处，「补齐」不等于「过关」。
- 错题强化场次自己产生的题不进错题本：它的结果记回原错题的重练状态，否则每重练
  一次就多出一条变式题，错题本越练越长。
"""
import hashlib
import re

MAX_DRILLS = 12          # 一场最多重练的错题数（题单上限同 QuestionPlan）
MAX_ITEMS = 200          # 错题本条数上限（按最近出现截断）：每份复盘 2~9 题，够回看二十几场
MAX_PROBLEMS = 6         # 合并后每题保留的失分点条数
PROBLEM_CHARS = 120      # 单条失分点截断：真实复盘的 bad 一条常 60~120 字
ADVICE_CHARS = 1200      # 高分示范/改进建议截断（出题与复盘对照用，面试官 prompt 不带）
CRITIQUE_CHARS = 400     # 上次点评摘录截断（复盘对照用）
LIST_CRITIQUE_CHARS = 200  # 错题本列表里的点评摘录更短（只供扫读）
MERGE_PREFIX = 20        # 同题合并：归一化后公共前缀达到此长度即视为同一道题
MIN_PATTERN_COUNT = 2    # 失分要素至少在 2 道错题里出现才进「跨场通病」

SOURCE_NAMES = {'mock': '模拟面试', 'real': '真实面试'}
PLACEHOLDER_CRITIQUE = '本条点评生成失败'

# 失分要素标签：对复盘写下的失分点做关键词归类，只用于错题本的扫读筛选与给面试官的
# 「跨场通病」提示；命不中的失分点原样保留，不因未归类而丢弃。
# 关键词按生产复盘逐条核对过命中片段（2026-11-25）：裸「深度」「岗位」「产出」误命中多
# （「参与深度」「对 5-10 年岗位来说」「产出多少候选片段」说的都是缺量化），已收窄。
PROBLEM_TAGS = [
    dict(id='quant', label='缺量化', pattern=r'量化|数字|指标|口径'),
    dict(id='action', label='个人动作不清', pattern=r'动作|亲手|亲自|个人贡献|职责|负责|角色|owner|参与'),
    dict(id='result', label='缺结果', pattern=r'结果|收益|成效|效果|改善'),
    dict(id='star', label='STAR 不全', pattern=r'STAR|背景S|任务T|结果R|情境|流水账'),
    dict(id='basis', label='缺依据与取舍', pattern=r'依据|压测|阈值|怎么定|怎么算|测算|取舍|优先级|拍脑袋|为什么选|理由'),
    dict(id='failure', label='异常路径缺失', pattern=r'失败|兜底|降级|重试|回滚|熔断|限流|异常|故障'),
    dict(id='structure', label='表达结构散', pattern=r'罗列|清单|平铺|主线|因果|条理|重点淹没'),
    dict(id='vague', label='含糊或存疑', pattern=r'含糊|模糊|存疑|真实性|露底|击穿|空泛'),
    dict(id='depth', label='细节深度不足', pattern=r'细节|深度不足|深度缺失|黑盒|一笔带过|没展开|未展开'),
    dict(id='jd', label='未勾连岗位', pattern=r'JD|勾连|匹配度'),
]
_TAG_PATTERNS = [(tag['id'], re.compile(tag['pattern'], re.IGNORECASE)) for tag in PROBLEM_TAGS]
_TAG_LABELS = {tag['id']: tag['label'] for tag in PROBLEM_TAGS}

_PUNCT = re.compile(r'[\s，。、；：,.!！?？（）()「」『』“”"\'【】\[\]{}<>《》\-—_/\\|]+')


def _norm(text):
    """归一化（只服务「是不是同一道题/同一条失分点」的比对）：去标点空白、转小写。"""
    return _PUNCT.sub('', str(text or '')).lower()


def _clip(text, limit):
    return ' '.join(str(text or '').split())[:limit]


def _int(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def review_failed(review):
    """逐题点评两次都坏后的占位：此时缺失要素为空只是没点评出来，不能当成「补齐了」。"""
    return str((review or {}).get('critique') or '').startswith(PLACEHOLDER_CRITIQUE)


def problem_tags(texts):
    """失分点命中的失分要素标签（按 PROBLEM_TAGS 顺序）——关键词归类的扫读线索，不是判定。"""
    blob = '\n'.join(str(text or '') for text in texts)
    return [tag_id for tag_id, pattern in _TAG_PATTERNS if pattern.search(blob)]


def habit_note(items, patterns):
    """跨场通病一句话（给面试官与出题官）：只列在多道错题里反复出现的失分要素。"""
    patterns = [item for item in patterns or [] if item.get('count', 0) >= MIN_PATTERN_COUNT]
    if not patterns or not items:
        return ''
    return '；'.join(f"{item['label']}（{item['count']}/{len(items)} 道错题）" for item in patterns[:4])


def brief(problems, limit=3, chars=40):
    """面试官 prompt 里的「上次失分点」压缩行：每轮对话都带，必须小——只取前几条、逐条截短。"""
    return '；'.join(_clip(text, chars) for text in (problems or [])[:limit])


# ---- 抽取：复盘报告 → 逐题记录 ----

def question_problems(review, source):
    """一道题的失分点清单：模拟面试复盘是 missing，真实面试复盘是 bad；点评失败的占位不算。"""
    if review_failed(review):
        return []
    key = 'bad' if source == 'real' else 'missing'
    return [text for text in (_clip(item, PROBLEM_CHARS) for item in (review or {}).get(key) or []) if text]


def _records(repo):
    """错题本的数据源：带复盘报告的场次与逐字稿快照（没有报告就没有逐题点评，跳过）。"""
    sessions = [repo.get_interview_session(item['id']) for item in repo.interview_sessions() if item.get('has_report')]
    transcripts = [repo.get_interview_transcript(item['id']) for item in repo.interview_transcripts() if item.get('has_report')]
    return ([('mock', record) for record in sessions if record]
            + [('real', record) for record in transcripts if record])


def _practice_index(records):
    """重练记录：错题强化场次的复盘里，每道题都记着它重练的是哪道原错题（drill_ref）。

    key = 原错题引用 (source, session_id, index)；同一条被重练多次取最近一次，count 记总次数。
    """
    result = {}
    for _source, record in records:
        report = record.get('report') or {}
        at = report.get('generated_at') or ''
        for review in report.get('questions') or []:
            ref = review.get('drill_ref') if isinstance(review, dict) else None
            if not isinstance(ref, dict) or not ref.get('session_id') or review_failed(review):
                continue
            key = (ref.get('source', 'mock'), ref['session_id'], _int(ref.get('index')))
            if key not in result or at >= result[key]['at']:
                result[key] = dict(at=at, before=len(review.get('previous_problems') or []),
                                   after=len(review.get('missing') or []), count=0)
            result[key]['count'] += 1
    return result


def _occurrences(records, practices):
    """逐题记录。index 是 report.questions 的下标——前端「重练此题」的引用以此为准。"""
    for source, record in records:
        if (record.get('material') or {}).get('drills'):
            continue  # 错题强化场次：结果已记回原错题（_practice_index），自己的变式题不再当新错题
        report = record.get('report') or {}
        company, job_title = record.get('company', '') or '', record.get('job_title', '') or ''
        base = dict(source=source, session_id=record.get('id', ''), job_id=record.get('job_id', '') or '',
                    job_title=job_title, company=company,
                    label=' · '.join(part for part in (company, job_title) if part),
                    at=report.get('generated_at') or record.get('created_at', ''))
        for index, review in enumerate(report.get('questions') or []):
            if not isinstance(review, dict):
                continue
            occurrence = dict(
                base, index=index, question=_clip(review.get('question'), 300), topic=_clip(review.get('topic'), 40),
                problems=question_problems(review, source),
                critique='' if review_failed(review) else _clip(review.get('critique'), CRITIQUE_CHARS),
                advice=_clip(review.get('model_answer') or review.get('suggestion'), ADVICE_CHARS))
            occurrence['practice'] = practices.get((source, record.get('id', ''), index))
            yield occurrence


# ---- 合并：同一道题跨场归并 ----

def _same_question(first, second):
    if not first or not second:
        return False
    if first == second:
        return True
    # 同一道题在两份复盘里被截短/续长（真实逐字稿两次导入的追问归并差异）：公共前缀够长即同题。
    return min(len(first), len(second)) >= MERGE_PREFIX and (first.startswith(second) or second.startswith(first))


def _groups(occurrences):
    """新→旧遍历，组首即这道题最近一次出现。"""
    groups = []
    for occurrence in sorted(occurrences, key=lambda item: item['at'], reverse=True):
        norm = _norm(occurrence['question'])
        group = next((item for item in groups if _same_question(norm, item['norm'])), None) if norm else None
        if group is None:
            groups.append(dict(norm=norm, items=[occurrence]))
        else:
            group['items'].append(occurrence)
    return groups


def _merge(group):
    items = group['items']
    head = items[0]                      # 最近一次出现：题面、来源场次、时间以它为准
    weak = [item for item in items if item['problems']]
    lead = weak[0] if weak else head     # 最近一次被点出失分点的记录：点评与建议以它为准
    problems, seen = [], set()
    for item in items:
        for text in item['problems']:
            key = _norm(text)[:40]
            if key and key not in seen:
                seen.add(key)
                problems.append(text)
    practices = [item['practice'] for item in items if item.get('practice')]
    return dict(
        id='W-' + hashlib.sha1(group['norm'][:60].encode('utf-8')).hexdigest()[:16],
        question=head['question'], topic=head['topic'], problems=problems[:MAX_PROBLEMS],
        critique=lead['critique'], advice=lead['advice'],
        source=head['source'], session_id=head['session_id'], label=head['label'],
        job_id=head['job_id'], job_title=head['job_title'], company=head['company'],
        tags=problem_tags(problems), occurrences=len(weak), last_at=head['at'], weak=bool(weak),
        practice=max(practices, key=lambda item: item['at'], default=None),
        practice_count=sum(item['count'] for item in practices),
        refs=[dict(source=item['source'], session_id=item['session_id'], index=item['index']) for item in items])


def _collect(repo):
    records = _records(repo)
    return [_merge(group) for group in _groups(_occurrences(records, _practice_index(records)))]


def _ref_key(ref):
    return (ref.get('source'), ref.get('session_id'), _int(ref.get('index')))


# ---- 对外：错题本 / 重练目标 ----

_LIST_FIELDS = ('id', 'question', 'topic', 'problems', 'source', 'session_id', 'label', 'job_id', 'job_title',
                'company', 'tags', 'occurrences', 'last_at', 'practice', 'practice_count', 'refs')


def _patterns(items):
    """跨场通病：各失分要素出现在多少道错题里（至少 MIN_PATTERN_COUNT 道才算通病），多的在前。"""
    patterns = [dict(tag=tag_id, label=label, count=count) for tag_id, label in _TAG_LABELS.items()
                if (count := sum(1 for item in items if tag_id in item['tags'])) >= MIN_PATTERN_COUNT]
    return sorted(patterns, key=lambda item: (-item['count'], item['tag']))


def weak_points(repo):
    """错题本（GET 载荷）：所有复盘里被点了失分点的题，跨场合并，最近的在前。

    只读既往报告做规则统计，不调模型——打开选错题面板即出。条数超过 MAX_ITEMS 时
    只留最近的并在 limitations 里明示，不假装列全了。
    """
    items = [item for item in _collect(repo) if item['weak']]
    items.sort(key=lambda item: item['last_at'], reverse=True)
    patterns = _patterns(items)
    listed = [dict({key: item[key] for key in _LIST_FIELDS}, critique=_clip(item['critique'], LIST_CRITIQUE_CHARS))
              for item in items[:MAX_ITEMS]]
    limitations = []
    if len(items) > MAX_ITEMS:
        limitations.append(f'共 {len(items)} 道错题，这里只列出最近的 {MAX_ITEMS} 道')
    return dict(items=listed, patterns=patterns, limitations=limitations, total=len(items),
                tags=[dict(id=tag['id'], label=tag['label']) for tag in PROBLEM_TAGS])  # 前端把题上的 tag id 翻译成标签名


def snapshot(item):
    """落进场次材料快照的重练目标：只带出题/面试官/复盘对照要用的字段，来源记录之后被删也不影响本场。"""
    return dict(id=item['id'], ref=item['refs'][0], question=item['question'], topic=item['topic'],
                problems=item['problems'], advice=item['advice'], critique=item['critique'],
                source=item['source'], session_id=item['session_id'], label=item['label'], job_id=item['job_id'])


def prepare(repo, refs):
    """建场入口的取数：错题引用 → 重练目标快照（合并后去重，保持请求顺序）+ 跨场通病一句话。

    引用指向的题即使没有被点过失分点也放行——用户点「重练此题」即是明示要练这道题；
    记录已删除/下标越界的引用丢弃，全部无效时返回空列表，由路由层映射 400。
    """
    items = _collect(repo)
    lookup = {_ref_key(ref): item for item in items for ref in item['refs']}
    drills, seen = [], set()
    for ref in refs or []:
        item = lookup.get(_ref_key(ref.model_dump() if hasattr(ref, 'model_dump') else ref))
        if item is None or item['id'] in seen:
            continue
        seen.add(item['id'])
        drills.append(snapshot(item))
    weak = [item for item in items if item['weak']]
    return dict(drills=drills, habit_note=habit_note(weak, _patterns(weak)))


# ---- 出题与复盘：题单对齐 / 兜底 / 对照 ----

def fallback_question(drill, index):
    """规则兜底题：出题模型失败或漏题时原题重问，追问直接对准上次失分点——保证每道错题都有题可练。"""
    problems = drill.get('problems') or []
    lead = [_clip(text, 40) for text in problems[:3]]
    return dict(
        topic='错题重练', question=_clip(drill.get('question'), 600),
        focus=('上次失分点：' + '；'.join(lead))[:300] if lead else '巩固这道题的回答，讲清背景、动作与结果',
        followups=[f'{text}——具体说说' for text in lead] or ['能展开讲讲吗？', '具体是你做的哪部分？'],
        source='weakness', drill=index)


def ensure_coverage(questions, drills):
    """题单与错题对齐：每道错题至少一道题（漏了的补兜底题），drill 下标越界的题丢弃。

    模型漏标 drill 但题数与错题数对得上时按序对应——变式题是好的，不该因为漏了个下标就整单作废。
    """
    def _valid(item):
        index = item.get('drill')
        return index if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(drills) else None
    valid = [{**item, 'drill': index} for item in questions if (index := _valid(item)) is not None]
    if not valid and len(questions) == len(drills):
        valid = [{**item, 'drill': index} for index, item in enumerate(questions)]
    result = valid[:MAX_DRILLS]
    covered = {item['drill'] for item in result}
    for index in range(len(drills)):
        if index not in covered and len(result) < MAX_DRILLS:
            result.append(fallback_question(drills[index], index))
    return result


def attach_previous(segments, drills):
    """给错题强化场次的片段挂上「上次失分档案」（复盘逐题对照上次的依据）。不改原片段，返回新列表。"""
    result = []
    for segment in segments:
        index = segment.get('drill')
        target = drills[index] if isinstance(index, int) and 0 <= index < len(drills) else None
        result.append(dict(segment, previous=target) if target else segment)
    return result


def compare_stats(report):
    """错题强化场次的复盘对照（纯计数）：重练了几道、几道本次没再被点出缺失要素、缺失要素 上次→本次。

    点评失败的题不计入——它的缺失要素为空只是没点评出来。没有错题对照的场次返回 None。
    """
    rows = [review for review in (report or {}).get('questions') or []
            if isinstance(review, dict) and review.get('drill_ref') and not review_failed(review)]
    if not rows:
        return None
    return dict(total=len(rows), cleared=sum(1 for review in rows if not review.get('missing')),
                before=sum(len(review.get('previous_problems') or []) for review in rows),
                after=sum(len(review.get('missing') or []) for review in rows))
