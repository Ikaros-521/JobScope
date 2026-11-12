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
    # P1 起 pay 有了评价规则（pay_vs_city_median）：缺口是「尚无来源」，不再是「没有可评价的规则」。
    assert any('pay_vs_city_median' in line and '尚无来源' in line for line in pay.missing)


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


# --- P1 四条样本闸门：不满足就不出分，且缺口文案能看出具体原因 ---

def test_pay_gate_fails_when_the_city_sample_is_below_twenty():
    result = score([sig('pay_vs_city_median', {'company_median': 20000, 'city_median': 18000,
                                               'ratio': 1.11, 'company_jobs': 5, 'city_jobs': 15})])
    pay = next(item for item in result.dimensions if item.dimension == 'pay')
    assert pay.score is None and pay.confidence == 0
    assert any('pay_vs_city_median' in line and '同层 ≥20 条' in line for line in pay.missing)


def test_pay_gate_fails_when_the_company_sample_is_below_three():
    result = score([sig('pay_vs_city_median', {'company_median': 20000, 'city_median': 18000,
                                               'ratio': 1.11, 'company_jobs': 2, 'city_jobs': 30})])
    pay = next(item for item in result.dimensions if item.dimension == 'pay')
    assert pay.score is None
    assert any('本公司 ≥3 条' in line for line in pay.missing)


def test_pay_gate_fails_without_a_positive_ratio_or_median():
    # 比值与中位数都不成立时闸门必须拦住：没有正的分母就不出分，缺口由 gate_note 说清。
    result = score([sig('pay_vs_city_median', {'company_median': 20000, 'city_median': 0,
                                               'ratio': 0, 'company_jobs': 5, 'city_jobs': 30})])
    pay = next(item for item in result.dimensions if item.dimension == 'pay')
    assert pay.score is None and pay.confidence == 0
    assert any('pay_vs_city_median' in line and '分层' in line for line in pay.missing)


def test_team_size_gate_fails_with_fewer_than_two_statements():
    result = score([sig('team_size_statement', {'median_size': 30, 'statements': 1, 'compatible': 1})])
    people = next(item for item in result.dimensions if item.dimension == 'people')
    assert people.score is None and people.confidence == 0
    assert any('team_size_statement' in line and '至少需要 2 条' in line for line in people.missing)


def test_trend_gate_fails_without_a_positive_previous_value():
    result = score([sig('disclosure_revenue_trend', {'current': 120000000, 'previous': 0})])
    operations = next(item for item in result.dimensions if item.dimension == 'operations')
    assert operations.score is None
    assert any('上期大于 0' in line for line in operations.missing)
    result = score([sig('disclosure_headcount_trend', {'current': 240, 'previous': -5})])
    operations = next(item for item in result.dimensions if item.dimension == 'operations')
    assert operations.score is None


# --- P1 新规则的映射表，与规格逐档一致 ---

def test_pay_ratio_bands_follow_the_spec():
    def pay_at(company, city):
        result = score([sig('pay_vs_city_median', {'company_median': company, 'city_median': city,
                                                   'ratio': 0, 'company_jobs': 5, 'city_jobs': 30})])
        return next(item for item in result.dimensions if item.dimension == 'pay').score
    assert pay_at(24000, 20000) == 95.0   # ratio 1.2
    assert pay_at(21000, 20000) == 80.0   # ratio 1.05
    assert pay_at(18000, 20000) == 60.0   # ratio 0.9
    assert pay_at(15000, 20000) == 45.0   # ratio 0.75
    assert pay_at(12000, 20000) == 30.0   # ratio 0.6


def test_jd_specificity_scores_only_three_or_more_complete_descriptions():
    base = {'jobs': 3, 'chars_mean': 600.0, 'items_mean': 8.0, 'skills_mean': 8.0, 'quantified_share': 1.0}
    result = score([sig('jd_specificity', base)])
    professional = next(item for item in result.dimensions if item.dimension == 'professional')
    # 四个子指标全部触顶：加权和就是满分。
    assert professional.score == 100.0
    thin = score([sig('jd_specificity', {**base, 'jobs': 2})])
    assert next(item for item in thin.dimensions if item.dimension == 'professional').score is None


def test_team_size_scores_statement_agreement_not_size_itself():
    def people_at(statements, compatible):
        result = score([sig('team_size_statement', {'median_size': 30.0, 'statements': statements,
                                                    'compatible': compatible})])
        return next(item for item in result.dimensions if item.dimension == 'people').score
    # 分数只跟自述之间的相容占比有关：30 人与 3000 人的公司在自述一致时同分。
    assert people_at(2, 2) == 90.0
    assert people_at(3, 2) == 60.0
    assert people_at(4, 1) == 30.0
    small = score([sig('team_size_statement', {'median_size': 8.0, 'statements': 3, 'compatible': 3})])
    large = score([sig('team_size_statement', {'median_size': 8000.0, 'statements': 3, 'compatible': 3})])
    assert small.score == large.score == 90.0


def test_revenue_and_headcount_trends_share_the_yoy_bands():
    def trend_at(rule, current, previous):
        result = score([sig(rule, {'current': current, 'previous': previous})])
        return next(item for item in result.dimensions if item.dimension == 'operations').score
    for rule in ('disclosure_revenue_trend', 'disclosure_headcount_trend'):
        assert trend_at(rule, 130, 100) == 95.0   # +30%
        assert trend_at(rule, 110, 100) == 80.0   # +10%
        assert trend_at(rule, 100, 100) == 65.0   # 持平
        assert trend_at(rule, 85, 100) == 45.0    # -15%
        assert trend_at(rule, 60, 100) == 25.0    # -40%


def test_industry_stays_unscored_with_only_a_material_registration():
    result = score([sig('industry_media_count', {'count': 3})])
    industry = next(item for item in result.dimensions if item.dimension == 'industry')
    assert result.score is None and industry.score is None and industry.confidence == 0
    # rubric 层的 RuleFinding 不带 kind：登记类这条在规则表里就是 MATERIAL，持久化后才写成 null 分。
    assert [item.rule for item in industry.findings] == ['industry_media_count']
    assert RULES['industry_media_count'].kind == MATERIAL
    assert any('可评价的规则' in line for line in industry.missing)
