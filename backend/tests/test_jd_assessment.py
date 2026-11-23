"""反向JD测评：成对平衡设计不变量、计分恢复仿真、质检闸门与 API 提交。

设计质量是本功能的命门（用户要求「题目经得起推敲」），所以设计不变量逐条断言：
同卡共现精确平衡（BIBD 4/对）、跨卡 5–7、左右各半（11/特征）、锚点为精确复制。
不变量按 key 计算——2026-11-23 起卡上文本是会话种子选出的变体，键结构种子无关。
计分准确性用植入人格仿真验证：确定性人格的偏好/回避/中性判定必须精确回收，
噪声人格下中性特征不得大面积误判（阈值 0.9/1.6 按此校准）。
"""
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


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def _feature_keys(trial, side):
    keys = {text: f['key'] for text, f in J.PHRASE_TO_FEATURE.items()}
    return [keys[p] for p in trial[side] if p in keys]


# ── 设计不变量：这三条是「题目质量」的统计底座，逐条断言。 ──

def test_design_shape_and_cleanliness():
    trials = J.trials()
    assert len(trials) == 48
    assert len({t['id'] for t in trials}) == 48
    uniq = [t for t in trials if t['id'].startswith('T')]
    assert len(uniq) == J.N_PAIRS
    fillers = set(J.FILLERS)
    for t in uniq:
        assert len(t['a']) == 4 and len(t['b']) == 4
        ka, kb = _feature_keys(t, 'a'), _feature_keys(t, 'b')
        assert len(ka) == 3 and len(kb) == 3          # 每卡 3 特征 + 1 填充
        assert not set(ka) & set(kb)                  # AB 不共享特征（否则无信息量）
        assert not (set(t['a']) & fillers) & (set(t['b']) & fillers)  # 不共享填充片段
        assert len(set(t['a'])) == 4 and len(set(t['b'])) == 4        # 卡内无重复条目
    # 锚点是来源题的精确复制；注意力题存在且片段专用（不进特征池，与变体也不撞文）
    keys = set(J.PHRASE_TO_FEATURE)
    by_id = {t['id']: t for t in trials}
    for anchor_id, source_id in J.anchor_of().items():
        assert by_id[anchor_id]['a'] == by_id[source_id]['a']
        assert by_id[anchor_id]['b'] == by_id[source_id]['b']
    assert all(p not in keys for p in J.ATTENTION_TRIAL['a'] + J.ATTENTION_TRIAL['b'])


def test_design_pairwise_balance():
    """成对平衡：同卡共现=4（BIBD 精确）、跨卡 5–7、左卡=11（精确）。"""
    idx = {f['key']: i for i, f in enumerate(J.FEATURES)}
    uniq = [t for t in J.trials() if t['id'].startswith('T')]
    same, oppo, left = Counter(), Counter(), Counter()
    for t in uniq:
        ka, kb = _feature_keys(t, 'a'), _feature_keys(t, 'b')
        for k in ka:
            left[k] += 1
        for x, y in combinations(ka, 2):
            same[J._edge(idx[x], idx[y])] += 1
        for x, y in combinations(kb, 2):
            same[J._edge(idx[x], idx[y])] += 1
        for x in ka:
            for y in kb:
                oppo[J._edge(idx[x], idx[y])] += 1
    assert set(same.values()) == {4}, f'同卡共现必须精确为 4：{sorted(set(same.values()))}'
    assert max(oppo.values()) <= 7 and min(oppo.values()) >= 5, f'跨卡超出 5–7：{sorted(set(oppo.values()))}'
    assert set(left.values()) == {11}, f'左卡计数必须精确为 11：{sorted(set(left.values()))}'


# ── 计分恢复仿真：植入人格的偏好/回避/中性判定必须回收。 ──

PLANTED_LIKES = {'i1', 'w1', 'p1'}
PLANTED_HATES = {'w2', 's2'}


def _planted_choice(t, rng=None):
    keys = {text: f['key'] for text, f in J.PHRASE_TO_FEATURE.items()}
    ka = [keys[p] for p in t['a'] if p in keys]
    kb = [keys[p] for p in t['b'] if p in keys]
    ua = sum(1 if k in PLANTED_LIKES else -1 if k in PLANTED_HATES else 0 for k in ka)
    ub = sum(1 if k in PLANTED_LIKES else -1 if k in PLANTED_HATES else 0 for k in kb)
    if rng is not None and abs(ua - ub) < 1e-9:
        return dict(trial_id=t['id'], choice=rng.choice(['a', 'b']), rt_ms=1800)
    if rng is not None:
        p = 1.0 / (1.0 + pow(2.718281828, -(ua - ub) * 1.2))
        return dict(trial_id=t['id'], choice='a' if rng.random() < p else 'b', rt_ms=1800)
    return dict(trial_id=t['id'], choice='a' if ua >= ub else 'b', rt_ms=2000)


def test_score_recovers_planted_preferences():
    trials = J.trials()
    report = J.score_responses([_planted_choice(t) for t in trials])
    assert report['quality']['grade'] == 'ok'
    assert report['quality']['anchor_agreement'] == 3
    assert report['quality']['attention_pass'] is True
    verdicts = {f['key']: f['verdict'] for f in report['features']}
    for key in PLANTED_LIKES:
        assert verdicts[key] in ('strong_like', 'like'), f'{key} 应判偏好，实际 {verdicts[key]}'
    for key in PLANTED_HATES:
        assert verdicts[key] in ('strong_avoid', 'avoid'), f'{key} 应判回避，实际 {verdicts[key]}'
    neutrals = [f for f in J.FEATURES if f['key'] not in PLANTED_LIKES | PLANTED_HATES]
    spurious = [f['key'] for f in report['features']
                if f['key'] in {n['key'] for n in neutrals} and f['verdict'].startswith('strong')]
    assert not spurious, f'中性特征不得误判为强偏好/强回避：{spurious}'
    assert set(report['actions']['prefer']) <= PLANTED_LIKES
    assert set(report['actions']['avoid']) <= PLANTED_HATES


def test_score_noisy_persona_stays_honest():
    """噪声人格：植入信号仍以偏好/回避为主 verdict，中性特征误判受阈值约束。"""
    rng = _random.Random(7)
    report = J.score_responses([_planted_choice(t, rng) for t in J.trials()])
    verdicts = {f['key']: f['verdict'] for f in report['features']}
    assert verdicts['w2'] in ('strong_avoid', 'avoid')
    assert sum(1 for k in PLANTED_LIKES if verdicts[k].startswith('strong')) >= 1
    spurious_strong = [f['key'] for f in report['features']
                       if f['key'] not in PLANTED_LIKES | PLANTED_HATES and f['verdict'].startswith('strong')]
    assert len(spurious_strong) <= 1, f'中性特征强判定误报过多：{spurious_strong}'


# ── 质检闸门。 ──

def test_quality_gates():
    trials = J.trials()
    # 全部过快：有效样本为 0，三个闸门齐响
    fast = [dict(trial_id=t['id'], choice='a', rt_ms=100) for t in trials]
    report = J.score_responses(fast)
    assert report['quality']['rt_excluded'] == 48
    assert report['quality']['valid_trials'] == 0
    assert any('过快' in flag for flag in report['quality']['flags'])
    assert any('有效样本不足' in flag for flag in report['quality']['flags'])
    assert all(f['verdict'] == 'uncertain' for f in report['features'])
    # 注意力题选错
    wrong_attention = [dict(trial_id=t['id'], choice='b' if t['id'] == 'AT1' else _planted_choice(t)['choice'],
                             rt_ms=2000) for t in trials]
    report = J.score_responses(wrong_attention)
    assert report['quality']['attention_pass'] is False
    assert any('注意力' in flag for flag in report['quality']['flags'])
    # 锚点不一致：翻两道锚点的选择
    flip = {'a': 'b', 'b': 'a'}
    mismatch = [dict(trial_id=t['id'],
                     choice=flip[_planted_choice(t)['choice']] if t['id'] in ('AC1', 'AC3') else _planted_choice(t)['choice'],
                     rt_ms=2000) for t in trials]
    report = J.score_responses(mismatch)
    assert report['quality']['anchor_agreement'] <= 1
    assert any('不一致' in flag for flag in report['quality']['flags'])


def test_score_rejects_invalid_input():
    with pytest.raises(ValueError):
        J.score_responses([dict(trial_id='NOPE', choice='a', rt_ms=1000)])
    trials = J.trials()
    with pytest.raises(ValueError):
        J.score_responses([dict(trial_id=trials[0]['id'], choice='a', rt_ms=1000)])  # 数量不符
    dup = [dict(trial_id=t['id'], choice='a', rt_ms=1500) for t in trials]
    dup[1]['trial_id'] = dup[0]['trial_id']
    with pytest.raises(ValueError):
        J.score_responses(dup)


# ── API。 ──

def test_api_roundtrip(client):
    trials = client.get('/api/jd-assessment/trials').json()['trials']
    assert len(trials) == 48
    responses = [dict(trial_id=t['id'], choice=_planted_choice(t)['choice'], rt_ms=2000) for t in trials]
    saved = client.post('/api/jd-assessment', json={'responses': responses})
    assert saved.status_code == 200
    body = saved.json()
    assert body['payload']['report']['quality']['grade'] == 'ok'
    assert body['payload']['report']['actions']['prefer']
    listing = client.get('/api/jd-assessment').json()['assessments']
    assert listing and listing[0]['id'] == body['id']
    assert listing[0]['payload']['report']['features']


def test_api_rejects_bad_payloads(client):
    assert client.post('/api/jd-assessment', json={'responses': [{'trial_id': 'X', 'choice': 'a'}]}).status_code == 400
    assert client.post('/api/jd-assessment', json={'responses': [{'trial_id': 'T01', 'choice': 'x'}]}).status_code == 422


# ── 会话种子与表层多样性（2026-11-23）：任何种子下键结构不变量成立、卷面文本确实换皮。 ──

def test_seed_keeps_key_structure_invariants():
    """多组种子：形状/洁净/锚点复制/左右平衡全部与默认卷一致（骨架种子无关）。"""
    idx = {f['key']: i for i, f in enumerate(J.FEATURES)}
    for seed in (1, 42, 20261123, 999999999):
        trials = J.trials(seed)
        assert len(trials) == 48 and len({t['id'] for t in trials}) == 48
        fillers = set(J.FILLERS)
        by_id = {t['id']: t for t in trials}
        uniq = [t for t in trials if t['id'].startswith('T')]
        left = Counter()
        for t in uniq:
            ka, kb = _feature_keys(t, 'a'), _feature_keys(t, 'b')
            assert len(ka) == 3 and len(kb) == 3 and not set(ka) & set(kb)
            assert not (set(t['a']) & fillers) & (set(t['b']) & fillers)
            assert len(set(t['a'])) == 4 and len(set(t['b'])) == 4
            for k in ka:
                left[k] += 1
        assert set(left.values()) == {11}
        for anchor_id, source_id in J.anchor_of(seed).items():
            assert by_id[anchor_id]['a'] == by_id[source_id]['a']
            assert by_id[anchor_id]['b'] == by_id[source_id]['b']


def test_seed_changes_surface_but_not_keys():
    """不同种子 → 题面文本/题序确实不同；同 trial_id 的键集合跨种子完全一致。
    （AC 锚点除外——锚点复制哪道题本就由种子决定，是设计的一部分。）"""
    t_default, t_other = J.trials(), J.trials(424242)
    assert [t['id'] for t in t_default] != [t['id'] for t in t_other]  # 题序换了
    texts_default = [tuple(t['a']) + tuple(t['b']) for t in t_default]
    texts_other = [tuple(t['a']) + tuple(t['b']) for t in t_other]
    assert texts_default != texts_other                                # 文本换了
    uniq_default = sorted((t for t in t_default if t['id'].startswith('T')), key=lambda t: t['id'])
    uniq_other = sorted((t for t in t_other if t['id'].startswith('T')), key=lambda t: t['id'])
    for td, to in zip(uniq_default, uniq_other):
        assert td['id'] == to['id']
        assert sorted(_feature_keys(td, 'a')) == sorted(_feature_keys(to, 'a'))
        assert sorted(_feature_keys(td, 'b')) == sorted(_feature_keys(to, 'b'))


def test_variant_pool_caps_text_exposure():
    """同一文本整卷出现次数被变体轮换压住：特征文本 ≤10、填充 ≤8（锚点复制另计 +1）；
    36 条变体与 16 条填充全部上场（AT1 专用片段不计入）。"""
    for seed in (None, 7, 31337):
        counter = Counter()
        for t in J.trials(seed):
            if t['id'] == 'AT1':
                continue
            for side in ('a', 'b'):
                counter.update(t[side])
        feature_texts = set(J.PHRASE_TO_FEATURE)
        assert all(v <= 10 for p, v in counter.items() if p in feature_texts)
        assert all(v <= 8 for p, v in counter.items() if p not in feature_texts)
        assert len([p for p in counter if p in feature_texts]) == 12 * 3
        assert set(p for p in counter if p not in feature_texts) == set(J.FILLERS)


def test_score_with_seed_roundtrip():
    """种子卷作答 + 同种子交卷：质检全过、植入偏好照常回收（计分对种子不敏感）。"""
    seed = 87654321
    report = J.score_responses([_planted_choice(t) for t in J.trials(seed)], seed)
    assert report['quality']['grade'] == 'ok'
    assert report['quality']['anchor_agreement'] == 3
    verdicts = {f['key']: f['verdict'] for f in report['features']}
    assert verdicts['w1'] in ('strong_like', 'like')
    assert verdicts['w2'] in ('strong_avoid', 'avoid')


def test_api_seed_roundtrip(client):
    trials = client.get('/api/jd-assessment/trials', params={'seed': 123456}).json()['trials']
    responses = [dict(trial_id=t['id'], choice=_planted_choice(t)['choice'], rt_ms=2000) for t in trials]
    saved = client.post('/api/jd-assessment', json={'responses': responses, 'seed': 123456})
    assert saved.status_code == 200
    assert saved.json()['payload']['seed'] == 123456
    assert saved.json()['payload']['report']['quality']['grade'] == 'ok'
    # 种子卷面确实与默认卷不同
    plain = client.get('/api/jd-assessment/trials').json()['trials']
    assert [t['id'] for t in plain] != [t['id'] for t in trials]
