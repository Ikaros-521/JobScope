"""平台投递记录回填（Repository.sync_applied）：状态合并规则、事件回溯与漏斗口径。

2026-11-23 Boss 投递记录同步的落库出口。全程 tmp_path 隔离数据库，不碰浏览器。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.repository import Repository, digest, iso_week_start

import pytest

APPLIED = '2026-11-20T11:15:03+00:00'  # 2026-11-20 19:15 北京时间（周五）


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


def make_job(repo, external_id, status=None):
    job = repo.save_job(dict(title=f'岗位 {external_id}', company='甲公司', description='岗位原文',
                             external_id=external_id), 'boss')
    if status is not None:
        repo.update_jobs([job['id']], {'pipeline_status': status})
    return repo.job(job['id'])


def events_of(repo, job_id):
    with repo.connection() as conn:
        return [dict(row) for row in conn.execute(
            'SELECT from_status,to_status,created_at FROM job_pipeline_events WHERE job_id=? ORDER BY id', (job_id,))]


def test_plain_and_interested_jobs_become_applied_with_real_time(repo):
    plain = make_job(repo, 'syncPlain001')
    interested = make_job(repo, 'syncInterest1', status='interested')
    outcome = repo.sync_applied([(plain['id'], APPLIED), (interested['id'], APPLIED)])
    assert outcome == {'changed': 2, 'kept': 0, 'archived': 0, 'events': 2}
    for job_id in (plain['id'], interested['id']):
        job = repo.job(job_id)
        assert job['pipeline_status'] == 'applied' and job['starred'] is True
        assert job['applied_at'] == APPLIED and job['applied_source'] == 'boss'
        # 跟进提醒按真实投递日算：状态变化时间是 Boss 记录的投递时刻，不是回填当天。
        assert job['pipeline_status_changed_at'] == APPLIED
        assert job['description'] == '岗位原文'  # 状态回填不覆盖档案正文
    assert [(e['from_status'], e['to_status'], e['created_at']) for e in events_of(repo, plain['id'])] == [
        ('', 'applied', APPLIED)]
    # 设「意向」时已落一条事件；投递事件排在末尾，from_status 承接上一状态。
    assert [e['from_status'] for e in events_of(repo, interested['id'])] == ['', 'interested']


def test_later_stages_keep_status_but_get_backfilled_event(repo):
    # 平台数据只到「发了简历」：用户手工推进的面试/Offer 与主动归档不能被拉回已投递。
    interviewing = make_job(repo, 'syncInterview1', status='interviewing')
    offer = make_job(repo, 'syncOffer0001', status='offer')
    archived = make_job(repo, 'syncArchived01', status='archived')
    outcome = repo.sync_applied([(job['id'], APPLIED) for job in (interviewing, offer, archived)])
    assert outcome == {'changed': 0, 'kept': 2, 'archived': 1, 'events': 3}
    assert repo.job(interviewing['id'])['pipeline_status'] == 'interviewing'
    assert repo.job(offer['id'])['pipeline_status'] == 'offer'
    assert repo.job(archived['id'])['pipeline_status'] == 'archived'
    for job in (interviewing, offer, archived):
        # 补一条回溯的「已投递」事件：漏斗分母（曾到达已投递）与推进周期才算得到这批岗位。
        applied_events = [e for e in events_of(repo, job['id']) if e['to_status'] == 'applied']
        assert [(e['from_status'], e['created_at']) for e in applied_events] == [('', APPLIED)]
    # 推进周期：投递（11-20）→ 约面（手工标记的当天）可以算出样本。
    steps = {(step['from'], step['to']): step for step in repo.pipeline_funnel()['steps']}
    assert steps[('applied', 'interviewing')]['cycle_n'] == 1


def test_existing_applied_event_aligns_to_platform_time_and_stays_idempotent(repo):
    # 用户 11-22 手工标的「已投递」，平台记录实际 11-20 发的简历：首条事件对齐平台时刻。
    job = make_job(repo, 'syncManual001', status='applied')
    assert events_of(repo, job['id'])[0]['created_at'] != APPLIED
    first = repo.sync_applied([(job['id'], APPLIED)])
    assert first == {'changed': 0, 'kept': 1, 'archived': 0, 'events': 1}
    assert [(e['from_status'], e['created_at']) for e in events_of(repo, job['id'])] == [('', APPLIED)]
    assert repo.job(job['id'])['pipeline_status_changed_at'] != APPLIED  # 已是已投递：状态位不动
    again = repo.sync_applied([(job['id'], APPLIED)])
    assert again['events'] == 0 and len(events_of(repo, job['id'])) == 1
    # 平台没给时刻（DOM 兜底通道）：不拿「现在」去改写已有事件。
    third = repo.sync_applied([(job['id'], '')])
    assert third == {'changed': 0, 'kept': 1, 'archived': 0, 'events': 0}
    assert events_of(repo, job['id'])[0]['created_at'] == APPLIED


def test_unknown_time_and_unknown_jobs_are_safe(repo):
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    job = make_job(repo, 'syncNoTime001')
    # 不存在的岗位静默跳过；非法/未来时刻按现在记，且不拿「现在」去改写已有事件。
    outcome = repo.sync_applied([('J-missing0000000000000000', APPLIED), (job['id'], 'not-a-date'),
                                 (job['id'], '2099-01-01T00:00:00+00:00')])
    assert outcome == {'changed': 1, 'kept': 1, 'archived': 0, 'events': 1}
    assert datetime.fromisoformat(repo.job(job['id'])['applied_at']) >= before
    assert events_of(repo, 'J-missing0000000000000000') == []


def test_funnel_uses_backfilled_applied_week(repo):
    # 漏斗的投递批次/周几按事件时间算：回填后落进真实投递周，而不是回填当周。
    job = make_job(repo, 'syncFunnel001')
    repo.sync_applied([(job['id'], APPLIED)])
    funnel = repo.pipeline_funnel()
    assert funnel['cohorts'] == [{'week_start': iso_week_start(APPLIED).date().isoformat(), 'applied': 1,
                                  'interviewing': 0, 'offer': 0, 'active': 1}]
    assert [row['weekday'] for row in funnel['by_weekday'] if row['applied'] == 1] == [5]


def test_followup_fields_survive_sync(repo):
    job = make_job(repo, 'syncTrack0001')
    repo.update_jobs([job['id']], {'pipeline_track': 'shift', 'pipeline_note': '走内推', 'pipeline_due': '2026-12-01'})
    repo.sync_applied([(job['id'], APPLIED)])
    job = repo.job(job['id'])
    assert job['pipeline_track'] == 'shift' and job['pipeline_note'] == '走内推' and job['pipeline_due'] == '2026-12-01'


def test_resave_keeps_track_and_applied_provenance(repo):
    # 重新采集是整包覆盖：求职线标记与平台投递溯源都是用户意图/既成事实，必须随收藏、管线状态一起幸存，
    # 否则一次日常刷新就把「转型线」静默抹掉、把投递时间打回未知。
    job_id = 'J-' + digest('boss:resaveJob001')[:24]  # save_job 同口径的主键
    repo.save_job(dict(title='重采集岗', company='甲公司', description='旧原文', external_id='resaveJob001'), 'boss')
    repo.update_jobs([job_id], {'pipeline_track': 'shift'})
    repo.sync_applied([(job_id, APPLIED)])
    again = repo.save_job(dict(title='重采集岗', company='甲公司', description='新原文', external_id='resaveJob001'), 'boss')
    assert again['id'] == job_id and again['description'] == '新原文'  # 采集数据照常刷新
    assert again['pipeline_track'] == 'shift'
    assert again['applied_at'] == APPLIED and again['applied_source'] == 'boss'


def test_mark_duplicates_carries_track_and_applied_provenance(repo):
    # 合并重复项：重复项上的求职线与投递溯源同样「主记录缺什么就上提什么」，不能随重复项一起被清掉。
    # 手工岗位没有 external_id，主键取 title+company+location——两条必须给不同 url 才是两条独立档案。
    primary = repo.save_job(dict(title='主记录岗', company='甲公司', description='原文', url='https://example.test/a'), 'user_import')
    dup = repo.save_job(dict(title='主记录岗', company='甲公司', description='原文（转载）', url='https://example.test/b'), 'user_import')
    repo.update_jobs([dup['id']], {'pipeline_status': 'applied', 'pipeline_track': 'shift'})
    repo.sync_applied([(dup['id'], APPLIED)])
    outcome = repo.mark_duplicates(primary['id'], [dup['id']])
    assert outcome['primary']['pipeline_track'] == 'shift'
    assert outcome['primary']['applied_at'] == APPLIED and outcome['primary']['applied_source'] == 'boss'
    assert repo.job(dup['id'])['duplicate_of'] == primary['id']
