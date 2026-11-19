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

    def fake_stream_turn(config, messages, tools, *, on_delta=None, on_reasoning=None, should_stop=None, register_abort=None, timeout=60):
        reply = answers[min(calls['n'], len(answers) - 1)]
        calls['n'] += 1
        if on_delta:
            on_delta(reply['content'])
        return dict(role='assistant', content=reply['content'],
                    tool_calls=[dict(id=f'c{calls["n"]}', type='function',
                                     function=dict(name='advance', arguments=reply.get('advance', advance_args())))]), {}, ''

    monkeypatch.setattr('jobscope.interview_runs.stream_turn', fake_stream_turn)


def patch_report(monkeypatch):
    monkeypatch.setattr('jobscope.interview_runs.generate_report', lambda persona, segments, material, should_stop=None: dict(
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
    body = dict(persona_id='ali_product', **extra)
    response = client.post('/api/interview/sessions', json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    join_run(client, data['run_id'])
    return data


def test_create_requires_model(bare_client):
    assert bare_client.post('/api/interview/sessions', json=dict(persona_id='ali_product')).status_code == 400


def test_personas_cards(client):
    cards = client.get('/api/interview/personas').json()['cards']
    assert {card['id'] for card in cards} >= {'ali_product', 'hr_interviewer'}
    assert all(card['rounds'] for card in cards)


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
    data = client.post('/api/interview/sessions', json=dict(persona_id='ali_product')).json()
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
