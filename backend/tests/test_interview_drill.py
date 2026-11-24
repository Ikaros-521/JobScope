"""错题强化：错题本抽取/跨场合并/引用解析、定向出题与兜底、面试官暗线、复盘前后对照。

模型调用全部打桩（传输层 complete + stream_turn），数据 tmp_path 隔离。
"""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope import drill
from jobscope.interview import build_interviewer_system, build_question_plan, review_segment
from jobscope.main import create_app

MOCK_Q1 = '视频拆条里你用信号量控制 LLM 子任务的并发，这个并发上限是怎么确定的？'
MOCK_Q2 = '讲讲 AgentBot 项目里你个人负责哪部分？'
MOCK_Q3 = '先做个自我介绍吧。'
REAL_Q = '先简单介绍一下你做过哪些项目，每个项目你主要负责什么。'
REAL_Q_LONG = REAL_Q + '还有别的要补充吗？'  # 同一场真实面试二次导入：追问被并进题面（前缀相同、更长）
VARIANT_A = '换个场景：新服务的并发控制你怎么设计？'
VARIANT_B = '最近一个项目里你亲自做了什么？'

PERSONA = dict(company='大厂风格', company_style=dict(values=['数据驱动'], probing='x'), role_label='产品',
               role=dict(focus='需求落地', mix=[], label='产品'), round_label='业务面（一面）',
               round_style=dict(focus='业务理解'), intensity='standard', intensity_label='标准',
               job_title='产品经理', company_name='甲公司')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    with TestClient(create_app(tmp_path)) as value:
        yield value


@pytest.fixture
def repo(client):
    return client.app.state.repo


# ---- 造数：直接落带复盘的记录（错题本取数只读 report 与记录头，不必真跑一遍面试） ----

def seed_mock(repo, questions, *, session_id='I-seed', company='甲公司', job_id='J-1', job_title='数据分析师',
              created_at='2026-11-20T10:00:00+00:00', critique='偏故事化。'):
    """questions: [(题面, 缺失要素 missing, 高分示范)]"""
    report = dict(summary='总评：项目有料但缺数据。', dimensions=[], risk_list=['补一组前后对比数据'], actions=['改简历'],
                  reviewed=len(questions), generated_at=created_at,
                  questions=[dict(topic='项目深挖', question=question, critique=critique, missing=list(missing),
                                  model_answer=answer, bad_answer_note='别只讲故事。')
                             for question, missing, answer in questions])
    repo.save_interview_session(dict(
        id=session_id, title=f'{company} · {job_title} 模拟面试', job_id=job_id, company=company, job_title=job_title,
        persona=dict(name='软件研发工程师', round_label='业务面（一面）'), material={}, config={},
        question_plan=None, progress=dict(phase='wrapup', current_index=0, segments=[], rounds=0),
        status='done', report=report, conversation_id='', created_at=created_at))
    return session_id


def seed_real(repo, questions, *, transcript_id, company='常佰岁', job_id='J-2', job_title='产线稳定性工程师',
              created_at='2026-11-21T10:00:00+00:00'):
    """questions: [(题面, 问题列表 bad)]"""
    report = dict(overall_score=3, summary='回答有料但缺数字。', strengths=['有项目'], problems=['零量化'],
                  jd_match=dict(fit_score=3, evidence=[], gaps=[]), actions=['先结论后细节'],
                  reviewed=len(questions), generated_at=created_at,
                  questions=[dict(question=question, critique='偏功能罗列。', good=['有项目'], bad=list(bad),
                                  suggestion='先结论后细节。') for question, bad in questions])
    repo.save_interview_transcript(dict(
        id=transcript_id, job_id=job_id, job_title=job_title, company=company, text='', segments=[],
        labeling_meta={}, status='done', report=report, error='', created_at=created_at))
    return transcript_id


def seed_library(repo):
    """一份模拟复盘（两道错题 + 一道没失分的题，挂在真实岗位档案上）+ 同一场真实面试的两次导入。"""
    job = repo.save_job(dict(title='数据分析师', company='甲公司', description='负责指标体系与实验设计', location='杭州'), 'user_import')
    seed_mock(repo, [(MOCK_Q1, ['压测规模与方法', '量化指标口径'], '先讲压测口径。'),
                     (MOCK_Q2, ['我的具体动作', '结果收益'], '讲清你负责的边界。'),
                     (MOCK_Q3, [], '')], session_id='I-mock', job_id=job['id'])
    seed_real(repo, [(REAL_Q, ['全程零量化：没有团队规模'])], transcript_id='T-real1')
    seed_real(repo, [(REAL_Q_LONG, ['没有一句关于自己角色和职责的内容'])], transcript_id='T-real2',
              created_at='2026-11-22T10:00:00+00:00')


def by_question(payload):
    return {item['question']: item for item in payload['items']}


# ---- 错题本：抽取与合并 ----

def test_weak_points_merges_same_question_across_reports(repo):
    seed_library(repo)
    payload = drill.weak_points(repo)
    items = by_question(payload)
    # 没被点失分点的题不进错题本；同一场真实面试的两次导入（题面一长一短）合并成一条
    assert set(items) == {MOCK_Q1, MOCK_Q2, REAL_Q_LONG}
    merged = items[REAL_Q_LONG]
    assert merged['occurrences'] == 2 and merged['source'] == 'real'
    assert merged['problems'] == ['没有一句关于自己角色和职责的内容', '全程零量化：没有团队规模']  # 新→旧取并集
    assert [ref['session_id'] for ref in merged['refs']] == ['T-real2', 'T-real1']
    assert merged['label'] == '常佰岁 · 产线稳定性工程师' and merged['job_id'] == 'J-2'
    # 最近的在前；同一场内保持题序
    assert [item['question'] for item in payload['items']] == [REAL_Q_LONG, MOCK_Q1, MOCK_Q2]
    assert payload['total'] == 3 and payload['limitations'] == []


def test_weak_points_tags_and_cross_session_patterns(repo):
    seed_library(repo)
    payload = drill.weak_points(repo)
    items = by_question(payload)
    assert items[MOCK_Q1]['tags'] == ['quant', 'basis']      # 量化指标口径 / 压测
    assert items[MOCK_Q2]['tags'] == ['action', 'result']    # 我的具体动作 / 结果收益
    # 只在 ≥2 道错题里出现的才算跨场通病；同次数按 tag id 稳定排序
    assert [(item['tag'], item['count']) for item in payload['patterns']] == [('action', 2), ('quant', 2)]


def test_short_common_prefix_is_not_the_same_question(repo):
    first = '讲讲你在视频拆条项目里负责的并发控制是怎么设计的'
    second = '讲讲你在视频拆条项目里负责的异常重试是怎么设计的'
    seed_mock(repo, [(first, ['量化数据'], ''), (second, ['量化数据'], '')])
    assert set(by_question(drill.weak_points(repo))) == {first, second}


def test_placeholder_review_is_never_a_weak_point(repo):
    # 逐题点评失败的占位：missing 为空只是没点评出来，不能当成错题、也不能当成「补齐了」
    seed_mock(repo, [(MOCK_Q1, ['量化数据'], '')], critique='本条点评生成失败，跳过。')
    assert drill.weak_points(repo)['items'] == []


def test_weak_points_truncates_and_says_so(repo, monkeypatch):
    seed_library(repo)
    monkeypatch.setattr(drill, 'MAX_ITEMS', 2)
    payload = drill.weak_points(repo)
    assert len(payload['items']) == 2 and payload['total'] == 3
    assert payload['limitations'] == ['共 3 道错题，这里只列出最近的 2 道']


def test_weak_points_endpoint(client, repo):
    empty = client.get('/api/interview/weak-points')
    assert empty.status_code == 200
    assert empty.json()['items'] == [] and empty.json()['total'] == 0 and empty.json()['limitations'] == []
    assert {tag['id'] for tag in empty.json()['tags']} >= {'quant', 'action', 'basis'}  # 标签字典常驻，空库也给
    seed_library(repo)
    payload = client.get('/api/interview/weak-points').json()
    assert by_question(payload)[MOCK_Q1]['session_id'] == 'I-mock'
    assert 'advice' not in by_question(payload)[MOCK_Q1]  # 列表不带高分示范，保持轻量


# ---- 纯函数：对齐 / 兜底 / 对照 / 通病 ----

def test_ensure_coverage_fills_missing_and_drops_out_of_range():
    targets = [dict(question=f'原题{index}', problems=[f'失分点{index}']) for index in range(3)]
    questions = [dict(topic='变式', question='变式题甲', focus='x', followups=['y'], source='weakness', drill=0),
                 dict(topic='越界', question='越界题', focus='x', followups=['y'], source='weakness', drill=9)]
    plan = drill.ensure_coverage(questions, targets)
    assert [item['drill'] for item in plan] == [0, 1, 2]
    assert plan[0]['question'] == '变式题甲'  # 模型出的变式题保留
    assert plan[1]['question'] == '原题1' and plan[1]['followups'] == ['失分点1——具体说说']
    assert plan[1]['focus'] == '上次失分点：失分点1' and plan[1]['source'] == 'weakness'
    assert '越界题' not in json.dumps(plan, ensure_ascii=False)
    # 模型漏标 drill 但题数对得上：按序对应，变式题不因漏了个下标被整单作废
    unlabeled = [dict(question=f'变式题{index}', drill=None) for index in range(3)]
    mapped = drill.ensure_coverage(unlabeled, targets)
    assert [item['drill'] for item in mapped] == [0, 1, 2]
    assert [item['question'] for item in mapped] == ['变式题0', '变式题1', '变式题2']
    # 没有失分点的重练目标：兜底题给通用追问，不硬编失分点
    plain = drill.fallback_question(dict(question='用一分钟介绍你自己。'), 0)
    assert plain['focus'] and plain['followups'] == ['能展开讲讲吗？', '具体是你做的哪部分？']


def test_compare_stats_counts_only_reviewed_drills():
    report = dict(questions=[
        dict(question='a', drill_ref=dict(source='mock', session_id='s', index=0), previous_problems=['x', 'y'], missing=[], critique='比上次好。'),
        dict(question='b', drill_ref=dict(source='mock', session_id='s', index=1), previous_problems=['x'], missing=['量化数据'], critique='仍缺数据。'),
        dict(question='c', drill_ref=dict(source='mock', session_id='s', index=2), previous_problems=['x'], missing=[], critique='本条点评生成失败，跳过。'),
        dict(question='d', missing=['量化数据'], critique='常规题。')])
    assert drill.compare_stats(report) == dict(total=2, cleared=1, before=3, after=1)
    assert drill.compare_stats(dict(questions=[dict(question='d')])) is None
    assert drill.compare_stats(None) is None


def test_habit_note_needs_repeats():
    assert drill.habit_note([1, 2], [dict(label='缺量化', count=1)]) == ''
    assert drill.habit_note([1, 2, 3], [dict(label='缺量化', count=2)]) == '缺量化（2/3 道错题）'
    assert drill.habit_note([], [dict(label='缺量化', count=2)]) == ''


# ---- prompt：面试官暗线 / 逐题点评对照 ----

def test_interviewer_prompt_carries_dossier_without_model_answer():
    material = dict(gaps=[], jd_summary='', resume_digest='', habit_note='缺量化（2/3 道错题）',
                    drills=[dict(question='原题一', problems=['压测规模与方法', '量化指标口径', '资源侧测算过程', '第四条不该出现'],
                                 advice='高分示范：先讲压测口径…')])
    questions = [dict(topic='并发', question='变式题一', focus='压测依据', followups=['上限怎么定'], source='weakness', drill=0),
                 dict(topic='常规', question='常规题', focus='x', followups=['y'], source='jd'),
                 dict(topic='越界', question='越界题', focus='x', followups=['y'], source='weakness', drill=5)]
    system = build_interviewer_system(PERSONA, material, dict(questions=questions))
    assert '上次失分点（候选人不可见）：压测规模与方法；量化指标口径；资源侧测算过程' in system
    assert '第四条不该出现' not in system   # 每轮对话都重 build：只带压缩后的前三条
    assert '高分示范' not in system         # 上次的高分示范不进面试官 prompt（面试官只需要知道往哪追问）
    assert system.count('上次失分点') == 1  # 常规题与下标越界的题不挂失分点
    assert '跨场通病' in system and '缺量化（2/3 道错题）' in system
    plain = build_interviewer_system(PERSONA, dict(gaps=[], jd_summary='', resume_digest=''), dict(questions=questions[1:2]))
    assert '上次失分点' not in plain and '跨场通病' not in plain


def test_review_segment_compares_against_previous(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    seen = []

    def fake_complete(config, system, user, **kwargs):
        seen.append((system, json.loads(user)))
        return json.dumps(dict(critique='点评。', missing=[], model_answer='示范', bad_answer_note='提醒'), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    segment = dict(question=dict(topic='t', question='q', focus='f', followups=['fu']), answers=['我的回答'], notes=[],
                   previous=dict(ref=dict(source='mock', session_id='s', index=0), question='原题', problems=['压测规模与方法'],
                                 advice='上次示范', critique='上次点评'))
    review_segment(PERSONA, segment)
    system, payload = seen[0]
    assert payload['previous'] == dict(question='原题', problems=['压测规模与方法'], advice='上次示范', critique='上次点评')
    assert '前后对照' in system
    review_segment(PERSONA, {key: value for key, value in segment.items() if key != 'previous'})
    assert 'previous' not in seen[1][1] and '前后对照' not in seen[1][0]  # 常规场次的点评不变


# ---- 集成：打桩传输层，全链路跑「错题本 → 错题强化场 → 对话 → 复盘对照 → 回写错题本」 ----

SUMMARY = dict(summary='总评：比上次有进步。', dimensions=[dict(name='结构化表达', score=3, comment='尚可')],
               risk_list=['补一组前后对比数据'], actions=['改简历'])


def install_model(monkeypatch, *, plan=None, plan_error=False, review=None):
    """打桩传输层 complete：按 system 提示词分流到 出题/逐题点评/汇总，返回录下的每次调用（system + payload）。"""
    calls = []

    def fake_complete(config, system, user, **kwargs):
        payload = json.loads(user)
        calls.append(dict(system=system, payload=payload))
        if '首席出题官' in system:
            if plan_error:
                return '不是 JSON', {}
            return json.dumps(plan, ensure_ascii=False), {}
        if system.startswith('你是面试教练。根据题单元信息'):
            return json.dumps(review(payload), ensure_ascii=False), {}
        if system.startswith('你是面试教练主管'):
            return json.dumps(SUMMARY, ensure_ascii=False), {}
        raise AssertionError('未预期的 system 提示词：' + system[:30])

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    return calls


def patch_stream(monkeypatch, prompts):
    """面试官轮次：开场留在本题，之后每轮换题（题号随轮次推进），录下每轮的 system prompt。"""
    calls = {'n': 0}

    def fake_stream_turn(config, messages, tools, *, on_delta=None, on_reasoning=None, should_stop=None,
                         register_abort=None, timeout=60, max_tokens=None):
        prompts.append(messages[0]['content'])
        verdict = 'followup' if calls['n'] == 0 else 'next'
        advance = json.dumps(dict(verdict=verdict, question_index=max(calls['n'] - 1, 0)))
        reply = dict(content='好的，我们继续。', tool_calls=[dict(id='c1', type='function',
                                                                  function=dict(name='advance', arguments=advance))])
        calls['n'] += 1
        if on_delta:
            on_delta(reply['content'])
        return reply, {}, ''

    monkeypatch.setattr('jobscope.interview_runs.stream_turn', fake_stream_turn)


def join_run(client, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        handle = client.app.state.interview_runs.get(run_id)
        if handle is None or handle.done:
            if handle and handle.thread is not None:
                handle.thread.join(timeout=5)
            return handle
        time.sleep(0.02)
    raise AssertionError('面试运行超时未完成')


def read_session(client, session_id):
    return client.get(f'/api/interview/sessions/{session_id}').json()['session']


def answer(client, session_id, text=''):
    response = client.post(f'/api/interview/sessions/{session_id}/runs', json=dict(answer=text))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])


def refs_of(client, *questions):
    """每道错题取最近一次出现的引用（建场入参的形态）。"""
    items = by_question(client.get('/api/interview/weak-points').json())
    return [items[question]['refs'][0] for question in questions], items


def test_drill_session_end_to_end(client, repo, monkeypatch):
    seed_library(repo)
    refs, items = refs_of(client, MOCK_Q1, MOCK_Q2, REAL_Q_LONG)
    # 模型只出了两道题（漏了第三道错题）：ensure_coverage 必须补上兜底题
    calls = install_model(
        monkeypatch,
        plan=dict(opening='你好，我们开始。', questions=[
            dict(topic='并发', question=VARIANT_A, focus='压测依据与量化', followups=['上限怎么定'], source='weakness', drill=0),
            dict(topic='项目深挖', question=VARIANT_B, focus='讲清个人动作', followups=['你做了什么'], source='weakness', drill=1)]),
        review=lambda payload: dict(critique='对照上次。',
                                    missing=[] if '并发' in payload['question'] else ['量化数据'] if '亲自' in payload['question'] else ['团队规模'],
                                    model_answer='示范答法。', bad_answer_note='踩坑提醒。'))

    response = client.post('/api/interview/sessions', json=dict(persona_id='reinforce', drill=refs, job_id=items[MOCK_Q1]['job_id']))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session_id = response.json()['session_id']
    session = read_session(client, session_id)

    # 建场：题量=错题数、标题区分、材料带重练快照（含上次高分示范与原错题引用）和跨场通病
    assert session['status'] == 'ready', session
    assert session['title'] == '错题强化 · 甲公司 · 数据分析师'
    assert session['config']['question_count'] == 3 and session['persona']['preset_id'] == 'reinforce'
    assert [item['question'] for item in session['material']['drills']] == [MOCK_Q1, MOCK_Q2, REAL_Q_LONG]
    assert session['material']['drills'][0]['advice'] == '先讲压测口径。'
    assert session['material']['drills'][2]['ref'] == items[REAL_Q_LONG]['refs'][0]
    assert session['material']['habit_note'] == '个人动作不清（2/3 道错题）；缺量化（2/3 道错题）'
    # 出题：走错题专用规则；三道题各对一个错题下标，模型漏掉的第三道原题重问
    assert [item['drill'] for item in session['question_plan']['questions']] == [0, 1, 2]
    assert [item['question'] for item in session['question_plan']['questions']] == [VARIANT_A, VARIANT_B, REAL_Q_LONG]
    assert 'fallback' not in session['question_plan']
    plan_call = calls[0]
    assert '错题清单' in plan_call['system'] and plan_call['payload']['question_count'] == 3
    assert plan_call['payload']['drills'][0] == dict(question=MOCK_Q1, problems=['压测规模与方法', '量化指标口径'], advice='先讲压测口径。')
    assert plan_call['payload']['habits'] == session['material']['habit_note']

    # 对话：面试官每轮 prompt 带「上次失分点」暗线与跨场通病，但不带高分示范
    prompts = []
    patch_stream(monkeypatch, prompts)
    answer(client, session_id)
    for text in ('我做过并发控制。', '我负责链路设计。', '我做过三个项目。'):
        answer(client, session_id, text)
    assert len(prompts) == 4 and all('上次失分点（候选人不可见）' in prompt for prompt in prompts)
    assert '压测规模与方法；量化指标口径' in prompts[1] and '跨场通病' in prompts[1]
    assert '先讲压测口径' not in prompts[1]

    # 复盘：逐题带上次失分档案（点评先对照），报告带上次失分点与原错题引用，汇总多一句重练效果
    response = client.post(f'/api/interview/sessions/{session_id}/finish')
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session = read_session(client, session_id)
    assert session['status'] == 'done', session
    report = session['report']
    assert [item['previous_problems'] for item in report['questions']] == [
        ['压测规模与方法', '量化指标口径'], ['我的具体动作', '结果收益'],
        ['没有一句关于自己角色和职责的内容', '全程零量化：没有团队规模']]
    assert report['questions'][2]['drill_ref'] == items[REAL_Q_LONG]['refs'][0]
    assert report['drill_stats'] == dict(total=3, cleared=1, before=6, after=2)
    reviews = [call for call in calls if call['system'].startswith('你是面试教练。根据题单元信息')]
    assert len(reviews) == 3 and all('前后对照' in call['system'] for call in reviews)
    assert all('previous_problems' not in call['payload'] and 'previous' in call['payload'] for call in reviews)
    assert '重练效果' in calls[-1]['system'] and calls[-1]['payload']['reviews'][0]['previous_problems']
    # 失分档案只在复盘时挂到新列表上，没有写回进度片段
    assert all('previous' not in segment for segment in session['progress']['segments'])

    # 回写错题本：原错题带上重练状态；强化场自己的变式题不作为新错题进本
    after = by_question(client.get('/api/interview/weak-points').json())
    assert set(after) == {MOCK_Q1, MOCK_Q2, REAL_Q_LONG}
    assert after[MOCK_Q1]['practice']['before'] == 2 and after[MOCK_Q1]['practice']['after'] == 0
    assert after[MOCK_Q2]['practice']['after'] == 1 and after[REAL_Q_LONG]['practice']['after'] == 1
    assert {question: after[question]['practice_count'] for question in after} == {MOCK_Q1: 1, MOCK_Q2: 1, REAL_Q_LONG: 1}


def test_regular_session_unchanged_by_drill_feature(client, monkeypatch):
    # 模型在常规场次多给了一个 drill 字段：接受但不外泄，题单/会话快照与引入错题强化之前一致
    calls = install_model(monkeypatch, plan=dict(opening='你好。', questions=[
        dict(topic='项目深挖', question='讲讲你的项目', focus='真实性', followups=['数据呢'], source='general', drill=7)]))
    response = client.post('/api/interview/sessions', json=dict(persona_id='product'))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session = read_session(client, response.json()['session_id'])
    assert session['status'] == 'ready' and session['title'].endswith('模拟面试')
    assert session['question_plan'] == dict(opening='你好。', questions=[
        dict(topic='项目深挖', question='讲讲你的项目', focus='真实性', followups=['数据呢'], source='general')])
    assert 'drills' not in session['material'] and 'habit_note' not in session['material']
    assert '错题清单' not in calls[0]['system'] and 'drills' not in calls[0]['payload'] and 'habits' not in calls[0]['payload']


def test_drill_planning_failure_falls_back_to_original_questions(client, repo, monkeypatch):
    seed_library(repo)
    refs, _items = refs_of(client, MOCK_Q1, MOCK_Q2)
    calls = install_model(monkeypatch, plan_error=True)
    response = client.post('/api/interview/sessions', json=dict(persona_id='reinforce', drill=refs))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session = read_session(client, response.json()['session_id'])
    # 出题模型两次都坏：错题强化不判死，原题重问也要让用户练上
    assert session['status'] == 'ready', session
    assert session['question_plan']['fallback'] is True
    assert [item['question'] for item in session['question_plan']['questions']] == [MOCK_Q1, MOCK_Q2]
    assert session['question_plan']['questions'][0]['followups'] == ['压测规模与方法——具体说说', '量化指标口径——具体说说']
    assert len(calls) == 2  # 重试一次后即退兜底，不再继续打模型


def test_drill_input_validation(client, repo, monkeypatch):
    install_model(monkeypatch, plan=dict(opening='你好。', questions=[
        dict(topic='项目深挖', question='讲讲你做过什么项目。', focus='真实性', followups=['数据呢'], source='weakness', drill=0)]))
    seed_library(repo)
    refs, _items = refs_of(client, MOCK_Q1, REAL_Q_LONG)
    ref = refs[0]

    def create(**extra):
        response = client.post('/api/interview/sessions', json=dict(persona_id='reinforce', **extra))
        return response.status_code, response.json().get('detail', '')

    assert create() == (400, '错题强化需要先选择要重练的错题')
    blocked = client.post('/api/interview/sessions', json=dict(persona_id='product', drill=[ref]))
    assert blocked.status_code == 400 and '错题强化官' in blocked.json()['detail']
    assert create(drill=[dict(source='mock', session_id='I-gone', index=0)]) == (400, '所选错题已不在复盘记录里（记录被删除或下标失效），请重新选择')
    assert create(drill=[dict(source='mock', session_id='I-mock', index=99)])[0] == 400
    assert create(drill=[dict(source='mock', session_id='I-mock', index=-1)])[0] == 422
    assert create(drill=[dict(source='paper', session_id='I-mock', index=0)])[0] == 422
    assert create(drill=[dict(source='mock', session_id='I-mock', index=index % 3) for index in range(13)])[0] == 422
    # 没被点过失分点的题不在错题本里，但「重练此题」是用户明示：照常放行
    allowed = client.post('/api/interview/sessions', json=dict(
        persona_id='reinforce', drill=[dict(source='mock', session_id='I-mock', index=2)]))
    assert allowed.status_code == 200, allowed.text
    join_run(client, allowed.json()['run_id'])  # 收尾：别让 worker 线程带着已拆卸的桩跨测试泄漏


def test_duplicate_imports_count_as_one_drill(client, repo, monkeypatch):
    install_model(monkeypatch, plan=dict(opening='你好。', questions=[
        dict(topic='项目职责', question=VARIANT_B, focus='职责边界', followups=['你做了什么'], source='weakness', drill=0)]))
    seed_library(repo)
    _heads, items = refs_of(client, REAL_Q_LONG)
    refs = items[REAL_Q_LONG]['refs']
    assert [ref['session_id'] for ref in refs] == ['T-real2', 'T-real1']  # 同一场真实面试的两次导入
    response = client.post('/api/interview/sessions', json=dict(persona_id='reinforce', drill=refs + refs))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    session = read_session(client, response.json()['session_id'])
    # 同一道题的多个引用（哪怕重复提交）只算一条错题、出一道题
    assert session['config']['question_count'] == 1 and len(session['material']['drills']) == 1
    assert session['material']['drills'][0]['problems'] == items[REAL_Q_LONG]['problems']


def test_personas_include_drill_card(client):
    payload = client.get('/api/interview/personas').json()
    card = next(item for item in payload['cards'] if item['id'] == 'reinforce')
    assert card['name'] == '错题强化官' and card['default_round'] == 'business' and card['rounds']


def test_build_question_plan_regular_payload_has_no_drill_keys(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    seen = []

    def fake_complete(config, system, user, **kwargs):
        seen.append((system, json.loads(user)))
        return json.dumps(dict(opening='你好。', questions=[
            dict(topic='项目深挖', question='讲讲你的项目', focus='真实性', followups=['数据呢'], source='general')]), ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    plan = build_question_plan(PERSONA, dict(jd_summary='', gaps=[], resume_digest=''), question_count=4)
    system, payload = seen[0]
    assert '错题清单' not in system and 'drills' not in payload and 'habits' not in payload
    assert plan == dict(opening='你好。', questions=[
        dict(topic='项目深挖', question='讲讲你的项目', focus='真实性', followups=['数据呢'], source='general')])
