"""Persisted scope proposals and atomic, idempotent conversation snapshots."""
import json
import time
import uuid

from .repository import now


class ScopeExpansionStore:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS scope_expansions (
                id TEXT PRIMARY KEY, source_conversation_id TEXT NOT NULL,
                source_scope TEXT NOT NULL, target_scope TEXT NOT NULL,
                reason TEXT NOT NULL, target_conversation_id TEXT,
                created_at TEXT NOT NULL, accepted_at TEXT)''')
            conn.execute('CREATE INDEX IF NOT EXISTS scope_expansions_source ON scope_expansions(source_conversation_id)')

    @staticmethod
    def _action(row):
        return dict(id=row['id'], type='scope_expansion', source_scope=json.loads(row['source_scope']),
                    target_scope=json.loads(row['target_scope']), reason=row['reason'],
                    target_conversation_id=row['target_conversation_id'])

    @staticmethod
    def _target(source, target_kind):
        kind = source.get('kind')
        if target_kind not in {'company', 'global'}:
            raise ValueError('扩展目标必须是公司或全局范围')
        if kind not in {'job', 'company'} or (kind == 'company' and target_kind != 'global'):
            raise ValueError('只能从岗位扩展到同一公司或全局，从公司扩展到全局')
        if kind == 'job' and not source.get('company'):
            raise ValueError('岗位会话缺少公司范围')
        return dict(kind='global') if target_kind == 'global' else dict(kind='company', company=source['company'])

    def propose(self, conversation_id, target_kind, reason=''):
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError('扩展原因必须是不超过2000字的文本')
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            source = conn.execute('SELECT scope FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            if source is None:
                raise ValueError('会话不存在')
            scope = json.loads(source['scope'])
            target = self._target(scope, target_kind)
            action_id = 'SE-' + uuid.uuid4().hex
            conn.execute('INSERT INTO scope_expansions VALUES (?,?,?,?,?,?,?,?)',
                         (action_id, conversation_id, source['scope'], json.dumps(target, sort_keys=True, ensure_ascii=False),
                          reason.strip(), None, now(), None))
            row = conn.execute('SELECT * FROM scope_expansions WHERE id=?', (action_id,)).fetchone()
        return self._action(row)

    def list(self, conversation_id):
        with self.repo.connection() as conn:
            rows = conn.execute('SELECT * FROM scope_expansions WHERE source_conversation_id=? ORDER BY created_at,id',
                                (conversation_id,)).fetchall()
        return [self._action(row) for row in rows]

    def actions_for_messages(self, messages):
        """Resolve inherited cards too, without modifying the saved message snapshot."""
        ids = {action['id'] for message in messages for action in message.get('actions', [])
               if action.get('type') == 'scope_expansion' and action.get('id')}
        if not ids:
            return {}
        with self.repo.connection() as conn:
            rows = conn.execute('SELECT * FROM scope_expansions WHERE id IN (' + ','.join('?' for _ in ids) + ')',
                                tuple(ids)).fetchall()
        return {row['id']: self._action(row) for row in rows}

    def accept(self, conversation_id, action_id):
        """Caller reserves RunManager._lock and rejects active/compressing sources.

        BEGIN IMMEDIATE serializes concurrent accept retries across store instances. The
        proposal and the complete source row are read in the same snapshot as the clone
        write; no pending tool or run state is copied.
        """
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM scope_expansions WHERE id=? AND source_conversation_id=?',
                               (action_id, conversation_id)).fetchone()
            if row is None:
                raise ValueError('范围扩展提议不存在')
            target_id = row['target_conversation_id']
            if target_id is None:
                source = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
                if source is None:
                    raise ValueError('会话不存在')
                source_scope = json.loads(source['scope'])
                target_scope = json.loads(row['target_scope'])
                if source_scope != json.loads(row['source_scope']) or self._target(source_scope, target_scope['kind']) != target_scope:
                    raise ValueError('会话范围已变化，请重新提出扩展')
                # Approval queues survive process restarts, unlike the in-memory registry.
                pending_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_pending'").fetchone()
                if pending_table and conn.execute('''SELECT 1 FROM agent_pending
                    WHERE json_extract(payload,'$.conversation_id')=? AND created>=?''',
                    (conversation_id, time.time() - 3600)).fetchone():
                    raise ValueError('会话正在等待工具确认，请完成或停止后再扩展')
                target_id = 'C-' + uuid.uuid4().hex
                timestamp = now()
                conn.execute('''INSERT INTO conversations
                    (id,scope,messages,title,created_at,updated_at,mode,model,llm_channel)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (target_id, row['target_scope'], source['messages'], source['title'], timestamp, timestamp,
                     'general', source['model'], source['llm_channel']))
                # ExtensionStore owns this optional table; its actual schema is id TEXT PRIMARY KEY.
                private_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_private_conversations'").fetchone()
                if private_table:
                    conn.execute('''INSERT OR IGNORE INTO agent_private_conversations(id)
                        SELECT ? FROM agent_private_conversations WHERE id=?''', (target_id, conversation_id))
                conn.execute('UPDATE scope_expansions SET target_conversation_id=?,accepted_at=? WHERE id=? AND target_conversation_id IS NULL',
                             (target_id, timestamp, action_id))
        return dict(conversation_id=target_id, scope=json.loads(row['target_scope']),
                    source_conversation_id=conversation_id, action_id=action_id)

    def lineage(self, conversation_id):
        with self.repo.connection() as conn:
            row = conn.execute('SELECT source_conversation_id,id FROM scope_expansions WHERE target_conversation_id=?',
                               (conversation_id,)).fetchone()
        return dict(source_conversation_id=row['source_conversation_id'], action_id=row['id']) if row else None
