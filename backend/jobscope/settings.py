"""Workspace-local model configuration; public reads never expose credentials."""
import json
import os
import time
from contextvars import ContextVar
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

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


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm: ProviderInput | None = None
    embedding: ProviderInput | None = None


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['llm', 'embedding']
    config: ProviderInput


def environment():
    def remote(prefix):
        url, model, key = (os.getenv('JOBSCOPE_' + prefix + part, '') for part in ('_URL', '_MODEL', '_KEY'))
        return dict(mode='remote' if url and model else 'disabled', channel='custom', protocol=os.getenv('JOBSCOPE_' + prefix + '_PROTOCOL', 'openai'), url=url, model=model, key=key, local_model='', source='environment' if any((url, model, key)) else 'default')
    result = dict(llm=remote('LLM'), embedding=remote('EMBEDDING'))
    local = os.getenv('JOBSCOPE_EMBEDDING_LOCAL_MODEL', '')
    if local:
        result['embedding'].update(mode='local', local_model=local, source='environment')
    return result


def get_model_config(target='llm'):
    return (_active.get() or environment())[target].copy()


class ModelSettings:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS model_settings(target TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    def effective(self):
        result = environment()
        with self.repo.connection() as conn:
            for row in conn.execute('SELECT target,payload FROM model_settings'):
                saved = json.loads(row[1])
                result[row[0]].update(**dict(channel='custom', protocol='openai') | saved, source='saved')
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
            if old.get('key') and current_url != previous_url:
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

    def test(self, target, value):
        from .model_transport import complete, OutputLimitError
        data = self.prepare(target, value, self.effective())
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

    def discover(self, target, value):
        from .model_transport import endpoint, headers
        data = self.prepare(target, value, self.effective(), require_model=False)
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
