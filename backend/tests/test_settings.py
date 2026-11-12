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
    for key in ('LLM_URL', 'LLM_MODEL', 'LLM_KEY', 'EMBEDDING_URL', 'EMBEDDING_MODEL', 'EMBEDDING_KEY', 'EMBEDDING_LOCAL_MODEL'):
        monkeypatch.delenv('JOBSCOPE_' + key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def llm(**changes):
    return dict(mode='remote', url='https://model.example/v1/chat/completions', model='test-model', key='secret-fixture-key', **changes)


def test_settings_save_mask_retain_clear_and_restart(client, tmp_path):
    assert client.get('/api/settings/models').json()['llm']['mode'] == 'disabled'
    saved = client.put('/api/settings/models', json={'llm': llm()})
    assert saved.status_code == 200
    assert saved.json()['llm']['key_set'] is True
    assert 'secret-fixture-key' not in saved.text
    assert client.get('/api/health').json()['model_configured'] is True
    assert client.put('/api/settings/models', json={'llm': {'mode': 'remote', 'url': 'https://model.example/v1/chat/completions', 'model': 'changed', 'key': ''}}).status_code == 200
    with TestClient(create_app(tmp_path)) as restarted:
        status = restarted.get('/api/settings/models').json()
        assert status['llm']['model'] == 'changed' and status['llm']['key_set']
        cleared = restarted.put('/api/settings/models', json={'llm': {'mode': 'disabled', 'clear_key': True}}).json()
        assert cleared['llm']['key_set'] is False
        assert restarted.get('/api/health').json()['model_configured'] is False
    assert 'secret-fixture-key' not in client.get('/api/settings/models').text


def test_settings_isolated_between_apps_and_override_environment(client, tmp_path, monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://env.example/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'environment')
    assert client.get('/api/settings/models').json()['llm']['source'] == 'environment'
    client.put('/api/settings/models', json={'llm': {'mode': 'disabled'}})
    assert not client.get('/api/health').json()['model_configured']
    with TestClient(create_app(tmp_path / 'other')) as other:
        assert other.get('/api/health').json()['model_configured']


def test_unsaved_connection_test_uses_draft_and_saved_key_without_mutation(client, monkeypatch):
    client.put('/api/settings/models', json={'llm': llm()})
    calls = []
    original_post = httpx.Client.post
    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original_post(self, url, **kwargs)
        calls.append((url, kwargs))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    response = client.post('/api/settings/models/test', json={'target': 'llm', 'config': {'mode': 'remote', 'url': 'https://model.example/v1/chat/completions', 'model': 'draft-model', 'key': ''}})
    assert response.status_code == 200 and response.json()['ok']
    assert calls[0][1]['headers']['Authorization'] == 'Bearer secret-fixture-key'
    assert calls[0][1]['json']['model'] == 'draft-model'
    assert '简历' not in json.dumps(calls[0][1]['json'], ensure_ascii=False)
    assert client.get('/api/settings/models').json()['llm']['model'] == 'test-model'
    # Do not send an existing key to a different provider merely to test a draft.
    assert client.post('/api/settings/models/test', json={'target': 'llm', 'config': {'mode': 'remote', 'url': 'https://other.example/v1/chat/completions', 'model': 'draft'}}).status_code == 422


def test_settings_validation_and_provider_failure_never_echo_secrets(client, monkeypatch):
    invalid = client.put('/api/settings/models', json={'llm': {'mode': 'remote', 'url': 'http://remote.example/x', 'model': 'x', 'key': 'secret-fixture-key'}})
    assert invalid.status_code == 422 and 'secret-fixture-key' not in invalid.text
    assert client.get('/api/settings/models').json()['llm']['mode'] == 'disabled'
    original_post = httpx.Client.post
    def fail(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original_post(self, url, **kwargs)
        raise httpx.ConnectError('secret-fixture-key https://bad.example')
    monkeypatch.setattr(httpx.Client, 'post', fail)
    result = client.post('/api/settings/models/test', json={'target': 'llm', 'config': llm()})
    assert result.status_code == 502 and 'secret-fixture-key' not in result.text
    assert client.get('/api/settings/models').json()['llm']['mode'] == 'disabled'


def test_embedding_settings_switch_modes_and_runtime_uses_saved_config(client, monkeypatch):
    result = client.put('/api/settings/models', json={'embedding': {'mode': 'local', 'local_model': 'cached-local'}})
    assert result.status_code == 200
    assert client.get('/api/research/index').json()['embedding_local'] is True
    calls = []
    class Encoder:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            return [[1., .5] for _ in texts]
    monkeypatch.setattr('jobscope.research.providers.local_encoder', lambda name: Encoder())
    tested = client.post('/api/settings/models/test', json={'target': 'embedding', 'config': {'mode': 'local', 'local_model': 'cached-local'}})
    assert tested.status_code == 200 and tested.json()['dimensions'] == 2
    assert calls == ['连接测试']
    client.put('/api/settings/models', json={'embedding': {'mode': 'disabled'}})
    assert not client.get('/api/research/index').json()['embedding_configured']


def test_key_cannot_follow_url_change_through_disabled_mode(client):
    client.put('/api/settings/models', json={'llm': llm()})
    result = client.put('/api/settings/models', json={'llm': {'mode': 'disabled', 'url': 'https://other.example/v1/chat/completions'}})
    assert result.status_code == 422
    assert client.get('/api/settings/models').json()['llm']['url'] == 'https://model.example/v1/chat/completions'


def test_invalid_url_port_is_rejected_without_server_error(client):
    result = client.put('/api/settings/models', json={'llm': {'mode': 'remote', 'url': 'https://example.com:bad/v1/chat/completions', 'model': 'x'}})
    assert result.status_code == 422


@pytest.mark.parametrize('url,reason', [
    ('127.0.0.1:3425/v1', '缺少协议'),
    ('http://remote.example/v1', '远程 HTTP'),
    ('ftp://remote.example/v1', '不支持 ftp'),
    ('https:///v1', '缺少主机名'),
    ('http://127.0.0.1:bad/v1', '端口'),
    ('https://example.com:70000/v1', '端口'),
    ('https://example.com/v1?token=private-token', '查询参数'),
    ('https://user:private-token@example.com/v1', '用户名或密码'),
    ('https://example.com/v1#part', '#'),
    ('https://bad host.example/v1', '空格'),
    ('http://[::1/v1', '方括号'),
])
def test_url_validation_names_the_specific_failure(client, url, reason):
    response = client.put('/api/settings/models', json={'llm': dict(mode='remote', url=url, model='fixture')})
    assert response.status_code == 422
    assert reason in response.json()['detail']
    assert 'private-token' not in response.text


@pytest.mark.parametrize('url', ['http://127.0.0.1:3425/v1', 'http://localhost:3425/v1', 'http://[::1]:3425/v1', 'https://gateway.example/v1'])
def test_url_validation_accepts_supported_addresses(client, url):
    result = client.put('/api/settings/models', json={'llm': dict(mode='remote', url=url, model='fixture')})
    assert result.status_code == 200


def test_system_settings_defaults_persist_and_clamp(client, tmp_path):
    defaults = client.get('/api/settings/system').json()
    assert defaults == {'chat_display_mode': 'merged', 'show_reasoning': True,
                        'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50, 'context_characters': 160000, 'max_seconds': 900}}
    saved = client.put('/api/settings/system', json={'chat_display_mode': 'sequential', 'show_reasoning': False,
                                                     'agent_limits': {'max_model_calls': 0, 'max_tool_calls': 9999, 'context_characters': 20000, 'max_seconds': 300}})
    assert saved.status_code == 200
    body = saved.json()
    assert body['chat_display_mode'] == 'sequential' and body['show_reasoning'] is False
    # 越界值一律钳回允许范围，不报错也不落原值。
    assert body['agent_limits'] == {'max_model_calls': 1, 'max_tool_calls': 300, 'context_characters': 20000, 'max_seconds': 300}
    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get('/api/settings/system').json()['chat_display_mode'] == 'sequential'
    assert client.put('/api/settings/system', json={'chat_display_mode': 'other', 'show_reasoning': True,
                                                    'agent_limits': dict(max_model_calls=60, max_tool_calls=50, context_characters=160000, max_seconds=900)}).status_code == 422
    assert client.get('/api/settings/system').json()['chat_display_mode'] == 'sequential'
