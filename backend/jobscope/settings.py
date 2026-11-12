"""Workspace-local model configuration; public reads never expose credentials."""
import json
import os
import time
import uuid
from contextvars import ContextVar
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .repository import now

_active = ContextVar('jobscope_model_config', default=None)


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['disabled', 'remote', 'local']
    channel: str = Field(default='custom', max_length=100)
    protocol: Literal['openai', 'anthropic'] = 'openai'
    url: str = Field(default='', max_length=2000)
    model: str = Field(default='', max_length=300)
    local_model: str = Field(default='', max_length=1000)
    key: str = Field(default='', max_length=4000)
    clear_key: bool = False
    # 上下文窗口大小只用于前端用量估算，不参与任何请求；留空按 128000 估算。
    context_tokens: int | None = Field(default=None, ge=4000, le=2_000_000)


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm: ProviderInput | None = None
    embedding: ProviderInput | None = None


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['llm', 'embedding']
    config: ProviderInput
    channel_id: str | None = Field(default=None, max_length=100)


class AgentLimitsInput(BaseModel):
    """Agent 执行限额：只做类型校验，范围钳制在 SystemSettingsStore.save 里统一处理。"""
    model_config = ConfigDict(extra='forbid')
    max_model_calls: int = Field(ge=0, le=100000)
    max_tool_calls: int = Field(ge=0, le=100000)
    context_characters: int = Field(ge=0, le=10000000)
    max_seconds: int = Field(ge=0, le=100000)


class SystemSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_display_mode: Literal['merged', 'sequential']
    show_reasoning: bool
    agent_limits: AgentLimitsInput


class LlmChannelInput(BaseModel):
    """一条可保存的对话渠道：密钥与连接信息的唯一事实源，功能绑定指向它。"""
    model_config = ConfigDict(extra='forbid')
    id: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    channel: str = Field(default='custom', max_length=100)
    protocol: Literal['openai', 'anthropic'] = 'openai'
    url: str = Field(default='', max_length=2000)
    model: str = Field(default='', max_length=300)
    models: list[str] = Field(default_factory=list, max_length=200)
    embeddings: list[str] = Field(default_factory=list, max_length=200)
    key: str = Field(default='', max_length=4000)
    clear_key: bool = False
    context_tokens: int | None = Field(default=None, ge=4000, le=2_000_000)


def environment():
    def remote(prefix):
        url, model, key = (os.getenv('JOBSCOPE_' + prefix + part, '') for part in ('_URL', '_MODEL', '_KEY'))
        return dict(mode='remote' if url and model else 'disabled', channel='custom', protocol=os.getenv('JOBSCOPE_' + prefix + '_PROTOCOL', 'openai'), url=url, model=model, key=key, local_model='', source='environment' if any((url, model, key)) else 'default')
    result = dict(llm=remote('LLM'), embedding=remote('EMBEDDING'))
    local = os.getenv('JOBSCOPE_EMBEDDING_LOCAL_MODEL', '')
    if local:
        result['embedding'].update(mode='local', local_model=local, source='environment')
    # 抽取/分析未绑定时跟随对话默认；这里是线程级兜底（正式组装在 ModelSettings.effective）。
    result['extraction'] = dict(result['llm'])
    result['analysis'] = dict(result['llm'])
    return result


def get_model_config(target='llm'):
    return (_active.get() or environment())[target].copy()


def get_purpose_config(purpose='extraction'):
    return (_active.get() or environment())[purpose].copy()


def purpose_configured(purpose):
    return get_purpose_config(purpose)['mode'] == 'remote'


class use_config:
    """Bind model config for the current thread (background run workers have no request context)."""

    def __init__(self, config):
        self._config = config
        self._token = None

    def __enter__(self):
        self._token = _active.set(self._config)
        return self._config

    def __exit__(self, *exc_info):
        _active.reset(self._token)
        return False


class ModelSettings:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            # effective() 单连接读三表的前提：这里的建表口径与 ModelBindings/LlmChannelStore
            # 各自的构造器完全一致（IF NOT EXISTS 幂等），避免组装配置时逐 store 再开连接建表。
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS model_settings(target TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_bindings(purpose TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_channels(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            ''')

    def channel_payload(self, channel_id):
        return LlmChannelStore(self.repo).get(channel_id)

    def effective(self):
        """组装四个 purpose 的生效配置。中间件每个 /api 请求都会调用：单连接一次读齐
        model_settings / model_bindings / llm_channels 三表，替代原先逐 store 各开连接
        （外加两次构造器建表 DDL）的固定税。"""
        result = environment()
        with self.repo.connection() as conn:
            for row in conn.execute('SELECT target,payload FROM model_settings'):
                saved = json.loads(row[1])
                result[row[0]].update(**dict(channel='custom', protocol='openai') | saved, source='saved')
            bindings = {row['purpose']: json.loads(row['payload'])
                        for row in conn.execute('SELECT purpose,payload FROM model_bindings')}
            channels = {row['id']: json.loads(row['payload'])
                        for row in conn.execute('SELECT id,payload FROM llm_channels')}

        def resolve(purpose):
            binding = bindings.get(purpose)
            if not binding or binding.get('kind') in (None, 'inherit'):
                return None
            kind = binding.get('kind')
            if kind == 'disabled':
                return dict(mode='disabled', channel='custom', protocol='openai', url='', model='', key='', local_model='', source='binding')
            if kind == 'local':
                return dict(mode='local', channel='custom', protocol='openai', url='', model='', key='', local_model=binding.get('local_model', ''), source='binding')
            payload = channels.get(binding.get('channel_id', ''))
            if payload is None:
                return None  # 渠道已被删除：回落默认配置，绑定在功能页可见为“渠道已删除”
            protocol = payload.get('protocol', 'openai')
            if purpose == 'embedding' and protocol == 'anthropic':
                return None
            from .model_transport import endpoint
            url = payload['url']
            if purpose == 'embedding':
                url = endpoint(url, 'openai', 'embedding')
                protocol = 'openai'
            return dict(mode='remote', channel=payload.get('channel', 'custom'), protocol=protocol, url=url,
                        model=binding.get('model') or payload.get('model', ''), key=payload.get('key', ''),
                        local_model='', context_tokens=payload.get('context_tokens'),
                        saved_channel_id=payload['id'], source='binding')

        agent = resolve('agent')
        if agent:
            result['llm'] = agent
        embedding = resolve('embedding')
        if embedding:
            result['embedding'] = embedding
        for purpose in ('extraction', 'analysis'):
            result[purpose] = resolve(purpose) or dict(result['llm'])
        return result

    @staticmethod
    def public(config):
        return {target: {**{k: v for k, v in value.items() if k != 'key'}, 'key_set': bool(value.get('key'))} for target, value in config.items()}

    def prepare(self, target, value, current, *, require_model=True):
        from .chat import validate_model_url
        from .channels import channel_by_id
        from .model_transport import endpoint
        data = value.model_dump(exclude={'clear_key'})
        data = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
        if target == 'llm' and data['mode'] == 'local':
            raise ValueError('对话模型请使用远端或本机 HTTP 兼容接口')
        old = current[target]
        channel = channel_by_id(data['channel'])
        if 'protocol' not in value.model_fields_set:
            data['protocol'] = channel['protocol']
        if target == 'embedding' and (data['protocol'] == 'anthropic' or channel['embeddings'] is None) and data['mode'] == 'remote':
            raise ValueError('该渠道或协议不支持 Embeddings，请选择支持向量模型的渠道')
        if data['mode'] == 'remote':
            data['url'] = data['url'] or channel['url']
            if data['url']:
                data['url'] = endpoint(data['url'], data['protocol'], target)
        # Disabled/local payloads need not repeat inactive fields. Preserve their
        # binding rather than moving an existing credential to another URL.
        if data['mode'] != 'remote':
            data['url'] = data['url'] or old.get('url', '')
            data['model'] = data['model'] or old.get('model', '')
            data['local_model'] = data['local_model'] or old.get('local_model', '')
            for field in ('channel', 'protocol'):
                if field not in value.model_fields_set:
                    data[field] = old.get(field, data[field])
        if not data['key'] and not value.clear_key:
            previous_url = endpoint(old['url'], old.get('protocol', 'openai'), target) if old.get('url') else ''
            current_url = endpoint(data['url'], data['protocol'], target) if data['url'] else ''
            if old.get('key') and (current_url != previous_url or data['protocol'] != old.get('protocol', 'openai')):
                raise ValueError('更换服务地址时请重新填写密钥，或勾选清除密钥')
            data['key'] = old.get('key', '')
        if data['mode'] == 'remote':
            if not data['url'] or (require_model and not data['model']):
                raise ValueError('请选择渠道或填写接口地址，并选择或输入模型 ID')
            validate_model_url(data['url'])
        if data['mode'] == 'local' and not data['local_model']:
            raise ValueError('请填写已缓存的本地模型名称或路径')
        return data

    def save(self, data):
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = self.effective()
            updates = {target: self.prepare(target, value, current) for target, value in [('llm', data.llm), ('embedding', data.embedding)] if value is not None}
            for target, value in updates.items():
                conn.execute('INSERT OR REPLACE INTO model_settings VALUES (?,?)', (target, json.dumps(value, ensure_ascii=False)))
        return self.public(self.effective())

    def connection_config(self, target, value, channel_id=None, *, require_model=True):
        current = self.effective()
        if channel_id:
            if target != 'llm':
                raise ValueError('已保存对话渠道仅支持对话模型')
            current['llm'] = LlmChannelStore(self.repo).get(channel_id)
        return self.prepare(target, value, current, require_model=require_model)

    def test(self, target, value, channel_id=None):
        from .model_transport import complete, OutputLimitError
        data = self.connection_config(target, value, channel_id)
        if data['mode'] == 'disabled':
            raise ValueError('请先选择一个模型连接方式')
        start = time.perf_counter()
        dimensions = None
        try:
            if target == 'embedding':
                from .research.providers import EmbeddingProvider
                vectors = EmbeddingProvider(config=data).embed(['连接测试'])
                dimensions = len(vectors[0])
            else:
                complete(data, '', '请只回复 OK。这是连接测试。', max_tokens=1024, timeout=60)
        except OutputLimitError:
            raise ConnectionError('服务已连接，模型已响应，但测试输出额度耗尽，尚未生成最终答案。不是地址或密钥错误；可换用非推理模型后重试。配置未被本次测试修改。') from None
        except httpx.TimeoutException:
            raise ConnectionError('推理请求超时，尚未验证最终回答。模型列表与推理接口耗时不同，请稍后重试或检查渠道的推理服务。配置未被本次测试修改。') from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, OSError):
            raise ConnectionError('连接测试失败，请检查接口地址、模型、密钥或本地模型缓存。配置未被本次测试修改。') from None
        return dict(ok=True, dimensions=dimensions, duration_ms=round((time.perf_counter() - start) * 1000), message='连接成功；仅验证服务与响应格式，未评估回答质量')

    def discover(self, target, value, channel_id=None):
        from .model_transport import endpoint, headers
        data = self.connection_config(target, value, channel_id, require_model=False)
        if data['mode'] != 'remote':
            raise ValueError('在线模型列表需要 API 连接')
        models = set()
        # Anthropic lists are paginated. Bound both pages and total entries.
        url = endpoint(data['url'], data['protocol'], 'models')
        try:
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
                after = None
                for _ in range(5):
                    kwargs = dict(headers=headers(data))
                    if data['protocol'] == 'anthropic':
                        kwargs['params'] = dict(limit=100, **({'after_id': after} if after else {}))
                    response = client.get(url, **kwargs)
                    response.raise_for_status()
                    if len(response.content) > 2000000:
                        raise ValueError()
                    result = response.json()
                    rows = result['data']
                    if not isinstance(rows, list):
                        raise ValueError()
                    for row in rows[:1000]:
                        name = row.get('id') if isinstance(row, dict) else None
                        if isinstance(name, str) and name.strip() and len(name) <= 300:
                            models.add(name.strip())
                    if data['protocol'] != 'anthropic' or not result.get('has_more'):
                        break
                    next_id = result.get('last_id')
                    if not isinstance(next_id, str) or not next_id or next_id == after:
                        break
                    after = next_id
            return dict(models=sorted(models)[:1000], source='live', message='账户返回的模型列表；具体调用权限请通过测试确认')
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
            raise ConnectionError('获取模型失败，请检查渠道地址、密钥和账户权限。该渠道也可能不提供模型列表；可搜索预设或手动输入模型 ID 后测试。') from None


class LlmChannelStore:
    """已保存的渠道清单：每条渠道自带连接信息、密钥与模型列表；功能绑定指向它。"""

    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS llm_channels(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    def list(self):
        with self.repo.connection() as conn:
            rows = conn.execute('SELECT payload FROM llm_channels').fetchall()
        channels = [json.loads(row[0]) for row in rows]
        channels.sort(key=lambda item: (item.get('created_at', ''), item.get('id', '')))
        return channels

    @staticmethod
    def public(channels):
        return [{**{k: v for k, v in item.items() if k != 'key'}, 'key_set': bool(item.get('key'))} for item in channels]

    def get(self, channel_id):
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload FROM llm_channels WHERE id=?', (channel_id,)).fetchone()
        if row is None:
            raise ValueError('渠道不存在或已删除')
        return json.loads(row[0])

    def save(self, data):
        from .chat import validate_model_url
        from .channels import channel_by_id
        from .model_transport import endpoint
        stored = {item['id']: item for item in self.list()}
        channel_id = data.id or 'LC-' + uuid.uuid4().hex[:12]
        old = stored.get(channel_id, {})
        if not data.id:
            # 同名视为同一条渠道：再保存一次是编辑而非无限增长。
            for item in stored.values():
                if item.get('name') == data.name.strip():
                    channel_id, old = item['id'], item
                    break
        channel = channel_by_id(data.channel)
        url = (data.url or '').strip() or channel['url']
        if not url:
            raise ValueError('请填写该渠道的服务地址')
        url = endpoint(url, data.protocol, 'llm')
        validate_model_url(url)
        # 与全局设置同一套凭证规则：换了地址或协议就必须重填密钥，不能把旧密钥发给新主机。
        if old and not data.clear_key and not data.key.strip() and old.get('key'):
            if url != old.get('url') or data.protocol != old.get('protocol'):
                raise ValueError('更换服务地址或协议时请重新填写密钥，或勾选清除密钥')
        models = [m.strip()[:300] for m in data.models if isinstance(m, str) and m.strip()][:200]
        if not models:
            # 渠道预设与上次保存的列表都比空列表有用，避免保存后下拉里没有可选模型。
            models = old.get('models') or list(channel.get('models') or [])[:200]
        embeddings = [m.strip()[:300] for m in data.embeddings if isinstance(m, str) and m.strip()][:200]
        if not embeddings:
            embeddings = old.get('embeddings') or list(channel.get('embeddings') or [])[:200]
        payload = dict(id=channel_id, name=data.name.strip()[:100], channel=data.channel, protocol=data.protocol,
                       url=url, model=data.model.strip()[:300], models=models, embeddings=embeddings,
                       context_tokens=data.context_tokens,
                       created_at=old.get('created_at') or now(), updated_at=now())
        if data.clear_key:
            payload['key'] = ''
        elif data.key.strip():
            payload['key'] = data.key.strip()
        else:
            payload['key'] = old.get('key', '')
        with self.repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO llm_channels VALUES (?,?)', (channel_id, json.dumps(payload, ensure_ascii=False)))
        return payload

    def delete(self, channel_id):
        with self.repo.connection() as conn:
            cursor = conn.execute('DELETE FROM llm_channels WHERE id=?', (channel_id,))
            if cursor.rowcount == 0:
                raise ValueError('渠道不存在或已删除')

    def activate(self, channel_id, bindings):
        """设为对话默认：写一条指向渠道的绑定，不复制密钥——渠道是唯一事实源。"""
        payload = self.get(channel_id)
        if not payload.get('model'):
            raise ValueError('该渠道还没有默认模型，请先在渠道里填写默认模型')
        return bindings.save('agent', dict(kind='channel', channel_id=channel_id, model=payload.get('model')))


# Agent 执行限额默认值：general_agent 的模块常量从这里取，保证设置页与代码同一份事实。
AGENT_LIMIT_DEFAULTS = dict(max_model_calls=60, max_tool_calls=50, context_characters=160000, max_seconds=900)


class SystemSettingsStore:
    """系统级参数（聊天显示模式、思维链显隐、Agent 执行限额）：默认值合并，保存时钳制范围。"""

    DISPLAY_MODES = ('merged', 'sequential')
    # 误填 0 会直接废掉 Agent，天文数字会撑爆上下文/时限，保存时一律钳回该范围。
    LIMIT_RANGES = dict(max_model_calls=(1, 200), max_tool_calls=(1, 300),
                        context_characters=(10000, 1000000), max_seconds=(60, 7200))

    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    @staticmethod
    def _sanitize_limits(value):
        limits = dict(AGENT_LIMIT_DEFAULTS)
        for key, (low, high) in SystemSettingsStore.LIMIT_RANGES.items():
            try:
                limits[key] = min(high, max(low, int(value.get(key, limits[key]))))
            except (TypeError, ValueError):
                continue
        return limits

    def effective(self):
        result = dict(chat_display_mode='merged', show_reasoning=True, agent_limits=dict(AGENT_LIMIT_DEFAULTS))
        with self.repo.connection() as conn:
            for row in conn.execute('SELECT key,payload FROM system_settings'):
                saved = json.loads(row[1])
                if row[0] == 'agent_limits':
                    result['agent_limits'].update(self._sanitize_limits(saved))
                elif row[0] in result:
                    result[row[0]] = saved
        return result

    def save(self, data):
        if data.chat_display_mode not in self.DISPLAY_MODES:
            raise ValueError('未知的聊天显示模式')
        limits = self._sanitize_limits(data.agent_limits.model_dump())
        with self.repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO system_settings VALUES (?,?)',
                         ('chat_display_mode', json.dumps(data.chat_display_mode)))
            conn.execute('INSERT OR REPLACE INTO system_settings VALUES (?,?)',
                         ('show_reasoning', json.dumps(bool(data.show_reasoning))))
            conn.execute('INSERT OR REPLACE INTO system_settings VALUES (?,?)',
                         ('agent_limits', json.dumps(limits, ensure_ascii=False)))
        return self.effective()
