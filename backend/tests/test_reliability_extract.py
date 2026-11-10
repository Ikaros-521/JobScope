import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability import extract

EVIDENCE = 'E-' + 'a' * 24


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    # 与 test_semantic.py 同一套做法：走真实配置读取路径，只把 HTTP 挡掉。
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')


def mock(monkeypatch, content):
    def post(self, url, **kwargs):
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)


def evidence():
    return [dict(id=EVIDENCE, title='年报摘要', kind='disclosure', text='参保人数 240 人')]


def signals(*items):
    return json.dumps(dict(signals=list(items)), ensure_ascii=False)


def test_clean_json_becomes_validated_signals(monkeypatch):
    mock(monkeypatch, signals(dict(rule='disclosure_available', dimension='operations', value={'count': 3}, evidence_id=EVIDENCE)))
    result = extract.extract('甲公司', evidence())
    assert result['signals'][0].rule == 'disclosure_available'
    assert result['status'] == 'completed'


def test_fenced_and_prose_wrapped_json_still_parses(monkeypatch):
    mock(monkeypatch, '```json\n{"signals":[{"rule":"disclosure_available","dimension":"operations","value":{"count":2},"evidence_id":"' + EVIDENCE + '"}]}\n```\n以上。')
    assert extract.extract('甲公司', evidence())['status'] == 'completed'


def test_invalid_json_is_dropped_with_raw_kept_and_no_signals(monkeypatch):
    mock(monkeypatch, '我觉得这家公司还行')
    result = extract.extract('甲公司', evidence())
    assert result['status'] == 'degraded' and result['signals'] == []
    assert '我觉得' in result['raw']


def test_invented_evidence_ids_are_rejected(monkeypatch):
    mock(monkeypatch, signals(dict(rule='company_age', dimension='stability', value={'years': 5}, evidence_id='E-forged')))
    result = extract.extract('甲公司', evidence())
    assert result['signals'] == [] and 'evidence_id' in result['message']


def test_unknown_rules_are_dropped(monkeypatch):
    mock(monkeypatch, signals(dict(rule='is_it_a_good_company', dimension='stability', value={'x': 1}, evidence_id=EVIDENCE)))
    result = extract.extract('甲公司', evidence())
    assert result['signals'] == [] and '未注册' in result['message']


def test_dimension_must_match_the_rule_table(monkeypatch):
    mock(monkeypatch, signals(dict(rule='company_age', dimension='pay', value={'years': 5}, evidence_id=EVIDENCE)))
    result = extract.extract('甲公司', evidence())
    assert result['signals'] == [] and '维度' in result['message']


def test_no_model_configured_returns_skipped(monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL')
    assert extract.extract('甲公司', [])['status'] == 'skipped'


def test_mixed_value_types_keep_only_numbers(monkeypatch):
    # 模型爱把原文句子和 True/False 一起塞进 value；文字与布尔既不能进 rubric，也不能被读成数字。
    mock(monkeypatch, signals(
        dict(rule='disclosure_available', dimension='operations',
             value={'count': 3, 'ratio': 0.5, 'note': '年报写参保 240 人', 'audited': True, 'available': False}, evidence_id=EVIDENCE),
        dict(rule='company_age', dimension='stability', value={'years': '约 8 年', 'note': '成立较早'}, evidence_id=EVIDENCE)))
    result = extract.extract('甲公司', evidence())
    assert [item.value for item in result['signals']] == [{'count': 3, 'ratio': 0.5}]
    assert result['status'] == 'completed' and 'company_age' in result['message'] and '数值' in result['message']


def test_transport_failure_degrades_to_failed_instead_of_raising(monkeypatch):
    def post(self, url, **kwargs):
        raise httpx.ConnectError('连接被拒绝', request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = extract.extract('甲公司', evidence())
    assert result['status'] == 'failed' and result['signals'] == [] and result['raw'] == ''


def test_the_prompt_forbids_scores_and_names_every_registered_rule():
    prompt = extract.prompt_text()
    assert '分数' in prompt and '不要' in prompt
    for rule in extract.RULES:
        assert rule in prompt


def test_the_prompt_binds_trend_fields_to_one_material():
    prompt = extract.prompt_text()
    # P1 的跨材料拼接防线：同一规则的两个时期数字必须来自同一份材料。
    assert '必须来自同一份材料' in prompt
    assert '跨材料或跨年份拼接' in prompt


def test_the_prompt_carries_the_value_field_hints_of_the_extracted_rules():
    prompt = extract.prompt_text()
    # R3/R4/R5 的 value 字段名进规则清单：模型必须按字段名交数字，聚合由代码完成。
    assert '{"size": 数字}' in prompt and '"size_max"' in prompt
    assert '{"current": 本期数字, "previous": 上期数字}' in prompt
    assert '不要自己算中位数' in prompt


def size_signal(value, evidence):
    return extract.RawSignal(rule='team_size_statement', dimension='people', value=dict(value), evidence_id=evidence)


def test_team_size_aggregation_keeps_a_normal_range_consistent():
    # 一条自述写「5000-10000 人」、另一条写「约 8000 人」：8000 落在实际范围内，两条相容，谁都不扣分。
    merged, participants = extract.aggregate_team_size([
        size_signal({'size': 5000, 'size_max': 10000}, 'E-range'),
        size_signal({'size': 8000}, 'E-point')])
    # 中位数取自述区间中点（7500）与点值（8000）的中位数；锚条在并列时取列表里靠前的一条。
    assert merged.value == {'median_size': 7750.0, 'statements': 2, 'compatible': 2}
    assert merged.evidence_id == 'E-range' and sorted(participants) == ['E-point', 'E-range']


def test_team_size_aggregation_flags_real_contradictions_only():
    # 30 与 45 是同一规模的两种说法；30 与 3000 才是互相矛盾。
    agree, _ = extract.aggregate_team_size([size_signal({'size': 30}, 'E-1'), size_signal({'size': 45}, 'E-2')])
    assert agree.value['compatible'] == 2
    conflict, _ = extract.aggregate_team_size([size_signal({'size': 30}, 'E-1'), size_signal({'size': 3000}, 'E-2')])
    assert conflict.value['compatible'] == 1 and conflict.value['statements'] == 2


def test_team_size_aggregation_needs_two_valid_statements():
    single, participants = extract.aggregate_team_size([size_signal({'size': 30}, 'E-1')])
    assert single is None and participants == ['E-1']
    # 非法数字（0、布尔、负数）不进聚合：四条里只剩一条有效自述，构不成一致性判断。
    merged, participants = extract.aggregate_team_size([
        size_signal({'size': 0}, 'E-1'), size_signal({'size': True}, 'E-2'),
        size_signal({'size': -5}, 'E-3'), size_signal({'size': 40}, 'E-4')])
    assert merged is None and participants == ['E-4']
