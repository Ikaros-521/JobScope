"""工作风格卷（2026-11-24 第二种考卷）：卷面不变量、计分口径、分档校准仿真、质检闸门、互证规则与 API。

设计质量逐条断言（与 test_jd_assessment.py 同一纪律）：每轴 12 句、正反极各 6 句；任意种子下同轴两句
间隔 ≥ 3、同一极向最多连出 3 句；计分口径用无噪声植入人格逐项核对（含「默认同意」必须被反向题抵消、
阈值边界不能被浮点表示带偏）。分档准确率与重测稳定性用仿真验证，并且直接调线上 score_style，
上下界取 output/calibrate_style_scale.py 的校准值留余量。
"""
import math
import random as _random
from collections import Counter
from itertools import combinations
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import jd_assessment as J
from jobscope import work_style as W
from jobscope.main import create_app
from jobscope.repository import Repository

AXIS_KEYS = [axis['key'] for axis in W.AXES]


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


# ── 仿真作答：thetas 是各轴的真倾向（d 量表，单位 = 量表「格」），σ 是单题噪声，load 是题目负荷（题目质量）。 ──

def _respond(thetas, rng, sigma=1.05, load=None, acq=0.0, extreme=False, rt=2600):
    rows = []
    for item in W.ITEMS:
        sign = 1 if item['pole'] == 'b' else -1
        y = 2 + sign * (load or {}).get(item['id'], 1.0) * thetas[item['axis']] + acq + rng.gauss(0, sigma)
        value = min(4, max(0, math.floor(y + 0.5)))
        if extreme:
            value = {1: 0, 3: 4}.get(value, value)
        rows.append(dict(item_id=item['id'], value=value, rt_ms=rt))
    rows.append(dict(item_id=W.ATTENTION_ITEM['id'], value=W.ATTENTION_EXPECT, rt_ms=rt))
    return rows


def _planted(**thetas):
    """无噪声植入人格：每句按 2 ± θ 取整（θ=1 → 自己一极选「同意」3、另一极选「不同意」1，d = θ）。"""
    return _respond({**{key: 0.0 for key in AXIS_KEYS}, **thetas}, _random.Random(0), sigma=0.0)


def _constant(value, rt=2600):
    return [dict(item_id=item['id'], value=value, rt_ms=rt) for item in W.paper()['items']]


def _axis(report, key):
    return next(axis for axis in report['axes'] if axis['key'] == key)


# ── 题库与卷面。 ──

def test_bank_shape_and_content_coverage():
    assert len(W.AXES) == 4 and len(W.ITEMS) == 48 and W.PER_AXIS == 12 and W.PER_POLE == 6
    assert [item['id'] for item in W.ITEMS] == [f'S{i:02d}' for i in range(1, 49)]
    assert Counter((item['axis'], item['pole']) for item in W.ITEMS) == {
        (axis, pole): 6 for axis in AXIS_KEYS for pole in 'ab'}
    texts = [item['text'] for item in W.ITEMS]
    assert len(set(texts)) == 48 and all(12 <= len(text) <= 40 for text in texts)
    assert len({text[:6] for text in texts}) == 48, '没有哪两句是换个说法的重复'
    assert all(text.endswith('。') for text in texts)
    # 两极画像齐全，名字与轴定义一致；选项五级；注意力题不在计分题库里
    pole_axis = {pole['key']: axis['key'] for axis in W.AXES for pole in (axis['a'], axis['b'])}
    assert set(pole_axis) == set(W.POLE_CONTENT)
    for axis in W.AXES:
        for side in 'ab':
            content = W.POLE_CONTENT[axis[side]['key']]
            assert content['name'] == axis[side]['name'] and content['tagline']
            assert len(content['strengths']) >= 3 and len(content['watch_outs']) >= 2
            assert len(content['seek']) >= 3 and len(content['caution']) >= 3
    assert [step['label'] for step in W.SCALE] == ['非常不同意', '不同意', '不确定', '同意', '非常同意']
    assert W.ATTENTION_ITEM['id'] not in W.ITEM_BY_ID and W.SCALE[W.ATTENTION_EXPECT]['label'] == '同意'
    # 两两组合的原型名覆盖全部「跨轴的两极」组合（24 种），不多不少、不撞名
    expected = {frozenset(pair) for pair in combinations(pole_axis, 2) if pole_axis[pair[0]] != pole_axis[pair[1]]}
    assert set(W.ARCHETYPES) == expected and len(expected) == 24
    assert all(value['name'] and value['essence'] and value['fits'] and value['drains'] for value in W.ARCHETYPES.values())
    assert len({value['name'] for value in W.ARCHETYPES.values()}) == 24


def test_paper_layout_invariants():
    """任意种子：48 计分句 + 1 注意力句各出现一次；同轴两句间隔 ≥ MIN_GAP；同一极向连出 ≤ POLE_RUN；
    注意力题落在中段三分之一；不同种子卷面不同，默认卷固定。"""
    orders = set()
    for seed in [None, *range(40)]:
        sheet = W.build_paper(seed)
        assert Counter(item['id'] for item in sheet) == Counter({**{item['id']: 1 for item in W.ITEMS}, W.ATTENTION_ITEM['id']: 1})
        last, previous, run = {}, None, 0
        for position, item in enumerate(sheet):
            if item['id'] == W.ATTENTION_ITEM['id']:
                assert len(sheet) // 3 <= position <= len(sheet) * 2 // 3
                continue
            assert position - last.get(item['axis'], -W.MIN_GAP) >= W.MIN_GAP, (seed, item['id'])
            last[item['axis']] = position
            run = run + 1 if item['pole'] == previous else 1
            previous = item['pole']
            assert run <= W.POLE_RUN, (seed, item['id'])
        orders.add(tuple(item['id'] for item in sheet))
    assert len(orders) == 41, '每种子的卷面都应不同'
    assert [item['id'] for item in W.build_paper()] == [item['id'] for item in W.build_paper()]


def test_paper_payload_hides_scoring_fields():
    payload = W.paper()
    assert set(payload) == {'items', 'scale', 'model'} and payload['model'] == W.MODEL
    assert len(payload['items']) == 49 and all(set(item) == {'id', 'text'} for item in payload['items'])
    assert [step['value'] for step in payload['scale']] == [0, 1, 2, 3, 4]


# ── 计分口径。 ──

def test_scoring_math_and_acquiescence_cancels():
    report = W.score_style(_planted(focus=1.0, thinking=-1.0, deciding=0.5))
    assert report['kind'] == 'style' and report['model'] == W.MODEL
    # 默认同意偏移只统计 48 句计分题（不含注意力题）：这里 raw 合计 102
    assert report['quality'] == dict(attention_pass=True, straight=False, fast_items=0, items=49,
                                     agree_bias=pytest.approx(102 / 48 - 2, abs=1e-4), flags=[], grade='ok')
    assert [axis['d'] for axis in report['axes']] == [1.0, -1.0, 0.5, 0.0]
    assert [axis['band'] for axis in report['axes']] == ['clear', 'clear', 'slight', 'even']
    assert [axis['pole'] for axis in report['axes']] == ['team', 'grounded', 'empathy', None]
    assert [axis['pole_name'] for axis in report['axes']] == ['协作共创', '落地务实', '共情权衡', None]
    assert _axis(report, 'focus')['se'] == 0 and _axis(report, 'focus')['n_items'] == 12
    assert _axis(report, 'focus')['label'] == '能量来源' and _axis(report, 'focus')['pole_a_name'] == '独立专注'
    # 原型 = 判型轴里最强的两极（明显档优先、强度降序）；rank 保留全部判型极，poles 带完整画像
    assert report['type']['poles'] == ['team', 'grounded'] and report['type']['names'] == ['协作共创', '落地务实']
    assert report['type']['headline'] == '协作共创 × 落地务实'
    assert report['type']['name'] == W.ARCHETYPES[frozenset(('team', 'grounded'))]['name']
    assert [rank['pole'] for rank in report['type']['rank']] == ['team', 'grounded', 'empathy']
    assert [pole['key'] for pole in report['poles']] == ['team', 'grounded', 'empathy']
    assert report['poles'][0]['strengths'] == W.POLE_CONTENT['team']['strengths']
    assert report['poles'][2]['band_label'] == '略偏'
    # 「默认同意」：每句都多选一格同意 → 反向题折算后正好抵消，轴分与判型不变（但会触发默认同意警示）
    shifted = W.score_style(_respond(dict(focus=1.0, thinking=-1.0, deciding=0.5, rhythm=0.0), _random.Random(0),
                                     sigma=0.0, acq=1.0))
    assert [axis['d'] for axis in shifted['axes']] == [axis['d'] for axis in report['axes']]
    assert shifted['quality']['agree_bias'] == pytest.approx(150 / 48 - 2, abs=1e-4) and shifted['quality']['grade'] == 'weak'
    assert any('同意' in flag for flag in shifted['quality']['flags'])


def test_band_thresholds_are_exact_and_seed_independent():
    """阈值边界用整数净偏离逐格核对：每轴 12 句，净偏离 12（平均一整格）→ 明显，11 → 略偏，
    6（半格）→ 略偏，5 → 不明显；负值朝 a 极。计分按题目编号，与题序（种子）无关。"""
    def with_net(net, axis='focus'):
        rows = _planted()
        for index, item in enumerate(item for item in W.ITEMS if item['axis'] == axis):
            step = 1 if net > 0 else -1                      # 前 |net| 句朝 b 极（净偏离为负时朝 a 极）偏一格
            row = next(row for row in rows if row['item_id'] == item['id'])
            row['value'] = 2 + (step if index < abs(net) else 0) * (1 if item['pole'] == 'b' else -1)
        return rows

    for net, band, pole in ((12, 'clear', 'team'), (11, 'slight', 'team'), (6, 'slight', 'team'), (5, 'even', None), (0, 'even', None)):
        axis = _axis(W.score_style(with_net(net)), 'focus')
        assert axis['band'] == band and axis['pole'] == pole and round(axis['d'] * 12) == net, (net, axis)
        assert axis['band_label'] == {'clear': '明显', 'slight': '略偏', 'even': '不明显'}[band]
    assert _axis(W.score_style(with_net(-6)), 'focus')['pole'] == 'solo'
    rows = _planted(focus=1.2, rhythm=-1.2)
    baseline = W.score_style(rows)
    assert [axis['d'] for axis in baseline['axes']] == [1.0, 0.0, 0.0, -1.0]
    assert [axis['d'] for axis in W.score_style(rows, 424242)['axes']] == [axis['d'] for axis in baseline['axes']]


def test_typology_ranking_single_and_balanced():
    only = W.score_style(_planted(focus=-1.0))['type']
    assert only['poles'] == ['solo'] and only['names'] == ['独立专注'] and only['headline'] == '独立专注'
    assert only['name'] == '独立专注型' and W.POLE_CONTENT['solo']['tagline'] in only['essence']
    balanced = W.score_style(_constant(2))
    assert [axis['band'] for axis in balanced['axes']] == ['even'] * 4 and balanced['poles'] == []
    assert balanced['type'] == dict(poles=[], names=[], headline='均衡适应', rank=[], **W.BALANCED)
    # 明显档排在略偏档前面，同档按强度降序；原型取前两极
    report = W.score_style(_planted(focus=-0.5, thinking=1.5, deciding=1.0, rhythm=-2.0))   # θ>0 倒向 b 极
    assert [rank['pole'] for rank in report['type']['rank']] == ['order', 'vision', 'empathy', 'solo']
    assert report['type']['poles'] == ['order', 'vision'] and report['type']['rank'][-1]['band_label'] == '略偏'
    assert report['type']['name'] == W.ARCHETYPES[frozenset(('order', 'vision'))]['name']
    assert [pole['band'] for pole in report['poles']] == ['clear', 'clear', 'clear', 'slight']


# ── 分档校准仿真：直接调线上计分，指标取校准值留余量。 ──

def test_band_accuracy_and_retest_stability():
    """默认作答模型（θ ~ N(0, 0.6²)、单题噪声 1.05、题目负荷 0.6–1.2、默认同意、25% 极端作答）下：
    判型方向准确率 ≥ 92%（校准 96%）；标「明显」的轴重测同向 ≥ 75%（83%）；真倾向 ≥ 1 格的轴判出 ≥ 90%（96%）；
    零倾向的轴被误标「明显」≤ 3%（1.2%）；认真作答被判质检警示 ≤ 4%（1.4%）。"""
    rng = _random.Random(20261124)
    stat = Counter()
    for _ in range(1500):
        thetas = {key: rng.gauss(0, 0.6) for key in AXIS_KEYS}
        load = {item['id']: rng.uniform(0.6, 1.2) for item in W.ITEMS}
        acq, extreme = max(0.0, rng.gauss(0.15, 0.2)), rng.random() < 0.25
        first = W.score_style(_respond(thetas, rng, load=load, acq=acq, extreme=extreme))
        second = W.score_style(_respond(thetas, rng, load=load, acq=acq, extreme=extreme))
        stat['weak'] += first['quality']['grade'] != 'ok'
        for axis in first['axes']:
            theta, band = thetas[axis['key']], axis['band']
            again = _axis(second, axis['key'])
            if band != 'even':
                stat['typed'] += 1
                stat['wrong'] += (axis['d'] > 0) != (theta > 0)
                stat[f'{band}_n'] += 1
                stat[f'{band}_same'] += again['band'] != 'even' and again['pole'] == axis['pole']
            if abs(theta) >= 1.0:
                stat['strong_n'] += 1
                stat['strong_typed'] += band != 'even'
            if abs(theta) < 0.15:
                stat['null_n'] += 1
                stat['null_clear'] += band == 'clear'
    assert stat['typed'] >= 2000 and stat['null_n'] >= 400
    assert stat['wrong'] / stat['typed'] <= 0.08
    assert stat['clear_same'] / stat['clear_n'] >= 0.75 and stat['slight_same'] / stat['slight_n'] >= 0.40
    assert stat['strong_typed'] / stat['strong_n'] >= 0.90
    assert stat['null_clear'] / stat['null_n'] <= 0.03
    assert stat['weak'] / 1500 <= 0.04


# ── 质检闸门。 ──

def test_quality_gates():
    rng = _random.Random(7)
    random_weak = 0
    for _ in range(200):                                  # 随机乱点：靠注意力题拦下大多数
        rows = [dict(item_id=item['id'], value=rng.randrange(5), rt_ms=2600) for item in W.paper()['items']]
        quality = W.score_style(rows)['quality']
        random_weak += quality['grade'] == 'weak'
        if not quality['attention_pass']:
            assert quality['grade'] == 'weak'
    assert random_weak >= 140, f'随机乱点被拦比例过低：{random_weak}/200'
    quality = W.score_style(_constant(2))['quality']
    assert quality['attention_pass'] is False and quality['straight'] is True and quality['grade'] == 'weak'
    assert any('注意力' in flag for flag in quality['flags']) and any('同一选项' in flag for flag in quality['flags'])
    quality = W.score_style(_constant(3))['quality']      # 全选「同意」：注意力题恰好也是同意，只能靠直线闸门
    assert quality['attention_pass'] is True and quality['straight'] is True and quality['grade'] == 'weak'
    rows = [dict(item_id=item['id'], value=rng.choice((3, 4)), rt_ms=2600) for item in W.paper()['items']]
    quality = W.score_style(rows)['quality']
    assert quality['straight'] is False and quality['agree_bias'] >= W.AGREE_BIAS and quality['grade'] == 'weak'
    quality = W.score_style(_constant(2, rt=100))['quality']
    assert quality['fast_items'] == 49 and any('过快' in flag for flag in quality['flags'])
    # 极端但认真的作答（每句都选最强的一档）不该误报，四轴全部判明显
    report = W.score_style(_planted(focus=-2.0, thinking=-2.0, deciding=2.0, rhythm=-2.0))
    assert report['quality'] == dict(attention_pass=True, straight=False, fast_items=0, items=49,
                                     agree_bias=pytest.approx(96 / 48 - 2, abs=1e-4), flags=[], grade='ok')
    assert [axis['d'] for axis in report['axes']] == [-2.0, -2.0, 2.0, -2.0]
    assert [axis['band'] for axis in report['axes']] == ['clear'] * 4
    assert report['type']['name'] == W.ARCHETYPES[frozenset(('solo', 'grounded'))]['name'] == '工匠'


def test_score_rejects_invalid_input():
    rows = _constant(2)
    with pytest.raises(ValueError):
        W.score_style(rows[:-1])                                            # 数量不符
    with pytest.raises(ValueError):
        W.score_style([dict(rows[0], item_id='NOPE'), *rows[1:]])           # 未知题目
    with pytest.raises(ValueError):
        W.score_style([rows[0], rows[0], *rows[2:]])                        # 重复题目
    for bad in (5, -1, True, 2.0, None, '2'):
        with pytest.raises(ValueError):
            W.score_style([dict(rows[0], value=bad), *rows[1:]])            # 越界 / 非整数


# ── 与 JD 取舍卷互证。 ──

def _fake_bws(verdicts):
    """取舍卷报告的最小骨架：只有互证用到的字段。"""
    return {'features': [dict(key=f['key'], screen=f['screen'], phrase=f['phrase'], verdict=verdicts.get(f['key'], 'neutral'),
                              best=1, worst=0, n_present=8) for f in J.FEATURES]}


def _fake_style(poles):
    return {'axes': [dict(key=key, band='even', d=0.0, pole=None) for key in AXIS_KEYS],
            'poles': [dict(key=pole, band=band, band_label=W.BAND_LABELS[band], d=1.0) for pole, band in poles]}


def test_cross_report_rules():
    verdicts = {'i1': 'like', 's3': 'strong_like', 'a1': 'strong_avoid', 'p2': 'like'}
    cross = W.cross_report(_fake_style([('solo', 'clear'), ('grounded', 'slight')]), _fake_bws(verdicts))
    assert [(note['style_pole'], note['bws_key'], note['kind']) for note in cross['notes']] == [
        ('solo', 's3', 'tradeoff'), ('solo', 'i1', 'match'), ('grounded', 'p2', 'match'), ('grounded', 'a1', 'match')]
    assert cross['matches'] == 3 and cross['tradeoffs'] == 1 and cross['summary'] == '3 条互相印证、1 条需要权衡。'
    note = cross['notes'][0]
    assert note['style_name'] == '独立专注' and note['style_band'] == 'clear' and note['kind_label'] == '取舍'
    assert note['bws_label'] == '频繁对外接触' and note['bws_phrase'] and note['text']
    assert (note['bws_best'], note['bws_worst'], note['bws_n']) == (1, 0, 8)
    # 同分档里「取舍」先于「印证」（上一条第 1、2 条）；最多 CROSS_LIMIT 条
    assert len(cross['notes']) == W.CROSS_LIMIT == 4
    every = W.cross_report(_fake_style([('solo', 'clear'), ('grounded', 'clear'), ('vision', 'clear'), ('team', 'clear')]),
                           _fake_bws({f['key']: 'like' for f in J.FEATURES}))
    assert len(every['notes']) == W.CROSS_LIMIT and {note['kind'] for note in every['notes']} == {'tradeoff'}
    assert (every['matches'], every['tradeoffs']) == (0, W.CROSS_LIMIT)
    assert every['summary'] == '4 条需要权衡：风格和诉求之间有几处张力，投递前值得先想清楚。'
    # 每条结论的两边都必须有明确判定：风格极 ≥ 略偏，构念判中的是与规则方向一致的偏好 / 回避
    sides = {(pole, key): side for pole, key, side, kind, text in W.CROSS_RULES}
    for candidate in every['notes']:
        assert candidate['style_band'] == 'clear'
        assert 'like' in W.CROSS_SIDES[sides[(candidate['style_pole'], candidate['bws_key'])]]
    # 风格卷四轴都不明显 → 没有结论但不是 None
    assert W.cross_report(_fake_style([]), _fake_bws(verdicts)) == dict(
        notes=[], matches=0, tradeoffs=0, summary='两卷之间没有明显的印证或冲突，各看各的结论即可。')
    assert W.cross_report(_fake_style([('solo', 'slight')]), _fake_bws({}))['notes'] == []
    # 任一方缺失 / 旧版取舍卷报告 → None
    style = _fake_style([('solo', 'clear')])
    assert W.cross_report(None, _fake_bws(verdicts)) is None and W.cross_report(style, None) is None
    assert W.cross_report(style, {'features': []}) is None and W.cross_report({'poles': []}, _fake_bws(verdicts)) is None
    legacy = dict(_fake_bws(verdicts), features=[{k: v for k, v in feature.items() if k != 'verdict'} for feature in _fake_bws(verdicts)['features']])
    assert W.cross_report(style, legacy) is None


def test_cross_rules_reference_real_constructs():
    """互证规则表引用的风格极、取舍卷构念都必须真实存在，同一（极, 构念, 方向）不重复，两极都有规则。"""
    poles = {pole['key'] for axis in W.AXES for pole in (axis['a'], axis['b'])}
    seen = set()
    for pole, key, side, kind, text in W.CROSS_RULES:
        assert pole in poles and key in J.FEATURE_BY_KEY and side in W.CROSS_SIDES and kind in W.CROSS_KINDS
        assert text.strip() and (pole, key, side) not in seen
        seen.add((pole, key, side))
    assert {pole for pole, key, side in seen} == poles


def _bws_choice(trial):
    """取舍卷植入人格（标准 MaxDiff 作答模型）：偏好 i1 / a1，回避 s3；按题目内容播种，注意力题认真作答。"""
    rng = _random.Random(f'cross:{tuple(sorted(trial["items"]))}')
    items = trial['items']
    if trial['id'] == J.ATTENTION_TRIAL['id']:
        return dict(trial_id=trial['id'], best=items.index(J.ATTENTION_TRIAL['items'][J.ATTENTION_BEST]),
                    worst=items.index(J.ATTENTION_TRIAL['items'][J.ATTENTION_WORST]), rt_ms=1800)
    likes, hates, scale = {'i1', 'a1'}, {'s3'}, 3.0
    utility = [scale if J.PHRASE_TO_FEATURE[p]['key'] in likes else -scale if J.PHRASE_TO_FEATURE[p]['key'] in hates else 0.0
               for p in items]
    best = rng.choices(range(4), weights=[math.exp(v - max(utility)) for v in utility])[0]
    rest = [i for i in range(4) if i != best]
    negative = [-utility[i] for i in rest]
    worst = rest[rng.choices(range(3), weights=[math.exp(v - max(negative)) for v in negative])[0]]
    return dict(trial_id=trial['id'], best=best, worst=worst, rt_ms=1800)


# ── API。 ──

def _post_style(client, responses, seed=None):
    return client.post('/api/jd-assessment/style', json={'responses': responses, 'seed': seed})


def test_api_roundtrip_and_listing_by_kind(client):
    trials = client.get('/api/jd-assessment/style/trials', params={'seed': 123456}).json()
    assert set(trials) == {'items', 'scale', 'model'} and len(trials['items']) == 49
    assert [item['id'] for item in trials['items']] != [item['id'] for item in client.get('/api/jd-assessment/style/trials').json()['items']]
    rows = [dict(item_id=item['id'], value=3, rt_ms=2600) for item in trials['items']]
    saved = _post_style(client, rows, 123456)
    assert saved.status_code == 200
    body = saved.json()
    assert body['id'].startswith('WS-') and body['payload']['kind'] == 'style' and body['payload']['seed'] == 123456
    assert body['payload']['report']['model'] == W.MODEL and len(body['payload']['responses']) == 49
    listing = client.get('/api/jd-assessment', params={'kind': 'style'}).json()['assessments']
    assert [item['id'] for item in listing] == [body['id']] and 'rowid' not in listing[0]
    assert client.get('/api/jd-assessment', params={'kind': 'bws'}).json()['assessments'] == []
    assert [item['id'] for item in client.get('/api/jd-assessment').json()['assessments']] == [body['id']]
    assert client.get('/api/jd-assessment', params={'kind': 'nope'}).status_code == 422
    # 只做过一卷 → 说明缺哪卷；两卷都做过 → 读时互证
    assert client.get('/api/jd-assessment/cross').json() == {'cross': None, 'missing': ['bws'], 'legacy': []}
    bws_trials = client.get('/api/jd-assessment/trials').json()['trials']
    bws = client.post('/api/jd-assessment', json={'responses': [dict(trial_id=trial['id'], best=0, worst=1, rt_ms=2000) for trial in bws_trials]}).json()
    assert bws['id'].startswith('JD-') and bws['payload']['kind'] == 'bws'
    cross = client.get('/api/jd-assessment/cross').json()
    assert cross['missing'] == [] and cross['legacy'] == []
    assert cross['cross']['notes'] == [] and cross['cross']['summary'].startswith('两卷之间没有明显')
    assert [item['id'] for item in client.get('/api/jd-assessment').json()['assessments']] == [bws['id'], body['id']]


def test_api_cross_with_real_planted_reports(client):
    """两卷都走线上计分：风格卷植入「独立专注 + 落地务实」，取舍卷植入偏好 i1 / a1、回避 s3 →
    互证必须拿到对应的三条（印证 ×2、取舍 ×1）。"""
    style_report = W.score_style(_planted(focus=-1.6, thinking=-1.6))
    assert _post_style(client, _planted(focus=-1.6, thinking=-1.6)).status_code == 200
    bws_rows = [_bws_choice(trial) for trial in J.trials()]
    assert client.post('/api/jd-assessment', json={'responses': bws_rows}).status_code == 200
    assert style_report['type']['poles'] == ['solo', 'grounded']
    verdicts = {feature['key']: feature['verdict'] for feature in J.score_responses(bws_rows)['features']}
    assert verdicts['i1'] in ('like', 'strong_like') and verdicts['a1'] in ('like', 'strong_like')
    assert verdicts['s3'] in ('avoid', 'strong_avoid')
    cross = client.get('/api/jd-assessment/cross').json()
    assert cross['missing'] == [] and cross['legacy'] == []
    found = {(note['style_pole'], note['bws_key'], note['kind']) for note in cross['cross']['notes']}
    planted = {('grounded', 'a1', 'tradeoff'), ('solo', 'i1', 'match'), ('solo', 's3', 'match')}
    assert planted <= found, found                                    # 第 4 条可能被偶然判中的构念占掉
    notes = cross['cross']['notes']
    assert cross['cross']['summary'] == f"{len(notes) - 1} 条互相印证、1 条需要权衡。"   # 植入的三条里只有一条是取舍


def test_api_cross_reports_legacy_bws(client):
    """取舍卷那份还是旧版（两张岗位卡二选一）报告 → 说明要重测，而不是报错或硬算。"""
    _post_style(client, _constant(2))
    with client.app.state.repo.connection() as conn:
        conn.execute('INSERT INTO jd_assessments (id,payload,created_at) VALUES (?,?,?)',
                     ('JD-legacy000000000000', '{"responses": [], "report": {"features": [{"key": "a1"}], "dimensions": []}}',
                      '2026-11-01T00:00:00+00:00'))
    assert client.get('/api/jd-assessment/cross').json() == {'cross': None, 'missing': [], 'legacy': ['bws']}


def test_retention_is_per_kind_and_legacy_rows_count_as_bws(repo):
    rows = _constant(2)
    style_ids = [repo.save_jd_assessment(dict(kind='style', responses=rows, report={'axes': []}))['id'] for _ in range(6)]
    bws_id = repo.save_jd_assessment(dict(responses=[], report={'features': []}))['id']
    assert bws_id.startswith('JD-') and repo.jd_assessments(kind='bws')[0]['payload']['kind'] == 'bws'
    kept = repo.jd_assessments(kind='style')
    assert [item['id'] for item in kept] == style_ids[::-1][:-1], '只留最近 5 份，最新在前'
    assert [item['id'] for item in repo.jd_assessments()] == [bws_id, *[item['id'] for item in kept]]
    # 改版前的记录没有 kind 字段 → 按 bws 取到；limit 对两卷各自生效
    with repo.connection() as conn:
        conn.execute('INSERT INTO jd_assessments (id,payload,created_at) VALUES (?,?,?)',
                     ('JD-legacy000000000000', '{"responses": [], "report": {}}', '2026-11-01T00:00:00+00:00'))
    assert [item['id'] for item in repo.jd_assessments(kind='bws')] == [bws_id, 'JD-legacy000000000000']
    assert len(repo.jd_assessments(limit=1)) == 2
    assert repo.jd_assessments(kind='nope') == []


def test_api_rejects_bad_payloads(client):
    assert _post_style(client, [dict(item_id='S01', value=5)]).status_code == 422          # 选项越界
    assert _post_style(client, [dict(item_id='S01')]).status_code == 422                   # 缺选项
    assert _post_style(client, [dict(item_id='NOPE', value=2)]).status_code == 400         # 未知题目（数量不符）
    rows = _constant(2)
    rows[3]['value'] = None
    assert _post_style(client, rows).status_code == 422                                   # 选项为空
    # 两卷的作答形态互不接受
    assert client.post('/api/jd-assessment', json={'responses': [dict(item_id='S01', value=2)]}).status_code == 422
    assert client.post('/api/jd-assessment/style', json={'responses': [dict(trial_id='T01', best=0, worst=1)]}).status_code == 422
