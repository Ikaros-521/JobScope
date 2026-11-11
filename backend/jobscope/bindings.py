"""Feature-level model bindings: each purpose points at a saved channel + model.

Channels (llm_channels) are the single source of truth for connection data and
keys; bindings are pointers, never copies. Missing rows fall back to the legacy
global model_settings rows so existing setups keep working unchanged.
"""
import json

from .repository import now

PURPOSES = ('agent', 'embedding', 'extraction', 'analysis')
# agent 没有模型就没有对话，不允许关闭；embedding 另有 local；抽取/分析可以关闭。
BINDING_KINDS = {
    'agent': ('channel',),
    'embedding': ('channel', 'local', 'disabled'),
    'extraction': ('channel', 'inherit', 'disabled'),
    'analysis': ('channel', 'inherit', 'disabled'),
}


def normalise_binding(purpose, payload):
    """Validate and normalise one binding payload; raises ValueError on bad input."""
    if purpose not in PURPOSES:
        raise ValueError('未知的模型用途')
    if not isinstance(payload, dict):
        raise ValueError('绑定格式无效')
    kind = payload.get('kind')
    if kind not in BINDING_KINDS[purpose]:
        raise ValueError(f'{purpose} 不支持绑定方式 {kind}')
    result = dict(kind=kind)
    if kind == 'channel':
        channel_id = payload.get('channel_id')
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError('请选择渠道')
        result['channel_id'] = channel_id.strip()
        model = payload.get('model')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('请选择该渠道下的模型')
        result['model'] = model.strip()[:300]
    elif kind == 'local':
        local_model = payload.get('local_model')
        if not isinstance(local_model, str) or not local_model.strip():
            raise ValueError('请填写本地缓存模型名称或路径')
        result['local_model'] = local_model.strip()[:1000]
    return result


class ModelBindings:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS model_bindings(purpose TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    def all(self):
        with self.repo.connection() as conn:
            rows = conn.execute('SELECT purpose,payload FROM model_bindings').fetchall()
        return {row['purpose']: json.loads(row['payload']) for row in rows}

    def get(self, purpose):
        return self.all().get(purpose)

    def save(self, purpose, payload):
        data = normalise_binding(purpose, payload)
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR REPLACE INTO model_bindings VALUES (?,?)', (purpose, json.dumps(data, ensure_ascii=False)))
        return data

    def clear(self, purpose):
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM model_bindings WHERE purpose=?', (purpose,))

    def channel_references(self, channel_id):
        """Purposes currently pointing at this channel; used as a delete guard."""
        found = []
        for purpose, payload in self.all().items():
            if payload.get('kind') == 'channel' and payload.get('channel_id') == channel_id:
                found.append(purpose)
        return found

    def migrate_legacy_activation(self, settings):
        """One-time: an activate() from before bindings existed left saved_channel_id
        on the materialised llm row. Promote it to an agent binding so switching to
        channel-as-truth does not silently change the user's default model."""
        with self.repo.connection() as conn:
            if conn.execute('SELECT 1 FROM model_bindings LIMIT 1').fetchone():
                return None
            row = conn.execute("SELECT payload FROM model_settings WHERE target='llm'").fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        channel_id = payload.get('saved_channel_id')
        if not channel_id:
            return None
        try:
            channel = settings.channel_payload(channel_id)
        except ValueError:
            return None
        return self.save('agent', dict(kind='channel', channel_id=channel_id, model=payload.get('model') or channel.get('model', '')))
