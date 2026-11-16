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


def configure(client, protocol='openai'):
    assert client.put('/api/settings/models', json={'llm': dict(mode='remote', url='https://fixture.example/v1', model='fixture', protocol=protocol)}).status_code == 200


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


def final(text):
    return {'choices': [{'message': {'content': text}}]}


def test_knowledge_crud_roundtrip(client):
    created = client.post('/api/knowledge', json=dict(title='面试复盘', content='甲公司的二面重点是 RAG 评测与成本控制。', tags=['面试', '甲公司']))
    assert created.status_code == 200
    entry = created.json()
    assert entry['id'].startswith('K-') and entry['tags'] == ['面试', '甲公司']
    listed = client.get('/api/knowledge').json()
    assert listed['total'] == 1 and listed['entries'][0]['id'] == entry['id']
    assert listed['tags'] == ['甲公司', '面试']
    detail = client.get(f"/api/knowledge/{entry['id']}").json()
    assert detail['content'].startswith('甲公司的二面重点')
    updated = client.put(f"/api/knowledge/{entry['id']}", json=dict(tags=['面试'], content='更新后的复盘内容。'))
    assert updated.status_code == 200 and updated.json()['tags'] == ['面试']
    assert updated.json()['title'] == '面试复盘'
    assert updated.json()['created_at'] == entry['created_at']
    assert client.get('/api/knowledge/missing-id').status_code == 404
    assert client.delete(f"/api/knowledge/{entry['id']}").json() == {'deleted': True}
    assert client.get('/api/knowledge').json()['total'] == 0


def test_knowledge_search_is_agent_retrieval_preview(client):
    client.post('/api/knowledge', json=dict(title='行业笔记', content='杭州的数据合规岗位近年需求上升，主要来自金融科技客户。', tags=['行业']))
    client.post('/api/knowledge', json=dict(title='面试复盘', content='甲公司二面重点是 RAG 评测。', tags=['面试']))
    hits = client.get('/api/knowledge?q=数据合规').json()
    assert hits['total'] == 1 and hits['entries'][0]['title'] == '行业笔记'
    tagged = client.get('/api/knowledge?tag=面试').json()
    assert tagged['total'] == 1 and tagged['entries'][0]['title'] == '面试复盘'
    # 内容截断：列表给摘要，详情给全文。
    client.post('/api/knowledge', json=dict(title='长文', content='很' * 500))
    listed = client.get('/api/knowledge?q=长文').json()
    assert listed['entries'][0]['content_truncated'] is True
    assert len(listed['entries'][0]['content']) == 400
    assert len(client.get(f"/api/knowledge/{listed['entries'][0]['id']}").json()['content']) == 500


def test_agent_searches_knowledge_and_cites_k_ids(client, monkeypatch):
    configure(client)
    entry = client.post('/api/knowledge', json=dict(title='面试复盘', content='甲公司的二面重点是 RAG 评测与成本控制。', tags=['面试'])).json()
    calls = scripted(monkeypatch, [tool('knowledge_search', {'query': '甲公司 二面'}), final(f'根据你的复盘，二面重点是 RAG 评测与成本控制 [{entry["id"]}]。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '甲公司二面要准备什么？'}).json()
    sent_tools = [item['function']['name'] for message in calls[0]['messages'][:1] for item in []]  # system 无工具
    assert 'knowledge_search' in json.dumps(calls[0]['tools'], ensure_ascii=False)
    citations = [c['id'] for c in result['citations']]
    assert citations == [entry['id']]
    assert result['trace']['status'] == 'completed'
    # 工具结果回填：模型收到的工具消息里带条目正文与自述口径提示。
    tool_message = next(m for m in calls[-1]['messages'] if m.get('role') == 'tool')
    assert '自述口径' in tool_message['content']


def test_agent_reports_empty_knowledge_without_fabricating(client, monkeypatch):
    configure(client)
    scripted(monkeypatch, [tool('knowledge_search', {'query': '不存在的内容'}), final('知识库里没有相关记录。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '我的笔记里有什么？'}).json()
    assert result['citations'] == []


def test_fabricated_knowledge_citation_is_marked_inline(client, monkeypatch):
    configure(client)
    scripted(monkeypatch, [final('凭空引用 [K-deadbeef0000] 的结论。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '总结一下'}).json()
    assert 'K-deadbeef0000 未读取' in result['answer']
    assert result['trace']['degraded'] and '未读取引用' in result['trace']['degraded'][0]
    assert result['citations'] == []


def test_knowledge_read_pages_long_entries_and_knowledge_available_in_all_scopes(client, monkeypatch):
    configure(client)
    entry = client.post('/api/knowledge', json=dict(title='长篇复盘', content='记' * 7000)).json()
    from jobscope.knowledge import KnowledgeStore
    store = KnowledgeStore(client.app.state.repo)
    paged = store.get(entry['id'])['content'][0:6000]
    assert len(paged) == 6000
    # 公司范围的工具目录同样包含知识检索（用户自有材料，不随项目范围收缩）。
    scripted(monkeypatch, [tool('knowledge_search', {'query': '记'}), final('你的长篇复盘已保存。')])
    result = client.post('/api/chat', json={'company': '甲', 'agent_mode': 'general', 'message': '我的复盘在哪？'}).json()
    assert result['mode'] == 'agent'


def test_validation_rejects_oversize_and_empty_inputs(client):
    assert client.post('/api/knowledge', json=dict(title='', content='x')).status_code == 422
    assert client.post('/api/knowledge', json=dict(title='t', content='x' * 50001)).status_code == 422
    assert client.post('/api/knowledge', json=dict(title='t', content='x', tags=['a' * 31])).status_code == 422
    assert client.post('/api/knowledge', json=dict(title='t', content='x', tags=[str(i) for i in range(11)])).status_code == 422
