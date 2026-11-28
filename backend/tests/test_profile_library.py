import json
import sqlite3
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.repository import Repository


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

    version, err = _optimize(client, dict(
        jd_text='负责高并发网关服务', company='字节跳动', job_title='后端开发工程师',
        profile_entry_ids=[project['id'], 'P-gone']))
    assert err is None
    payload = json.loads(captured['user'])
    # 完整版材料进 payload：标题/组织/时间与全文都在；停用与不存在的条目静默跳过。
    assert '【项目经历】电商大促网关｜甲科技｜2023-2024' in payload['profile_materials']
    assert '峰值 QPS 4.2 万' in payload['profile_materials']
    assert '业余摄影' not in payload['profile_materials']
    # 铁律修订：资料库是第二事实来源；未提供时明确只有基础简历。
    assert '个人资料库' in captured['system'] and '未提供个人资料库时' in captured['system']


def _optimize(client, payload):
    """起优化 run 并等 worker 收尾；返回 (version, error_message)。"""
    start = client.post('/api/resume-versions/optimize/runs', json=payload)
    handle = client.app.state.resume_optimize_runs.get(start.json()['run_id'])
    handle.thread.join(timeout=10)
    assert handle.done
    for kind, frame in handle.events:
        if kind == 'done':
            return frame['version'], None
    return None, handle.error


def test_optimize_without_profile_ids_keeps_single_source_payload(client, monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    captured = mock_optimize_model(monkeypatch, MODEL_REPLY)
    client.put('/api/resume', json=dict(text='# 张三', format='text'))
    create_entry(client)
    version, err = _optimize(client, dict(jd_text='后端开发'))
    assert err is None
    payload = json.loads(captured['user'])
    assert payload['profile_materials'] == ''


PREFERENCE_CONTENT = '接受做六休一，周日固定休息'


def test_preference_entries_feed_pipeline_not_workshop_material(client, monkeypatch):
    """求职偏好（category='preference'，原独立页并入）：启用即进评估事实池；工坊取材不作素材。"""
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    captured = mock_optimize_model(monkeypatch, MODEL_REPLY)
    client.put('/api/resume', json=dict(text='# 张三', format='text'))
    pref = create_entry(client, category='preference', title='求职偏好', content=PREFERENCE_CONTENT).json()
    repo = client.app.state.repo
    assert repo.preference_statements() == [{'id': pref['id'], 'statement': PREFERENCE_CONTENT}]

    # 停用即退出事实池；再启用恢复。材料类条目永不进事实池。
    client.patch(f"/api/profile-entries/{pref['id']}", json={'enabled': False})
    assert repo.preference_statements() == []
    client.patch(f"/api/profile-entries/{pref['id']}", json={'enabled': True})
    create_entry(client, title='真实材料', content='独立设计网关限流方案')
    assert repo.preference_statements() == [{'id': pref['id'], 'statement': PREFERENCE_CONTENT}]

    # 工坊取材：偏好条目即使被误传 id 也不进摘录块（双保险，前端勾选列表已排除）。
    version, err = _optimize(client, dict(
        jd_text='后端开发', profile_entry_ids=[pref['id']]))
    assert err is None
    assert '做六休一' not in json.loads(captured['user'])['profile_materials']
    # 原 /api/job-preferences 端点随旧表退役。
    assert client.get('/api/job-preferences').status_code == 404


def test_legacy_job_preferences_migrate_into_profile_entries(tmp_path):
    """升级迁移：旧 job_preferences 表并入 profile_entries（preference 类），旧表退役删除。"""
    repo_path = tmp_path / 'jobscope.sqlite3'
    conn = sqlite3.connect(repo_path)
    conn.execute('CREATE TABLE job_preferences (id TEXT PRIMARY KEY, dimension TEXT NOT NULL, '
                 'statement TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, '
                 'created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    conn.execute("INSERT INTO job_preferences (id, dimension, statement, enabled, created_at, updated_at) "
                 "VALUES ('PREF-old', 'skill', '五年 Go 经验', 1, '2026-01-01', '2026-01-02')")
    conn.execute("INSERT INTO job_preferences (id, dimension, statement, enabled, created_at, updated_at) "
                 "VALUES ('PREF-off', 'preference', '接受出差', 0, '2026-01-01', '2026-01-02')")
    conn.commit()
    conn.close()

    repo = Repository(tmp_path)
    entries = {item['id']: item for item in repo.profile_entries()}
    migrated = entries['PREF-old']
    assert migrated['category'] == 'preference' and migrated['title'] == '技能'
    assert migrated['content'] == '五年 Go 经验' and migrated['tags'] == ['skill']
    assert migrated['enabled'] is True
    assert entries['PREF-off']['enabled'] is False and entries['PREF-off']['title'] == '求职偏好'
    # 注入口径沿用：仅启用偏好进事实池，id 原样保留（已存报告 fact_ids 引用不失效）。
    assert repo.preference_statements() == [{'id': 'PREF-old', 'statement': '五年 Go 经验'}]

    with sqlite3.connect(repo_path) as verify:
        names = {row[0] for row in verify.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'job_preferences' not in names
    # 再次打开不重复迁移（旧表已不存在）。
    assert len(Repository(tmp_path).profile_entries()) == 2
