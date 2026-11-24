"""工作风格测评（2026-11-24，第二种考卷）：陈述句 + 五级同意度（MBTI 式自陈题）→ 工作风格报告。
纯规则、无 LLM、确定性、可测。与反向JD测评（jd_assessment.py）同页分页签、同表分卷别存储，互补不重叠：
    JD 取舍卷（best-worst）   测「你要什么」：对真实 JD 片段做取舍，反推隐性筛选条件；
    工作风格卷（Likert 自陈） 测「你适合怎么工作」：能量来源 / 思维取向 / 决策依据 / 节奏秩序 四根轴。
刻意不问薪资、晋升、稳定这类诉求：自陈题问诉求人人都会选「重要」，没有区分度——那正是取舍卷存在的理由。

为什么是连续分 + 分档，而不是 MBTI 式二分类型（2026-11-24 仿真：output/tune_style_headline.py 选型、
output/calibrate_style_scale.py 用线上计分复核。作答模型 θ ~ N(0, 0.6²)、单题噪声 σ=1.05、题目负荷
0.6–1.2、默认同意倾向、25% 极端作答；两次作答误差相互独立，所以下面的重测一致率是下限）：
  · 48 句（每轴 12 句）下逐轴方向是可靠的：判型方向准确率约 96%；标「明显」的轴重测 83% 仍同向，
    标「略偏」的 55%（回落成「不明显」的居多，真翻方向的少）；真倾向 ≥ 1 格的轴 96% 能判出来。
  · 但从 4 个带噪声的轴分里「取最强两极拼类型」天然不稳：重测后原型名一致只有约 18%（两极至少一个
    重合 72%），加题到 16 句/轴也只到三成——这是连续特质切类型的固有属性，不是题库质量问题。
    所以报告的主结论是**逐轴三档**，原型名只是当前最强的两个倾向的速记，前端重测时并排显示上次结论。
  · 分档阈值落在 1/k 的整数倍上并用整数比较（d 只能取 1/12 的整数倍，0.9 这种阈值会撞浮点表示）：
    略偏 = 平均比中线偏离半格，明显 = 偏离一整格。零倾向的轴被误标「明显」约 1.2%、被误判型约 18%；
    认真作答被判质检警示约 1.4%。

计分：每句按极向折算到该轴的 b 极方向（0–4），轴分 d = 折算均值 − 2 ∈ [-2, 2]。正反各半的措辞让
「默认同意」在均值里恰好抵消（认同题 +c、反向题折算后 −c）。不用常模：d 的单位就是量表上的「格」，
「平均偏向某极 0.8 格」本身可解释，不依赖人群分布假设。
质检：1 句指令式注意力题（按提示选项作答）+ 全程同一选项比例 + 过快作答比例 + 默认同意倾向。仿真里
随机乱点 80% 被拦（其余 20% 是注意力题蒙对的，这时四轴基本都是「不明显」，不会给出像样的结论），
全程同一选项、「同意 / 非常同意」混选 100% 被拦。
题库本身尚未经真人数据验证（单一用户产品，样本不够算 α）；作答原文随报告落库，攒够份数后可回算题总相关。
"""
import math
import random as _random
from collections import Counter

MODEL = 'work-style-v1'
DESIGN_SEED = 20261124

SCALE = tuple(dict(value=value, label=label) for value, label in
              enumerate(('非常不同意', '不同意', '不确定', '同意', '非常同意')))

# ── 四根轴。a / b 是两极，两极都写成中性偏正面的工作风格——没有哪一极「更好」。 ──
AXES = (
    dict(key='focus', label='能量来源', question='一个人的时候，还是和人一起的时候，你状态更好？',
         a=dict(key='solo', name='独立专注'), b=dict(key='team', name='协作共创')),
    dict(key='thinking', label='思维取向', question='你的注意力更容易落在眼前的事实，还是未来的可能？',
         a=dict(key='grounded', name='落地务实'), b=dict(key='vision', name='探索构想')),
    dict(key='deciding', label='决策依据', question='做判断时，你更信依据，还是更顾人的感受？',
         a=dict(key='logic', name='逻辑取舍'), b=dict(key='empathy', name='共情权衡')),
    dict(key='rhythm', label='节奏秩序', question='你靠计划推进，还是边做边调？',
         a=dict(key='order', name='计划有序'), b=dict(key='agile', name='灵活应变')),
)

# ── 题库：每轴 12 句，a / b 极各 6 句（pole = 同意这句话会倒向哪一极）。每句只说一件事、
# 写行为和体验而不是愿望，同一轴的 12 句覆盖不同场景（精力恢复 / 卡壳 / 开会 / 收尾 …），
# 避免同一句话换皮重复。改题库不涉及区组设计，但题量变了要重跑 calibrate_style_scale.py。 ──
_STATEMENTS = {
    ('focus', 'a'): (
        '一整天不被打扰地独自干活，对我来说是享受而不是煎熬。',
        '遇到卡壳的问题，我习惯先自己啃一阵，而不是立刻找人讨论。',
        '连开一整天会之后，我需要独处一会儿才能恢复精力。',
        '人多的会上我通常说得不多，会后一对一聊反而更能表达我的想法。',
        '工作里需要静下心来的那部分，是我一天中状态最好的时候。',
        '我的好点子多半是在独自思考时冒出来的，不是在会上。',
    ),
    ('focus', 'b'): (
        '和同事边聊边做，我的思路比一个人闷头想更顺。',
        '工作一整天都没和同事说上话，我会觉得发闷。',
        '想法在讨论里被人挑战，对我来说是充电而不是消耗。',
        '很多时候我要说着说着，才把自己真正的想法理清楚。',
        '午休或休息时，我更愿意和同事待在一起，而不是自己一个人。',
        '比起写一大段文字，我更愿意打个电话或者当面把事说清楚。',
    ),
    ('thinking', 'a'): (
        '看一个新方案，我第一反应是“这要花多少成本、多久能落地”。',
        '把方案的每个细节抠到能执行，比提出这个方案更让我有成就感。',
        '接到模糊的任务，我会先把它拆成今天就能动手的第一步。',
        '别人聊“五年后会怎样”时，我更关心下个季度怎么交付。',
        '一份只讲愿景、不讲怎么做的东西，我读两段就想放下。',
        '把手头的事做熟做精，比开辟一个新方向更让我踏实。',
    ),
    ('thinking', 'b'): (
        '闲下来的时候，我常会冒出和手头工作不直接相关的新点子。',
        '比起优化现有做法，我更容易被“换一种完全不同的思路”吸引。',
        '读一份材料，我最容易被它的言外之意和长远含义抓住。',
        '看到一个东西，我的第一反应常常是“它还能被改成什么样”。',
        '那种“还没有答案”的问题，我琢磨起来就觉得有意思。',
        '我关注的往往不是“这件事是什么”，而是“它背后是什么规律”。',
    ),
    ('deciding', 'a'): (
        '讨论方案时，我最常问的一句是“依据是什么”。',
        '起争执时，我想先把“谁说得对”弄清楚，而不是先缓和气氛。',
        '别人找我倾诉时，我的第一反应是帮他想办法，而不是先安慰。',
        '我不觉得“大家都同意”就能说明一个决定是对的。',
        '只讲情怀、拿不出数据的论证，很难说服我。',
        '我可以和一个私下合不来的人，把工作配合得很好。',
    ),
    ('deciding', 'b'): (
        '一个方案再合理，如果会让执行的人很难受，我也会重新考虑。',
        '开会时我会留意谁一直没说话、谁的表情不太对。',
        '指出别人的问题之前，我会先想怎么开口才不让对方难堪。',
        '相比“哪个方案更对”，我更在意“哪个方案大家愿意一起干”。',
        '做决定之前，我会把每个相关的人会受到的影响挨个想一遍。',
        '气氛僵住的时候，我会下意识地去打圆场。',
    ),
    ('rhythm', 'a'): (
        '我习惯把一周要做的事提前列出来，按部就班地推进。',
        '计划临时被打乱，是我工作里最消耗心力的事之一。',
        '没弄清规则和验收标准之前，我不太愿意动手。',
        '一件事没收尾，我会一直惦记着，很难安心切到下一件。',
        '要做的事如果不写下来，我心里就不踏实。',
        '我做事习惯提前动手、留出富余，不喜欢卡着截止时间赶。',
    ),
    ('rhythm', 'b'): (
        '计划对我只是个大致方向，边做边调才自然。',
        '同时推进好几件事、来回切换，反而让我觉得有节奏。',
        '规则还没定的模糊阶段，我照样能动手，边做边找感觉。',
        '我习惯把选项留到最后一刻，不喜欢过早定死。',
        '不带稿子上台发言、临场应答，我也不太会慌。',
        '时间紧的时候，我的脑子反而更清楚。',
    ),
}
ITEMS = tuple(dict(id=f'S{index + 1:02d}', axis=axis, pole=pole, text=text)
              for index, (axis, pole, text) in enumerate(
                  (axis, pole, text) for (axis, pole), texts in _STATEMENTS.items() for text in texts))
ITEM_BY_ID = {item['id']: item for item in ITEMS}
PER_POLE = len(next(iter(_STATEMENTS.values())))     # 每极 6 句
PER_AXIS = 2 * PER_POLE                              # 每轴 12 句

# 指令式注意力题：按提示选项作答才算通过。位置由种子决定，落在卷子中段三分之一。
ATTENTION_ITEM = dict(id='SA1', text='这一题用来确认你在认真读题，请选择「同意」。')
ATTENTION_EXPECT = 3

MIN_GAP = 3            # 同一轴的两句至少隔 3 个位置
POLE_RUN = 3           # 同一极向的句子最多连出 3 句


def _lanes(rng):
    """四条「轴泳道」各持一个洗好的题堆。"""
    lanes = {}
    for axis in AXES:
        pool = [item for item in ITEMS if item['axis'] == axis['key']]
        rng.shuffle(pool)
        lanes[axis['key']] = pool
    return lanes


def _take(pool, left, rng):
    """从泳道取一句：优先出全卷剩余更多的那一极——两种极向交替消耗，卷尾不会只剩同一极。"""
    want = max(('a', 'b'), key=lambda pole: (left[pole], rng.random()))
    return pool.pop(rng.choice([i for i, item in enumerate(pool) if item['pole'] == want] or list(range(len(pool)))))


def _lay_out(rng):
    """排一次计分题。每步从「最近没用过的轴」里按剩余题数（带随机抖动）加权抽一条泳道；同一极向已
    连出 POLE_RUN 句时，从所有可用泳道里改抽一句另一极向的。卷尾只剩两三条泳道时这两条约束在数学上
    无解（任意连续 3 句需要 3 条不同的轴），所以这里只尽量满足，由 build_paper 出卷后复核、不合格重排。"""
    lanes = _lanes(rng)
    left = Counter(item['pole'] for item in ITEMS)
    recent, run, sequence = [], '', []
    while any(lanes.values()):
        allowed = [key for key, pool in lanes.items() if pool and key not in recent] or             [key for key, pool in lanes.items() if pool]
        same_run = len(run) >= POLE_RUN and run[-POLE_RUN:] == run[-1] * POLE_RUN
        options = [(key, i) for key in allowed for i, item in enumerate(lanes[key]) if item['pole'] != run[-1:]]
        if same_run and options:
            axis_key, index = rng.choice(options)
            item = lanes[axis_key].pop(index)
        else:
            axis_key = rng.choices(allowed, weights=[len(lanes[key]) * rng.uniform(0.5, 1.5) for key in allowed])[0]
            item = _take(lanes[axis_key], left, rng)
        left[item['pole']] -= 1
        sequence.append(item)
        recent = (recent + [axis_key])[1 - MIN_GAP:]
        run += item['pole']
    return sequence


def _cycle_layout(rng):
    """保底排法：按一条洗过的轴序轮转出题（同轴间隔恒为轴数），随机抖动重排 400 次仍不合格时才用。"""
    lanes, sequence = _lanes(rng), []
    left = Counter(item['pole'] for item in ITEMS)
    while any(lanes.values()):
        for key in lanes:
            if lanes[key]:
                item = _take(lanes[key], left, rng)
                left[item['pole']] -= 1
                sequence.append(item)
    return sequence


def _longest_pole_run(sequence):
    best = run = 0
    previous = None
    for item in sequence:
        run = run + 1 if item['pole'] == previous else 1
        previous = item['pole']
        best = max(best, run)
    return best


def _spaced(sequence):
    """同轴的两句是否处处间隔 ≥ MIN_GAP。"""
    last = {}
    for position, item in enumerate(sequence):
        if position - last.get(item['axis'], -MIN_GAP) < MIN_GAP:
            return False
        last[item['axis']] = position
    return True


def build_paper(seed=None):
    """卷面 = 48 句计分题 + 1 句注意力题。题序随种子变化（重测不是同一张卷）。约束由出卷后复核保证：
    同轴的两句间隔 ≥ MIN_GAP（同一根轴的句子不会扎堆，减少「怎么又在问这个」的感觉）、
    同一极向不连出超过 POLE_RUN 句、注意力题落在中段三分之一。"""
    rng = _random.Random(DESIGN_SEED if seed is None else int(seed))
    for _ in range(400):
        sequence = _lay_out(rng)
        if _spaced(sequence) and _longest_pole_run(sequence) <= POLE_RUN:
            break
    else:
        sequence = _cycle_layout(rng)
    low, high = len(sequence) // 3, len(sequence) * 2 // 3
    sequence.insert(rng.randint(low, high), ATTENTION_ITEM)
    return sequence


_SESSION_CACHE = {}


def _session(seed):
    key = DESIGN_SEED if seed is None else int(seed)
    if key not in _SESSION_CACHE:
        if len(_SESSION_CACHE) >= 64:          # 会话级缓存兜底，防长驻进程无限增长
            _SESSION_CACHE.clear()
        _SESSION_CACHE[key] = build_paper(key)
    return _SESSION_CACHE[key]


def paper(seed=None):
    """取卷：{items, scale}。前端按此渲染与回传 item_id，提交时带上同一 seed（计分按题目编号，与题序无关）。"""
    return dict(items=[dict(id=item['id'], text=item['text']) for item in _session(seed)],
                scale=[dict(value=step['value'], label=step['label']) for step in SCALE], model=MODEL)


# ── 计分。 ──

RT_FLOOR = 350           # 单句低于此耗时视为没读题（20 字陈述的阅读 + 点击远不止这个数）
FAST_SHARE = 0.3         # 过快句子占比超过 → 建议重测
STRAIGHT_SHARE = 0.8     # 全程同一选项占比超过 → 建议重测
AGREE_BIAS = 0.75        # 原始答案均值比中线高出这么多 → 「默认同意」抵消了反向题，判质检警示
BAND_SLIGHT, BAND_CLEAR = 0.5, 1.0
BAND_LABELS = {'clear': '明显', 'slight': '略偏', 'even': '不明显'}
CROSS_LIMIT = 4


def _band(total, n):
    """total 是 n 句折算分之和相对中点的整数净偏离。阈值比较先把商 round 到 6 位，
    避开 0.5 的浮点表示（sum/12 这类除法会差一个 ulp）。"""
    magnitude = round(abs(total) / n, 6)
    return 'clear' if magnitude >= BAND_CLEAR else 'slight' if magnitude >= BAND_SLIGHT else 'even'


def score_style(responses, seed=None):
    """responses: [{item_id, value, rt_ms}]（全量；value 是 0–4 的选项下标）。返回报告 dict；非法输入抛 ValueError。
    seed 只用来核对题卷，计分按题目编号，与题序无关。"""
    sheet = _session(seed)
    ids = {item['id'] for item in sheet}
    if len(responses) != len(sheet):
        raise ValueError(f'作答数量不符：应答 {len(sheet)} 句，收到 {len(responses)} 句')
    answers = {}
    for row in responses:
        item_id = row.get('item_id') or ''
        value = row.get('value')
        if item_id not in ids or item_id in answers:
            raise ValueError(f'作答含未知或重复的题目编号：{item_id}')
        if type(value) is not int or not 0 <= value <= 4:
            raise ValueError(f'作答选项非法：{item_id} 的选项下标越界')
        answers[item_id] = (value, int(row.get('rt_ms') or 0))

    count = len(sheet)
    scored = {item_id: answers[item_id] for item_id in answers if item_id != ATTENTION_ITEM['id']}
    fast = sum(1 for _, rt in answers.values() if rt < RT_FLOOR)
    attention_pass = answers[ATTENTION_ITEM['id']][0] == ATTENTION_EXPECT
    same = Counter(value for value, _ in scored.values()).most_common(1)[0][1]
    straight = same >= STRAIGHT_SHARE * len(scored)
    agree_bias = sum(value for value, _ in scored.values()) / len(scored) - 2
    flags = []
    if not attention_pass:
        flags.append('注意力校验未通过（没有按提示选项作答），结果可信度低')
    if fast / count > FAST_SHARE:
        flags.append(f'过快作答过多（{fast} 句），建议重测')
    if straight:
        flags.append(f'大比例选了同一选项（{same}/{len(scored)} 句），建议重测')
    if agree_bias >= AGREE_BIAS:
        flags.append('答案整体偏向「同意」（反向题的效果被抵消），各轴容易显得不明显，建议重测时多用「不同意」')

    axes_report = []
    for axis in AXES:
        # 只遍历计分题库（注意力题不在 ITEMS 里）；答案已按编号全部校验过。
        keys = [answers[item['id']][0] if item['pole'] == 'b' else 4 - answers[item['id']][0]
                for item in ITEMS if item['axis'] == axis['key']]
        n = len(keys)
        net = sum(keys) - 2 * n              # 整数净偏离：正 = 偏 b 极，负 = 偏 a 极
        mean = sum(keys) / n
        variance = sum((k - mean) ** 2 for k in keys) / (n - 1)
        band = _band(net, n)
        side = axis['b'] if net > 0 else axis['a']
        axes_report.append(dict(
            key=axis['key'], label=axis['label'], question=axis['question'],
            pole_a=axis['a']['key'], pole_a_name=axis['a']['name'],
            pole_b=axis['b']['key'], pole_b_name=axis['b']['name'],
            d=round(net / n, 4), se=round(math.sqrt(variance / n), 4),
            band=band, band_label=BAND_LABELS[band],
            pole=side['key'] if band != 'even' else None,
            pole_name=side['name'] if band != 'even' else None,
            n_items=n,
        ))

    return dict(
        kind='style', model=MODEL,
        quality=dict(attention_pass=attention_pass, straight=straight, fast_items=fast, items=count,
                     agree_bias=round(agree_bias, 4), flags=flags, grade='weak' if flags else 'ok'),
        axes=axes_report,
        **_typology(axes_report),
        note='结论来自你的自我描述，反映的是你觉得自己是什么样，不是能力评价，也不是岗位市场判断。'
             '标「明显」的倾向重测很稳定；标「略偏」的方向大概率正确，但重测可能归为不明显。'
             '原型名只是当前最强的两个倾向的速记，以逐轴结论为准。',
    )


# ── 报告文案：每个极的工作画像，以及两两组合的原型名。 ──

POLE_CONTENT = {
    'solo': dict(
        name='独立专注', tagline='一个人的时候状态最好，深度比热闹更让你有产出。',
        strengths=['能长时间钻进一个问题，不需要有人在旁边才做得下去',
                   '先想清楚再开口，给出的意见通常想得更全',
                   '驱动力来自事情本身，不用别人盯着'],
        watch_outs=['闷头太久才同步，别人可能到最后一刻才知道进展',
                    '该求助时会拖，小问题容易一个人扛成大问题'],
        seek=['独立负责', '端到端', '核心模块深耕', '可支配的专注时间'],
        caution=['高频对外沟通', '大量跨部门拉通', '全天结对 / 开放协作'],
    ),
    'team': dict(
        name='协作共创', tagline='和人一起时思路更顺，想法是在碰撞里长出来的。',
        strengths=['沟通主动，信息不容易在你这里断链',
                   '善于借力，不会一个人把事扛死',
                   '能觉察团队状态，推着事情往前走'],
        watch_outs=['独处专注的时间容易被会议挤掉',
                    '过于依赖讨论，关键决定可能等到所有人都点头'],
        seek=['团队协作紧密', '跨部门推进', '头脑风暴、共创', '技术分享氛围'],
        caution=['长期单兵作战', '远程独立作业', '几乎不需要沟通的岗位'],
    ),
    'grounded': dict(
        name='落地务实', tagline='先看成本和路径，把事做成比想得漂亮更重要。',
        strengths=['接到模糊需求，也能拆出今天就能动手的第一步',
                   '对细节和可执行性敏感，方案不容易悬空',
                   '交付稳定，交给你的事有着落'],
        watch_outs=['没想清楚就开干，有时会换来返工',
                    '对纯概念的讨论没耐心，可能错过方向性的信息'],
        seek=['目标与验收标准明确', '成熟业务持续优化', '可量化的交付'],
        caution=['方向未定、反复探索', '只谈愿景不讲路径', '以研究为主'],
    ),
    'vision': dict(
        name='探索构想', tagline='注意力天然落在「还能是什么」上，模糊里反而有灵感。',
        strengths=['能看到别人没注意的可能性和联系',
                   '对陈旧做法不认命，愿意推倒重来',
                   '在没人给答案的地方也能起步'],
        watch_outs=['兴趣容易被新想法带走，收尾是弱项',
                    '落地细节没人补位时，想法容易停在纸上'],
        seek=['0 到 1', '新业务、预研', '创新空间', '从无到有搭建'],
        caution=['按 SOP 执行', '成熟流程里的固定环节', '重复性维护为主'],
    ),
    'logic': dict(
        name='逻辑取舍', tagline='先问依据，对事不对人，结论要经得起推敲。',
        strengths=['判断不受人情左右，对人对事标准一致',
                   '能直接指出问题，讨论效率高',
                   '可以和私下合不来的人把事做好'],
        watch_outs=['直接的反馈有时比本意更重，别人记住的可能是方式',
                    '容易忽略「说得对」和「别人愿意听」之间的差别'],
        seek=['数据驱动', '技术评审氛围', '结果导向', '清晰的考核标准'],
        caution=['靠关系推动', '决策标准模糊', '重态度轻依据'],
    ),
    'empathy': dict(
        name='共情权衡', tagline='做决定时，会把每个人的感受和处境放进去。',
        strengths=['能注意到沉默的人和没说出口的顾虑',
                   '给出的反馈让人听得进去',
                   '团队有摩擦时，你是先缓和的那个'],
        watch_outs=['为了不伤和气，可能把真话往后放',
                    '别人的情绪容易变成你自己的负担'],
        seek=['团队氛围好', '用户、客户导向', '支持型角色', '导师带教'],
        caution=['对抗式考核', '末位淘汰', '只看数字不看人'],
    ),
    'order': dict(
        name='计划有序', tagline='靠计划和秩序推进，确定感是你高产的前提。',
        strengths=['排期靠谱，说什么时候交付就什么时候交付',
                   '做事留富余，很少把自己逼到截止线',
                   '规则和标准清楚后，执行又快又稳'],
        watch_outs=['计划被打乱时内耗大，需要给自己留缓冲',
                    '对「还没定」的阶段容易着急，可能过早把方案定死'],
        seek=['流程规范', '评审与文档齐全', '稳定的迭代节奏', '明确的项目排期'],
        caution=['需求天天变', '早期团队的无章法', '救火文化'],
    ),
    'agile': dict(
        name='灵活应变', tagline='边做边调，变数多的时候你反而更灵活。',
        strengths=['模糊和变化里照样能起步，不等万事俱备',
                   '多线并行不慌，切换成本低',
                   '临场反应快，没有脚本也能上'],
        watch_outs=['喜欢留选项，可能拖到最后一刻才收口',
                    '排期和文档如果不刻意经营，容易欠账'],
        seek=['快节奏', '多线并行', '探索型、早期项目', '自主安排'],
        caution=['严格的固定流程', '长期一成不变的例行工作', '排期精细到天'],
    ),
}

BALANCED = dict(
    name='均衡适应',
    essence='四根轴都没有明显偏向：你更像看情境切换风格的人，哪种环境都能找到自己的做事方式。',
    fits='职责边界清楚、可以自己定义做事方法的岗位——风格不定型的人在那里最能发挥。',
    drains='强推某一种工作方式的团队（比如人人必须结对，或者完全不许沟通）。',
)

# 两两组合（一定来自不同的轴，共 24 种）：name 是原型速记，essence / fits / drains 各一句。
ARCHETYPES = {
    frozenset(('solo', 'grounded')): dict(name='工匠', essence='一个人把一件事做到极深，靠手艺和交付质量立身。',
                                          fits='职责清晰、可以长期深耕的技术或专业岗，考核看质量。',
                                          drains='频繁被打断、需要大量对外协调、指标月月变的岗位。'),
    frozenset(('solo', 'vision')): dict(name='研究者', essence='独自啃没人解过的问题，享受「还没有答案」。',
                                        fits='预研、算法、架构探索、长期课题。',
                                        drains='重复性的维护活、马上要结果的短期项目。'),
    frozenset(('solo', 'logic')): dict(name='系统构建者', essence='靠严密性立身，方案必须经得起推敲。',
                                       fits='架构、核心模块、审计、风控这类「错一次都不行」的活。',
                                       drains='靠人情推动、标准模糊、凭感觉拍板的环境。'),
    frozenset(('solo', 'empathy')): dict(name='深度倾听者', essence='不爱热闹，但在一对一里能听见别人没说出口的话。',
                                         fits='咨询、教练、用户访谈、写作这类少而深的沟通。',
                                         drains='需要同时应付很多人的高频社交型岗位。'),
    frozenset(('solo', 'order')): dict(name='守门人', essence='独立、低差错、按表交付。',
                                       fits='质量保障、运维、财务、合规这类要稳要准的岗位。',
                                       drains='需求天天变、没有文档、全靠临场发挥的团队。'),
    frozenset(('solo', 'agile')): dict(name='排障专家', essence='一个人面对突发，也能见招拆招。',
                                       fits='技术支持、值班排障、安全应急这类独自处理疑难的活。',
                                       drains='流程冗长、什么都要层层审批的环境。'),
    frozenset(('team', 'grounded')): dict(name='推进者', essence='把人协调起来，把事推到落地。',
                                          fits='项目经理、交付负责人、跨团队推进类岗位。',
                                          drains='只需要单兵产出、几乎不用和人打交道的工作。'),
    frozenset(('team', 'vision')): dict(name='布道者', essence='把新想法讲给人听、把人拉上船。',
                                        fits='产品愿景、解决方案、内部创新推动这类岗位。',
                                        drains='只执行、不许提想法的流水线岗位。'),
    frozenset(('team', 'logic')): dict(name='评审主持', essence='在讨论里逼出最优解，对事不对人。',
                                       fits='技术评审、方案评审、数据分析、需要拍板讨论的角色。',
                                       drains='讲关系不讲依据、开会一团和气没人说真话的文化。'),
    frozenset(('team', 'empathy')): dict(name='凝聚者', essence='团队的连接器，谁的状态都看得见。',
                                         fits='团队负责人、HR、客户成功、社群运营。',
                                         drains='人人各干各的、几乎没有协作的岗位。'),
    frozenset(('team', 'order')): dict(name='组织者', essence='把人、时间、事排进一张表。',
                                       fits='项目 / 运营管理、流程建设、大型活动的统筹。',
                                       drains='完全没有章法、全靠临场救火的小团队。'),
    frozenset(('team', 'agile')): dict(name='开拓者', essence='在人和变数里都如鱼得水。',
                                       fits='销售、商务、现场交付这类既要见人又要应变的活。',
                                       drains='一成不变、按部就班的例行工作。'),
    frozenset(('grounded', 'logic')): dict(name='工程师', essence='讲依据、能落地，方案要能验证。',
                                           fits='研发、数据分析、测试、工艺改进这类讲道理又出结果的岗位。',
                                           drains='目标含糊、只看表态不看结果的团队。'),
    frozenset(('grounded', 'empathy')): dict(name='服务者', essence='把别人的需要落成实事。',
                                             fits='客户成功、售后、用户研究执行、服务类岗位。',
                                             drains='只看冷冰冰指标、不管人怎么受影响的环境。'),
    frozenset(('grounded', 'order')): dict(name='运营者', essence='流程和节奏尽在掌握。',
                                           fits='运营、供应链、生产管理、财务流程类岗位。',
                                           drains='天天救火、永远在改需求的团队。'),
    frozenset(('grounded', 'agile')): dict(name='现场担当', essence='哪里出问题，就在哪里解决。',
                                           fits='现场工程、实施交付、一线技术支持。',
                                           drains='节奏慢、层层审批、远离一线的岗位。'),
    frozenset(('vision', 'logic')): dict(name='谋局者', essence='想得远，也说得通。',
                                         fits='战略、投研、咨询、政策研究这类先想清楚再讲清楚的活。',
                                         drains='只管执行、不给时间思考的岗位。'),
    frozenset(('vision', 'empathy')): dict(name='洞察者', essence='看见别人没说出口的需要。',
                                           fits='产品设计、用户研究、内容创作、教育创新。',
                                           drains='需求完全定死、只照着做的执行岗。'),
    frozenset(('vision', 'order')): dict(name='规划者', essence='把蓝图排成一张能执行的路线图。',
                                         fits='产品规划、路线图管理、长期项目的计划岗。',
                                         drains='没有长期目标、月月推倒重来的团队。'),
    frozenset(('vision', 'agile')): dict(name='开创者', essence='在模糊里起盘，边做边找方向。',
                                         fits='0 到 1 的新业务、创业团队、内部孵化。',
                                         drains='成熟业务里只做微小优化的岗位。'),
    frozenset(('logic', 'order')): dict(name='规则制定者', essence='标准清楚、依据明确才安心。',
                                        fits='质量体系、合规、法务、审计、标准化类岗位。',
                                        drains='「先干了再说」、规则朝令夕改的环境。'),
    frozenset(('logic', 'agile')): dict(name='战术家', essence='判断快，取舍果断。',
                                        fits='交易、投研、应急决策、竞争分析这类要快速判断的活。',
                                        drains='什么都得层层汇报、迟迟不拍板的组织。'),
    frozenset(('empathy', 'order')): dict(name='守护者', essence='稳定、可依赖，把人也安排进计划里。',
                                          fits='团队运营、HR 流程、教务 / 医疗流程类岗位。',
                                          drains='人事变动频繁、没人管流程的团队。'),
    frozenset(('empathy', 'agile')): dict(name='协调者', essence='随人而变，临场圆场。',
                                          fits='客户关系、社群运营、商务接待、跨文化沟通。',
                                          drains='没有人际互动、节奏死板的工作。'),
}


def _typology(axes_report):
    """判型轴（≥ 略偏）按「明显档优先、强度降序」排序：原型取前两个，rank 保留全部判型极的次序。
    类型名只是速记（见模块说明），报告里同时带上每个判型极的完整画像。"""
    typed = sorted((axis for axis in axes_report if axis['band'] != 'even'),
                   key=lambda axis: (axis['band'] != 'clear', -abs(axis['d'])))
    top = typed[:2]
    names = [axis['pole_name'] for axis in top]
    if len(top) == 2:
        archetype = ARCHETYPES[frozenset(axis['pole'] for axis in top)]
    elif len(top) == 1:
        content = POLE_CONTENT[top[0]['pole']]
        archetype = dict(name=f'{names[0]}型',
                         essence=f'{content["tagline"]}其余三轴没有明显偏向——这份报告先看这一个方向。',
                         fits='、'.join(content['seek']) + '。', drains='、'.join(content['caution']) + '。')
    else:
        archetype = BALANCED
    return dict(
        type=dict(poles=[axis['pole'] for axis in top], names=names,
                  headline=' × '.join(names) if names else BALANCED['name'],
                  rank=[dict(pole=axis['pole'], name=axis['pole_name'], band=axis['band'],
                             band_label=axis['band_label']) for axis in typed],
                  **archetype),
        poles=[dict(key=axis['pole'], band=axis['band'], band_label=axis['band_label'],
                    d=axis['d'], **POLE_CONTENT[axis['pole']]) for axis in typed],
    )


# ── 与 JD 取舍卷互证：风格极 × 取舍卷构念判定 → 印证（match）或取舍（tradeoff）。
# 只在两边都有明确结论时触发（风格极 ≥ 略偏、构念判偏好或回避），最多输出 CROSS_LIMIT 条。 ──

CROSS_RULES = (
    ('solo', 'i1', 'like', 'match', '独自干活时状态最好，也想独立负责一摊事——专精、owner 型的岗位对口。'),
    ('solo', 's2', 'avoid', 'match', '不想频繁跨部门拉通，而你的能量也不靠协调——避开重度协调型岗位对你没有损失。'),
    ('solo', 's3', 'avoid', 'match', '不想频繁对外接触，加上独自专注的取向——面向事情的幕后岗位更省电。'),
    ('solo', 's3', 'like', 'tradeoff', '想多接触客户和外部伙伴，但你的精力来自独处——选对外沟通占比适中、其余时间能自己安排的岗位。'),
    ('solo', 's2', 'like', 'tradeoff', '想做跨部门拉通的事，但独自专注才是你的充电方式——先确认沟通占工时的比例。'),
    ('solo', 's1', 'like', 'tradeoff', '看重互相补位的团队氛围，但节奏偏独——找氛围好、各管一摊的小团队，而不是事事绑在一起的大组。'),
    ('solo', 'i1', 'avoid', 'tradeoff', '独自干活更顺，却不想独立扛一整条线——适合做团队里的深度专家，把拍板和兜底留给别人。'),
    ('team', 's3', 'like', 'match', '你想频繁对接客户和外部伙伴，协作里也确实更来电——接口、客户、对外型岗位对口。'),
    ('team', 's2', 'like', 'match', '想做跨部门拉通，而你的能量正来自和人打交道——协调、推进型岗位对口。'),
    ('team', 'i2', 'like', 'match', '想在小团队里单线汇报——正是你喜欢的协作密度：人不多、够紧密。'),
    ('team', 's3', 'avoid', 'tradeoff', '协作时状态最好，却不想频繁对外——找团队内部高频配合的岗位，少接对外接口的活。'),
    ('team', 's2', 'avoid', 'tradeoff', '喜欢和人一起做事，但不想做跨部门拉通——选边界清晰的小团队，协调成本会低很多。'),
    ('team', 'i1', 'like', 'tradeoff', '想独立负责一摊事，但你的能量来自和人一起干——选自治小队（自己说了算、身边有人），而不是单兵作战。'),
    ('grounded', 'a4', 'like', 'match', '不背硬指标、按质量口碑考核——和「把事做扎实」的取向一致。'),
    ('grounded', 'p2', 'like', 'match', '流程规范、评审齐全——落地型的人在有章可循的环境里最省心。'),
    ('grounded', 'a1', 'like', 'tradeoff', '想从 0 到 1，但你的强项是把方案抠到能执行——你适合 0 到 1 团队里「把蓝图落地」的角色，最好配一个擅长开脑洞的搭档。'),
    ('grounded', 'a1', 'avoid', 'match', '对从 0 到 1 搭新模块无感，更想在成熟方案上做精——成熟业务的岗位更能发挥你。'),
    ('vision', 'a1', 'like', 'match', '满脑子可能性，也想从 0 到 1——新业务、预研、探索型岗位对口。'),
    ('vision', 'p2', 'like', 'tradeoff', '爱开脑洞，又喜欢规范流程——平台型团队里的探索岗（资源足、有沉淀、不卡细节）比早期的混乱更适合你。'),
    ('vision', 'p2', 'avoid', 'match', '不喜欢被流程框住——构想型的人在轻流程的探索团队里更能施展。'),
    ('logic', 'a2', 'like', 'match', '愿意背量化指标——数字面前讲依据，正是你的决策方式。'),
    ('logic', 'r2', 'like', 'match', '想要清晰透明的晋升通道——规则清楚你才好发力。'),
    ('logic', 's1', 'like', 'tradeoff', '对事不对人，同时看重轻松氛围——选直接坦诚、不记仇的团队文化，委婉和办公室政治会消耗你。'),
    ('empathy', 's1', 'like', 'match', '把人的感受放进决策，也看重互相补位的氛围——支持型、服务型岗位能发挥这一点。'),
    ('empathy', 's3', 'like', 'match', '想多接触客户——共情型的人做对外接口有天然优势。'),
    ('empathy', 'a2', 'like', 'tradeoff', '背硬性指标时你会同时顾忌人的感受——优先选指标本身就指向人的岗位（满意度、留存、服务质量）。'),
    ('order', 'p2', 'like', 'match', '流程规范、评审文档齐全——和你按计划推进的节奏一致。'),
    ('order', 'i1', 'like', 'match', '想自主安排优先级——有秩序的人自己排期最顺。'),
    ('order', 'w1', 'like', 'match', '想要双休弹性、节奏可预期——计划型的人最怕作息被随时打乱。'),
    ('order', 'p2', 'avoid', 'tradeoff', '做事讲秩序，却不喜欢被流程管——你要的是目标清楚、过程自己定，而不是没有章法。'),
    ('order', 'w2', 'like', 'tradeoff', '能接受冲刺式加班，但你的节奏依赖稳定计划——确认冲刺是偶发还是常态，常态冲刺会反复打乱你的排期。'),
    ('agile', 'i1', 'like', 'match', '想自己排优先级——随机应变的人需要这个自由度。'),
    ('agile', 'i3', 'like', 'match', '想要实现路径的自主权——见招拆招的人不该被钉死方案。'),
    ('agile', 'p2', 'like', 'tradeoff', '边做边调，又喜欢规范流程——成熟平台里的探索型岗位能两头兼顾。'),
    ('agile', 'i1', 'avoid', 'tradeoff', '随机应变，却不想自己排优先级——跟着方向感强的负责人打配合，比自己扛更顺。'),
)
CROSS_SIDES = {'like': ('like', 'strong_like'), 'avoid': ('avoid', 'strong_avoid')}
CROSS_KINDS = {'match': '印证', 'tradeoff': '取舍'}


def cross_report(style_report, bws_report):
    """风格卷报告 × 取舍卷报告 → 互证结论；任一方缺失或是旧版题型报告时返回 None。
    纯函数、读两份已存报告，所以两卷谁后做都能立刻互证，不需要回写旧报告。
    风格卷四个轴都不明显时 poles 为空，互证结论是「没有印证或冲突」，不是 None。"""
    if (not isinstance(style_report, dict) or not isinstance(bws_report, dict)
            or not style_report.get('axes') or not bws_report.get('features')):
        return None
    if any('verdict' not in feature for feature in bws_report['features']):
        return None                              # 旧版（两张岗位卡二选一）的取舍卷报告没有 verdict
    poles = {pole['key']: pole for pole in style_report['poles']}
    features = {feature['key']: feature for feature in bws_report['features']}
    notes = []
    for index, (pole, key, side, kind, text) in enumerate(CROSS_RULES):
        style, feature = poles.get(pole), features.get(key)
        if not style or feature['verdict'] not in CROSS_SIDES[side]:
            continue
        notes.append(dict(
            kind=kind, kind_label=CROSS_KINDS[kind], text=text,
            style_pole=pole, style_name=POLE_CONTENT[pole]['name'], style_band=style['band'],
            bws_key=key, bws_label=feature['screen'], bws_phrase=feature['phrase'],
            bws_best=feature['best'], bws_worst=feature['worst'], bws_n=feature['n_present'], _order=index,
        ))
    # 明显档的结论先出；同档里「取舍」比「印证」更值得先看。
    notes.sort(key=lambda note: (note['style_band'] != 'clear', note['kind'] != 'tradeoff', note['_order']))
    notes = [{key: value for key, value in note.items() if key != '_order'} for note in notes[:CROSS_LIMIT]]
    matches = sum(note['kind'] == 'match' for note in notes)
    tradeoffs = len(notes) - matches
    if not notes:
        summary = '两卷之间没有明显的印证或冲突，各看各的结论即可。'
    elif not tradeoffs:
        summary = f'{matches} 条互相印证：你的工作风格和择业诉求基本自洽。'
    elif not matches:
        summary = f'{tradeoffs} 条需要权衡：风格和诉求之间有几处张力，投递前值得先想清楚。'
    else:
        summary = f'{matches} 条互相印证、{tradeoffs} 条需要权衡。'
    return dict(notes=notes, matches=matches, tradeoffs=tradeoffs, summary=summary)
