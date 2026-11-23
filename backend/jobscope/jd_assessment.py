"""反向JD测评（2026-11-22 首版，2026-11-23 扩池）：真实JD片段二选一 → 隐性筛选条件报告。
纯规则，无 LLM、确定性、可测。

形态对标强制选择（forced-choice）实验范式：每题两张由 JD 原文式片段拼成的岗位卡，
强制选一张更愿意投的。维度词表取 O*NET Work Values 六维（Achievement / Independence /
Recognition / Relationships / Support / Working Conditions）的中文转译，每维 3 条特征片段
（2026-11-23 由 2 扩到 3：12 条构念撑 48 题时相邻题平均共享 3/6 构念，做题像重复；
18 条压到 2/6，同时每条文本配 3 条同义变体）。构念池取 18 而非更大，是统计力与
重复感的折中：48 对 × 3+3 卡让每特征恰 16 次观测，判定阈值不用大幅放宽。
设计目标是把「MBTI 式的巴纳姆标签」换成「具体到片段的反应率与筛选建议」：
报告输出「含「独立负责」的片段你选中率 82%，含「频繁跨部门」的仅 23%」，而非人格标签。

配对设计（质量闸门，tests/test_jd_assessment.py 逐项断言）：
1. 平衡：每条特征恰出现 16 次、左右卡位各 8（特征固定一侧）——AMCE 不被位置/频次混淆；
2. 洁净：同卡同维最多一条、AB 卡不共享特征或填充片段；同卡共现均匀
   （288 槽 / 135 跨维对：93 对×2 + 34 对×3 + 8 对×0，max=3，均值 2.13）。
   跨卡共现不设硬平衡（扩池后组合不可行），由差向量回归显式建模校正；
3. 质检：3 道前后重复锚点题（测一致性）+ 1 道显性占优题（测注意力）+ 反应时下限。

表层多样性（2026-11-23）：每条特征 3 条同义变体轮换、填充 24 条，题序/变体/填充由会话种子
决定——计分按键池化，变体只换皮不换骨；同一条文本整卷曝光 ~5.3 次，重测必换卷。

计分：选中率差（AMCE = 含该片段的卡被选比例 − 不含的被选比例）为主结论，
条件逻辑斯蒂回归（岭正则，纯 Python）权重仅作共现校正的排序参考。
"""
import math
import random as _random

# ── 词表：6 维 × 4 特征。phrase 是报告引用的正典表述，variants 是出题用的同义变体
#（同一构念、JD 原文式中性陈述，只换措辞不换强度——计分按 key 池化全部变体）。
# screen 是用户回头筛岗位时的关键词形态。片段本身不带褒贬引导——报告反映用户自己的反应。
# 顺序即设计下标（维内分组连续），改顺序或增删必须重新生成 _DESIGNED_TRIALS。 ──

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

FILLERS = [
    '负责业务模块的日常开发与维护',
    '参与需求评审，输出技术方案',
    '配合测试完成版本发布',
    '编写和维护项目技术文档',
    '优化既有功能的性能与稳定性',
    '跟进线上问题并修复',
    '定期同步开发进度',
    '完成日常迭代任务',
    '参与技术方案讨论，推动方案落地',
    '编写单元测试与集成测试',
    '与产品经理对齐需求细节',
    '维护服务可用性，响应线上告警',
    '梳理接口文档与调用关系',
    '在既有代码基础上做增量开发',
    '协助定位排查线上环境问题',
    '参加评审会，给出技术意见',
    '参加需求澄清会议并确认排期',
    '编写接口联调文档并与前端对齐',
    '排查并修复偶发的超时问题',
    '根据反馈调整既有模块的交互细节',
    '参与周会同步本周进展与风险',
    '完善监控告警与值班响应流程',
    '梳理上线步骤并执行发布检查单',
    '整理故障复盘结论并落实改进项',
]

# 注意力校验题：A 卡在常识上严格占优（休息/透明/流程），选 B 视为未认真作答。
# 片段全部专用，不进特征统计池，避免污染 AMCE。
ATTENTION_TRIAL = dict(
    id='AT1',
    a=['双休不加班，弹性打卡', '尊重生活边界，不鼓励无效加班', '按法定节假日放假', '需求稳定，不频繁变更'],
    b=['大小周，工作日晚间常态加班', '调薪机制不透明', '流程混乱，需求频繁变更', '通勤远且需常驻现场'],
)

FEATURES_PER_CARD = 3          # 每卡 3 条特征 + 1 条填充片段 = 4 条
N_PAIRS = 48                   # 48 组唯一配对 → 每特征 6 左 + 6 右 = 12 次
DESIGN_SEED = 20261122

# 52 题序列：48 唯一 + 1 注意力 + 3 锚点。锚点是唯一配对的精确重复（间隔 ≥20 题），
# 用来测前后一致性——孤立的选择是噪声，重复题上的稳定选择才是信号。
ANCHOR_SOURCES = (4, 13, 22)   # 唯一序列中的下标（即第 5 / 14 / 23 题）


def _edge(i, j):
    return (i, j) if i < j else (j, i)


# 成对平衡设计（2026-11-23 扩池版固化）：48 组试次，每组 [A 卡特征下标, B 卡特征下标]。
# 生成器：维三元组多重集构造（每维恰 48 槽）→ 分侧局部搜索（每特征 A 卡恰 8，特征固定一侧）
# → AB 不相交贪心配对 → 维内换位模拟退火抛光共现均匀度（脚本 output/gen_design_18.py）。
# 已验证的不变量：
#   1. 频次：任一特征恰出现 16 次，其中左卡恰 8 次（精确）；
#   2. 洁净：每卡 3 特征分属 3 个不同维度，AB 卡特征不相交；
#   3. 同卡共现：288 槽摊到 135 个跨维特征对——93 对×2 + 34 对×3 + 8 对×0，max=3，均值 2.13。
# 跨卡共现不设硬平衡（扩池后组合不可行），其混淆由差向量回归显式建模校正。
# 改动 FEATURES 数量或顺序会使下标失效，必须重新生成此常量。
_DESIGNED_TRIALS = [
    [[2, 10, 13], [1, 5, 7]],
    [[7, 9, 14], [4, 10, 17]],
    [[9, 14, 17], [5, 12, 15]],
    [[4, 11, 15], [1, 9, 12]],
    [[6, 9, 16], [0, 8, 14]],
    [[0, 12, 17], [8, 11, 16]],
    [[2, 7, 15], [4, 8, 14]],
    [[0, 5, 16], [6, 9, 12]],
    [[0, 5, 10], [2, 4, 17]],
    [[3, 6, 17], [5, 7, 11]],
    [[3, 14, 15], [2, 12, 16]],
    [[0, 4, 6], [8, 11, 17]],
    [[5, 8, 10], [0, 7, 14]],
    [[4, 6, 12], [5, 14, 15]],
    [[1, 11, 12], [4, 8, 9]],
    [[10, 14, 16], [2, 7, 15]],
    [[11, 13, 17], [0, 3, 12]],
    [[7, 12, 17], [2, 3, 6]],
    [[1, 8, 13], [4, 7, 12]],
    [[2, 5, 6], [3, 10, 14]],
    [[2, 8, 12], [0, 10, 17]],
    [[0, 7, 9], [2, 4, 14]],
    [[3, 9, 13], [2, 6, 16]],
    [[1, 14, 16], [4, 11, 15]],
    [[1, 3, 16], [8, 9, 17]],
    [[1, 4, 15], [0, 11, 17]],
    [[3, 10, 17], [1, 5, 9]],
    [[2, 14, 17], [1, 6, 13]],
    [[5, 9, 15], [0, 11, 16]],
    [[11, 12, 15], [0, 6, 13]],
    [[4, 8, 16], [1, 3, 7]],
    [[8, 13, 15], [5, 6, 16]],
    [[2, 8, 17], [3, 13, 16]],
    [[4, 10, 16], [1, 5, 13]],
    [[3, 7, 11], [10, 12, 16]],
    [[9, 13, 16], [7, 10, 12]],
    [[3, 9, 12], [10, 13, 15]],
    [[0, 7, 13], [3, 10, 15]],
    [[6, 11, 14], [7, 13, 17]],
    [[0, 3, 8], [2, 11, 13]],
    [[1, 10, 15], [2, 3, 11]],
    [[0, 4, 6], [9, 14, 15]],
    [[5, 8, 12], [6, 13, 17]],
    [[2, 5, 13], [1, 6, 14]],
    [[1, 4, 7], [0, 5, 9]],
    [[1, 6, 11], [4, 9, 16]],
    [[2, 7, 10], [3, 8, 15]],
    [[5, 11, 14], [1, 8, 10]],
]


def _repair_fillers(filler_pairs):
    """AB 卡不共用同一填充片段（两卡出现一模一样的条目会显得出题马虎）：与一张两侧
    都没有该片段的题对换。填充片段不进计分，这条只关乎观感。"""
    for _ in range(200):
        pi = next((i for i, (x, y) in enumerate(filler_pairs) if x == y), None)
        if pi is None:
            return
        a, b = filler_pairs[pi]
        for pj, (a2, b2) in enumerate(filler_pairs):
            if pj == pi or a in (a2, b2):
                continue
            donor, slot = (a2, 0) if a2 != b else (b2, 1)
            filler_pairs[pi][0] = donor
            filler_pairs[pj][slot] = a
            break
        else:
            return


def _build_trials(seed):
    """seed 决定表层（题序/变体/填充/卡内条目序），配对骨架 _DESIGNED_TRIALS 不变——
    所以任何种子下按 key 计算的设计不变量（平衡/洁净/锚点）都与默认卷完全一致。"""
    rng = _random.Random(seed)
    texts_of = {f['key']: [f['phrase'], *f['variants']] for f in FEATURES}
    # 每条特征一个轮换牌组：洗牌后逐次取用、用尽重洗。3 条变体在整卷出现次数天然均衡（±1），
    # 同一文本的两次出现被强制隔开至少一轮（≈3 次该特征的出现）——比独立随机抽样更抗扎堆。
    decks = {key: [] for key in texts_of}

    def _text_for(key):
        if not decks[key]:
            decks[key] = list(texts_of[key])
            rng.shuffle(decks[key])
        return decks[key].pop()

    # 填充同理轮换：88 槽位对 16 条片段，每条约 5–6 次且不扎堆、AB 不重样。
    filler_tokens = []
    while len(filler_tokens) < N_PAIRS * 2:
        batch = list(FILLERS)
        rng.shuffle(batch)
        filler_tokens.extend(batch)
    filler_pairs = [[filler_tokens[i * 2], filler_tokens[i * 2 + 1]] for i in range(N_PAIRS)]
    _repair_fillers(filler_pairs)
    key_of = [f['key'] for f in FEATURES]
    trials = []
    for index, ((a, b), (fa, fb)) in enumerate(zip(_DESIGNED_TRIALS, filler_pairs)):
        card_a = [_text_for(key_of[k]) for k in a] + [fa]
        card_b = [_text_for(key_of[k]) for k in b] + [fb]
        rng.shuffle(card_a)
        rng.shuffle(card_b)
        trials.append(dict(id=f'T{index + 1:02d}', a=card_a, b=card_b))
    rng.shuffle(trials)
    # 锚点按「洗牌后的序列位置」复制，来源 id 必须记实际 id（id 是配对序号，与位置无关）。
    anchor_of = {f'AC{i + 1}': trials[src]['id'] for i, src in enumerate(ANCHOR_SOURCES)}
    def _copy(i):
        return dict(id=f'AC{i + 1}', a=list(trials[ANCHOR_SOURCES[i]]['a']), b=list(trials[ANCHOR_SOURCES[i]]['b']))
    sequence = (trials[:32] + [ATTENTION_TRIAL] + trials[32:36]
                + [_copy(0)] + trials[36:40]
                + [_copy(1)] + trials[40:42]
                + [_copy(2)] + trials[42:])
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


# ── 计分。RT_FLOOR 以下的作答视为没读题；锚点/注意力结果进质检不进结论。 ──

RT_FLOOR = 800
MIN_FEATURE_N = 10          # 特征出现卡数低于此 → 该条低置信
# 判定阈值作用于回归权重（共现校正后的净效应），不是原始选中率差——原始差会被
# 「这张卡的其他片段恰好更强」拉偏，权重把共现结构显式建模掉了。阈值由噪声人格
# 仿真重校准（2026-11-23 扩池后每特征 16 观测）：中性特征 |w| 的 p90≈1.15、p99≈1.82，
# 阈值取 p92/p99——误报约 8%/特征（旧 22 观测设计为 5%），换构念丰富度的精度税。
WEIGHT_STRONG, WEIGHT_LIKE = 1.8, 1.2
WEIGHT_ITERS, WEIGHT_RIDGE = 1500, 0.25

PHRASE_TO_FEATURE = {text: f for f in FEATURES for text in (f['phrase'], *f['variants'])}
FEATURE_BY_KEY = {f['key']: f for f in FEATURES}
DIMENSION_BY_KEY = {d['key']: d for d in DIMENSIONS}

VERDICT_LABELS = {
    'strong_like': '明显偏好', 'like': '倾向偏好', 'neutral': '无明显倾向',
    'avoid': '倾向回避', 'strong_avoid': '明显回避', 'uncertain': '样本不足',
}


def _sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _verdict(weight, low_confidence):
    """判定看回归权重（净效应方向与强度），幅度阈值经仿真校准。"""
    if low_confidence:
        return 'uncertain'
    if weight >= WEIGHT_STRONG:
        return 'strong_like'
    if weight >= WEIGHT_LIKE:
        return 'like'
    if weight > -WEIGHT_LIKE:
        return 'neutral'
    if weight > -WEIGHT_STRONG:
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
    """responses: [{trial_id, choice, rt_ms}]（全量、有序）；seed 必须与取题时一致（决定锚点
    配对来源，特征计数与键结构无关种子）。返回报告 dict；非法输入抛 ValueError。"""
    all_trials = trials(seed)
    trial_map = {t['id']: t for t in all_trials}
    if len(responses) != len(all_trials):
        raise ValueError(f'作答数量不符：应答 {len(all_trials)} 题，收到 {len(responses)} 题')
    seen = set()
    valid, excluded = [], []
    for row in responses:
        trial_id = row.get('trial_id') or ''
        if trial_id not in trial_map or trial_id in seen:
            raise ValueError(f'作答含未知或重复的题目编号：{trial_id}')
        seen.add(trial_id)
        choice = row.get('choice')
        if choice not in ('a', 'b'):
            raise ValueError(f'作答选项非法：{choice}')
        rt = int(row.get('rt_ms') or 0)
        target = valid if rt >= RT_FLOOR else excluded
        target.append(dict(trial=trial_map[trial_id], chosen=choice, trial_id=trial_id))

    valid_n = len(valid)
    flags = []
    attention_pass = next((r['chosen'] == 'a' for r in valid if r['trial_id'] == 'AT1'), None)
    if attention_pass is None:
        attention_pass = next((r['chosen'] == 'a' for r in excluded if r['trial_id'] == 'AT1'), False)
        flags.append('注意力校验题被判定为过快作答')
    if not attention_pass:
        flags.append('注意力校验未通过，结果可信度低')

    anchor_results = []
    anchor_map = anchor_of(seed)
    for r in valid + excluded:
        source = anchor_map.get(r['trial_id'])
        if not source:
            continue
        original = next((row['chosen'] for row in valid + excluded if row['trial_id'] == source), None)
        if original is not None:
            anchor_results.append(r['chosen'] == original)
    anchor_agreement = sum(anchor_results)
    if len(anchor_results) == len(ANCHOR_SOURCES) and anchor_agreement <= 1:
        flags.append('重复题前后不一致，建议重测')

    rt_excluded_ratio = len(excluded) / len(all_trials) if all_trials else 0.0
    if rt_excluded_ratio > 0.3:
        flags.append(f'过快作答过多（{len(excluded)} 题），建议重测')
    if valid_n < 30:
        flags.append(f'有效样本不足（{valid_n} 题），建议重测')

    # 特征计数：每道有效题贡献「选中卡含 f」与「未选中卡含 f」；权重用差向量回归做共现校正。
    # 卡上文本可能是任一变体——映射按 key 池化，与种子/变体选择无关。
    wins = {f['key']: 0 for f in FEATURES}
    present = {f['key']: 0 for f in FEATURES}
    feature_keys = {text: f['key'] for text, f in PHRASE_TO_FEATURE.items()}
    diff_rows = []
    for r in valid:
        chosen_card = r['trial'][r['chosen']]
        other_card = r['trial']['b' if r['chosen'] == 'a' else 'a']
        for phrase in chosen_card:
            key = feature_keys.get(phrase)
            if key:
                wins[key] += 1
        for phrase in chosen_card + other_card:
            key = feature_keys.get(phrase)
            if key:
                present[key] += 1
        diff_rows.append(([feature_keys[p] for p in chosen_card if p in feature_keys],
                          [feature_keys[p] for p in other_card if p in feature_keys]))
    weight_map = dict(zip([f['key'] for f in FEATURES], _fit_diff_weights(diff_rows)))

    total_cards = 2 * valid_n
    features_report = []
    for f in FEATURES:
        key = f['key']
        n_present = present[key]
        n_absent = total_cards - n_present
        low_confidence = n_present < MIN_FEATURE_N or valid_n < 30
        rate_present = wins[key] / n_present if n_present else 0.0
        rate_absent = (valid_n - wins[key]) / n_absent if n_absent else 0.0
        amce = rate_present - rate_absent
        se = 0.0
        if n_present and n_absent:
            se = math.sqrt(rate_present * (1 - rate_present) / n_present
                           + rate_absent * (1 - rate_absent) / n_absent)
        features_report.append(dict(
            key=key, dim=f['dim'], dim_label=DIMENSION_BY_KEY[f['dim']]['label'],
            phrase=f['phrase'], screen=f['screen'],
            n_present=n_present, rate_present=round(rate_present, 4),
            rate_absent=round(rate_absent, 4), amce=round(amce, 4), se=round(se, 4),
            weight=round(weight_map.get(key, 0.0), 4),
            verdict=_verdict(weight_map.get(key, 0.0), low_confidence), low_confidence=low_confidence,
        ))
    # 展示与结论排序都按权重（净效应）：显示里保留原始选中率差（amce）作参考。
    features_report.sort(key=lambda item: -item['weight'])

    dimensions_report = []
    for d in DIMENSIONS:
        members = [item for item in features_report if item['dim'] == d['key']]
        mean_w = sum(item['weight'] for item in members) / len(members) if members else 0.0
        mean_amce = sum(item['amce'] for item in members) / len(members) if members else 0.0
        dimensions_report.append(dict(key=d['key'], label=d['label'], onet=d['onet'],
                                      weight=round(mean_w, 4), amce=round(mean_amce, 4),
                                      verdict=_verdict(mean_w, any(item['low_confidence'] for item in members))))

    prefer = [item['key'] for item in features_report if item['verdict'] in ('strong_like', 'like')][:4]
    avoid = [item['key'] for item in reversed(features_report) if item['verdict'] in ('avoid', 'strong_avoid')][:4]
    grade = 'weak' if flags else 'ok'
    report = dict(
        quality=dict(attention_pass=attention_pass, anchor_agreement=anchor_agreement,
                     anchors=len(ANCHOR_SOURCES), rt_excluded=len(excluded),
                     valid_trials=valid_n, flags=flags, grade=grade),
        features=features_report,
        dimensions=dimensions_report,
        actions=dict(prefer=prefer, avoid=avoid,
                     prefer_line=_line('prefer', prefer), avoid_line=_line('avoid', avoid)),
        note='结果只反映你在这组片段上的相对倾向，不是岗位市场判断；二选一信息量有限，'
             '差异小的条目仅供参考。质检全过（一致性 3/3、注意力通过、无过快作答）时结果最可信。',
    )
    return report


def _fit_diff_weights(diff_rows):
    """差向量回归：选中侧特征 +w，未选中侧 −w，最大化 Σ log σ(w·d)。纯 Python 梯度下降。"""
    k = len(FEATURES)
    index_of = {f['key']: i for i, f in enumerate(FEATURES)}
    prepared = [([index_of[key] for key in pos], [index_of[key] for key in neg]) for pos, neg in diff_rows]
    w = [0.0] * k
    n = max(1, len(prepared))
    for it in range(WEIGHT_ITERS):
        step = 0.4 / (1.0 + it * 0.002)
        grad = [WEIGHT_RIDGE * w[i] / n for i in range(k)]
        for pos, neg in prepared:
            z = sum(w[i] for i in pos) - sum(w[i] for i in neg)
            g = _sigmoid(z) - 1.0
            for i in pos:
                grad[i] += g / n
            for i in neg:
                grad[i] -= g / n
        w = [w[i] - step * grad[i] for i in range(k)]
    return w
