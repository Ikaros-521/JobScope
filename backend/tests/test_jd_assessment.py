"""反向JD测评（best-worst 选题）：区组设计不变量、题序间隔、计分恢复仿真、质检闸门与 API 提交。

设计质量是本功能的命门（用户要求「题目经得起推敲」），所以设计不变量逐条断言：
每构念恰 8 次出场、任意两条构念同组 1–2 次（零共现对为 0）、同题同维最多 2 条、无孪生题
（没有两道题共享 ≥3 条）；题序经间隔感知排序，相邻题不共享构念、同一句原文不挨着。
不变量按 key 计算——卡上文本是会话种子选出的变体，键结构种子无关。
计分准确性用仿真验证（标准 MaxDiff 作答模型）：有效强度 3.0 的明确偏好人格，偏好/回避判定
必须全部回收、中性零强误报；有效强度 1.5 的中等偏好按校准灵敏度回收（~55%）；
纯中性人格（毫无偏好、每题随机点两条）的误报率受阈值约束——构念级 倾向 ~7.7% / 强级 ~0.4%，
由 output/calibrate_bws_rules.py 校准（旧版二选一同口径实测 13.6% / 3.0%）。
"""
import math
import random as _random
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import jd_assessment as J
from jobscope.main import create_app
from jobscope.repository import Repository

PLANTED_LIKES = {'a1', 'i1', 'i3'}
PLANTED_HATES = {'i2', 's2'}
STRONG, MODERATE = 3.0, 1.5       # 植入强度（logit 尺度）：单独对比时约 95% / 82% 会选它
FLAGGED = {'like', 'strong_like', 'avoid', 'strong_avoid'}
STRONG_LEVEL = {'strong_like', 'strong_avoid'}


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def _keys(trial):
    return [J.PHRASE_TO_FEATURE[p]['key'] for p in trial['items'] if p in J.PHRASE_TO_FEATURE]


def _unique(trials):
    return [t for t in trials if t['id'].startswith('T')]


def _utility(text, scale, likes, hates):
    key = J.PHRASE_TO_FEATURE[text]['key']
    return scale if key in likes else -scale if key in hates else 0.0


def _planted_choice(t, tag='main', scale=STRONG, likes=PLANTED_LIKES, hates=PLANTED_HATES):
    """按内容播种的植入人格，标准 MaxDiff 作答模型：最想投 ∝ e^u，其余三条里最不想投 ∝ e^-u。
    随机流由题目文本决定 → 锚点副本（同内容）前后选择一致、结果可复现；AT1 注意力题认真作答。
    scale=0 即纯中性人格（每题随机点两条）。"""
    rng = _random.Random(f'{tag}:{tuple(sorted(t["items"]))}')
    items = t['items']
    if t['id'] == 'AT1':
        return dict(trial_id=t['id'], best=items.index(J.ATTENTION_TRIAL['items'][J.ATTENTION_BEST]),
                    worst=items.index(J.ATTENTION_TRIAL['items'][J.ATTENTION_WORST]), rt_ms=1800)
    u = [_utility(p, scale, likes, hates) for p in items]
    best = rng.choices(range(4), weights=[math.exp(v - max(u)) for v in u])[0]
    rest = [i for i in range(4) if i != best]
    neg = [-u[i] for i in rest]
    worst = rest[rng.choices(range(3), weights=[math.exp(v - max(neg)) for v in neg])[0]]
    return dict(trial_id=t['id'], best=best, worst=worst, rt_ms=1800)


def _null_choice(t, tag):
    return _planted_choice(t, tag=tag, scale=0.0, likes=set(), hates=set())


# ── 设计不变量：这些是「题目质量」的统计底座，逐条断言。 ──

def test_design_shape_and_cleanliness():
    trials = J.trials()
    assert len(trials) == J.N_BLOCKS + len(J.ANCHOR_SOURCES) + 1 == 40
    assert len({t['id'] for t in trials}) == 40
    assert len(_unique(trials)) == J.N_BLOCKS
    attention_items = set(J.ATTENTION_TRIAL['items'])
    dim_of = {f['key']: f['dim'] for f in J.FEATURES}
    for t in trials:
        assert len(t['items']) == J.BLOCK_SIZE == 4 and len(set(t['items'])) == 4
        if t['id'] == 'AT1':
            assert set(t['items']) == attention_items
            continue
        keys = _keys(t)
        assert len(keys) == J.BLOCK_SIZE                        # 正式题 / 锚点每条都是构念片段
        assert len(set(keys)) == J.BLOCK_SIZE                   # 同题不重复构念
        assert max(Counter(dim_of[k] for k in keys).values()) <= 2   # 同题同维最多 2 条
    # 注意力题片段专用：不进特征统计池，与变体也不撞文
    assert not attention_items & set(J.PHRASE_TO_FEATURE)
    # 锚点是来源题的精确复制（条目顺序也一致），排在来源之后至少 10 题
    position = {t['id']: (i, t) for i, t in enumerate(trials)}
    for anchor_id, source_id in J.anchor_of().items():
        assert position[anchor_id][1]['items'] == position[source_id][1]['items']
        assert position[anchor_id][0] - position[source_id][0] >= 10


def test_design_block_balance():
    """区组平衡：每构念恰 8 次出场；任意两条构念同组 1–2 次（零共现对为 0）；无孪生题。"""
    blocks = [set(b) for b in J._DESIGNED_BLOCKS]
    assert len(blocks) == J.N_BLOCKS and all(len(b) == J.BLOCK_SIZE for b in blocks)
    total = Counter(x for b in blocks for x in b)
    assert set(total.values()) == {8}, f'出场次数必须精确为 8：{sorted(set(total.values()))}'
    same = Counter(p for b in blocks for p in combinations(sorted(b), 2))
    all_pairs = list(combinations(range(len(J.FEATURES)), 2))
    # 216 个配对槽 / 153 个构念对 = 90 对×1 + 63 对×2：没有从不同组的，也没有同组 3 次的
    assert Counter(same.values()) == {1: 90, 2: 63}, f'同组共现分布异常：{Counter(same.values())}'
    assert sum(1 for p in all_pairs if same[p] == 0) == 0
    dim_of = [f['dim'] for f in J.FEATURES]
    for x, y in all_pairs:
        if dim_of[x] == dim_of[y]:
            assert same[(x, y)] >= 1                        # 18 个同维对各同组 1–2 次（维内对照）
    for i, j in combinations(range(len(blocks)), 2):
        assert len(blocks[i] & blocks[j]) <= 2              # 无孪生题：没有两道题共享 ≥3 条


def test_order_spacing():
    """题序经间隔感知排序：相邻题基本不共享构念、同一句原文从不挨着；同一句原文整卷不刷屏。"""
    feature_texts = set(J.PHRASE_TO_FEATURE)
    positions = Counter()          # T01 里构念 a1 落在块内第几位（随种子变化 → 位置习惯不偏袒任何构念）
    probe = J.FEATURES[0]['key']
    for seed in (None, 1, 7, 42, 20261124, 999999999):
        trials = J.trials(seed)
        seq = [t for t in trials if not t['id'].startswith('AC')]        # 锚点本就是原样重现，不计
        keys = [{J.PHRASE_TO_FEATURE[p]['key'] for p in t['items'] if p in J.PHRASE_TO_FEATURE} for t in seq]
        texts = [set(t['items']) & feature_texts for t in seq]
        assert sum(len(a & b) for a, b in zip(keys, keys[1:])) <= 2
        assert not any(a & b for a, b in zip(texts, texts[1:]))         # 相邻题没有同一句原文
        exposure = Counter(p for t in trials for p in t['items'] if p in feature_texts)
        # 8 次出场 / 3 条变体 → 每句 2–3 次，最多再被 3 道锚点各复制一次；54 条变体全部上场
        assert all(v <= 6 for v in exposure.values()) and all(v >= 2 for v in exposure.values())
        assert len(exposure) == len(J.FEATURES) * 3
        first = next(t for t in trials if t['id'] == 'T01')
        positions[next(i for i, p in enumerate(first['items']) if J.PHRASE_TO_FEATURE[p]['key'] == probe)] += 1
    assert len(positions) >= 2


def test_seed_keeps_key_structure_invariants():
    """多组种子：形状 / 出场次数 / 锚点复制全部与默认卷一致（骨架种子无关）。"""
    for seed in (1, 42, 20261124, 999999999):
        trials = J.trials(seed)
        assert len(trials) == 40 and len({t['id'] for t in trials}) == 40
        by_id = {t['id']: t for t in trials}
        total = Counter(k for t in _unique(trials) for k in _keys(t))
        assert set(total.values()) == {8}
        assert all(len(t['items']) == 4 for t in trials)
        for anchor_id, source_id in J.anchor_of(seed).items():
            assert by_id[anchor_id]['items'] == by_id[source_id]['items']


def test_seed_changes_surface_but_not_keys():
    """不同种子 → 题序 / 块内顺序 / 变体确实不同；同 trial_id 的键集合跨种子完全一致。"""
    t_default, t_other = J.trials(), J.trials(424242)
    assert [t['id'] for t in t_default] != [t['id'] for t in t_other]          # 题序换了
    by_id_default = sorted(_unique(t_default), key=lambda t: t['id'])
    by_id_other = sorted(_unique(t_other), key=lambda t: t['id'])
    for td, to in zip(by_id_default, by_id_other):
        assert td['id'] == to['id']
        assert sorted(_keys(td)) == sorted(_keys(to))                          # 键集合不变
    assert [t['items'] for t in by_id_default] != [t['items'] for t in by_id_other]   # 块内顺序 / 变体换了
    attention = lambda ts: next(t for t in ts if t['id'] == 'AT1')['items']   # noqa: E731
    assert attention(t_default) != attention(t_other)                         # 注意力题顺序也被种子打乱


# ── 计分恢复仿真：植入人格的偏好/回避判定必须回收。 ──

def test_score_recovers_planted_preferences():
    """明确偏好人格：3 偏好 + 2 回避全部回收、中性零强误报；计分口径逐条核对。"""
    report = J.score_responses([_planted_choice(t) for t in J.trials()])
    quality = report['quality']
    assert quality['grade'] == 'ok' and quality['flags'] == []
    assert quality['anchor_agreement'] == 3 and quality['anchors'] == 3
    assert quality['attention_pass'] is True
    assert quality['valid_trials'] == 36 and quality['total_trials'] == 40 and quality['rt_excluded'] == 0
    verdicts = {f['key']: f['verdict'] for f in report['features']}
    for key in PLANTED_LIKES:
        assert verdicts[key] in ('strong_like', 'like'), f'{key} 应判偏好，实际 {verdicts[key]}'
    for key in PLANTED_HATES:
        assert verdicts[key] in ('strong_avoid', 'avoid'), f'{key} 应判回避，实际 {verdicts[key]}'
    planted = PLANTED_LIKES | PLANTED_HATES
    assert not [f['key'] for f in report['features'] if f['key'] not in planted and f['verdict'] in STRONG_LEVEL]
    for f in report['features']:
        # 只有正式题进计分：每条恰 8 次有效出场；计分 =（最想投 − 最不想投）/ 出场数
        assert f['n_present'] == 8 and not f['low_confidence']
        assert f['best'] + f['worst'] <= 8
        assert abs(f['score'] - (f['best'] - f['worst']) / 8) < 1e-3
        assert abs(f['best_rate'] - f['best'] / 8) < 1e-3 and abs(f['worst_rate'] - f['worst'] / 8) < 1e-3
        assert 0 <= f['se'] <= 1            # 8 次全被选为最想（或全没被选）时标准差为 0
    assert [f['key'] for f in report['features']] == sorted((f['key'] for f in report['features']),
                                                             key=lambda k: -verdict_score(report, k))
    prefer, avoid = report['actions']['prefer'], report['actions']['avoid']
    assert set(PLANTED_LIKES) <= set(prefer) and len(prefer) <= 4
    assert set(PLANTED_HATES) <= set(avoid) and len(avoid) <= 4
    # 倾向级允许少量中性误报（13 条中性 × ~6% ≈ 期望 0.8 个），强级已零容忍
    assert len([k for k in prefer + avoid if k not in planted]) <= 3
    assert len(report['dimensions']) == 6 and all(abs(d['score']) <= 1 for d in report['dimensions'])


def verdict_score(report, key):
    return next(f['score'] for f in report['features'] if f['key'] == key)


def test_score_detects_moderate_preferences():
    """中等偏好（单独对比时约 82% 会选它）按校准灵敏度回收：20 个人格 × 5 条植入，回收率 ≥ 40%
    （校准值约 55%），且明显高于同一批报告里中性构念的被标记率。"""
    trials = J.trials()
    recovered = neutral_flagged = 0
    for i in range(20):
        report = J.score_responses([_planted_choice(t, tag=f'moderate-{i}', scale=MODERATE) for t in trials])
        for f in report['features']:
            if f['key'] in PLANTED_LIKES:
                recovered += f['verdict'] in ('like', 'strong_like')
            elif f['key'] in PLANTED_HATES:
                recovered += f['verdict'] in ('avoid', 'strong_avoid')
            else:
                neutral_flagged += f['verdict'] in FLAGGED
    assert recovered / 100 >= 0.40, f'中等偏好回收率过低：{recovered}%'
    assert recovered / 100 > 4 * (neutral_flagged / 260)


def test_null_persona_false_positives_bounded():
    """纯中性人格（毫无偏好、每题随机点两条）：误报受阈值约束——
    构念级 倾向 ≤ 11%（校准 7.7%）、强级 ≤ 1.5%（校准 0.4%）；维度级强判定几乎为零。"""
    trials = J.trials()
    flagged = strong = dims_strong = 0
    for i in range(200):
        report = J.score_responses([_null_choice(t, i) for t in trials])
        assert report['quality']['grade'] == 'ok'            # 随机作答也能过质检（锚点内容播种前后一致）
        for f in report['features']:
            flagged += f['verdict'] in FLAGGED
            strong += f['verdict'] in STRONG_LEVEL
        dims_strong += sum(d['verdict'] in STRONG_LEVEL for d in report['dimensions'])
    assert flagged / (200 * 18) <= 0.11, f'中性误判倾向过多：{flagged / (200 * 18):.1%}'
    assert strong / (200 * 18) <= 0.015, f'中性误判明显过多：{strong / (200 * 18):.1%}'
    assert dims_strong <= 3


# ── 质检闸门。 ──

def test_quality_gates():
    trials = J.trials()
    # 全部过快：有效样本为 0，三个闸门齐响，结论全部「样本不足」
    fast = [dict(trial_id=t['id'], best=0, worst=1, rt_ms=100) for t in trials]
    report = J.score_responses(fast)
    quality = report['quality']
    assert quality['rt_excluded'] == 40 and quality['valid_trials'] == 0 and quality['attention_pass'] is False
    assert any('过快' in flag for flag in quality['flags']) and any('有效样本不足' in flag for flag in quality['flags'])
    assert all(f['verdict'] == 'uncertain' and f['n_present'] == 0 for f in report['features'])
    assert report['actions']['prefer'] == [] and report['actions']['avoid'] == []
    # 注意力题选反（最想投点了坏片段）
    wrong_attention = []
    for t in trials:
        row = dict(_planted_choice(t), rt_ms=2000)
        if t['id'] == 'AT1':
            row['best'], row['worst'] = row['worst'], row['best']
        wrong_attention.append(row)
    quality = J.score_responses(wrong_attention)['quality']
    assert quality['attention_pass'] is False and any('注意力' in flag for flag in quality['flags'])
    # 锚点不一致：把两道锚点的最想 / 最不想对调（其余照常）→ 触发「重复题前后不一致」；
    # 锚点只进质检不进计分，所以构念出场数仍是 8。
    mismatch = []
    for t in trials:
        row = dict(_planted_choice(t), rt_ms=2000)
        if t['id'] in ('AC1', 'AC3'):
            row['best'], row['worst'] = row['worst'], row['best']
        mismatch.append(row)
    report = J.score_responses(mismatch)
    assert report['quality']['anchor_agreement'] <= 1 and report['quality']['grade'] == 'weak'
    assert any('不一致' in flag for flag in report['quality']['flags'])
    assert all(f['n_present'] == 8 for f in report['features'])
    # 正式题大面积过快：有效样本不足 → 整卷低置信
    slow = []
    fast_seen = 0
    for t in trials:
        row = dict(_planted_choice(t), rt_ms=2000)
        if t['id'].startswith('T') and fast_seen < 15:
            row['rt_ms'] = 100
            fast_seen += 1
        slow.append(row)
    report = J.score_responses(slow)
    assert report['quality']['valid_trials'] == 21 and report['quality']['rt_excluded'] == 15
    assert any('有效样本不足' in flag for flag in report['quality']['flags'])
    assert any('过快作答过多' in flag for flag in report['quality']['flags'])
    assert all(f['verdict'] == 'uncertain' for f in report['features'])


def test_score_rejects_invalid_input():
    trials = J.trials()
    with pytest.raises(ValueError):
        J.score_responses([dict(trial_id='NOPE', best=0, worst=1, rt_ms=1000)])           # 未知题目
    with pytest.raises(ValueError):
        J.score_responses([dict(trial_id=trials[0]['id'], best=0, worst=1, rt_ms=1000)])  # 数量不符
    rows = [_planted_choice(t) for t in trials]
    dup = [dict(r) for r in rows]
    dup[1]['trial_id'] = dup[0]['trial_id']
    with pytest.raises(ValueError):
        J.score_responses(dup)                                                           # 重复题目
    for bad in (dict(best=1, worst=1), dict(best=4, worst=0), dict(best=-1, worst=0),
                dict(best=True, worst=0), dict(best=0.0, worst=1)):
        rows = [_planted_choice(t) for t in trials]
        rows[5].update(bad)
        with pytest.raises(ValueError):
            J.score_responses(rows)                                                      # 同一条 / 越界 / 非整数


# ── API。 ──

def test_api_roundtrip(client):
    trials = client.get('/api/jd-assessment/trials').json()['trials']
    assert len(trials) == 40 and all(len(t['items']) == 4 for t in trials)
    assert all(set(t) == {'id', 'items'} for t in trials)
    saved = client.post('/api/jd-assessment', json={'responses': [_planted_choice(t) for t in trials]})
    assert saved.status_code == 200
    body = saved.json()
    assert body['payload']['report']['quality']['grade'] == 'ok'
    assert set(PLANTED_LIKES) <= set(body['payload']['report']['actions']['prefer'])
    listing = client.get('/api/jd-assessment').json()['assessments']
    assert listing and listing[0]['id'] == body['id']
    assert listing[0]['payload']['report']['features'][0]['n_present'] == 8


def test_api_rejects_bad_payloads(client):
    post = lambda responses: client.post('/api/jd-assessment', json={'responses': responses}).status_code  # noqa: E731
    assert post([dict(trial_id='X', best=0, worst=1)]) == 400                 # 未知题目
    assert post([dict(trial_id='T01', best=4, worst=0)]) == 422               # 下标越界
    assert post([dict(trial_id='T01', choice='a')]) == 422                    # 旧版二选一的作答形态不再接受
    trials = client.get('/api/jd-assessment/trials').json()['trials']
    rows = [_planted_choice(t) for t in trials]
    rows[5].update(best=1, worst=1)
    assert post(rows) == 400                                                 # 最想投与最不想投是同一条


def test_score_with_seed_roundtrip():
    """种子卷作答 + 同种子交卷：质检全过、植入偏好照常回收（计分对种子不敏感）。"""
    seed = 87654321
    report = J.score_responses([_planted_choice(t) for t in J.trials(seed)], seed)
    assert report['quality']['grade'] == 'ok'
    assert report['quality']['anchor_agreement'] == 3
    verdicts = {f['key']: f['verdict'] for f in report['features']}
    assert verdicts['a1'] in ('strong_like', 'like')
    assert verdicts['i2'] in ('strong_avoid', 'avoid')


def test_api_seed_roundtrip(client):
    trials = client.get('/api/jd-assessment/trials', params={'seed': 123456}).json()['trials']
    saved = client.post('/api/jd-assessment', json={'responses': [_planted_choice(t) for t in trials], 'seed': 123456})
    assert saved.status_code == 200
    assert saved.json()['payload']['seed'] == 123456
    assert saved.json()['payload']['report']['quality']['grade'] == 'ok'
    # 种子卷面确实与默认卷不同
    plain = client.get('/api/jd-assessment/trials').json()['trials']
    assert [t['id'] for t in plain] != [t['id'] for t in trials]
