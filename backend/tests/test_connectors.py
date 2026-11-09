import importlib.util
import json
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
    assert browser.collect('python', data_dir=tmp_path)['status'] == 'unsupported'


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
