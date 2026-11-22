"""反向JD测评（2026-11-22）：真实JD片段二选一 → 隐性筛选条件报告。纯规则，无 LLM、确定性、可测。

形态对标强制选择（forced-choice）实验范式：每题两张由 JD 原文式片段拼成的岗位卡，
强制选一张更愿意投的。维度词表取 O*NET Work Values 六维（Achievement / Independence /
Recognition / Relationships / Support / Working Conditions）的中文转译，每维 2 条特征片段；
设计目标是把「MBTI 式的巴纳姆标签」换成「具体到片段的反应率与筛选建议」：
报告输出「含「独立负责」的片段你选中率 82%，含「频繁跨部门」的仅 23%」，而非人格标签。

配对设计（质量三闸门，tests/test_jd_assessment.py 逐项断言）：
1. 平衡：每条特征出现次数恒定、左右卡位各半——反应率差异（AMCE）不被位置/频次混淆；
2. 洁净：同卡同维最多一条特征、AB 卡不共享特征或填充片段——避免同题内互相污染；
3. 质检：3 道前后重复锚点题（测一致性）+ 1 道显性占优题（测注意力）+ 反应时下限。

计分：选中率差（AMCE = 含该片段的卡被选比例 − 不含的被选比例）为主结论，
条件逻辑斯蒂回归（岭正则，纯 Python）权重仅作共现校正的排序参考。
"""
import math
import random as _random

# ── 词表：6 维 × 2 特征。phrase 尽量用 JD 原文式中性陈述（可观察事实），screen 是
# 用户回头筛岗位时的关键词形态。片段本身不带褒贬引导——报告反映的是用户自己的反应。 ──

DIMENSIONS = [
    dict(key='achievement', label='成就攻坚', onet='Achievement'),
    dict(key='independence', label='独立自主', onet='Independence'),
    dict(key='recognition', label='认可回报', onet='Recognition'),
    dict(key='relationships', label='关系协作', onet='Relationships'),
    dict(key='support', label='组织支撑', onet='Support'),
    dict(key='conditions', label='工作条件', onet='Working Conditions'),
]

FEATURES = [
    dict(key='a1', dim='achievement', phrase='从0到1搭建新模块，对交付结果负责', screen='从0到1'),
    dict(key='a2', dim='achievement', phrase='背量化业绩指标，按季度结果考核', screen='量化指标'),
    dict(key='i1', dim='independence', phrase='独立负责一条业务线，自主安排优先级', screen='独立负责'),
    dict(key='i2', dim='independence', phrase='小团队作战，单线汇报，决策链短', screen='单线汇报'),
    dict(key='r1', dim='recognition', phrase='薪资高于同类岗位，调薪机制明确', screen='薪资高于市场'),
    dict(key='r2', dim='recognition', phrase='晋升通道清晰，职级体系透明', screen='晋升通道清晰'),
    dict(key='s1', dim='relationships', phrase='团队氛围轻松，同事靠谱互相补位', screen='团队氛围好'),
    dict(key='s2', dim='relationships', phrase='需要频繁跨部门拉通对齐', screen='频繁跨部门'),
    dict(key='p1', dim='support', phrase='有导师带教和体系化的培训机制', screen='导师带教'),
    dict(key='p2', dim='support', phrase='工程流程规范，评审与文档机制齐全', screen='流程规范'),
    dict(key='w1', dim='conditions', phrase='双休，弹性打卡，支持混合远程', screen='双休弹性'),
    dict(key='w2', dim='conditions', phrase='版本期常态加班，冲刺节奏', screen='常态加班'),
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
]

# 注意力校验题：A 卡在常识上严格占优（休息/透明/流程），选 B 视为未认真作答。
# 片段全部专用，不进特征统计池，避免污染 AMCE。
ATTENTION_TRIAL = dict(
    id='AT1',
    a=['双休不加班，弹性打卡', '尊重生活边界，不鼓励无效加班', '按法定节假日放假', '需求稳定，不频繁变更'],
    b=['大小周，工作日晚间常态加班', '调薪机制不透明', '流程混乱，需求频繁变更', '通勤远且需常驻现场'],
)

FEATURES_PER_CARD = 3          # 每卡 3 条特征 + 1 条填充片段 = 4 条
N_PAIRS = 44                   # 44 组唯一配对 → 每特征 11 左 + 11 右 = 22 次
DESIGN_SEED = 20261122

# 48 题序列：44 唯一 + 1 注意力 + 3 锚点。锚点是唯一配对的精确重复（间隔 ≥25 题），
# 用来测前后一致性——孤立的选择是噪声，重复题上的稳定选择才是信号。
ANCHOR_SOURCES = (4, 13, 22)   # 唯一序列中的下标（即第 5 / 14 / 23 题）


def _edge(i, j):
    return (i, j) if i < j else (j, i)


# 成对平衡设计（2026-11-22 固化）：44 组试次，每组 [A 卡特征下标, B 卡特征下标]。
# 由「4K12 三角形分解 + 跨卡配对退火 + 左右取向退火」一次性生成后写入（生成脚本见
# tests/test_jd_assessment.py 的设计注释）。已验证的不变量：
#   1. 同卡：任两特征同卡恰 4 次（BIBD 精确，66/66 对全部命中）；
#   2. 跨卡：任两特征分处 AB 卡 5–7 次（目标 6，局部搜索最优残差 ≤±1）；
#   3. 位置：任一特征出现在左卡恰 11 次（精确）。
# 这三条保证：反应率差异（AMCE）不受共现运气与左右位置污染——这是「题目经得起推敲」
# 的统计底座。改动 FEATURES 数量或顺序会使下标失效，必须重新生成此常量。
_DESIGNED_TRIALS = [
    [[2, 6, 11], [4, 5, 8]], [[2, 4, 7], [0, 6, 8]], [[3, 4, 8], [7, 10, 11]],
    [[0, 1, 3], [4, 9, 10]], [[0, 1, 7], [4, 5, 6]], [[4, 6, 11], [1, 8, 10]],
    [[2, 4, 5], [6, 7, 9]], [[0, 5, 11], [3, 9, 10]], [[5, 6, 8], [0, 3, 4]],
    [[1, 6, 10], [3, 5, 7]], [[3, 6, 10], [4, 5, 9]], [[4, 8, 9], [1, 2, 3]],
    [[1, 5, 9], [3, 4, 8]], [[2, 6, 10], [0, 1, 7]], [[3, 10, 11], [1, 2, 8]],
    [[0, 2, 5], [1, 4, 11]], [[0, 4, 10], [2, 3, 6]], [[1, 3, 9], [0, 7, 11]],
    [[2, 4, 10], [6, 9, 11]], [[3, 7, 10], [0, 6, 8]], [[7, 8, 9], [3, 4, 6]],
    [[2, 3, 8], [1, 5, 10]], [[5, 7, 10], [1, 2, 4]], [[3, 5, 7], [2, 10, 11]],
    [[1, 9, 11], [5, 6, 8]], [[2, 7, 8], [5, 9, 10]], [[1, 6, 7], [2, 9, 11]],
    [[0, 4, 7], [1, 5, 11]], [[3, 5, 11], [1, 4, 6]], [[0, 4, 11], [2, 3, 7]],
    [[0, 1, 5], [7, 8, 9]], [[8, 9, 11], [1, 6, 7]], [[0, 6, 9], [2, 5, 10]],
    [[2, 6, 9], [0, 3, 11]], [[5, 6, 11], [0, 8, 10]], [[6, 7, 10], [1, 3, 9]],
    [[1, 4, 10], [0, 2, 9]], [[0, 8, 10], [4, 7, 11]], [[4, 7, 9], [8, 10, 11]],
    [[0, 2, 9], [5, 7, 8]], [[3, 8, 11], [0, 2, 5]], [[3, 5, 9], [2, 7, 11]],
    [[1, 2, 8], [0, 3, 6]], [[1, 8, 11], [0, 9, 10]],
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


def _build_trials():
    rng = _random.Random(DESIGN_SEED)
    key_of = [f['key'] for f in FEATURES]
    filler_tokens = [f for f in FILLERS for _ in range(N_PAIRS * 2 // len(FILLERS))]
    rng.shuffle(filler_tokens)
    filler_pairs = [[filler_tokens[i * 2], filler_tokens[i * 2 + 1]] for i in range(N_PAIRS)]
    _repair_fillers(filler_pairs)
    phrase_of = {f['key']: f['phrase'] for f in FEATURES}
    trials = []
    for index, ((a, b), (fa, fb)) in enumerate(zip(_DESIGNED_TRIALS, filler_pairs)):
        card_a = [phrase_of[key_of[k]] for k in a] + [fa]
        card_b = [phrase_of[key_of[k]] for k in b] + [fb]
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


_TRIALS = None
_ANCHOR_OF = None


def trial_list():
    """题序列只随代码版本变化（固定种子），进程内缓存；前端按此渲染与回传 trial_id。"""
    global _TRIALS, _ANCHOR_OF
    if _TRIALS is None:
        _TRIALS, _ANCHOR_OF = _build_trials()
    return {'trials': _TRIALS}


def trials():
    if _TRIALS is None:
        trial_list()
    return _TRIALS


def anchor_of():
    if _ANCHOR_OF is None:
        trial_list()
    return _ANCHOR_OF


# ── 计分。RT_FLOOR 以下的作答视为没读题；锚点/注意力结果进质检不进结论。 ──

RT_FLOOR = 800
MIN_FEATURE_N = 10          # 特征出现卡数低于此 → 该条低置信
# 判定阈值作用于回归权重（共现校正后的净效应），不是原始选中率差——原始差会被
# 「这张卡的其他片段恰好更强」拉偏，权重把共现结构显式建模掉了。阈值由噪声人格
# 仿真校准（tests/test_jd_assessment.py）：中性特征 |w| 的 95 分位 ≈ 0.9。
WEIGHT_STRONG, WEIGHT_LIKE = 1.6, 0.9
WEIGHT_ITERS, WEIGHT_RIDGE = 1500, 0.25

PHRASE_TO_FEATURE = {f['phrase']: f for f in FEATURES}
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


def score_responses(responses):
    """responses: [{trial_id, choice, rt_ms}]（全量、有序）。返回报告 dict；非法输入抛 ValueError。"""
    all_trials = trials()
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
    anchor_map = anchor_of()
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
    wins = {f['key']: 0 for f in FEATURES}
    present = {f['key']: 0 for f in FEATURES}
    feature_keys = {f['phrase']: f['key'] for f in FEATURES}
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
