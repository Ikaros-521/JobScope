"""失败博物馆：分类学不变量 / 标注校验 / 跨场模式报告 / API 往返与级联（纯规则，无 LLM）。"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import failure_museum as FM
from jobscope.interview import ADVANCE_TOOL
from jobscope.interview_runs import new_session
from jobscope.main import create_app
from jobscope.repository import Repository
from jobscope.transcript_runs import new_transcript


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as value:
        yield value


def _weakness_enum():
    return ADVANCE_TOOL['parameters']['properties']['weakness']['description'].split('取值：')[1].split('/')


# ── 分类学不变量：标签集是产品语义的底座，逐条断言。 ──

def test_taxonomy_shape():
    ids = [t['id'] for t in FM.CONTROLLED_TAGS + FM.UNCONTROLLED_TAGS]
    assert len(ids) == len(set(ids)) == 15
    for tag in FM.CONTROLLED_TAGS:
        assert tag['label'] and tag['hint'] and tag['advice'], f'可控标签必须给建议：{tag["id"]}'
    for tag in FM.UNCONTROLLED_TAGS:
        assert tag['label'] and tag['hint'], f'不可控标签必须给定位文案：{tag["id"]}'
        assert 'advice' not in tag, '不可控因素不给建议（定位而非改造）'


def test_weakness_map_covers_enum_and_targets_controlled():
    enum_values = _weakness_enum()
    assert set(FM.WEAKNESS_TO_TAG) == set(enum_values), '面试官暗线弱点信号必须全部有归因出口'
    assert set(FM.WEAKNESS_TO_TAG.values()) <= FM.CONTROLLED_IDS, '弱点信号只映射到可控标签'


# ── normalize_tags：校验、去重、备注截断。 ──

def test_normalize_tags_validates_and_dedups():
    tags = FM.normalize_tags([
        dict(scope='controlled', tag='structure', note='STAR 讲崩了'),
        dict(scope='controlled', tag='structure', note=''),          # 同 tag 去重
        dict(tag='interviewer_rush'),                                # scope 缺省按分类学推断
        dict(scope='uncontrolled', tag='budget_hc', note='x' * 500), # 备注截断
    ])
    assert [t['tag'] for t in tags] == ['structure', 'interviewer_rush', 'budget_hc']
    assert tags[0]['note'] == 'STAR 讲崩了'
    assert len(tags[2]['note']) == FM.MAX_NOTE


@pytest.mark.parametrize('tags', [
    [],
    [dict(scope='controlled', tag='not_exist')],
    [dict(scope='uncontrolled', tag='structure')],   # 分组与标签不匹配
    [dict(scope='side', tag='structure')],
    [dict(scope='controlled', tag=t['id']) for t in FM.CONTROLLED_TAGS + FM.UNCONTROLLED_TAGS],  # 超上限
])
def test_normalize_tags_rejects_bad_payloads(tags):
    with pytest.raises(ValueError):
        FM.normalize_tags(tags)


# ── 跨场模式报告：重复失分点、结果对比、样本 limitations。 ──

def _record(session_id, tags, outcome='', source='mock'):
    return dict(session_id=session_id, tags=tags, outcome=outcome, source=source)


def test_report_empty_state():
    report = FM.build_pattern_report([])
    assert report['total_sessions'] == 0 and report['recurring'] == []
    assert report['limitations']


def test_report_finds_recurring_and_outcome_split():
    records = [
        _record('a', [dict(scope='controlled', tag='detail_followup', note='')], outcome='fail', source='real'),
        _record('b', [dict(scope='controlled', tag='detail_followup', note='')], outcome='pass', source='real'),
        _record('c', [dict(scope='controlled', tag='intro', note='')], outcome='', source='mock'),
        _record('d', [dict(scope='uncontrolled', tag='budget_hc', note='')], source='real'),
    ]
    report = FM.build_pattern_report(records)
    assert report['total_sessions'] == 4
    recurring = {item['tag']: item for item in report['recurring']}
    assert set(recurring) == {'detail_followup'}
    assert recurring['detail_followup']['count'] == 2
    assert recurring['detail_followup']['negative'] == 1 and recurring['detail_followup']['positive'] == 1
    assert recurring['detail_followup']['advice'], '重复项要带下一场建议'
    by_tag = {item['tag']: item for item in report['by_tag']}
    assert by_tag['intro']['negative'] == 0 and by_tag['intro']['positive'] == 0  # 无 outcome 不计
    assert {item['tag'] for item in report['uncontrolled']} == {'budget_hc'}
    assert not report['limitations'], '≥3 场且有结论时不应挂 limitations'


def test_report_no_recurring_when_all_unique():
    records = [
        _record(f's{i}', [dict(scope='controlled', tag=tag_id, note='')])
        for i, tag_id in enumerate(['structure', 'intro', 'vague', 'nerves', 'qa_missing'])
    ]
    report = FM.build_pattern_report(records)
    assert report['total_sessions'] == 5 and report['recurring'] == []
    assert any('重复' in line for line in report['limitations'])


# ── Repository：session_id 唯一 upsert（返回修改共用同一行）。 ──

def test_repository_attribution_upsert_roundtrip(repo):
    first = repo.save_interview_attribution(dict(
        session_id='I-1', source='mock', job_id='J-1', company='字节', job_title='后端',
        outcome='self_negative', tags=[dict(scope='controlled', tag='structure', note='')], note='第一版'))
    assert first['id'].startswith('FM-')
    second = repo.save_interview_attribution(dict(
        session_id='I-1', source='mock', job_id='J-1', company='字节', job_title='后端',
        outcome='self_negative', tags=[dict(scope='controlled', tag='nerves', note='')], note='改判'))
    assert second['id'] == first['id'], '返回修改必须复用同一行'
    rows = repo.interview_attributions()
    assert len(rows) == 1 and rows[0]['note'] == '改判' and rows[0]['tags'][0]['tag'] == 'nerves'
    assert repo.get_interview_attribution('I-1')['outcome'] == 'self_negative'
    assert repo.delete_interview_attribution('I-1') and repo.get_interview_attribution('I-1') is None


# ── API 往返：造场次 → 标注 → 博物馆报告 → 级联删除。 ──

def _seed_mock_session(repo):
    session = new_session(persona_id='dev', round_id='business', intensity='standard', question_count=4,
                          material=dict(job_id='J-x', job_title='后端工程师', company='字节'),
                          job_title='后端工程师', company_name='字节')
    return repo.save_interview_session(session)['id']


def _seed_transcript(repo):
    transcript = new_transcript(job_id='J-x', job_title='后端工程师', company='字节', text='逐字稿原文')
    return repo.save_interview_transcript(transcript)['id']


def test_api_roundtrip_mock_and_real(client, repo):
    mock_id, real_id = _seed_mock_session(repo), _seed_transcript(repo)
    for session_id in (mock_id, real_id):
        resp = client.post('/api/interview/attributions', json=dict(
            source='real' if session_id == real_id else 'mock', session_id=session_id,
            tags=[dict(scope='controlled', tag='detail_followup', note='追问两层就卡'),
                  dict(scope='uncontrolled', tag='interviewer_rush')],
            note='整场备注', outcome='fail' if session_id == real_id else 'self_negative'))
        assert resp.status_code == 200, resp.text
    data = client.get('/api/interview/failure-museum').json()
    assert data['report']['total_sessions'] == 2
    assert data['report']['recurring'][0]['tag'] == 'detail_followup'
    assert {t['id'] for t in data['controlled']} == {t['id'] for t in FM.CONTROLLED_TAGS}
    assert len(data['records']) == 2
    # 返回修改：重新提交同场次，报告仍只算一场
    client.post('/api/interview/attributions', json=dict(
        source='mock', session_id=mock_id,
        tags=[dict(scope='controlled', tag='detail_followup', note='')], note='改判后'))
    data = client.get('/api/interview/failure-museum').json()
    assert data['report']['total_sessions'] == 2


def test_api_rejects_bad_payloads_and_unknown_session(client, repo):
    mock_id = _seed_mock_session(repo)
    assert client.post('/api/interview/attributions', json=dict(
        source='mock', session_id='I-none',
        tags=[dict(scope='controlled', tag='structure')])).status_code == 404
    assert client.post('/api/interview/attributions', json=dict(
        source='mock', session_id=mock_id,
        tags=[dict(scope='controlled', tag='not_exist')])).status_code == 400
    assert client.post('/api/interview/attributions', json=dict(
        source='mock', session_id=mock_id, tags=[])).status_code == 422
    assert client.get('/api/interview/attributions/I-none').status_code == 404
    assert client.delete('/api/interview/attributions/I-none').status_code == 404


def test_api_cascade_delete_removes_attribution(client, repo):
    mock_id, real_id = _seed_mock_session(repo), _seed_transcript(repo)
    for session_id in (mock_id, real_id):
        client.post('/api/interview/attributions', json=dict(
            source='mock' if session_id == mock_id else 'real', session_id=session_id,
            tags=[dict(scope='controlled', tag='intro')]))
    client.delete(f'/api/interview/sessions/{mock_id}')
    client.delete(f'/api/interview/transcripts/{real_id}')
    assert client.get('/api/interview/failure-museum').json()['report']['total_sessions'] == 0
