import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def create_entry(client, **overrides):
    body = dict(category='project', title='电商大促网关', org='甲科技', period='2023-2024',
                content='独立设计网关限流与灰度方案，峰值 QPS 4.2 万，大促零故障；沉淀公司级限流组件。',
                tags=['后端', '高并发'])
    body.update(overrides)
    return client.post('/api/profile-entries', json=body)


def test_profile_entry_crud_roundtrip(client):
    created = create_entry(client)
    assert created.status_code == 200
    entry = created.json()
    assert entry['id'].startswith('P-') and entry['enabled'] is True
    assert entry['tags'] == ['后端', '高并发']

    # 列表与单查；禁用条目在 enabled_only 下隐藏。
    assert client.get('/api/profile-entries').json()['total' if False else 'entries'].__len__() == 1
    assert len(client.get('/api/profile-entries', params={'enabled_only': 'true'}).json()['entries']) == 1
    client.patch(f"/api/profile-entries/{entry['id']}", json={'enabled': False})
    assert client.get('/api/profile-entries', params={'enabled_only': 'true'}).json()['entries'] == []
    patched = client.get('/api/profile-entries').json()['entries'][0]
    assert patched['enabled'] is False and patched['title'] == '电商大促网关'  # 部分更新不动其他字段

    # 整体替换（PUT）覆盖全部字段；PATCH 缺省字段保持不变。
    client.put(f"/api/profile-entries/{entry['id']}", json=dict(
        category='work', title='乙公司后端组', org='乙公司', period='2024-2025',
        content='负责结算服务', tags=None, enabled=True))
    replaced = client.get(f"/api/profile-entries/{entry['id']}").json()
    assert replaced['category'] == 'work' and replaced['title'] == '乙公司后端组' and replaced['tags'] == []

    assert client.delete(f"/api/profile-entries/{entry['id']}").json()['deleted'] == entry['id']
    assert client.get(f"/api/profile-entries/{entry['id']}").status_code == 404
    assert client.delete(f"/api/profile-entries/{entry['id']}").status_code == 404


def test_profile_entry_validates_input(client):
    assert create_entry(client, title='').status_code == 422
    assert create_entry(client, content='   ').status_code == 422
    assert create_entry(client, category='secret').status_code == 422
    assert client.put('/api/profile-entries/P-missing', json=dict(
        category='project', title='t', content='c')).status_code == 404
    assert client.patch('/api/profile-entries/P-missing', json={'enabled': True}).status_code == 404
    # tags 去重且缺省为空
    entry = create_entry(client, tags=['后端', '后端']).json()
    assert entry['tags'] == ['后端']
    entry = create_entry(client, tags=None).json()
    assert entry['tags'] == []


def mock_optimize_model(monkeypatch, content):
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


MODEL_REPLY = '<<<RESUME>>>\n# 张三\n\n## 项目经验\n- 电商大促网关\n<<<NOTES>>>\n- 取材资料库扩写项目经历'


def test_optimize_pulls_profile_library_materials(client, monkeypatch):
    """简历工坊取材个人资料库：选中条目进 prompt（完整细节供扩写），停用/未选不进。"""
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    captured = mock_optimize_model(monkeypatch, MODEL_REPLY)
    client.put('/api/resume', json=dict(text='# 张三\n\n## 项目经验\n- 电商大促网关（简历阉割版）', format='text'))
    project = create_entry(client).json()
    create_entry(client, title='不相关的爱好', category='other', content='业余摄影', enabled=False)

    result = client.post('/api/resume-versions/optimize', json=dict(
        jd_text='负责高并发网关服务', company='字节跳动', job_title='后端开发工程师',
        profile_entry_ids=[project['id'], 'P-gone']))
    assert result.status_code == 200
    payload = json.loads(captured['user'])
    # 完整版材料进 payload：标题/组织/时间与全文都在；停用与不存在的条目静默跳过。
    assert '【项目经历】电商大促网关｜甲科技｜2023-2024' in payload['profile_materials']
    assert '峰值 QPS 4.2 万' in payload['profile_materials']
    assert '业余摄影' not in payload['profile_materials']
    # 铁律修订：资料库是第二事实来源；未提供时明确只有基础简历。
    assert '个人资料库' in captured['system'] and '未提供个人资料库时' in captured['system']


def test_optimize_without_profile_ids_keeps_single_source_payload(client, monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    captured = mock_optimize_model(monkeypatch, MODEL_REPLY)
    client.put('/api/resume', json=dict(text='# 张三', format='text'))
    create_entry(client)
    result = client.post('/api/resume-versions/optimize', json=dict(jd_text='后端开发'))
    assert result.status_code == 200
    payload = json.loads(captured['user'])
    assert payload['profile_materials'] == ''
