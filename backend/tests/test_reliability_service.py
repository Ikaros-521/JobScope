import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability.models import SignalInput
from jobscope.reliability.rubric import RULES
from jobscope.reliability.service import ReliabilityService
from jobscope.repository import Repository


def build(tmp_path, local_source=True):
    """local_source=False 模拟用户在设置里关掉本地来源：recompute 的自动刷新必须尊重这个开关。"""
    repo = Repository(tmp_path)
    if not local_source:
        from jobscope.reliability.sources import SignalSourceInput, SignalSourceStore
        SignalSourceStore(repo).save('jobscope_jobs', SignalSourceInput(mode='disabled'))
    return repo, ReliabilityService(repo)


def add_job(repo, company, title='后端工程师', location='杭州', salary='15-18K·13薪',
            status='complete', description=None):
    payload = dict(company=company, title=title, location=location, salary=salary,
                   description_status=status,
                   description=description or '负责服务端开发\n1、按需求实现接口\n2、排查线上问题',
                   source='boss', url='')
    return repo.save_job(payload, 'boss')


def add(repo, company, rule, value, evidence, source_class='official', collected_at='2026-11-09T00:00:00+00:00'):
    signal = SignalInput(company_id=company['id'], dimension=RULES[rule].dimension, rule=rule, value=value,
                         unit=RULES[rule].unit, source_class=source_class, evidence_id=evidence,
                         observed_at='', collected_at=collected_at)
    return repo.save_signal(signal)


def dimension(report, name):
    return next(item for item in report['dimensions'] if item['dimension'] == name)


def test_a_company_without_signals_gets_no_number_but_still_lists_gaps(tmp_path):
    # 「没有任何信号」现在只能通过关闭本地源构造：重新计算默认会先刷新本地统计。
    repo, service = build(tmp_path, local_source=False)
    company = repo.resolve_company('甲公司')
    report = service.recompute(company['name'])
    assert report['score'] is None and report['confidence'] == 0
    assert len(report['dimensions']) == 6
    assert all(item['score'] is None and item['confidence'] == 0 and not item['findings']
               for item in report['dimensions'])
    # 每个维度都必须自己说清楚为什么没有分数：有评价规则的列「尚无来源」，一条评价规则都没有的说明还没接入来源。
    gaps = {item['dimension']: item['missing'] for item in report['dimensions']}
    assert all(gaps[name] for name in gaps)
    assert [line.split('（')[0] for line in gaps['stability']] == sorted(
        rule for rule, spec in RULES.items() if spec.dimension == 'stability' and spec.kind == 'assessment')
    assert all('尚无来源' in line for line in gaps['stability'])
    # P1 起除 industry 外都有评价规则：缺口是「尚无来源」；industry 仍只有材料登记，说明还没有可评价的规则。
    for name in ('pay', 'operations', 'people', 'professional'):
        assert any('尚无来源' in line for line in gaps[name]), name
    assert any('可评价的规则' in line for line in gaps['industry'])
    assert report['disclaimer'] and report['nature'] == 'subjective_reference'


def test_material_findings_persist_without_a_number(tmp_path):
    # 「存了 4 份公告」里的 4 是我们手上有几条材料，露出 25.0 这种分数就会被读成对经营的评估。
    repo, service = build(tmp_path)
    company = repo.resolve_company('乙公司')
    add(repo, company, 'disclosure_available', {'count': 4}, 'E-' + 'd' * 24)
    add(repo, company, 'company_age', {'years': 8}, 'E-' + 'c' * 24)
    report = service.recompute('乙公司')
    assert dimension(report, 'operations')['score'] is None
    assert [item['kind'] for item in dimension(report, 'operations')['findings']] == ['material']
    assert dimension(report, 'operations')['findings'][0]['score'] is None
    assert dimension(report, 'operations')['findings'][0]['text']
    assert dimension(report, 'stability')['findings'][0]['kind'] == 'assessment'
    assert dimension(report, 'stability')['findings'][0]['score'] == 80.0
    assert any('尚无来源' in line for line in dimension(report, 'operations')['missing'])


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
    repo, service = build(tmp_path, local_source=False)
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
    repo, service = build(tmp_path, local_source=False)
    company = repo.resolve_company('甲公司')
    evidence = 'E-' + '2' * 24
    add(repo, company, 'salary_quote_coverage', {'known': 9, 'total': 10}, evidence)
    report = service.recompute('甲公司')
    assert report['score'] is None and report['confidence'] == 0
    pay = dimension(report, 'pay')
    assert pay['score'] is None and pay['confidence'] == 0
    assert pay['findings'][0]['text'] == '该公司已保存岗位中 9/10 条能解析出人民币月薪区间，其余报价口径不支持。'
    assert pay['findings'][0]['evidence_id'] == evidence
    assert any('pay_vs_city_median' in line and '尚无来源' in line for line in pay['missing'])
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


def test_recompute_refreshes_local_signals_before_scoring(tmp_path):
    """重新计算是用户的最强直觉动作：存了岗位的公司点它必须直接出分，而不是永远「尚无来源」。"""
    repo, service = build(tmp_path)
    company = repo.resolve_company('丙公司')
    for index in range(3):
        add_job(repo, '丙公司', title=f'后端工程师{index}', salary=f'{15 + index}-{18 + index}K')
    for index in range(20):
        add_job(repo, '同城对照公司', title=f'工程师{index}', salary=f'{20 + index % 5}-{25 + index % 5}K')
    report = service.recompute('丙公司')
    # 本地统计在打分前已刷新：collection 报告来源与样本，薪资维度直接过闸门出分。
    assert report['collection']['status'] == 'completed' and report['collection']['count'] >= 2
    assert report['score'] is not None and dimension(report, 'pay')['score'] is not None
    assert dimension(report, 'professional')['score'] is not None  # 3 条完整 JD 过闸门
    assert '尚无来源' not in ''.join(dimension(report, 'pay')['missing'])


def test_recompute_local_refresh_is_idempotent(tmp_path):
    repo, service = build(tmp_path)
    repo.resolve_company('丙公司')
    add_job(repo, '丙公司')
    first, second = service.recompute('丙公司'), service.recompute('丙公司')
    assert first['collection']['count'] == second['collection']['count']
    assert first['input_hash'] == second['input_hash']


def test_recompute_respects_a_disabled_local_source(tmp_path):
    repo, service = build(tmp_path, local_source=False)
    add_job(repo, '丙公司')
    report = service.recompute('丙公司')
    assert report['collection'] is None
    assert [item['rule'] for item in repo.signals(report['company_id'])] == []


def company_rows(repo):
    with repo.connection() as conn:
        return conn.execute('SELECT count(*) FROM companies').fetchone()[0]


def test_peeking_a_report_leaves_no_company_behind(tmp_path):
    """读入口必须无副作用：查一家没见过的公司不该写出实体。"""
    repo, service = build(tmp_path)
    assert service.peek('甲公司') is None
    assert company_rows(repo) == 0
    built = service.recompute('甲公司')
    assert company_rows(repo) == 1
    assert service.peek('  甲 公司 ')['input_hash'] == built['input_hash']
    assert company_rows(repo) == 1


def test_peeking_a_different_strong_key_finds_nothing_and_creates_nothing(tmp_path):
    repo, service = build(tmp_path)
    service.recompute('甲公司')
    assert service.peek('甲公司', credit_code='91110000MA01ABCD7X') is None
    assert company_rows(repo) == 1
