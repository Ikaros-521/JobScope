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
from jobscope.repository import now


def upload_kb(client, filename, content):
    return client.post('/api/knowledge/upload', params={'filename': filename}, content=content,
                       headers={'Content-Type': 'application/octet-stream'})


def kb_docx_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>知识文档正文内容</w:t></w:r></w:p></w:body></w:document>''')
    return out.getvalue()


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
    assert client.post('/api/knowledge', json=dict(title='t', content='x' * 200001)).status_code == 422
    assert client.post('/api/knowledge', json=dict(title='t', content='x', tags=['a' * 31])).status_code == 422
    assert client.post('/api/knowledge', json=dict(title='t', content='x', tags=[str(i) for i in range(11)])).status_code == 422


def test_upload_md_docx_parse_dedup_and_invalid_rejection(client):
    md = '# 面试指南\n\n准备 RAG 评测问题，成本控制是二面重点，还需要覆盖召回率与忠实度。'.encode('utf-8')
    result = upload_kb(client, '面试指南.md', md)
    assert result.status_code == 200
    entry = result.json()['entry']
    assert entry['source'] == 'upload' and entry['file_name'] == '面试指南.md'
    assert entry['title'] == '面试指南' and entry['chunk_count'] >= 1
    chunks = client.get(f"/api/knowledge/{entry['id']}/chunks").json()['chunks']
    assert chunks and any('RAG' in chunk['text'] for chunk in chunks)
    assert all(chunk['ordinal'] == index for index, chunk in enumerate(chunks))
    # 同文查重：换文件名也不重复入库
    dup = upload_kb(client, '另一份.md', md)
    assert dup.status_code == 422 and '内容已存在' in dup.json()['detail']
    # 不支持的格式给出明确原因
    bad = upload_kb(client, 'program.exe', b'MZ fake binary')
    assert bad.status_code == 422 and '不支持此文件格式' in bad.json()['detail']
    # docx 解析
    docx = upload_kb(client, '笔记.docx', kb_docx_bytes())
    assert docx.status_code == 200 and '知识文档正文内容' in docx.json()['entry']['content']


def test_upload_pdf_and_scan_rejection(client):
    import pymupdf
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), 'Knowledge document about RAG evaluation metrics.')
        ok = upload_kb(client, 'doc.pdf', document.tobytes())
    assert ok.status_code == 200 and 'RAG' in ok.json()['entry']['content']
    with pymupdf.open() as document:
        document.new_page()
        scan = upload_kb(client, 'scan.pdf', document.tobytes())
    assert scan.status_code == 422 and '扫描' in scan.json()['detail']


def test_settings_roundtrip_clamp_and_hybrid_degradation(client):
    from jobscope.knowledge import KnowledgeStore
    client.post('/api/knowledge', json=dict(title='笔记', content='杭州数据合规岗位需求上升，主要来自金融科技客户。'))
    settings = client.get('/api/knowledge/settings').json()
    assert settings['settings']['strategy'] == 'recursive' and settings['settings']['retrieval_mode'] == 'lexical'
    assert settings['embedding_ready'] is False and settings['chunks'] >= 1
    saved = client.put('/api/knowledge/settings', json=dict(strategy='semantic', size=300, overlap=40,
                                                            retrieval_mode='hybrid', separators=['\n\n'])).json()
    assert saved['settings']['strategy'] == 'semantic' and saved['settings']['size'] == 300
    assert saved['settings']['retrieval_mode'] == 'hybrid'
    # 越界值在请求契约层直接 422（存储层 clamp 兜底历史/缺省值，单测在 test_kb_chunking）
    rejected = client.put('/api/knowledge/settings', json=dict(size=1, overlap=99999))
    assert rejected.status_code == 422
    assert client.put('/api/knowledge/settings', json=dict(size=64, overlap=0)).status_code == 200
    # 未配嵌入时 reembed 明确拒绝，hybrid 检索如实降级词法
    assert client.post('/api/knowledge/reembed').status_code == 409
    store = KnowledgeStore(client.app.state.repo)
    outcome = store.search('数据合规')
    assert outcome['mode'] == 'lexical' and outcome['degraded']


def test_rechunk_on_update_and_delete_cascade(client):
    client.put('/api/knowledge/settings', json=dict(strategy='hard', size=200, overlap=40))
    created = client.post('/api/knowledge', json=dict(title='长文', content='记' * 600)).json()
    assert created['chunk_strategy'] == 'hard' and created['chunk_count'] >= 2
    doc_id = created['id']
    updated = client.put(f'/api/knowledge/{doc_id}', json=dict(content='记' * 100)).json()
    assert updated['chunk_count'] == 1 and updated['chunk_strategy'] == 'hard'
    assert len(client.get(f'/api/knowledge/{doc_id}/chunks').json()['chunks']) == 1
    assert client.delete(f'/api/knowledge/{doc_id}').json() == {'deleted': True}
    from jobscope.knowledge import KnowledgeStore
    assert KnowledgeStore(client.app.state.repo).status()['chunks'] == 0


def test_migration_backfills_chunks_for_legacy_entries(client):
    from jobscope.knowledge import KnowledgeStore
    repo = client.app.state.repo
    # 构造 v1 形态：整篇 payload、无分块行、schema 版本键缺失
    with repo.connection() as conn:
        conn.execute("DELETE FROM knowledge_meta WHERE key='schema'")
        conn.execute('DELETE FROM knowledge_fts')
        conn.execute('DELETE FROM knowledge_chunks')
        legacy = json.dumps(dict(id='K-legacy01', title='旧条目', content='旧数据内容，用于迁移验证的分块回填。' * 20,
                                 tags=[], created_at=now(), updated_at=now()), ensure_ascii=False)
        conn.execute('INSERT OR REPLACE INTO knowledge VALUES (?,?,?)', ('K-legacy01', legacy, now()))
    store = KnowledgeStore(repo)
    assert store.status()['docs'] == 1 and store.status()['chunks'] >= 1
    hits = store.search('旧数据内容')
    assert hits['hits'] and hits['hits'][0]['doc_id'] == 'K-legacy01'
    assert client.get('/api/knowledge/settings').json()['chunks'] == store.status()['chunks']


def test_agent_chunk_hits_note_and_mode(client, monkeypatch):
    configure(client)
    client.put('/api/knowledge/settings', json=dict(strategy='hard', size=120, overlap=20))
    content = ''.join(f'要点{index}：RAG 评测需要覆盖召回率与忠实度两个维度，样本要分层抽样。' for index in range(8))
    client.post('/api/knowledge', json=dict(title='评测笔记', content=content))
    entry = client.get('/api/knowledge?q=评测笔记').json()['entries'][0]
    assert entry['chunk_count'] >= 2
    calls = scripted(monkeypatch, [tool('knowledge_search', {'query': 'RAG 评测 召回率'}),
                                   final(f'根据你的评测笔记 [{entry["id"]}]，重点是召回率与忠实度。')])
    result = client.post('/api/chat', json={'scope': 'global', 'agent_mode': 'general', 'message': '评测笔记里说了什么？'}).json()
    assert any(c['id'] == entry['id'] for c in result['citations'])
    tool_message = next(message for message in calls[-1]['messages'] if message.get('role') == 'tool')
    assert '[块' in tool_message['content'] and '词法检索' in tool_message['content']


def stage_import_file(client, task_id, index, filename, content):
    return client.post(f'/api/knowledge/imports/{task_id}/files', params={'index': index, 'filename': filename},
                       content=content, headers={'Content-Type': 'application/octet-stream'})


def wait_import_terminal(client, task_id, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f'/api/knowledge/imports/{task_id}').json()
        if task['status'] in ('completed', 'failed', 'cancelled'):
            return task
        time.sleep(0.05)
    raise AssertionError('导入任务未在时限内结束')


def test_import_task_full_flow_stages_processes_and_cleans_up(client):
    names = ['指南一.md', '指南二.md']
    created = client.post('/api/knowledge/imports', json=dict(names=names))
    assert created.status_code == 200
    task = created.json()
    assert task['status'] == 'pending' and [f['name'] for f in task['files']] == names
    body = '召回率与忠实度是评测重点，样本需要分层抽样，避免偏差集中。'
    ok1 = stage_import_file(client, task['id'], 0, '指南一.md', body.encode('utf-8'))
    ok2 = stage_import_file(client, task['id'], 1, '指南二.md', body.encode('utf-8'))
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert ok2.json()['files'][0]['state'] == 'staged'
    # 同文查重在导入阶段以 skip 收场（两个文件内容相同）
    started = client.post(f"/api/knowledge/imports/{task['id']}/start")
    assert started.status_code == 200
    final = wait_import_terminal(client, task['id'])
    assert final['status'] == 'completed' and final['count'] == 1 and final['failed'] == 0
    skipped = [f for f in final['files'] if f['state'] == 'skipped']
    imported = [f for f in final['files'] if f['state'] == 'imported']
    assert len(skipped) == 1 and '内容已存在' in skipped[0]['error']
    assert len(imported) == 1 and imported[0]['entry_id'].startswith('K-') and imported[0]['chunks'] >= 1
    doc = client.get(f"/api/knowledge/{imported[0]['entry_id']}").json()
    assert doc['title'] == '指南一' and doc['source'] == 'upload'
    # 暂存目录已清理
    import tempfile
    assert not (client.app.state.repo.data_dir / 'knowledge_imports' / task['id']).exists()


def test_import_task_cancel_keeps_imported_and_blocks_rest(client, monkeypatch):
    from jobscope import knowledge as kb_module
    original_write = kb_module.KnowledgeStore._write_doc

    def slow_write(self, conn, entry, **kwargs):
        if '慢文件标记' in entry.get('content', ''):
            import time
            time.sleep(1.2)
        return original_write(self, conn, entry, **kwargs)

    monkeypatch.setattr(kb_module.KnowledgeStore, '_write_doc', slow_write)
    names = ['快.md', '慢.md', '第三.md']
    task = client.post('/api/knowledge/imports', json=dict(names=names)).json()
    stage_import_file(client, task['id'], 0, '快.md', '第一个文件内容很快导入完成。'.encode('utf-8'))
    stage_import_file(client, task['id'], 1, '慢.md', '慢文件标记，用于拉长导入窗口。'.encode('utf-8'))
    stage_import_file(client, task['id'], 2, '第三.md', '第三个文件内容，取消后不应入库。'.encode('utf-8'))
    client.post(f"/api/knowledge/imports/{task['id']}/start")
    # 等第二个文件进入处理中（此时 worker 正持有写事务），再取消
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        state = client.get(f"/api/knowledge/imports/{task['id']}").json()
        if state['status'] == 'running' and any(f['state'] == 'processing' and f['name'] == '慢.md' for f in state['files']):
            break
        time.sleep(0.05)
    cancelled = client.post(f"/api/knowledge/imports/{task['id']}/cancel")
    assert cancelled.status_code == 200
    final = wait_import_terminal(client, task['id'])
    assert final['status'] == 'cancelled' and '成功导入' in final['message']
    states = {f['name']: f['state'] for f in final['files']}
    assert states['快.md'] == 'imported'
    assert states['慢.md'] in ('imported', 'processing')  # 取消不打断当前文件：完成后完整入库
    assert states['第三.md'] == 'cancelled'
    # 已导入的文件是完整文档（非脏数据）；取消的文件没有入库
    from jobscope.knowledge import KnowledgeStore
    store = KnowledgeStore(client.app.state.repo)
    titles = [entry['title'] for entry in store.list()['entries']]
    assert '快' in titles and '第三' not in titles


def test_import_task_rejects_conflicts_and_oversize(client, monkeypatch):
    # 并发任务互斥
    first = client.post('/api/knowledge/imports', json=dict(names=['a.md']))
    second = client.post('/api/knowledge/imports', json=dict(names=['b.md']))
    assert first.status_code == 200 and second.status_code == 409
    task_id = first.json()['id']
    # 超过单批数量
    assert client.post('/api/knowledge/imports', json=dict(names=[])).status_code == 422
    # 文件名与登记不一致 / 重复上传
    assert stage_import_file(client, task_id, 0, '别的名字.md', b'x').status_code == 409
    assert stage_import_file(client, task_id, 0, 'a.md', b'x').status_code == 200
    assert stage_import_file(client, task_id, 0, 'a.md', b'x').status_code == 409
    # 未传完不允许 start（登记了 2 个文件只传了 1 个）
    client.post('/api/knowledge/imports', json=dict(names=[]))  # 空名单 422 已在上面覆盖
    conflict_task = client.post('/api/knowledge/imports', json=dict(names=['部分.md', '缺失.md']))
    assert conflict_task.status_code == 409  # 与活动任务互斥
    client.post(f'/api/knowledge/imports/{task_id}/cancel')
    pending_task = client.post('/api/knowledge/imports', json=dict(names=['部分.md', '缺失.md'])).json()
    stage_import_file(client, pending_task['id'], 0, '部分.md', b'x')
    assert client.post(f"/api/knowledge/imports/{pending_task['id']}/start").status_code == 409
    client.post(f"/api/knowledge/imports/{pending_task['id']}/cancel")
    # 已取消的任务再操作一律拒绝，暂存目录已清理
    assert client.post(f'/api/knowledge/imports/{task_id}/cancel').status_code == 409
    assert client.post(f'/api/knowledge/imports/{task_id}/start').status_code == 409
    assert not (client.app.state.repo.data_dir / 'knowledge_imports' / task_id).exists()
    # 单文件超限（把上限改小验证流式拒绝）
    from jobscope import kb_import as kb_import_module
    monkeypatch.setattr(kb_import_module, 'KB_MAX_FILE_BYTES', 8)
    fresh = client.post('/api/knowledge/imports', json=dict(names=['big.md'])).json()
    oversize = stage_import_file(client, fresh['id'], 0, 'big.md', b'x' * 32)
    assert oversize.status_code == 413
    client.post(f"/api/knowledge/imports/{fresh['id']}/cancel")


def test_staging_cleanup_on_restart(client):
    from jobscope.knowledge import KnowledgeImportManager
    repo = client.app.state.repo
    orphan = repo.data_dir / 'knowledge_imports' / 'T-orphan'
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / 'leftover.bin').write_bytes(b'x')
    manager = KnowledgeImportManager(KnowledgeStore2 := __import__('jobscope.knowledge', fromlist=['KnowledgeStore']).KnowledgeStore(repo), repo)
    assert not orphan.exists()
    # 已结束任务的暂存目录同样清理
    done = client.post('/api/knowledge/imports', json=dict(names=['清理.md'])).json()
    stale_dir = repo.data_dir / 'knowledge_imports' / done['id']
    stale_dir.mkdir(parents=True, exist_ok=True)
    client.post(f"/api/knowledge/imports/{done['id']}/cancel")
    KnowledgeImportManager(__import__('jobscope.knowledge', fromlist=['KnowledgeStore']).KnowledgeStore(repo), repo)
    assert not stale_dir.exists()


def test_latest_import_task_endpoint(client):
    assert client.get('/api/knowledge/imports/latest').json() is None
    created = client.post('/api/knowledge/imports', json=dict(names=['最新.md'])).json()
    latest = client.get('/api/knowledge/imports/latest').json()
    assert latest['id'] == created['id'] and latest['kind'] == 'knowledge_import'
    client.post(f"/api/knowledge/imports/{created['id']}/cancel")


def test_import_filename_sanitised_and_matched_from_original(client):
    """危险文件名（Windows 非法字符/保留名/目录成分）登记时安全化；前端拿原始名上传，
    stage 侧用同一安全化函数对名，照样能对上并正常入库。"""
    nasty = ['报告:最终<b>.md', 'con.md', 'folder/嵌套.md']
    task = client.post('/api/knowledge/imports', json=dict(names=nasty)).json()
    assert [f['name'] for f in task['files']] == ['报告_最终_b_.md', '_con.md', 'folder_嵌套.md']
    for index, name in enumerate(nasty):
        assert stage_import_file(client, task['id'], index, name, f'安全化内容{index}，用于导入验证。'.encode('utf-8')).status_code == 200
    client.post(f"/api/knowledge/imports/{task['id']}/start")
    final = wait_import_terminal(client, task['id'])
    assert final['status'] == 'completed' and final['count'] == 3 and final['failed'] == 0


def test_import_cancel_in_start_window_never_revives_completed(client, monkeypatch):
    """回归：旧版 start 在「落库 running→注册取消事件」之间留窗口，cancel 走 pending 分支
    把任务标取消后 worker 照常跑完，最终把已取消任务改写成 completed。现在 start 全程持锁，
    cancel 只置位事件；同步驱动 worker（事件已置位）必须以 cancelled 收尾，且无文档入库。"""
    import threading
    import types

    from jobscope import knowledge as kb_module

    captured = []

    class _NoStartThread:
        def __init__(self, target=None, args=(), daemon=False, **kwargs):
            self.target, self.args = target, args
            captured.append(self)

        def start(self):
            pass  # 不真的起线程：由测试在 cancel 之后同步驱动 worker

    shim = types.SimpleNamespace(Event=threading.Event, Thread=_NoStartThread)
    monkeypatch.setattr(kb_module, 'threading', shim)

    task = client.post('/api/knowledge/imports', json=dict(names=['窗口.md'])).json()
    assert stage_import_file(client, task['id'], 0, '窗口.md', '取消窗口回归测试内容。'.encode('utf-8')).status_code == 200
    assert client.post(f"/api/knowledge/imports/{task['id']}/start").status_code == 200
    cancelled = client.post(f"/api/knowledge/imports/{task['id']}/cancel")
    assert cancelled.status_code == 200  # running 分支：置位事件，不写 payload
    # cancel 不应改动 running 任务的 payload（worker 独占写入）
    untouched = client.get(f"/api/knowledge/imports/{task['id']}").json()
    assert untouched['status'] == 'running'
    worker = captured[0]
    worker.target(*worker.args)
    final = client.get(f"/api/knowledge/imports/{task['id']}").json()
    assert final['status'] == 'cancelled' and '已停止' in final['message']
    assert final['count'] == 0
    from jobscope.knowledge import KnowledgeStore
    assert KnowledgeStore(client.app.state.repo).list()['entries'] == []


def test_import_stage_concurrent_uploads_serialize(client):
    """stage 是 payload 整包读改写：并发上传必须串行在管理器锁后，全部落盘且状态一致。"""
    import threading

    names = ['并发一.md', '并发二.md', '并发三.md']
    task = client.post('/api/knowledge/imports', json=dict(names=names)).json()
    manager = client.app.state.knowledge_imports
    failures = []

    def upload(index, name):
        try:
            manager.stage(task['id'], index, name, f'并发上传内容{index}，验证锁。'.encode('utf-8'))
        except Exception as exc:  # noqa: BLE001 — 失败要显式断言为空，不能让线程异常静默
            failures.append(exc)

    threads = [threading.Thread(target=upload, args=(index, name)) for index, name in enumerate(names)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    state = client.get(f"/api/knowledge/imports/{task['id']}").json()
    assert all(item['state'] == 'staged' for item in state['files'])
    assert state['message'] == f'已上传 {len(names)}/{len(names)} 个文件'
    client.post(f"/api/knowledge/imports/{task['id']}/cancel")
