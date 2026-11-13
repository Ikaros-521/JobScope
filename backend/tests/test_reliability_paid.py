"""Paid-source collection driven by a fixed JSON payload: no socket, no vendor account.

这些测试喂给 `paid.collect()` 的字段名全部取自 `sources.SIGNAL_SOURCES` 里声明的候选映射——
那份映射本身是「等用户按自己采购的接口文档校正」的假设，不是任何服务商真实 schema 的转述。
测试锁的是机制：字段对得上就产出、对不上就点名缺什么，绝不补默认值。
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.reliability import collectors, paid
from jobscope.reliability.rubric import RULES

ENDPOINT = 'https://api.vendor.example/v1/company'
KEY = 'sk-paid-abcdef-123456'

# 一份「映射里所有字段都齐全」的假响应。外层刻意套 data/result，验证取值不依赖厂商的嵌套结构。
# 字段名全部取自 sources.SIGNAL_SOURCES['qcc']['fields'] 里的第一候选，不是任何真实厂商的 schema。
FULL = {'data': {'result': {
    'establish_date': '2015-04-01',
    'insured_num': 240,
    'insured_num_last': 312,
    'case_num': 3,
    'avg_salary': 20000,
    'city_avg_salary': 16000,
    'salary_sample_num': 5,
    'city_job_num': 40,
    'revenue_current': 120000000,
    'revenue_previous': 100000000,
    'employee_num': 240,
    'employee_num_previous': 260,
}}}
# `None` 本身是要测的一种响应（不是对象），所以「没传响应」得用另一个哨兵值。
MISSING = object()


class FakeClient:
    """只给 `paid.collect()` 真正用到的东西：接口地址、密钥、fetch。假 client 不碰 httpx。"""

    def __init__(self, payload=MISSING, error=None, *, url=ENDPOINT, key=KEY):
        self.payload = FULL if payload is MISSING else payload
        self.error = error
        self.url = url
        self.key = key
        self.source_id = 'qcc'
        self.calls = []

    def fetch(self, company):
        self.calls.append(company)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeTab:
    """collectors 的假页面，只用来对拍「日期 → 年数」的同一把尺子。"""

    def __init__(self, html):
        self._html = html

    def get(self, url, timeout=None):
        return True

    @property
    def html(self):
        return self._html


def run(payload=MISSING, *, error=None, client=None, source='qcc', company='甲公司'):
    used = client if client is not None else FakeClient(payload, error)
    outcome = paid.collect(source, company, client=used)
    return outcome, {item['rule']: item for item in outcome['signals']}


def test_a_response_with_every_mapped_field_yields_all_mapped_rules():
    outcome, by_rule = run()
    assert outcome['status'] == 'completed'
    assert set(by_rule) == {'company_age', 'insured_headcount_trend', 'judicial_case_ratio',
                            'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'}
    expected = collectors.years_since(datetime(2015, 4, 1, tzinfo=timezone.utc))
    assert by_rule['company_age']['value'] == {'years': expected}
    assert by_rule['insured_headcount_trend']['value'] == {'before': 312, 'after': 240}
    # 件/万人：3 件 / 240 人 × 10000。分子分母都是声明过的字段，不是挑出来的数字。
    assert by_rule['judicial_case_ratio']['value'] == {'ratio': 125.0}
    # P1 三条新规则的取值：槽位全对上才产出，比值由代码换算、两端数字不换算不补单位。
    assert by_rule['pay_vs_city_median']['value'] == {
        'company_median': 20000, 'city_median': 16000, 'ratio': 1.25, 'company_jobs': 5, 'city_jobs': 40}
    assert by_rule['disclosure_revenue_trend']['value'] == {'current': 120000000, 'previous': 100000000}
    assert by_rule['disclosure_headcount_trend']['value'] == {'current': 240, 'previous': 260}


def test_paid_signals_carry_the_rubric_unit_and_a_traceable_source():
    _outcome, by_rule = run()
    for rule, item in by_rule.items():
        spec = RULES[rule]
        # 维度与单位一律取自 rubric，本模块不许自己写一份。
        assert item['dimension'] == spec.dimension and item['unit'] == spec.unit
        assert item['source_class'] == 'paid_api'
        assert item['url'] == ENDPOINT and item['source'] == 'qcc'
        assert item['collected_at']
        assert set(item) >= {'rule', 'dimension', 'value', 'unit', 'source_class',
                             'observed_at', 'url', 'collected_at', 'text'}


def test_a_missing_field_drops_only_its_rule_and_names_the_candidates():
    payload = {'establish_date': '2015-04-01', 'insured_num': 240}
    outcome, by_rule = run(payload)
    assert set(by_rule) == {'company_age'}
    assert outcome['status'] == 'partial'
    assert '未取到 insured_headcount_trend' in outcome['message']
    assert '未取到 judicial_case_ratio' in outcome['message']
    # 缺哪个字段要说得出候选名，用户才知道映射该改哪一行。
    assert 'insured_num_last' in outcome['message'] and 'case_num' in outcome['message']


def test_an_absent_field_is_never_replaced_by_a_default_or_a_zero():
    payload = {'establish_date': '2015-04-01', 'insured_num': None, 'insured_num_last': '', 'case_num': '未知'}
    outcome, by_rule = run(payload)
    assert list(by_rule) == ['company_age']
    for item in outcome['signals']:
        assert all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in item['value'].values())
        assert None not in item['value'].values() and '' not in item['value'].values()
    assert '未取到 insured_headcount_trend' in outcome['message']
    assert '未取到 judicial_case_ratio' in outcome['message']


def test_the_first_declared_candidate_name_wins():
    names = paid.candidate_names('qcc', 'company_age', 'founded_date')
    assert len(names) >= 2
    _outcome, by_rule = run({names[0]: '2001-01-01', names[1]: '1990-06-06'})
    assert by_rule['company_age']['value'] == {
        'years': collectors.years_since(datetime(2001, 1, 1, tzinfo=timezone.utc))}
    assert by_rule['company_age']['observed_at'] == '2001-01-01'


def test_the_only_present_candidate_is_used_and_a_date_string_becomes_years():
    names = paid.candidate_names('qcc', 'company_age', 'founded_date')
    _outcome, by_rule = run({names[-1]: '2015年04月01日'})
    page = collectors._read_registration(FakeTab('<td>成立日期 2015-04-01</td>'), '甲公司')
    # 同一份日期在浏览器采集与付费接口两条路径上必须换算成同一个年数：只有一处算法。
    assert by_rule['company_age']['value']['years'] == page['value']['years']
    assert by_rule['company_age']['observed_at'] == '2015-04-01'


def test_a_computed_ratio_states_its_numerator_and_denominator():
    outcome, _by_rule = run()
    assert 'judicial_case_ratio' in outcome['message']
    assert '分子' in outcome['message'] and '分母' in outcome['message']
    assert 'case_num' in outcome['message'] and 'insured_num' in outcome['message']
    assert '件/万人' in outcome['message']


def test_a_direct_ratio_field_is_labelled_as_a_declared_unit_claim():
    ratio_name = paid.candidate_names('qcc', 'judicial_case_ratio', 'ratio')[0]
    outcome, by_rule = run({**FULL, ratio_name: 12.5})
    assert by_rule['judicial_case_ratio']['value'] == {'ratio': 12.5}
    assert ratio_name in outcome['message'] and '件/万人' in outcome['message']
    # 直接取比率字段时没有分子分母可报，硬写换算口径会变成第二套说法。
    assert '分子' not in outcome['message']


def test_a_field_name_that_maps_to_two_different_values_is_not_resolved_by_guessing():
    outcome, by_rule = run({'insured_num': [240, 312], 'insured_num_last': 312})
    assert 'insured_headcount_trend' not in by_rule
    assert 'insured_num' in outcome['message'] and '无法确定' in outcome['message']


def test_values_are_read_from_nested_lists_and_deeper_objects():
    payload = {'items': [{'child': {'establish_time': '2010-03-05'}}], 'result': {'rows': [{'case_num': 7}]}}
    outcome, by_rule = run(payload)
    assert by_rule['company_age']['observed_at'] == '2010-03-05'
    # 只有涉诉数量、没有参保人数时比率算不出来，不能拿别的数字顶上。
    assert 'judicial_case_ratio' not in by_rule and '未取到 judicial_case_ratio' in outcome['message']


def test_a_response_that_is_not_an_object_is_unsupported():
    for payload in (['a', 'b'], 'not a json object', 42, None):
        client = FakeClient(payload=payload)
        outcome, by_rule = run(client=client)
        assert outcome['status'] == 'unsupported', payload
        assert by_rule == {}
        assert client.calls == ['甲公司']


def test_an_object_whose_fields_simply_do_not_match_is_partial():
    outcome, by_rule = run({'data': {'result': {'name': '甲公司', 'legal_person': '张某'}}})
    assert outcome['status'] == 'partial' and by_rule == {}
    assert '未取到 company_age' in outcome['message']
    # 有响应、有内容，但字段对不上：这跟「根本不是对象」不是一回事，也不能被读成公司没有这些数据。
    assert '映射' in outcome['message']


def test_a_fetch_that_raises_is_reported_and_never_retried():
    client = FakeClient(error=ValueError('付费来源未填写接口地址'))
    outcome, by_rule = run(client=client)
    assert outcome['status'] == 'failed' and by_rule == {}
    assert '付费来源未填写接口地址' in outcome['message']
    assert client.calls == ['甲公司']


def test_a_vendor_error_response_fails_loudly_instead_of_inventing_a_signal():
    """服务商要求签名或换认证方式时会直接 4xx：这类失败必须原样报出来，一条信号都不许造。"""
    request = httpx.Request('GET', ENDPOINT)
    error = httpx.HTTPStatusError('Client error 401', request=request, response=httpx.Response(401, request=request))
    outcome, by_rule = run(client=FakeClient(error=error))
    assert outcome['status'] == 'failed' and by_rule == {}
    assert '401' in outcome['message']


def test_the_key_never_reaches_the_signals_or_the_message():
    client = FakeClient(url=f'https://api.vendor.example/v1?token={KEY}')
    outcome, by_rule = run(client=client)
    assert outcome['status'] == 'failed' and by_rule == {}
    assert KEY not in repr(outcome)
    assert '密钥' in outcome['message']


def test_a_clean_collection_calls_the_vendor_exactly_once():
    client = FakeClient()
    run(client=client)
    assert client.calls == ['甲公司']


def test_a_source_without_a_declared_mapping_is_unsupported():
    outcome, by_rule = run(source='gsxt')
    assert outcome['status'] == 'unsupported' and by_rule == {}
    assert '付费' in outcome['message']


def test_a_missing_client_is_reported_instead_of_raising():
    outcome = paid.collect('qcc', '甲公司', client=None)
    assert outcome['status'] == 'unsupported' and outcome['signals'] == []
    assert '密钥' in outcome['message']


def test_the_signal_shape_feeds_the_stored_signal_contract():
    from jobscope.reliability.models import SignalInput
    _outcome, by_rule = run()
    for item in by_rule.values():
        signal = SignalInput(company_id='CO-0123456789abcdef', evidence_id='E-1',
                             **{key: item[key] for key in
                                ('rule', 'dimension', 'value', 'unit', 'source_class',
                                 'observed_at', 'collected_at')})
        assert signal.source_class == 'paid_api'


def test_the_evidence_text_is_stable_for_the_same_payload():
    """正文里不许出现计算时刻：证据身份含内容哈希，带时间会让每次点击多生一份证据。"""
    first, _ = run()
    second, _ = run()
    assert [item['text'] for item in first['signals']] == [item['text'] for item in second['signals']]
    for item in first['signals']:
        assert not re.search(r'\d{2}:\d{2}:\d{2}', item['text'])


def test_the_statuses_stay_inside_the_collectors_vocabulary():
    allowed = {'completed', 'partial', 'needs_login', 'needs_verification', 'backoff', 'unsupported', 'failed'}
    outcomes = [run(client=FakeClient(payload=payload))[0] for payload in (FULL, {'x': 1}, [], 'text', None)]
    outcomes.append(paid.collect('qcc', '甲公司', client=None))
    assert all(item['status'] in allowed for item in outcomes)


def test_client_configuration_overrides_mapping_without_guessing_vendor_fields():
    import copy
    from jobscope.reliability.sources import SIGNAL_SOURCES
    fields = copy.deepcopy(SIGNAL_SOURCES['qcc']['fields'])
    fields['company_age'][0]['founded_date'] = ['incorporated_on']
    client = FakeClient(payload={'incorporated_on': '2015-04-01'})
    client.config = {'fields': fields}
    outcome, by_rule = run(client=client)
    assert outcome['status'] == 'partial' and set(by_rule) == {'company_age'}
    assert 'incorporated_on' in by_rule['company_age']['text']
    client.payload = {'establish_date': '2015-04-01'}
    _, by_rule = run(client=client)
    assert 'company_age' not in by_rule  # configured fields replace, rather than supplement, defaults


def test_paid_client_sends_custom_auth_header_prefix_and_company_param(monkeypatch):
    from jobscope.reliability.sources import PaidClient
    requests = []
    original_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'incorporated_on': '2015-04-01'})

    def client_factory(**kwargs):
        assert kwargs == dict(timeout=20, follow_redirects=False, trust_env=False)
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, 'Client', client_factory)
    client = PaidClient('qcc', dict(url='http://localhost:9500/company?format=json', key=KEY,
                       auth_header='X-Api-Key', auth_prefix='', company_param='keyword'))
    assert client.fetch('甲公司') == {'incorporated_on': '2015-04-01'}
    request = requests.pop()
    assert request.headers['X-Api-Key'] == KEY and 'Authorization' not in request.headers
    assert dict(request.url.params) == {'format': 'json', 'keyword': '甲公司'}
    assert KEY not in str(request.url) and KEY not in repr(client)
    default = PaidClient('qcc', dict(url=ENDPOINT, key=KEY))
    default.fetch('乙公司')
    request = requests.pop()
    assert request.headers['Authorization'] == 'Bearer ' + KEY
    assert dict(request.url.params) == {'company': '乙公司'}


def test_unexpected_paid_errors_never_echo_headers_keys_or_signed_urls():
    error = RuntimeError(f'Authorization: Bearer {KEY}; gateway returned https://vendor.example/?secret=other-secret')
    client = FakeClient(error=error)
    outcome, signals = run(client=client)
    assert outcome['status'] == 'failed' and signals == {}
    assert KEY not in str(outcome) and 'other-secret' not in str(outcome)
    assert client.calls == ['甲公司']
