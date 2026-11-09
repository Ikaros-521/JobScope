"""Workspace extensions: explicit MCP connections and instruction-only skills."""
import hashlib
import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import timedelta

import anyio
import httpx
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .chat import validate_model_url

SCRIPT_SUFFIXES = {'.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.ps1', '.bat', '.cmd', '.vbs', '.exe', '.dll', '.bin', '.so', '.dylib'}
RESOURCE_MAX_FILES = 20
RESOURCE_MAX_EACH = 60000
RESOURCE_MAX_TOTAL = 240000
ARCHIVE_MAX_BYTES = 2 * 1024 * 1024
ARCHIVE_MAX_ENTRIES = 60
HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def normalize_skill_path(path):
    """Canonical slash path inside the skill folder; raises on traversal."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError('资源路径为空')
    clean = path.strip().replace('\\', '/')
    if clean.startswith('/') or clean.endswith('/'):
        raise ValueError(f'资源路径 {clean[:80]} 不能以斜杠开头或结尾')
    parts = clean.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise ValueError(f'资源路径 {clean[:80]} 不允许空段、. 或 ..')
    if len(parts) > 5:
        raise ValueError(f'资源路径 {clean[:80]} 层级过深（最多 5 层）')
    if any(len(part) > 120 for part in parts):
        raise ValueError(f'资源路径 {clean[:80]} 段名过长')
    return clean


def parse_skill_markdown(content):
    """Shared SKILL.md parser: YAML frontmatter then body."""
    if not content.startswith('---\n') and not content.startswith('---\r\n'):
        raise ValueError('SKILL.md 须包含 YAML 元信息 name 和 description')
    parts = re.split(r'^---\s*$', content, maxsplit=2, flags=re.M)
    try:
        meta = yaml.safe_load(parts[1])
    except (yaml.YAMLError, RecursionError, IndexError):
        raise ValueError('Skill 元信息格式无效') from None
    if len(parts) < 3 or not isinstance(meta, dict) or not isinstance(meta.get('name'), str) or not isinstance(meta.get('description'), str):
        raise ValueError('SKILL.md 须包含字符串 name 和 description')
    name, description = meta['name'].strip(), meta['description'].strip()
    if not name or len(name) > 80 or not description or len(description) > 1000 or not parts[2].strip():
        raise ValueError('Skill 名称、描述或正文为空或过长')
    optional = {}
    if isinstance(meta.get('category'), str) and meta['category'].strip():
        optional['category'] = meta['category'].strip()[:20]
    if isinstance(meta.get('author'), str) and meta['author'].strip():
        optional['author'] = meta['author'].strip()[:60]
    keywords = meta.get('keywords')
    if isinstance(keywords, list) and keywords:
        words = [str(word).strip()[:20] for word in keywords if str(word).strip()][:8]
        if words:
            optional['keywords'] = words
    if isinstance(meta.get('icon'), str) and meta['icon'].strip():
        optional['icon'] = meta['icon'].strip()[:4]
    return name, description, parts[2].strip(), optional


def skill_entry(name, description, content, optional=None, resources=None, scripts=None):
    skill_id = 'S-' + hashlib.sha256(name.encode()).hexdigest()[:16]
    return dict(id=skill_id, name=name, description=description, content=content, enabled=True,
                resources=list(resources or []), scripts=list(scripts or []), **(optional or {}))


def public_skill(item):
    """Skills never expose raw resource text over the management API."""
    return {k: v for k, v in item.items() if k != 'content' and k != 'resources'} | {
        'resources': [dict(path=r['path'], characters=len(r.get('text', ''))) for r in item.get('resources', [])]}


def tool_alias(server_id, name):
    return 'mcp_' + hashlib.sha256((server_id + ':' + name).encode()).hexdigest()[:24]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MCPInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    transport: Literal['http', 'sse', 'stdio'] = 'http'
    url: str = Field(default='', max_length=2000)
    command: str = Field(default='', max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=40)
    env: dict[str, str] = Field(default_factory=dict, max_length=30)
    headers: dict[str, str] = Field(default_factory=dict, max_length=10)
    token: str = Field(default='', max_length=4000)
    clear_secrets: bool = False


class MCPPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=50)
    auto_tools: list[str] | None = Field(default=None, max_length=50)


class SkillInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=60000)


class SkillFileEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=400)
    text: str | None = Field(default=None, max_length=200000)


class SkillFilesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    files: list[SkillFileEntry] = Field(min_length=1, max_length=ARCHIVE_MAX_ENTRIES)


class OnlineSkillInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repo: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=200)


class SkillResourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=400)


class ToggleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: bool
    approval_id: str = Field(min_length=1, max_length=100)


def validate_schema(schema):
    if not isinstance(schema, dict) or len(json.dumps(schema)) > 16000:
        raise ValueError('工具参数结构无效或过大')
    # Never resolve external schema references supplied by a server.
    def visit(node, depth=0):
        if depth > 30:
            raise ValueError('工具参数结构层级过深')
        if isinstance(node, dict):
            if any(key in node for key in ('$dynamicRef', '$recursiveRef', '$id')):
                raise ValueError('工具参数结构不支持动态引用或 $id 重定位，请使用内联结构或本地 $defs 引用')
            if '$ref' in node and (not isinstance(node['$ref'], str) or not node['$ref'].startswith('#/')):
                raise ValueError('工具参数结构不支持外部引用，请使用内联结构或本地 $defs 引用')
            for value in node.values():
                visit(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                visit(value, depth + 1)
    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError('工具参数结构不符合 JSON Schema') from None
    return schema


def schema_validator(schema):
    # Empty registry has an offline-only resolver; no urllib/network fallback.
    return Draft202012Validator(schema, registry=Registry())


class MCPClient:
    @staticmethod
    def _headers(config):
        merged = {}
        if config.get('token'):
            merged['Authorization'] = 'Bearer ' + config['token']
        merged.update({k: v for k, v in config.get('headers', {}).items()})
        return merged

    @staticmethod
    async def _request(config, operation, name=None, args=None):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client
        with open(os.devnull, 'w') as errlog:
            async with AsyncExitStack() as stack:
                if config['transport'] == 'stdio':
                    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(command=config['command'], args=config['args'], env=config['env']), errlog=errlog))
                else:
                    def factory(headers=None, timeout=None, auth=None):
                        return httpx.AsyncClient(headers=headers, timeout=timeout or 25, auth=auth, trust_env=False, follow_redirects=False)
                    # SSE 与 Streamable HTTP 共用同一组认证/自定义头，token 先设、自定义头可覆盖。
                    headers = MCPClient._headers(config)
                    if config['transport'] == 'sse':
                        streams = await stack.enter_async_context(sse_client(config['url'], headers=headers, timeout=timedelta(seconds=25), sse_read_timeout=timedelta(seconds=25), httpx_client_factory=factory))
                    else:
                        streams = await stack.enter_async_context(streamablehttp_client(config['url'], headers=headers, timeout=timedelta(seconds=25), sse_read_timeout=timedelta(seconds=25), httpx_client_factory=factory))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=25)))
                await session.initialize()
                if operation == 'list':
                    tools, cursor = [], None
                    for _ in range(5):
                        result = await session.list_tools(cursor=cursor)
                        for item in result.tools:
                            if len(tools) >= 50:
                                break
                            schema = validate_schema(item.inputSchema)
                            tools.append(dict(name=item.name[:200], description=(item.description or '')[:1500], inputSchema=schema, annotations=item.annotations.model_dump(exclude_none=True) if item.annotations else {}))
                        if not result.nextCursor or len(tools) >= 50 or result.nextCursor == cursor:
                            break
                        cursor = result.nextCursor
                    return tools
                result = await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=25))
                text = '\n'.join(block.text for block in result.content if getattr(block, 'type', '') == 'text')
                if result.structuredContent:
                    text += '\n' + json.dumps(result.structuredContent, ensure_ascii=False)
                return dict(text=text[:10000] or '工具未返回文本内容', is_error=bool(result.isError), truncated=len(text) > 10000)

    @staticmethod
    async def discover(config):
        try:
            with anyio.fail_after(35):
                return await MCPClient._request(config, 'list')
        except Exception:
                raise ValueError('MCP 连接或工具发现失败。请检查服务是否启动、传输方式（Streamable HTTP / SSE / stdio）、命令/地址和认证。') from None

    @staticmethod
    async def call(config, name, args):
        try:
            with anyio.fail_after(35):
                return await MCPClient._request(config, 'call', name, args)
        except Exception:
            raise ValueError('MCP 工具执行失败或超时；未自动重试，请检查服务状态。') from None


class ExtensionStore:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS agent_mcp(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_skills(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_pending(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_market(payload TEXT NOT NULL,fetched_at REAL NOT NULL);''')
            conn.execute('CREATE TABLE IF NOT EXISTS agent_private_conversations(id TEXT PRIMARY KEY)')

    def mark_private(self, conversation_id):
        with self.repo.connection() as conn:
            conn.execute('INSERT OR IGNORE INTO agent_private_conversations VALUES (?)', (conversation_id,))

    def is_private(self, conversation_id):
        with self.repo.connection() as conn:
            return conn.execute('SELECT 1 FROM agent_private_conversations WHERE id=?', (conversation_id,)).fetchone() is not None

    def _all(self, table):
        with self.repo.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute(f'SELECT payload FROM {table} ORDER BY id')]

    def _put(self, table, item):
        with self.repo.connection() as conn:
            conn.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)', (item['id'], json.dumps(item, ensure_ascii=False)))

    def mcp(self, server_id):
        return next((item for item in self._all('agent_mcp') if item['id'] == server_id), None)

    @staticmethod
    def public_server(item):
        return {**{k: v for k, v in item.items() if k not in {'token', 'env', 'headers'}}, 'token_set': bool(item.get('token')), 'env_keys': sorted(item.get('env', {})), 'headers_set': sorted(item.get('headers', {}))}

    def public(self):
        return dict(servers=[self.public_server(item) for item in self._all('agent_mcp')], skills=[public_skill(item) for item in self._all('agent_skills')])

    def market_cache(self):
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload,fetched_at FROM agent_market ORDER BY fetched_at DESC LIMIT 1').fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def save_market_cache(self, payload):
        with self.repo.connection() as conn:
            conn.execute('DELETE FROM agent_market')
            conn.execute('INSERT INTO agent_market VALUES (?,?)', (json.dumps(payload, ensure_ascii=False), time.time()))

    def save_server(self, data, server_id=None):
        old = self.mcp(server_id) if server_id else None
        if server_id and not old:
            raise ValueError('MCP 连接不存在')
        if not old and len(self._all('agent_mcp')) >= 12:
            raise ValueError('最多保存 12 个 MCP 连接')
        item = data.model_dump(exclude={'clear_secrets'})
        item['name'] = item['name'].strip()
        if not item['name']:
            raise ValueError('请填写连接名称')
        if item['transport'] in ('http', 'sse'):
            item['url'] = validate_model_url(item['url'].strip())
            item['command'] = ''
        elif not item['command'].strip() or any('\x00' in value for value in [item['command'], *item['args']]):
            raise ValueError('请填写可执行命令及独立参数，不支持空命令或空字符')
        if any(len(k) > 200 or len(v) > 4000 for k, v in item['env'].items()):
            raise ValueError('环境变量名称或值过长')
        item['headers'] = {k.strip(): v for k, v in item.get('headers', {}).items() if k.strip()}
        if any(not HEADER_NAME.match(k) for k in item['headers']):
            raise ValueError('自定义请求头名称不符合 HTTP 头格式，请使用英文字母、数字与 - _ 等常规字符')
        if any(len(k) > 200 or len(v) > 2000 for k, v in item['headers'].items()):
            raise ValueError('自定义请求头名称或值过长')
        if item['token'] and any(k.lower() == 'authorization' for k in item['headers']):
            raise ValueError('认证令牌与自定义 Authorization 头重复，请只保留一种认证方式')
        binding = lambda value: (value['transport'], value['url']) if value['transport'] in ('http', 'sse') else (value['transport'], value['command'], value['args'])
        if old and not data.clear_secrets:
            if binding(item) != binding(old) and (old.get('token') or old.get('env') or old.get('headers')):
                raise ValueError('更换 MCP 地址或命令时，请选择清除旧凭证后重新填写')
            item['token'] = item['token'] or old.get('token', '')
            item['env'] = item['env'] or old.get('env', {})
            item['headers'] = item['headers'] or old.get('headers', {})
        item.update(id=server_id or 'M-' + uuid.uuid4().hex[:16], enabled=False, allowed_tools=[], auto_tools=[], tools=[], tested=False)
        self._put('agent_mcp', item)
        return self.public_server(item)

    def patch_server(self, server_id, data):
        item = self.mcp(server_id)
        if not item:
            raise ValueError('MCP 连接不存在')
        names = {tool['name'] for tool in item['tools']}
        changes = data.model_dump(exclude_none=True)
        if changes.get('enabled') and not item['tested']:
            raise ValueError('请先测试连接并发现工具')
        if any(set(changes.get(key, [])) - names for key in ('allowed_tools', 'auto_tools')):
            raise ValueError('只能启用本次测试实际发现的工具')
        item.update(changes)
        if set(item['auto_tools']) - set(item['allowed_tools']):
            raise ValueError('自动执行工具必须同时被启用')
        self._put('agent_mcp', item)
        return self.public_server(item)

    async def test_server(self, server_id):
        item = self.mcp(server_id)
        if not item:
            raise ValueError('MCP 连接不存在')
        tools = await MCPClient.discover(item)
        # Testing updates the catalogue, but never grants execution permission.
        item.update(tools=tools, tested=True, enabled=False, allowed_tools=[], auto_tools=[])
        self._put('agent_mcp', item)
        return self.public_server(item)

    def _store_skill(self, item):
        skills = self._all('agent_skills')
        if len(skills) >= 30 and not any(existing['id'] == item['id'] for existing in skills):
            raise ValueError('最多保存 30 个 Skills')
        self._put('agent_skills', item)
        return public_skill(item)

    def save_skill(self, content):
        name, description, body, optional = parse_skill_markdown(content)
        return self._store_skill(skill_entry(name, description, body, optional))

    def _classify_entries(self, skill_md, entries):
        """entries: [(path, text|None)]; None text means script/binary — recorded, never stored."""
        root = skill_md[:-len('SKILL.md')]
        resources, scripts = [], []
        for path, text in entries:
            relative = path[len(root):] if root and path.startswith(root) else path
            if relative == 'SKILL.md':
                continue
            if relative.split('/')[-1].rsplit('.', 1)[-1].lower() in {suffix[1:] for suffix in SCRIPT_SUFFIXES} or text is None:
                if len(scripts) >= RESOURCE_MAX_FILES:
                    continue
                scripts.append(relative)
                continue
            if len(resources) >= RESOURCE_MAX_FILES:
                raise ValueError(f'文本资源最多 {RESOURCE_MAX_FILES} 个文件，请精简 {relative}')
            if len(text) > RESOURCE_MAX_EACH:
                raise ValueError(f'资源 {relative} 超过 {RESOURCE_MAX_EACH} 字符')
            resources.append(dict(path=relative, text=text))
        if sum(len(r['text']) for r in resources) > RESOURCE_MAX_TOTAL:
            raise ValueError(f'文本资源总量超过 {RESOURCE_MAX_TOTAL} 字符')
        return resources, scripts

    def save_skill_files(self, files):
        if not files or len(files) > ARCHIVE_MAX_ENTRIES:
            raise ValueError(f'Skill 文件数量需在 1–{ARCHIVE_MAX_ENTRIES} 之间')
        entries = []
        for row in files:
            path = normalize_skill_path(row.get('path'))
            text = row.get('text')
            entries.append((path, text if isinstance(text, str) else None))
        skill_md = next((path for path, _ in entries if path == 'SKILL.md'), None) \
            or next((path for path, _ in entries if path.endswith('/SKILL.md') and path.count('/') == 1), None)
        if not skill_md:
            raise ValueError('多文件 Skill 必须包含 SKILL.md（根目录或单层子目录内）')
        content = next((text for path, text in entries if path == skill_md), None)
        if not isinstance(content, str):
            raise ValueError('SKILL.md 无法按文本读取，请确认编码为 UTF-8')
        name, description, body, optional = parse_skill_markdown(content)
        resources, scripts = self._classify_entries(skill_md, entries)
        return self._store_skill(skill_entry(name, description, body, optional, resources, scripts))

    def save_skill_archive(self, raw: bytes):
        if len(raw) > ARCHIVE_MAX_BYTES:
            raise ValueError('zip 包超过 2 MB，请精简后再导入')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                infos = [info for info in bundle.infolist() if not info.filename.endswith('/')]
                if len(infos) > ARCHIVE_MAX_ENTRIES:
                    raise ValueError(f'Skill 压缩包最多包含 {ARCHIVE_MAX_ENTRIES} 个文件')
                files = []
                for info in infos:
                    data = bundle.read(info)
                    if b'\x00' in data:
                        text = None
                    else:
                        try:
                            text = data.decode('utf-8')
                        except UnicodeDecodeError:
                            text = None
                    files.append(dict(path=info.filename, text=text))
        except zipfile.BadZipFile:
            raise ValueError('无法读取 zip 文件，请确认是有效的 Skill 压缩包') from None
        return self.save_skill_files(files)

    def export_skill(self, skill_id):
        item = next((s for s in self._all('agent_skills') if s['id'] == skill_id), None)
        if not item:
            raise ValueError('Skill 不存在')
        meta = {key: item[key] for key in ('name', 'description', 'category', 'author', 'keywords', 'icon') if item.get(key)}
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        document = f'---\n{front}\n---\n\n{item["content"]}\n'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('SKILL.md', document)
            for resource in item.get('resources', []):
                bundle.writestr(resource['path'], resource['text'])
            bundle.writestr('jobscope-skill.json', json.dumps(dict(name=item['name'], scripts=item.get('scripts', []), note='脚本文件仅登记路径，不包含内容，也不会被执行'), ensure_ascii=False, indent=2))
        return buffer.getvalue(), item['name']

    def toggle_skill(self, skill_id, enabled):
        skill = next((item for item in self._all('agent_skills') if item['id'] == skill_id), None)
        if not skill:
            raise ValueError('Skill 不存在')
        skill['enabled'] = enabled
        self._put('agent_skills', skill)
        return public_skill(skill)

    def read_resource(self, skill_id, path):
        item = next((s for s in self._all('agent_skills') if s['id'] == skill_id), None)
        if not item:
            raise ValueError('Skill 不存在')
        resource = next((r for r in item.get('resources', []) if r['path'] == path), None)
        if not resource:
            raise ValueError('资源不存在或不是文本资源')
        return dict(path=resource['path'], text=resource['text'], characters=len(resource['text']))

    def delete(self, kind, item_id):
        table = {'mcp': 'agent_mcp', 'skills': 'agent_skills'}[kind]
        with self.repo.connection() as conn:
            conn.execute(f'DELETE FROM {table} WHERE id=?', (item_id,))

    def pending(self, run_id, payload):
        with self.repo.connection() as conn:
            conn.execute("DELETE FROM agent_pending WHERE created<?", (time.time() - 3600,))
            conn.execute('INSERT OR REPLACE INTO agent_pending VALUES (?,?,?,?)', (run_id, 'waiting', json.dumps(payload, ensure_ascii=False), time.time()))

    def claim(self, run_id, approval_id):
        with self.repo.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT state,payload,created FROM agent_pending WHERE run_id=?', (run_id,)).fetchone()
            if not row or row['state'] != 'waiting' or row['created'] < time.time() - 3600:
                raise ValueError('待确认调用不存在、已处理或已过期，请重新提问')
            payload = json.loads(row['payload'])
            if payload['run']['pending']['approval_id'] != approval_id:
                raise ValueError('此确认已失效，请确认当前显示的具体工具调用')
            conn.execute("UPDATE agent_pending SET state='claimed' WHERE run_id=?", (run_id,))
            return payload

    def finish_pending(self, run_id):
        with self.repo.connection() as conn:
            conn.execute('DELETE FROM agent_pending WHERE run_id=?', (run_id,))
