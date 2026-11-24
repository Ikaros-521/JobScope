"""反向JD测评（2026-11-22 首版，2026-11-24 题型改 best-worst 选题）：真实 JD 片段里点出
「最想投」和「最不想投」各一条 → 隐性筛选条件报告。纯规则，无 LLM、确定性、可测。

为什么换题型（2026-11-24 诊断与仿真，下面是旧题型的实测数字）：旧版是「两张
各 3 条片段的岗位卡二选一」——每题 6 条构念、18 条构念各出现 12 次（每 3 题一次），且每次
观测都被同卡另 5 条稀释：重复感强（每题平均 5.5 道题有卡与其 2/3 片段相同）而精度低
（纯中性人格误报实测 13.6% / 3.0%，真效应 1.2–1.8 的偏好检出率约 25%）。best-worst 选题
（MaxDiff）每题只放 4 条、点「最想投」「最不想投」各一，每次出场都是 4 条里的直接比较，
不再稀释：同样 40 题下每构念出场 8 次，纯中性误报 7.7% / 0.4%、检出率 50% / 76% / 95%
（真效应 1.2–1.8 / 1.8–2.5 / ≥2.5），构念密度从 1/3 降到 2/9，且没有孪生题。

题型对标 best-worst scaling 的 object case：与其让用户给每条片段打分（人人都会说「重要」），
不如强迫在 4 条里挑出相对最想要和最不想要的，得到可比较的相对倾向。
维度词表取 O*NET Work Values 六维（Achievement / Independence / Recognition / Relationships /
Support / Working Conditions）的中文转译，每维 3 条构念，每条 3 条同义变体。
报告输出「含「独立负责」的片段 8 次出现里你 6 次选为最想投」，而非人格标签。

区组设计（质量闸门，tests/test_jd_assessment.py 逐项断言）：
1. 平衡：每条构念恰出场 8 次；任意两条构念同组 1–2 次（216 个配对槽 / 153 个构念对 =
   90 对×1 + 63 对×2，零共现对为 0）；同块同维最多 2 条，18 个同维对各同组 1–2 次（维内
   对照）；没有两道题共享 ≥3 条（无孪生题）。设计是 0-1 整数规划的精确解（output/gen_blocks_36.py）。
2. 间隔：题序由间隔感知排序决定（相邻题基本不共享构念、同一句原文从不相邻），不是裸洗牌。
3. 质检：3 道重复校验题（原题隔 ≥10 题原样重现，测一致性）+ 1 道注意力题（一好一坏两条
   显性片段 + 2 条中性片段，测注意力）+ 反应时下限。质检题不进计分。
题序 / 块内条目顺序 / 同义变体由会话种子决定——计分按键池化，重测必换卷。

题量为什么是 36 而不是 27：每构念 6 次出场（27 题）时计分只有 7 个取值，判定只能在
「误报 14.8%、检出 52% / 77% / 94%」与「误报 3.9%、检出 27% / 52% / 80%」之间二选一；
8 次出场才能同时压住误报、保住灵敏度（output/compare_27_vs_36.py）。重复感只取决于构念
密度（每题 4 条 / 18 条）和孪生题，两版相同，所以多出的 9 题只换来精度。

计分：有界计分 = 被选「最想投」的比例 − 被选「最不想投」的比例，取值 [-1, 1]。
不用 logit 权重是因为出场次数少时权重的零分布重尾（近可分时发散），阈值没法稳定校准；
有界计分无需迭代、可解释，仿真里与岭回归 logit 的秩相关相当（0.81 vs 0.82）。
判定阈值由纯中性人格仿真校准（output/calibrate_bws_rules.py），见 SCORE_LIKE / SCORE_STRONG。
"""
import math
import random as _random

# ── 词表：6 维 × 3 构念。phrase 是报告引用的正典表述，variants 是出题用的同义变体
#（同一构念、JD 原文式中性陈述，只换措辞不换强度——计分按 key 池化全部变体）。
# screen 是用户回头筛岗位时的关键词形态。片段本身不带褒贬引导——报告反映用户自己的反应。
# 顺序即设计下标（维内分组连续），改顺序或增删必须重新生成 _DESIGNED_BLOCKS。 ──

DIMENSIONS = [
    dict(key='achievement', label='成就攻坚', onet='Achievement'),
    dict(key='independence', label='独立自主', onet='Independence'),
    dict(key='recognition', label='认可回报', onet='Recognition'),
    dict(key='relationships', label='关系协作', onet='Relationships'),
    dict(key='support', label='组织支撑', onet='Support'),
    dict(key='conditions', label='工作条件', onet='Working Conditions'),
]

FEATURES = [
    dict(key='a1', dim='achievement', phrase='从0到1搭建新模块，对交付结果负责', screen='从0到1',
         variants=['牵头把新业务从想法做到上线，交付质量由你兜底', '负责没人趟过路的新模块，从设计到落地全程主导']),
    dict(key='a2', dim='achievement', phrase='背量化业绩指标，按季度结果考核', screen='量化指标',
         variants=['目标全部数字化，季度考核只看结果达成', '绩效与可量化产出直接挂钩，达成多少算多少']),
    dict(key='a4', dim='achievement', phrase='考核看交付质量与同事口碑，不设硬性数字指标', screen='质量导向考核',
         variants=['绩效评价以交付质量和口碑为主，没有硬性 KPI 数字', '不背数字指标，按交付质量和协作口碑评级']),
    dict(key='i1', dim='independence', phrase='独立负责一条业务线，自主安排优先级', screen='独立负责',
         variants=['一条产品线只归你管，节奏和优先级自己定', '手头项目自主决策，不用事事对齐']),
    dict(key='i2', dim='independence', phrase='小团队作战，单线汇报，决策链短', screen='单线汇报',
         variants=['团队精简，只向一位负责人汇报，拍板快', '扁平架构，重大决策当天就能定']),
    dict(key='i3', dim='independence', phrase='需求只提目标，怎么实现由工程师自己定', screen='实现自主权',
         variants=['产品给目标和边界，技术方案工程师说了算', '实现路径自主选择，只对交付结果负责']),
    dict(key='r1', dim='recognition', phrase='薪资高于同类岗位，调薪机制明确', screen='薪资高于市场',
         variants=['薪水在同类岗位里属高位，调薪规则写进制度', '薪酬定位高于市场同类，调薪窗口固定']),
    dict(key='r2', dim='recognition', phrase='晋升通道清晰，职级体系透明', screen='晋升通道清晰',
         variants=['职级标准公开，晋升路径和周期都明确', '上升通道写得很清楚，到什么标准拿什么职级']),
    dict(key='r3', dim='recognition', phrase='项目成果对外可见，有署名和案例沉淀', screen='成果署名',
         variants=['做的项目会署名，成果案例对外展示', '产出可被外部看见，署名与案例都归你']),
    dict(key='s1', dim='relationships', phrase='团队氛围轻松，同事靠谱互相补位', screen='团队氛围好',
         variants=['团队磨合成熟，关键时候有人搭手', '氛围松弛，分工之外愿意互相顶上']),
    dict(key='s2', dim='relationships', phrase='需要频繁跨部门拉通对齐', screen='频繁跨部门',
         variants=['大量时间花在跨团队沟通和对齐会上', '工作重心包含跨部门协调与推进']),
    dict(key='s3', dim='relationships', phrase='工作需要频繁接触客户或外部合作方', screen='频繁对外接触',
         variants=['岗位对外接口属性强，常与客户和合作方打交道', '需要经常出面对接客户与外部伙伴']),
    dict(key='p1', dim='support', phrase='有导师带教和体系化的培训机制', screen='导师带教',
         variants=['入职有导师一对一带，培训体系成建制', '成长有专人护航，技能培训排进工时']),
    dict(key='p2', dim='support', phrase='工程流程规范，评审与文档机制齐全', screen='流程规范',
         variants=['研发流程成熟，代码评审和文档是硬性环节', '规范先行：评审、文档、发布全流程有章法']),
    dict(key='p3', dim='support', phrase='工具链与内部平台完善，不用自己造轮子', screen='基建完善',
         variants=['内部平台和工具链齐全，开箱即用', '基础设施完善，精力花在业务而不是轮子上']),
    dict(key='w1', dim='conditions', phrase='双休，弹性打卡，支持混合远程', screen='双休弹性',
         variants=['五天工作制，上下班时间灵活，可混合办公', '标准双休，打卡时间自定，每周可远程']),
    dict(key='w2', dim='conditions', phrase='版本期常态加班，冲刺节奏', screen='常态加班',
         variants=['发版前连续加班是常态，节奏偏冲刺型', '上线窗口期高强度作战，加班随版本走']),
    dict(key='w4', dim='conditions', phrase='常驻客户现场办公，随项目地点迁移', screen='常驻现场',
         variants=['驻场办公为主，地点随项目走', '长期在客户现场工作，随项目搬迁']),
]

# 注意力校验题：一好一坏两条显性片段（常识上严格分高低）+ 两条中性片段凑成 4 条。
# 认真作答应把第 1 条选为最想投、第 2 条选为最不想投。片段全部专用，不进特征统计池；
# 会话种子会打乱这 4 条的呈现顺序，判定按文本对回，不受位置习惯影响。
ATTENTION_TRIAL = dict(
    id='AT1',
    items=['双休不加班，弹性打卡', '大小周，工作晚间常态加班', '负责业务模块的日常开发与维护',
           '编写和维护项目技术文档'],
)
ATTENTION_BEST, ATTENTION_WORST = 0, 1     # ATTENTION_TRIAL['items'] 里的下标

BLOCK_SIZE = 4                  # 每题 4 条构念片段
N_BLOCKS = 36                   # 36 道正式题 → 每构念出场 8 次
DESIGN_SEED = 20261122

# 40 题序列 = 36 正式 + 1 注意力 + 3 重复校验。重复校验题是正式题的精确复制，分布在序列中后段、
# 与原题隔 ≥10 题，用来测前后一致性——孤立的选择是噪声，重复题上的稳定选择才是信号。
# ANCHOR_SOURCES 是排序后正式题序列里的下标；它们只进质检、不进计分（同一题数两遍会虚增样本）。
ANCHOR_SOURCES = (3, 12, 20)
_ORDER_WEIGHTS = (20.0, 3.0, 1.0, 0.5, 0.25)   # 隔 1..5 题的重复代价权重


# best-worst 区组设计（2026-11-24 固化）：36 题，每题 [4 条构念下标]。由 0-1 整数规划求得
# （候选块 = 全部 4 元子集去掉同维 3 条的；约束 = 每构念恰 8 次、每对同组 1–2 次、共 36 块、
# 孪生块对互斥，脚本 output/gen_blocks_36.py，HiGHS 求解）。已验证的不变量：
#   1. 频次：任一构念恰出场 8 次；
#   2. 共现：任意两条构念同组 1–2 次——90 对×1 + 63 对×2，零共现对为 0；
#      同维对（18 个）8 对×1 + 10 对×2，同块同维最多 2 条；
#   3. 无孪生题：没有两道题共享 ≥3 条构念。
# 改动 FEATURES 数量或顺序会使下标失效，必须重新生成此常量。
_DESIGNED_BLOCKS = [
    [0, 1, 3, 15],
    [0, 1, 7, 10],
    [0, 2, 5, 13],
    [0, 2, 9, 16],
    [0, 3, 8, 12],
    [0, 4, 7, 17],
    [0, 5, 11, 14],
    [0, 6, 10, 11],
    [1, 2, 4, 11],
    [1, 2, 6, 15],
    [1, 5, 9, 17],
    [1, 6, 8, 13],
    [1, 11, 12, 16],
    [1, 13, 14, 16],
    [2, 3, 10, 14],
    [2, 7, 8, 14],
    [2, 7, 15, 16],
    [2, 10, 12, 17],
    [3, 4, 11, 13],
    [3, 4, 15, 17],
    [3, 5, 12, 16],
    [3, 6, 7, 16],
    [3, 9, 10, 13],
    [4, 5, 7, 8],
    [4, 6, 14, 15],
    [4, 8, 10, 16],
    [4, 9, 12, 14],
    [5, 6, 8, 9],
    [5, 6, 12, 13],
    [5, 10, 11, 15],
    [6, 12, 14, 17],
    [7, 9, 11, 13],
    [7, 9, 12, 15],
    [8, 9, 11, 17],
    [8, 13, 15, 17],
    [10, 14, 16, 17],
]


def _greedy_order(cost, rng):
    """最近邻贪心初始解：每一步接上与上一题重复代价最低的未排题（代价并列时随机）。"""
    current = rng.randrange(len(cost))
    order, left = [current], set(range(len(cost))) - {current}
    while left:
        current = min(left, key=lambda j: (cost[current][j], rng.random()))
        order.append(current)
        left.discard(current)
    return order


def _spaced_order(cost, rng, iters=5000):
    """间隔感知题序：模拟退火最小化 Σ 隔 d 题的重复代价（d=1..5，权重递减）。
    cost[i][j] = 两题共享构念数 + 4×同一句原文数。旧版这里是裸 shuffle，实测 35 对相邻题里
    15 对含同一句原文、同一构念最长连出 5 题。
    移动用「插入」（抽出第 x 题插到第 y 位，60%）与「对换」（40%）两种——只做对换时相邻题仍会
    残留共享。权重是权衡出来的（output/tune_order_weights.py）：隔 1 题权重 10 时优化器宁可让
    相邻题共享构念（代价 10）也要避开「隔一题重现同一句」（(1+4)×3=15）；提到 20 后相邻题
    共享构念降到 ~0.06 条/卷、同一句原文从不相邻。剩余重复在「隔 1–2 题再出现」，那是构念
    密度的数学下限（每条构念 8 次出场摊在 36 题，总有一半的重现间隔 ≤3），消不掉。
    单卷构建约 300ms，按种子缓存后只付一次。"""
    n = len(cost)
    weights = _ORDER_WEIGHTS
    span = len(weights)

    def edges_at(p):
        for d, w in enumerate(weights, start=1):
            if p - d >= 0:
                yield (p, d, w)
            if p + d < n:
                yield (p + d, d, w)

    order = _greedy_order(cost, rng)

    def energy(edges):
        return sum(w * cost[order[k - d]][order[k]] for k, d, w in edges)

    cur = best = energy([e for k in range(n) for e in edges_at(k) if e[0] == k])
    best_order = list(order)
    t0, t1 = 5.0, 0.05
    for it in range(iters):
        temp = t0 * (t1 / t0) ** (it / iters)
        x, y = rng.sample(range(n), 2)
        if rng.random() < 0.6:
            lo, hi = min(x, y), max(x, y)
            touched = {e for p in range(max(0, lo - span), min(n, hi + span + 1)) for e in edges_at(p)}
            before = energy(touched)
            item = order.pop(x)
            order.insert(y, item)
            new = cur - before + energy(touched)
            take = new <= cur or rng.random() < math.exp(-(new - cur) / temp)
            if not take:
                order.pop(y)
                order.insert(x, item)
        else:
            touched = {e for p in (x, y) for e in edges_at(p)}
            before = energy(touched)
            order[x], order[y] = order[y], order[x]
            new = cur - before + energy(touched)
            take = new <= cur or rng.random() < math.exp(-(new - cur) / temp)
            if not take:
                order[x], order[y] = order[y], order[x]
        if take:
            cur = new
            if new < best:
                best, best_order = new, list(order)
    return best_order


def _build_trials(seed):
    """seed 决定表层（题序/变体/块内条目顺序/注意力题顺序），设计骨架 _DESIGNED_BLOCKS 不变——
    所以任何种子下按键计算的设计不变量（平衡/共现/锚点）都与默认卷完全一致。"""
    rng = _random.Random(seed)
    texts_of = {f['key']: [f['phrase'], *f['variants']] for f in FEATURES}
    # 每条构念一个轮换牌组：洗牌后逐次取用、用尽重洗。8 次出场对 3 条变体 = 每句 2–3 次，
    # 一轮（3 次出场）内同一句至多出现一次；跨轮可能挨着，交给后面的题序排序隔开。
    decks = {key: [] for key in texts_of}

    def _text_for(key):
        if not decks[key]:
            decks[key] = list(texts_of[key])
            rng.shuffle(decks[key])
        return decks[key].pop()

    key_of = [f['key'] for f in FEATURES]
    blocks = []
    for index, feats in enumerate(_DESIGNED_BLOCKS):
        items = [_text_for(key_of[k]) for k in feats]
        rng.shuffle(items)          # 块内位置随机化：位置习惯（总点第一条）不偏袒任何构念
        blocks.append(dict(id=f'T{index + 1:02d}', items=items))

    n = len(blocks)
    cost = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            shared = len(set(_DESIGNED_BLOCKS[i]) & set(_DESIGNED_BLOCKS[j]))
            same_text = len(set(blocks[i]['items']) & set(blocks[j]['items']))
            cost[i][j] = cost[j][i] = shared + 4.0 * same_text
    order = _spaced_order(cost, rng)
    blocks = [blocks[i] for i in order]

    # 锚点按「排序后的序列位置」复制，来源 id 必须记实际 id（id 是设计序号，与位置无关）。
    anchor_of = {f'AC{i + 1}': blocks[src]['id'] for i, src in enumerate(ANCHOR_SOURCES)}

    def _copy(i):
        return dict(id=f'AC{i + 1}', items=list(blocks[ANCHOR_SOURCES[i]]['items']))

    attention_items = list(ATTENTION_TRIAL['items'])
    rng.shuffle(attention_items)
    # 排布：u[:13] AC1 u[13:25] AT1 u[25:27] AC2 u[27:32] AC3 u[32:] → 锚点与原题相隔 10 / 17 / 14 题。
    sequence = (blocks[:13] + [_copy(0)] + blocks[13:25]
                + [dict(id=ATTENTION_TRIAL['id'], items=attention_items)]
                + blocks[25:27] + [_copy(1)] + blocks[27:32] + [_copy(2)] + blocks[32:])
    return sequence, anchor_of


_SESSION_CACHE = {}


def _session(seed):
    key = DESIGN_SEED if seed is None else int(seed)
    if key not in _SESSION_CACHE:
        if len(_SESSION_CACHE) >= 64:      # 会话级缓存兜底，防长驻进程无限增长
            _SESSION_CACHE.clear()
        _SESSION_CACHE[key] = _build_trials(key)
    return _SESSION_CACHE[key]


def trial_list(seed=None):
    """题序列随会话种子变化（缺省用固定种子，等价于旧行为），进程内按种子缓存；
    前端按此渲染与回传 trial_id，提交时带上同一 seed 供服务端复现锚点配对。"""
    return {'trials': _session(seed)[0]}


def trials(seed=None):
    return _session(seed)[0]


def anchor_of(seed=None):
    return _session(seed)[1]


# ── 计分。RT_FLOOR 以下的作答视为没读题；重复校验题/注意力题的结果进质检不进结论。 ──

RT_FLOOR = 800
MIN_FEATURE_N = 6            # 构念有效出场次数低于此（8 次里被剔除超过 2 次）→ 该条低置信
MIN_VALID = 24               # 有效正式题少于 36 的 2/3 → 建议重测
# 判定阈值作用于有界计分（最想投率 − 最不想投率），由纯中性人格仿真校准（毫无偏好、每题随机
# 点两条：output/calibrate_bws_rules.py）。旧版二选一的阈值在纯中性人格下实测误报 13.6% / 3.0%
# （注释自称 8% / 1%），所以这次按纯中性口径定，并且取能同时压住误报、保住灵敏度的题量：
#   构念级：净差/出场 ≥ 1/2（8 次里差 4 次）→ 倾向，误报 7.7%，真效应 1.2–1.8 / 1.8–2.5 / ≥2.5
#           的检出率 50% / 76% / 95%；≥ 3/4（差 6 次）→ 明显，误报 0.4%。
#   维度级：3 条构念计分的均值，噪声只有单条的 1/√3，阈值相应收紧（均值 ≥ 1/3 倾向 1.9%、≥ 1/2 明显 0.02%）。
SCORE_LIKE, SCORE_STRONG = 1 / 2, 3 / 4
DIM_LIKE, DIM_STRONG = 0.33, 0.5

PHRASE_TO_FEATURE = {text: f for f in FEATURES for text in (f['phrase'], *f['variants'])}
FEATURE_BY_KEY = {f['key']: f for f in FEATURES}
DIMENSION_BY_KEY = {d['key']: d for d in DIMENSIONS}

VERDICT_LABELS = {
    'strong_like': '明显偏好', 'like': '倾向偏好', 'neutral': '无明显倾向',
    'avoid': '倾向回避', 'strong_avoid': '明显回避', 'uncertain': '样本不足',
}


def _verdict(score, like, strong, low_confidence):
    """判定看有界计分（净方向与强度），幅度阈值经中性人格仿真校准。"""
    if low_confidence:
        return 'uncertain'
    if score >= strong:
        return 'strong_like'
    if score >= like:
        return 'like'
    if score > -like:
        return 'neutral'
    if score > -strong:
        return 'avoid'
    return 'strong_avoid'


def _line(kind, keys):
    if not keys:
        if kind == 'prefer':
            return '本卷没有检测到足够强的正向偏好，先维持现有筛选方式，样本攒够再回来看。'
        return '没有检测到你明确想回避的片段，不必硬凑回避清单。'
    words = '、'.join(f'「{FEATURE_BY_KEY[key]["screen"]}」' for key in keys)
    if kind == 'prefer':
        return f'下一轮投递，优先看含 {words} 的岗位。'
    return f'对含 {words} 的岗位先降级或直接跳过，别花时间细读。'


def score_responses(responses, seed=None):
    """responses: [{trial_id, best, worst, rt_ms}]（全量；best/worst 是该题 items 的下标）；
    seed 必须与取题时一致（决定题序与锚点配对，计分本身与种子无关）。返回报告 dict；
    非法输入抛 ValueError。"""
    all_trials = trials(seed)
    trial_map = {t['id']: t for t in all_trials}
    if len(responses) != len(all_trials):
        raise ValueError(f'作答数量不符：应答 {len(all_trials)} 题，收到 {len(responses)} 题')
    seen = set()
    records, valid, excluded, fast = {}, [], [], []
    for row in responses:
        trial_id = row.get('trial_id') or ''
        if trial_id not in trial_map or trial_id in seen:
            raise ValueError(f'作答含未知或重复的题目编号：{trial_id}')
        seen.add(trial_id)
        best, worst = row.get('best'), row.get('worst')
        items = trial_map[trial_id]['items']
        if not (type(best) is int and type(worst) is int
                and 0 <= best < len(items) and 0 <= worst < len(items)):
            raise ValueError(f'作答选项非法：{trial_id} 的最想/最不想下标越界')
        if best == worst:
            raise ValueError(f'作答选项非法：{trial_id} 的最想投与最不想投是同一条')
        rt = int(row.get('rt_ms') or 0)
        record = dict(trial=trial_map[trial_id], best=best, worst=worst, trial_id=trial_id, fast=rt < RT_FLOOR)
        records[trial_id] = record
        if record['fast']:
            fast.append(record)
        if trial_id.startswith('T'):          # 只有正式题进计分
            (valid if not record['fast'] else excluded).append(record)

    valid_n = len(valid)
    flags = []
    attention_items = ATTENTION_TRIAL['items']
    attention = records.get(ATTENTION_TRIAL['id'])
    attention_pass = False
    if attention is None or attention['fast']:
        flags.append('注意力校验题被判定为过快作答')
    else:
        attention_pass = (attention['trial']['items'][attention['best']] == attention_items[ATTENTION_BEST]
                          and attention['trial']['items'][attention['worst']] == attention_items[ATTENTION_WORST])
        if not attention_pass:
            flags.append('注意力校验未通过，结果可信度低')

    anchor_results = []
    for anchor_id, source_id in anchor_of(seed).items():
        anchor, source = records.get(anchor_id), records.get(source_id)
        if anchor and source:
            anchor_results.append((anchor['best'], anchor['worst']) == (source['best'], source['worst']))
    anchor_agreement = sum(anchor_results)
    if len(anchor_results) == len(ANCHOR_SOURCES) and anchor_agreement <= 1:
        flags.append('重复题前后不一致，建议重测')

    if len(fast) / len(all_trials) > 0.3:
        flags.append(f'过快作答过多（{len(fast)} 题），建议重测')
    if valid_n < MIN_VALID:
        flags.append(f'有效样本不足（{valid_n} 题），建议重测')

    # 有界计分：每次出场记 +1（被选最想投）/ −1（被选最不想投）/ 0，计分 = 均值。
    # 卡上文本可能是任一变体——映射按 key 池化，与种子/变体选择无关。
    feature_keys = {text: f['key'] for text, f in PHRASE_TO_FEATURE.items()}
    present = {f['key']: 0 for f in FEATURES}
    picks = {f['key']: 0 for f in FEATURES}
    marks = {f['key']: [] for f in FEATURES}
    for r in valid:
        for idx, text in enumerate(r['trial']['items']):
            key = feature_keys[text]
            present[key] += 1
            mark = 1 if idx == r['best'] else -1 if idx == r['worst'] else 0
            picks[key] += mark
            marks[key].append(mark)

    features_report = []
    for f in FEATURES:
        key = f['key']
        n_present = present[key]
        low_confidence = n_present < MIN_FEATURE_N or valid_n < MIN_VALID
        score = picks[key] / n_present if n_present else 0.0
        # 标准误取逐次出场记号的样本标准差 / √n（中性人格下 ≈ 0.707/√8 ≈ 0.25）。
        if n_present >= 2:
            variance = sum((m - score) ** 2 for m in marks[key]) / (n_present - 1)
            se = math.sqrt(variance / n_present)
        else:
            se = 1.0
        best_n = sum(1 for m in marks[key] if m == 1)
        worst_n = sum(1 for m in marks[key] if m == -1)
        features_report.append(dict(
            key=key, dim=f['dim'], dim_label=DIMENSION_BY_KEY[f['dim']]['label'],
            phrase=f['phrase'], screen=f['screen'],
            n_present=n_present, best=best_n, worst=worst_n,
            best_rate=round(best_n / n_present, 4) if n_present else 0.0,
            worst_rate=round(worst_n / n_present, 4) if n_present else 0.0,
            score=round(score, 4), se=round(se, 4),
            verdict=_verdict(score, SCORE_LIKE, SCORE_STRONG, low_confidence),
            low_confidence=low_confidence,
        ))
    # 展示与结论排序都按计分（净方向）。
    features_report.sort(key=lambda item: -item['score'])

    dimensions_report = []
    for d in DIMENSIONS:
        members = [item for item in features_report if item['dim'] == d['key']]
        mean_score = sum(item['score'] for item in members) / len(members)
        dimensions_report.append(dict(key=d['key'], label=d['label'], onet=d['onet'],
                                      score=round(mean_score, 4),
                                      verdict=_verdict(mean_score, DIM_LIKE, DIM_STRONG,
                                                       any(item['low_confidence'] for item in members))))

    prefer = [item['key'] for item in features_report if item['verdict'] in ('strong_like', 'like')][:4]
    avoid = [item['key'] for item in reversed(features_report) if item['verdict'] in ('avoid', 'strong_avoid')][:4]
    grade = 'weak' if flags else 'ok'
    report = dict(
        quality=dict(attention_pass=attention_pass, anchor_agreement=anchor_agreement,
                     anchors=len(ANCHOR_SOURCES), rt_excluded=len(fast),
                     valid_trials=valid_n, total_trials=len(all_trials), flags=flags, grade=grade),
        features=features_report,
        dimensions=dimensions_report,
        actions=dict(prefer=prefer, avoid=avoid,
                     prefer_line=_line('prefer', prefer), avoid_line=_line('avoid', avoid)),
        note='结果只反映你在这组片段上的相对倾向，不是岗位市场判断；差异小的条目仅供参考。'
             '质检全过（重复一致 3/3、注意力通过、无过快作答）时结果最可信。',
    )
    return report
