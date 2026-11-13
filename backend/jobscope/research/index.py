"""Incremental, rebuildable read models. Archived evidence stays immutable."""
import json
import math
import re

from ..analysis import parse_salary
from ..repository import digest, now
from .query import tokenize

VERSION = 'research-index-3'


def chunks(text, size=1400, overlap=200):
    markers = list(re.finditer(r'[【\[]\s*(?:第\s*\d+\s*页|Page\s+\d+)\s*[】\]]', text, re.I))
    boundaries = sorted(set([0, *[m.start() for m in markers], len(text)]))
    for begin, end in zip(boundaries, boundaries[1:]):
        page = next((m[0] for m in reversed(markers) if m.start() <= begin), '')
        for start in range(begin, end, size - overlap):
            stop = min(end, start + size)
            content = text[start:stop].strip()
            if content:
                yield dict(text=(page + '\n' if page and not content.startswith(page) else '') + content,
                           start=start, end=stop, page=page)
            if stop == end:
                break


def scope_clause(scope, plan=None, alias='d'):
    predicates, values = [], []
    if scope['kind'] != 'global':
        predicates.append(f'{alias}.company=?')
        values.append(scope['company'])
    if scope['kind'] == 'job':
        predicates.append(f"({alias}.evidence_id IN (SELECT evidence_id FROM research_job_documents WHERE job_id=?) OR {alias}.kind!='job_posting')")
        values.append(scope['job_id'])
    if plan and plan.companies:
        predicates.append(f"{alias}.company IN ({','.join('?' for _ in plan.companies)})")
        values.extend(plan.companies)
    if plan and (plan.location or plan.threshold is not None or plan.skills):
        filters, params = [], []
        if plan.location:
            filters.append('instr(j.location,?)>0')
            params.append(plan.location)
        if plan.threshold is not None:
            operator = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}[plan.operator]
            filters.append(f'j.{plan.threshold_basis}{operator}?')
            params.append(plan.threshold)
        # Salary/location restrictions apply to job evidence, while company
        # documents remain available as explicitly non-job background.
        if filters:
            predicates.append(f"({alias}.kind!='job_posting' OR {alias}.evidence_id IN (SELECT jd.evidence_id FROM research_jobs j JOIN research_job_documents jd ON jd.job_id=j.job_id WHERE " + ' AND '.join(filters) + '))')
            values.extend(params)
    return (' AND '.join(predicates) or '1=1'), values


class ResearchIndex:
    def __init__(self, repo):
        self.repo = repo
        # DDL 只在同一个 repo 实例上跑一次：状态端点每次请求都构造 ResearchIndex，
        # 稳态下 10 条 IF NOT EXISTS 只是重复的 schema 检查。
        if getattr(repo, '_research_schema_ready', False):
            return
        with repo.connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS research_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_docs(evidence_id TEXT PRIMARY KEY,company TEXT NOT NULL,kind TEXT NOT NULL,
                    job_id TEXT,title TEXT NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,published_at TEXT,collected_at TEXT,content_hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_company ON research_docs(company,kind,job_id);
                CREATE TABLE IF NOT EXISTS research_job_documents(job_id TEXT PRIMARY KEY,evidence_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,evidence_id TEXT NOT NULL,
                    text TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,page TEXT NOT NULL,content_hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_chunk_doc ON research_chunks(evidence_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(body,title,company);
                CREATE TABLE IF NOT EXISTS research_jobs(job_id TEXT PRIMARY KEY,company TEXT NOT NULL,location TEXT NOT NULL,
                    title TEXT NOT NULL,salary TEXT NOT NULL,monthly_min REAL,monthly_max REAL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_salary ON research_jobs(company,monthly_min,monthly_max);
                CREATE TABLE IF NOT EXISTS research_vectors(model TEXT NOT NULL,content_hash TEXT NOT NULL,vector TEXT NOT NULL,
                    PRIMARY KEY(model,content_hash));
                CREATE TABLE IF NOT EXISTS research_runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
        repo._research_schema_ready = True

    def needs_sync(self):
        """只读探测是否有未并入索引的变更（与 sync 的检测口径一致）。

        False 时状态端点可直接读 status()，不必进 sync 起手的 BEGIN IMMEDIATE 写事务——
        后台采集正在写库时，GET 抢写锁会把「看一眼状态」卡成等锁。
        """
        with self.repo.connection() as c:
            version = c.execute("SELECT value FROM research_meta WHERE key='version'").fetchone()
            if not version or version[0] != VERSION:
                return True
            marker = c.execute("SELECT value FROM research_meta WHERE key='evidence_cursor'").fetchone()
            cursor = int(marker[0]) if marker else 0
            if c.execute('SELECT 1 FROM evidence WHERE rowid>? LIMIT 1', (cursor,)).fetchone():
                return True
            return bool(c.execute('''SELECT 1 FROM jobs j LEFT JOIN research_jobs r ON r.job_id=j.id
                                     WHERE r.job_id IS NULL OR r.updated_at!=j.updated_at LIMIT 1''').fetchone())

    def _remove(self, c, evidence_id):
        c.execute('DELETE FROM research_fts WHERE rowid IN (SELECT id FROM research_chunks WHERE evidence_id=?)', (evidence_id,))
        c.execute('DELETE FROM research_chunks WHERE evidence_id=?', (evidence_id,))
        c.execute('DELETE FROM research_docs WHERE evidence_id=?', (evidence_id,))

    def _document(self, c, item, job_id=None):
        if c.execute('SELECT 1 FROM research_docs WHERE evidence_id=?', (item['id'],)).fetchone():
            return
        c.execute('INSERT INTO research_docs VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (item['id'], item['company'], item['kind'], job_id, item['title'], item.get('url', ''), item['source'],
                   item.get('published_at'), item.get('collected_at'), item['content_hash']))
        for chunk in chunks(item['text']):
            row = c.execute('INSERT INTO research_chunks(evidence_id,text,start,end,page,content_hash) VALUES (?,?,?,?,?,?)',
                            (item['id'], chunk['text'], chunk['start'], chunk['end'], chunk['page'], digest(chunk['text'])))
            c.execute('INSERT INTO research_fts(rowid,body,title,company) VALUES (?,?,?,?)',
                      (row.lastrowid, ' '.join(tokenize(chunk['text'])), ' '.join(tokenize(item['title'])), ' '.join(tokenize(item['company']))))

    def sync(self, rebuild=False):
        updated = 0
        with self.repo.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            version = c.execute("SELECT value FROM research_meta WHERE key='version'").fetchone()
            if rebuild or not version or version[0] != VERSION:
                for table in ('research_fts', 'research_chunks', 'research_docs', 'research_jobs', 'research_meta', 'research_job_documents'):
                    c.execute(f'DELETE FROM {table}')
            marker = c.execute("SELECT value FROM research_meta WHERE key='evidence_cursor'").fetchone()
            cursor = int(marker[0]) if marker else 0
            rows = c.execute('SELECT rowid,payload FROM evidence WHERE rowid>? ORDER BY rowid', (cursor,)).fetchall()
            for row in rows:
                item = json.loads(row['payload'])
                if item['kind'] != 'job_posting':
                    self._document(c, item)
                    updated += 1
                cursor = row['rowid']
            jobs = c.execute('''SELECT j.payload FROM jobs j LEFT JOIN research_jobs r ON r.job_id=j.id
                                WHERE r.job_id IS NULL OR r.updated_at!=j.updated_at''').fetchall()
            for row in jobs:
                item = json.loads(row[0])
                old = c.execute('SELECT evidence_id FROM research_job_documents WHERE job_id=?', (item['id'],)).fetchall()
                if item.get('description_source') == 'user' and item.get('manual_description_evidence_id'):
                    # A user supplement has its own provenance. Later salary/location changes must not
                    # unindex this JD or reattribute its text to the platform that supplied the listing.
                    evidence = c.execute('''SELECT e.payload FROM evidence e JOIN job_evidence je ON je.evidence_id=e.id
                        WHERE je.job_id=? AND e.company=? AND e.kind='job_posting' AND e.id=?
                        AND json_extract(e.payload,'$.source')='user_import' ''',
                        (item['id'], item['company'], item['manual_description_evidence_id'])).fetchone()
                else:
                    body = '\n'.join(item.get(k, '') for k in ('title', 'company', 'location', 'salary', 'description'))
                    evidence = c.execute('''SELECT e.payload FROM evidence e JOIN job_evidence je ON je.evidence_id=e.id
                        WHERE je.job_id=? AND e.company=? AND json_extract(e.payload,'$.content_hash')=? ORDER BY e.rowid DESC LIMIT 1''',
                                         (item['id'], item['company'], digest(body))).fetchone()
                current = json.loads(evidence[0]) if evidence else None
                c.execute('DELETE FROM research_job_documents WHERE job_id=?', (item['id'],))
                if current:
                    c.execute('INSERT INTO research_job_documents VALUES (?,?)', (item['id'], current['id']))
                for document in old:
                    if (not current or document[0] != current['id']) and not c.execute('SELECT 1 FROM research_job_documents WHERE evidence_id=?', (document[0],)).fetchone():
                        self._remove(c, document[0])
                if current:
                    self._document(c, current, item['id'])
                salary = parse_salary(item.get('salary', ''))
                payload = dict(job_id=item['id'], title=item['title'], company=item['company'], location=item.get('location', ''),
                               evidence_id=current['id'] if current else None, url=item.get('url', ''),
                               search_text=' '.join((item['title'], item.get('description', ''))), **salary)
                c.execute('INSERT OR REPLACE INTO research_jobs VALUES (?,?,?,?,?,?,?,?,?)',
                          (item['id'], item['company'], item.get('location', ''), item['title'], item.get('salary', ''),
                           salary['monthly_min'], salary['monthly_max'], json.dumps(payload, ensure_ascii=False), item['updated_at']))
                updated += 1
            for key, value in [('version', VERSION), ('evidence_cursor', str(cursor)), ('updated_at', now())]:
                c.execute('INSERT OR REPLACE INTO research_meta VALUES (?,?)', (key, value))
        return dict(updated=updated, **self.status())

    def status(self):
        with self.repo.connection() as c:
            return dict(version=VERSION, documents=c.execute('SELECT count(*) FROM research_docs').fetchone()[0],
                        chunks=c.execute('SELECT count(*) FROM research_chunks').fetchone()[0],
                        jobs=c.execute('SELECT count(*) FROM research_jobs').fetchone()[0],
                        cached_vectors=c.execute('SELECT count(*) FROM research_vectors').fetchone()[0])

    def embed(self, provider):
        self.sync()
        count = 0
        with self.repo.connection() as c:
            rows = c.execute('''SELECT DISTINCT c.content_hash,c.text FROM research_chunks c LEFT JOIN research_vectors v
                ON v.content_hash=c.content_hash AND v.model=? WHERE v.content_hash IS NULL''', (provider.identity,)).fetchall()
        for start in range(0, len(rows), 24):
            batch = rows[start:start + 24]
            vectors = provider.embed([r['text'] for r in batch])
            validate_vectors(vectors, len(batch))
            with self.repo.connection() as c:
                old = c.execute('SELECT vector FROM research_vectors WHERE model=? LIMIT 1', (provider.identity,)).fetchone()
                if old and len(json.loads(old[0])) != len(vectors[0]):
                    raise ValueError('Embedding维度变化，请更换模型版本标识或重建向量缓存')
                for row, vector in zip(batch, vectors):
                    c.execute('INSERT OR REPLACE INTO research_vectors VALUES (?,?,?)', (provider.identity, row['content_hash'], json.dumps(vector)))
                    count += 1
        return dict(embedded=count, model=provider.identity)


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ValueError('Embedding数量不一致')
    dim = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not 1 <= dim <= 8192:
        raise ValueError('Embedding维度无效')
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dim or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector) or not any(vector):
            raise ValueError('Embedding包含无效向量')
