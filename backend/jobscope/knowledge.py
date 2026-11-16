"""User-authored knowledge documents; chunked lexical/hybrid retrieval shared by UI and Agent tools.

分块/检索设计对齐 kb-kit（packages/core）：文档按策略切块后逐块入 FTS 与向量缓存；
检索 = 词法(BM25) 与可选向量通道的 RRF 融合；语义/LLM 分块依赖缺失时显式回退 recursive。
"""

import json
import uuid

from .kb_chunking import STRATEGIES, clamp_overlap, clamp_size, chunk_text, format_for
from .repository import digest, now

SCHEMA_VERSION = 'knowledge-store-2'
LIST_SNIPPET_CHARS = 400
SEARCH_TEXT_CHARS = 4000   # Agent 检索结果里单文档拼接分块的窗口；超长用 knowledge_read 翻页
READ_WINDOW_CHARS = 6000   # knowledge_read 的 offset 窗口（与 project_read/skill_resource 同口径）
MAX_TAGS = 10
MAX_SEARCH_DOCS = 8
MAX_EVIDENCE_DOCS = 12
VECTOR_BATCH = 24
RRF_K = 60
VECTOR_WEIGHT, LEXICAL_WEIGHT = 1.2, 1.0
RETRIEVAL_MODES = ('lexical', 'hybrid')
DEFAULT_SETTINGS = dict(strategy='recursive', size=500, overlap=50, separators=[], retrieval_mode='lexical')


def _normalise_tags(tags):
    cleaned = []
    for tag in tags or []:
        text = str(tag).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:MAX_TAGS]


class KnowledgeStore:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id TEXT NOT NULL,ordinal INTEGER NOT NULL,text TEXT NOT NULL,content_hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS knowledge_chunk_doc ON knowledge_chunks(doc_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(body,title);
                CREATE TABLE IF NOT EXISTS knowledge_vectors(model TEXT NOT NULL,content_hash TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(model,content_hash));
                CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
        self._migrate()

    # ---------- schema & settings ----------

    def _migrate(self):
        """v1（整篇入 FTS）自动迁移为分块模型：旧条目按 recursive + 当前参数回填分块。"""
        with self.repo.connection() as conn:
            row = conn.execute("SELECT value FROM knowledge_meta WHERE key='schema'").fetchone()
            if row and row[0] == SCHEMA_VERSION:
                return
            conn.execute('DELETE FROM knowledge_fts')
            conn.execute('DELETE FROM knowledge_chunks')
            for row in conn.execute('SELECT payload FROM knowledge').fetchall():
                try:
                    entry = json.loads(row['payload'])
                    self._write_doc(conn, {**entry, 'source': entry.get('source', 'manual')},
                                    strategy='recursive', size=None, overlap=None, embed=False)
                except (ValueError, KeyError, TypeError):
                    continue  # 单条异常文档跳过，不阻断整个库的迁移
            conn.execute("INSERT OR REPLACE INTO knowledge_meta VALUES ('schema',?)", (SCHEMA_VERSION,))

    def settings(self):
        stored = {}
        with self.repo.connection() as conn:
            row = conn.execute("SELECT value FROM knowledge_meta WHERE key='settings'").fetchone()
            if row:
                try:
                    stored = json.loads(row[0])
                except ValueError:
                    stored = {}
        result = dict(DEFAULT_SETTINGS)
        result['strategy'] = stored['strategy'] if stored.get('strategy') in STRATEGIES else result['strategy']
        result['retrieval_mode'] = stored['retrieval_mode'] if stored.get('retrieval_mode') in RETRIEVAL_MODES else result['retrieval_mode']
        result['size'] = clamp_size(stored.get('size', result['size']))
        result['overlap'] = clamp_overlap(stored.get('overlap', result['overlap']), result['size'])
        separators = stored.get('separators')
        result['separators'] = [str(s)[:20] for s in separators if str(s).strip()][:10] if isinstance(separators, list) else []
        return result

    def save_settings(self, data):
        current = self.settings()
        merged = {**current, **{key: value for key, value in data.items() if value is not None}}
        merged['strategy'] = merged['strategy'] if merged['strategy'] in STRATEGIES else current['strategy']
        merged['retrieval_mode'] = merged['retrieval_mode'] if merged['retrieval_mode'] in RETRIEVAL_MODES else current['retrieval_mode']
        merged['size'] = clamp_size(merged['size'])
        merged['overlap'] = clamp_overlap(merged['overlap'], merged['size'])
        merged['separators'] = [str(s).strip()[:20] for s in (merged.get('separators') or []) if str(s).strip()][:10]
        with self.repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO knowledge_meta VALUES (?,?)', ('settings', json.dumps(merged, ensure_ascii=False)))
        return dict(settings=self.settings(), **self.status())

    # ---------- chunking & persistence ----------

    def _embedder(self):
        from .research.providers import EmbeddingProvider
        provider = EmbeddingProvider()
        return provider if provider.configured else None

    def _chat(self):
        from .chat import model_configured
        if not model_configured():
            return None
        from .model_transport import complete
        from .settings import get_model_config
        config = get_model_config()

        def chat(prompt):
            content, _ = complete(config, '你是文档切分助手，只按指令输出 JSON 数组。', prompt, temperature=0, max_tokens=2000, timeout=60)
            return content

        return chat

    def _write_doc(self, conn, entry, strategy=None, size=None, overlap=None, embed=True):
        """按策略切块并写 doc + chunks + FTS；返回更新后的 entry。嵌入失败保持待补，不阻塞。"""
        requested = strategy or entry.get('chunk_strategy') or self.settings()['strategy']
        size = clamp_size(size if size is not None else entry.get('chunk_size') or self.settings()['size'])
        overlap = clamp_overlap(overlap if overlap is not None else entry.get('chunk_overlap') or self.settings()['overlap'], size)
        fmt = format_for(entry.get('file_name'))
        chunks, effective = chunk_text(entry['content'], strategy=requested, size=size, overlap=overlap,
                                       separators=self.settings()['separators'] or None, format=fmt,
                                       embedder=self._embedder() if requested == 'semantic' else None,
                                       chat=self._chat() if requested == 'llm' else None)
        entry.update(chunk_strategy=effective, chunk_size=size, chunk_overlap=overlap,
                     content_hash=digest(entry['content']), chunk_count=len(chunks), status='ready')
        entry['updated_at'] = now()
        conn.execute('INSERT OR REPLACE INTO knowledge VALUES (?,?,?)',
                     (entry['id'], json.dumps(entry, ensure_ascii=False), entry['updated_at']))
        # 先清 FTS（要靠 chunks 表找 rowid），再删 chunks 本体，然后重写。
        conn.execute('DELETE FROM knowledge_fts WHERE rowid IN (SELECT id FROM knowledge_chunks WHERE doc_id=?)', (entry['id'],))
        conn.execute('DELETE FROM knowledge_chunks WHERE doc_id=?', (entry['id'],))
        from .research.query import tokenize
        title_tokens = ' '.join(tokenize(entry['title']))
        hashes = []
        for ordinal, text in enumerate(chunks):
            text = text.strip()
            if not text:
                continue
            content_hash = digest(text)
            hashes.append(content_hash)
            cursor = conn.execute('INSERT INTO knowledge_chunks(doc_id,ordinal,text,content_hash) VALUES (?,?,?,?)',
                                  (entry['id'], ordinal, text, content_hash))
            conn.execute('INSERT INTO knowledge_fts(rowid,body,title) VALUES (?,?,?)',
                         (cursor.lastrowid, ' '.join(tokenize(text)), title_tokens))
        if embed and hashes:
            try:
                self._embed_chunks([(text, digest(text)) for text in chunks if text.strip()])
            except (ValueError, RuntimeError, OSError):
                pass  # 嵌入不可用：向量保持待补，词法检索不受影响，status 会如实报告 pending
        return entry

    def _embedding_identity(self):
        provider = self._embedder()
        return provider.identity if provider else None

    def _embed_chunks(self, pairs):
        """pairs: [(text, content_hash)]，只为缺失/换模型的分块补向量；返回补上的数量。"""
        provider = self._embedder()
        if provider is None or not pairs:
            return 0
        with self.repo.connection() as conn:
            pending = []
            for text, content_hash in pairs:
                row = conn.execute('SELECT 1 FROM knowledge_vectors WHERE model=? AND content_hash=?',
                                   (provider.identity, content_hash)).fetchone()
                if not row:
                    pending.append((text, content_hash))
        count = 0
        for start in range(0, len(pending), VECTOR_BATCH):
            batch = pending[start:start + VECTOR_BATCH]
            vectors = provider.embed([text for text, _ in batch])
            with self.repo.connection() as conn:
                for (text, content_hash), vector in zip(batch, vectors):
                    conn.execute('INSERT OR REPLACE INTO knowledge_vectors VALUES (?,?,?)',
                                 (provider.identity, content_hash, json.dumps(vector)))
                    count += 1
        return count

    def save(self, data):
        """Create or update one doc; content changes re-chunk with the doc's recorded strategy."""
        entry_id = data.get('id') or ('K-' + uuid.uuid4().hex[:12])
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload FROM knowledge WHERE id=?', (entry_id,)).fetchone()
            if row:
                existing = json.loads(row['payload'])
                entry = {**existing, **data, 'id': entry_id}
            else:
                settings = self.settings()
                entry = {**data, 'id': entry_id, 'created_at': now(), 'source': data.get('source', 'manual'),
                         'chunk_strategy': settings['strategy'], 'chunk_size': settings['size'],
                         'chunk_overlap': settings['overlap']}
            entry['title'] = str(entry.get('title', '')).strip()
            entry['content'] = str(entry.get('content', '')).strip()
            entry['tags'] = _normalise_tags(entry.get('tags'))
            content_changed = 'content' in data or not row
            if content_changed:
                entry = self._write_doc(conn, entry)
            else:
                entry['updated_at'] = now()
                conn.execute('INSERT OR REPLACE INTO knowledge VALUES (?,?,?)',
                             (entry_id, json.dumps(entry, ensure_ascii=False), entry['updated_at']))
        return self._public(entry)

    def import_file(self, filename, content, title=''):
        """解析上传文件 → 分块入库；同文查重，词法行同步可检索，嵌入失败保持待补。"""
        from . import kb_import
        parsed = kb_import.extract_file(filename, content)
        text = parsed['text']
        content_hash = digest(text)
        with self.repo.connection() as conn:
            dup = conn.execute("SELECT payload FROM knowledge WHERE json_extract(payload,'$.content_hash')=?", (content_hash,)).fetchone()
            if dup:
                dup_entry = json.loads(dup['payload'])
                raise ValueError(f'内容已存在：{dup_entry["title"]}（{dup_entry["id"]}），同一文档不重复入库')
        entry = dict(id='K-' + uuid.uuid4().hex[:12], title=(title or '').strip() or kb_import.title_from_filename(filename),
                     content=text, tags=[], created_at=now(), source='upload',
                     file_name=parsed['file_name'], file_size=parsed['file_size'])
        with self.repo.connection() as conn:
            entry = self._write_doc(conn, entry)
        return dict(entry=self._public(entry), warnings=parsed['warnings'])

    def delete(self, entry_id):
        with self.repo.connection() as conn:
            hashes = [row[0] for row in conn.execute('SELECT content_hash FROM knowledge_chunks WHERE doc_id=?', (entry_id,))]
            # 先清 FTS（要靠 chunks 表找 rowid），再删 chunks 本体，最后连带向量。
            conn.execute('DELETE FROM knowledge_fts WHERE rowid IN (SELECT id FROM knowledge_chunks WHERE doc_id=?)', (entry_id,))
            conn.execute('DELETE FROM knowledge_chunks WHERE doc_id=?', (entry_id,))
            cursor = conn.execute('DELETE FROM knowledge WHERE id=?', (entry_id,))
            for content_hash in hashes:
                conn.execute('DELETE FROM knowledge_vectors WHERE content_hash=?', (content_hash,))
            return cursor.rowcount > 0

    def get(self, entry_id):
        """全文返回：编辑弹窗与预览都依赖完整正文；截断只在列表（_public）。"""
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload FROM knowledge WHERE id=?', (entry_id,)).fetchone()
            if not row:
                return None
            entry = json.loads(row['payload'])
            return {**entry, 'characters': len(entry.get('content', ''))}

    def doc_chunks(self, entry_id):
        with self.repo.connection() as conn:
            return [dict(ordinal=row['ordinal'], text=row['text'], characters=len(row['text']))
                    for row in conn.execute('SELECT ordinal,text FROM knowledge_chunks WHERE doc_id=? ORDER BY ordinal', (entry_id,))]

    # ---------- listing & retrieval ----------

    def list(self, q='', tag='', page=1, page_size=20):
        """UI listing；q 走分块 FTS（与 Agent 同通道），命中时附最佳分块摘录。"""
        hit_by_doc = {}
        with self.repo.connection() as conn:
            entries = [json.loads(row['payload']) for row in
                       conn.execute('SELECT payload FROM knowledge ORDER BY updated_at DESC')]
            tags = sorted({tag for entry in entries for tag in entry['tags']})
            if tag:
                entries = [entry for entry in entries if tag in entry['tags']]
            if q.strip():
                from .research.query import tokenize
                terms = tokenize(q)
                if terms:
                    expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
                    rows = conn.execute('''SELECT c.doc_id,c.text FROM knowledge_fts
                                           JOIN knowledge_chunks c ON c.id=knowledge_fts.rowid
                                           WHERE knowledge_fts MATCH ? ORDER BY bm25(knowledge_fts)''', (expression,)).fetchall()
                    for row in rows:
                        hit_by_doc.setdefault(row['doc_id'], row['text'])
                entries = [entry for entry in entries if entry['id'] in hit_by_doc]
        entries = [self._public(entry) for entry in entries]
        listed = []
        for entry in entries:
            listed.append({**entry, 'hit_excerpt': (hit_by_doc.get(entry['id']) or entry['content'])[:LIST_SNIPPET_CHARS]})
        total = len(listed)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        return dict(entries=listed[(page - 1) * page_size:page * page_size], tags=tags, total=total,
                    page=page, page_size=page_size, pages=pages, **self.status())

    def _lexical(self, expression, limit):
        with self.repo.connection() as conn:
            return [dict(row) for row in conn.execute('''SELECT c.id,c.doc_id,c.ordinal,c.text,
                     json_extract(d.payload,'$.title') AS title, json_extract(d.payload,'$.tags') AS tags,
                     bm25(knowledge_fts) AS distance FROM knowledge_fts
                     JOIN knowledge_chunks c ON c.id=knowledge_fts.rowid
                     JOIN knowledge d ON d.id=c.doc_id
                     WHERE knowledge_fts MATCH ? ORDER BY distance,c.id LIMIT ?''', (expression, limit))]

    def _vector(self, query, provider, limit):
        query_vector = provider.embed([query])[0]
        qnorm = sum(x * x for x in query_vector) ** .5
        if not qnorm:
            return []
        with self.repo.connection() as conn:
            rows = [dict(row) for row in conn.execute('''SELECT c.id,c.doc_id,c.ordinal,c.text,
                     json_extract(d.payload,'$.title') AS title, json_extract(d.payload,'$.tags') AS tags, v.vector
                     FROM knowledge_chunks c JOIN knowledge_vectors v ON v.content_hash=c.content_hash AND v.model=?
                     JOIN knowledge d ON d.id=c.doc_id''', (provider.identity,))]
        scored = []
        for row in rows:
            vector = json.loads(row.pop('vector'))
            vnorm = sum(x * x for x in vector) ** .5
            if not vnorm:
                continue
            similarity = sum(a * b for a, b in zip(vector, query_vector)) / (qnorm * vnorm)
            scored.append({**row, 'similarity': similarity})
        scored.sort(key=lambda item: item['similarity'], reverse=True)
        return scored[:limit]

    def search(self, query, limit=5):
        """Agent/UI 检索：词法 BM25；hybrid 再加向量通道 RRF 融合（kb-kit 同权重）。"""
        from .research.query import tokenize
        terms = tokenize(query or '')
        if not terms:
            return dict(mode='lexical', degraded=[], hits=[])
        expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
        mode, degraded = self.settings()['retrieval_mode'], []
        lexical = self._lexical(expression, limit * 4)
        vector_rows = []
        if mode == 'hybrid':
            provider = self._embedder()
            if provider is None:
                degraded.append('嵌入服务未配置，混合检索降级为词法检索')
            else:
                try:
                    vector_rows = self._vector(query, provider, limit * 4)
                    if not vector_rows:
                        degraded.append('向量索引尚未建立（可在知识库页重建向量），已降级为词法检索')
                except (ValueError, RuntimeError) as exc:
                    degraded.append(f'向量召回失败，已降级为词法检索：{exc}')
        scores = {}
        for rank, row in enumerate(lexical, 1):
            scores.setdefault(row['id'], dict(row=row, score=0.0, lexical=0.0, vector=None))
            scores[row['id']]['score'] += LEXICAL_WEIGHT / (RRF_K + rank)
            scores[row['id']]['lexical'] = rank
        for rank, row in enumerate(vector_rows, 1):
            scores.setdefault(row['id'], dict(row=row, score=0.0, lexical=None, vector=0.0))
            scores[row['id']]['score'] += VECTOR_WEIGHT / (RRF_K + rank)
            scores[row['id']]['vector'] = round(row['similarity'], 4)
        max_raw = (VECTOR_WEIGHT + LEXICAL_WEIGHT) / (RRF_K + 1) if mode == 'hybrid' and vector_rows else LEXICAL_WEIGHT / (RRF_K + 1)
        ordered = sorted(scores.values(), key=lambda item: item['score'], reverse=True)[:limit]
        hits = []
        for item in ordered:
            row = item['row']
            hits.append(dict(doc_id=row['doc_id'], title=row['title'], tags=json.loads(row['tags']) if isinstance(row['tags'], str) else (row['tags'] or []),
                             ordinal=row['ordinal'], text=row['text'],
                             score=round(min(1.0, item['score'] / max_raw), 4),
                             lexical_rank=item['lexical'], vector_score=item['vector']))
        return dict(mode='hybrid' if (mode == 'hybrid' and vector_rows) else 'lexical', degraded=degraded, hits=hits)

    # ---------- vector status ----------

    def status(self):
        with self.repo.connection() as conn:
            total = conn.execute('SELECT count(*) FROM knowledge_chunks').fetchone()[0]
            docs = conn.execute('SELECT count(*) FROM knowledge').fetchone()[0]
            models = {row[0]: row[1] for row in conn.execute('SELECT model,count(*) FROM knowledge_vectors GROUP BY model')}
        identity = self._embedding_identity()
        embedded = models.get(identity, 0) if identity else 0
        stale = bool(models) and set(models) - {identity} and total - embedded > 0
        return dict(docs=docs, chunks=total, embedded=min(embedded, total), pending=max(0, total - embedded), stale=stale,
                    embedding_model=identity or None, embedding_ready=bool(identity))

    def reembed(self):
        provider = self._embedder()
        if provider is None:
            raise ValueError('嵌入服务未配置：请先在「模型设置 → Embedding」配置后再构建向量')
        with self.repo.connection() as conn:
            conn.execute('DELETE FROM knowledge_vectors')
            rows = conn.execute('SELECT text,content_hash FROM knowledge_chunks').fetchall()
        pairs = [(row['text'], row['content_hash']) for row in rows]
        for start in range(0, len(pairs), VECTOR_BATCH):
            batch = pairs[start:start + VECTOR_BATCH]
            vectors = provider.embed([text for text, _ in batch])
            with self.repo.connection() as conn:
                for (text, content_hash), vector in zip(batch, vectors):
                    conn.execute('INSERT OR REPLACE INTO knowledge_vectors VALUES (?,?,?)',
                                 (provider.identity, content_hash, json.dumps(vector)))
        return dict(embedded=len(pairs), model=provider.identity, **self.status())

    # ---------- helpers ----------

    @staticmethod
    def _public(entry):
        """对外条目：content 截断预览 + chunk 元数据；全文走 get/{id} 或分块接口。"""
        content = entry.get('content', '')
        return {**entry, 'content': content[:LIST_SNIPPET_CHARS],
                'content_truncated': len(content) > LIST_SNIPPET_CHARS, 'characters': len(content)}
