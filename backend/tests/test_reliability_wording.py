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
    'pay_vs_city_median': {'company_median': 20000, 'city_median': 18000, 'ratio': 1.11,
                           'company_jobs': 5, 'city_jobs': 30},
    'jd_specificity': {'jobs': 4, 'chars_mean': 350.0, 'items_mean': 6.0, 'skills_mean': 5.0,
                       'quantified_share': 0.25},
    'team_size_statement': {'median_size': 30.0, 'statements': 3, 'compatible': 3},
    'disclosure_revenue_trend': {'current': 120000000, 'previous': 100000000},
    'disclosure_headcount_trend': {'current': 240, 'previous': 260},
    'industry_media_count': {'count': 3},
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


def test_pay_template_declares_its_caliber():
    text = render(next(iter(score([sig('pay_vs_city_median', VALUES['pay_vs_city_median'])]).findings)))
    # 规格要求的口径声明必须逐字在句子里：分层只比标注相当的样本、报价对比、非到手薪资、不含奖金股票。
    assert '报价的对比' in text and '不是实际到手薪资' in text
    assert '不含奖金与股票' in text and '只对比标注相当的样本' in text
    assert '要求相当也不等于职级完全一致' in text and '1.11' in text and lint(text) == []


def test_jd_specificity_template_measures_clarity_not_strength():
    text = render(next(iter(score([sig('jd_specificity', VALUES['jd_specificity'])]).findings)))
    # 这条量的是「把要做什么讲清楚没有」，模板必须把「不代表技术实力」写死。
    assert '讲得有多具体' in text and '不代表公司技术实力' in text and lint(text) == []


def test_team_size_template_states_company_caliber_and_refuses_a_verdict():
    text = render(next(iter(score([sig('team_size_statement', VALUES['team_size_statement'])]).findings)))
    # 规格写死的口径：公司口径自述、未见独立核实、只说明一致度，不评价规模好坏、不说明氛围。
    assert '公司口径' in text and '未见独立核实' in text
    assert '不评价规模好坏' in text and '不说明团队氛围' in text
    assert '30' in text and '3' in text and lint(text) == []


def test_headcount_trend_template_is_the_fixed_wording_and_never_mentions_layoffs():
    text = render(next(iter(score([sig('disclosure_headcount_trend', VALUES['disclosure_headcount_trend'])]).findings)))
    # 规格钉死的写法；「裁员」「裁撤」都在禁用词表里，lint 过不了就出不了报告。
    assert '公告口径在职员工数由 260 变为 240' in text
    assert '不据此判断招聘冻结' in text
    assert '裁员' not in text and '裁撤' not in text and lint(text) == []


def test_revenue_trend_template_states_both_periods_without_a_conclusion():
    text = render(next(iter(score([sig('disclosure_revenue_trend', VALUES['disclosure_revenue_trend'])]).findings)))
    assert '本期营业收入或营收数字为 120000000' in text and '上一期为 100000000' in text
    assert '不推断经营好坏' in text and lint(text) == []


def test_industry_media_count_is_a_material_statement_not_a_verdict():
    text = render(next(iter(score([sig('industry_media_count', VALUES['industry_media_count'])]).findings)))
    # industry 维度 P1 不出分：这句话的作用是「有材料但无可评价依据」，不是任何评价。
    assert '3' in text and '没有可评价行业前景的依据' in text and lint(text) == []
