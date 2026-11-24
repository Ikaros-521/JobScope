"""失败博物馆：面试「可控/不可控」结构化归因 + 跨场次失分模式报告。

纯确定性模块（无 DB、无 LLM），jd_assessment 同款纪律：分类学常量进代码、
报告由规则统计生成。设计取向借 CBT 思维记录（分步结构化、不自由写作）与
错题本归因分类学（错因分型 → 针对性复习）：产品不做评判，只做归因分流——
可控项单独拎出来变成下一场的准备清单，不可控项只做定位，阻止用户把所有
失败内化为「我不行」。

第三层（失败模式回灌出题）已由两条链路落地，本模块只做归因与跨场统计：
- 主题级：prep_plan.collect_focus 取本模块 report.recurring（重复失分点权重最高）
  → material.weak_focus，题单里至少 1/3 的题围绕它。
- 题目级（2026-11-25，drill.py）：既往复盘里被点了失分点的题换问法重考，
  面试官按上次失分点追问，复盘逐题对照上次，重练状态回写错题本。
"""

# ---- 分类学：可控 = 用户能改变的（给建议）；不可控 = 环境因素（只定位，不给建议） ----

CONTROLLED_TAGS = [
    dict(id='prep_knowledge', label='准备不足', hint='被问到的知识点/业务背景没准备到',
         advice='把本场被问倒的点记进下一场的预习清单，出题时定向核实'),
    dict(id='structure', label='应答结构混乱', hint='回答缺少框架，STAR 讲不完整、重点淹没',
         advice='每答先给结论再展开：情境一句→动作两句→结果带数字'),
    dict(id='detail_followup', label='细节追问卡壳', hint='表面答上了，被追问两层就露底',
         advice='简历里每个项目都要经得起「为什么这么设计/换方案会怎样」两层追问'),
    dict(id='intro', label='自我介绍失控', hint='开场介绍超时、流水账或没突出与岗位的匹配',
         advice='按「岗位匹配点」重排自我介绍，控制在 90 秒内并留钩子'),
    dict(id='tech_live', label='技术临场失误', hint='会做但现场写/推错了，或被白板/手撕打回原形',
         advice='练过的题要能脱稿讲：白板先讲思路边界再动笔'),
    dict(id='nerves', label='紧张/语速失控', hint='情绪影响表达：语速过快、大脑空白、声音发抖',
         advice='下一场开场前做 3 轮深呼吸；紧张时放慢语速=给自己思考时间'),
    dict(id='qa_missing', label='缺量化无结果', hint='回答里没有数字、没有个人动作、没有结果闭环',
         advice='简历与答案预埋量化锚点：规模/百分比/时长/团队人数'),
    dict(id='vague', label='含糊其辞', hint='用「大概/可能/参与过」糊弄过去，被识破后信任下降',
         advice='不会就直说边界+给推理路径，硬撑比承认更扣分'),
    dict(id='comp_salary', label='薪资应对不当', hint='期望报价失当或被压价时没有应对策略',
         advice='提前定好「理想/底线/不接」三档话术，反问薪资结构再报数'),
    dict(id='reverse_q', label='反问敷衍', hint='「你有什么想问的」环节答不出有质量的问题',
         advice='备 3 个业务真问题（团队分工/考核方式/业务方向），别问百度就能查到的'),
]

UNCONTROLLED_TAGS = [
    dict(id='interviewer_rush', label='面试官赶时间', hint='明显催促、压缩环节、心不在焉——你的表现不是主要原因'),
    dict(id='budget_hc', label='预算/HC 迹象', hint='预算冻结、岗位可能内定或招满的信号，与你的表现无关'),
    dict(id='off_topic', label='与岗位无关的问题', hint='考察点偏离 JD 或岗位实际需求，结果不反映岗位匹配度'),
    dict(id='competitor', label='竞争者碾压迹象', hint='面试官透露有多位强候选人在流程中——供给端的事'),
    dict(id='process_flow', label='流程外因素', hint='HR 失联、流程取消、改期超两次等流程问题，非能力原因'),
]

TAGS_BY_ID = {tag['id']: tag for tag in CONTROLLED_TAGS + UNCONTROLLED_TAGS}
CONTROLLED_IDS = {tag['id'] for tag in CONTROLLED_TAGS}

# 模拟面试面试官暗线已采集的弱点信号（interview.ADVANCE_TOOL.weakness 枚举）
# → 可控标签：标注面板把它当「系统观察信号」预选建议，用户可取消。
WEAKNESS_TO_TAG = {
    '无量化数据': 'qa_missing',
    '无个人动作': 'qa_missing',
    '无结果': 'qa_missing',
    'STAR缺失': 'structure',
    '含糊其辞': 'vague',
    '知识硬伤': 'prep_knowledge',
}

OUTCOMES = {
    'mock': ('self_positive', 'self_neutral', 'self_negative'),      # 自评：好/一般/差
    'real': ('pass', 'fail', 'unknown'),                            # 真实结果：通过/挂了/未知
}

MAX_TAGS = 12          # 可控+不可控总点选上限：防全选失焦，一场抓不住所有问题
MAX_NOTE = 200         # 单条备注上限
MAX_SESSIONS = 500     # 归档行数护栏（单用户场景远用不到）


def normalize_tags(tags):
    """校验并规范化标注列表：scope/tag 合法性、备注截断、去重（同 tag 只留一条）。

    ValueError 携带中文原因，路由层原样映射 400。
    """
    if not tags:
        raise ValueError('至少勾选一个归因标签')
    if len(tags) > MAX_TAGS:
        raise ValueError(f'一场最多标注 {MAX_TAGS} 个标签——抓最关键的两三个才有用')
    seen, result = {}, []
    for item in tags:
        tag_id = (item.get('tag') or '').strip()
        if tag_id not in TAGS_BY_ID:
            raise ValueError(f'未知标签：{tag_id}')
        scope = item.get('scope') or ('controlled' if tag_id in CONTROLLED_IDS else 'uncontrolled')
        if scope not in ('controlled', 'uncontrolled'):
            raise ValueError(f'未知归因分组：{scope}')
        if (scope == 'controlled') != (tag_id in CONTROLLED_IDS):
            raise ValueError(f'标签 {tag_id} 与分组 {scope} 不匹配')
        note = (item.get('note') or '').strip()[:MAX_NOTE]
        if tag_id not in seen:
            seen[tag_id] = True
            result.append(dict(scope=scope, tag=tag_id, note=note))
    return result


def build_pattern_report(records):
    """跨场次模式报告（规则版）：重复可控失分点 + 不可控环境频次 + 结果对比。

    records 为归档行列表（dict，含 tags/outcome/source/created_at）。孤立的失败
    是消耗，重复的失败被看见才是信号：样本 <3 场时明示 limitations，不下强结论。
    """
    if not records:
        return dict(total_sessions=0, recurring=[], uncontrolled=[], by_tag=[],
                    limitations=['还没有标注任何场次：完成一场复盘后，在报告页做归因标注。'])
    controlled_count, uncontrolled_count = {}, {}
    outcome_stats = {}   # tag_id -> {'total': n, 'negative': n, 'positive': n}
    sessions_with_tags = set()
    for record in records:
        sessions_with_tags.add(record.get('session_id', ''))
        for item in record.get('tags') or []:
            tag_id = item.get('tag', '')
            bucket = controlled_count if item.get('scope') == 'controlled' else uncontrolled_count
            bucket[tag_id] = bucket.get(tag_id, 0) + 1
            stats = outcome_stats.setdefault(tag_id, dict(total=0, negative=0, positive=0))
            stats['total'] += 1
            outcome = record.get('outcome') or ''
            if outcome in ('self_negative', 'fail'):
                stats['negative'] += 1
            elif outcome in ('self_positive', 'pass'):
                stats['positive'] += 1
    total_sessions = len(sessions_with_tags)
    limitations = []
    if total_sessions < 3:
        limitations.append(f'样本仅 {total_sessions} 场（<3）：重复项只作线索，不构成结论，继续积累。')

    by_tag = []
    for tag_id, count in sorted(controlled_count.items(), key=lambda kv: (-kv[1], kv[0])):
        tag = TAGS_BY_ID[tag_id]
        stats = outcome_stats[tag_id]
        by_tag.append(dict(tag=tag_id, label=tag['label'], scope='controlled', count=count,
                           negative=stats['negative'], positive=stats['positive']))
    uncontrolled = []
    for tag_id, count in sorted(uncontrolled_count.items(), key=lambda kv: (-kv[1], kv[0])):
        uncontrolled.append(dict(tag=tag_id, label=TAGS_BY_ID[tag_id]['label'],
                                 scope='uncontrolled', count=count,
                                 hint=TAGS_BY_ID[tag_id]['hint']))

    recurring = []
    for tag_id, count in controlled_count.items():
        if count < 2:
            continue
        tag = TAGS_BY_ID[tag_id]
        stats = outcome_stats[tag_id]
        recurring.append(dict(tag=tag_id, label=tag['label'], count=count,
                              advice=tag['advice'], negative=stats['negative'], positive=stats['positive']))
    recurring.sort(key=lambda item: (-item['count'], item['tag']))
    if not recurring and total_sessions >= 3:
        limitations.append('暂无重复出现的可控失分点（各标签只出现一次）：继续记录，别急着下结论。')
    return dict(total_sessions=total_sessions, recurring=recurring, uncontrolled=uncontrolled,
                by_tag=by_tag, limitations=limitations)


def suggestion_line(report):
    """「下一场准备清单」一句话：取重复榜首的可控标签建议，无重复时给通用引导。"""
    if report.get('recurring'):
        top = report['recurring'][0]
        return f'重复失分点「{top["label"]}」出现 {top["count"]} 次 → {top["advice"]}'
    if report.get('total_sessions'):
        return '还没有重复失分点；每场标注后系统会在出现第二次时自动点名。'
    return ''
