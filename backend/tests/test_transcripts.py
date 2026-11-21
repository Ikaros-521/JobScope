"""真实面试复盘：逐字稿导入 / 说话人标注 / 人工核对 / 复盘报告（模型调用打桩，数据 tmp_path 隔离）。

两条配置挂接金丝雀照 test_interview 模式：标注 worker 与复盘线程池都在后台线程，
contextvar 不跨线程——生效配置必须显式挂，否则回落环境变量 →「地址为空」。
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 配置分析模型 env：purpose_configured('analysis') 命中后导入/标注/复盘都走后台 run。
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    with TestClient(create_app(tmp_path)) as value:
        yield value


@pytest.fixture
def bare_client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


SAMPLE_TEXT = '面试官：请先自我介绍，再讲一个最有代表性的项目。' * 6  # ≥50 字

FAKE_SEGMENTS = [
    dict(speaker='interviewer_1', text='请讲讲你最有代表性的项目。'),
    dict(speaker='me', text='我做过会员增长项目，把下单转化率提升了 12%。'),
    dict(speaker='interviewer_1', text='这个 12% 的口径是怎么算的？'),
]


def patch_label(monkeypatch, segments=None):
    monkeypatch.setattr('jobscope.transcript_runs.label_speakers', lambda text, **kwargs: segments or FAKE_SEGMENTS)


def fake_retro_report(question_count):
    """复盘产物：与 generate_retro_report 同形（questions 数随拆解题数）。"""
    return dict(overall_score=4, summary='回答有数据但结构松散。',
                strengths=['有量化意识，主动给数字'], problems=['先细节后结论，结构欠佳'],
                jd_match=dict(fit_score=4, evidence=['提到转化率提升 12%'], gaps=['没讲数据口径与样本量']),
                actions=['回答先结论后细节'], limitations=[],
                questions=[dict(question=f'问题{index}', critique='回答有数据。', good=['有量化'], bad=['口径略浅'], suggestion='先结论后细节。')
                           for index in range(question_count)])


def patch_report(monkeypatch, reporter=None):
    def default_reporter(segments, jd_context, job_meta, *, should_stop=None, on_progress=None):
        report = fake_retro_report(2)
        if on_progress:
            on_progress(dict(stage='start', total=2, items=[dict(index=0, question='问题0'), dict(index=1, question='问题1')]))
            on_progress(dict(stage='review', done=1, total=2, index=0, question='问题0', review=report['questions'][0]))
            on_progress(dict(stage='review', done=2, total=2, index=1, question='问题1', review=report['questions'][1]))
            on_progress(dict(stage='summary', done=2, total=2))
        return report
    monkeypatch.setattr('jobscope.transcript_runs.generate_retro_report', reporter or default_reporter)


def gated_reporter(gate):
    """阻塞式复盘：gate 放行前 run 一直挂着（测并发闸门用），返回值避开 `wait() or X` 的布尔短路陷阱。"""
    def reporter(segments, jd_context, job_meta, *, should_stop=None, on_progress=None):
        gate.wait(timeout=5)
        return fake_retro_report(2)
    return reporter


def gated_label(gate):
    def label(text):
        gate.wait(timeout=5)
        return FAKE_SEGMENTS
    return label


def join_run(client, run_id, timeout=5):
    handle = client.app.state.transcript_runs.get(run_id)
    if handle and handle.thread is not None:
        handle.thread.join(timeout=timeout)


def make_job(repo, title='数据分析师', company='甲公司'):
    return repo.save_job(dict(title=title, company=company,
                              description='负责指标体系与实验设计，要求 SQL 与业务归因能力', location='杭州'), 'user_import')


def import_transcript(client, monkeypatch, **extra):
    job = make_job(client.app.state.repo)
    patch_label(monkeypatch)
    body = dict(job_id=job['id'], text=SAMPLE_TEXT)
    body.update(extra)
    response = client.post('/api/interview/transcripts', json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    join_run(client, data['run_id'])
    return data, job


# ---- 入口校验 ----

def test_import_requires_model(bare_client):
    assert bare_client.post('/api/interview/transcripts', json=dict(job_id='j1', text=SAMPLE_TEXT)).status_code == 400


def test_import_unknown_job_404(client):
    assert client.post('/api/interview/transcripts', json=dict(job_id='missing', text=SAMPLE_TEXT)).status_code == 404


def test_import_rejects_short_text(client):
    job = make_job(client.app.state.repo)
    assert client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text='太短了')).status_code == 400


def test_import_labels_and_persists(client, monkeypatch):
    data, job = import_transcript(client, monkeypatch)
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'labeled'
    assert [seg['index'] for seg in transcript['segments']] == [0, 1, 2]
    assert transcript['segments'][1]['speaker'] == 'me'
    assert transcript['job_id'] == job['id'] and transcript['job_title'] == '数据分析师'
    assert transcript['labeling_meta']['segment_count'] == 3
    # 摘要列表带上锚点与状态
    summary = next(item for item in client.get('/api/interview/transcripts').json()['transcripts']
                   if item['id'] == data['transcript_id'])
    assert summary['status'] == 'labeled' and summary['segment_count'] == 3 and summary['job_title'] == '数据分析师'


def test_sse_replays_label_events(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    handle = client.app.state.transcript_runs.get(data['run_id'])
    assert [kind for kind, _payload in handle.events] == ['start', 'done']


def test_relabel_reruns_and_loses_manual_fixes(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    # 人工修正后再重标：段落被标注结果覆盖（前端须确认提示）
    client.patch(f"/api/interview/transcripts/{data['transcript_id']}/segments",
                 json=dict(segments=[dict(speaker='me', text='我改成候选人了。')]))
    patch_label(monkeypatch, FAKE_SEGMENTS)
    response = client.post(f"/api/interview/transcripts/{data['transcript_id']}/relabel")
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'labeled' and len(transcript['segments']) == 3


def test_relabel_rejected_when_done(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    patch_report(monkeypatch)
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    join_run(client, started.json()['run_id'])
    assert client.post(f"/api/interview/transcripts/{data['transcript_id']}/relabel").status_code == 409


# ---- 人工核对 ----

def test_patch_segments_persists_and_reindexes(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    response = client.patch(f"/api/interview/transcripts/{data['transcript_id']}/segments", json=dict(segments=[
        dict(speaker='interviewer_1', text='第一题，讲讲项目。'),
        dict(speaker='me', text='我改对了说话人。'),
        dict(speaker='me', text='  '),  # 全空白段丢弃
    ]))
    assert response.status_code == 200, response.text
    transcript = response.json()['transcript']
    assert transcript['status'] == 'labeled'
    assert [seg['index'] for seg in transcript['segments']] == [0, 1]  # 服务端按序重编，空段不占号
    assert transcript['segments'][1]['text'] == '我改对了说话人。'


def test_patch_rejected_while_reporting(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    gate = threading.Event()
    patch_report(monkeypatch, reporter=gated_reporter(gate))
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    assert started.status_code == 200, started.text
    assert client.patch(f"/api/interview/transcripts/{data['transcript_id']}/segments",
                        json=dict(segments=[dict(speaker='me', text='迟到的修改')])).status_code == 409
    gate.set()
    join_run(client, started.json()['run_id'])


# ---- 复盘报告 ----

def test_report_requires_labeled(client, monkeypatch):
    job = make_job(client.app.state.repo)
    gate = threading.Event()
    monkeypatch.setattr('jobscope.transcript_runs.label_speakers', gated_label(gate))
    response = client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text=SAMPLE_TEXT))
    transcript_id = response.json()['transcript_id']
    # 标注还没跑完（labeling 中）：不能出报告
    assert client.post(f"/api/interview/transcripts/{transcript_id}/report").status_code == 409
    gate.set()
    join_run(client, response.json()['run_id'])


def test_report_progressive_events_and_persist(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    patch_report(monkeypatch)
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    assert started.status_code == 200, started.text
    join_run(client, started.json()['run_id'])
    handle = client.app.state.transcript_runs.get(started.json()['run_id'])
    kinds = [kind for kind, _payload in handle.events]
    assert kinds == ['start', 'progress', 'progress', 'progress', 'progress', 'done']
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'done'
    assert transcript['report']['overall_score'] == 4
    assert transcript['report']['jd_match']['fit_score'] == 4
    assert len(transcript['report']['questions']) == 2
    assert transcript['report']['reviewed'] == 2
    # done 状态可重新生成（重新出报告）
    again = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    assert again.status_code == 200
    join_run(client, again.json()['run_id'])


def test_report_failure_rolls_back_status(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)

    def broken_reporter(*args, **kwargs):
        raise ValueError('问答拆解结果无效，请重试')

    patch_report(monkeypatch, reporter=broken_reporter)
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    assert started.status_code == 200, started.text
    join_run(client, started.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'labeled'  # 无旧报告 → 回 labeled，可重试
    handle = client.app.state.transcript_runs.get(started.json()['run_id'])
    assert handle.error == '问答拆解结果无效，请重试'


def test_report_on_done_keeps_old_report_on_failure(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    patch_report(monkeypatch)
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    join_run(client, started.json()['run_id'])
    patch_report(monkeypatch, reporter=lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('汇总无效')))
    again = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    join_run(client, again.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'done'  # 带旧报告的重生成失败 → 回 done，旧报告保留
    assert transcript['report']['overall_score'] == 4


def test_delete_blocked_while_active(client, monkeypatch):
    data, _ = import_transcript(client, monkeypatch)
    gate = threading.Event()
    patch_report(monkeypatch, reporter=gated_reporter(gate))
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    assert started.status_code == 200, started.text
    assert client.delete(f"/api/interview/transcripts/{data['transcript_id']}").status_code == 409
    gate.set()
    join_run(client, started.json()['run_id'])
    assert client.delete(f"/api/interview/transcripts/{data['transcript_id']}").status_code == 200


def test_report_without_job_notes_limitation(client, monkeypatch):
    data, job = import_transcript(client, monkeypatch)
    client.app.state.repo.delete_jobs([job['id']])
    patch_report(monkeypatch)
    started = client.post(f"/api/interview/transcripts/{data['transcript_id']}/report")
    join_run(client, started.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{data['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'done'
    assert '未结合 JD' in transcript['report']['limitations'][0]


# ---- 配置挂接金丝雀 ----

def test_label_worker_uses_bound_model_config(client, monkeypatch):
    """回归：标注 worker 在后台线程，contextvar 不跨线程——必须显式挂生效配置（delenv 模拟）。"""
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    assert client.put('/api/settings/models', json={
        'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture-model', protocol='openai'),
    }).status_code == 200
    recorded = []

    def fake_stream(config, system, user, *, on_delta=None, **kwargs):
        recorded.append(dict(url=config.get('url'), model=config.get('model')))
        content = json.dumps(dict(segments=[dict(speaker='me', text='你好，我是候选人。'),
                                            dict(speaker='interviewer_1', text='请讲讲项目。')]), ensure_ascii=False)
        step = max(1, len(content) // 3)  # 分 3 片流式喂入：跨片对象的增量解析走真实调用链
        for i in range(0, len(content), step):
            if on_delta:
                on_delta(content[i:i + step])
        return content, {}

    # label_speakers 内延迟 import stream_complete：patch 传输层才覆盖真实调用链
    monkeypatch.setattr('jobscope.model_transport.stream_complete', fake_stream)
    job = make_job(client.app.state.repo)
    response = client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text=SAMPLE_TEXT))
    assert response.status_code == 200, response.text
    join_run(client, response.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{response.json()['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'labeled'
    assert recorded, '标注没有走到模型调用'
    assert recorded[0]['url'] == 'https://fixture.example/v1/chat/completions'
    assert recorded[0]['model'] == 'fixture-model'


def test_report_pipeline_threads_carry_config(client, monkeypatch):
    """回归：复盘全链路（拆解/池内逐题点评/汇总）都走真实 transcript 管线——每处 complete 都拿到生效配置。

    池内线程与 worker 线程都必须非空 url；打桩按 system 前缀分发四类调用。
    """
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    assert client.put('/api/settings/models', json={
        'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture-model', protocol='openai'),
    }).status_code == 200
    recorded = []

    def fake_stream(config, system, user, *, on_delta=None, **kwargs):
        recorded.append((threading.get_ident(), config.get('url')))
        content = json.dumps(dict(segments=[dict(speaker='interviewer_1', text='讲讲你最有代表性的项目。'),
                                            dict(speaker='me', text='会员增长项目，转化率提升 12%。'),
                                            dict(speaker='interviewer_1', text='口径是什么？'),
                                            dict(speaker='me', text='下单转化，A/B 实验得出。')]), ensure_ascii=False)
        for i in range(0, len(content), 40):
            if on_delta:
                on_delta(content[i:i + 40])
        return content, {}

    def fake_complete(config, system, user, **kwargs):
        recorded.append((threading.get_ident(), config.get('url')))
        if system.startswith('你是面试复盘的结构分析员'):
            payload = dict(items=[dict(question='讲讲项目', segments=[0, 1]), dict(question='口径', segments=[2, 3])])
        elif system.startswith('你是面试教练。根据题目'):
            payload = dict(critique='有数据。', good=['有量化'], bad=['口径略浅'], suggestion='先结论后细节。')
        else:  # 汇总轮
            payload = dict(overall_score=4, summary='有数据。', strengths=['量化'], problems=['结构'],
                           fit_score=4, jd_evidence=['转化率 12%'], jd_gaps=['口径'], actions=['先结论'])
        return json.dumps(payload, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.model_transport.stream_complete', fake_stream)
    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)
    job = make_job(client.app.state.repo)
    imported = client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text=SAMPLE_TEXT))
    join_run(client, imported.json()['run_id'])
    started = client.post(f"/api/interview/transcripts/{imported.json()['transcript_id']}/report")
    assert started.status_code == 200, started.text
    join_run(client, started.json()['run_id'])
    transcript = client.get(f"/api/interview/transcripts/{imported.json()['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'done'
    assert len(transcript['report']['questions']) == 2
    # 标注(worker) + 拆解(worker) + 逐题×2(池子线程) + 汇总(worker)：全链路无一空 url，且确实跨线程
    assert len(recorded) == 5
    assert all(url == 'https://fixture.example/v1/chat/completions' for _ident, url in recorded), recorded
    assert len({ident for ident, _url in recorded}) >= 2


# ---- 流式标注：解析器（纯单元） ----

def test_stream_label_parser_extracts_across_chunks():
    """分片喂入（7 字一片，对象边界与字符串内部都被切开）：每个对象闭合即产出，最终与整段一致。"""
    from jobscope.transcript import _StreamLabelParser
    parser = _StreamLabelParser()
    payload = dict(segments=[dict(speaker='me', text='你好。'), dict(speaker='interviewer_2', text='讲讲项目。')])
    content = '```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```'
    emitted = []
    for i in range(0, len(content), 7):
        emitted.extend(parser.feed(content[i:i + 7]))
    assert [(seg['speaker'], seg['text']) for seg in emitted] == [('me', '你好。'), ('interviewer_2', '讲讲项目。')]
    assert parser.segments == emitted


def test_stream_label_parser_skips_junk_and_truncated_tail():
    """围栏/前言杂讯天然跳过；尾部对象未闭合（截断）不产出，已完成段不受影响。"""
    from jobscope.transcript import _StreamLabelParser
    parser = _StreamLabelParser()
    assert parser.feed('好的，以下是标注：\n{"segments": [{"text": "第一段", "speaker": "面试官A"},') \
        == [dict(speaker='interviewer_1', text='第一段')]
    assert parser.feed('{"text": "被截断的半截') == []
    assert len(parser.segments) == 1


def test_stream_label_parser_shared_unknown_speaker_numbering():
    """无编号称呼跨段共享 seen：考官A → interviewer_1，考官B → interviewer_2。"""
    from jobscope.transcript import _StreamLabelParser
    parser = _StreamLabelParser()
    parser.feed('{"text": "考官A说话", "speaker": "考官A"}')
    assert parser.feed('{"text": "考官B说话", "speaker": "考官B"}') == [dict(speaker='interviewer_2', text='考官B说话')]
    assert parser.segments[0]['speaker'] == 'interviewer_1'


def test_stream_label_parser_braces_inside_text():
    """字符串内部的大括号/转义不破坏切分（in_string 状态机保护）。"""
    from jobscope.transcript import _StreamLabelParser
    parser = _StreamLabelParser()
    tricky = json.dumps(dict(segments=[dict(speaker='me', text='伪对象 {"x":1} 收尾 \\" 引号')]), ensure_ascii=False)
    assert parser.feed(tricky) == [dict(speaker='me', text='伪对象 {"x":1} 收尾 \\" 引号')]


# ---- 归一化与软校验（纯单元） ----

def test_norm_speaker_mapping():
    from jobscope.transcript import _norm_speaker
    seen = {}
    assert _norm_speaker('me', seen) == 'me'
    assert _norm_speaker('候选人', seen) == 'me'
    assert _norm_speaker('Interviewer_2', seen) == 'interviewer_2'
    assert _norm_speaker('面试官2', seen) == 'interviewer_2'
    assert _norm_speaker('说话人10', seen) == 'interviewer_5'  # 超编封顶
    assert _norm_speaker('考官A', seen) == 'interviewer_1'     # 无编号按出场顺序分配
    assert _norm_speaker('考官B', seen) == 'interviewer_2'
    assert _norm_speaker('考官A', seen) == 'interviewer_1'     # 同称呼同编号


def test_stream_label_parser_normalizes_and_truncates():
    """流式段对象同款软校验：空文本丢弃、超编封顶、超长截断、坏对象跳过。"""
    from jobscope.transcript import _StreamLabelParser
    parser = _StreamLabelParser()
    raw = ('{"segments": [{"text": "  ", "speaker": "me"},'                    # 空文本 → 丢
           '{"text": "' + '长' * 5000 + '", "speaker": "interviewer_9"},'      # 超编封顶 + 截断
           '{"text": "你好", "speaker": "面试官A"},'
           '"垃圾项",{"no_text": 1}]}')
    found = parser.feed(raw)
    assert found == [dict(speaker='interviewer_5', text='长' * 4000), dict(speaker='interviewer_1', text='你好')]


def test_label_stream_progress_events(client, monkeypatch):
    """流式事件契约：start → label_reset → label_segment×N（带 done 计数）→ done 换装。"""
    job = make_job(client.app.state.repo)

    def fake_label(text, *, on_event=None, should_stop=None):
        if on_event:
            on_event('reset')
            for seg in FAKE_SEGMENTS:
                on_event('segment', seg)
        return FAKE_SEGMENTS

    monkeypatch.setattr('jobscope.transcript_runs.label_speakers', fake_label)
    response = client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text=SAMPLE_TEXT))
    join_run(client, response.json()['run_id'])
    handle = client.app.state.transcript_runs.get(response.json()['run_id'])
    kinds = [kind for kind, _payload in handle.events]
    assert kinds == ['start', 'progress', 'progress', 'progress', 'progress', 'done']  # start + reset + 3段 + done
    payloads = [payload for kind, payload in handle.events if kind == 'progress']
    assert payloads[0]['stage'] == 'label_reset'
    assert [payload['stage'] for payload in payloads[1:]] == ['label_segment'] * 3
    assert payloads[1]['segment']['speaker'] == 'interviewer_1'
    assert payloads[-1]['done'] == 3
    transcript = client.get(f"/api/interview/transcripts/{response.json()['transcript_id']}").json()['transcript']
    assert transcript['status'] == 'labeled'


def test_label_partial_persist_every_20(client, monkeypatch):
    """流式节流落库：第 20 段触发存档（状态仍 labeling），重启自愈与断线续看都有数据可依。"""
    job = make_job(client.app.state.repo)
    repo = client.app.state.repo
    gate = threading.Event()

    def fake_label(text, *, on_event=None, should_stop=None):
        if on_event:
            on_event('reset')
            for i in range(20):
                on_event('segment', dict(speaker='me', text=f'第{i}段'))
            gate.wait(timeout=5)  # 第 20 段已触发节流落库；主线程断言后放行
            on_event('segment', dict(speaker='me', text='第20段'))
        return [dict(speaker='me', text=f'第{i}段') for i in range(21)]

    monkeypatch.setattr('jobscope.transcript_runs.label_speakers', fake_label)
    response = client.post('/api/interview/transcripts', json=dict(job_id=job['id'], text=SAMPLE_TEXT))
    transcript_id = response.json()['transcript_id']
    deadline = time.time() + 5
    while time.time() < deadline:
        mid = repo.get_interview_transcript(transcript_id)
        if len(mid.get('segments') or []) == 20 and mid['status'] == 'labeling':
            break
        time.sleep(0.02)
    else:
        raise AssertionError('节流落库未发生（20 段应已进存档且状态仍 labeling）')
    gate.set()
    join_run(client, response.json()['run_id'])
    final = repo.get_interview_transcript(transcript_id)
    assert final['status'] == 'labeled' and len(final['segments']) == 21


def test_startup_sweep_repairs_stale_statuses(tmp_path, monkeypatch):
    """服务重启杀死了后台 run：labeling/reporting 存档在下次启动自愈，段落与报告不丢（关页/刷新无感）。"""
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test")
    from jobscope.main import create_app
    stamp = '2026-11-21T10:00:00'

    def row(transcript_id, status, segments, report=None):
        return dict(id=transcript_id, job_id='', job_title='', company='', raw_text='x', status=status,
                    error='', segments=segments, labeling_meta=None, report=report, created_at=stamp, updated_at=stamp)

    seg = dict(index=0, speaker='me', text='x')
    first = create_app(tmp_path)
    repo = first.state.repo
    repo.save_interview_transcript(row('T-l', 'labeling', [seg]))            # 标注中断有部分段落 → labeled
    repo.save_interview_transcript(row('T-e', 'labeling', []))               # 标注中断无段落 → imported
    repo.save_interview_transcript(row('T-r', 'reporting', [seg], report=dict(overall_score=4)))  # 复盘中断有旧报告 → done
    repo.save_interview_transcript(row('T-n', 'reporting', [seg]))           # 复盘中断无报告 → labeled
    second = create_app(tmp_path)  # 重启：sweep 在 TranscriptManager 构造时执行
    repo2 = second.state.repo
    repaired_l = repo2.get_interview_transcript('T-l')
    assert repaired_l['status'] == 'labeled' and '服务重启' in repaired_l['error']
    assert repo2.get_interview_transcript('T-e')['status'] == 'imported'
    assert repo2.get_interview_transcript('T-r')['status'] == 'done'
    assert repo2.get_interview_transcript('T-n')['status'] == 'labeled'
    assert repo2.get_interview_transcript('T-l')['segments'][0]['text'] == 'x'  # 部分结果保留
    first.state.transcript_runs  # noqa: B018 — 确认两个 app 各自持有 manager（互不影响）


def test_coerce_qa_validates_indices():
    from jobscope.transcript import _coerce_qa
    raw = dict(items=[dict(question='Q1', segments=[0, 99, 'x', 1, 0]),   # 越界/非数/重复全剔
                      dict(question='', segments=[0]),                     # 无问题名丢弃
                      dict(question='Q2', segments=[2])])
    items = _coerce_qa(raw, 3)
    assert [item['question'] for item in items] == ['Q1', 'Q2']
    assert items[0]['segments'] == [0, 1]
    assert items[1]['segments'] == [2]
