import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobscope.reliability.rubric import RULES, score
from jobscope.reliability.wording import BANNED, WORDING_VERSION, lint, render


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
    finding = next(iter(score([sig(rule, {key: probe for key in VALUES[rule]})]).findings))
    text = render(finding)
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
