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
                CREATE TABLE IF NOT EXISTS company_merge_proposals (id TEXT PRIMARY KEY, company_a TEXT NOT NULL, company_b TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS resume_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, job_id TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', job_title TEXT NOT NULL DEFAULT '', jd_digest TEXT NOT NULL DEFAULT '', base_text TEXT NOT NULL DEFAULT '', markdown TEXT NOT NULL, template_id TEXT NOT NULL DEFAULT 'classic', notes TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wage_baselines (id TEXT PRIMARY KEY, city TEXT NOT NULL, role TEXT NOT NULL, education TEXT NOT NULL, year TEXT NOT NULL, monthly_median REAL NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_entries (id TEXT PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, org TEXT NOT NULL DEFAULT '', period TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requirement_overrides (job_id TEXT NOT NULL REFERENCES jobs(id), quote_digest TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(job_id, quote_digest));
                CREATE TABLE IF NOT EXISTS interview_sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS interview_transcripts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            self._migrate_conversations(conn)
            # profile_entries.source 记录条目来源（笔记库导入的「规范化库根::相对路径」锚点），幂等重导靠它原位更新。
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(profile_entries)')}
            if 'source' not in columns:
                conn.execute("ALTER TABLE profile_entries ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            # resume_versions.pinned：岗位绑定「评估用」简历（同岗位互斥），评估适配自动取用。
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(resume_versions)')}
            if 'pinned' not in columns:
                conn.execute('ALTER TABLE resume_versions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0')
            # resume_versions.typography：排版设置 JSON（字体/字号/行距/页边距/一页纸），随版本保存。
            if 'typography' not in columns:
                conn.execute("ALTER TABLE resume_versions ADD COLUMN typography TEXT NOT NULL DEFAULT '{}'")
            # job_preferences 并入 profile_entries（2026-11-18）：偏好申报本就是个人资料的一种立场
            # 条目，category='preference' 承载；原 dimension 降为标签（模型注入只取 statement，无损），
            # title 用维度中文名，id 原样保留（已存报告的 fact_ids 引用不失效）。迁移后旧表退役删除。
            legacy_tables = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'job_preferences' in legacy_tables:
                dimension_names = {'preference': '求职偏好', 'skill': '技能',
                                   'experience': '经历与职责', 'industry': '行业与业务'}
                rows = conn.execute('SELECT id, dimension, statement, enabled, created_at, updated_at FROM job_preferences').fetchall()
                for row in rows:
                    # 列名显式：profile_entries 表尾追加过 source 列，物理列序非建表顺序，位置插值会错位。
                    conn.execute('INSERT INTO profile_entries (id,category,title,org,period,content,tags,enabled,source,created_at,updated_at) '
                                 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                 (row['id'], 'preference', dimension_names.get(row['dimension'], row['dimension']), '', '',
                                  row['statement'], json.dumps([row['dimension']], ensure_ascii=False),
                                  int(row['enabled']), '', row['created_at'], row['updated_at']))
                conn.execute('DROP TABLE job_preferences')
            # 读路径高频查询的二级索引（放在 _migrate_conversations 之后：conversations 的
            # updated_at 等列由迁移补齐）。列表排序、公司过滤、取最新报告与最新会话不再随数据量
            # 增长退化为全表扫描；首次对既有库建索引是一次性开销。
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_evidence_company ON evidence(company);
                CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
                CREATE INDEX IF NOT EXISTS idx_company_reliability_lookup ON company_reliability(company_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_company_signals_company ON company_signals(company_id);
                CREATE INDEX IF NOT EXISTS idx_conversations_scope ON conversations(scope, updated_at);
            """)
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
        with self.connection() as conn:
            return self._save_evidence(conn, data, source)

    def _save_evidence(self, conn, data, source="user_import"):
        item = {"title": "资料", "url": "", "company": "", "kind": "user_material", "text": "", "published_at": None, **data}
        item.pop("id", None)
        item["source"] = source
        item["content_hash"] = digest(item["text"])
        identity = digest(json.dumps([source, item["company"], item["url"], item["kind"], item["title"], item["published_at"], item["content_hash"]], ensure_ascii=False))
        # Manual provenance is part of identity: equal text from different jobs or notes
        # must not silently reuse another supplement's attribution.
        if item.get('original_job_id') or item.get('source_note'):
            identity = digest(json.dumps([identity, item.get('original_job_id'), item.get('source_note')], ensure_ascii=False))
        item.update(id="E-" + identity[:24], collected_at=now())
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
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM jobs WHERE id=?', (item['id'],)).fetchone()
            previous = json.loads(row['payload']) if row else None
            if previous and previous.get('description_status') == 'complete' and item.get('description_status') != 'complete':
                for field in ('description', 'description_status', 'detail_collected_at', 'detail_source_url', 'detail_transport'):
                    if field in previous:
                        item[field] = previous[field]
            # 收藏标记是用户意图不是采集数据：重采集整包覆盖 payload 时必须幸存，
            # 否则一次日常刷新就把用户标过的岗位全部静默取消收藏。
            # 机会管线状态与跟进信息（2026-11-17/18）、去重标记（2026-11-19）同为用户意图，随收藏一起幸存。
            if previous:
                for field in ('starred', 'starred_at', 'pipeline_status', 'pipeline_status_changed_at',
                              'pipeline_note', 'pipeline_due', 'duplicate_of'):
                    if field in previous:
                        item[field] = previous[field]
            item['platform_tags'] = list(dict.fromkeys(data.get('platform_tags', data.get('tags', []))))
            item['personal_tags'] = list((previous or {}).get('personal_tags', []))
            if previous and 'personal_tags' not in previous:
                item['personal_tags'] = list(previous.get('tags', []))
            item['tags'] = list(dict.fromkeys(item['platform_tags'] + item['personal_tags']))
            if previous and previous.get('description_source') == 'user':
                for field in ('description', 'description_status', 'description_source', 'description_source_note', 'manual_description_at', 'manual_description_evidence_id'):
                    if field in previous:
                        item[field] = previous[field]
            item.update(source=source, updated_at=now(), evidence_ids=[])
            # Every job revision retains a complete attributable snapshot.
            body = "\n".join([item["title"], item["company"], item["location"], item["salary"], (data.get("description", "") if item.get("description_source") == "user" else item["description"])])
            evidence = self._save_evidence(conn, dict(title=item["title"], company=item["company"], url=item["url"], kind="job_posting", text=body, published_at=item.get("published_at")), source)
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

    def jobs(self, q="", location="", include_duplicates=False):
        # 全量读回是 compare/details-status/reliability 等重端点的底座：逐条 self.job() 是
        # 每行一次新连接的 N+1，几千份档案就是几千次连接。这里单连接两次查询等价替换。
        # 去重标记的重复项默认不进统计与回顾（主记录已代表）；需要看全量时显式传 include_duplicates。
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM jobs ORDER BY updated_at DESC").fetchall()
            linked = {}
            for job_id, evidence_id in conn.execute("SELECT job_id, evidence_id FROM job_evidence ORDER BY rowid"):
                linked.setdefault(job_id, []).append(evidence_id)
            items = []
            for row in rows:
                item = json.loads(row["payload"])
                if not include_duplicates and item.get("duplicate_of"):
                    continue
                item["evidence_ids"] = linked.get(item["id"], [])
                items.append(item)
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

    # ---- 简历工坊：每次针对岗位的定制结果保存为一个可二次编辑的投递版本 ----

    _VERSION_COLUMNS = "id,title,job_id,company,job_title,jd_digest,base_text,markdown,template_id,notes,created_at,updated_at,pinned,typography"

    def list_resume_versions(self, job_id=None):
        """存档摘要（新→旧）；job_id 给定时只看绑定该岗位的版本。"""
        with self.connection() as conn:
            sql = "SELECT id,title,job_id,company,job_title,template_id,pinned,created_at,updated_at,length(markdown) AS characters FROM resume_versions"
            params = ()
            if job_id:
                sql += " WHERE job_id=?"
                params = (job_id,)
            rows = conn.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
            return [dict(row, pinned=bool(row['pinned'])) for row in rows]

    def set_pinned_resume_version(self, version_id, pinned):
        """岗位级「评估用」标记：pin 时清除同岗位其他 pin（互斥），返回更新后整行；版本不存在返回 None。"""
        version = self.resume_version(version_id)
        if version is None:
            return None
        with self.connection() as conn:
            if pinned:
                conn.execute('UPDATE resume_versions SET pinned=0 WHERE job_id=? AND id!=?', (version['job_id'], version_id))
            conn.execute('UPDATE resume_versions SET pinned=? WHERE id=?', (int(pinned), version_id))
        return self.resume_version(version_id)

    def pinned_resume_version(self, job_id):
        """该岗位当前「评估用」的存档版本；未 pin 返回 None（调用方回退全局我的简历）。"""
        with self.connection() as conn:
            row = conn.execute("SELECT id,title,markdown,template_id,updated_at FROM resume_versions WHERE job_id=? AND pinned=1 ORDER BY updated_at DESC LIMIT 1", (job_id,)).fetchone()
        return dict(row) if row else None

    def resume_version(self, version_id):
        with self.connection() as conn:
            row = conn.execute(f"SELECT {self._VERSION_COLUMNS} FROM resume_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            return None
        item = dict(row, pinned=bool(row['pinned']))
        try:
            item["notes"] = json.loads(item.get("notes") or "[]")
        except ValueError:
            item["notes"] = []
        try:
            item["typography"] = json.loads(item.get("typography") or "{}")
        except ValueError:
            item["typography"] = {}
        return item

    def save_resume_version(self, data):
        """带 id 更新既有版本，否则新建；更新时保留创建时间与 base_text 原快照。"""
        existing = self.resume_version(data["id"]) if data.get("id") else None
        timestamp = now()
        if existing:
            fields = {key: data[key] for key in ("title", "job_id", "company", "job_title", "jd_digest", "markdown", "template_id", "typography") if key in data}
            fields["notes"] = json.dumps(data.get("notes", existing["notes"]), ensure_ascii=False)
            if "typography" in fields:
                fields["typography"] = json.dumps(fields["typography"] if fields["typography"] is not None else {}, ensure_ascii=False)
            fields["updated_at"] = timestamp
            with self.connection() as conn:
                conn.execute(f"UPDATE resume_versions SET {','.join(key + '=?' for key in fields)} WHERE id=?", (*fields.values(), data["id"]))
            return self.resume_version(data["id"])
        item = dict(job_id="", company="", job_title="", jd_digest="", base_text="", notes=[], template_id="classic", typography={})
        item.update(data)
        item["notes"] = json.dumps(item["notes"], ensure_ascii=False)
        item["typography"] = json.dumps(item.get("typography") or {}, ensure_ascii=False)
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO resume_versions (title,job_id,company,job_title,jd_digest,base_text,markdown,template_id,notes,created_at,updated_at,typography) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["title"], item["job_id"], item["company"], item["job_title"], item["jd_digest"], item["base_text"], item["markdown"], item["template_id"], item["notes"], timestamp, timestamp, item["typography"]),
            )
            version_id = cursor.lastrowid
        return self.resume_version(version_id)

    def delete_resume_version(self, version_id):
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM resume_versions WHERE id=?", (version_id,))
            return cursor.rowcount > 0

    # ── 个人资料库（2026-11-17）：完整版个人材料，与「我的简历」分离 ──
    # 简历因匹配度与篇幅常是阉割版；这里存完整的项目经历等工作材料，简历工坊优化时按需取材。
    # 条目是用户手写的第一手事实，不属于任何外部来源体系。

    @staticmethod
    def _profile_row(row):
        item = dict(row)
        try:
            item['tags'] = json.loads(item.get('tags') or '[]')
        except ValueError:
            item['tags'] = []
        item['enabled'] = bool(item.get('enabled'))
        return item

    def profile_entry(self, entry_id):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM profile_entries WHERE id=?', (entry_id,)).fetchone()
        return None if row is None else self._profile_row(row)

    def profile_entries(self, enabled_only=False):
        """全量条目：按类别名内分组顺序（category, created_at）排列，前端按类别分组展示。"""
        clause = ' WHERE enabled=1' if enabled_only else ''
        with self.connection() as conn:
            rows = conn.execute(f'SELECT * FROM profile_entries{clause} ORDER BY category, created_at').fetchall()
        return [self._profile_row(row) for row in rows]

    def profile_entry_by_source(self, source):
        """按来源锚点取条目（笔记库 Agent 导入的幂等锚点）：没有就是 None。"""
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM profile_entries WHERE source=?', (source,)).fetchone()
        return None if row is None else self._profile_row(row)

    def save_profile_entry(self, data):
        """带 id 更新既有条目，否则新建；更新时保留创建时间，enabled 恒为 0/1 整数。"""
        timestamp = now()
        item = {**dict(category='project', title='', org='', period='', content='', tags=[], enabled=True, source=''), **data}
        if item.get('tags') is None:
            item['tags'] = []  # PATCH 未携带 tags 时保持空集，不让 None 进 json.dumps
        item['tags'] = json.dumps(list(dict.fromkeys(item['tags'])), ensure_ascii=False)
        item['enabled'] = 1 if item['enabled'] else 0
        with self.connection() as conn:
            if item.get('id') and self.profile_entry(item['id']):
                fields = {key: item[key] for key in ('category', 'title', 'org', 'period', 'content', 'tags', 'enabled', 'source')}
                fields['updated_at'] = timestamp
                conn.execute(f'UPDATE profile_entries SET {",".join(key + "=?" for key in fields)} WHERE id=?',
                             (*fields.values(), item['id']))
            else:
                item['id'] = 'P-' + uuid.uuid4().hex[:20]
                item['created_at'] = timestamp
                # 列名显式：迁移给表尾追加过 source 列，物理列序不再是建表顺序，位置插值会错位。
                conn.execute('INSERT INTO profile_entries (id,category,title,org,period,content,tags,enabled,source,created_at,updated_at) '
                             'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                             (item['id'], item['category'], item['title'], item['org'], item['period'],
                              item['content'], item['tags'], item['enabled'], item.get('source', ''),
                              item['created_at'], timestamp))
        return self.profile_entry(item['id'])

    def patch_profile_entry(self, entry_id, patch):
        """部分更新（启停开关等）：在已存条目上合并，缺省字段保持原值；条目不存在返回 None。"""
        existing = self.profile_entry(entry_id)
        if existing is None:
            return None
        base = {key: existing[key] for key in ('category', 'title', 'org', 'period', 'content', 'tags', 'enabled')}
        return self.save_profile_entry({**base, **patch, 'id': entry_id})

    def delete_profile_entry(self, entry_id):
        with self.connection() as conn:
            return conn.execute('DELETE FROM profile_entries WHERE id=?', (entry_id,)).rowcount > 0

    # ---- 模拟面试：场次 = 画像/材料/题单/进度的整体快照（payload JSON，jobs 同款模式） ----

    def save_interview_session(self, session):
        session = {**session, 'updated_at': now()}
        with self.connection() as conn:
            conn.execute('INSERT INTO interview_sessions (id,payload,updated_at) VALUES (?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at',
                         (session['id'], json.dumps(session, ensure_ascii=False), session['updated_at']))
        return session

    def interview_sessions(self, limit=100):
        """场次摘要（新→旧）：列表页只要标题与状态，不解析完整 payload 的材料与题单。"""
        with self.connection() as conn:
            rows = conn.execute('SELECT id, payload FROM interview_sessions ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row['payload'])
            persona = payload.get('persona') or {}
            result.append(dict(id=row['id'], title=payload.get('title', '模拟面试'), company=payload.get('company', ''),
                               job_id=payload.get('job_id', ''), status=payload.get('status', 'planning'),
                               persona_name=persona.get('name', ''), round_label=persona.get('round_label', ''),
                               has_report=bool(payload.get('report')), created_at=payload.get('created_at', ''),
                               updated_at=payload.get('updated_at', '')))
        return result

    def get_interview_session(self, session_id):
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM interview_sessions WHERE id=?', (session_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def delete_interview_session(self, session_id):
        with self.connection() as conn:
            return conn.execute('DELETE FROM interview_sessions WHERE id=?', (session_id,)).rowcount > 0

    # ---- 真实面试逐字稿：导入 → 标注 → 复盘（payload JSON 快照，interview_sessions 同款模式） ----

    def save_interview_transcript(self, transcript):
        transcript = {**transcript, 'updated_at': now()}
        with self.connection() as conn:
            conn.execute('INSERT INTO interview_transcripts (id,payload,updated_at) VALUES (?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at',
                         (transcript['id'], json.dumps(transcript, ensure_ascii=False), transcript['updated_at']))
        return transcript

    def interview_transcripts(self, limit=100):
        """逐字稿摘要（新→旧）：列表页只要岗位锚点与状态，不解析完整 payload 的段落与报告。"""
        with self.connection() as conn:
            rows = conn.execute('SELECT id, payload FROM interview_transcripts ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row['payload'])
            result.append(dict(id=row['id'], job_id=payload.get('job_id', ''), job_title=payload.get('job_title', ''),
                               company=payload.get('company', ''), status=payload.get('status', 'imported'),
                               segment_count=len(payload.get('segments') or []), has_report=bool(payload.get('report')),
                               created_at=payload.get('created_at', ''), updated_at=payload.get('updated_at', '')))
        return result

    def get_interview_transcript(self, transcript_id):
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM interview_transcripts WHERE id=?', (transcript_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def delete_interview_transcript(self, transcript_id):
        with self.connection() as conn:
            return conn.execute('DELETE FROM interview_transcripts WHERE id=?', (transcript_id,)).rowcount > 0

    def save_report(self, report):
        with self.connection() as conn:
            conn.execute("INSERT INTO reports VALUES (?,?,?,?)", (str(uuid.uuid4()), report["job_id"], now(), json.dumps(report, ensure_ascii=False)))

    def list_reports(self, job_id):
        """评估报告摘要（新→旧，created_at 同秒按写入序）+ 最近一份完整报告；查无则 ([], None)。"""
        with self.connection() as conn:
            rows = conn.execute('SELECT id, created_at, payload FROM reports WHERE job_id = ? ORDER BY created_at DESC, rowid DESC', (job_id,)).fetchall()
        summaries = []
        for row in rows:
            payload = json.loads(row['payload'])
            summaries.append(dict(id=row['id'], created_at=row['created_at'], method=payload.get('method'), score=payload.get('score')))
        return summaries, (json.loads(rows[0]['payload']) if rows else None)

    def get_report(self, job_id, report_id):
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM reports WHERE job_id = ? AND id = ?', (job_id, report_id)).fetchone()
        return json.loads(row['payload']) if row else None

    def update_report(self, report_id, payload):
        with self.connection() as conn:
            conn.execute('UPDATE reports SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), report_id))

    # ---- 求职偏好：category='preference' 的资料条目，启用即注入评估管线作已确认事实 ----

    def preference_statements(self):
        """启用的求职偏好条目 → 评估管线的已确认事实（id+statement 口径，与简历解析事实同形）。"""
        return [{'id': item['id'], 'statement': item['content']}
                for item in self.profile_entries(enabled_only=True) if item['category'] == 'preference']

    # ---- 需求状态手动改判：按岗位 + jd_quote 摘要记忆，重评自动沿用 ----

    def save_override(self, job_id, quote_digest, status, note=''):
        with self.connection() as conn:
            conn.execute('INSERT INTO requirement_overrides (job_id, quote_digest, status, note, updated_at) VALUES (?,?,?,?,?) '
                         'ON CONFLICT(job_id, quote_digest) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at',
                         (job_id, quote_digest, status, note, now()))

    def list_overrides(self, job_id):
        with self.connection() as conn:
            rows = conn.execute('SELECT quote_digest, status, note FROM requirement_overrides WHERE job_id=?', (job_id,)).fetchall()
        return {row['quote_digest']: dict(status=row['status'], note=row['note']) for row in rows}

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
    def _job_filters(q='', location='', source='', salary_min=None, salary_max=None, starred=False, pipeline_status=''):
        """薪资区间按重叠判定：只要岗位月薪范围与给定区间有交集就算命中，面议/年薪等无法解析的岗位自然落选。

        去重标记（2026-11-19）的重复项一律不进列表：它们是同一岗位的次要记录，主记录已代表；
        详情按 id 直读仍可见（那里能看到「与 X 视为同一岗位」并拆开）。"""
        where, params = ["json_extract(payload,'$.duplicate_of') IS NULL"], []
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
        if starred:
            # JSON true 经 json_extract 折算成 1：只认显式收藏，旧行（字段缺失=NULL）与已取消（false=0）都不算。
            where.append("json_extract(payload,'$.starred')=1")
        # 机会管线（2026-11-17）：'tracked' 是全部在管岗位，其余按状态精确匹配；空串不过滤。
        if pipeline_status == 'tracked':
            where.append("json_extract(payload,'$.pipeline_status') IS NOT NULL")
        elif pipeline_status:
            where.append("json_extract(payload,'$.pipeline_status')=?")
            params.append(pipeline_status)
        return where, params

    def job_ids(self, q='', location='', source='', salary_min=None, salary_max=None, starred=False, limit=20000, pipeline_status=''):
        """供“选择筛选结果全部”：同一套筛选条件返回全部命中 id。"""
        where, params = self._job_filters(q, location, source, salary_min, salary_max, starred, pipeline_status)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with self.connection() as conn:
            total = conn.execute('SELECT count(*) FROM jobs' + clause, params).fetchone()[0]
            rows = conn.execute('SELECT id FROM jobs' + clause + ' ORDER BY updated_at DESC, id DESC LIMIT ?', [*params, limit]).fetchall()
        return [row['id'] for row in rows], total

    def update_jobs(self, ids, patch):
        """批量规范化字段（地点/薪资/标签/收藏/管线状态与跟进信息）。原位改 payload：id 不重排、不新增证据快照。

        管线状态（2026-11-17）与收藏共用一个篮子：给未收藏岗位设状态会自动收藏（进入管理视图）；
        取消收藏时管线状态一并清掉（离开篮子即离开管线）。'' 表示显式移出管线。
        跟进信息（2026-11-18）：note/due 独立于状态可改（清空状态/取消收藏时连带清掉），
        '' 显式清空、缺省不动。"""
        fields = {key: patch[key] for key in ('location', 'salary', 'tags', 'starred', 'pipeline_status',
                                              'pipeline_note', 'pipeline_due') if key in patch}
        if not fields:
            return 0
        updated = 0
        for job_id in dict.fromkeys(ids):
            with self.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
                item = json.loads(row['payload']) if row else None
                if item is None:
                    continue
                if 'tags' in fields:
                    item.setdefault('platform_tags', [])
                    item['personal_tags'] = list(dict.fromkeys(fields['tags']))
                item.update({key: value for key, value in fields.items()
                             if key not in ('tags', 'pipeline_note', 'pipeline_due')})
                if 'tags' in fields:
                    item['tags'] = list(dict.fromkeys(item['platform_tags'] + item['personal_tags']))
                # 跟进信息：空串显式清空（键整个移除），缺省不动。
                for key in ('pipeline_note', 'pipeline_due'):
                    if key in fields:
                        if fields[key]:
                            item[key] = fields[key]
                        else:
                            item.pop(key, None)
                if fields.get('starred'):
                    item['starred_at'] = now()
                elif 'starred' in fields:
                    item.pop('starred_at', None)
                    # 取消收藏即移出管线：状态、变化时间与跟进信息跟着清掉，避免留下不可见的幽灵跟踪。
                    for key in ('pipeline_status', 'pipeline_status_changed_at', 'pipeline_note', 'pipeline_due'):
                        item.pop(key, None)
                if fields.get('pipeline_status') == '':
                    for key in ('pipeline_status', 'pipeline_status_changed_at', 'pipeline_note', 'pipeline_due'):
                        item.pop(key, None)
                elif fields.get('pipeline_status'):
                    item['pipeline_status'] = fields['pipeline_status']
                    item['pipeline_status_changed_at'] = now()
                    if not item.get('starred'):
                        item['starred'] = True
                        item['starred_at'] = now()
                if 'salary' in fields:
                    item.update(self._salary_numbers(item['salary']))
                item['evidence_ids'] = []
                item['updated_at'] = now()
                conn.execute('UPDATE jobs SET payload=?, updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item['updated_at'], job_id))
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
            # 主记录被删时其重复项自动晋升：清掉指向已删 id 的 duplicate_of，不让标记悬空。
            dup_rows = conn.execute(f"SELECT id, payload FROM jobs WHERE json_extract(payload,'$.duplicate_of') IN ({marks})", existing).fetchall()
            for row in dup_rows:
                item = json.loads(row['payload'])
                item.pop('duplicate_of', None)
                conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), row['id']))
            cursor = conn.execute(f'DELETE FROM jobs WHERE id IN ({marks})', existing)
            return cursor.rowcount

    @staticmethod
    def _attach_evidence_ids(conn, items):
        """分页后的岗位批量补 evidence_ids（保持 job_evidence 的 rowid 顺序），替代逐条回读。"""
        ids = [item['id'] for item in items]
        if not ids:
            return items
        marks = ','.join('?' * len(ids))
        linked = {}
        for job_id, evidence_id in conn.execute(f'SELECT job_id, evidence_id FROM job_evidence WHERE job_id IN ({marks}) ORDER BY rowid', ids):
            linked.setdefault(job_id, []).append(evidence_id)
        for item in items:
            item['evidence_ids'] = linked.get(item['id'], [])
        return items

    @staticmethod
    def _trim_evidence_text(items, q, trim_text):
        """列表端点不回传全文正文：text 截断并打标，摘要命中窗口（text_hit）在截断前基于全文算出，
        前端 EvidenceCard 用它渲染搜索摘要，展开时再按 id 取全文。"""
        terms = [term.lower() for term in q.split() if term]
        for item in items:
            text = str(item.get('text') or '')
            if terms:
                lowered = text.lower()
                hit_at = min((pos for pos in (lowered.find(term) for term in terms) if pos >= 0), default=-1)
                if hit_at >= 0:
                    start = max(0, hit_at - 60)
                    end = min(len(text), hit_at + 200)
                    item['text_hit'] = re.sub(r'\s+', ' ', f"{'…' if start > 0 else ''}{text[start:end]}{'…' if end < len(text) else ''}")
            if len(text) > trim_text:
                item['text'] = text[:trim_text]
                item['text_truncated'] = True
        return items

    def page(self, resource, page=1, page_size=20, q='', location='', source='', salary_min=None, salary_max=None,
             company=None, job_id=None, linked_job_id=None, include_job_postings=True,
             trim_description=None, trim_text=None, starred=False, pipeline_status=''):
        """Bounded SQL queries; filters run before count and pagination.

        trim_description / trim_text 仅供列表 HTTP 端点裁剪大字段（前端列表只渲染摘要，
        全文走详情端点按需读取）；仓储内部调用方默认不裁剪，保持完整负载。
        """
        where, params = [], []
        if resource == 'jobs':
            table, order = 'jobs', 'updated_at DESC, id DESC'
            where, params = self._job_filters(q, location, source, salary_min, salary_max, starred, pipeline_status)
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
                items = self._attach_evidence_ids(conn, items)
                if trim_description:
                    for item in items:
                        description = str(item.get('description') or '')
                        if len(description) > trim_description:
                            item['description'] = description[:trim_description]
                            item['description_truncated'] = True
            elif resource == 'evidence' and trim_text:
                items = self._trim_evidence_text(items, q, trim_text)
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

    # ── 今日概览（2026-11-18）：单端点聚合的本地查询，全部只读本库。 ──

    def overview_counts(self):
        """档案规模一览：岗位/证据（不含岗位快照）/公司主体/收藏/管线中。"""
        with self.connection() as conn:
            row = conn.execute("""SELECT
                (SELECT count(*) FROM jobs) AS jobs,
                (SELECT count(*) FROM evidence WHERE kind!='job_posting') AS evidence,
                (SELECT count(*) FROM companies) AS companies,
                (SELECT count(*) FROM jobs WHERE json_extract(payload,'$.starred')=1) AS favorites,
                (SELECT count(*) FROM jobs WHERE json_extract(payload,'$.pipeline_status') IS NOT NULL) AS tracked""").fetchone()
        return dict(row)

    def recent_first_seen(self, days=7, limit=6):
        """近 N 天首次入库的岗位：首见时间取该岗位关联快照的最早采集时间（重采集会刷新 updated_at，
        但首见锚在第一次落库，不受影响）。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.connection() as conn:
            rows = conn.execute("""SELECT j.payload, min(json_extract(e.payload,'$.collected_at')) AS first_seen
                                   FROM jobs j
                                   JOIN job_evidence je ON je.job_id=j.id
                                   JOIN evidence e ON e.id=je.evidence_id
                                   WHERE e.kind='job_posting' AND json_extract(j.payload,'$.duplicate_of') IS NULL
                                   GROUP BY j.id HAVING first_seen >= ?
                                   ORDER BY first_seen DESC LIMIT ?""", (cutoff, limit)).fetchall()
        items = []
        for row in rows:
            item = json.loads(row['payload'])
            items.append({'id': item.get('id'), 'title': item.get('title', ''), 'company': item.get('company', ''),
                          'source': item.get('source', ''), 'salary': item.get('salary', ''),
                          'location': item.get('location', ''), 'starred': bool(item.get('starred')),
                          'first_seen_at': row['first_seen']})
        return items

    def pipeline_due_items(self, limit=20):
        """设了截止日且仍在活跃状态（意向/已投/面试中）的管线岗位：今日概览的待办主体。

        逾期排最前，其余按截止日升序；offer/归档是终态，不再算待办。"""
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            rows = conn.execute("""SELECT payload FROM jobs
                                   WHERE json_extract(payload,'$.pipeline_due') IS NOT NULL
                                   ORDER BY json_extract(payload,'$.pipeline_due') ASC""").fetchall()
        items = []
        for row in rows:
            item = json.loads(row[0])
            if item.get('pipeline_status') not in ('interested', 'applied', 'interviewing'):
                continue
            due = str(item.get('pipeline_due') or '')
            items.append({'id': item.get('id'), 'title': item.get('title', ''), 'company': item.get('company', ''),
                          'status': item.get('pipeline_status'), 'due': due, 'overdue': due < today,
                          'note': item.get('pipeline_note', ''), 'changed_at': item.get('pipeline_status_changed_at', '')})
            if len(items) >= limit:
                break
        return items

    # ── 岗位去重（2026-11-19）：同一岗位多平台/多次采集各存一条时，标记主次而不是删除。 ──
    # 重复项默认不进列表与统计（_job_filters / jobs()），详情按 id 仍可读；拆开即恢复独立身份。

    @staticmethod
    def _job_title_key(title):
        """标题归一键：NFKC 折全角、去括号装饰与空白、casefold。只做形态归一，不做语义判断。"""
        text = unicodedata.normalize('NFKC', str(title or '')).casefold()
        text = re.sub(r'[\s]*[\u3010\[\(（][^\u3011\]\)）]*[\u3011\]\)）][\s]*', '', text)
        text = re.sub(r'[^\w\u4e00-\u9fff]+', '', text, flags=re.UNICODE)
        return text

    def duplicate_candidates(self, max_pairs=50):
        """规则扫描候选重复对：同公司 + 标题归一相等或互相包含 + 双方都未标记。

        只返回候选，绝不自动合并——错合并比漏合并糟（删错的统计口径要人工回溯）。"""
        with self.connection() as conn:
            rows = conn.execute("""SELECT payload FROM jobs
                                   ORDER BY json_extract(payload,'$.company'), json_extract(payload,'$.title')""").fetchall()
        jobs = [json.loads(row[0]) for row in rows]
        def summary(item):
            return {'id': item.get('id'), 'title': item.get('title', ''), 'company': item.get('company', ''),
                    'location': item.get('location', ''), 'salary': item.get('salary', ''),
                    'source': item.get('source', ''), 'updated_at': item.get('updated_at', ''),
                    'starred': bool(item.get('starred')), 'pipeline_status': item.get('pipeline_status') or ''}
        candidates = []
        groups: dict[str, list[dict]] = {}
        for item in jobs:
            if item.get('duplicate_of'):
                continue
            groups.setdefault(str(item.get('company') or ''), []).append(item)
        for company, members in groups.items():
            if len(members) < 2 or len(members) > 60:  # 超大组多半是泛化公司名，配对噪声大，不扫
                continue
            keyed = [(self._job_title_key(item.get('title')), item) for item in members]
            for i in range(len(keyed)):
                for j in range(i + 1, len(keyed)):
                    key_a, item_a = keyed[i]
                    key_b, item_b = keyed[j]
                    if not key_a or not key_b:
                        continue
                    if key_a == key_b or key_a in key_b or key_b in key_a:
                        candidates.append({'company': company, 'a': summary(item_a), 'b': summary(item_b),
                                           'match': 'exact' if key_a == key_b else 'contains'})
                        if len(candidates) >= max_pairs:
                            return {'candidates': candidates, 'truncated': True}
        return {'candidates': candidates, 'truncated': False}

    def mark_duplicates(self, primary_id, duplicate_ids):
        """标记主次：重复项的用户意图字段（收藏/管线/跟进）主记录缺什么就上提什么，
        然后重复项自身清掉这些字段并写 duplicate_of——意图不丢，统计不再双算。"""
        primary = self.job(primary_id)
        if primary is None:
            raise ValueError('主记录岗位不存在')
        duplicate_ids = [job_id for job_id in dict.fromkeys(duplicate_ids) if job_id != primary_id]
        carried: dict[str, bool] = {}
        with self.connection() as conn:
            for job_id in duplicate_ids:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    continue
                dup = json.loads(row['payload'])
                if dup.get('duplicate_of'):
                    continue  # 已属于其他组：不跨组抢记录，UI 层面由「拆开」先行
                # 意图上提：主记录没有的字段才搬（重复项的收藏/管线不该静默消失）。
                if dup.get('starred') and not primary.get('starred'):
                    primary['starred'] = True
                    primary['starred_at'] = dup.get('starred_at') or primary.get('starred_at') or now()
                    carried['starred'] = True
                if dup.get('pipeline_status') and not primary.get('pipeline_status'):
                    for field in ('pipeline_status', 'pipeline_status_changed_at', 'pipeline_note', 'pipeline_due'):
                        if dup.get(field):
                            primary[field] = dup[field]
                    carried['pipeline'] = True
                dup['duplicate_of'] = primary_id
                for field in ('starred', 'starred_at', 'pipeline_status', 'pipeline_status_changed_at',
                              'pipeline_note', 'pipeline_due'):
                    dup.pop(field, None)
                conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(dup, ensure_ascii=False), job_id))
            primary['updated_at'] = now()
            conn.execute('UPDATE jobs SET payload=?, updated_at=? WHERE id=?',
                         (json.dumps(primary, ensure_ascii=False), primary['updated_at'], primary_id))
        return {'primary': primary, 'marked': len(duplicate_ids), 'carried': carried}

    def release_duplicates(self, ids):
        """拆开：清掉 duplicate_of，岗位恢复独立身份（重新进列表与统计）。
        之前上提到主记录的收藏/管线不回迁——那是一次显式合并动作，不能靠猜反向执行。"""
        released = 0
        for job_id in dict.fromkeys(ids):
            with self.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    continue
                item = json.loads(row['payload'])
                if not item.get('duplicate_of'):
                    continue
                item.pop('duplicate_of', None)
                conn.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(item, ensure_ascii=False), job_id))
                released += 1
        return released

    def duplicate_groups(self):
        """现有重复组：主记录 + 其重复项的摘要；主记录已删除的孤儿自动晋升（清标记）。"""
        orphans = []
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.duplicate_of') IS NOT NULL").fetchall()
            live_ids = {row['id'] for row in conn.execute('SELECT id FROM jobs').fetchall()} if rows else set()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            item = json.loads(row['payload'])
            target = str(item.get('duplicate_of') or '')
            if target not in live_ids:
                orphans.append(item['id'])
                continue
            grouped.setdefault(target, []).append(item)
        released = self.release_duplicates(orphans) if orphans else 0
        groups = []
        for primary_id, dups in grouped.items():
            primary = self.job(primary_id)
            if primary is None:
                continue
            def summary(item):
                return {'id': item.get('id'), 'title': item.get('title', ''), 'company': item.get('company', ''),
                        'location': item.get('location', ''), 'salary': item.get('salary', ''),
                        'source': item.get('source', ''), 'updated_at': item.get('updated_at', '')}
            groups.append({'primary': summary(primary), 'duplicates': [summary(item) for item in dups]})
        return {'groups': groups, 'promoted': released}

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
            messages = messages + stamped
            conn.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), now(), conversation_id))
        return stamped

    def append_conversation(self, conversation_id, message, answer):
        self.append_messages(conversation_id, [dict(role='user', content=message), dict(role='assistant', content=answer)])

    def list_conversations(self, scope, limit=200):
        encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        # 摘要字段（条数、首条用户消息）由 SQLite 的 JSON 函数在库侧计算：
        # 会话 messages 含完整 trace，逐会话 json.loads 只为了数个数在这里是纯开销。
        with self.connection() as conn:
            rows = conn.execute('''
                SELECT id,title,mode,model,llm_channel,permission_mode,updated_at,
                       json_array_length(messages) AS message_count,
                       (SELECT substr(CAST(json_extract(value,'$.content') AS TEXT),1,80)
                        FROM json_each(conversations.messages)
                        WHERE json_extract(value,'$.role')='user' LIMIT 1) AS preview
                FROM conversations WHERE scope=? ORDER BY updated_at DESC LIMIT ?''', (encoded, limit)).fetchall()
        return [dict(id=row['id'], title=row['title'] or row['preview'] or '新对话', mode=row['mode'] or 'research',
                     model=row['model'], llm_channel=row['llm_channel'] or '',
                     permission_mode=row['permission_mode'] or 'autonomous', updated_at=row['updated_at'],
                     message_count=row['message_count'] or 0, preview=row['preview'] or '') for row in rows]

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

    def branch_for_edit(self, conversation_id, scope, message_id):
        """Create an independent history branch without copying runtime approvals."""
        original = self.get_conversation(conversation_id, scope)
        messages = original['messages']
        index = next((i for i, item in enumerate(messages) if item.get('id') == message_id), None)
        if index is None:
            raise ValueError('消息不存在')
        if messages[index].get('role') != 'user':
            raise ValueError('只能对用户消息执行重新生成')
        branch = self.create_conversation(scope, mode=original['mode'], model=original.get('model', ''), llm_channel=original.get('llm_channel', ''), permission_mode=original.get('permission_mode', 'autonomous'))
        history = messages[:index]
        with self.connection() as conn:
            private_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_private_conversations'").fetchone()
            if private_table:
                conn.execute('INSERT OR IGNORE INTO agent_private_conversations(id) SELECT ? FROM agent_private_conversations WHERE id=?', (branch, conversation_id))
        self.append_messages(branch, history)
        self.ensure_title(branch, scope, (original.get('title') or '对话') + ' · 修订')
        return branch, history

    def supplement_job_description(self, job_id, description, source_note):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('岗位不存在')
            item = json.loads(row['payload'])
            evidence = self._save_evidence(conn, dict(title=item['title'] + '（用户补录完整职责）', company=item['company'], kind='job_posting', text=description, source_note=source_note, original_job_id=job_id, url=''), 'user_import')
            conn.execute('INSERT OR IGNORE INTO job_evidence VALUES (?,?)', (job_id, evidence['id']))
            timestamp = now()
            item.update(description=description, description_status='complete', description_source='user', description_source_note=source_note, manual_description_at=timestamp, manual_description_evidence_id=evidence['id'], evidence_ids=[], updated_at=timestamp)
            conn.execute('UPDATE jobs SET payload=?, updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), timestamp, job_id))
        return self.job(job_id)

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

    # ── 工资基线（2026-11-17，人社局工资指导价位的手动导入）──────────────────
    # 身份 = 城市×职类×学历×年份：重复导入同一行是更新而不是新增（官方一年发一版，重导最新版
    # 是日常动作）。基线只作 pay_analysis 的同城对照兜底，出处始终写「官方指导价位导入」，
    # 与招聘报价中位数严格区分口径。

    def save_wage_baselines(self, rows):
        saved = 0
        with self.connection() as conn:
            for row in rows:
                identity = digest(json.dumps([row['city'], row['role'], row['education'], row['year']], ensure_ascii=False))
                conn.execute('INSERT INTO wage_baselines VALUES (?,?,?,?,?,?,?,?) '
                             'ON CONFLICT(id) DO UPDATE SET monthly_median=excluded.monthly_median, note=excluded.note, created_at=excluded.created_at',
                             ('W-' + identity[:24], row['city'], row['role'], row['education'], row['year'],
                              row['monthly_median'], row.get('note', ''), now()))
                saved += 1
        return saved

    def wage_baselines(self):
        """全量基线，同键多年份按年份倒序（取数方拿第一行即最新版）。"""
        with self.connection() as conn:
            rows = conn.execute('SELECT id, city, role, education, year, monthly_median, note, created_at '
                                'FROM wage_baselines ORDER BY city, role, education, year DESC').fetchall()
        return [dict(row) for row in rows]

    def delete_wage_baseline(self, baseline_id):
        with self.connection() as conn:
            return conn.execute('DELETE FROM wage_baselines WHERE id=?', (baseline_id,)).rowcount

    def save_reliability(self, report):
        with self.connection() as conn:
            # 同一输入重算时原位更新而不是 IGNORE：TTL 过期这类判定随时间变化而输入哈希不变，
            # IGNORE 会把新算的报告静默丢掉，重算响应与库里 latest 各说各话（打开详情即自动
            # 重算后这种漂移每次刷新都会发生）。历史仍按输入变化记行：同输入永远只有一行。
            conn.execute('INSERT INTO company_reliability VALUES (?,?,?,?) '
                         'ON CONFLICT(company_id, input_hash) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload',
                         (report['company_id'], report['input_hash'], report['computed_at'], json.dumps(report, ensure_ascii=False)))

    def latest_reliability(self, company_id):
        with self.connection() as conn:
            row = conn.execute('SELECT payload FROM company_reliability WHERE company_id=? ORDER BY created_at DESC LIMIT 1', (company_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def reliability_history(self, company_id):
        with self.connection() as conn:
            return [json.loads(row['payload']) for row in conn.execute('SELECT payload FROM company_reliability WHERE company_id=? ORDER BY created_at', (company_id,)).fetchall()]

    @staticmethod
    def _jobs_counts(conn):
        """按规范化公司名统计已存岗位数（一条 GROUP BY 在库侧归并）：compare 候选与主体归并页共用。"""
        counts = {}
        for row in conn.execute("SELECT json_extract(payload,'$.company') AS company, count(*) AS n FROM jobs GROUP BY json_extract(payload,'$.company')"):
            name = Repository.normalise_name(row['company'])
            if name:
                counts[name] = counts.get(name, 0) + row['n']
        return counts

    @staticmethod
    def _entity_jobs_count(counts, name, aliases):
        names = {Repository.normalise_name(value) for value in [name, *aliases] if str(value or '').strip()}
        return sum(counts.get(value, 0) for value in names)

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
            # jobs_count 用一条 GROUP BY 在库侧归并，替代全量 jobs() 的逐行遍历。
            counts = self._jobs_counts(conn)
        result = []
        for row in rows:
            item = dict(row)
            try:
                aliases = json.loads(item.pop('aliases') or '[]')
            except ValueError:
                aliases = []
            item['jobs_count'] = self._entity_jobs_count(counts, item['company'], aliases)
            result.append(item)
        return dict(companies=result)

    # ── 主体归并：合并=别名并集+迁移附属行+删被并方实体；拆分=别名独立重建实体。 ──
    # jobs/evidence 只存原始公司名，读路径一律按「实体名+别名」的规范化集合匹配，所以合并不需要
    # 回填任何岗位行；需要搬家的只有按 company_id 外键挂着的 company_signals / company_reliability
    # （外键 PRAGMA 是开着的，直接删实体行会炸）。

    def list_merge_entities(self):
        """主体归并页的实体全量列表：名称、别名、岗位数。按岗位数排序，数据多的主体排前面。"""
        with self.connection() as conn:
            rows = conn.execute("SELECT id, name, credit_code, aliases, payload FROM companies ORDER BY name").fetchall()
            counts = self._jobs_counts(conn)
        entities = []
        for row in rows:
            try:
                aliases = json.loads(row['aliases'] or '[]')
            except ValueError:
                aliases = []
            entities.append(dict(id=row['id'], name=row['name'], credit_code=row['credit_code'],
                                 aliases=aliases, jobs_count=self._entity_jobs_count(counts, row['name'], aliases)))
        entities.sort(key=lambda item: (-item['jobs_count'], item['name']))
        return entities

    def merge_companies(self, primary_id, secondary_id):
        """把 secondary 实体并入 primary：别名并集、附属行迁移、删被并方实体行，单事务完成。

        primary/secondary 存疑时由调用方（LLM 判定或人工选择）决定方向；这里只管原子地搬家。
        信号直接 UPDATE company_id（行 id/identity 不变）；报告撞主名方同 input_hash 时跳过并
        丢弃被并方那份——报告是派生数据，合并后重算即恢复，不值得为此让合并失败。
        """
        if primary_id == secondary_id:
            raise ValueError('不能把公司主体并入它自己')
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = {row['id']: row for row in conn.execute(
                'SELECT id, name, credit_code, aliases, payload FROM companies WHERE id IN (?, ?)',
                (primary_id, secondary_id))}
            if primary_id not in rows or secondary_id not in rows:
                raise ValueError('公司主体不存在或已被合并')
            primary, secondary = rows[primary_id], rows[secondary_id]
            entity = json.loads(primary['payload'])
            try:
                absorbed = json.loads(secondary['aliases'] or '[]')
            except ValueError:
                absorbed = []
            merged = {self.normalise_name(value) for value in [*entity['aliases'], *absorbed, secondary['name']]
                      if str(value or '').strip()}
            merged.discard(entity['name'])
            entity['aliases'] = sorted(merged)
            entity['updated_at'] = now()
            conn.execute('UPDATE companies SET aliases=?, payload=?, updated_at=? WHERE id=?',
                         (json.dumps(entity['aliases'], ensure_ascii=False), json.dumps(entity, ensure_ascii=False),
                          entity['updated_at'], primary_id))
            # 附属行搬家用 UPDATE 改 company_id（行 id 与 identity 不变，天然不撞键）；
            # 报告的 PK 含 company_id，主名方已有同 input_hash 的报告时 UPDATE OR IGNORE 跳过，
            # 残留在被并方名下的行随 DELETE 丢弃——报告是派生数据，合并后重算即恢复。
            conn.execute('UPDATE company_signals SET company_id=? WHERE company_id=?', (primary_id, secondary_id))
            conn.execute('UPDATE OR IGNORE company_reliability SET company_id=? WHERE company_id=?',
                         (primary_id, secondary_id))
            conn.execute('DELETE FROM company_reliability WHERE company_id=?', (secondary_id,))
            conn.execute('DELETE FROM companies WHERE id=?', (secondary_id,))
        return entity

    def split_alias(self, company_id, alias):
        """把别名从实体拆出去独立成新实体：只动别名表，signals/报告不回迁（拆分后各自重算）。"""
        cleaned = self.normalise_name(alias)
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM companies WHERE id=?', (company_id,)).fetchone()
            if row is None:
                raise ValueError('公司主体不存在')
            entity = json.loads(row['payload'])
            aliases = [value for value in entity.get('aliases', []) if self.normalise_name(value) != cleaned]
            if len(aliases) == len(entity.get('aliases', [])):
                raise ValueError('这个名字不是该主体的别名，无法拆分')
            entity['aliases'] = aliases
            entity['updated_at'] = now()
            conn.execute('UPDATE companies SET aliases=?, payload=?, updated_at=? WHERE id=?',
                         (json.dumps(entity['aliases'], ensure_ascii=False), json.dumps(entity, ensure_ascii=False),
                          entity['updated_at'], company_id))
            reborn = self._upsert_company(conn, cleaned)
        return reborn

    def add_merge_proposal(self, name_a, name_b, reason):
        """登记一对合并候选（pending）。同一对（任一方向）出现过任何状态就跳过：pending/accepted
        不重复提，dismissed 的也不重提——用户明确忽略过的一对，下一轮扫描再弹出来只是打扰。"""
        a, b = self.normalise_name(name_a), self.normalise_name(name_b)
        if not a or not b or a == b:
            return None
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('''SELECT 1 FROM company_merge_proposals
                WHERE (company_a=? AND company_b=?) OR (company_a=? AND company_b=?)''',
                (a, b, b, a)).fetchone()
            if existing:
                return None
            proposal_id = 'CM-' + uuid.uuid4().hex[:16]
            conn.execute('INSERT INTO company_merge_proposals VALUES (?,?,?,?,?,?,?)',
                         (proposal_id, a, b, str(reason)[:2000], 'pending', now(), ''))
            row = conn.execute('SELECT * FROM company_merge_proposals WHERE id=?', (proposal_id,)).fetchone()
        return dict(row)

    def list_merge_proposals(self, limit=100):
        """pending 在前、resolved 按时间倒序：归并页一次拉全，徽标数取 pending 条数。"""
        with self.connection() as conn:
            rows = conn.execute('''SELECT * FROM company_merge_proposals
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC, id LIMIT ?''',
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def resolve_merge_proposal(self, proposal_id, status):
        """pending → accepted/dismissed 的单向流转；返回该 proposal（含双方规范化名），供调用方执行合并。"""
        if status not in ('accepted', 'dismissed'):
            raise ValueError('合并建议只能被接受或忽略')
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM company_merge_proposals WHERE id=?', (proposal_id,)).fetchone()
            if row is None:
                raise ValueError('合并建议不存在')
            if row['status'] != 'pending':
                raise ValueError('这条合并建议已经处理过了')
            conn.execute('UPDATE company_merge_proposals SET status=?, resolved_at=? WHERE id=?',
                         (status, now(), proposal_id))
            updated = conn.execute('SELECT * FROM company_merge_proposals WHERE id=?', (proposal_id,)).fetchone()
        return dict(updated)
