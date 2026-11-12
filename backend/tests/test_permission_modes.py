import json
import sys
import time
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


def configure(client):
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture')}).status_code == 200


def scripted(monkeypatch, replies):
    calls = []
    original = httpx.Client.post
    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        calls.append(json.loads(json.dumps(kwargs['json'])))
        body = replies.pop(0)
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    return calls


def tool(name, args, call_id='call1'):
    return {'choices': [{'message': {'content': None, 'tool_calls': [dict(id=call_id, type='function', function=dict(name=name, arguments=json.dumps(args)))]}}]}


def final(text='已按工具结果整理。'):
    return {'choices': [{'message': {'content': text}}]}


def install_server(client, monkeypatch, tools, auto_tools=()):
    """注册一个 MCP 服务器：tools 形如 [('lookup', False, False)]，后两项为 readOnlyHint/destructiveHint（缺省 False）。"""
    from jobscope.extensions import MCPClient, tool_alias
    normalized = [((t + (False, False))[:3]) for t in tools]
    async def inspect(config):
        return [dict(name=name, description='Fixture', inputSchema={'type': 'object', 'properties': {'q': {'type': 'string'}}},
                     annotations={**(dict(readOnlyHint=True) if read_only else {}), **(dict(destructiveHint=True) if destructive else {})})
                 for name, read_only, destructive in normalized]
    executed = []
    async def invoke(config, name, args):
        executed.append((name, args))
        return dict(text='external result', is_error=False)
    monkeypatch.setattr(MCPClient, 'discover', staticmethod(inspect))
    monkeypatch.setattr(MCPClient, 'call', staticmethod(invoke))
    server = client.post('/api/agent/mcp', json=dict(name='权限夹具', transport='http', url='http://127.0.0.1:9000/mcp')).json()
    client.post(f"/api/agent/mcp/{server['id']}/test")
    client.patch(f"/api/agent/mcp/{server['id']}", json=dict(enabled=True, allowed_tools=[t[0] for t in tools], auto_tools=list(auto_tools)))
    return server, executed, {t[0]: tool_alias(server['id'], t[0]) for t in tools}


def conversation(client, permission_mode):
    return client.app.state.repo.create_conversation(dict(kind='global'), mode='general', permission_mode=permission_mode)


def test_confirm_mode_requires_approval_even_for_auto_tools(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('lookup', False)], auto_tools=['lookup'])
    conversation_id = conversation(client, 'confirm')
    configure(client)
    scripted(monkeypatch, [tool(alias['lookup'], {'q': 'x'}), final()])
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    # confirm 档是主策略：工具级「自动执行」勾选暂不生效。
    assert result['trace']['status'] == 'awaiting_approval' and not executed
    assert '变更前确认' in result['pending_approval']['reason']
    approved = client.post(f"/api/agent/runs/{result['trace']['id']}/approval",
                           json=dict(approved=True, approval_id=result['pending_approval']['approval_id'])).json()
    assert approved['trace']['status'] == 'completed' and executed == [('lookup', {'q': 'x'})]


def test_autonomous_mode_runs_readonly_and_auto_tools_but_confirms_others(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('peek', True), ('mutate', False), ('trusted', False)], auto_tools=['trusted'])
    configure(client)
    # readOnlyHint 工具与 auto_tools 工具直接执行；普通工具仍逐次确认（兼容既有语义）。
    for name in ('peek', 'trusted'):
        conversation_id = conversation(client, 'autonomous')
        scripted(monkeypatch, [tool(alias[name], {'q': 'x'}), final()])
        result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
        assert result['trace']['status'] == 'completed' and executed == [(name, {'q': 'x'})]
        executed.clear()
    conversation_id = conversation(client, 'autonomous')
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'x'}), final()])
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    assert result['trace']['status'] == 'awaiting_approval' and not executed
    assert '只读' in result['pending_approval']['reason']


def test_full_mode_executes_without_approval(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('mutate', False)])
    conversation_id = conversation(client, 'full')
    configure(client)
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'x'}), final()])
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    assert result['trace']['status'] == 'completed' and executed == [('mutate', {'q': 'x'})]
    assert not result.get('pending_approval')


def test_resume_taint_still_requires_approval_in_full_mode(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('lookup', False)], auto_tools=['lookup'])
    conversation_id = conversation(client, 'full')
    configure(client)
    client.put('/api/resume', json=dict(text='Python开发', format='text'))
    scripted(monkeypatch, [tool('resume_read', {}), final('你的简历包含Python开发。'), tool(alias['lookup'], {'q': 'x'})])
    client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='读简历'))
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    # 简历红线：完全访问也不放行简历参数外发。
    assert result['trace']['status'] == 'awaiting_approval' and not executed
    assert '简历' in result['pending_approval']['reason']


@pytest.mark.parametrize('mode,marker', [
    ('confirm', '「变更前确认」'),
    ('autonomous', '「自主」'),
    ('full', '「完全访问」'),
])
def test_system_prompt_carries_permission_guidance(client, monkeypatch, mode, marker):
    conversation_id = conversation(client, mode)
    configure(client)
    calls = scripted(monkeypatch, [final()])
    client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='你好'))
    assert marker in calls[0]['messages'][0]['content']
    assert '未经授权不得把简历等个人资料发送到外部工具' in calls[0]['messages'][0]['content']


def test_conversation_permission_mode_patch_roundtrip_and_validation(client):
    conversation_id = client.app.state.repo.create_conversation(dict(kind='global'), mode='general')
    assert client.get(f'/api/conversations/{conversation_id}').json()['permission_mode'] == 'autonomous'
    patched = client.patch(f'/api/conversations/{conversation_id}', json={'permission_mode': 'full'})
    assert patched.status_code == 200 and patched.json()['permission_mode'] == 'full'
    assert client.patch(f'/api/conversations/{conversation_id}', json={'permission_mode': 'yolo'}).status_code == 422
    # 空字段不误写；非法值已被 422 挡住，会话保持 full。
    assert client.patch(f'/api/conversations/{conversation_id}', json={}).status_code == 200
    assert client.get(f'/api/conversations/{conversation_id}').json()['permission_mode'] == 'full'


def test_chat_run_persists_and_inherits_permission_mode(client, monkeypatch):
    configure(client)
    scripted(monkeypatch, [final(), final()])
    assert client.post('/api/chat/runs', json=dict(message='你好', agent_mode='general', permission_mode='full')).status_code == 200
    detail = client.get('/api/conversations').json()['conversations'][0]
    conversation_id = detail['id']
    deadline = time.time() + 5
    while time.time() < deadline:
        current = client.get(f'/api/conversations/{conversation_id}').json()
        if current['active_run'] is None and len(current['messages']) >= 2:
            break
        time.sleep(0.05)
    assert current['permission_mode'] == 'full', '新会话首跑应落库 permission_mode'
    # 省略 permission_mode 沿用会话已存值，且非法值被 pydantic 挡住。
    assert client.post('/api/chat/runs', json=dict(message='再问', agent_mode='general', conversation_id=conversation_id)).status_code == 200
    assert client.post('/api/chat/runs', json=dict(message='坏值', agent_mode='general', permission_mode='yolo')).status_code == 422
    deadline = time.time() + 5
    while time.time() < deadline:
        current = client.get(f'/api/conversations/{conversation_id}').json()
        if current['active_run'] is None and len(current['messages']) >= 4:
            break
        time.sleep(0.05)
    assert current['permission_mode'] == 'full'

def test_session_grant_skips_later_confirmations(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('mutate', False)])
    conversation_id = conversation(client, 'autonomous')
    configure(client)
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'one'}), final(), tool(alias['mutate'], {'q': 'two'}), final()])
    pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    assert pending['trace']['status'] == 'awaiting_approval' and not executed
    # 批准并勾选「本会话始终允许」
    approved = client.post(f"/api/agent/runs/{pending['trace']['id']}/approval",
                           json=dict(approved=True, approval_id=pending['pending_approval']['approval_id'], grant=True)).json()
    assert approved['trace']['status'] == 'completed' and executed == [('mutate', {'q': 'one'})]
    # 同会话第二次调用同工具：授权生效，不再弹卡
    second = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='再查')).json()
    assert second['trace']['status'] == 'completed' and executed == [('mutate', {'q': 'one'}), ('mutate', {'q': 'two'})]
    assert not second.get('pending_approval')


def test_grant_is_scoped_to_conversation_and_honored_in_confirm_mode(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('mutate', False)])
    granted_conv = conversation(client, 'confirm')
    configure(client)
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'x'}), final()])
    pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=granted_conv, message='查')).json()
    client.post(f"/api/agent/runs/{pending['trace']['id']}/approval",
                json=dict(approved=True, approval_id=pending['pending_approval']['approval_id'], grant=True))
    # confirm 档认显式授权：同工具直跑
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'y'}), final()])
    again = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=granted_conv, message='再查')).json()
    assert again['trace']['status'] == 'completed' and len(executed) == 2
    # 授权按会话隔离：另一会话不继承
    other_conv = conversation(client, 'confirm')
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'z'}), final()])
    other = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=other_conv, message='查')).json()
    assert other['trace']['status'] == 'awaiting_approval' and len(executed) == 2


def test_resume_taint_beats_session_grant(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('lookup', False)], auto_tools=['lookup'])
    conversation_id = conversation(client, 'full')
    configure(client)
    client.put('/api/resume', json=dict(text='Python开发', format='text'))
    scripted(monkeypatch, [tool('resume_read', {}), final('简历包含Python开发。'), tool(alias['lookup'], {'q': 'x'}), final('完成。')])
    client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='读简历'))
    pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    assert pending['trace']['status'] == 'awaiting_approval'
    # 即使带 grant 批准，本轮读过简历后同工具再调用仍走确认（红线优先于授权）
    client.post(f"/api/agent/runs/{pending['trace']['id']}/approval",
                json=dict(approved=True, approval_id=pending['pending_approval']['approval_id'], grant=True))
    scripted(monkeypatch, [tool(alias['lookup'], {'q': 'y'}), final('完成。')])
    third = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='再查')).json()
    assert third['trace']['status'] == 'awaiting_approval' and '简历' in third['pending_approval']['reason']


def test_rejection_never_records_grant(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('mutate', False)])
    conversation_id = conversation(client, 'autonomous')
    configure(client)
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'x'}), final(), tool(alias['mutate'], {'q': 'y'}), final()])
    pending = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    # 拒绝且请求 grant：授权不得记录
    denied = client.post(f"/api/agent/runs/{pending['trace']['id']}/approval",
                         json=dict(approved=False, approval_id=pending['pending_approval']['approval_id'], grant=True)).json()
    assert denied['trace']['status'] == 'completed' and not executed
    again = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='再查')).json()
    assert again['trace']['status'] == 'awaiting_approval' and not executed


def test_destructive_hint_shows_in_approval_reason(client, monkeypatch):
    server, executed, alias = install_server(client, monkeypatch, [('mutate', False, True)])
    conversation_id = conversation(client, 'autonomous')
    configure(client)
    scripted(monkeypatch, [tool(alias['mutate'], {'q': 'x'}), final()])
    result = client.post('/api/chat', json=dict(scope='global', agent_mode='general', conversation_id=conversation_id, message='查')).json()
    assert result['trace']['status'] == 'awaiting_approval'
    assert '修改外部数据' in result['pending_approval']['reason']
