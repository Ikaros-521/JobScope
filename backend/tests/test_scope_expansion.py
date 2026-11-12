import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.chat_runs import RunHandle
from jobscope.extensions import ExtensionStore
from jobscope.main import create_app
from jobscope.scope_expansion import ScopeExpansionStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def seed(client, kind='job'):
    repo = client.app.state.repo
    job = repo.save_job(dict(title='Python', company='甲公司', description='Python RAG', external_id='scope-job'), 'user_import')
    scope = dict(kind=kind)
    if kind != 'global':
        scope['company'] = job['company']
    if kind == 'job':
        scope['job_id'] = job['id']
    cid = repo.create_conversation(scope, model='saved-model', llm_channel='saved-channel')
    repo.update_conversation(cid, title='原对话标题')
    repo.append_messages(cid, [dict(role='user', content='原问题', compacted=True),
                              dict(role='assistant', content='摘要', kind='compaction', trace={'scope': scope}),
                              dict(role='assistant', content='回答', reasoning='完整元数据', citations=[{'id': 'E-test'}])])
    return cid, scope


def propose(client, cid, target='company'):
    response = client.post(f'/api/conversations/{cid}/scope-expansions', json={'target': target, 'reason': '比较更多岗位'})
    assert response.status_code == 200, response.text
    return response.json()


def accept(client, cid, action):
    return client.post(f"/api/conversations/{cid}/scope-expansions/{action['id']}/accept")


def raw_row(repo, cid):
    with repo.connection() as conn:
        return dict(conn.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone())


def test_proposal_does_not_clone_and_targets_only_source_company(client):
    cid, source = seed(client)
    before = raw_row(client.app.state.repo, cid)
    action = propose(client, cid)
    assert action == dict(id=action['id'], type='scope_expansion', source_scope=source,
                          target_scope={'kind': 'company', 'company': '甲公司'}, reason='比较更多岗位', target_conversation_id=None)
    assert raw_row(client.app.state.repo, cid) == before
    assert client.get('/api/conversations', params={'scope': 'company', 'company': '甲公司'}).json()['conversations'] == []
    assert client.get(f'/api/conversations/{cid}').json()['scope_expansions'] == [action]
    assert client.post(f'/api/conversations/{cid}/scope-expansions', json={'target': 'company', 'company': '乙公司'}).status_code == 422


@pytest.mark.parametrize('kind,target,expected', [('job', 'global', 200), ('company', 'global', 200),
    ('company', 'company', 400), ('global', 'global', 400), ('global', 'company', 400), ('job', 'job', 422)])
def test_only_strict_scope_widening_is_accepted(client, kind, target, expected):
    cid, _ = seed(client, kind)
    assert client.post(f'/api/conversations/{cid}/scope-expansions', json={'target': target}).status_code == expected


def test_accept_copies_complete_snapshot_private_state_and_lineage_without_source_mutation(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    extensions = ExtensionStore(repo)
    extensions.mark_private(cid)
    action = propose(client, cid)
    # Snapshot is taken at acceptance, including history added after the proposal.
    repo.append_messages(cid, [dict(role='user', content='最后一条消息')])
    before = raw_row(repo, cid)
    result = accept(client, cid, action)
    assert result.status_code == 200, result.text
    target = result.json()['conversation_id']
    cloned = raw_row(repo, target)
    assert cloned['messages'] == before['messages']
    assert (cloned['title'], cloned['model'], cloned['llm_channel']) == (before['title'], before['model'], before['llm_channel'])
    assert cloned['mode'] == 'general' and extensions.is_private(target)
    assert raw_row(repo, cid) == before
    detail = client.get(f'/api/conversations/{target}').json()
    assert detail['active_run'] is None and detail['scope_expansions'] == []
    assert detail['lineage'] == {'source_conversation_id': cid, 'action_id': action['id']}
    assert client.get(f'/api/conversations/{cid}').json()['scope_expansions'][0]['target_conversation_id'] == target
    repo.append_messages(target, [dict(role='user', content='新范围问题')])
    assert raw_row(repo, cid) == before


def test_accept_does_not_truncate_large_history_or_rewrite_legacy_messages(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    with repo.connection() as conn:
        conn.execute('UPDATE conversations SET messages=? WHERE id=?',
                     (json.dumps([dict(role='user', content=str(i), compacted=i < 100) for i in range(220)]), cid))
    before = raw_row(repo, cid)
    action = propose(client, cid, 'global')
    result = accept(client, cid, action).json()
    assert raw_row(repo, result['conversation_id'])['messages'] == before['messages']
    assert raw_row(repo, cid) == before


def test_accept_is_idempotent_across_concurrent_stores_and_api_retries(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    action = propose(client, cid)
    stores = [ScopeExpansionStore(repo) for _ in range(8)]
    gate = threading.Barrier(len(stores))
    def clone(store):
        gate.wait()
        return store.accept(cid, action['id'])
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(clone, stores))
    assert all(result == results[0] for result in results)
    assert accept(client, cid, action).json() == results[0]
    assert len(repo.list_conversations(action['target_scope'])) == 1


@pytest.mark.parametrize('busy', ['active', 'approval', 'compression', 'persisted_approval'])
def test_accept_rejects_unstable_source_and_can_retry(client, busy):
    repo, runs = client.app.state.repo, client.app.state.runs
    cid, _ = seed(client)
    action = propose(client, cid)
    handle = RunHandle('R-busy', cid, {})
    if busy in {'active', 'approval'}:
        handle.done = busy == 'approval'
        handle.pending_approval = busy == 'approval'
        runs._register(handle)
    elif busy == 'compression':
        assert runs.begin_compression(cid)
    else:
        ExtensionStore(repo).pending('R-pending', dict(conversation_id=cid))
    before = raw_row(repo, cid)
    assert accept(client, cid, action).status_code == 409
    assert raw_row(repo, cid) == before
    handle.done, handle.pending_approval = True, False
    runs.end_compression(cid)
    ExtensionStore(repo).finish_pending('R-pending')
    assert accept(client, cid, action).status_code == 200


def test_expired_persisted_approval_does_not_block_expansion_forever(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    action = propose(client, cid)
    ExtensionStore(repo).pending('R-expired', dict(conversation_id=cid))
    with repo.connection() as conn:
        conn.execute('UPDATE agent_pending SET created=0 WHERE run_id=?', ('R-expired',))
    assert accept(client, cid, action).status_code == 200


def test_action_belongs_to_source_and_server_revalidates_scope(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    other, _ = seed(client, 'company')
    action = propose(client, cid)
    assert accept(client, other, action).status_code == 404
    assert accept(client, cid, {'id': 'unknown'}).status_code == 404
    assert client.post('/api/conversations/missing/scope-expansions', json={'target': 'global'}).status_code == 404
    with repo.connection() as conn:
        conn.execute('UPDATE conversations SET scope=? WHERE id=?', (json.dumps({'kind': 'global'}), cid))
    assert accept(client, cid, action).status_code == 409


def test_detail_restores_message_action_status_without_mutating_source_history(client):
    repo = client.app.state.repo
    cid, _ = seed(client)
    action = propose(client, cid)
    repo.append_messages(cid, [dict(role='assistant', content='扩展建议', actions=[action])])
    before = raw_row(repo, cid)
    target = accept(client, cid, action).json()['conversation_id']
    detail = client.get(f'/api/conversations/{cid}').json()
    assert detail['messages'][-1]['actions'][0]['target_conversation_id'] == target
    clone_detail = client.get(f'/api/conversations/{target}').json()
    assert clone_detail['messages'][-1]['actions'][0]['target_conversation_id'] == target
    assert accept(client, cid, action).json()['conversation_id'] == target
    assert raw_row(repo, cid) == before


@pytest.mark.parametrize('endpoint', ['/api/chat', '/api/chat/runs'])
@pytest.mark.parametrize('mode', [None, 'research', 'general'])
def test_all_configured_entry_points_use_scoped_general_runtime_and_persist_actions(client, monkeypatch, endpoint, mode):
    from jobscope.general_agent import GeneralAgent
    repo = client.app.state.repo
    job = repo.save_job(dict(title='Python', company='甲公司', description='Python', external_id='runtime-job'), 'user_import')
    client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture')})
    calls = []
    def start(self, message, history, conversation_id, hooks=None, run_id=None, scope=None, permission_mode='autonomous'):
        calls.append(scope)
        action = ScopeExpansionStore(repo).propose(conversation_id, 'company', '查看公司资料')
        if hooks:
            hooks.on_event({'tool': 'request_scope_expansion', 'action': action})
        return dict(answer='建议扩展', mode='agent', actions=[action], trace=dict(id=run_id, scope=scope, status='completed'))
    monkeypatch.setattr(GeneralAgent, 'start', start)
    payload = dict(message='查看相关公司岗位', job_id=job['id'])
    if mode:
        payload['agent_mode'] = mode
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    if endpoint.endswith('/runs'):
        handle = client.app.state.runs.get(result['run_id'])
        handle.thread.join(timeout=5)
        assert any(kind == 'step' and event.get('action') for kind, event in handle.events)
    detail = client.get(f"/api/conversations/{result['conversation_id']}").json()
    expected = dict(kind='job', company='甲公司', job_id=job['id'])
    assert calls == [expected] and detail['scope'] == expected and detail['mode'] == 'general'
    assert detail['messages'][-1]['actions'] == detail['scope_expansions']
    assert detail['messages'][-1]['trace']['scope'] == expected
    # The legacy endpoint and run endpoint can continue each other's canonical history.
    wrong = client.post(endpoint, json={**payload, 'conversation_id': result['conversation_id'], 'job_id': None, 'company': '甲公司'})
    assert wrong.status_code == 409


def test_default_general_requires_model_and_explicit_research_retains_offline_fallback(client):
    for endpoint in ('/api/chat', '/api/chat/runs'):
        body = dict(message='离线资料')
        if endpoint == '/api/chat':
            body['scope'] = 'global'
        assert client.post(endpoint, json=body).status_code == 400
        response = client.post(endpoint, json={**body, 'agent_mode': 'research'})
        assert response.status_code == 200
        if endpoint.endswith('/runs'):
            client.app.state.runs.get(response.json()['run_id']).thread.join(timeout=5)


def test_busy_run_rejects_edit_before_mutating_history(client):
    repo, runs = client.app.state.repo, client.app.state.runs
    cid, scope = seed(client)
    before = raw_row(repo, cid)
    handle = RunHandle('R-active', cid, {})
    runs._register(handle)
    with pytest.raises(ValueError, match='正在生成'):
        runs.start(message='编辑', conversation_id=cid, mode='research', scope=scope,
                   edit_message_id=json.loads(before['messages'])[0]['id'])
    assert raw_row(repo, cid) == before
    assert not runs.begin_compression(cid)
