"""面试备战计划（2026-11-24）：把本机已有的面试短板翻译成每天可做的一两件事。

纯规则模块：无 LLM、不联网、不改面试报告本身。数据全部来自本机 SQLite——
模拟面试报告的维度/高危清单/行动建议、失败博物馆的重复可控失分点、岗位评估缺口；
全部为空时给一份明示「通用入门」的入门计划，不假装知道用户的薄弱项。

设计见 docs/proposals/2026-11-24-interview-prep-plan-design.md。
"""
import json
import re
import uuid
from datetime import date, timedelta

from . import failure_museum
from .repository import business_today, now

DAYS_CHOICES = (7, 14, 28)
DEFAULT_DAYS = 14
MAX_FOCUS = 6
MAX_TITLE = 120
MAX_DETAIL = 600
MAX_NOTE = 500

# 无本机面试数据时fallback 的通用主题：只写「通用入门」，不冒充针对性分析。
GENERIC_FOCUS = [
    dict(topic='自我介绍 90 秒', detail='按岗位匹配点重排开场，控制在 90 秒内并留追问钩子',
         source='通用入门：本机还没有面试复盘数据，主题为通用建议', weight=0, kind_hint='drill', session_id=''),
    dict(topic='STAR 与量化表达', detail='情境一句→动作两句→结果带数字',
         source='通用入门：本机还没有面试复盘数据，主题为通用建议', weight=0, kind_hint='drill', session_id=''),
    dict(topic='岗位核心知识', detail='从目标岗位 JD 里挑 3 项硬要求，逐项准备可讲的例子',
         source='通用入门：本机还没有面试复盘数据，主题为通用建议', weight=0, kind_hint='study', session_id=''),
    dict(topic='反问环节', detail='备 3 个业务真问题（团队分工/考核方式/业务方向）',
         source='通用入门：本机还没有面试复盘数据，主题为通用建议', weight=0, kind_hint='study', session_id=''),
    dict(topic='空窗期解释', detail='准备一段有时间段、有动作、有产出的简短说明',
         source='通用入门：本机还没有面试复盘数据，主题为通用建议', weight=0, kind_hint='drill', session_id=''),
]

_PUNCT = re.compile(r'[\s，。、；：,.!！?？（）()「」【】\[\]{}<>《》\-—_/\\|]+')


def _date():
    """业务日（东八区 business_today）：与前端本地日期同口径，pipeline_today_counts 同此。"""
    return business_today()


def _key(text):
    return _PUNCT.sub('', str(text or '')).lower()


def _clip(text, limit):
    value = str(text or '').strip()
    return value[:limit]


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect_focus(repo):
    """从本机材料提取短板主题。返回 (focus, source_sessions, created_from)。"""
    focus, by_key = [], {}
    sessions_used = []
    has_report_focus = False

    def add(topic, detail, source, weight, kind_hint, session_id='', job_id='', report=False):
        nonlocal has_report_focus
        topic = _clip(topic, MAX_TITLE)
        if not topic:
            return
        if report and weight > 0:
            # 只有真的从模拟面试复盘里取到短板，才允许标成 interview_report；
            # JD 缺口/失败博物馆各有自己的来源标签，不能冒充面试报告。
            has_report_focus = True
        key = _key(topic)
        if not key:
            return
        existing = by_key.get(key)
        if existing:
            if weight > existing['weight']:
                existing['weight'] = weight
                existing['kind_hint'] = kind_hint
                if detail:
                    existing['detail'] = _clip(detail, MAX_DETAIL)
                if session_id and not existing['session_id']:
                    existing['session_id'] = session_id
                if job_id and not existing['job_id']:
                    existing['job_id'] = job_id
            if source and source not in existing['source'].split('；') and len(existing['source'].split('；')) < 3:
                existing['source'] = _clip(existing['source'] + '；' + source, MAX_DETAIL)
            return
        item = dict(topic=topic, detail=_clip(detail, MAX_DETAIL), source=_clip(source, MAX_DETAIL),
                    weight=weight, kind_hint=kind_hint, session_id=session_id, job_id=job_id)
        by_key[key] = item
        focus.append(item)

    # 1. 最近的模拟面试复盘（最多 6 场，避免一场场全表解析拖慢打开计划页）。
    for summary in repo.interview_sessions(limit=30):
        if not summary.get('has_report') or len(sessions_used) >= 6:
            continue
        session = repo.get_interview_session(summary['id'])
        report = (session or {}).get('report') or {}
        if not report:
            continue
        sessions_used.append(summary['id'])
        sid = summary['id']
        jid = session.get('job_id') or ''
        label = _clip(session.get('job_title') or summary.get('title') or '模拟面试', 40)
        for dimension in report.get('dimensions') or []:
            score = _as_number(dimension.get('score'))
            if score is None or score > 3:
                continue
            name = _clip(dimension.get('name'), 40)
            add(name, dimension.get('comment'),
                f'{label} · 维度「{name}」{score:g}/5', 2, 'study', sid, jid, report=True)
        for text in (report.get('risk_list') or [])[:4]:
            add(text, '下一场真面试前必须补的高危问题', f'{label} · 高危清单', 2, 'drill', sid, jid, report=True)
        for text in (report.get('actions') or [])[:4]:
            add(text, '复盘建议的下一动作', f'{label} · 复盘建议', 1, 'drill', sid, jid, report=True)
        missing_seen = set()
        for question in report.get('questions') or []:
            for text in question.get('missing') or []:
                topic = f'补齐：{text}'
                if _key(topic) in missing_seen:
                    continue
                missing_seen.add(_key(topic))
                add(topic, '逐题点评里指出缺失的要素', f'{label} · 逐题缺失', 1, 'drill', sid, jid, report=True)
                if len(missing_seen) >= 4:
                    break
            if len(missing_seen) >= 4:
                break

    # 2. 失败博物馆：重复出现 ≥2 次的可控失分点权重最高——它比单场报告更能说明问题。
    recurring = failure_museum.build_pattern_report(repo.interview_attributions()).get('recurring') or []
    for item in recurring[:4]:
        add(item['label'], item.get('advice'),
            f'失败博物馆 · 重复失分 {item.get("count", 0)} 次', 3, 'drill')

    # 3. 岗位评估缺口：最近有报告的岗位，每个最多 2 条；缺口是「未知」不是「不会」。
    jd_used = 0
    for job in repo.jobs()[:20]:
        if jd_used >= 3:
            break
        _, report = repo.list_reports(job['id'])
        gaps = (report or {}).get('gaps') or []
        for text in gaps[:2]:
            add(text, '评估里标为未知的岗位要求，核实后再决定要不要补',
                f'{job.get("company", "")} · {job.get("title", "")} 的评估缺口', 1, 'study')
            jd_used += 1
            if jd_used >= 3:
                break

    if focus:
        focus.sort(key=lambda item: (-item['weight'], -len(item['source'])))
        return focus[:MAX_FOCUS], sessions_used, ('failure_museum' if any(item['weight'] == 3 for item in focus)
                                                   else 'interview_report' if has_report_focus else 'jd_gaps')

    # 4. 完全无数据：通用入门，明示来源，绝不冒充针对性分析。
    return [dict(item) for item in GENERIC_FOCUS], [], 'generic'


def _make_task(plan_id, day_index, day, kind, title, detail, topic, minutes, required, source, session_id='', job_id=''):
    return dict(id='T-' + uuid.uuid4().hex[:20], plan_id=plan_id, day_index=day_index, date=day,
                kind=kind, title=_clip(title, MAX_TITLE), detail=_clip(detail, MAX_DETAIL),
                topic=_clip(topic, 80), minutes=minutes, required=1 if required else 0,
                source=_clip(source, MAX_DETAIL), session_id=session_id, job_id=job_id,
                status='pending', note='', completed_at='')


def normalize_prefs(prefs=None):
    """自定义参数护栏：UI 传什么都不能把计划搞坏，缺省保持旧行为。"""
    prefs = prefs or {}
    try:
        minutes = int(prefs.get('minutes_per_day') or 20)
    except (TypeError, ValueError):
        minutes = 20
    return dict(goal=_clip(prefs.get('goal'), 300), job_id=_clip(prefs.get('job_id'), 100),
                minutes_per_day=max(10, min(120, minutes)),
                use_llm=bool(prefs.get('use_llm', True)))


def _budget_minutes(budget, kind):
    """按每日预算档给任务估时；生成与对话调整共用同一口径，改预算不会出现两套时长。"""
    if kind == 'mock':
        return 18
    if kind == 'review':
        return 10
    if budget <= 20:
        return 12 if kind == 'drill' else 15
    if budget <= 40:
        return 18 if kind == 'drill' else 20
    return 25


def _tasks_for(plan, focus):
    """按计划头与短板清单生成全部任务；生成与空计划自愈共用，保证进度口径一致。

    focus 里的 drill 是模型给的具体练法（规则版没有）：有就写进练法任务的标题与详情，
    让任务真的长成「为这个人定制的动作」，而不是永远一句通用 STAR 模板。
    """
    if not focus:
        focus = GENERIC_FOCUS
    prefs = plan.get('prefs') or {}
    diagnosis = str(plan.get('diagnosis') or '')
    budget = prefs.get('minutes_per_day') or 20
    tasks = []
    start = date.fromisoformat(plan['start_date'])
    for index in range(plan['days']):
        day_index = index + 1
        day = (start + timedelta(days=index)).isoformat()
        item = focus[index % len(focus)]
        topic = item['topic']
        source = item['source']
        drill = str(item.get('drill') or '').strip()
        sid = item.get('session_id') or ''
        jid = item.get('job_id') or prefs.get('job_id') or ''
        mock_detail = (f'开一场 6–8 题的模拟面试并读完复盘报告；专项考察「{topic}」，答完对照报告里这一项的点评。'
                       + (f' 当日专项练法：{drill}' if drill else '') + ' 复盘生成后这一项会自动打卡。')
        tasks.append(_make_task(
            plan['id'], day_index, day, 'mock', f'完成一场模拟面试：{topic}',
            mock_detail, topic, _budget_minutes(budget, 'mock'), True, source, sid, jid))
        if (item.get('kind_hint') or item.get('kind')) == 'study':
            tasks.append(_make_task(
                plan['id'], day_index, day, 'study',
                drill[:40] if drill else f'补一个知识点：{topic}',
                (drill + ' 产出一份能回看的笔记或例子。') if drill else
                f'找 1 份资料或笔记，围绕「{topic}」写 200 字要点 + 1 个自己能讲出来的例子。',
                topic, _budget_minutes(budget, 'study'), False, source, sid, jid))
        else:
            tasks.append(_make_task(
                plan['id'], day_index, day, 'drill',
                drill[:40] if drill else f'练一版表达：{topic}',
                (drill + ' 做完回听一遍，确认自己能讲清楚。') if drill else
                f'把「{topic}」按 STAR 重写一版，补上 2 个数字；90 秒内讲完并回听一遍。',
                topic, _budget_minutes(budget, 'drill'), True, source, sid, jid))
        if day_index % 7 == 0:
            review_detail = '回看这周的模拟面试报告，勾出仍然薄弱的 1 点；下一轮专项会优先练它。'
            if diagnosis:
                review_detail += f' 对照教练诊断：{diagnosis[:160]}'
            tasks.append(_make_task(
                plan['id'], day_index, day, 'review', '本周复盘：挑出还卡的一点',
                review_detail, topic, _budget_minutes(budget, 'review'), False,
                '每周一次的回看，不要求全做完'))
    return tasks


def _emit(on_event, kind, payload):
    """进度回调的护栏：回调异常只丢事件，绝不打断生成本身。"""
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception:
        pass


def build_plan(repo, days=DEFAULT_DAYS, prefs=None, allow_llm=True, on_event=None):
    """生成计划 + 全部任务：模型负责诊断与练法，规则负责排期；失败退规则版。

    on_event 提供时逐阶段外发进度（focus → model_start → diagnosis_delta… →
    model_done|model_fallback → focus_item… → scheduled），供流式运行推送 SSE。
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    if days not in DAYS_CHOICES:
        days = DEFAULT_DAYS
    prefs = normalize_prefs(prefs)
    focus, sessions, created_from = collect_focus(repo)
    _emit(on_event, 'focus', dict(focus=focus, created_from=created_from, source_sessions=sessions))
    diagnosis, origin = '', 'rules'
    if allow_llm and prefs['use_llm']:
        from . import prep_plan_llm
        _emit(on_event, 'model_start', dict())
        # 只有流式运行（on_event 提供时）才走流式模型调用；同步路径保持非流式，便于测试与低延迟。
        result = prep_plan_llm.analyze(
            repo, focus, prefs,
            on_delta=(lambda piece: _emit(on_event, 'diagnosis_delta', dict(text=piece))) if on_event else None)
        if result:
            focus, diagnosis, origin = result['focus'], result['diagnosis'], 'model'
            _emit(on_event, 'model_done', dict(diagnosis=diagnosis, count=len(focus)))
            for index, item in enumerate(focus):
                _emit(on_event, 'focus_item', dict(index=index, item=item))
        else:
            _emit(on_event, 'model_fallback', dict(message='模型分析没有成功，已按本机材料规则排期'))
    start = date.fromisoformat(_date())
    plan_id = 'P-' + uuid.uuid4().hex[:20]
    plan = dict(id=plan_id, status='active', days=days, start_date=start.isoformat(),
                end_date=(start + timedelta(days=days - 1)).isoformat(),
                created_from=created_from, focus=focus, source_sessions=sessions,
                origin=origin, diagnosis=diagnosis, prefs=prefs, created_at=now())
    tasks = _tasks_for(plan, focus)
    _emit(on_event, 'scheduled', dict(days=days, task_count=len(tasks)))
    # 头与任务同一事务：任一步失败都不留空壳计划（历史上缺列导致过「0/0」）。
    repo.save_prep_plan_with_tasks(plan, tasks)
    return plan


def idempotent_plan(repo, prefs=None, allow_llm=True):
    """同日同数据已生成的计划（含「规则版可升级为模型版」判断）：可复用返回它，否则 None。

    流式入口用它决定走同步返回还是起后台 run，与 generate() 共用同一份判断，避免两处漂移。
    """
    prefs = normalize_prefs(prefs)
    existing = repo.active_prep_plan()
    if not (existing and existing.get('created_at', '')[:10] == _date()):
        return None
    wanted = collect_focus(repo)[1]
    same_prefs = normalize_prefs(existing.get('prefs')) == prefs
    if set(wanted) != set(existing.get('source_sessions') or []) or not same_prefs:
        return None
    # 之前规则兜底、现在模型可用：允许用户按最新配置升级为 AI 定制版。
    # 模型仍不可用时必须幂等，否则每次打开页面/重复请求都会新建一份计划。
    if allow_llm and prefs['use_llm'] and existing.get('origin') != 'model':
        from .settings import purpose_configured
        if purpose_configured('analysis'):
            return None
    return existing


def generate(repo, days=DEFAULT_DAYS, prefs=None, allow_llm=True, on_event=None, force=False):
    """生成入口：同日同数据重复调用返回原计划，不产出第二份；force=True 跳过幂等（对话调整里用户确认的整轮重排）。"""
    prefs = normalize_prefs(prefs)
    if not force:
        existing = idempotent_plan(repo, prefs, allow_llm)
        if existing is not None:
            return existing
    plan = build_plan(repo, days, prefs, allow_llm, on_event=on_event)
    repo.archive_prep_plans(except_id=plan['id'])
    return plan


_PREP_KINDS = ('mock', 'study', 'drill', 'review')


def apply_adjustments(repo, plan, changes):
    """执行模型给出的结构化修改指令；逐条护栏校验，坏的跳过并写明理由。

    返回 (applied, skipped)。rebuild 不在这里执行——它由 run 层转成 confirm_required，
    用户确认后走 force 重新生成；这里只会收到它被过滤后的剩余指令。
    """
    applied, skipped = [], []
    prefs = dict(plan.get('prefs') or {})
    plan_changed = False

    for change in changes or []:
        action = str(change.get('action') or '')
        if action not in ('set_prefs', 'edit_task', 'remove_task', 'add_task'):
            # rebuild 已被 run 层拦截，到这里同样不执行；其余未知类型跳过。
            skipped.append(dict(action=action, note='整轮重排需要你确认后才会执行' if action == 'rebuild' else '未知的修改类型'))
            continue
        if action == 'set_prefs':
            touched = []
            if change.get('minutes_per_day') is not None:
                try:
                    minutes = int(change['minutes_per_day'])
                except (TypeError, ValueError):
                    minutes = None
                if minutes is not None and 10 <= minutes <= 120:
                    if minutes != prefs.get('minutes_per_day'):
                        prefs['minutes_per_day'] = minutes
                        touched.append(f'每日预算改为 {minutes} 分钟')
            if 'goal' in change and change['goal'] is not None:
                goal = _clip(change.get('goal'), 300)
                if goal != prefs.get('goal'):
                    prefs['goal'] = goal
                    touched.append('备战重点已更新')
            if 'job_id' in change and change['job_id'] is not None:
                job_id = _clip(change.get('job_id'), 100)
                if job_id != prefs.get('job_id'):
                    prefs['job_id'] = job_id
                    touched.append('目标岗位已更新')
            if touched:
                plan_changed = True
                applied.append(dict(action=action, summary='，'.join(touched)))
                # 预算变了就按同一口径重估全部 pending 的专项任务时长（mock/复盘固定）。
                if any('预算' in item for item in touched):
                    for task in repo.prep_tasks(plan['id']):
                        if task['status'] == 'pending' and task['kind'] in ('drill', 'study'):
                            minutes = _budget_minutes(prefs['minutes_per_day'], task['kind'])
                            if minutes != task['minutes']:
                                repo.edit_prep_task(task['id'], {'minutes': minutes})
                                applied.append(dict(action=action, task_id=task['id'],
                                                    summary=f'「{task["title"][:20]}」时长调整为约 {minutes} 分钟'))
            else:
                skipped.append(dict(action=action, note='没有产生任何变化'))
            continue

        if action == 'add_task':
            try:
                day_index = int(change.get('day_index'))
            except (TypeError, ValueError):
                skipped.append(dict(action=action, note='缺少有效的 day_index'))
                continue
            if not 1 <= day_index <= plan['days']:
                skipped.append(dict(action=action, note=f'day_index 超出计划范围 1–{plan["days"]}'))
                continue
            title = _clip(change.get('title'), MAX_TITLE)
            if not title:
                skipped.append(dict(action=action, note='新任务必须有标题'))
                continue
            kind = change.get('kind') if change.get('kind') in _PREP_KINDS else 'drill'
            try:
                minutes = int(change.get('minutes') or _budget_minutes(prefs.get('minutes_per_day') or 20, kind))
            except (TypeError, ValueError):
                minutes = _budget_minutes(prefs.get('minutes_per_day') or 20, kind)
            minutes = max(5, min(120, minutes))
            day = (date.fromisoformat(plan['start_date']) + timedelta(days=day_index - 1)).isoformat()
            row = dict(id='T-' + uuid.uuid4().hex[:20], plan_id=plan['id'], day_index=day_index, date=day,
                       kind=kind, title=title, detail=_clip(change.get('detail'), MAX_DETAIL),
                       topic=_clip(change.get('topic') or title, 80), minutes=minutes,
                       required=1 if change.get('required') else 0,
                       source='对话调整：你与教练的修改约定', session_id='',
                       job_id=prefs.get('job_id') or '', status='pending', note='', completed_at='')
            repo.save_prep_tasks([row])
            applied.append(dict(action=action, task_id=row['id'],
                                summary=f'第 {day_index} 天新增「{title[:24]}」'))
            continue

        # edit/remove 才需要落到具体任务上。
        task_id = _clip(change.get('task_id'), 60)
        task = repo.prep_task(task_id) if task_id else None
        if task is None or task['plan_id'] != plan['id']:
            skipped.append(dict(action=action, task_id=task_id, note='任务不存在或不属于当前计划'))
            continue
        if action == 'edit_task':
            if task['status'] != 'pending':
                skipped.append(dict(action=action, task_id=task_id, note='已完成/已跳过的任务不再改动'))
                continue
            fields = {}
            if change.get('title'):
                fields['title'] = _clip(change.get('title'), MAX_TITLE)
            if change.get('detail'):
                fields['detail'] = _clip(change.get('detail'), MAX_DETAIL)
            if change.get('kind') in _PREP_KINDS:
                fields['kind'] = change['kind']
            if change.get('minutes') is not None:
                try:
                    minutes = int(change['minutes'])
                    if 5 <= minutes <= 120:
                        fields['minutes'] = minutes
                except (TypeError, ValueError):
                    pass
            if change.get('required') is not None:
                fields['required'] = 1 if change['required'] else 0
            if not fields:
                skipped.append(dict(action=action, task_id=task_id, note='没有可用的修改字段'))
                continue
            updated = repo.edit_prep_task(task_id, fields)
            applied.append(dict(action=action, task_id=task_id,
                                summary=f'已修改「{updated["title"][:24]}」'))
        elif action == 'remove_task':
            if task['status'] != 'pending':
                skipped.append(dict(action=action, task_id=task_id, note='已完成/已跳过的任务不删除，打卡记录要保留'))
                continue
            repo.delete_prep_task(task_id)
            applied.append(dict(action=action, task_id=task_id,
                                summary=f'已删除「{task["title"][:24]}」'))

    if plan_changed:
        plan = dict(plan, prefs=prefs)
        repo.save_prep_plan(plan)
    return applied, skipped


def _streak(tasks):
    """连续天数：以「当天有 ≥1 个必做任务完成」为准；今天未完成从昨天起算，不打断当天。"""
    done_days = {task['completed_at'][:10] for task in tasks
                 if task['status'] == 'done' and task['required'] and task['completed_at']}
    if not done_days:
        return 0, 0
    cursor = date.fromisoformat(_date())
    if cursor.isoformat() not in done_days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.isoformat() in done_days:
        streak += 1
        cursor -= timedelta(days=1)
    best = run = 0
    previous = None
    for value in sorted(done_days):
        current = date.fromisoformat(value)
        run = run + 1 if previous and (current - previous).days == 1 else 1
        best = max(best, run)
        previous = current
    return streak, best


def snapshot(repo):
    """GET 载荷：当前计划 + 今天要做的 + 折叠周视图 + 进度 + 历史摘要。"""
    plan = repo.active_prep_plan()
    today = _date()
    if plan is None:
        return dict(plan=None, today=dict(date=today, tasks=[], done=0, required_total=0, required_done=0, overdue=[]),
                    week=[], progress=dict(total=0, done=0, skipped=0, required_total=0, required_done=0,
                                           rate=0, streak=0, best_streak=0, last_done_at=''),
                    history=[{'id': item['id'], 'status': item['status'], 'days': item['days'],
                              'start_date': item['start_date'], 'created_from': item['created_from']}
                             for item in repo.prep_plans(10)], generated_at=now())

    tasks = repo.prep_tasks(plan['id'])
    if not tasks:
        # 历史空壳计划（旧库缺 job_id 列导致任务整批写失败）：按计划里已存好的短板幂等补齐。
        # 不重新 collect_focus，避免已展示的来源/文案悄悄变样；此处不触碰任何已有打卡记录。
        repo.save_prep_tasks(_tasks_for(plan, plan.get('focus') or []))
        tasks = repo.prep_tasks(plan['id'])
    overdue = [dict(task, overdue=True) for task in tasks
               if task['date'] < today and task['status'] == 'pending'][:2]
    today_tasks = [dict(task, overdue=False) for task in tasks if task['date'] == today]
    done_required = sum(1 for task in tasks if task['required'] and task['status'] == 'done')
    required_total = sum(1 for task in tasks if task['required'])
    done = sum(1 for task in tasks if task['status'] == 'done')
    skipped = sum(1 for task in tasks if task['status'] == 'skipped')
    streak, best = _streak(tasks)
    last_done = max((task['completed_at'] for task in tasks
                     if task['status'] == 'done' and task['completed_at']), default='')
    week = []
    for index in range(plan['days']):
        day = (date.fromisoformat(plan['start_date']) + timedelta(days=index)).isoformat()
        day_tasks = [task for task in tasks if task['date'] == day]
        if day_tasks:
            week.append(dict(day_index=index + 1, date=day,
                             total=len(day_tasks),
                             done=sum(1 for task in day_tasks if task['status'] == 'done'),
                             tasks=[dict(id=task['id'], kind=task['kind'], title=task['title'],
                                         status=task['status'], required=task['required']) for task in day_tasks]))
    return dict(
        plan=plan,
        today=dict(date=today, tasks=today_tasks, overdue=overdue,
                   done=sum(1 for task in today_tasks if task['status'] == 'done'),
                   required_total=sum(1 for task in today_tasks if task['required']),
                   required_done=sum(1 for task in today_tasks if task['required'] and task['status'] == 'done')),
        week=week,
        progress=dict(total=len(tasks), done=done, skipped=skipped,
                      required_total=required_total, required_done=done_required,
                      rate=round(done / len(tasks), 4) if tasks else 0,
                      streak=streak, best_streak=best, last_done_at=last_done),
        history=[{'id': item['id'], 'status': item['status'], 'days': item['days'],
                  'start_date': item['start_date'], 'created_from': item['created_from']}
                 for item in repo.prep_plans(10)],
        generated_at=now())


def set_task_status(repo, task_id, status, note=None):
    """任务状态流转；不存在的 id 返回 None，由路由层映射 404。"""
    if status == 'done' and note is not None:
        note = _clip(note, MAX_NOTE)
    return repo.update_prep_task(task_id, status, note)


def auto_complete_mock(repo, session_id):
    """模拟面试复盘生成成功后自动打卡当日必做的 mock 任务。

    只碰 active 计划、只碰当天、只碰一条 pending；任何异常都被调用方吞掉，
    绝不因为打卡失败而让一场面试的复盘报错。
    """
    plan = repo.active_prep_plan()
    if not plan:
        return False
    today = _date()
    for task in repo.prep_tasks_for_date(plan['id'], today):
        if task['kind'] == 'mock' and task['required'] and task['status'] == 'pending':
            repo.update_prep_task(task['id'], 'done', f'模拟面试 {session_id} 复盘完成，自动打卡')
            return True
    return False


def archive(repo, plan_id):
    return repo.archive_prep_plan(plan_id)
