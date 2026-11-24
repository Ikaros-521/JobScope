"""面试备战计划：模型个性化生成、规则兜底、幂等、来源提取、打卡与连续天数。

全部 create_app(tmp_path) / Repository(tmp_path) 隔离，不碰真实 .data。
时间敏感断言直插 completed_at / created_at（now() 同秒会掩盖顺序）。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope import prep_plan_llm
from jobscope.prep_plan import auto_complete_mock, collect_focus, generate, idempotent_plan, snapshot
from jobscope.prep_plan_llm import DiagnosisProbe
from jobscope.repository import Repository, now


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def make_session(session_id, report):
    return dict(id=session_id, title='模拟面试', job_title='产品经理', company='甲公司',
                persona={}, material={}, question_plan=None,
                progress=dict(phase='wrapup', current_index=0, rounds=1, segments=[]),
                status='done', report=report, conversation_id='',
                created_at=now(), updated_at=now())


def test_empty_db_generates_generic_plan(repo):
    plan = generate(repo)
    assert plan['created_from'] == 'generic'
    assert plan['days'] == 14
    assert len(plan['focus']) == 5
    assert all('通用入门' in item['source'] for item in plan['focus'])

    tasks = repo.prep_tasks(plan['id'])
    assert len(tasks) == 14 * 2 + 2  # 每天一件 mock + 一件 drill/study，第 7/14 天各加一次周复盘
    assert sum(1 for task in tasks if task['kind'] == 'mock') == 14
    assert all(task['required'] for task in tasks if task['kind'] == 'mock')
    assert {task['date'] for task in tasks} == {
        (datetime.fromisoformat(plan['start_date']) + timedelta(days=index)).date().isoformat()
        for index in range(14)}


def test_generate_is_idempotent_same_day(repo):
    first = generate(repo)
    second = generate(repo)
    assert first['id'] == second['id']
    assert len(repo.prep_tasks(first['id'])) == 30


def test_focus_from_interview_report(repo):
    report = dict(
        summary='', dimensions=[dict(name='数据与量化', score=2, comment='没有数字')],
        risk_list=['说不清项目结果'], actions=['用 STAR 重写两个项目'],
        questions=[dict(topic='项目', question='？', critique='', missing=['个人动作', '结果'],
                        model_answer='', bad_answer_note='')],
        reviewed=1, generated_at=now())
    repo.save_interview_session(make_session('I-1', report))

    focus, sessions, created_from = collect_focus(repo)
    topics = [item['topic'] for item in focus]
    assert '数据与量化' in topics
    assert '说不清项目结果' in topics
    assert '补齐：个人动作' in topics
    assert sessions == ['I-1']
    assert created_from == 'interview_report'
    assert any('维度「数据与量化」2/5' in item['source'] for item in focus)

    plan = generate(repo)
    assert plan['created_from'] == 'interview_report'
    assert '数据与量化' in repo.prep_tasks(plan['id'])[0]['topic']


def test_recurring_failure_has_highest_weight(repo):
    for sid in ('I-1', 'I-2'):
        repo.save_interview_attribution(dict(session_id=sid, source='mock', outcome='fail',
                                             tags=[dict(scope='controlled', tag='structure', note='')]))
    focus, _, created_from = collect_focus(repo)
    assert created_from == 'failure_museum'
    assert focus[0]['topic'] == '应答结构混乱'
    assert focus[0]['weight'] == 3
    assert '重复失分 2 次' in focus[0]['source']


def test_task_status_flow_and_snapshot(client, repo):
    generated = generate(repo)
    snapshot_before = client.get('/api/prep-plan').json()
    today_tasks = snapshot_before['today']['tasks']
    assert len(today_tasks) == 2
    mock_task = next(task for task in today_tasks if task['kind'] == 'mock')
    other = next(task for task in today_tasks if task['kind'] != 'mock')

    done = client.post(f"/api/prep-plan/tasks/{mock_task['id']}/done", json={'note': '练完了'}).json()
    assert done['task']['status'] == 'done'
    assert done['task']['note'] == '练完了'
    assert done['progress']['required_done'] == 1
    assert done['progress']['streak'] == 1

    skipped = client.post(f"/api/prep-plan/tasks/{other['id']}/skip").json()
    assert skipped['task']['status'] == 'skipped'
    assert skipped['progress']['skipped'] == 1

    reopened = client.post(f"/api/prep-plan/tasks/{other['id']}/reopen").json()
    assert reopened['task']['status'] == 'pending'
    assert reopened['progress']['skipped'] == 0

    missing = client.post('/api/prep-plan/tasks/T-does-not-exist/done')
    assert missing.status_code == 404
    assert generated['id'] == done['plan']['id']


def test_streak_counts_required_done_days(repo):
    plan = generate(repo)
    tasks = repo.prep_tasks(plan['id'])
    today = datetime.now(timezone.utc).date()
    # 昨天、前天的必做 mock 各完成一次；今天未完成 → streak 从昨天起算，不能判为断档。
    for offset, task in enumerate((task for task in tasks if task['kind'] == 'mock'), start=1):
        if offset > 2:
            break
        stamp = (today - timedelta(days=offset)).isoformat() + 'T10:00:00+00:00'
        with repo.connection() as conn:
            conn.execute('UPDATE prep_tasks SET status=?, completed_at=? WHERE id=?', ('done', stamp, task['id']))
    data = snapshot(repo)
    assert data['progress']['streak'] == 2
    assert data['progress']['best_streak'] == 2

    # 再补一个 5 天前 + 4 天前的连续段：best 取历史最长，streak 仍是当前段。
    older = [task for task in tasks if task['kind'] == 'mock'][2:4]
    for offset, task in zip((5, 4), older):
        stamp = (today - timedelta(days=offset)).isoformat() + 'T10:00:00+00:00'
        with repo.connection() as conn:
            conn.execute('UPDATE prep_tasks SET status=?, completed_at=? WHERE id=?', ('done', stamp, task['id']))
    data = snapshot(repo)
    assert data['progress']['best_streak'] == 2


def test_auto_complete_mock_only_today_once(repo):
    plan = generate(repo)
    assert auto_complete_mock(repo, 'I-9') is True
    assert auto_complete_mock(repo, 'I-9') is False
    today = now()[:10]
    mock = [task for task in repo.prep_tasks(plan['id'])
            if task['kind'] == 'mock' and task['date'] == today][0]
    assert mock['status'] == 'done'
    assert 'I-9' in mock['note']


def test_generate_new_data_archives_old_plan(repo):
    old = generate(repo)
    report = dict(dimensions=[dict(name='业务理解', score=1, comment='不足')], risk_list=[], actions=[], questions=[])
    repo.save_interview_session(make_session('I-new', report))
    new = generate(repo)
    assert new['id'] != old['id']
    assert repo.get_prep_plan(old['id'])['status'] == 'archived'
    assert repo.get_prep_plan(new['id'])['status'] == 'active'
    assert snapshot(repo)['plan']['id'] == new['id']


def test_archive_endpoint_and_dashboard_field(client, repo):
    generated = generate(repo)
    assert client.delete(f"/api/prep-plan/{generated['id']}").json() == {'archived': True}
    assert client.delete(f"/api/prep-plan/{generated['id']}").status_code == 404
    assert client.get('/api/prep-plan').json()['plan'] is None

    fresh = generate(repo)
    dashboard = client.get('/api/dashboard').json()
    assert dashboard['prep_plan']['plan']['id'] == fresh['id']
    assert dashboard['prep_plan']['today']['date'] == now()[:10]


def test_generate_days_validation_and_note_guard(client, repo):
    assert client.post('/api/prep-plan', json={'days': 99}).status_code == 422
    generated = generate(repo, 7)
    assert generated['days'] == 7
    task = next(task for task in repo.prep_tasks(generated['id']) if task['kind'] == 'mock')
    long_note = 'x' * 3000
    result = client.post(f"/api/prep-plan/tasks/{task['id']}/done", json={'note': long_note}).json()
    assert len(result['task']['note']) == 500


def test_legacy_prep_tasks_schema_is_migrated(tmp_path):
    """既有库建表时没有 job_id：CREATE TABLE IF NOT EXISTS 不会补列，必须显式迁移。"""
    legacy_dir = tmp_path / 'legacy'
    legacy_dir.mkdir()
    conn = sqlite3.connect(legacy_dir / 'jobscope.sqlite3')
    conn.execute('CREATE TABLE prep_tasks (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, day_index INTEGER NOT NULL, '
                 'date TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT \'\', '
                 'topic TEXT NOT NULL DEFAULT \'\', minutes INTEGER NOT NULL DEFAULT 15, required INTEGER NOT NULL DEFAULT 0, '
                 'source TEXT NOT NULL DEFAULT \'\', session_id TEXT NOT NULL DEFAULT \'\', status TEXT NOT NULL DEFAULT \'pending\', '
                 'note TEXT NOT NULL DEFAULT \'\', completed_at TEXT NOT NULL DEFAULT \'\', created_at TEXT NOT NULL)')
    conn.commit()
    conn.close()

    repo = Repository(legacy_dir)
    with repo.connection() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(prep_tasks)')}
    assert 'job_id' in columns
    plan = generate(repo)
    assert len(repo.prep_tasks(plan['id'])) == 30


def test_jd_gaps_alone_are_not_mislabeled_as_interview_report(repo):
    """只有岗位评估缺口、没有面试复盘时，来源必须是 jd_gaps，不能冒充面试报告。"""
    job = repo.save_job(dict(title='前端工程师', company='甲公司'), 'test')
    repo.save_report(dict(job_id=job['id'], kind='analysis', gaps=['不懂 Kubernetes']))
    _, _, created_from = collect_focus(repo)
    assert created_from == 'jd_gaps'
    plan = generate(repo)
    assert plan['created_from'] == 'jd_gaps'
    assert any('评估缺口' in item['source'] for item in plan['focus'])


def test_snapshot_repairs_empty_shell_plan(repo):
    """旧库写失败留下的「有计划、0 任务」空壳，读取时按已存 focus 幂等补齐。"""
    plan = generate(repo)
    with repo.connection() as conn:
        conn.execute('DELETE FROM prep_tasks WHERE plan_id=?', (plan['id'],))
    assert repo.prep_tasks(plan['id']) == []

    data = snapshot(repo)
    assert data['progress']['total'] == 30
    assert data['progress']['required_total'] == 22
    assert len(data['today']['tasks']) == 2
    assert data['plan']['id'] == plan['id']
    assert [item['topic'] for item in data['plan']['focus']] == [item['topic'] for item in plan['focus']]
    # 再次读取不能重复补任务，也不能把已有打卡状态洗掉。
    assert len(snapshot(repo)['week'][0]['tasks']) == 2


def test_llm_generation_personalizes_tasks(monkeypatch, repo):
    """模型给出诊断与具体练法后，任务标题/详情必须带上它，而不是通用模板。"""
    report = dict(dimensions=[dict(name='数据与量化', score=2, comment='没有数字')],
                  risk_list=[], actions=[], questions=[])
    repo.save_interview_session(make_session('I-llm', report))
    model_reply = json.dumps(dict(
        diagnosis='两场面试都卡在项目深挖：关键数字说不出依据，串并行链路也讲不稳。',
        focus=[
            dict(topic='链路串并行讲不清', detail='高危清单指出 VLM 链路完全缺失',
                 source='I-llm 高危清单', drill='画一张端到端链路图标出串并行，再口头讲 90 秒',
                 kind='drill', weight=3),
            dict(topic='量化没有依据', detail='数据与量化 2/5',
                 source='I-llm 维度评分', drill='把并发 3 的依据写成一页测算：上限、p95、超时率',
                 kind='study', weight=2),
        ]), ensure_ascii=False)
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)
    monkeypatch.setattr('jobscope.model_transport.complete',
                        lambda config, system, user, **kwargs: (model_reply, {}))

    plan = generate(repo, 7, prefs=dict(goal='主攻前端架构岗', minutes_per_day=40, job_id='J-target'))
    assert plan['origin'] == 'model'
    assert '项目深挖' in plan['diagnosis']
    assert plan['prefs']['goal'] == '主攻前端架构岗'
    assert plan['prefs']['minutes_per_day'] == 40
    tasks = repo.prep_tasks(plan['id'])
    assert any('画一张端到端链路图' in task['title'] for task in tasks)
    assert any('画一张端到端链路图' in task['detail'] for task in tasks)
    assert all(task['job_id'] == 'J-target' for task in tasks if task['kind'] != 'review')
    assert any('并发 3 的依据' in task['detail'] for task in tasks)


def test_llm_failure_falls_back_to_rules(monkeypatch, repo):
    """模型不可用或输出垃圾时退规则版，计划照常生成且 origin 明示。"""
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)
    monkeypatch.setattr('jobscope.model_transport.complete',
                        lambda config, system, user, **kwargs: ('garbage not json', {}))
    plan = generate(repo, 7, prefs=dict(use_llm=True))
    assert plan['origin'] == 'rules'
    assert len(repo.prep_tasks(plan['id'])) == 15


def test_use_llm_false_never_calls_model(monkeypatch, repo):
    def boom(*args, **kwargs):
        raise AssertionError('use_llm=false 时不应调用模型')
    monkeypatch.setattr('jobscope.model_transport.complete', boom)
    plan = generate(repo, 7, prefs=dict(use_llm=False))
    assert plan['origin'] == 'rules'
    assert plan['prefs']['use_llm'] is False


def test_parse_json_tolerates_fence_and_drops_bad_focus():
    good = dict(topic='追问下失守', detail='降级链路答不完整', source='面试报告',
                drill='写出降级链路三步并口述', kind='drill', weight=3)
    content = '```json\n' + json.dumps(dict(
        diagnosis='诊断', focus=[{'topic': ''}, good, dict(topic='量化没依据', drill='补一页测算')]
    ), ensure_ascii=False) + '\n```'
    result = prep_plan_llm._coerce(prep_plan_llm._parse_json(content))
    assert result['diagnosis'] == '诊断'
    assert [item['topic'] for item in result['focus']] == ['追问下失守', '量化没依据']
    assert result['focus'][0]['drill'] == '写出降级链路三步并口述'


def test_legacy_prep_plans_schema_is_migrated(tmp_path):
    """旧计划头没有 origin/diagnosis/prefs，迁移后读写都要兼容。"""
    legacy_dir = tmp_path / 'legacy-plans'
    legacy_dir.mkdir()
    conn = sqlite3.connect(legacy_dir / 'jobscope.sqlite3')
    conn.execute('CREATE TABLE prep_plans (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT \'active\', '
                 'days INTEGER NOT NULL DEFAULT 14, start_date TEXT NOT NULL, end_date TEXT NOT NULL, '
                 'created_from TEXT NOT NULL DEFAULT \'generic\', focus TEXT NOT NULL DEFAULT \'[]\', '
                 'source_sessions TEXT NOT NULL DEFAULT \'[]\', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    conn.commit()
    conn.close()

    repo = Repository(legacy_dir)
    with repo.connection() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(prep_plans)')}
    assert {'origin', 'diagnosis', 'prefs'} <= columns
    plan = generate(repo, 7, prefs=dict(goal='旧库迁移', use_llm=False))
    assert repo.get_prep_plan(plan['id'])['prefs']['goal'] == '旧库迁移'


def test_generate_endpoint_accepts_custom_prefs(client):
    result = client.post('/api/prep-plan', json=dict(
        days=7, goal='两周后终面，重点补项目深挖', minutes_per_day=40, use_llm=False)).json()
    plan = result['plan']
    assert plan['prefs']['goal'] == '两周后终面，重点补项目深挖'
    assert plan['prefs']['minutes_per_day'] == 40
    assert result['model_configured'] is False


# ---- 流式生成（2026-11-24 第三轮）：诊断探测器 / 阶段事件 / run + SSE ----

def test_diagnosis_probe_streams_incrementally():
    """诊断文本要从流式 JSON 里边收边解锁：跨块、半个转义、\\uXXXX、围栏前缀都要扛住。"""
    probe = DiagnosisProbe()
    chunks = ['```json\n{"diagn', 'osis":"两场都卡在', '深挖：数字\\', '"不足，', '\\n需要量化",',
              '"focus":[{"topic":"链路讲不清","drill":"画链路图"}]}']
    pieces = [probe.feed(chunk) for chunk in chunks]
    assert ''.join(pieces) == '两场都卡在深挖：数字"不足，\n需要量化'
    assert all(piece == '' or not piece.startswith('```') for piece in pieces)
    assert pieces[2] == '深挖：数字' and pieces[3] == '"不足，' and pieces[4] == '\n需要量化'
    # diagnosis 键不在最前（先输出了别的键）同样能探测到
    probe2 = DiagnosisProbe()
    out = ''.join(probe2.feed(piece) for piece in ['{"focus":[],"diagnosis":"第', '二种布局\\"也可"}'])
    assert out == '第二种布局"也可'


def _fake_stream(reply, piece_size=9):
    def fake(config, system, user, *, max_tokens=0, temperature=None, timeout=0, on_delta=None):
        for index in range(0, len(reply), piece_size):
            if on_delta:
                on_delta(reply[index:index + piece_size])
        return reply, {}
    return fake


def _payload_of(events, kind):
    return next(payload for name, payload in events if name == kind)


def test_generate_emits_progress_events(monkeypatch, repo):
    """生成管线按序外发阶段事件：focus → model_start → diagnosis_delta… → model_done → focus_item… → scheduled。"""
    report = dict(dimensions=[dict(name='数据与量化', score=2, comment='没有数字')], risk_list=[], actions=[], questions=[])
    repo.save_interview_session(make_session('I-stream', report))
    reply = json.dumps(dict(diagnosis='两场都卡在项目深挖，数字说不出依据。', focus=[
        dict(topic='链路讲不清', detail='高危清单指出缺失', source='I-stream 高危', drill='画链路图讲 90 秒', kind='drill', weight=3),
        dict(topic='量化没依据', detail='维度 2/5', source='I-stream 维度', drill='补一页并发测算', kind='study', weight=2),
    ]), ensure_ascii=False)
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)
    monkeypatch.setattr('jobscope.model_transport.stream_complete', _fake_stream(reply))

    events = []
    plan = generate(repo, 7, prefs=dict(use_llm=True), on_event=lambda kind, payload: events.append((kind, payload)))
    kinds = [kind for kind, _ in events]
    assert kinds[0] == 'focus' and kinds[1] == 'model_start'
    assert kinds.count('diagnosis_delta') >= 2
    assert kinds.count('focus_item') == 2
    assert kinds[-1] == 'scheduled'
    assert kinds.index('model_done') < kinds.index('focus_item')
    assert '项目深挖' in _payload_of(events, 'model_done')['diagnosis']
    assert _payload_of(events, 'focus')['created_from'] == 'interview_report'
    assert _payload_of(events, 'scheduled')['task_count'] == len(repo.prep_tasks(plan['id']))
    assert plan['origin'] == 'model'


def test_generate_emits_fallback_event_when_model_fails(monkeypatch, repo):
    """模型失败要外发 model_fallback 说明，计划照常按规则生成——不静默、不中断。"""
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)

    def broken(config, system, user, **kwargs):
        raise ValueError('模型请求失败，请检查服务配置后重试')
    monkeypatch.setattr('jobscope.model_transport.stream_complete', broken)

    events = []
    plan = generate(repo, 7, prefs=dict(use_llm=True), on_event=lambda kind, payload: events.append((kind, payload)))
    assert plan['origin'] == 'rules'
    kinds = [kind for kind, _ in events]
    assert 'model_fallback' in kinds and 'model_done' not in kinds
    assert '规则排期' in _payload_of(events, 'model_fallback')['message']
    assert 'scheduled' in kinds


def test_idempotent_plan_and_model_upgrade_path(monkeypatch, repo):
    """同日同数据幂等；规则版在模型变为可用时放行重新生成（升级为 AI 定制）。"""
    first = generate(repo, 7, prefs=dict(use_llm=True))  # 模型未配置 → 规则版
    same = idempotent_plan(repo, dict(use_llm=True))
    assert same is not None and same['id'] == first['id']
    # prefs 变化 → 不幂等
    assert idempotent_plan(repo, dict(use_llm=True, goal='补项目深挖')) is None
    # 规则版 + 模型现在可用 → 返回 None，走重新生成升级
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)
    assert idempotent_plan(repo, dict(use_llm=True)) is None
    # 模型仍不可用时保持幂等，不能每次打开页面都重建
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: False)
    assert idempotent_plan(repo, dict(use_llm=True))['id'] == first['id']


def test_run_endpoint_rule_fast_path_and_idempotent(client):
    """规则快路径同步返回快照不起 run；重复请求幂等沿用当天计划。"""
    first = client.post('/api/prep-plan/runs', json=dict(days=7, use_llm=False)).json()
    assert first['run_id'] is None and first['reused'] is False
    assert first['snapshot']['plan']['days'] == 7
    second = client.post('/api/prep-plan/runs', json=dict(days=7, use_llm=False)).json()
    assert second['reused'] is True and second['run_id'] is None
    assert second['snapshot']['plan']['id'] == first['snapshot']['plan']['id']
    assert 'active_run' in client.get('/api/prep-plan').json()
    assert client.post('/api/prep-plan/runs', json=dict(days=99)).status_code == 422


@pytest.fixture
def model_client(tmp_path, monkeypatch):
    """配置分析模型 env：让 runs 入口走流式 run（模型调用本身由用例替换 stub）。"""
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def _read_prep_stream(client, run_id):
    frames = []
    with client.stream('GET', f'/api/prep-plan/runs/{run_id}/stream') as response:
        assert response.status_code == 200
        event = None
        for line in response.iter_lines():
            if line.startswith('event: '):
                event = line[7:].strip()
            elif line.startswith('data: '):
                frames.append((event, json.loads(line[6:])))
    return frames


def test_run_endpoint_streams_progress_and_done(model_client, monkeypatch):
    """SSE 从头重放：start → focus → diagnosis_delta… → focus_item… → scheduled → done(完整快照) → end。"""
    reply = json.dumps(dict(diagnosis='卡在项目深挖，数字说不出依据。', focus=[
        dict(topic='链路讲不清', detail='高危清单', source='报告', drill='画链路图讲 90 秒', kind='drill', weight=3),
        dict(topic='量化没依据', detail='维度 2/5', source='报告', drill='补一页测算', kind='study', weight=2),
    ]), ensure_ascii=False)
    monkeypatch.setattr('jobscope.model_transport.stream_complete', _fake_stream(reply))
    run_id = model_client.post('/api/prep-plan/runs', json=dict(days=7, use_llm=True)).json()['run_id']
    assert run_id

    deadline = time.time() + 15
    while time.time() < deadline:
        if model_client.get(f'/api/prep-plan/runs/{run_id}').json().get('done'):
            break
        time.sleep(0.05)
    else:
        raise AssertionError('备战计划生成超时未完成')
    handle = model_client.app.state.prep_plan_runs.get(run_id)
    if handle and handle.thread is not None:
        handle.thread.join(timeout=5)

    frames = _read_prep_stream(model_client, run_id)
    kinds = [kind for kind, _ in frames]
    assert kinds[0] == 'start' and kinds[-1] == 'end'
    for expected in ('focus', 'model_start', 'diagnosis_delta', 'model_done', 'focus_item', 'scheduled', 'done'):
        assert expected in kinds
    done = next(payload for kind, payload in frames if kind == 'done')
    assert done['plan']['origin'] == 'model'
    assert done['plan']['diagnosis'].startswith('卡在项目深挖')
    assert done['progress']['total'] > 0
    assert 'active_run' in model_client.get('/api/prep-plan').json()
    assert model_client.get('/api/prep-plan/runs/nope').status_code == 404
    assert model_client.get('/api/prep-plan/runs/nope/stream').status_code == 404


def test_run_endpoint_reuses_active_run(model_client, monkeypatch):
    """重复发起在活动 run 期间原样返回同一 run，不并发起第二份。"""
    import threading
    entered, release = threading.Event(), threading.Event()

    def slow_stream(config, system, user, *, max_tokens=0, temperature=None, timeout=0, on_delta=None):
        entered.set()
        release.wait(5)
        return json.dumps(dict(diagnosis='诊断文本足够长以通过校验。', focus=[
            dict(topic='链路讲不清', detail='', source='报告', drill='画链路图', kind='drill', weight=3),
            dict(topic='量化没依据', detail='', source='报告', drill='补测算', kind='study', weight=2),
        ]), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.stream_complete', slow_stream)
    first = model_client.post('/api/prep-plan/runs', json=dict(days=7)).json()
    assert entered.wait(5)
    second = model_client.post('/api/prep-plan/runs', json=dict(days=7)).json()
    assert second['reused'] is True and second['run_id'] == first['run_id']
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        if model_client.get(f"/api/prep-plan/runs/{first['run_id']}").json().get('done'):
            break
        time.sleep(0.05)
    handle = model_client.app.state.prep_plan_runs.get(first['run_id'])
    if handle and handle.thread is not None:
        handle.thread.join(timeout=5)

