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


def test_builtin_web_tools_always_available(client):
    tools, registry, web_tools, guidance = system_prompt_of(client)
    # 内置联网原语常驻：无需用户自装 MCP 即可联网，catalog 里有且仅有一份
    assert web_tools is True
    names = [item['name'] for item in tools]
    assert names.count('web_search') == 1 and names.count('web_read') == 1
    # 研究法引导：多角度并行查询、甄别同名多主体、并行读页、标来源 URL、不伪装证据
    assert 'web_search' in guidance and 'web_read' in guidance
    assert '同名多主体' in guidance
    assert '来源URL' in guidance
    assert '不得把搜索结果伪装成项目证据' in guidance
    assert 'company_web_lookup' in guidance  # 分工口径保留：工商核实仍优先快查


def test_search_mcp_still_cataloged_as_supplement(client):
    add_search_mcp(client, tool_name='tavily-search', description='Web search for company news')
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is True
    assert any('search' in (item['description'] or '').lower() for item in tools if item['name'].startswith('mcp_'))
    assert 'MCP' in guidance  # MCP 降级为补充来源


def test_non_search_mcp_cataloged_without_effect(client):
    add_search_mcp(client, name='Time 时间时区', tool_name='get_current_time', description='Get current time')
    tools, registry, web_tools, guidance = system_prompt_of(client)
    # 时间工具仍在目录里；联网引导不再依赖它
    assert web_tools is True
    assert any(item['name'].startswith('mcp_') for item in tools)


def test_disabled_server_not_cataloged(client):
    action_id = add_search_mcp(client)
    repo = client.app.state.repo
    with repo.connection() as conn:
        payload = json.loads(conn.execute('SELECT payload FROM agent_mcp WHERE id=?', (action_id,)).fetchone()[0])
        payload['enabled'] = False
        conn.execute('UPDATE agent_mcp SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), action_id))
    tools, registry, web_tools, guidance = system_prompt_of(client)
    assert web_tools is True and not any(item['name'].startswith('mcp_') for item in tools)
