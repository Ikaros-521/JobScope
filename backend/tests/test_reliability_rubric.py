import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability.rubric import ASSESSMENT, DIMENSIONS, MATERIAL, RULES, score


def sig(rule, value, source_class='official', evidence='E-1'):
    from jobscope.reliability.models import SignalInput
    return SignalInput(company_id='CO-1', dimension=RULES[rule].dimension, rule=rule, value=value,
                       unit=RULES[rule].unit, source_class=source_class, evidence_id=evidence,
                       observed_at='', collected_at='2026-11-09T00:00:00+00:00')


def test_every_rule_declares_a_dimension_ttl_and_kind():
    for name, rule in RULES.items():
        assert rule.dimension in DIMENSIONS
        assert rule.ttl_days > 0
        assert rule.template
        assert rule.kind in {ASSESSMENT, MATERIAL}


def test_missing_rules_leave_the_dimension_unknown_instead_of_mid_range():
    result = score([sig('company_age', {'years': 8})])
    pay = next(item for item in result.dimensions if item.dimension == 'pay')
    assert pay.score is None and pay.confidence == 0
    assert result.confidence < 1


def test_absence_of_negative_signals_does_not_raise_the_score():
    bare = score([])
    assert bare.score is None
    assert bare.confidence == 0


def test_overall_averages_only_dimensions_with_assessment_rules():
    result = score([sig('company_age', {'years': 8}), sig('insured_headcount_trend', {'before': 312, 'after': 330})])
    stability = next(item for item in result.dimensions if item.dimension == 'stability')
    assert result.score == stability.score
    assert result.confidence == round(DIMENSIONS['stability'].weight, 3)


def test_material_rules_describe_coverage_without_producing_a_score():
    # 「9/10 条岗位能解析出月薪」是本项目的数据覆盖度，不是对这家公司的薪资待遇。
    result = score([sig('salary_quote_coverage', {'known': 9, 'total': 10})])
    pay = next(item for item in result.dimensions if item.dimension == 'pay')
    assert result.score is None and pay.score is None and pay.confidence == 0
    assert [item.rule for item in pay.findings] == ['salary_quote_coverage']
    assert any('可评价的规则' in line for line in pay.missing)


def test_absence_of_court_records_is_not_read_as_a_compliment():
    result = score([sig('company_age', {'years': 8}), sig('judicial_case_ratio', {'ratio': 0})])
    stability = next(item for item in result.dimensions if item.dimension == 'stability')
    assert [item.rule for item in stability.findings] == ['company_age']
    assert any('judicial_case_ratio' in line for line in stability.missing)


def test_employee_only_findings_cannot_settle_a_dimension():
    result = score([sig('employee_review_sample', {'samples': 3, 'negative': 3}, source_class='employee')])
    people = next(item for item in result.dimensions if item.dimension == 'people')
    assert people.score is None
    assert people.missing


def test_stale_signals_are_excluded_and_reported_as_missing():
    result = score([sig('company_age', {'years': 8})], stale_rules={'company_age'})
    stability = next(item for item in result.dimensions if item.dimension == 'stability')
    assert stability.score is None
    assert 'company_age' in ' '.join(stability.missing)
