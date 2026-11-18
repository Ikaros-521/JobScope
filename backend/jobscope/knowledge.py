"""User-authored knowledge documents; chunked lexical/hybrid retrieval shared by UI and Agent tools.

分块/检索设计对齐 kb-kit（packages/core）：文档按策略切块后逐块入 FTS 与向量缓存；
检索 = 词法(BM25) 与可选向量通道的 RRF 融合；语义/LLM 分块依赖缺失时显式回退 recursive。
批量文件导入为后台任务：文件先落盘暂存、逐个解析入库（事务级），可取消、崩溃不留脏数据。
"""

import json
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path

from .kb_chunking import STRATEGIES, clamp_overlap, clamp_size, chunk_text, format_for
from .repository import digest, now

logger = logging.getLogger(__name__)

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
            except (ValueError, RuntimeError, OSError) as exc:
                logger.warning('知识嵌入失败（向量保持待补，词法不受影响）entry=%s：%s', entry['id'], exc)  # status 会如实报告 pending
        return entry

    def _embedding_identity(self):
        provider = self._embedder()
        return provider.identity if provider else None

    def _embed_chunks(self, pairs, should_stop=None):
        """pairs: [(text, content_hash)]，只为缺失/换模型的分块补向量；返回补上的数量。
        should_stop 为真时在批间停止（余下保持待补，词法检索不受影响）。"""
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
            if should_stop is not None and should_stop():
                break
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
                    logger.warning('知识向量召回失败，已降级词法检索：%s', exc)
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
        logger.info('知识向量重建完成：%s 个分块，模型=%s', len(pairs), provider.identity)
        return dict(embedded=len(pairs), model=provider.identity, **self.status())

    # ---------- helpers ----------

    @staticmethod
    def _public(entry):
        """对外条目：content 截断预览 + chunk 元数据；全文走 get/{id} 或分块接口。"""
        content = entry.get('content', '')
        return {**entry, 'content': content[:LIST_SNIPPET_CHARS],
                'content_truncated': len(content) > LIST_SNIPPET_CHARS, 'characters': len(content)}


class KnowledgeImportManager:
    """知识库批量导入任务：暂存→后台逐文件解析入库，可取消；崩溃/中断不留脏数据。

    生命周期：pending（前端逐文件上传暂存）→ running（后台线程逐文件解析、事务落库）
    → completed / failed / cancelled。脏数据防线：
    · 每个文件解析+分块+落库在单事务内完成，失败只影响该文件；
    · 内容 sha256 查重（含同批先入库的），重复文件 skip 不重复入库；
    · 取消只阻止「未来」文件，已导入文件是完整提交的文档，非半成品；
    · 向量补齐批间检查取消标志，中断只留下 pending 向量，词法检索不受影响；
    · 进程重启后：Repository 启动即把 running 任务标失败，本管理器初始化时清理孤儿暂存目录。
    并发纪律：create/stage/start/cancel 是任务 payload 的整包读改写，全部串行在 self._lock
    之后（FastAPI 同步端点跑在线程池，缺锁会互相覆盖状态）；running 期间 worker 独占任务
    写入，cancel 只置位取消事件、不写 payload。worker 收尾先落终态再摘取消事件，保证 cancel
    读到的任务状态与事件注册情况不会错位（否则会把已完成的任务改写成 cancelled）。
    """

    KIND = 'knowledge_import'
    ACTIVE = ('pending', 'running')
    MAX_FILES = 20
    _STAGING = 'knowledge_imports'

    # Windows 非法字符与保留设备名：文件名只用于暂存路径拼接，坏名不能携带目录成分、
    # 不能写出任务目录，也不能让 write_bytes 在非法字符上抛 OSError 变成 500。
    _UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    _RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL',
                       *(f'COM{n}' for n in range(1, 10)), *(f'LPT{n}' for n in range(1, 10))}

    def __init__(self, store, repo):
        self.store = store
        self.repo = repo
        self._lock = threading.Lock()
        self._cancel_events = {}
        self._cleanup_staging()

    @classmethod
    def _safe_name(cls, name):
        """非法字符换成下划线、去结尾点空格、保留名加前缀；扩展名保留供格式判定。
        create 与 stage 两侧同用本函数，登记名与上传名才能对得上。"""
        cleaned = cls._UNSAFE_NAME.sub('_', str(name).strip()).rstrip('. ')
        if not cleaned:
            cleaned = '未命名'
        if Path(cleaned).stem.upper() in cls._RESERVED_NAMES:
            cleaned = '_' + cleaned
        return cleaned[:200]

    # ---------- staging paths ----------

    def _staging_dir(self, task_id):
        return Path(self.repo.data_dir) / self._STAGING / task_id

    def _cleanup_staging(self):
        """进程启动时清理孤儿暂存目录：任务不存在或已非活动态即删（Repository 启动已把
        中断的 running 任务标失败，所以这里只需按当前任务状态判断）。"""
        root = Path(self.repo.data_dir) / self._STAGING
        if not root.is_dir():
            return
        for child in root.iterdir():
            task = self.get(child.name) if child.is_dir() else None
            if task is None or task['status'] not in self.ACTIVE:
                shutil.rmtree(child, ignore_errors=True)

    # ---------- task persistence ----------

    def _load(self, task_id):
        task = self.repo.task(task_id)
        return task if task and task.get('kind') == self.KIND else None

    def _save(self, task):
        task['updated_at'] = now()
        self.repo.update_task(task)
        return task

    def get(self, task_id):
        return self._load(task_id)

    def latest(self):
        with self.repo.connection() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE json_extract(payload,'$.kind')=? ORDER BY rowid DESC LIMIT 1",
                               (self.KIND,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---------- lifecycle ----------

    def create(self, names):
        with self._lock:
            names = [str(name).strip() for name in names if str(name).strip()]
            if not names:
                raise ValueError('请先选择要导入的文件')
            if len(names) > self.MAX_FILES:
                raise ValueError(f'单批最多 {self.MAX_FILES} 个文件，请分批导入')
            names = [self._safe_name(name) for name in names]
            latest = self.latest()
            if latest and latest['status'] in self.ACTIVE:
                raise ValueError('已有导入任务在进行中，请等待完成或先停止')
            files = [dict(index=index, name=name, size=None, state='pending', entry_id=None,
                          chunks=None, error=None, warnings=[]) for index, name in enumerate(names)]
            task = dict(id='T-' + uuid.uuid4().hex, source=self.KIND, kind=self.KIND,
                        query=f'{len(files)} 个文件', status='pending', message='等待文件上传',
                        created_at=now(), count=0, total=len(files), processed=0, failed=0, files=files)
            self._save(task)
            self._staging_dir(task['id']).mkdir(parents=True, exist_ok=True)
            return task

    def stage(self, task_id, index, filename, content):
        with self._lock:
            task = self._require_active(task_id)
            if task['status'] != 'pending':
                raise ValueError('导入已开始，不能再上传文件')
            if not 0 <= index < len(task['files']):
                raise ValueError('文件序号超出任务范围')
            declared = task['files'][index]
            if declared['name'] != self._safe_name(filename):
                raise ValueError(f'文件名与任务登记不一致：{declared["name"]}')
            from .kb_import import KB_MAX_FILE_BYTES
            if len(content) > KB_MAX_FILE_BYTES:
                raise ValueError(f'文件超过 {KB_MAX_FILE_BYTES // (1024 * 1024)} MB，请压缩或拆分后导入')
            if declared['state'] != 'pending':
                raise ValueError('该文件已上传过，请重新创建导入任务')
            target = self._staging_dir(task_id) / f'{index:04d}-{declared["name"]}'
            target.write_bytes(content)
            declared.update(size=len(content), state='staged')
            staged = sum(1 for f in task['files'] if f['state'] == 'staged')
            task['message'] = f'已上传 {staged}/{len(task["files"])} 个文件'
            self._save(task)
            return task

    def start(self, task_id):
        # 落库 running、注册取消事件、起线程三步必须在锁内一次完成：否则 cancel 会在
        # 「状态已 running 但事件未注册」的窗口里走 pending 分支，把任务误标取消。
        with self._lock:
            task = self._require_active(task_id)
            if task['status'] != 'pending':
                raise ValueError('导入任务已开始')
            pending = [f['name'] for f in task['files'] if f['state'] != 'staged']
            if pending:
                raise ValueError('仍有未上传完成的文件：' + '、'.join(pending[:3]))
            task.update(status='running', message='正在导入…')
            self._save(task)
            event = threading.Event()
            self._cancel_events[task_id] = event
            settings = self.store.settings()
            thread = threading.Thread(target=self._run, args=(task_id, event, settings), daemon=True)
            thread.start()
            return task

    def cancel(self, task_id):
        with self._lock:
            task = self._load(task_id)
            if task is None:
                raise ValueError('导入任务不存在')
            if task['status'] not in self.ACTIVE:
                raise ValueError('任务已结束，无法停止')
            event = self._cancel_events.get(task_id)
            if event is not None:
                # running：只设置取消标志，不写任务载荷——running 期间 worker 独占任务写入，
                # 双写会互相覆盖文件状态；worker 在当前文件后停止并把余下文件标记取消。
                event.set()
                return task
            # pending（还在收文件）：直接取消并清理暂存。锁保证此刻不会有并发的 stage/start。
            task.update(status='cancelled', message='已取消，未导入任何文件')
            for file_state in task['files']:
                if file_state['state'] in ('pending', 'staged'):
                    file_state['state'] = 'cancelled'
            self._save(task)
            shutil.rmtree(self._staging_dir(task_id), ignore_errors=True)
            return task

    def _require_active(self, task_id):
        task = self._load(task_id)
        if task is None or task['status'] not in self.ACTIVE:
            raise ValueError('导入任务不存在或已结束')
        return task

    # ---------- worker ----------

    def _run(self, task_id, event, settings):
        task = self._load(task_id)
        try:
            # worker 线程收不到请求中间件的线程级模型配置（contextvar 不跨线程），
            # LLM 切分按 DB 生效配置解析，否则回落环境变量 → 地址为空。
            from .settings import ModelSettings, use_config
            with use_config(ModelSettings(self.repo).effective()):
                self._process_files(task, event, settings)
        except Exception as exc:  # noqa: BLE001 — 后台线程异常必须落库可见，静默死亡会让任务永远停在 running
            logger.exception('知识库导入任务异常终止 task=%s', task_id)
            # 中途死掉的文件不能留在 processing：那是不可见的中间态，重启清理只看任务状态。
            for file_state in task.get('files', []):
                if file_state['state'] == 'processing':
                    file_state.update(state='failed', error=f'任务异常终止：{exc}'[:200])
            task.update(status='failed', message=f'导入任务异常终止：{exc}'[:500])
        finally:
            # 先落终态再摘取消事件：cancel 在锁内按「状态 + 事件」决定走哪个分支，顺序反了会
            # 出现「事件已摘、状态还是 running」的窗口，cancel 走 pending 分支覆盖掉 completed。
            self._save(task)
            with self._lock:
                self._cancel_events.pop(task_id, None)
            shutil.rmtree(self._staging_dir(task_id), ignore_errors=True)

    def _process_files(self, task, event, settings):
        task_id = task['id']
        imported = failed = skipped = 0
        logger.info('知识库导入开始 task=%s 文件=%s', task_id, len(task['files']))
        for file_state in task['files']:
            if event.is_set():
                break
            if file_state['state'] != 'staged':
                continue
            file_state['state'] = 'processing'
            task['message'] = f'正在导入 {imported + failed + skipped + 1}/{len(task["files"])}：{file_state["name"]}'
            self._save(task)
            try:
                outcome = self._process_file(task_id, file_state, settings)
                if outcome.get('skipped'):
                    skipped += 1
                    file_state.update(state='skipped', error=outcome.get('error'))
                else:
                    imported += 1
                    file_state.update(state='imported', entry_id=outcome['entry']['id'],
                                      chunks=outcome['entry']['chunk_count'], warnings=outcome.get('warnings') or [])
            except (ValueError, RuntimeError, OSError) as exc:
                failed += 1
                file_state.update(state='failed', error=str(exc))
            staged_file = self._staging_dir(task_id) / f'{file_state["index"]:04d}-{file_state["name"]}'
            staged_file.unlink(missing_ok=True)
        cancelled = event.is_set()
        for file_state in task['files']:
            if file_state['state'] in ('pending', 'staged'):
                file_state['state'] = 'cancelled'
        # 向量补齐：批间响应取消；取消/失败只留下 pending 向量，不影响已入库文档。
        embedded_note = ''
        if imported and not cancelled:
            pairs = self._pending_pairs([f['entry_id'] for f in task['files'] if f['state'] == 'imported'])
            if pairs:
                task['message'] = f'导入完成，向量补齐中（{len(pairs)} 个分块）…'
                self._save(task)
                done = self.store._embed_chunks(pairs, should_stop=event.is_set)
                if done < len(pairs):
                    embedded_note = f'；向量补齐被中断（剩 {len(pairs) - done} 个分块待补，可稍后重建）'
        task['count'], task['processed'], task['failed'] = imported, imported + failed + skipped, failed
        if cancelled:
            task.update(status='cancelled', message=f'已停止：成功导入 {imported} 个文件，余下文件未导入')
        elif imported == 0 and failed > 0:
            first_error = next((f['error'] for f in task['files'] if f['state'] == 'failed'), '')
            task.update(status='failed', message=f'导入失败（{failed} 个文件均失败）：{first_error}'[:500])
        else:
            note = f'（{skipped} 个重复跳过）' if skipped else ''
            task.update(status='completed',
                        message=f'导入完成：成功 {imported}、失败 {failed}{note}{embedded_note}'[:500])
        logger.info('知识库导入结束 task=%s：成功=%s 失败=%s 跳过=%s', task_id, imported, failed, skipped)

    def _process_file(self, task_id, file_state, settings):
        content = (self._staging_dir(task_id) / f'{file_state["index"]:04d}-{file_state["name"]}').read_bytes()
        from . import kb_import
        parsed = kb_import.extract_file(file_state['name'], content)
        content_hash = digest(parsed['text'])
        with self.repo.connection() as conn:
            dup = conn.execute("SELECT payload FROM knowledge WHERE json_extract(payload,'$.content_hash')=?",
                               (content_hash,)).fetchone()
            if dup:
                dup_entry = json.loads(dup['payload'])
                return dict(skipped=True, error=f'内容已存在：{dup_entry["title"]}')
        entry = dict(id='K-' + uuid.uuid4().hex, title=kb_import.title_from_filename(file_state['name']),
                     content=parsed['text'], tags=[], created_at=now(), source='upload',
                     file_name=parsed['file_name'], file_size=parsed['file_size'],
                     chunk_strategy=settings['strategy'], chunk_size=settings['size'],
                     chunk_overlap=settings['overlap'])
        with self.repo.connection() as conn:
            entry = self.store._write_doc(conn, entry, strategy=settings['strategy'], size=settings['size'],
                                          overlap=settings['overlap'], embed=False)
        return dict(entry=entry, warnings=parsed['warnings'])

    def _pending_pairs(self, doc_ids):
        from .research.providers import EmbeddingProvider
        provider = EmbeddingProvider()
        if not provider.configured or not doc_ids:
            return []
        pairs = []
        with self.repo.connection() as conn:
            for doc_id in doc_ids:
                rows = conn.execute('''SELECT c.text,c.content_hash FROM knowledge_chunks c
                     LEFT JOIN knowledge_vectors v ON v.content_hash=c.content_hash AND v.model=?
                     WHERE c.doc_id=? AND v.content_hash IS NULL''', (provider.identity, doc_id)).fetchall()
                pairs.extend((row['text'], row['content_hash']) for row in rows)
        return pairs
