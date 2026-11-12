import json
import sys
from pathlib import Path

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


def system_prompt_of(client):
    repo = client.app.state.repo
    from jobscope.general_agent import GeneralAgent
    agent = GeneralAgent(repo)
    tools, registry, web_tools = agent._catalog()
    guidance = agent._web_search_guidance(web_tools)
    return tools, registry, web_tools, guidance


def add_search_mcp(client, name='Tavily Search', tool_name='tavily_search', description='Web search', enabled=True):
    repo = client.app.state.repo
    import uuid
    server_id = 'S-' + uuid.uuid4().hex[:12]
    payload = dict(id=server_id, name=name, transport='http', url='http://127.0.0.1:9100/mcp', command='', args=[],
                   env={}, headers={}, token='', enabled=enabled, tested=True,
                   tools=[dict(name=tool_name, description=description, inputSchema={'type': 'object', 'properties': {}})],
                   allowed_tools=[tool_name], auto_tools=[tool_name] if enabled else [])
    with repo.connection() as conn:
        conn.execute('INSERT INTO agent_mcp VALUES (?,?)', (server_id, json.dumps(payload, ensure_ascii=False)))
    return server_id


def test_no_search_mcp_guides_user_to_install(client):
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is False
    assert '当前没有可用的联网搜索工具' in guidance
    assert '工具市场' in guidance and 'Tavily' in guidance
    assert '不要虚构检索过程' in guidance and '禁止输出任何 [E-…] 证据引用' in guidance


def test_search_mcp_triggers_proactive_guidance(client):
    add_search_mcp(client, tool_name='tavily-search', description='Web search for company news')
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is True
    assert any('search' in (item['description'] or '').lower() for item in tools if item['name'].startswith('mcp_'))
    assert '主动调用搜索类MCP工具' in guidance
    assert '来源URL与访问时间' in guidance
    assert '不得把搜索结果伪装成项目证据' in guidance


def test_non_search_mcp_does_not_trigger_guidance(client):
    add_search_mcp(client, name='Time 时间时区', tool_name='get_current_time', description='Get current time')
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is False
    assert '当前没有可用的联网搜索工具' in guidance
    # 时间工具仍在目录里，只是不触发联网引导
    assert any(item['name'].startswith('mcp_') for item in tools)


def test_disabled_server_does_not_trigger_guidance(client):
    action_id = add_search_mcp(client)
    repo = client.app.state.repo
    with repo.connection() as conn:
        payload = json.loads(conn.execute('SELECT payload FROM agent_mcp WHERE id=?', (action_id,)).fetchone()[0])
        payload['enabled'] = False
        conn.execute('UPDATE agent_mcp SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), action_id))
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is False and not any(item['name'].startswith('mcp_') for item in tools)
    assert '当前没有可用的联网搜索工具' in guidance
