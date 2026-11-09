import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def connectors():
    assert importlib.util.find_spec('jobscope.connectors'), '接口采集器尚未实现'
    from jobscope import connectors as module
    return module


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_boss_verification_is_not_empty_success():
    module = connectors()
    client = client_for(lambda request: httpx.Response(200, json={'code': 37, 'zpData': {'seed': 'DO_NOT_EXPOSE'}}))
    result = module.collect('boss', 'Python', client=client)
    assert result['status'] == 'needs_verification'
    assert result['jobs'] == []
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)


def test_html_with_200_is_a_failed_collection():
    module = connectors()
    client = client_for(lambda request: httpx.Response(200, text='<html>login secret-token</html>'))
    result = module.collect('boss', 'Python', client=client)
    assert result['status'] == 'failed'
    assert 'secret-token' not in result['message']


def test_remotive_filters_locally_and_reuses_snapshot(tmp_path):
    module = connectors()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'jobs': [
            {'id': 1, 'title': 'Python engineer', 'company_name': 'Acme', 'description': '<p>Build services</p>', 'candidate_required_location': 'Europe', 'url': 'https://remotive.com/remote-jobs/a/1', 'tags': ['Python']},
            {'id': 2, 'title': 'Writer', 'company_name': 'Other', 'description': 'Content', 'candidate_required_location': 'Worldwide', 'url': 'https://remotive.com/remote-jobs/a/2'},
        ]})
    client = client_for(handler)
    first = module.collect('remotive', 'python', 'Europe', client=client, cache_dir=tmp_path)
    second = module.collect('remotive', 'writer', client=client, cache_dir=tmp_path)
    assert first['status'] == 'completed'
    assert [job['title'] for job in first['jobs']] == ['Python engineer']
    assert first['evidence'][0]['kind'] == 'job_posting'
    assert first['evidence'][0]['company'] == 'Acme'
    assert second['jobs'][0]['title'] == 'Writer'
    assert len(calls) == 1
    assert '24' in first['message']


def test_cninfo_posts_form_and_preserves_issuer_identity():
    module = connectors()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'announcements': [
            {'announcementId': '123', 'secName': '<em>比亚迪</em>', 'secCode': '002594', 'announcementTitle': '2025年年度报告', 'announcementTime': 1775001600000, 'adjunctUrl': 'finalpage/2026-04-01/123.PDF'}
        ], 'hasMore': False})
    result = module.collect('cninfo', '比亚迪', client=client_for(handler))
    assert calls[0].method == 'POST'
    assert b'pageNum=1' in calls[0].content
    assert result['status'] == 'completed'
    assert result['jobs'] == []
    evidence = result['evidence'][0]
    assert evidence['company'] == '比亚迪'
    assert evidence['kind'] == 'disclosure_metadata'
    assert evidence['url'] == 'https://static.cninfo.com.cn/finalpage/2026-04-01/123.PDF'
    assert '未读取' in evidence['text']
    assert evidence['published_at'].startswith('2026-04-01')


def test_cninfo_paginates_and_deduplicates():
    module = connectors()
    requests = []
    def handler(request):
        requests.append(request)
        page = len(requests)
        items = [{'announcementId': str(n), 'secName': '甲', 'announcementTitle': str(n), 'adjunctUrl': f'finalpage/{n}.PDF'} for n in ([1, 2] if page == 1 else [2, 3])]
        return httpx.Response(200, json={'announcements': items, 'hasMore': page == 1})
    result = module.collect('cninfo', '甲', limit=3, client=client_for(handler))
    assert len(result['evidence']) == 3
    assert len(requests) == 2


def test_boss_mapping_does_not_merge_company_names():
    module = connectors()
    data = {'code': 0, 'zpData': {'jobList': [{'encryptJobId': 'abc', 'jobName': 'Python工程师', 'brandName': '某品牌', 'salaryDesc': '20-30K·13薪', 'cityName': '北京', 'jobLabels': ['Python'], 'jobExperience': '3-5年'}]}}
    result = module.collect('boss', 'Python', client=client_for(lambda request: httpx.Response(200, json=data)))
    assert result['jobs'][0]['company'] == '某品牌'
    assert '品牌' in result['evidence'][0]['text']


def test_unknown_source_cannot_turn_into_arbitrary_url():
    module = connectors()
    result = module.collect('http://127.0.0.1/private', 'anything')
    assert result['status'] == 'unsupported'


def test_browser_missing_dependency_is_explicit(tmp_path, monkeypatch):
    assert importlib.util.find_spec('jobscope.browser'), '浏览器采集器尚未实现'
    from jobscope import browser
    monkeypatch.setattr(browser, '_has_driver', lambda: False)
    assert browser.status(tmp_path)['installed'] is False
    assert browser.collect('boss', 'python', data_dir=tmp_path)['status'] == 'unsupported'
    assert browser.collect('zhaopin', 'python', data_dir=tmp_path)['status'] == 'unsupported'
    assert browser.collect('nobody', 'python', data_dir=tmp_path)['status'] == 'unsupported'
    assert browser.collect('boss', '')['status'] == 'failed'


def test_rate_limit_is_not_retried():
    module = connectors()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, text='token should not appear')
    output = module.collect('boss', 'python', client=client_for(handler))
    assert output['status'] == 'backoff'
    assert len(calls) == 1


def test_browser_process_lock_prevents_second_owner(tmp_path):
    from jobscope import browser
    with browser.session_lock(tmp_path) as acquired:
        assert acquired
        with browser.session_lock(tmp_path) as second:
            assert second is False


def test_cninfo_metadata_cannot_publish_external_attachment():
    module = connectors()
    item = {'announcementId': '123', 'secName': 'Acme', 'announcementTitle': 'Report', 'adjunctUrl': 'https://evil.test/x.PDF'}
    output = module.collect('cninfo', 'Acme', client=client_for(lambda request: httpx.Response(200, json={'announcements': [item], 'hasMore': False})))
    assert output['evidence'] == []


def test_boss_malformed_payload_is_a_visible_failure():
    module = connectors()
    output = module.collect('boss', 'python', client=client_for(lambda request: httpx.Response(200, json={'code': 0, 'zpData': 'unexpected'})))
    assert output['status'] == 'failed'


def test_cross_origin_redirect_is_not_followed():
    module = connectors()
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/private'})
    output = module.collect('boss', 'python', client=client_for(handler))
    assert output['status'] == 'failed'
    assert len(calls) == 1


def test_remotive_invalid_cached_shape_is_refetched(tmp_path):
    module = connectors()
    (tmp_path / 'remotive-snapshot.json').write_text('{}', encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'jobs': []})
    output = module.collect('remotive', 'python', cache_dir=tmp_path, client=client_for(handler))
    assert output['status'] == 'completed'
    assert calls


def zhaopin_position(**changes):
    position = {'number': 'CCL1212490300J40899799810', 'name': '数据采集分析师',
                'companyName': '江苏成功企业管理有限公司苏州分公司', 'workCity': '北京',
                'cityDistrict': '海淀', 'salary60': '7000-11000元',
                'publishTime': '2026-11-06 16:37:45',
                'positionUrl': 'https://www.zhaopin.com/jobdetail/CCL1212490300J40899799810.htm',
                'jobDescription': '负责数据采标与质量复核；参与模型效果评测。',
                'jobSkillTags': [{'name': 'Python'}, {'name': '数据标注'}],
                'workingExp': '1-3年', 'education': '本科', 'industryName': '人力资源'}
    return {**position, **changes}


def test_zhaopin_ssr_state_maps_real_duty_text():
    module = connectors()
    output = module.map_zhaopin({'positionList': [zhaopin_position()]}, 20, '北京')
    job = output['jobs'][0]
    assert output['status'] == 'completed'
    assert job['external_id'] == 'CCL1212490300J40899799810'
    assert job['location'] == '北京 海淀' and job['salary'] == '7000-11000元'
    assert job['description_status'] == 'complete'
    assert '负责数据采标' in job['description'] and '经验：1-3年' in job['description']
    assert job['tags'] == ['Python', '数据标注']
    assert job['url'] == 'https://www.zhaopin.com/jobdetail/CCL1212490300J40899799810.htm'
    assert job['published_at'].startswith('2026-11-06')
    assert '江苏成功企业管理' in output['evidence'][0]['text']
    assert '法定主体未核验' in output['evidence'][0]['text']


def test_zhaopin_without_duty_text_stays_a_labelled_summary():
    module = connectors()
    output = module.map_zhaopin({'positionList': [zhaopin_position(jobDescription='')]}, 20)
    job = output['jobs'][0]
    assert job['description_status'] == 'listing'
    assert '完整岗位职责尚未读取' in job['description']


def test_zhaopin_missing_list_is_a_visible_failure():
    module = connectors()
    for payload in ({}, {'positionList': 'unexpected'}, None):
        assert module.map_zhaopin(payload, 20)['status'] == 'failed'


def test_zhaopin_drops_foreign_city_and_unsafe_links():
    module = connectors()
    output = module.map_zhaopin({'positionList': [
        zhaopin_position(number='CCL1111111111J11111111111', workCity='上海'),
        zhaopin_position(number='CCL2222222222J22222222222', positionUrl='http://127.0.0.1/private'),
    ]}, 20, '北京')
    assert output['jobs'] == []


def job51_item(**changes):
    item = {'jobId': '174166248', 'jobName': '高级python', 'companyName': '东软集团',
            'fullCompanyName': '东软集团股份有限公司', 'jobAreaString': '杭州·西湖区',
            'provideSalaryString': '1.5-1.9万', 'issueDateString': '2026-11-09 15:24:49',
            'jobHref': 'https://jobs.51job.com/hangzhou-xhq/174166248.html?s=sou_sou_soulb&t=0_0&req=f82a',
            'jobDescribe': '岗位职责：1. 使用 Python 开发服务接口。', 'jobTags': ['python', 'django'],
            'workYearString': '5年及以上', 'degreeString': '本科', 'termStr': '全职',
            'isPromotion': False}
    return {**item, **changes}


def test_job51_maps_search_api_and_prefers_registered_name():
    module = connectors()
    payload = {'status': '1', 'message': '成功', 'resultbody': {'job': {'items': [job51_item()]}}}
    output = module.map_job51(payload, 20, '杭州')
    job = output['jobs'][0]
    assert job['company'] == '东软集团股份有限公司'
    assert job['location'] == '杭州·西湖区' and job['description_status'] == 'complete'
    assert job['detail_transport'] == 'search_api'
    assert job['url'].startswith('https://jobs.51job.com/hangzhou-xhq/174166248.html')
    assert '使用 Python 开发服务接口' in job['description']
    assert output['evidence'][0]['kind'] == 'job_posting'


def test_job51_failure_statuses_are_not_zero_results():
    module = connectors()
    assert module.map_job51({'status': '-1', 'message': '请求异常', 'resultbody': {}}, 20)['status'] == 'failed'
    assert module.map_job51({'status': '1', 'message': '成功', 'resultbody': {'job': {}}}, 20)['status'] == 'failed'


def test_job51_jsonp_wrapped_body_is_parsed():
    module = connectors()
    payload = module.loads_payload('cb_({"status":"1","message":"成功","resultbody":{"job":{"items":[]}}});')
    assert payload['resultbody']['job']['items'] == []
    assert module.loads_payload('<html>login</html>') is None


def test_job51_summary_only_when_no_duty_text():
    module = connectors()
    payload = {'status': '1', 'resultbody': {'job': {'items': [job51_item(jobDescribe='')]}}}
    job = module.map_job51(payload, 20)['jobs'][0]
    assert job['description_status'] == 'listing'


def liepin_card(changes=None, comp=None):
    card = {'jobId': '86374907', 'title': '销售效能主管', 'dq': '北京-昌平区',
            'salary': '15-25k·16薪', 'link': 'https://www.liepin.com/job/1986374907.shtml',
            'refreshTime': '20261027173558', 'labels': ['销售运营'], 'topJob': False,
            'requireWorkYears': '2年以上', 'requireEduLevel': '本科'}
    company = {'compId': 21200669, 'compName': '北大医疗管理有限责任公司北京分公司',
               'compIndustry': '医疗机构', 'link': 'https://www.liepin.com/company/21200669/'}
    return {'job': dict(card, **(changes or {})), 'comp': dict(company, **(comp or {}))}


def liepin_payload(cards, has_next=False):
    return {'flag': 1, 'data': {'data': {'jobCardList': cards},
                                'pagination': {'currentPage': 0, 'hasNext': has_next, 'totalPage': 10}}}


def test_liepin_uses_the_public_search_api_headers():
    module = connectors()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=liepin_payload([liepin_card()]))
    output = module.collect('liepin', 'Python', client=client_for(handler))
    job = output['jobs'][0]
    assert output['status'] == 'completed'
    assert str(calls[0].url) == module.LIEPIN_API
    assert calls[0].headers['X-Fscp-Version'] == '1.1'
    assert calls[0].headers['X-Fscp-Std-Info'] == '{"client_id": "40108"}'
    assert calls[0].headers['X-Fscp-Trace-Id']
    assert 'Cookie' not in calls[0].headers
    assert job['url'] == 'https://www.liepin.com/job/1986374907.shtml'
    assert job['description_status'] == 'listing'
    assert '仅搜索列表字段' in job['description']
    assert job['published_at'].startswith('2026-10-27')


def test_liepin_drops_related_recommendations_from_other_cities():
    module = connectors()
    cards = [liepin_card(), liepin_card({'jobId': '99999999', 'dq': '合肥-蜀山区', 'title': '外地推荐'})]
    output = module.map_liepin(liepin_payload(cards), 20, '北京')
    assert [job['external_id'] for job in output['jobs']] == ['86374907']


def test_liepin_marks_promoted_cards():
    module = connectors()
    output = module.map_liepin(liepin_payload([liepin_card({'topJob': True})]), 20)
    assert output['jobs'][0]['tags'][0] == '推广'


def test_liepin_paginates_until_limit_and_deduplicates():
    module = connectors()
    pages = []
    def handler(request):
        pages.append(json.loads(request.content))
        index = len(pages) - 1
        cards = [liepin_card({'jobId': str(86374907 + n), 'title': f'岗位{n}'}) for n in range(index * 2, index * 2 + 2)]
        cards.append(liepin_card())
        return httpx.Response(200, json=liepin_payload(cards, has_next=index < 2))
    output = module.collect('liepin', 'Python', limit=50, client=client_for(handler))
    ids = [job['external_id'] for job in output['jobs']]
    assert len(ids) == len(set(ids)) == len(output['jobs'])
    assert len(pages) == 3
    assert pages[1]['data']['mainSearchPcConditionForm']['currentPage'] == 1
    assert f'{len(ids)} 条' in output['message']


def test_liepin_city_codes_are_only_the_verified_ones():
    module = connectors()
    missing = module.collect('liepin', 'Python', location='廊坊')
    assert missing['status'] == 'unsupported' and '北京' in missing['message']
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=liepin_payload([]))
    module.collect('liepin', 'Python', location='深圳', client=client_for(handler))
    assert calls[0]['data']['mainSearchPcConditionForm']['dq'] == module.LIEPIN_CITY_CODES['深圳']


def test_liepin_rejected_response_is_not_reported_as_no_results():
    module = connectors()
    assert module.map_liepin({'flag': 0, 'msg': '签名校验失败'}, 20)['status'] == 'failed'
    assert module.map_liepin({'flag': 1, 'data': {}}, 20)['status'] == 'failed'
    output = module.collect('liepin', 'Python', client=client_for(lambda request: httpx.Response(200, json={'flag': 0})))
    assert output['status'] == 'failed' and output['jobs'] == []


def test_registry_matches_the_api_contracts():
    from typing import get_args
    from jobscope import browser, models
    module = connectors()
    allowed = set(get_args(models.CollectInput.model_fields['source'].annotation))
    assert allowed == set(module.SOURCES) - {'user_import'}
    assert set(browser.PAGES) == {'boss', 'zhaopin', '51job'} >= set(module.BROWSER_SOURCES)
    assert set(get_args(models.BrowserCollectInput.model_fields['source'].annotation)) >= set(module.BROWSER_SOURCES)
    assert allowed - set(module.HTTP_SOURCES) == set(module.BROWSER_SOURCES)


def test_browser_only_sources_are_never_collected_over_plain_http():
    module = connectors()
    calls = []
    client = client_for(lambda request: calls.append(request) or httpx.Response(200, json={}))
    for source in module.BROWSER_SOURCES:
        if source == 'boss':
            continue
        output = module.collect(source, 'Python', client=client)
        assert output['status'] == 'unsupported', source
        assert '浏览器' in output['message']
    assert calls == []


def test_registry_cities_cover_the_platform_urls():
    module = connectors()
    assert module.SOURCES['zhaopin']['codes']['北京'] == module.ZHAOPIN_CITY_CODES['北京']
    assert module.SOURCES['51job']['codes']['广州'] == module.JOB51_CITY_CODES['广州']
    for source in ('zhaopin', '51job', 'liepin'):
        assert all(str(code) for code in module.SOURCES[source]['codes'].values())
    assert module.SOURCES['liepin']['transport'] == 'http_api'
    assert module.SOURCES['zhaopin']['transport'] == module.SOURCES['51job']['transport'] == 'browser_page'


def test_boss_uses_the_platform_city_dictionary():
    module = connectors()
    assert len(module.CITY_CODES) > 300, 'BOSS 应带上平台自身的全量城市字典，而不是十来个城市'
    assert re.fullmatch(r'\d{9}', module.CITY_CODES['佛山'])
    # 平台全量字典必须逐条复现已回验的下限，否则码制对不上就是采错城市。
    for name, code in module.VERIFIED_BOSS_CITY_CODES.items():
        assert module.CITY_CODES[name] == code
    hot = module.SOURCES['boss']['hot']
    # 全国是平台字典里的伪城市，没有拼音条目；其余热门城市都要能按拼音命中。
    assert set(hot) <= set(module.CITY_CODES) and all(name in module.CITY_PINYIN for name in hot if name != '全国')
    assert len(module.CITY_PINYIN) > 300
    assert module.CITY_PINYIN['佛山'] == ['foshan', 'fs']


def test_unmapped_city_lists_hot_cities_instead_of_the_whole_dictionary():
    module = connectors()
    calls = []
    client = client_for(lambda request: calls.append(request) or httpx.Response(200, json={'code': 0, 'zpData': {'jobList': []}}))
    output = module.collect('boss', 'Python', '不存在的城市', client=client)
    assert output['status'] == 'unsupported' and calls == []
    assert re.search(r'等 \d+ 个城市', output['message']) and len(output['message']) < 120
