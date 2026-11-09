"""SQLite persistence. All source revisions are immutable evidence records."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "jobscope.sqlite3"
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, company TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_evidence (job_id TEXT REFERENCES jobs(id), evidence_id TEXT REFERENCES evidence(id), PRIMARY KEY(job_id,evidence_id));
                CREATE TABLE IF NOT EXISTS resume (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, format TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, scope TEXT NOT NULL, messages TEXT NOT NULL);
            """)
            self._migrate_conversations(conn)
            # A previous process can no longer complete its in-flight synchronous tasks.
            for row in conn.execute("SELECT id,payload FROM tasks").fetchall():
                task = json.loads(row["payload"])
                if task["status"] == "running":
                    task.update(status="failed", message="上次进程中断，请重新采集")
                    conn.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), row["id"]))

    def _migrate_conversations(self, conn):
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(conversations)')}
        for name, definition in (
            ('title', "TEXT NOT NULL DEFAULT ''"),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('updated_at', "TEXT NOT NULL DEFAULT ''"),
            ('mode', "TEXT NOT NULL DEFAULT 'research'"),
            ('model', "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f'ALTER TABLE conversations ADD COLUMN {name} {definition}')
        # Legacy global conversations bound agent_mode into the scope; modes are now switchable per conversation.
        for row in conn.execute('SELECT id,scope FROM conversations').fetchall():
            try:
                scope = json.loads(row['scope'])
            except ValueError:
                continue
            if scope.get('kind') == 'global' and 'agent_mode' in scope:
                scope.pop('agent_mode')
                conn.execute('UPDATE conversations SET scope=? WHERE id=?', (json.dumps(scope, sort_keys=True, ensure_ascii=False), row['id']))
        conn.execute("UPDATE conversations SET created_at=?, updated_at=? WHERE created_at='' OR updated_at=''", (now(), now()))

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_evidence(self, data, source="user_import"):
        item = {"title": "资料", "url": "", "company": "", "kind": "user_material", "text": "", "published_at": None, **data}
        item.pop("id", None)
        item["source"] = source
        item["content_hash"] = digest(item["text"])
        identity = digest(json.dumps([source, item["company"], item["url"], item["kind"], item["title"], item["published_at"], item["content_hash"]], ensure_ascii=False))
        item.update(id="E-" + identity[:24], collected_at=now())
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)", (item["id"], identity, item["company"], item["kind"], json.dumps(item, ensure_ascii=False)))
            return json.loads(conn.execute("SELECT payload FROM evidence WHERE identity=?", (identity,)).fetchone()["payload"])

    def save_job(self, data, source):
        item = {"title": "", "company": "", "description": "", "location": "", "salary": "", "url": "", "tags": [], **data}
        key = item.get("external_id") or item.get("url") or json.dumps([item["title"], item["company"], item["location"]], ensure_ascii=False)
        item["id"] = "J-" + digest(source + ":" + str(key))[:24]
        previous = self.job(item["id"])
        if source == 'boss' and previous and previous.get('description_status') == 'complete' and item.get('description_status') != 'complete':
            for field in ('description', 'description_status', 'detail_collected_at', 'detail_source_url', 'detail_transport'):
                if field in previous:
                    item[field] = previous[field]
        item.update(source=source, updated_at=now(), evidence_ids=[])
        # Every job revision retains a complete attributable snapshot.
        body = "\n".join([item["title"], item["company"], item["location"], item["salary"], item["description"]])
        evidence = self.save_evidence(dict(title=item["title"], company=item["company"], url=item["url"], kind="job_posting", text=body, published_at=item.get("published_at")), source)
        with self.connection() as conn:
            conn.execute("INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (item["id"], json.dumps(item, ensure_ascii=False), item["updated_at"]))
            conn.execute("INSERT OR IGNORE INTO job_evidence VALUES (?,?)", (item["id"], evidence["id"]))
        return self.job(item["id"])

    def link_evidence(self, job_id, evidence_id):
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO job_evidence VALUES (?,?)", (job_id, evidence_id))

    def job(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            item = json.loads(row["payload"])
            item["evidence_ids"] = [r[0] for r in conn.execute("SELECT evidence_id FROM job_evidence WHERE job_id=? ORDER BY rowid", (job_id,))]
            return item

    def jobs(self, q="", location=""):
        with self.connection() as conn:
            rows = conn.execute("SELECT id FROM jobs ORDER BY updated_at DESC").fetchall()
        items = [self.job(r["id"]) for r in rows]
        return [item for item in items if q.casefold() in " ".join([item["title"], item["company"], item["description"]]).casefold() and location.casefold() in item["location"].casefold()]

    def evidence(self, company=None, q="", include_job_postings=True):
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM evidence" + (" WHERE company=?" if company is not None else "") + " ORDER BY rowid DESC", (company,) if company is not None else ()).fetchall()
        items = [json.loads(r["payload"]) for r in rows]
        return [item for item in items if (include_job_postings or item["kind"] != "job_posting") and q.casefold() in (item["title"] + " " + item["text"]).casefold()]

    def job_evidence(self, job_id):
        with self.connection() as conn:
            rows = conn.execute("SELECT e.payload FROM evidence e JOIN job_evidence j ON e.id=j.evidence_id WHERE j.job_id=? ORDER BY e.rowid DESC", (job_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_evidence(self, evidence_id):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def resume(self):
        with self.connection() as conn:
            row = conn.execute("SELECT text,format,revision FROM resume WHERE id=1").fetchone()
            return dict(row) if row else dict(text="", format="text", revision=0)

    def save_resume(self, text, format):
        old = self.resume()
        revision = old["revision"] + int((old["text"], old["format"]) != (text, format))
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO resume VALUES (1,?,?,?)", (text, format, revision))
        return self.resume()

    def save_report(self, report):
        with self.connection() as conn:
            conn.execute("INSERT INTO reports VALUES (?,?,?,?)", (str(uuid.uuid4()), report["job_id"], now(), json.dumps(report, ensure_ascii=False)))

    def create_task(self, source, query):
        task = dict(id="T-" + uuid.uuid4().hex, source=source, query=query, status="running", message="采集中", created_at=now(), count=0)
        self.update_task(task)
        return task

    def update_task(self, task):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))

    def tasks(self):
        with self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM tasks ORDER BY rowid DESC LIMIT 100")]

    def page(self, resource, page=1, page_size=20, q='', location='', company=None, job_id=None,
             linked_job_id=None, include_job_postings=True):
        """Bounded SQL queries; filters run before count and pagination."""
        where, params = [], []
        if resource == 'jobs':
            table, order = 'jobs', 'updated_at DESC, id DESC'
            if q:
                where.append("instr(lower(json_extract(payload,'$.title') || ' ' || json_extract(payload,'$.company') || ' ' || json_extract(payload,'$.description')),lower(?)) > 0")
                params.append(q)
            if location:
                where.append("instr(lower(json_extract(payload,'$.location')),lower(?)) > 0")
                params.append(location)
        elif resource == 'evidence':
            table, order = 'evidence', 'rowid DESC'
            if not include_job_postings:
                where.append("kind!='job_posting'")
            if company is not None:
                where.append('company=?')
                params.append(company)
            # 空格分词：每个词都要在标题或正文中出现，缺一词即排除。
            for term in q.split():
                where.append("instr(lower(json_extract(payload,'$.title') || ' ' || json_extract(payload,'$.text')),lower(?)) > 0")
                params.append(term)
            if job_id:
                where.append("company=(SELECT json_extract(payload,'$.company') FROM jobs WHERE id=?)")
                params.append(job_id)
                where.append("(id IN (SELECT evidence_id FROM job_evidence WHERE job_id=?) OR (company=(SELECT json_extract(payload,'$.company') FROM jobs WHERE id=?) AND kind!='job_posting'))")
                params.extend([job_id, job_id])
            if linked_job_id:
                where.append('id IN (SELECT evidence_id FROM job_evidence WHERE job_id=?)')
                params.append(linked_job_id)
        elif resource == 'tasks':
            table, order = 'tasks', 'rowid DESC'
        else:
            raise ValueError('Unknown resource')
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with self.connection() as conn:
            total = conn.execute(f'SELECT count(*) FROM {table}' + clause, params).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = conn.execute(f'SELECT payload FROM {table}' + clause + f' ORDER BY {order} LIMIT ? OFFSET ?',
                                [*params, page_size, (page - 1) * page_size]).fetchall()
        items = [json.loads(row[0]) for row in rows]
        if resource == 'jobs':
            items = [self.job(item['id']) for item in items]
        return {resource: items, 'page': page, 'page_size': page_size, 'total': total, 'pages': pages}

    def companies_page(self, q='', page=1, page_size=20):
        with self.connection() as conn:
            sql = "SELECT company, count(*) AS evidence_count FROM evidence WHERE company!='' AND instr(lower(company),lower(?))>0 GROUP BY company"
            total = conn.execute('SELECT count(*) FROM (' + sql + ')', (q,)).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = conn.execute(sql + ' ORDER BY company LIMIT ? OFFSET ?', (q, page_size, (page - 1) * page_size)).fetchall()
        return dict(companies=[dict(r) for r in rows], total=total, pages=pages, page=page, page_size=page_size)

    def conversation(self, conversation_id, scope):
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            if conversation_id:
                row = conn.execute('SELECT scope,messages FROM conversations WHERE id=?', (conversation_id,)).fetchone()
                if row is None or row['scope'] != encoded:
                    raise ValueError('会话范围不匹配，请为当前范围新建对话')
                return conversation_id, json.loads(row['messages'])
            conversation_id = 'C-' + uuid.uuid4().hex
            conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)', (conversation_id, encoded, '[]', '', now(), now(), 'research', ''))
        return conversation_id, []

    def create_conversation(self, scope, mode='research', model=''):
        conversation_id = 'C-' + uuid.uuid4().hex
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)', (conversation_id, encoded, '[]', '', now(), now(), mode, model))
        return conversation_id

    @staticmethod
    def _stamp(entry):
        item = dict(entry)
        item.setdefault('id', 'M-' + uuid.uuid4().hex[:16])
        item.setdefault('ts', now())
        return item

    def _fill_message_ids(self, messages):
        changed = False
        for item in messages:
            if not item.get('id'):
                item.update(id='M-' + uuid.uuid4().hex[:16], ts=now())
                changed = True
        return changed

    def append_messages(self, conversation_id, entries):
        stamped = [self._stamp(entry) for entry in entries]
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT messages FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            if row is None:
                raise ValueError('会话不存在')
            messages = json.loads(row[0])
            self._fill_message_ids(messages)
            messages = (messages + stamped)[-200:]
            conn.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), now(), conversation_id))
        return stamped

    def append_conversation(self, conversation_id, message, answer):
        self.append_messages(conversation_id, [dict(role='user', content=message), dict(role='assistant', content=answer)])

    def list_conversations(self, scope, limit=200):
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            rows = conn.execute('SELECT id,title,mode,model,updated_at,messages FROM conversations WHERE scope=? ORDER BY updated_at DESC LIMIT ?', (encoded, limit)).fetchall()
        result = []
        for row in rows:
            messages = json.loads(row['messages'])
            preview = next((m['content'][:80] for m in messages if m.get('role') == 'user'), '')
            result.append(dict(id=row['id'], title=row['title'] or preview or '新对话', mode=row['mode'] or 'research', model=row['model'], updated_at=row['updated_at'], message_count=len(messages), preview=preview))
        return result

    def get_conversation(self, conversation_id, scope=None):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None or (scope is not None and row['scope'] != json.dumps(scope, sort_keys=True, ensure_ascii=False)):
            raise ValueError('会话不存在或范围不匹配')
        messages = json.loads(row['messages'])
        if self._fill_message_ids(messages):
            with self.connection() as conn:
                conn.execute('UPDATE conversations SET messages=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), conversation_id))
        return dict(id=row['id'], scope=json.loads(row['scope']), title=row['title'] or '新对话', mode=row['mode'] or 'research', model=row['model'], created_at=row['created_at'], updated_at=row['updated_at'], messages=messages)

    def _conversation_row(self, conn, conversation_id, scope=None):
        row = conn.execute('SELECT scope,title,messages FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None or (scope is not None and row['scope'] != json.dumps(scope, sort_keys=True, ensure_ascii=False)):
            raise ValueError('会话不存在或范围不匹配')
        return row

    def update_conversation(self, conversation_id, scope=None, *, title=None, mode=None, model=None):
        fields, values = [], []
        if title is not None:
            fields.append('title=?')
            values.append(title[:100])
        if mode is not None:
            fields.append('mode=?')
            values.append(mode)
        if model is not None:
            fields.append('model=?')
            values.append(model[:200])
        if not fields:
            return
        clause = ' AND scope=?' if scope is not None else ''
        with self.connection() as conn:
            cursor = conn.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id=?{clause}", values + [conversation_id] + ([json.dumps(scope, sort_keys=True, ensure_ascii=False)] if scope is not None else []))
            if cursor.rowcount == 0:
                raise ValueError('会话不存在或范围不匹配')

    def ensure_title(self, conversation_id, scope, title):
        """Set the auto title only when the conversation has no user-facing title yet."""
        with self.connection() as conn:
            row = self._conversation_row(conn, conversation_id, scope)
            if row['title']:
                return
            stored = json.loads(row['messages'])
            self._fill_message_ids(stored)
            conn.execute('UPDATE conversations SET title=?, messages=? WHERE id=?', (title[:100], json.dumps(stored, ensure_ascii=False), conversation_id))

    def delete_conversation(self, conversation_id, scope=None):
        clause = ' AND scope=?' if scope is not None else ''
        values = [conversation_id] + ([json.dumps(scope, sort_keys=True, ensure_ascii=False)] if scope is not None else [])
        with self.connection() as conn:
            cursor = conn.execute('DELETE FROM conversations WHERE id=?' + clause, values)
            if cursor.rowcount == 0:
                raise ValueError('会话不存在或范围不匹配')

    def delete_message(self, conversation_id, scope, message_id):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._conversation_row(conn, conversation_id, scope)
            messages = json.loads(row['messages'])
            index = next((i for i, item in enumerate(messages) if item.get('id') == message_id), None)
            if index is None:
                raise ValueError('消息不存在')
            # Deleting a user message takes its direct assistant reply with it.
            removed = 2 if messages[index].get('role') == 'user' and index + 1 < len(messages) and messages[index + 1].get('role') == 'assistant' else 1
            messages = messages[:index] + messages[index + removed:]
            conn.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), now(), conversation_id))

    def truncate_for_edit(self, conversation_id, scope, message_id):
        """Drop the user message at message_id and everything after it; returns the remaining history."""
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._conversation_row(conn, conversation_id, scope)
            messages = json.loads(row['messages'])
            index = next((i for i, item in enumerate(messages) if item.get('id') == message_id), None)
            if index is None:
                raise ValueError('消息不存在')
            if messages[index].get('role') != 'user':
                raise ValueError('只能对用户消息执行重新生成')
            messages = messages[:index]
            conn.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), now(), conversation_id))
            return messages
