"""SQLite persistence. All source revisions are immutable evidence records."""

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, company TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_evidence (job_id TEXT REFERENCES jobs(id), evidence_id TEXT REFERENCES evidence(id), PRIMARY KEY(job_id,evidence_id));
                CREATE TABLE IF NOT EXISTS resume (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, format TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, scope TEXT NOT NULL, messages TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, name TEXT NOT NULL, credit_code TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]', payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_signals (id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), identity TEXT UNIQUE NOT NULL, rule TEXT NOT NULL, dimension TEXT NOT NULL, source_class TEXT NOT NULL, evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_reliability (company_id TEXT REFERENCES companies(id), input_hash TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(company_id, input_hash));
            """)
            self._migrate_conversations(conn)
            self._backfill_salary_numbers(conn)
            # A previous process can no longer complete its in-flight synchronous tasks.
            for row in conn.execute("SELECT id,payload FROM tasks").fetchall():
                task = json.loads(row["payload"])
                if task["status"] == "running":
                    task.update(status="failed", message="上次进程中断，请重新采集")
                    conn.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), row["id"]))

    @staticmethod
    def _salary_numbers(text):
        # 延迟导入：repository 是最底层，analysis 面向模型侧且会继续演化。
        from .analysis import parse_salary
        parsed = parse_salary(text or '')
        return {'salary_monthly_min': parsed['monthly_min'], 'salary_monthly_max': parsed['monthly_max'], 'salary_parsed': True}

    def _backfill_salary_numbers(self, conn):
        """老档案只有薪资原文；薪资区间筛选需要每月数值，启动时补算一次。"""
        rows = conn.execute("SELECT id, payload FROM jobs WHERE json_extract(payload,'$.salary_parsed') IS NULL AND trim(coalesce(json_extract(payload,'$.salary'),'')) != ''").fetchall()
        for row in rows:
            item = json.loads(row['payload'])
            item.update(self._salary_numbers(item.get('salary')))
            conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), row['id']))

    def _migrate_conversations(self, conn):
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(conversations)')}
        for name, definition in (
            ('title', "TEXT NOT NULL DEFAULT ''"),
            ('created_at', "TEXT NOT NULL DEFAULT ''"),
            ('updated_at', "TEXT NOT NULL DEFAULT ''"),
            ('mode', "TEXT NOT NULL DEFAULT 'research'"),
            ('model', "TEXT NOT NULL DEFAULT ''"),
            ('llm_channel', "TEXT NOT NULL DEFAULT ''"),
            # 工具权限档位：confirm=逐次确认 autonomous=只读/白名单自动 full=全部自动；存量会话迁移为自主，兼容 auto_tools 现状。
            ('permission_mode', "TEXT NOT NULL DEFAULT 'autonomous'"),
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
        # WAL 下小事务不再每笔强制 fsync，批量保存岗位从分钟级降到秒级。
        conn.execute("PRAGMA synchronous=NORMAL")
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
            # 与 save_job 同一口径：证据落库即登记其公司主体，资料库随材料生长。
            if str(item["company"]).strip():
                self._upsert_company(conn, item["company"])
            return json.loads(conn.execute("SELECT payload FROM evidence WHERE identity=?", (identity,)).fetchone()["payload"])

    def save_job(self, data, source):
        item = {"title": "", "company": "", "description": "", "location": "", "salary": "", "url": "", "tags": [], **data}
        item.update(self._salary_numbers(item["salary"]))
        key = item.get("external_id") or item.get("url") or json.dumps([item["title"], item["company"], item["location"]], ensure_ascii=False)
        item["id"] = "J-" + digest(source + ":" + str(key))[:24]
        previous = self.job(item["id"])
        if previous and previous.get('description_status') == 'complete' and item.get('description_status') != 'complete':
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
            # 岗位/证据落库时顺带登记公司主体，公司资料库随采集生长。这里登记的是平台或用户
            # 给出的数据本体，不是查询参数，不违反「拼错的查询不留 companies 行」的读路径约定；
            # 也只登记存在、不做变体合并——错合并比漏登记更糟，合并仍由 reliability 流程的
            # credit_code 强键完成。空名不登记：那不是主体，只是缺失字段。
            if str(item["company"]).strip():
                self._upsert_company(conn, item["company"])
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

    def company_evidence(self, name, aliases=(), page=1, page_size=10):
        """实体口径的证据索引：本体与别名都算这家主体，只回元数据不回正文。

        正文一律按 evidence_id 走 get_evidence / project_read 读取，Agent 按需展开时才把全文拉进上下文。
        """
        names = sorted({name, *aliases})
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT payload FROM evidence WHERE company IN ({','.join('?' * len(names))}) ORDER BY rowid DESC",
                tuple(names)).fetchall()
        items = [json.loads(row["payload"]) for row in rows]
        total = len(items)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        keys = ("id", "title", "kind", "source", "url", "collected_at", "published_at")
        return dict(total=total, page=page, pages=pages,
                    evidence=[{key: item.get(key) for key in keys} for item in items[(page - 1) * page_size:page * page_size]])

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

    def task(self, task_id):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

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

    @staticmethod
    def _job_filters(q='', location='', source='', salary_min=None, salary_max=None):
        """薪资区间按重叠判定：只要岗位月薪范围与给定区间有交集就算命中，面议/年薪等无法解析的岗位自然落选。"""
        where, params = [], []
        if q:
            where.append("instr(lower(json_extract(payload,'$.title') || ' ' || json_extract(payload,'$.company') || ' ' || json_extract(payload,'$.description')),lower(?)) > 0")
            params.append(q)
        if location:
            where.append("instr(lower(json_extract(payload,'$.location')),lower(?)) > 0")
            params.append(location)
        if source:
            where.append("json_extract(payload,'$.source')=?")
            params.append(source)
        if salary_min is not None:
            where.append("json_extract(payload,'$.salary_monthly_max')>=?")
            params.append(salary_min)
        if salary_max is not None:
            where.append("json_extract(payload,'$.salary_monthly_min')<=?")
            params.append(salary_max)
        return where, params

    def job_ids(self, q='', location='', source='', salary_min=None, salary_max=None, limit=20000):
        """供“选择筛选结果全部”：同一套筛选条件返回全部命中 id。"""
        where, params = self._job_filters(q, location, source, salary_min, salary_max)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with self.connection() as conn:
            total = conn.execute('SELECT count(*) FROM jobs' + clause, params).fetchone()[0]
            rows = conn.execute('SELECT id FROM jobs' + clause + ' ORDER BY updated_at DESC, id DESC LIMIT ?', [*params, limit]).fetchall()
        return [row['id'] for row in rows], total

    def update_jobs(self, ids, patch):
        """批量规范化字段（地点/薪资/标签）。原位改 payload：id 不重排、不新增证据快照。"""
        fields = {key: patch[key] for key in ('location', 'salary', 'tags') if key in patch}
        if not fields:
            return 0
        updated = 0
        for job_id in ids:
            item = self.job(job_id)
            if item is None:
                continue
            item.update(fields)
            if 'salary' in fields:
                item.update(self._salary_numbers(item['salary']))
            item['evidence_ids'] = []
            with self.connection() as conn:
                conn.execute('UPDATE jobs SET payload=?, updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), now(), job_id))
            updated += 1
        return updated

    def delete_jobs(self, ids):
        """连同岗位的专属证据、报告与岗位对话一并删除；多岗位共享的公司证据保留。"""
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0
        marks = ','.join('?' for _ in ids)
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = [row['id'] for row in conn.execute(f'SELECT id FROM jobs WHERE id IN ({marks})', ids).fetchall()]
            if not existing:
                return 0
            marks = ','.join('?' for _ in existing)
            exclusive = [row['evidence_id'] for row in conn.execute(
                f'SELECT evidence_id FROM job_evidence WHERE job_id IN ({marks}) '
                f'EXCEPT SELECT evidence_id FROM job_evidence WHERE job_id NOT IN ({marks})', existing + existing).fetchall()]
            conn.execute(f'DELETE FROM job_evidence WHERE job_id IN ({marks})', existing)
            conn.execute(f'DELETE FROM reports WHERE job_id IN ({marks})', existing)
            if exclusive:
                marks_e = ','.join('?' for _ in exclusive)
                conn.execute(f'DELETE FROM evidence WHERE id IN ({marks_e})', exclusive)
            for row in conn.execute('SELECT id, scope FROM conversations').fetchall():
                try:
                    scope = json.loads(row['scope'])
                except ValueError:
                    continue
                if isinstance(scope, dict) and scope.get('kind') == 'job' and scope.get('job_id') in existing:
                    conn.execute('DELETE FROM conversations WHERE id=?', (row['id'],))
            cursor = conn.execute(f'DELETE FROM jobs WHERE id IN ({marks})', existing)
            return cursor.rowcount

    def page(self, resource, page=1, page_size=20, q='', location='', source='', salary_min=None, salary_max=None,
             company=None, job_id=None, linked_job_id=None, include_job_postings=True):
        """Bounded SQL queries; filters run before count and pagination."""
        where, params = [], []
        if resource == 'jobs':
            table, order = 'jobs', 'updated_at DESC, id DESC'
            where, params = self._job_filters(q, location, source, salary_min, salary_max)
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

    def locations(self, limit=40):
        """按已保存岗位的地点原样统计，供地点框提示；Remotive 与手动导入存的是原文，不并入城市字典。"""
        with self.connection() as conn:
            rows = conn.execute("""SELECT json_extract(payload,'$.location') AS location, count(*) AS jobs FROM jobs
                                   GROUP BY location
                                   HAVING location NOT NULL AND location != '' AND length(location) <= 40
                                   ORDER BY jobs DESC, location LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def conversation(self, conversation_id, scope):
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            if conversation_id:
                row = conn.execute('SELECT scope,messages FROM conversations WHERE id=?', (conversation_id,)).fetchone()
                if row is None or row['scope'] != encoded:
                    raise ValueError('会话范围不匹配，请为当前范围新建对话')
                return conversation_id, json.loads(row['messages'])
            conversation_id = 'C-' + uuid.uuid4().hex
            conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)', (conversation_id, encoded, '[]', '', now(), now(), 'research', '', '', 'autonomous'))
        return conversation_id, []

    def create_conversation(self, scope, mode='research', model='', llm_channel='', permission_mode='autonomous'):
        conversation_id = 'C-' + uuid.uuid4().hex
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connection() as conn:
            conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)', (conversation_id, encoded, '[]', '', now(), now(), mode, model, llm_channel, permission_mode))
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
            rows = conn.execute('SELECT id,title,mode,model,llm_channel,permission_mode,updated_at,messages FROM conversations WHERE scope=? ORDER BY updated_at DESC LIMIT ?', (encoded, limit)).fetchall()
        result = []
        for row in rows:
            messages = json.loads(row['messages'])
            preview = next((m['content'][:80] for m in messages if m.get('role') == 'user'), '')
            result.append(dict(id=row['id'], title=row['title'] or preview or '新对话', mode=row['mode'] or 'research', model=row['model'], llm_channel=row['llm_channel'] or '', permission_mode=row['permission_mode'] or 'autonomous', updated_at=row['updated_at'], message_count=len(messages), preview=preview))
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
        return dict(id=row['id'], scope=json.loads(row['scope']), title=row['title'] or '新对话', mode=row['mode'] or 'research', model=row['model'], llm_channel=row['llm_channel'] or '', permission_mode=row['permission_mode'] or 'autonomous', created_at=row['created_at'], updated_at=row['updated_at'], messages=messages)

    def _conversation_row(self, conn, conversation_id, scope=None):
        row = conn.execute('SELECT scope,title,messages FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None or (scope is not None and row['scope'] != json.dumps(scope, sort_keys=True, ensure_ascii=False)):
            raise ValueError('会话不存在或范围不匹配')
        return row

    def update_conversation(self, conversation_id, scope=None, *, title=None, mode=None, model=None, llm_channel=None, permission_mode=None):
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
        if llm_channel is not None:
            fields.append('llm_channel=?')
            values.append(llm_channel[:100])
        if permission_mode is not None:
            fields.append('permission_mode=?')
            values.append(permission_mode)
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

    def compress_conversation(self, conversation_id, boundary, summary, expected_ids=None):
        """Fold messages [0, boundary) behind a summary checkpoint inserted at boundary; nothing is deleted.

        Older messages keep a `compacted` flag so history builders can skip them while the
        UI still shows the originals; the checkpoint message itself stays in the model view.
        expected_ids makes the write compare-and-swap: if the conversation changed while the
        summary was being generated (a new message, an edit, a delete), nothing is written.
        """
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._conversation_row(conn, conversation_id)
            messages = json.loads(row['messages'])
            if expected_ids is not None and [item.get('id') for item in messages] != list(expected_ids):
                raise ValueError('会话在压缩期间发生了变化，请重试')
            if not 0 < boundary <= len(messages):
                raise ValueError('压缩边界无效')
            folded = 0
            for item in messages[:boundary]:
                if not item.get('compacted'):
                    item['compacted'] = True
                    folded += 1
            stamped = self._stamp(dict(role='assistant', kind='compaction', content=summary, compacted_count=folded))
            messages = messages[:boundary] + [stamped] + messages[boundary:]
            conn.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), now(), conversation_id))
        return dict(compacted_count=folded, summary_message_id=stamped['id'], message_count=len(messages))

    @staticmethod
    def normalise_name(name):
        text = str(name or '').strip().replace('\u3000', ' ')
        text = unicodedata.normalize('NFKC', text)
        # 空白一律删除而非压成空格：'A B' 与 'AB' 是同一家，留下空格就把同一个名字拆成两个实体。
        return re.sub(r'\s+', '', text)

    def find_company(self, name, credit_code=''):
        """Read-only twin of `resolve_company`: same normalisation, same strong key, no row written.

        读路径写实体等于让一次拼错的查询在库里留下永久记录，所以这里只 SELECT：找不到就返回 None。
        身份键的算法必须和 `resolve_company` 保持一致，否则同一个名字在读写两侧会指向两个实体。
        """
        cleaned = self.normalise_name(name)
        if not cleaned:
            raise ValueError('公司名称不能为空')
        code = str(credit_code or '').strip().upper()
        company_id = 'CO-' + digest(code or cleaned)[:24]
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM companies WHERE id=?', (company_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def _upsert_company(self, conn, name, credit_code='', aliases=()):
        """resolve_company 的身份与合并逻辑，跑在调用方已打开的连接里（落库事务内登记主体时用）。"""
        cleaned = self.normalise_name(name)
        if not cleaned:
            raise ValueError('公司名称不能为空')
        code = str(credit_code or '').strip().upper()
        identity = code or cleaned
        company_id = 'CO-' + digest(identity)[:24]
        row = conn.execute('SELECT payload FROM companies WHERE id=?', (company_id,)).fetchone()
        known = json.loads(row['payload']) if row else dict(id=company_id, name=cleaned, credit_code=code, aliases=[], created_at=now())
        merged = {self.normalise_name(a) for a in [*known['aliases'], *aliases, cleaned] if str(a).strip()}
        merged.discard(known['name'])
        known['aliases'] = sorted(merged)
        known['updated_at'] = now()
        conn.execute('INSERT INTO companies VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,credit_code=excluded.credit_code,aliases=excluded.aliases,payload=excluded.payload,updated_at=excluded.updated_at',
                     (company_id, known['name'], known['credit_code'], json.dumps(known['aliases'], ensure_ascii=False), json.dumps(known, ensure_ascii=False), known['updated_at']))
        return known

    def resolve_company(self, name, credit_code='', aliases=()):
        """Exact normalised match or credit code only; a wrong merge is worse than a miss."""
        with self.connection() as conn:
            return self._upsert_company(conn, name, credit_code, aliases)

    def save_signal(self, signal):
        item = signal.model_dump()
        identity = digest(json.dumps([item['company_id'], item['rule'], item['evidence_id'],
                                      item['observed_at'], json.dumps(item['value'], sort_keys=True, ensure_ascii=False)], ensure_ascii=False))
        item.update(id='S-' + identity[:24], created_at=now())
        with self.connection() as conn:
            conn.execute('INSERT OR IGNORE INTO company_signals VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (item['id'], item['company_id'], identity, item['rule'], item['dimension'],
                          item['source_class'], item['evidence_id'], item['observed_at'],
                          json.dumps(item, ensure_ascii=False), item['created_at']))
        return item

    def signals(self, company_id, ttl_days=None):
        """Latest collection per rule; TTL marks staleness instead of silently dropping data."""
        with self.connection() as conn:
            # collected_at 只存在 payload 里（建表脚本无此列），照 page() 的做法用 json_extract 排序。
            rows = conn.execute("SELECT payload FROM company_signals WHERE company_id=? ORDER BY json_extract(payload,'$.collected_at') DESC, rowid DESC", (company_id,)).fetchall()
        seen, result = set(), []
        for row in rows:
            item = json.loads(row['payload'])
            if item['rule'] in seen:
                continue
            seen.add(item['rule'])
            limit = (ttl_days or {}).get(item['rule'])
            stale = True
            if limit:
                try:
                    # 计划原文用 .days 取整，400 天 1 秒会被截成 400，边界上恰好漏报过期；改成整段时长比较（也是 timedelta 这个导入的用途）。
                    stale = (datetime.now(timezone.utc) - datetime.fromisoformat(item['collected_at'])) > timedelta(days=limit)
                except ValueError:
                    stale = True
            item['stale'] = stale
            result.append(item)
        return result

    def raw_signals(self, company_id):
        # 排序要全序：同一 rule 可以有多条来自不同出处的信号，只按 rule 排时并列行的先后取决于查询计划，
        # 那会让同一批输入算出不同的 input_hash。
        with self.connection() as conn:
            return [dict(row) for row in conn.execute('SELECT id,payload FROM company_signals WHERE company_id=? ORDER BY rule,id', (company_id,)).fetchall()]

    def save_reliability(self, report):
        with self.connection() as conn:
            conn.execute('INSERT OR IGNORE INTO company_reliability VALUES (?,?,?,?)',
                         (report['company_id'], report['input_hash'], report['computed_at'], json.dumps(report, ensure_ascii=False)))

    def latest_reliability(self, company_id):
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM company_reliability WHERE company_id=? ORDER BY created_at DESC LIMIT 1', (company_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def reliability_history(self, company_id):
        with self.connection() as conn:
            return [json.loads(row['payload']) for row in conn.execute('SELECT payload FROM company_reliability WHERE company_id=? ORDER BY created_at', (company_id,)).fetchall()]

    def compare_page(self, q='', page_size=50):
        """横评选择器的候选列表：实体表全量（含还没算过参考分的），LEFT JOIN 各自最新一份报告概要。

        分数与覆盖度照实带出 null——没算过就是没算过，绝不补 0 伪装成低分；按名称排序，
        这个列表是选择器不是榜单，名次语义留给页面上的图表与用户自己。
        jobs_count 是该实体口径（名称+别名规范化归并）下的已存岗位数：横评的主要数据面就是
        岗位，检索时看得见哪家有料、哪家只是登记过主体。
        """
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT c.name AS company, c.credit_code AS credit_code, c.aliases AS aliases,
                       json_extract(r.payload, '$.score') AS score,
                       json_extract(r.payload, '$.confidence') AS confidence,
                       r.created_at AS computed_at
                FROM companies c
                LEFT JOIN (SELECT company_id, payload, created_at, rowid,
                                  ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY created_at DESC, rowid DESC) AS rn
                           FROM company_reliability) r ON r.company_id = c.id AND r.rn = 1
                WHERE instr(lower(c.name), lower(?)) > 0
                ORDER BY c.name LIMIT ?""", (q, page_size)).fetchall()
        counts = {}
        for item in self.jobs():
            name = Repository.normalise_name(item.get('company'))
            if name:
                counts[name] = counts.get(name, 0) + 1
        result = []
        for row in rows:
            item = dict(row)
            try:
                aliases = json.loads(item.pop('aliases') or '[]')
            except ValueError:
                aliases = []
            names = {Repository.normalise_name(value) for value in [item['company'], *aliases] if str(value or '').strip()}
            item['jobs_count'] = sum(counts.get(name, 0) for name in names)
            result.append(item)
        return dict(companies=result)
