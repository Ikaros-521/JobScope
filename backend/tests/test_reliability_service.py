import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability.models import SignalInput
from jobscope.reliability.rubric import RULES
from jobscope.reliability.service import ReliabilityService
from jobscope.repository import Repository


def build(tmp_path):
    repo = Repository(tmp_path)
    return repo, ReliabilityService(repo)


def add(repo, company, rule, value, evidence, source_class='official', collected_at='2026-11-09T00:00:00+00:00'):
    signal = SignalInput(company_id=company['id'], dimension=RULES[rule].dimension, rule=rule, value=value,
                         unit=RULES[rule].unit, source_class=source_class, evidence_id=evidence,
                         observed_at='', collected_at=collected_at)
    return repo.save_signal(signal)


def dimension(report, name):
    return next(item for item in report['dimensions'] if item['dimension'] == name)


def test_a_company_without_signals_gets_no_number_but_still_lists_gaps(tmp_path):
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    report = service.recompute(company['name'])
    assert report['score'] is None and report['confidence'] == 0
    assert len(report['dimensions']) == 6
    assert all(item['score'] is None and item['confidence'] == 0 and not item['findings']
               for item in report['dimensions'])
    # 缺口清单只可能由规则表推出来：P0 有 ASSESSMENT 规则的是 stability，它列出三条「尚无来源」；
    # 其余维度一条可评价规则都没有，rubric-2 就没话可说（修正记录 #5/#8），service 不替它编文案。
    gaps = {item['dimension']: item['missing'] for item in report['dimensions']}
    assert sorted(name for name, lines in gaps.items() if lines) == ['stability']
    assert [line.split('（')[0] for line in gaps['stability']] == sorted(
        rule for rule, spec in RULES.items() if spec.dimension == 'stability')
    assert all('尚无来源' in line for line in gaps['stability'])
    assert report['disclaimer'] and report['nature'] == 'subjective_reference'


def test_recompute_is_idempotent_for_identical_inputs(tmp_path):
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    add(repo, company, 'company_age', {'years': 8}, 'E-' + 'a' * 24)
    first = service.recompute('甲公司')
    second = service.recompute('甲公司')
    assert first['input_hash'] == second['input_hash']
    assert len(repo.reliability_history(company['id'])) == 1


def test_new_evidence_changes_the_hash_and_keeps_history(tmp_path):
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    add(repo, company, 'company_age', {'years': 8}, 'E-' + 'a' * 24)
    service.recompute('甲公司')
    add(repo, company, 'disclosure_available', {'count': 3}, 'E-' + 'b' * 24)
    service.recompute('甲公司')
    history = repo.reliability_history(company['id'])
    assert len(history) == 2
    assert history[0]['input_hash'] != history[1]['input_hash']
    assert repo.latest_reliability(company['id'])['score'] is not None
    assert service.latest('甲公司')['input_hash'] == history[1]['input_hash']


def test_findings_carry_the_evidence_they_came_from(tmp_path):
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    evidence = 'E-' + 'c' * 24
    add(repo, company, 'company_age', {'years': 8}, evidence)
    report = service.recompute('甲公司')
    stability = dimension(report, 'stability')
    assert stability['evidence_ids'] == [evidence]
    assert stability['findings'][0]['evidence_id'] == evidence


def test_every_persisted_finding_points_back_to_one_saved_signal(tmp_path):
    # 「无证据不出结论」这条红线在数据结构上的落点：持久化的每一句话都必须带得出处的编号。
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    saved = {item['evidence_id'] for item in repo.signals(company['id'])}
    for rule, value, evidence, source in (
        ('company_age', {'years': 8}, 'E-' + 'd' * 24, 'official'),
        ('insured_headcount_trend', {'before': 312, 'after': 330}, 'E-' + 'e' * 24, 'platform'),
        ('salary_quote_coverage', {'known': 9, 'total': 10}, 'E-' + 'f' * 24, 'user_material'),
        ('employee_review_sample', {'samples': 3}, 'E-' + '1' * 24, 'employee'),
    ):
        add(repo, company, rule, value, evidence, source_class=source)
        saved.add(evidence)
    report = service.recompute('甲公司')
    findings = [item for entry in report['dimensions'] for item in entry['findings']]
    assert len(findings) == 4
    for entry in report['dimensions']:
        for item in entry['findings']:
            assert item['evidence_id'] and item['evidence_id'] in saved
            assert item['evidence_id'] in entry['evidence_ids']
            assert item['text'] and '{' not in item['text']
    assert set(report['evidence_ids']) == {item['evidence_id'] for item in findings}


def test_a_material_rule_describes_coverage_without_producing_a_score(tmp_path):
    # MATERIAL 规则量的是本项目手上的材料，不是这家公司的薪资待遇：只出文字，不出分。
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    evidence = 'E-' + '2' * 24
    add(repo, company, 'salary_quote_coverage', {'known': 9, 'total': 10}, evidence)
    report = service.recompute('甲公司')
    assert report['score'] is None and report['confidence'] == 0
    pay = dimension(report, 'pay')
    assert pay['score'] is None and pay['confidence'] == 0
    assert pay['findings'][0]['text'] == '该公司已保存岗位中 9/10 条能解析出人民币月薪区间，其余报价口径不支持。'
    assert pay['findings'][0]['evidence_id'] == evidence
    assert any('可评价的规则' in line for line in pay['missing'])
    assert report['summary'] == '可纳入计算的公开依据不足，暂不给出综合参考分。补齐下方缺口后重新计算。'


def test_a_signal_past_its_ttl_is_demoted_to_a_gap_instead_of_a_number(tmp_path):
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    stale = (datetime.now(timezone.utc) - timedelta(days=RULES['company_age'].ttl_days + 30)).isoformat()
    add(repo, company, 'company_age', {'years': 8}, 'E-' + '3' * 24, collected_at=stale)
    report = service.recompute('甲公司')
    stability = dimension(report, 'stability')
    assert stability['score'] is None and stability['findings'] == []
    assert any('company_age' in line and '已过期' in line for line in stability['missing'])
    assert report['score'] is None


def test_report_carries_the_three_versions_and_a_subjective_reference_label(tmp_path):
    from jobscope.reliability import wording
    from jobscope.reliability.models import DISCLAIMER, DISCLAIMER_VERSION
    from jobscope.reliability.rubric import RUBRIC_VERSION
    repo, service = build(tmp_path)
    company = repo.resolve_company('甲公司')
    add(repo, company, 'company_age', {'years': 8}, 'E-' + '4' * 24)
    report = service.recompute('甲公司')
    assert (report['rubric_version'], report['wording_version'], report['disclaimer_version']) == (
        RUBRIC_VERSION, wording.WORDING_VERSION, DISCLAIMER_VERSION)
    assert report['disclaimer'] == DISCLAIMER
    assert report['limitations'] and report['computed_at']
