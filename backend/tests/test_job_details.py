import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.repository import Repository


def listing(repo, external_id='abc123'):
    return repo.save_job(dict(external_id=external_id, title='开发', company='甲品牌',
                              description='Python\n仅搜索列表字段；完整岗位职责尚未读取。',
                              url=f'https://www.zhipin.com/job_detail/{external_id}.html'), 'boss')


def snapshot(job, **changes):
    return dict(url=job['url'], title='开发工程师', salary='20-30K',
                description='岗位职责\n负责 Python 服务开发和数据平台建设。\n任职要求\n熟悉 SQL，具备3年以上开发经验。',
                company='甲品牌', conditions='杭州 3-5年 本科',
                company_profile='公司自述：团队20人。', company_overview='甲品牌\n20-99人',
                **changes)


def test_detail_normalization_and_identity(tmp_path):
    from jobscope.job_details import normalize_detail
    job = listing(Repository(tmp_path))
    output = normalize_detail(job, snapshot(job))
    assert output['status'] == 'completed'
    assert output['job']['description_status'] == 'complete'
    assert '任职要求' in output['job']['description']
    assert '杭州 3-5年 本科' in output['job']['description']
    assert output['job']['company'] == '甲品牌'
    assert output['company_evidence']['kind'] == 'company_statement'
    assert normalize_detail(job, {**snapshot(job), 'url': 'https://www.zhipin.com/job_detail/other.html'})['status'] == 'failed'
    assert normalize_detail(job, {**snapshot(job), 'url': 'https://www.zhipin.com/web/user/safe/verify'})['status'] == 'needs_verification'
    assert normalize_detail(job, {**snapshot(job), 'description': ''})['status'] != 'completed'


def test_detail_save_retains_immutable_summary_and_resists_downgrade(tmp_path):
    from jobscope.job_details import normalize_detail, save_detail
    repo = Repository(tmp_path)
    job = listing(repo)
    output = normalize_detail(job, snapshot(job))
    saved = save_detail(repo, output)
    assert saved['id'] == job['id']
    assert saved['detail_collected_at']
    assert len(repo.job_evidence(job['id'])) >= 2
    assert any('仅搜索列表' in e['text'] for e in repo.job_evidence(job['id']))
    assert any('任职要求' in e['text'] for e in repo.job_evidence(job['id']))
    assert repo.evidence(company=job['company'], include_job_postings=False)[0]['kind'] == 'company_statement'
    refreshed = listing(repo)
    assert refreshed['description_status'] == 'complete'
    assert '任职要求' in refreshed['description']
    assert refreshed['detail_collected_at'] == saved['detail_collected_at']


def test_incomplete_boss_cannot_be_analyzed(tmp_path):
    from jobscope.main import create_app
    with TestClient(create_app(tmp_path)) as client:
        job = listing(client.app.state.repo)
        client.put('/api/resume', json={'text': 'Python SQL', 'format': 'text'})
        response = client.post('/api/analyze', json={'job_id': job['id']})
        assert response.status_code == 409
        assert '完整' in response.json()['detail']


def test_single_detail_endpoint_uses_saved_job_and_evidence(tmp_path, monkeypatch):
    from jobscope import job_details
    from jobscope.main import create_app
    with TestClient(create_app(tmp_path)) as client:
        job = listing(client.app.state.repo)
        monkeypatch.setattr(job_details, 'collect_detail', lambda target, data_dir: job_details.normalize_detail(target, snapshot(target)))
        result = client.post(f"/api/jobs/{job['id']}/detail")
        assert result.status_code == 200
        assert result.json()['job']['description_status'] == 'complete'
        assert result.json()['task']['status'] == 'completed'
        assert client.post('/api/jobs/not-found/detail').status_code == 404


def test_batch_skips_complete_saves_progress_and_stops_at_verification(tmp_path):
    from jobscope.job_details import normalize_detail, run_batch, save_detail
    repo = Repository(tmp_path)
    completed = listing(repo, 'done')
    save_detail(repo, normalize_detail(completed, snapshot(completed)))
    for identifier in ['pending1', 'pending2', 'pending3']:
        listing(repo, identifier)
    task = repo.create_task('boss', '完整JD补采')
    calls = []

    def collect(job, data_dir):
        calls.append(job['id'])
        if len(calls) == 2:
            return {'status': 'needs_verification', 'message': '请人工验证'}
        return normalize_detail(job, snapshot(job))

    run_batch(repo, task, limit=100, collector=collect, pause=lambda _: None)
    result = next(t for t in repo.tasks() if t['id'] == task['id'])
    assert completed['id'] not in calls
    assert len(calls) == 2
    assert result['status'] == 'needs_verification'
    assert result['count'] == 1
    assert result['total'] == 3
    assert result['processed'] == 2
    assert sum(j.get('description_status') == 'complete' for j in repo.jobs()) == 2


@pytest.mark.parametrize('url', ['http://127.0.0.1/private', 'https://evil.test/job_detail/abc.html',
                                  'https://www.zhipin.com@evil.test/job_detail/abc.html'])
def test_detail_url_is_only_saved_boss_job(tmp_path, url):
    from jobscope.job_details import valid_detail_url
    assert not valid_detail_url({**listing(Repository(tmp_path)), 'url': url})


def zhaopin_listing(repo, external_id='CCL1212490300J40899799810'):
    return repo.save_job(dict(external_id=external_id, title='数据采集分析师', company='甲品牌苏州分公司',
                              location='北京 海淀', salary='7000-11000元',
                              description='经验：1-3年\n学历：本科\n\n负责数据采标与质量复核。\n以上为搜索页摘要，来源已截断；完整岗位职责尚未读取。',
                              description_status='listing',
                              url=f'https://www.zhaopin.com/jobdetail/{external_id}.htm'), 'zhaopin')


def zhaopin_snapshot(job, **changes):
    snapshot = dict(url=job['url'], title='数据采集分析师', salary='7000-11000元',
                    description='1、负责数据采集和整理，推动问题及时合理解决;\n2、负责数据采集工作，之前有传感器采集经验最好;\n3、有python等脚本编程能力者优先。',
                    raw_description='', working_exp='1-3年', education='本科', company='甲品牌苏州分公司',
                    page_title='数据采集分析师招聘 - 智联招聘')
    return {**snapshot, **changes}


def test_zhaopin_detail_normalization_replaces_truncated_teaser(tmp_path):
    from jobscope.job_details import normalize_detail
    repo = Repository(tmp_path)
    job = zhaopin_listing(repo)
    output = normalize_detail(job, zhaopin_snapshot(job))
    assert output['status'] == 'completed'
    assert output['job']['description_status'] == 'complete'
    assert output['job']['detail_transport'] == 'browser_dom'
    assert output['job']['description'].startswith('经验：1-3年\n学历：本科\n\n1、负责数据采集和整理')
    assert '搜索页摘要' not in output['job']['description']
    assert output['company_evidence'] is None
    # 详情页状态缺失时保留原摘要，不误标完整。
    assert normalize_detail(job, zhaopin_snapshot(job, description=''))['status'] == 'unavailable'
    # raw_description 兜底：description 为空但 jobDesc HTML 存在时清洗后可用。
    fallback = normalize_detail(job, zhaopin_snapshot(job, description='',
                                                     raw_description='1、负责数据采集和整理，推动问题及时合理解决;\n2、负责数据采集工作，之前有传感器采集经验最好;\n3、有python等脚本编程能力者优先。'))
    assert fallback['status'] == 'completed'
    assert '1、负责数据采集和整理' in fallback['job']['description']


def test_zhaopin_detail_identity_and_verification(tmp_path):
    from jobscope.job_details import normalize_detail
    job = zhaopin_listing(Repository(tmp_path))
    other = zhaopin_snapshot(job, url='https://www.zhaopin.com/jobdetail/CCL9999999999J99999999999.htm')
    assert normalize_detail(job, other)['status'] == 'failed'
    assert normalize_detail(job, zhaopin_snapshot(job, url='https://www.zhaopin.com/safe/verify'))['status'] == 'needs_verification'
    assert normalize_detail(job, zhaopin_snapshot(job, page_title='Security Verification'))['status'] == 'needs_verification'
    assert normalize_detail(job, zhaopin_snapshot(job, url='http://www.zhaopin.com/jobdetail/CCL1212490300J40899799810.htm'))['status'] == 'failed'


def test_zhaopin_detail_saves_and_backfills_legacy_teaser(tmp_path):
    from jobscope.job_details import normalize_detail, pending_jobs, save_detail
    repo = Repository(tmp_path)
    job = zhaopin_listing(repo)
    # 旧版把截断摘要误标为 complete（detail_transport='page_state'），仍要被批量补采捞起。
    legacy = dict(job, description_status='complete', detail_transport='page_state')
    repo.save_job(legacy, 'zhaopin')
    assert [j['id'] for j in pending_jobs(repo)] == [job['id']]
    saved = save_detail(repo, normalize_detail(repo.job(job['id']), zhaopin_snapshot(job)))
    assert saved['description_status'] == 'complete' and saved['detail_transport'] == 'browser_dom'
    assert '1、负责数据采集和整理' in saved['description']
    assert pending_jobs(repo) == []
    # 完成后再次采集摘要不会降级已保存的完整正文。
    zhaopin_listing(repo)
    refreshed = repo.job(job['id'])
    assert refreshed['description_status'] == 'complete'
    assert '1、负责数据采集和整理' in refreshed['description']
