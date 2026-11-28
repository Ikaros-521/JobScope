"""行动处方引擎（2026-11-22）：漏斗数据 → 可执行动作。规则版，无 LLM、确定性、可测。

闭环断点在「反馈 → 下一步行动」：漏斗/周报已经能定位弱点，但只呈现不开药。
本模块把同一份本地统计翻译成 3–6 张处方卡（每张：现象+数据依据+具体动作+跳转），
外加今日行动清单（投递目标 + 停滞跟进数）。规则选取「当前转化率最低的环节」
这类排序型结论而不设好坏阈值——数字自己说话，指路不施压（同收藏页回顾口径）。
样本不足时如实标注参考有限，不硬编处方。深度复盘仍由全局对话 Agent 的
「投递复盘」卡承接；这里是每天打开就能看到的行动层。
"""
from datetime import datetime, timezone

from .repository import business_today, now

TRACKED_ACTIVE = ('interested', 'applied', 'interviewing')
# 与收藏页「超 N 天未跟进」同一口径，改这里两处一起生效。
FOLLOW_UP_STALE_DAYS = 14
MIN_APPLIED_FOR_DIAGNOSIS = 5

STAGE_NAMES = {'interested': '意向', 'applied': '已投递', 'interviewing': '面试中', 'offer': 'Offer', 'archived': '归档'}

# 各环节的处方动作：转化率最低的环节指向对应工作区。
STEP_ACTIONS = {
    'interested': dict(action='投递前的转化：优先从约面率高的来源渠道挑岗位，投递前先看可靠度分；'
                              '对意向岗位用触达话术主动开场，别让收藏夹变仓库。',
                       target='favorites', cta='去收藏夹'),
    'applied': dict(action='简历初筛通过率是当前最低环节：用岗位详情的评估缺口定向改写简历'
                            '（工坊按缺口优化），改完再投下一批，对比批次约面率变化。',
                    target='workshop', cta='去简历工坊'),
    'interviewing': dict(action='面试转化是当前最低环节：针对挂点开模拟面试专项复盘；'
                                 '若追问集中在空窗期，先在资料库补「空窗经历」并生成应答话术。',
                         target='interview', cta='去模拟面试'),
}


def _stale_followups(repo, reference):
    """活跃状态超 14 天无变化（与收藏页回顾同口径），最久的排前。"""
    stale = []
    for job in repo.jobs():
        if job.get('pipeline_status') not in TRACKED_ACTIVE:
            continue
        changed = str(job.get('pipeline_status_changed_at') or '')
        try:
            seconds = (reference - datetime.fromisoformat(changed)).total_seconds()
        except (TypeError, ValueError):
            continue
        if seconds > FOLLOW_UP_STALE_DAYS * 86400:
            stale.append(dict(id=job['id'], title=job.get('title', ''), company=job.get('company', ''),
                              days=max(0, int(seconds // 86400))))
    stale.sort(key=lambda item: -item['days'])
    return stale


def build(repo):
    """读本地统计 → 处方卡列表 + 今日行动清单。纯只读，不写库。"""
    funnel = repo.pipeline_funnel()
    weekly = repo.pipeline_weekly()
    due = repo.pipeline_due_items(limit=50)
    gap_count = len([item for item in repo.profile_entries(enabled_only=True) if item['category'] == 'gap'])
    reference = datetime.now(timezone.utc)

    ever = {stage['status']: stage['count'] for stage in funnel['stages']}
    prescriptions: list[dict] = []
    overdue_due = [item for item in due if item['overdue']]
    stale = _stale_followups(repo, reference)
    stale_only = [item for item in stale if item['id'] not in {item['id'] for item in overdue_due}]

    # 1. 跟进积压：逾期截止日 + 长期无变化的活跃岗位，一并开一张卡。
    backlog = len(overdue_due) + len(stale_only)
    if backlog:
        worst = max(overdue_due + stale_only, key=lambda item: item.get('days') or 0)
        prescriptions.append(dict(
            id='followup', level='warn',
            title=f'{len(overdue_due)} 条跟进逾期、{len(stale_only)} 条超 {FOLLOW_UP_STALE_DAYS} 天无变化' if stale_only
            else f'{len(overdue_due)} 条跟进已逾期',
            evidence=f'最久的一条已 {worst.get("days") or 0} 天未动（{worst["title"]} · {worst["company"]}）。',
            action='从最久没动的开始逐条处理：有进展就推进状态，没回音就在话术弹窗生成一条跟进消息发出去。',
            target='favorites', cta='去收藏夹'))

    # 2. 瓶颈环节：只在投递样本 ≥5 时诊断，否则「最低转化率」没有参考意义。
    rates = [step for step in funnel['steps'] if step['rate'] is not None]
    if ever.get('applied', 0) < MIN_APPLIED_FOR_DIAGNOSIS:
        prescriptions.append(dict(
            id='small_sample', level='info',
            title=f'投递样本还少（{ever.get("applied", 0)} 份），处方参考有限',
            evidence='转化率诊断至少要 5 份投递才开始有意义。',
            action='先把投递与状态如实记进管线（收藏夹改状态即可），数据会自己说话。',
            target='favorites', cta='去收藏夹'))
    elif rates:
        worst = min(rates, key=lambda step: step['rate'])
        prescribed = STEP_ACTIONS.get(worst['from'])
        if prescribed:
            prescriptions.append(dict(
                id='bottleneck', level='info',
                title=f'当前转化率最低的环节：{STAGE_NAMES[worst["from"]]}→{STAGE_NAMES[worst["to"]]}',
                evidence=f'转化率 {round(worst["rate"] * 100)}%（n={ever.get(worst["from"], 0)}）。数字只是排序，不必焦虑——先按动作走一轮再看变化。',
                action=prescribed['action'], target=prescribed['target'], cta=prescribed['cta']))

    # 3. 本周停摆：投递与新收藏全 0 是「还没动」的事实提醒，不是催办。
    # 只对有过活动历史的用户提醒——空库用户看的是收藏夹的入门引导，不是这里。
    weeks = weekly.get('weeks') or []
    had_history = sum(week['applied'] for week in weeks[:-1]) > 0 or ever.get('interested', 0) > 0
    if weeks and had_history and weeks[-1]['applied'] == 0 and weeks[-1]['new_favorites'] == 0:
        prescriptions.append(dict(
            id='no_activity', level='info', title='本周还没有投递与新收藏',
            evidence='节奏比单日爆发更可靠；本周节奏面板同样是 0。',
            action='先动起来：从收藏夹挑两家投出去，或去岗位研究补一批意向岗位。',
            target='jobs', cta='去岗位研究'))

    # 4. 空窗叙事就绪度：有管线活动但零空窗素材时提醒一次，补齐后不再打扰。
    if gap_count == 0 and ever.get('interested', 0) > 0:
        prescriptions.append(dict(
            id='gap_readiness', level='info', title='空窗期叙事还没有素材',
            evidence='长期求职者初筛最常见的一刀是空窗解释，面试追问也集中在这一段。',
            action='在资料库补 1–2 条「空窗经历」条目（时间段+做了什么+产出），可一键生成简历段落与面试应答。',
            target='profile', cta='去资料库'))

    # 今日行动清单：投递目标按近一周节奏定（低节奏给 3 的小目标，高节奏封顶 8）；已投数取当日事件。
    today_counts = repo.pipeline_today_counts()
    last_week_applied = weeks[-1]['applied'] if weeks else 0
    if last_week_applied <= 0:
        apply_target, basis = 3, '近一周无投递，先定个小目标'
    elif last_week_applied <= 3:
        apply_target, basis = 3, f'近一周仅 {last_week_applied} 条，保持小目标节奏'
    else:
        daily_avg = round(last_week_applied / 7, 1)
        apply_target = min(8, round(last_week_applied / 7))
        apply_target = max(3, apply_target)
        basis = f'按近一周日均 {daily_avg} 条' + ('（封顶 8）' if round(last_week_applied / 7) > 8 else '')
    today_plan = dict(
        date=business_today(), targets=dict(apply=apply_target, apply_basis=basis),
        done=dict(apply=today_counts.get('applied', 0)),
        follow_ups=len(overdue_due))
    return dict(prescriptions=prescriptions, today_plan=today_plan, generated_at=now())
