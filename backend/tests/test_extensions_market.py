import io
import json
import sys
import zipfile
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
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture', protocol='openai')}).status_code == 200


def scripted(monkeypatch, replies):
    calls = []
    original = httpx.Client.post

    def post(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original(self, url, **kwargs)
        calls.append(json.loads(json.dumps(kwargs['json'])))
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    return calls


def tool(name, args, call_id='call1'):
    return {'choices': [{'message': {'content': None, 'tool_calls': [dict(id=call_id, type='function', function=dict(name=name, arguments=json.dumps(args)))]}}]}


def final(text='完成。'):
    return {'choices': [{'message': {'content': text}}]}


SKILL_MD = '''---
name: folder-skill
description: 带资源的多文件技能
category: 面试
author: 知根
keywords: [面试, 演练]
icon: 🎯
---
# 正文指令

按步骤执行。'''
FAQ = 'Q: 如何自我介绍？\nA: 用两分钟讲清事实与岗位的匹配。'


def multi_files(**overrides):
    rows = [dict(path='SKILL.md', text=SKILL_MD), dict(path='references/faq.md', text=FAQ), dict(path='scripts/run.py', text=None)]
    if overrides:
        rows = [dict(row, **overrides) if row['path'] in overrides else row for row in rows]
    return rows


def add_server(client, **overrides):
    body = dict(name='远端工具', transport='http', url='https://mcp.example.com/mcp', command='', args=[], env={}, headers={}, token='tok-123', clear_secrets=False)
    body.update(overrides)
    return client.post('/api/agent/mcp', json=body)


def test_sse_transport_with_custom_headers_and_masking(client):
    result = add_server(client, transport='sse', url='https://old.example.com/sse', headers={'X-Api-Key': 'secret-value'})
    assert result.status_code == 200
    data = client.get('/api/agent/extensions').json()
    server = data['servers'][0]
    assert server['transport'] == 'sse' and server['headers_set'] == ['X-Api-Key']
    assert 'secret-value' not in json.dumps(data)
    assert not any('secret-value' in json.dumps(value) for value in data['servers'])
    # 令牌与自定义 Authorization 头二选一
    conflict = add_server(client, name='冲突', headers={'Authorization': 'Bearer abc'})
    assert conflict.status_code == 422
    illegal = add_server(client, name='坏头', headers={'Bad Header': 'x'})
    assert illegal.status_code == 422


def test_credential_binding_on_endpoint_change(client):
    assert add_server(client).status_code == 200
    server_id = client.get('/api/agent/extensions').json()['servers'][0]['id']
    moved = client.put(f'/api/agent/mcp/{server_id}', json=dict(name='远端工具', transport='http', url='https://other.example.com/mcp', command='', args=[], env={}, headers={}, token='', clear_secrets=False))
    assert moved.status_code == 422
    cleared = client.put(f'/api/agent/mcp/{server_id}', json=dict(name='远端工具', transport='http', url='https://other.example.com/mcp', command='', args=[], env={}, headers={}, token='', clear_secrets=True))
    assert cleared.status_code == 200 and cleared.json()['token_set'] is False


def test_multi_file_skill_with_resources_and_scripts(client):
    created = client.post('/api/agent/skills/files', json={'files': multi_files()})
    assert created.status_code == 200
    skill = created.json()
    assert skill['category'] == '面试' and skill['icon'] == '🎯' and skill['keywords'] == ['面试', '演练']
    assert skill['resources'] == [dict(path='references/faq.md', characters=len(FAQ))]
    assert skill['scripts'] == ['scripts/run.py']
    listing = json.dumps(client.get('/api/agent/extensions').json(), ensure_ascii=False)
    assert FAQ not in listing and 'run.py' in listing
    nested = client.post('/api/agent/skills/files', json={'files': [dict(path='pack/SKILL.md', text=SKILL_MD), dict(path='pack/a.txt', text='A')]})
    assert nested.status_code == 200 and nested.json()['resources'] == [dict(path='a.txt', characters=1)]


def test_multi_file_skill_rejects_traversal_and_bad_skill_md(client):
    escape = client.post('/api/agent/skills/files', json={'files': [dict(path='SKILL.md', text=SKILL_MD), dict(path='../evil.md', text='x')]})
    assert escape.status_code == 422
    binary = client.post('/api/agent/skills/files', json={'files': [dict(path='SKILL.md', text=None)]})
    assert binary.status_code == 422
    oversized = client.post('/api/agent/skills/files', json={'files': [dict(path='SKILL.md', text=SKILL_MD), dict(path='big.md', text='字' * 60001)]})
    assert oversized.status_code == 422


def test_zip_import_and_export_roundtrip(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as bundle:
        bundle.writestr('demo/SKILL.md', SKILL_MD)
        bundle.writestr('demo/references/faq.md', FAQ)
        bundle.writestr('demo/scripts/run.py', b'print("binary-free")')
    imported = client.post('/api/agent/skills/archive', params={'filename': 'demo.zip'}, content=buffer.getvalue())
    assert imported.status_code == 200
    skill = imported.json()
    assert [r['path'] for r in skill['resources']] == ['references/faq.md']
    assert skill['scripts'] == ['scripts/run.py']
    exported = client.get(f"/api/agent/skills/{skill['id']}/export")
    assert exported.status_code == 200 and exported.headers['content-type'].startswith('application/zip')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
        names = set(bundle.namelist())
        assert names == {'SKILL.md', 'references/faq.md', 'jobscope-skill.json'}
        manifest = json.loads(bundle.read('jobscope-skill.json'))
        assert manifest['scripts'] == ['scripts/run.py']  # 脚本仅登记路径，导出不含其内容
    bad = client.post('/api/agent/skills/archive', params={'filename': 'demo.zip'}, content=b'not a zip')
    assert bad.status_code == 422
    slip = io.BytesIO()
    with zipfile.ZipFile(slip, 'w') as bundle:
        bundle.writestr('SKILL.md', SKILL_MD)
        bundle.writestr('../evil.md', 'x')
    assert client.post('/api/agent/skills/archive', params={'filename': 'evil.zip'}, content=slip.getvalue()).status_code == 422
    assert client.get('/api/agent/skills/S-missing/export').status_code == 422


def test_skill_resource_tool_in_agent_loop(client, monkeypatch):
    configure(client)
    created = client.post('/api/agent/skills/files', json={'files': multi_files()}).json()
    calls = scripted(monkeypatch, [tool('skill_read', {'skill_id': created['id']}, 'c1'), tool('skill_resource', {'skill_id': created['id'], 'path': 'references/faq.md'}, 'c2'), final('按 FAQ 指导完成。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '帮我演练面试'})
    assert result.status_code == 200 and result.json()['answer'] == '按 FAQ 指导完成。'
    assert any(t.get('name') == 'skill_resource' or t.get('function', {}).get('name') == 'skill_resource' for t in calls[0]['tools'])
    second = calls[2]['messages']
    assert '如何自我介绍' in json.dumps(second, ensure_ascii=False)  # skill_resource 结果已回传给模型
    # 不存在的资源路径会被拒绝并把错误回传给模型
    assert '资源不存在' not in result.json()['answer']


def test_market_catalog_install_and_refresh(client, monkeypatch):
    market = client.get('/api/agent/market').json()
    assert len(market['curated_mcp']) >= 10 and len(market['curated_skills']) >= 8
    assert market['online'] is None and market['fetched_at'] is None
    curated = market['curated_skills'][0]
    assert curated['content'].startswith('---')
    assert client.post('/api/agent/skills', json={'content': curated['content']}).status_code == 200
    remote = client.post('/api/agent/mcp', json=dict(name=market['curated_mcp'][0]['name'], transport=market['curated_mcp'][0]['transport'], url=market['curated_mcp'][0].get('url', ''), command=market['curated_mcp'][0].get('command', ''), args=market['curated_mcp'][0].get('args', []), env={}, headers={}, token='', clear_secrets=False))
    assert remote.status_code == 200
    after = client.get('/api/agent/market').json()
    assert next(item for item in after['curated_skills'] if item['id'] == curated['id'])['installed'] is True
    assert next(item for item in after['curated_mcp'] if item['id'] == market['curated_mcp'][0]['id'])['installed'] is True
    # 在线刷新失败：明确报错且精选目录不受影响
    original = httpx.Client.get
    monkeypatch.setattr(httpx.Client, 'get', lambda self, url, **kwargs: (_ for _ in ()).throw(httpx.ConnectError('down')) if not isinstance(self, TestClient) else original(self, url, **kwargs))
    failed = client.post('/api/agent/market/refresh')
    assert failed.status_code == 503 and '在线目录刷新失败' in failed.json()['detail']
    assert client.get('/api/agent/market').json()['curated_skills']


def test_market_online_refresh_and_online_skill_install(client, monkeypatch):
    TREE = {'tree': [{'type': 'blob', 'path': 'skills/demo/SKILL.md'}, {'type': 'blob', 'path': 'README.md'}]}
    ONLINE_SKILL = SKILL_MD.replace('name: folder-skill', 'name: online-skill')
    NPM = {'objects': [{'package': {'name': '@scope/fake-mcp', 'description': '演示 npm MCP', 'links': {'repository': 'https://github.com/scope/fake-mcp'}}}]}

    def fake_get(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original_get(self, url, **kwargs)
        url = str(url)
        if 'git/trees/HEAD' in url:
            return httpx.Response(200, json=TREE, request=httpx.Request('GET', url))
        if url.startswith('https://api.github.com/repos/') and 'git/trees' not in url:
            return httpx.Response(200, json={'stargazers_count': 42}, request=httpx.Request('GET', url))
        if url.startswith('https://raw.githubusercontent.com/'):
            return httpx.Response(200, text=ONLINE_SKILL, request=httpx.Request('GET', url))
        if 'registry.npmjs.org' in url:
            return httpx.Response(200, json=NPM, request=httpx.Request('GET', url))
        return httpx.Response(404, request=httpx.Request('GET', url))

    original_get = httpx.Client.get
    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    refreshed = client.post('/api/agent/market/refresh')
    assert refreshed.status_code == 200
    online = refreshed.json()['online']
    assert online['skills'] and online['skills'][0]['repo'] and not online['skills'][0].get('content')
    assert online['mcp'][0]['command'] == 'npx'
    assert online['skills'][0]['stars'] == 42
    cached = client.get('/api/agent/market').json()['online']
    assert cached['skills'][0]['name'] == 'online-skill'
    entry = online['skills'][0]
    installed = client.post('/api/agent/skills/install', json={'repo': entry['repo'], 'path': entry['path']})
    assert installed.status_code == 200 and installed.json()['name'] == 'online-skill'
    outside = client.post('/api/agent/skills/install', json={'repo': 'evil repo', 'path': 'x/SKILL.md'})
    assert outside.status_code == 422


def test_market_live_skill_search_and_install(client, monkeypatch):
    import jobscope.market as market
    market._SEARCH_CACHE.clear()
    NPM = {'objects': [{'package': {'name': '@playwright/mcp', 'description': 'Playwright MCP server', 'links': {}}}]}
    REPOS = {'items': [{'full_name': 'user/pdf-skill', 'stargazers_count': 7, 'description': 'pdf skill'}]}
    TREE = {'tree': [{'type': 'blob', 'path': 'SKILL.md'}]}
    SKILL_DOC = SKILL_MD.replace('name: folder-skill', 'name: pdf-skill-live')

    def fake_get(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original_get(self, url, **kwargs)
        url = str(url)
        if 'registry.npmjs.org' in url:
            return httpx.Response(200, json=NPM, request=httpx.Request('GET', url))
        if 'search/repositories' in url:
            assert 'pdf skill' in str(kwargs.get('params', {}).get('q', ''))  # 检索词自动附加 skill
            return httpx.Response(200, json=REPOS, request=httpx.Request('GET', url))
        if 'git/trees/HEAD' in url:
            return httpx.Response(200, json=TREE, request=httpx.Request('GET', url))
        if url.startswith('https://raw.githubusercontent.com/'):
            return httpx.Response(200, text=SKILL_DOC, request=httpx.Request('GET', url))
        return httpx.Response(404, request=httpx.Request('GET', url))

    original_get = httpx.Client.get
    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    result = client.get('/api/agent/market/search', params={'q': 'pdf'})
    assert result.status_code == 200
    payload = result.json()
    assert payload['mcp'] and payload['skills_error'] is None
    entry = payload['skills'][0]
    assert entry['repo'] == 'user/pdf-skill' and entry['path'] == 'SKILL.md' and entry['stars'] == 7
    assert not entry.get('content')  # 在线结果不携带正文，安装时再取
    installed = client.post('/api/agent/skills/install', json={'repo': entry['repo'], 'path': entry['path']})
    assert installed.status_code == 200 and installed.json()['name'] == 'pdf-skill-live'
    market._SEARCH_CACHE.clear()


def test_market_live_search_filters_and_caches(client, monkeypatch):
    import jobscope.market as market
    market._SEARCH_CACHE.clear()
    NPM = {'objects': [
        {'package': {'name': '@playwright/mcp', 'description': 'Playwright MCP server', 'links': {'repository': 'https://github.com/microsoft/playwright-mcp'}}},
        {'package': {'name': 'unrelated-pkg', 'description': '完全不相关的软件包', 'links': {}}},
        {'package': {'name': 'playwright-mcp-extra', 'description': 'another model context protocol tool', 'links': {}}},
    ]}
    calls = []

    def fake_get(self, url, **kwargs):
        if isinstance(self, TestClient):
            return original_get(self, url, **kwargs)
        calls.append(str(kwargs.get('params', {}).get('text', '')))
        return httpx.Response(200, json=NPM, request=httpx.Request('GET', str(url)))

    original_get = httpx.Client.get
    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    first = client.get('/api/agent/market/search', params={'q': 'playwright'})
    assert first.status_code == 200
    payload = first.json()
    # 只保留名称或描述提到 MCP 的结果
    assert [item['name'] for item in payload['mcp']] == ['@playwright/mcp', 'playwright-mcp-extra']
    assert payload['mcp'][0]['command'] == 'npx' and payload['mcp'][0]['args'] == ['-y', '@playwright/mcp']
    assert 'playwright mcp' in calls[0]  # 检索词自动附加 mcp
    second = client.get('/api/agent/market/search', params={'q': 'Playwright'})
    assert second.status_code == 200 and second.json()['mcp'] == payload['mcp']
    assert len([text for text in calls if text]) == 1  # 同词大小写不敏感，命中本机缓存（另有一次 GitHub Skills 检索不计入）
    assert client.get('/api/agent/market/search').status_code == 422
    market._SEARCH_CACHE.clear()
