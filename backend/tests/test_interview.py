"""模拟面试：建场出题 / 逐题对话 / 复盘报告 / 场次管理（模型调用全部打桩，数据 tmp_path 隔离）。"""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 配置分析模型 env：purpose_configured('analysis') 命中后建场/对话/复盘都走后台 run。
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    with TestClient(create_app(tmp_path)) as value:
        yield value


@pytest.fixture
def bare_client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def fake_plan(job_count=2):
    return dict(opening='你好，我是今天的面试官。',
                questions=[dict(topic='项目深挖', question=f'讲讲项目{index}', focus='真实性',
                                followups=['数据呢？', '你做了什么'], source='resume') for index in range(job_count)])


def patch_plan(monkeypatch, plan=None):
    monkeypatch.setattr('jobscope.interview_runs.build_question_plan',
                        lambda persona, material, question_count=8: plan or fake_plan())


def advance_args(verdict='next', index=0, weakness=('无量化数据',)):
    return json.dumps(dict(verdict=verdict, question_index=index, weakness=list(weakness), note='讲得浅'), ensure_ascii=False)


def patch_stream(monkeypatch, answers):
    """按次序回放面试官轮次：content + advance 调度。"""
    calls = {'n': 0}

    def fake_stream_turn(config, messages, tools, *, on_delta=None, on_reasoning=None, should_stop=None, register_abort=None, timeout=60, max_tokens=None):
        reply = answers[min(calls['n'], len(answers) - 1)]
        calls['n'] += 1
        if on_delta:
            on_delta(reply['content'])
        return dict(role='assistant', content=reply['content'],
                    tool_calls=[dict(id=f'c{calls["n"]}', type='function',
                                     function=dict(name='advance', arguments=reply.get('advance', advance_args())))]), {}, ''

    monkeypatch.setattr('jobscope.interview_runs.stream_turn', fake_stream_turn)


def patch_report(monkeypatch):
    monkeypatch.setattr('jobscope.interview_runs.generate_report', lambda persona, segments, material, should_stop=None, on_progress=None: dict(
        summary='总评：项目有料但缺数据。', dimensions=[dict(name='数据与量化', score=2, comment='全程没有量化')],
        risk_list=['每个项目补一组前后对比数据'], actions=['改简历补结果'],
        questions=[dict(topic=seg['question'].get('topic', ''), question=seg['question'].get('question', ''),
                        critique='偏故事化。', missing=['量化数据'], model_answer='示范答法。', bad_answer_note='别只讲故事。')
                   for seg in segments]))


def await_session_done(client, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        handle = client.app.state.interview_runs.get(run_id)
        if handle is None or handle.done:
            return handle
        time.sleep(0.02)
    raise AssertionError('面试运行超时未完成')


def join_run(client, run_id, timeout=5):
    handle = client.app.state.interview_runs.get(run_id)
    if handle and handle.thread is not None:
        handle.thread.join(timeout=timeout)


def create_session(client, monkeypatch, **extra):
    patch_plan(monkeypatch)
    body = dict(persona_id='product', company_style='alibaba')
    body.update(extra)
    response = client.post('/api/interview/sessions', json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    join_run(client, data['run_id'])
    return data


def test_create_requires_model(bare_client):
    assert bare_client.post('/api/interview/sessions', json=dict(persona_id='ali_product')).status_code == 400


def test_personas_cards(client):
    payload = client.get('/api/interview/personas').json()
    assert {card['id'] for card in payload['cards']} >= {'product', 'hr'}
    assert all(card['rounds'] for card in payload['cards'])


def test_create_session_plans(client, monkeypatch):
    data = create_session(client, monkeypatch)
    detail = client.get(f"/api/interview/sessions/{data['session_id']}").json()
    session = detail['session']
    assert session['status'] == 'ready'
    assert len(session['question_plan']['questions']) == 2
    assert session['persona']['round_label'] == '业务面（一面）'
    assert session['conversation_id']
    # 出题阶段对话还没内容
    assert client.get(f"/api/interview/sessions/{data['session_id']}/messages").json()['messages'] == []


def test_turn_flow_advances_plan(client, monkeypatch):
    data = create_session(client, monkeypatch)
    patch_stream(monkeypatch, [
        dict(content='先讲讲你最有代表性的项目。', advance=advance_args('next', 0)),
        dict(content='这个项目结果怎么衡量的？', advance=advance_args('followup', 1, ('含糊其辞',))),
    ])
    # 开场轮（answer 为空）
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    assert opened.status_code == 200
    join_run(client, opened.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'live'
    assert session['progress']['current_index'] == 1  # 开场后 verdict=next 推进到第 1 题
    # 回答一轮 followup
    turn = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='我做了一个会员增长项目'))
    assert turn.status_code == 200
    join_run(client, turn.json()['run_id'])
    messages = client.get(f"/api/interview/sessions/{data['session_id']}/messages").json()['messages']
    assert [item['role'] for item in messages] == ['assistant', 'user', 'assistant']
    assert '会员增长项目' in messages[1]['content']
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['progress']['segments'][0]['answers'] == ['我做了一个会员增长项目']
    # 开场轮没有回答可归档，weakness 落空；本轮 followup 的信号与内心评价归到第 1 题片段
    assert session['progress']['segments'][0]['signals'] == ['含糊其辞']
    assert session['progress']['segments'][0]['notes'] == ['讲得浅']


def test_finish_generates_report(client, monkeypatch):
    data = create_session(client, monkeypatch)
    patch_stream(monkeypatch, [dict(content='讲讲你的项目。', advance=advance_args('next', 0))])
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    join_run(client, opened.json()['run_id'])
    turn = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='我做过会员项目，提升了 12%'))
    join_run(client, turn.json()['run_id'])
    patch_report(monkeypatch)
    finished = client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={})
    assert finished.status_code == 200
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'done'
    assert session['report']['summary'].startswith('总评')
    assert len(session['report']['questions']) == 1  # 只有归档了回答的片段进复盘
    # 已复盘的场次不能再追加对话
    again = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='再答一题'))
    assert again.status_code == 409


def test_regenerate_report_on_done_session(client, monkeypatch):
    """复盘支持重新生成：done 场次再 finish 从既有片段重跑；失败回滚到 done（不重开面试）、旧报告保留。"""
    data = create_session(client, monkeypatch)
    patch_stream(monkeypatch, [dict(content='讲讲你的项目。', advance=advance_args('next', 0))])
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    join_run(client, opened.json()['run_id'])
    turn = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='我做过会员项目，提升了 12%'))
    join_run(client, turn.json()['run_id'])
    patch_report(monkeypatch)
    first = client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={})
    join_run(client, first.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'done'

    # 重新生成：换一版报告内容
    monkeypatch.setattr('jobscope.interview_runs.generate_report', lambda persona, segments, material, should_stop=None, on_progress=None: dict(
        summary='总评（重跑版）：更好了。', dimensions=[dict(name='数据与量化', score=4, comment='这次有数据')],
        risk_list=['继续补实验设计'], actions=['再改一版'],
        questions=[dict(topic=seg['question'].get('topic', ''), question=seg['question'].get('question', ''),
                        critique='重跑点评。', missing=[], model_answer='新版示范。', bad_answer_note='新坑。')
                   for seg in segments]))
    second = client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={})
    assert second.status_code == 200, second.text
    join_run(client, second.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'done'
    assert session['report']['summary'].startswith('总评（重跑版）')

    # 重生成失败：回滚到 done（面试不重开），旧报告原样保留
    def boom(*args, **kwargs):
        raise ValueError('模型抽风')
    monkeypatch.setattr('jobscope.interview_runs.generate_report', boom)
    third = client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={})
    assert third.status_code == 200
    join_run(client, third.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'done'  # 不是 live
    assert session['report']['summary'].startswith('总评（重跑版）')


def test_free_practice_without_job_or_jd(client, monkeypatch):
    data = create_session(client, monkeypatch)
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['job_id'] == ''
    assert session['material']['jd_summary'] == ''


def test_conflict_finish_during_planning(client, monkeypatch):
    # 出题线程被阻塞时结束复盘：场次还在 planning，应 409
    import threading
    gate = threading.Event()
    monkeypatch.setattr('jobscope.interview_runs.build_question_plan',
                        lambda persona, material, question_count=8: (gate.wait(5), fake_plan())[1])
    data = client.post('/api/interview/sessions', json=dict(persona_id='product')).json()
    time.sleep(0.2)  # 等 worker 起来并进入阻塞
    assert client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={}).status_code == 409
    gate.set()
    join_run(client, data['run_id'])


def test_delete_session_removes_conversation(client, monkeypatch):
    data = create_session(client, monkeypatch)
    patch_stream(monkeypatch, [dict(content='开始吧。')])
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    join_run(client, opened.json()['run_id'])
    deleted = client.delete(f"/api/interview/sessions/{data['session_id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/interview/sessions/{data['session_id']}").status_code == 404
    repo = client.app.state.repo
    assert repo.list_conversations(dict(kind='interview', session_id=data['session_id']))['conversations'] if False else True
    conversations = repo.list_conversations(dict(kind='interview', session_id=data['session_id']))
    assert conversations == []


def test_unknown_persona_rejected(client, monkeypatch):
    patch_plan(monkeypatch)
    assert client.post('/api/interview/sessions', json=dict(persona_id='nope')).status_code == 400


def test_worker_uses_bound_model_config(client, monkeypatch):
    """回归：出题 worker 在后台线程，contextvar 不跨线程——必须显式挂生效配置。

    修前的 bug：请求线程 purpose_configured 走有效配置放行，worker 却回落环境变量
    （本测试 delenv 模拟未设 JOBSCOPE_LLM_URL），complete 收到空 url →「地址为空」。
    """
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    assert client.put('/api/settings/models', json={
        'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture-model', protocol='openai'),
    }).status_code == 200
    recorded = []

    def fake_complete(config, system, user, **kwargs):
        recorded.append(dict(url=config.get('url'), model=config.get('model')))
        plan = dict(opening='你好', questions=[dict(topic='项目深挖', question='讲讲你的项目', focus='真实性',
                                                    followups=['数据呢'], source='general')])
        return json.dumps(plan, ensure_ascii=False), {}

    # build_question_plan 内延迟 import complete：patch 传输层才覆盖真实调用链
    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    response = client.post('/api/interview/sessions', json=dict(persona_id='product', company_style='generic'))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{response.json()['session_id']}").json()['session']
    assert session['status'] == 'ready'
    assert recorded, '出题没有走到模型调用'
    # 生效配置（saved llm → analysis 回落）拼出完整 chat/completions 端点，而非环境回落空地址
    assert recorded[0]['url'] == 'https://fixture.example/v1/chat/completions'
    assert recorded[0]['model'] == 'fixture-model'


def test_unknown_company_style_rejected(client, monkeypatch):
    patch_plan(monkeypatch)
    assert client.post('/api/interview/sessions', json=dict(persona_id='product', company_style='nope')).status_code == 400


def test_custom_requires_job_anchor(client, monkeypatch):
    patch_plan(monkeypatch)
    # 自定义岗位没有任何锚点（无岗位/无JD/无岗位名）→ 400
    assert client.post('/api/interview/sessions', json=dict(persona_id='custom')).status_code == 400
    # 填了岗位名即可开面，岗位名进画像
    data = create_session(client, monkeypatch, persona_id='custom', job_title='数据分析师')
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['persona']['job_title'] == '数据分析师'


def test_job_session_carries_company_brief(client, monkeypatch):
    repo = client.app.state.repo
    job = repo.save_job(dict(title='数据分析师', company='甲公司', description='负责指标体系与实验设计', location='杭州'), 'user_import')
    repo.save_evidence(dict(title='公司简介', company='甲公司', kind='company_statement', text='主营电商与云计算业务，年度活跃用户过亿'), 'user_import')
    data = create_session(client, monkeypatch, job_id=job['id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert '电商与云计算' in session['material']['company_brief']
    assert session['material']['gaps'] or session['material']['jd_summary']  # JD 材料照常带上


def test_personas_payload_has_styles(client):
    payload = client.get('/api/interview/personas').json()
    assert {card['id'] for card in payload['cards']} >= {'product', 'operations', 'data', 'dev', 'hr', 'custom'}
    assert {style['id'] for style in payload['styles']} == {'alibaba', 'bigtech', 'generic'}


PERSONA = dict(company='大厂风格', company_style=dict(values=['数据驱动']), role_label='产品', role=dict(focus='需求落地'),
               round_label='业务面（一面）', round_style=dict(focus='业务理解'), intensity='standard',
               intensity_label='标准', job_title='产品经理', company_name='甲公司')
MATERIAL = dict(gaps=[], jd_summary='', resume_digest='')


def test_interviewer_prompt_carries_progress():
    """追问超限/反问超轮必须注入系统记账硬指令——模型自己数不清追问轮数（线上第 0 题追 18 轮的根因）。"""
    from jobscope.interview import build_interviewer_system
    plan = fake_plan()
    base = build_interviewer_system(PERSONA, MATERIAL, plan, phase='questioning')
    assert '实时进度' not in base  # 无进度时保持旧形态
    over = build_interviewer_system(PERSONA, MATERIAL, plan, phase='questioning',
                                    progress=dict(current_index=0, segments=[dict(answers=['答'] * 5)]))
    assert '当前第 1/2 题' in over and '追问层 4/3' in over
    assert '硬性要求' in over and 'verdict=next' in over
    under = build_interviewer_system(PERSONA, MATERIAL, plan, phase='questioning',
                                     progress=dict(current_index=1, segments=[dict(answers=['答'] * 2)]))
    assert '当前第 2/2 题' in under and '追问层 1/3' in under and '硬性要求' not in under
    wrapup = build_interviewer_system(PERSONA, MATERIAL, plan, phase='wrapup',
                                      progress=dict(segments=[dict(index=-1, reviewable=False, answers=['问'] * 5)]))
    assert '反问环节已进行 5 轮' in wrapup and '本轮必须直接告别收尾' in wrapup


def test_turn_prompt_injects_progress(client, monkeypatch):
    """每轮对话的 system 都带最新进度（system 逐轮重建，进行中的场次下一轮即生效）。"""
    data = create_session(client, monkeypatch)
    seen = []

    def fake_stream_turn(config, messages, tools, **kwargs):
        seen.append(messages[0]['content'])
        reply = dict(role='assistant', content='讲讲你的项目。',
                     tool_calls=[dict(id='c1', type='function', function=dict(name='advance', arguments=advance_args('next', 0)))])
        return reply, {}, ''

    monkeypatch.setattr('jobscope.interview_runs.stream_turn', fake_stream_turn)
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    join_run(client, opened.json()['run_id'])
    assert '【实时进度' in seen[0] and '当前第 1/2 题' in seen[0]
    turn = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='我做了一个会员增长项目'))
    join_run(client, turn.json()['run_id'])
    assert '当前第 2/2 题' in seen[1]


def test_report_pool_threads_carry_config(client, monkeypatch):
    """回归：复盘逐题点评跑在线程池，contextvar 不跨线程——生效配置必须显式带进池子任务。

    修前的 bug：use_config 挂在复盘 worker 线程上，generate_report 的 ThreadPoolExecutor
    里 review_segment 在池子线程 get_config() 取到空 → complete 报「地址为空，请填写服务地址」。
    """
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    assert client.put('/api/settings/models', json={
        'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture-model', protocol='openai'),
    }).status_code == 200
    data = create_session(client, monkeypatch)
    patch_stream(monkeypatch, [dict(content='讲讲你的项目。', advance=advance_args('next', 0))])
    opened = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer=''))
    join_run(client, opened.json()['run_id'])
    turn = client.post(f"/api/interview/sessions/{data['session_id']}/runs", json=dict(answer='我做过会员项目，提升了 12%'))
    join_run(client, turn.json()['run_id'])
    recorded = []

    def fake_complete(config, system, user, **kwargs):
        recorded.append(config.get('url'))
        if 'evaluation_gaps' in user:  # 汇总轮 payload 顶层带 evaluation_gaps
            payload = dict(summary='总评：有料。', dimensions=[dict(name='表达', score=3, comment='清晰')],
                           risk_list=['补数据'], actions=['改简历'])
        else:  # 逐题点评轮（池子线程）
            payload = dict(critique='偏故事化。', missing=['量化数据'], model_answer='示范。', bad_answer_note='别只讲故事。')
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{data['session_id']}/finish", json={})
    assert finished.status_code == 200, finished.text
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{data['session_id']}").json()['session']
    assert session['status'] == 'done'
    # 逐题点评（池子线程）+ 汇总（worker 线程）都拿到生效配置的完整端点，无一空 url
    assert len(recorded) == 2
    assert all(url == 'https://fixture.example/v1/chat/completions' for url in recorded), recorded


def _session_with_two_segments(client, monkeypatch):
    """三题题单 × 开场+两答：留下两个有回答的可复盘片段。"""
    patch_plan(monkeypatch, fake_plan(3))
    body = dict(persona_id='product', company_style='alibaba')
    response = client.post('/api/interview/sessions', json=body)
    join_run(client, response.json()['run_id'])
    session_id = response.json()['session_id']
    patch_stream(monkeypatch, [dict(content='开始。', advance=advance_args('next', 0)),
                               dict(content='下一题。', advance=advance_args('next', 1)),
                               dict(content='再下一题。', advance=advance_args('next', 2))])
    for answer in ('', '答第一题', '答第二题'):
        turn = client.post(f"/api/interview/sessions/{session_id}/runs", json=dict(answer=answer))
        join_run(client, turn.json()['run_id'])
    return session_id


def _summary_payload(risks=2, actions=2):
    return dict(summary='总评：有料但缺数据。', dimensions=[dict(name='结构化表达', score=3, comment='清晰')],
                risk_list=[f'高危{i}' for i in range(risks)], actions=[f'行动{i}' for i in range(actions)])


def test_report_coerces_oversized_summary(client, monkeypatch):
    """回归（线上 2026-11-20）：模型多给几条 risk/actions 是常态——软裁剪保住整份报告，不再整份作废。"""
    session_id = _session_with_two_segments(client, monkeypatch)

    def fake_complete(config, system, user, **kwargs):
        if 'evaluation_gaps' in user:
            payload = _summary_payload(risks=9, actions=8)  # 线上实测：9 条高危 + 8 条行动
        else:
            payload = dict(critique='偏故事化。', missing=['量化数据'], model_answer='示范。', bad_answer_note='别讲故事。')
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{session_id}/finish", json={})
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{session_id}").json()['session']
    assert session['status'] == 'done', session
    assert len(session['report']['risk_list']) == 8  # 截到上限，而不是整份报「汇总无效」
    assert len(session['report']['actions']) == 6
    assert len(session['report']['questions']) == 2


def test_report_retries_invalid_summary_once(client, monkeypatch):
    """真垃圾（非 JSON）自动重试一次；两次都坏才报「请重试」。"""
    session_id = _session_with_two_segments(client, monkeypatch)
    summary_calls = {'n': 0}

    def fake_complete(config, system, user, **kwargs):
        if 'evaluation_gaps' not in user:
            return json.dumps(dict(critique='点评。', missing=[], model_answer='示范。', bad_answer_note='坑。'), ensure_ascii=False), {}
        summary_calls['n'] += 1
        if summary_calls['n'] == 1:
            return '抱歉，我无法输出 JSON。', {}
        return json.dumps(_summary_payload(), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{session_id}/finish", json={})
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{session_id}").json()['session']
    assert session['status'] == 'done'
    assert summary_calls['n'] == 2  # 第一次垃圾 + 重试成功


def test_report_emits_progress_events(client, monkeypatch):
    """复盘渐进流：start 带全部题目清单（铺骨架），每条 review 带完整点评内容（原位填充），summary 收尾。"""
    session_id = _session_with_two_segments(client, monkeypatch)

    def fake_complete(config, system, user, **kwargs):
        if 'evaluation_gaps' in user:
            payload = _summary_payload()
        else:
            payload = dict(critique='点评。', missing=[], model_answer='示范。', bad_answer_note='坑。')
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{session_id}/finish", json={})
    join_run(client, finished.json()['run_id'])
    handle = client.app.state.interview_runs.get(finished.json()['run_id'])
    progress = [payload for kind, payload in handle.events if kind == 'progress']
    manifest = progress[0]
    assert manifest['stage'] == 'start' and manifest['total'] == 2
    assert [item['index'] for item in manifest['items']] == [0, 1]
    assert all(item['topic'] == '项目深挖' and item['question'] for item in manifest['items'])
    reviews = [item for item in progress if item['stage'] == 'review']
    assert sorted(item['index'] for item in reviews) == [0, 1]
    assert [item['done'] for item in reviews] == [1, 2]
    assert all(item['review']['critique'] == '点评。' and item['review']['model_answer'] == '示范。' for item in reviews)
    assert progress[-1]['stage'] == 'summary'


def test_review_coerces_oversized_missing(client, monkeypatch):
    """回归（线上 2026-11-21）：逐题点评给了 9 条 missing（上限 6）被整条作废换占位——
    模型几千 token 的点评不该为条数截掉；软校验截到 6 保住内容。"""
    session_id = _session_with_two_segments(client, monkeypatch)

    def fake_complete(config, system, user, **kwargs):
        if 'evaluation_gaps' in user:
            payload = _summary_payload()
        else:
            payload = dict(critique='真点评：讲得太浅。', missing=[f'缺要素{i}' for i in range(9)],
                           model_answer='高分示范。', bad_answer_note='别讲故事。')
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{session_id}/finish", json={})
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{session_id}").json()['session']
    assert session['status'] == 'done'
    for question in session['report']['questions']:
        assert question['critique'].startswith('真点评')  # 真点评保住，不是「生成失败，跳过」占位
        assert len(question['missing']) == 6  # 截到上限
        assert question['model_answer'] == '高分示范。'


def test_review_retries_truncated_json_once(client, monkeypatch):
    """回归（线上 2026-11-21）：输出=4000 精确触顶 → JSON 截断「Unterminated string」。
    逐题点评截断/垃圾自动重试一次，两次都坏才落占位。"""
    session_id = _session_with_two_segments(client, monkeypatch)
    review_calls = {'n': 0}

    def fake_complete(config, system, user, **kwargs):
        if 'evaluation_gaps' in user:
            payload = _summary_payload()
        else:
            review_calls['n'] += 1
            if review_calls['n'] == 1:
                return '{"critique": "截断在半路', {}  # 线上实录：max_tokens 截断的半截 JSON
            payload = dict(critique='重试后的真点评。', missing=['量化数据'], model_answer='示范。', bad_answer_note='坑。')
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    finished = client.post(f"/api/interview/sessions/{session_id}/finish", json={})
    join_run(client, finished.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{session_id}").json()['session']
    assert session['status'] == 'done'
    critiques = [question['critique'] for question in session['report']['questions']]
    assert critiques == ['重试后的真点评。', '重试后的真点评。']  # 无一占位
    assert review_calls['n'] == 3  # 2 题 + 1 次重试


def test_plan_retries_invalid_json_once(client, monkeypatch):
    """出题截断/垃圾自动重试一次：出题失败=整场判死，一次抽风不该让用户删场重开。"""
    calls = {'n': 0}

    def fake_complete(config, system, user, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return '{"opening": "截断', {}
        return json.dumps(fake_plan(), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    response = client.post('/api/interview/sessions', json=dict(persona_id='product', company_style='generic'))
    assert response.status_code == 200
    join_run(client, response.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{response.json()['session_id']}").json()['session']
    assert session['status'] == 'ready'
    assert calls['n'] == 2


def test_interview_respects_channel_output_budget(client, monkeypatch):
    """渠道级 max_output_tokens 用户自定义优先于调用点默认：超限问题用户自己可调，不被硬编码卡死。"""
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    channel = client.post('/api/settings/llm-channels', json=dict(
        name='预算渠道', channel='custom', protocol='openai', url='https://budget.example/v1',
        model='budget-model', key='sk-x', max_output_tokens=5000)).json()['channel']
    assert channel['max_output_tokens'] == 5000
    assert client.put('/api/settings/bindings', json=dict(
        purpose='analysis', kind='channel', channel_id=channel['id'], model='budget-model')).status_code == 200
    budgets = []

    def fake_complete(config, system, user, **kwargs):
        budgets.append(kwargs.get('max_tokens'))
        return json.dumps(fake_plan(), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    response = client.post('/api/interview/sessions', json=dict(persona_id='product'))
    join_run(client, response.json()['run_id'])
    session = client.get(f"/api/interview/sessions/{response.json()['session_id']}").json()['session']
    assert session['status'] == 'ready'
    assert budgets == [5000]  # 渠道自定义生效，而非默认 PLAN_MAX_TOKENS=32000