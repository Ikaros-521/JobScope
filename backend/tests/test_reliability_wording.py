import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobscope.reliability.rubric import RULES, score
from jobscope.reliability.wording import BANNED, WORDING_VERSION, lint, overall_text, render


VALUES = {
    'company_age': {'years': 8},
    'insured_headcount_trend': {'before': 312, 'after': 240},
    'judicial_case_ratio': {'ratio': 12},
    'salary_quote_coverage': {'known': 9, 'total': 10},
    'employee_review_sample': {'samples': 3, 'negative': 3},
    'disclosure_available': {'count': 2},
}


def sig(rule, value, source_class='official'):
    from jobscope.reliability.models import SignalInput
    return SignalInput(company_id='CO-1', dimension=RULES[rule].dimension, rule=rule, value=value,
                       unit=RULES[rule].unit, source_class=source_class, evidence_id='E-' + rule,
                       observed_at='', collected_at='2026-11-09T00:00:00+00:00')


def test_every_rule_template_renders_without_placeholders():
    result = score([sig(rule, value) for rule, value in VALUES.items()])
    assert len(result.findings) == len(VALUES)
    for finding in result.findings:
        text = render(finding)
        assert '{' not in text and text.endswith('。'), text
        assert lint(text) == []


@pytest.mark.parametrize('rule', sorted(VALUES))
@pytest.mark.parametrize('probe', [0, 1, 7, 10 ** 6])
def test_templates_stay_clean_and_fillable_at_extreme_values(rule, probe):
    result = score([sig(rule, {key: probe for key in VALUES[rule]})])
    findings = [item for item in result.findings if item.rule == rule]
    if not findings:
        # 极端取值下判不了就不出句：必须落到缺口清单，而不是硬凑一句话。
        assert any(rule in line for item in result.dimensions for line in item.missing)
        return
    text = render(findings[0])
    assert '{' not in text and lint(text) == []


def test_banned_assertive_words_are_rejected():
    for text in ['该公司存在欠薪问题。', '疑似皮包公司，风险很高。', '正在大规模裁员。', '员工评价这是黑心企业。']:
        assert lint(text)


def test_headcount_wording_states_numbers_and_refuses_a_conclusion():
    finding = next(iter(score([sig('insured_headcount_trend', {'before': 312, 'after': 240})]).findings))
    text = render(finding)
    assert '312' in text and '240' in text
    assert '不推断' in text and lint(text) == []


def test_self_reported_material_is_labelled_as_unverified():
    text = render(next(iter(score([sig('employee_review_sample', VALUES['employee_review_sample'], 'employee')]).findings)))
    assert '未见独立核实' in text


def test_unsupported_rule_names_raise_instead_of_inventing_a_sentence():
    from jobscope.reliability.models import RuleFinding
    with pytest.raises(KeyError):
        render(RuleFinding(rule='x', dimension='stability', score=50, template='no_such_template',
                           data={}, evidence_id='E-1', source_class='official', weight=1))


def test_rule_templates_and_registry_stay_one_to_one():
    from jobscope.reliability.wording import TEMPLATES
    assert set(TEMPLATES) == {spec.template for spec in RULES.values()}


def test_mid_range_score_does_not_claim_problems():
    # 成立 2 年但在扩员的公司会落在 55 上下；这一段只能说材料方向不一致，不能说「出现了需要核实的信号」。
    text = overall_text(55.6, 0.22)
    assert '方向不一致' in text and '重点核实' not in text
    assert lint(text) == []


def test_only_the_low_band_asks_for_verification():
    assert '重点核实' in overall_text(30.0, 0.22)
    for value in (45, 60, 75, 90):
        assert '重点核实' not in overall_text(value, 0.5)


def test_sample_count_never_carries_a_size_judgement():
    # 条数多少不等于可信与否，模板不许替用户下这个判断。
    for samples in (1, 3, 10 ** 6):
        text = render(next(iter(score([sig('employee_review_sample', {'samples': samples})]).findings)))
        assert '样本较小' not in text and str(samples) in text
