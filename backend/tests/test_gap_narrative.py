"""空窗期叙事（资料库 gap 条目 → 简历段落/面试应答）与空窗期面试专题卡。

生成走规则兜底（测试环境未配模型）断言三段产物与版本落库；模型路径用 httpx 拦截
（同 test_resume_workshop 范式）断言协议解析与坏输出回退。面试侧断言 gap 画像卡、
材料空窗块注入与面试官 system 的专题区块，不碰出题模型。
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.gap_narrative import _split_narrative, entries_block
from jobscope.interview import build_interviewer_system, build_persona, persona_cards
from jobscope.main import create_app
from jobscope.repository import Repository

import pytest


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def create_gap(client, title='备考与接单', content='考取 PMP 证书；完成两个小程序外包项目并上线。', **extra):
    body = dict(category='gap', title=title, period='2025-06 至 2026-05', content=content, **extra)
    response = client.post('/api/profile-entries', json=body)
    assert response.status_code == 200
    return response.json()


MODEL_REPLY = ('<<<PITCH>>>空窗期完成认证与两个上线项目，现以准备好的状态回到求职节奏。\n'
               '<<<RESUME>>>- 2025-06 至 2026-05｜职业空窗期：系统考取 PMP 认证；独立交付两个小程序外包项目并上线。\n'
               '- 同期保持技术手感：完成需求拆解、开发与交付全流程各 2 次。\n'
               '<<<INTERVIEW>>>Q：这段时间你在做什么？\n这段时间我主要做了两件事：'
               '一是系统学习项目管理并通过 PMP 认证；二是接了两个小程序外包项目，从需求到上线完整交付。')


def configure_model(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')


def mock_model(monkeypatch, content):
    captured = {}
    original = httpx.Client.post

    def post(self, url, **kwargs):
        payload = kwargs.get('json') or {}
        if 'messages' not in payload:
            return original(self, url, **kwargs)
        captured['system'] = payload['messages'][0]['content']
        captured['user'] = payload['messages'][1]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    return captured


def test_gap_category_roundtrip(client):
    entry = create_gap(client)
    assert entry['category'] == 'gap'
    listed = client.get('/api/profile-entries').json()['entries']
    assert any(item['category'] == 'gap' for item in listed)
    assert client.post('/api/profile-entries', json=dict(category='vacation', title='x', content='y')).status_code == 422
    patched = client.patch(f"/api/profile-entries/{entry['id']}", json=dict(category='gap', content='更新后的事实'))
    assert patched.status_code == 200


def test_generate_requires_enabled_gap_entries(client):
    assert client.post('/api/profile/gap-narrative', json={}).status_code == 400
    entry = create_gap(client)
    client.patch(f"/api/profile-entries/{entry['id']}", json=dict(enabled=False))
    assert client.post('/api/profile/gap-narrative', json={}).status_code == 400
    client.patch(f"/api/profile-entries/{entry['id']}", json=dict(enabled=True))
    result = client.post('/api/profile/gap-narrative', json={})
    assert result.status_code == 200
    narrative = result.json()
    # 无模型环境走规则骨架：三段产物齐全、事实只来自条目、来源如实标注。
    assert narrative['origin'] == 'rules'
    payload = narrative['payload']
    assert payload['pitch'] and payload['resume_block'] and payload['interview_block']
    assert 'PMP' in payload['resume_block']
    assert payload['entry_ids'] == [entry['id']]
    listed = client.get('/api/profile/gap-narratives').json()['narratives']
    assert [item['id'] for item in listed] == [narrative['id']]


def test_model_path_parses_protocol_and_bad_output_falls_back(client, monkeypatch):
    create_gap(client)
    configure_model(monkeypatch)
    captured = mock_model(monkeypatch, MODEL_REPLY)
    narrative = client.post('/api/profile/gap-narrative', json=dict(job_title='产品经理')).json()
    assert narrative['origin'] == 'model'
    assert '电梯陈述' not in narrative['payload']['pitch']
    assert 'PMP 认证' in narrative['payload']['interview_block']
    user_payload = json.loads(captured['user'])
    assert 'PMP' in user_payload['gap_entries'] and user_payload['job_title'] == '产品经理'
    # 坏输出（协议缺失）→ 整体回退规则骨架，不 500 不给空产物。
    mock_model(monkeypatch, '对不起，我无法完成这个任务。')
    fallback = client.post('/api/profile/gap-narrative', json={}).json()
    assert fallback['origin'] == 'rules'
    assert fallback['payload']['resume_block'] and fallback['payload']['interview_block']


def test_split_narrative_tolerates_marker_order():
    pitch, resume, interview = _split_narrative(MODEL_REPLY)
    assert pitch.startswith('空窗期') and 'PMP' in resume and interview.startswith('Q：')
    with pytest.raises(ValueError):
        _split_narrative('没有协议标记的普通回答')


def test_gap_persona_and_material_block(repo):
    cards, _styles = persona_cards()
    gap_card = next(card for card in cards if card['id'] == 'gap')
    assert 'hr' in [item['id'] for item in gap_card['rounds']]
    persona = build_persona('gap', 'hr')
    assert '空窗期专题' in persona['directive']
    repo.save_profile_entry(dict(category='gap', title='备考与接单', period='2025-06', content='考取 PMP；交付两个小程序。'))
    block = entries_block(repo)
    assert '【空窗经历】' in block and 'PMP' in block
    plan = dict(questions=[dict(topic='空窗核实', question='这段时间你在做什么？', focus='具体事实',
                                followups=['产出是什么'], source='gap')])
    system = build_interviewer_system(persona, dict(gap_digest=block), plan)
    assert '空窗期事实' in system and 'PMP' in system and '空窗期专题' in system
    # 未申报空窗素材：明确提示不引用不存在的事实，不编造。
    system_without = build_interviewer_system(persona, {}, plan)
    assert '不引用不存在的事实' in system_without
