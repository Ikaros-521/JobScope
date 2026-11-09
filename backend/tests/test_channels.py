"""Channel presets, live model discovery and actual protocol dispatch."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def intercept(monkeypatch, method, handler):
    original = getattr(httpx.Client, method)
    def wrapped(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        return handler(str(url), kwargs)
    monkeypatch.setattr(httpx.Client, method, wrapped)


def test_catalog_and_preset_resolve_without_manual_endpoint(client):
    catalog = client.get('/api/settings/channels').json()['channels']
    assert {'openai', 'anthropic', 'deepseek', 'aliyun', 'volcengine', 'zhipu', 'moonshot', 'siliconflow', 'custom'} <= {c['id'] for c in catalog}
    result = client.put('/api/settings/models', json={'llm': {'mode': 'remote', 'channel': 'deepseek', 'model': 'deepseek-chat'}})
    assert result.status_code == 200
    assert result.json()['llm']['url'] == 'https://api.deepseek.com/v1/chat/completions'
    assert result.json()['llm']['protocol'] == 'openai'
    assert client.put('/api/settings/models', json={'llm': {'mode': 'remote', 'channel': 'missing', 'model': 'x'}}).status_code == 422
    assert client.put('/api/settings/models', json={'embedding': {'mode': 'remote', 'channel': 'anthropic', 'protocol': 'anthropic', 'model': 'x'}}).status_code == 422


def test_model_discovery_uses_draft_no_model_required_and_never_saves(client, monkeypatch):
    calls = []
    def get(url, kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'data': [{'id': 'z-model'}, {'id': 'a-model'}, {'id': 'a-model'}, {'bad': 1}]}, request=httpx.Request('GET', url))
    intercept(monkeypatch, 'get', get)
    response = client.post('/api/settings/models/discover', json={'target': 'llm', 'config': {'mode': 'remote', 'channel': 'custom', 'url': 'https://gateway.example/v1', 'key': 'fixture-token'}})
    assert response.status_code == 200
    assert response.json()['models'] == ['a-model', 'z-model']
    assert calls[0][0] == 'https://gateway.example/v1/models'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer fixture-token'
    assert client.get('/api/settings/models').json()['llm']['mode'] == 'disabled'


def test_anthropic_test_plan_and_answer_use_messages_protocol(client, monkeypatch):
    calls = []
    def post(url, kwargs):
        calls.append((url, kwargs))
        body = kwargs['json']
        assert 'system' in body and all(m['role'] != 'system' for m in body['messages'])
        assert kwargs['headers']['x-api-key'] == 'fixture-token'
        assert kwargs['headers']['anthropic-version'] == '2023-06-01'
        assert 'Authorization' not in kwargs['headers']
        text = 'OK'
        if body['system']:
            data = json.loads(body['messages'][0]['content'])
            if 'baseline' in data:
                text = json.dumps(data['baseline'])
            elif 'evidence' in data:
                eid = data['evidence'][0]['id']
                text = json.dumps({'answer': f'需要Python [{eid}]', 'evidence_ids': [eid]})
        return httpx.Response(200, json={'content': [{'type': 'thinking', 'thinking': 'hidden'}, {'type': 'text', 'text': text}], 'usage': {'input_tokens': 10}}, request=httpx.Request('POST', url))
    intercept(monkeypatch, 'post', post)
    config = dict(mode='remote', channel='custom', protocol='anthropic', url='https://gateway.example/v1', model='claude-fixture', key='fixture-token')
    assert client.post('/api/settings/models/test', json={'target': 'llm', 'config': config}).json()['ok']
    assert client.put('/api/settings/models', json={'llm': config}).status_code == 200
    client.post('/api/import/job', json={'title': 'Python开发', 'company': '协议验收公司', 'description': '需要Python'})
    result = client.post('/api/chat', json={'scope': 'global', 'message': 'Python岗位要求'}).json()
    assert result['mode'] == 'model' and result['citations']
    assert len(calls) == 3 and all(url == 'https://gateway.example/v1/messages' for url, _ in calls)


def test_discovery_failure_is_actionable_and_credentials_stay_bound(client, monkeypatch):
    config = dict(mode='remote', channel='custom', protocol='openai', url='https://gateway.example/v1', model='x', key='fixture-token')
    client.put('/api/settings/models', json={'llm': config})
    def fail(url, kwargs):
        return httpx.Response(401, text='fixture-token', request=httpx.Request('GET', url))
    intercept(monkeypatch, 'get', fail)
    result = client.post('/api/settings/models/discover', json={'target': 'llm', 'config': {**config, 'key': ''}})
    assert result.status_code == 502 and 'fixture-token' not in result.text and '密钥' in result.text
    changed = {**config, 'url': 'https://other.example/v1', 'key': ''}
    assert client.post('/api/settings/models/discover', json={'target': 'llm', 'config': changed}).status_code == 422


def test_full_endpoints_are_not_duplicated_and_old_config_migrates(client):
    from jobscope.model_transport import endpoint
    assert endpoint('https://gateway.example/v1/chat/completions', 'openai', 'llm') == 'https://gateway.example/v1/chat/completions'
    assert endpoint('https://gateway.example/v1/messages', 'anthropic', 'models') == 'https://gateway.example/v1/models'
    assert endpoint('https://gateway.example', 'anthropic', 'llm') == 'https://gateway.example/v1/messages'
    assert endpoint('https://gateway.example/chat/completions', 'openai', 'llm') == 'https://gateway.example/chat/completions'
    assert endpoint('https://gateway.example/messages', 'anthropic', 'models') == 'https://gateway.example/models'
    assert endpoint('https://gateway.example/embeddings', 'openai', 'embedding') == 'https://gateway.example/embeddings'
    repo = client.app.state.repo
    with repo.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO model_settings VALUES (?,?)', ('llm', json.dumps(dict(mode='remote', url='https://old.example/v1/chat/completions', model='old', key='', local_model=''))))
    status = client.get('/api/settings/models').json()['llm']
    assert status['channel'] == 'custom' and status['protocol'] == 'openai' and status['model'] == 'old'


def test_legacy_base_url_keeps_key_on_same_endpoint(client):
    with client.app.state.repo.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO model_settings VALUES (?,?)', ('llm', json.dumps(dict(mode='remote', url='https://old.example/v1', model='old', key='legacy-token', local_model=''))))
    result = client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://old.example/v1/chat/completions', model='new')})
    assert result.status_code == 200 and result.json()['llm']['key_set']
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', protocol='anthropic', url='https://old.example/v1', model='new')}).status_code == 422


@pytest.mark.parametrize('body', [{'content': [None]}, {'content': {}}, {'content': [{'type': 'thinking'}]}, None])
def test_anthropic_malformed_response_is_sanitized(client, monkeypatch, body):
    intercept(monkeypatch, 'post', lambda url, kwargs: httpx.Response(200, json=body, request=httpx.Request('POST', url)))
    result = client.post('/api/settings/models/test', json={'target': 'llm', 'config': dict(mode='remote', channel='anthropic', model='fixture')})
    assert result.status_code == 502 and '连接测试失败' in result.text


def test_anthropic_fit_uses_messages_and_keeps_fact_validation(monkeypatch):
    from jobscope.semantic import analyze_semantically
    from jobscope.settings import _active, environment
    config = environment()
    config['llm'].update(mode='remote', protocol='anthropic', url='https://gateway.example/v1', model='fixture')
    def reply(url, kwargs):
        assert url.endswith('/messages')
        body = kwargs['json']
        assert body['system'] and len(body['messages']) == 1
        data = json.loads(body['messages'][0]['content'])
        assert data['facts'][0]['id'] == 'F-1'
        text = json.dumps(dict(requirements=[dict(requirement='Python', jd_quote='Python', status='met', fact_ids=['F-1'], dimension='skill', hard=False, rationale='已提供Python事实')], suggestions=[]))
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': text}]}, request=httpx.Request('POST', url))
    intercept(monkeypatch, 'post', reply)
    token = _active.set(config)
    try:
        report = analyze_semantically(dict(id='J-fixture', title='开发', company='甲', description='Python', evidence_ids=[]), dict(text='facts:\n  - id: F-1\n    status: confirmed\n    statement: Python开发\n', format='facts_yaml', revision=1), [])
        assert report['score'] == 100 and report['requirements'][0]['evidence'] == ['F-1']
    finally:
        _active.reset(token)


def test_connection_test_leaves_room_for_reasoning_before_answer(client, monkeypatch):
    def reply(url, kwargs):
        budget = kwargs['json']['max_tokens']
        enough = budget >= 512
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop' if enough else 'length', 'message': {'content': 'OK' if enough else '', 'reasoning_content': 'internal reasoning'}}]}, request=httpx.Request('POST', url))
    intercept(monkeypatch, 'post', reply)
    response = client.post('/api/settings/models/test', json={'target': 'llm', 'config': dict(mode='remote', url='https://gateway.example/v1', model='reasoning-fixture')})
    assert response.status_code == 200 and response.json()['ok']


def test_truncated_test_reports_budget_instead_of_bad_credentials(client, monkeypatch):
    intercept(monkeypatch, 'post', lambda url, kwargs: httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '', 'reasoning_content': 'hidden reasoning'}}]}, request=httpx.Request('POST', url)))
    response = client.post('/api/settings/models/test', json={'target': 'llm', 'config': dict(mode='remote', url='https://gateway.example/v1', model='reasoning-fixture')})
    assert response.status_code == 502 and '输出额度' in response.json()['detail']
    assert 'hidden reasoning' not in response.text
