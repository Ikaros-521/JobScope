"""Shared OpenAI Chat Completions / Anthropic Messages transport."""
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# 上游嫌 max_tokens 超限的 400 里，真实上限写在响应体措辞中（2026-11-21 复盘 400：渠道填 256000、
# Z.ai 真实上限 131072，报错原因全在 body 里而旧日志不带 body，排障卡壳）。
_CAP_PATTERNS = (
    re.compile(r'at most (\d+)'),                                # OpenAI 系：supports at most 16384 completion tokens
    re.compile(r'限制数值范围\[1,\s*(\d+)\]'),                    # Z.ai 系：限制数值范围[1,131072]
    re.compile(r'max(?:imum)?[ _-]?(?:output|completion)?[ _-]?tokens?[^\d]{0,40}(\d{3,})', re.I),
)
# complete() 钳制重试时记下的实测输出上限：(端点, 模型) -> 上限；渠道测试据此回写渠道配置。
learned_caps = {}


def parse_max_tokens_cap(message, requested=None):
    """从上游错误体解析 max_tokens 数值上限；识别不出、或命中的只是我们刚发值的回显时返回 None。"""
    if not message:
        return None
    for pattern in _CAP_PATTERNS:
        for match in pattern.finditer(message):
            value = int(match.group(1))
            if value == requested or not 256 <= value <= 2_000_000:
                continue
            return value
    return None


def remember_cap(config, cap):
    key = (endpoint(config['url'], config.get('protocol', 'openai')), str(config.get('model', '')))
    learned_caps[key] = min(cap, learned_caps.get(key, cap))


def learned_cap(config):
    return learned_caps.get((endpoint(config['url'], config.get('protocol', 'openai')), str(config.get('model', ''))))


def _status_detail(exc):
    """HTTPStatusError 的响应体（截断）：400/429 的真实原因都在 body 里，日志必须带出来。"""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return (exc.response.text or '')[:600]
        except Exception:
            return ''
    return ''


def _log_call(config, purpose, max_tokens):
    """DEBUG 明细：模型/协议/端点/参数。凭证（key）永不进日志。"""
    logger.debug('模型调用[%s] model=%s protocol=%s endpoint=%s max_tokens=%s timeout=%s',
                 purpose, config.get('model'), config.get('protocol', 'openai'),
                 endpoint(config['url'], config.get('protocol', 'openai')), max_tokens)


def output_budget(config, default):
    """单次调用输出上限：渠道级用户自定义（max_output_tokens）优先，未设用调用点语义默认。

    现代模型上下文普遍 1M、输出可达 64K-128K，硬编码小上限会截断长产出（JSON 断半路）。
    用户在设置中心渠道上按所配模型的真实输出上限填写即可全局生效。
    """
    value = config.get('max_output_tokens')
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _log_usage(purpose, usage):
    """每次模型调用的 token 用量（INFO：输出额度排障要看得见）。兼容 OpenAI/Anthropic 字段名。"""
    if not usage:
        return
    prompt = usage.get('prompt_tokens', usage.get('input_tokens'))
    completion = usage.get('completion_tokens', usage.get('output_tokens'))
    total = usage.get('total_tokens')
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    logger.info('模型调用[%s] tokens 输入=%s 输出=%s 总计=%s', purpose,
                prompt if prompt is not None else '?',
                completion if completion is not None else '?',
                total if total is not None else '?')


class OutputLimitError(ValueError):
    """Provider responded, but exhausted its output budget before final text."""


class TransportStatusError(ValueError):
    """上游返回 HTTP 错误状态：带状态码与响应体（截断），供 Agent 循环按型分策。

    全部为 ValueError 子类——现有 `except ValueError` 调用方（分析管线、旧问答等）
    行为完全不变；只有显式 import 这些子类的调用方（general_agent 错误恢复）会分流。
    """

    def __init__(self, message, *, status=None, body='', retry_after=None):
        super().__init__(message)
        self.status = status
        self.body = body
        # Retry-After 响应头（秒）；上游给出时 429/529 退避取它与指数退避的较大值。
        self.retry_after = retry_after


class RateLimitError(TransportStatusError):
    """429 限流：指数退避后有界重试通常可恢复。"""


class OverloadedError(TransportStatusError):
    """529/overloaded 过载：退避重试，连续失败切备用渠道（对话 Agent 的 fallback 语义）。"""


class ContextOverflowError(TransportStatusError):
    """400 且响应体命中上下文超长词表：本地字符估算漏了真实 token 占用，先压缩再重试。"""


# 上下文超长的 body 词表：OpenAI「context length」、Anthropic「prompt is too long /
# too many tokens」、国内网关的中文措辞都收进来；匹配在 lower() 之后做，中文原样命中。
_CONTEXT_OVERFLOW_PATTERNS = (
    'context length', 'context_length_exceeded', 'prompt is too long', 'too many tokens',
    'maximum context', '上下文长度', '上下文过长', '超出上下文', '超过上下文',
)


def classify_transport_error(exc, message):
    """把 httpx.HTTPStatusError 归类为带元数据的 TransportStatusError 子类。

    状态码优先、body 词表兜底；识别不出返回 None（调用方维持通用 ValueError 文案）。
    message 沿用调用点现有文案，保证日志与最终失败提示不变。
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    status = exc.response.status_code
    body = _status_detail(exc)
    lower = body.lower()
    retry_after = None
    try:
        raw = exc.response.headers.get('retry-after')
        retry_after = float(raw) if raw and float(raw) > 0 else None
    except (TypeError, ValueError):
        retry_after = None
    if status == 429:
        return RateLimitError(message, status=status, body=body, retry_after=retry_after)
    if status == 529 or 'overloaded' in lower:
        return OverloadedError(message, status=status, body=body, retry_after=retry_after)
    if status == 400 and any(pattern in lower for pattern in _CONTEXT_OVERFLOW_PATTERNS):
        return ContextOverflowError(message, status=status, body=body)
    return TransportStatusError(message, status=status, body=body, retry_after=retry_after)


def endpoint(url, protocol='openai', purpose='llm'):
    from .chat import validate_model_url
    parsed = urlsplit(validate_model_url(url.strip()))
    path = parsed.path.rstrip('/')
    explicit_endpoint = False
    for suffix in ('/chat/completions', '/messages', '/embeddings', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            explicit_endpoint = True
            break
    if not path and not explicit_endpoint:
        path = '/v1'
    suffix = '/models' if purpose == 'models' else '/embeddings' if purpose == 'embedding' else '/messages' if protocol == 'anthropic' else '/chat/completions'
    return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, parsed.query, parsed.fragment))


def headers(config):
    result = {'Content-Type': 'application/json'}
    if config.get('protocol') == 'anthropic':
        result['anthropic-version'] = '2023-06-01'
        if config.get('key'):
            result['x-api-key'] = config['key']
    elif config.get('key'):
        result['Authorization'] = 'Bearer ' + config['key']
    return result


def stream_complete(config, system, user, *, max_tokens=1800, temperature=None, timeout=45, on_delta=None):
    """Streaming complete(): text deltas reach on_delta as they arrive.

    Returns (content, usage) with the same semantics as complete(); the returned
    content is the full text and remains the only input to downstream validation.
    """
    from .chat import validate_model_url
    import json
    protocol = config.get('protocol', 'openai')
    body = dict(model=config['model'], messages=[dict(role='user', content=user)], max_tokens=max_tokens, stream=True)
    if protocol == 'anthropic':
        body['system'] = system
    elif system:
        body['messages'].insert(0, dict(role='system', content=system))
    if temperature is not None:
        body['temperature'] = temperature
    text, usage = '', {}
    finish_reason = ''
    _log_call(config, 'complete', max_tokens)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # 与 agent_stream 同理：先 read() 再取 body，日志才能带出 4xx 响应体。
                    try:
                        exc.response.read()
                    except Exception:
                        pass
                    logger.error('模型调用失败[stream_complete] model=%s status=%s 响应体=%s',
                                 config.get('model'), exc.response.status_code, _status_detail(exc))
                    raise ValueError('模型请求失败，请检查服务配置后重试') from None
                for payload in _sse_payloads(response):
                    data = json.loads(payload)
                    if protocol == 'anthropic':
                        usage = data.get('usage', usage) or usage
                        if data.get('type') == 'message_delta':
                            finish_reason = data.get('delta', {}).get('stop_reason') or finish_reason
                        if data.get('type') == 'content_block_delta':
                            delta = data.get('delta') or {}
                            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                                text += delta['text']
                                if on_delta:
                                    on_delta(delta['text'])
                    else:
                        if data.get('usage'):
                            usage = data['usage']
                        choices = data.get('choices') or []
                        if not choices:
                            continue
                        finish_reason = choices[0].get('finish_reason') or finish_reason
                        delta = choices[0].get('delta') or {}
                        if isinstance(delta.get('content'), str):
                            text += delta['content']
                            if on_delta:
                                on_delta(delta['content'])
    except httpx.HTTPStatusError as exc:
        logger.error('模型调用失败[stream_complete] model=%s status=%s 响应体=%s',
                     config.get('model'), exc.response.status_code, _status_detail(exc))
        raise ValueError('模型请求失败，请检查服务配置后重试') from None
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError, TypeError, IndexError):
        logger.exception('模型调用失败[complete] model=%s', config.get('model'))
        raise ValueError('模型请求失败，请检查服务配置后重试') from None
    if not isinstance(text, str) or not text.strip():
        truncated = finish_reason in ('max_tokens', 'length')
        if truncated:
            logger.warning('模型输出触达单次上限[stream_complete] max_tokens=%s finish_reason=%s usage=%s',
                           max_tokens, finish_reason, usage)
            raise OutputLimitError('模型输出触达单次上限（max_tokens），未生成完整回答；与账户余额无关，可重试一次。')
        raise ValueError('模型返回格式无效')
    _log_usage('complete', usage)
    return text, usage


def complete(config, system, user, *, max_tokens=1800, temperature=None, timeout=600):
    from .chat import validate_model_url
    _log_call(config, 'complete', max_tokens)
    protocol = config.get('protocol', 'openai')
    body = dict(model=config['model'], messages=[dict(role='user', content=user)], max_tokens=max_tokens)
    if protocol == 'anthropic':
        body['system'] = system
    elif system:
        body['messages'].insert(0, dict(role='system', content=system))
    if temperature is not None:
        body['temperature'] = temperature
    budget = max_tokens
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            for attempt in (1, 2):
                body['max_tokens'] = budget
                response = client.post(validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body)
                if response.status_code >= 400:
                    # 400 常带着「max_tokens 超限」实锤：按它说的上限钳制重试一次，别整条调用失败。
                    detail = (getattr(response, 'text', '') or '')[:600]
                    cap = parse_max_tokens_cap(detail, requested=budget)
                    if attempt == 1 and response.status_code == 400 and cap and cap < budget:
                        remember_cap(config, cap)
                        logger.warning('上游拒绝 max_tokens=%s，按其上限 %s 钳制重试[model=%s]：%s',
                                       max_tokens, cap, config.get('model'), detail)
                        budget = cap
                        continue
                    # 思考模型把预算全花在推理上、正文为空，被上游包成 5xx（Z.ai "empty reply …
                    # whole output spent on reasoning"，2026-11-21 抽取管线实测）：同参数重试必然再炸，
                    # 提升预算重试一次；真实网关故障的 5xx 不带这个特征，照旧快速失败。
                    if attempt == 1 and response.status_code >= 500 and 'empty reply' in detail.lower():
                        lifted = min(max(budget * 4, 8192), output_budget(config, 32768))
                        if lifted > budget:
                            logger.warning('上游 5xx 空回复（思考耗尽预算），max_tokens %s→%s 重试一次[model=%s]：%s',
                                           budget, lifted, config.get('model'), detail)
                            budget = lifted
                            continue
                response.raise_for_status()
                if len(response.content) > 2000000:
                    raise ValueError('模型响应超过上限')
                data = response.json()
                break
    except (httpx.HTTPError, ValueError) as exc:
        detail = _status_detail(exc)
        logger.exception('模型调用失败[complete] model=%s%s', config.get('model'),
                         f' status={exc.response.status_code} 响应体={detail}' if detail else '')
        raise
    try:
        if protocol == 'anthropic':
            blocks = data['content']
            if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
                raise ValueError('模型返回结构无效')
            content = ''.join(block['text'] for block in blocks if block.get('type') == 'text' and isinstance(block.get('text'), str))
        else:
            content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise ValueError('模型返回结构无效') from None
    truncated = data.get('stop_reason') == 'max_tokens' if protocol == 'anthropic' else (data.get('choices') or [{}])[0].get('finish_reason') in ('length', 'max_tokens')
    if truncated:
        # 截断一律可见：非空半截内容（如断在半路的 JSON）也打 WARNING——上层重试/软校验兜底，
        # 但排障时必须一眼看到「输出=上限」的实锤（2026-11-21 线上就是静默半截 JSON）。
        logger.warning('模型输出触达单次上限[complete] max_tokens=%s usage=%s（内容非空属截断半成品）', max_tokens, data.get('usage'))
    if not isinstance(content, str) or not content.strip():
        if truncated:
            raise OutputLimitError('模型输出触达单次上限（max_tokens），未生成完整回答；与账户余额无关，可重试一次。')
        raise ValueError('模型返回格式无效')
    _log_usage('complete', data.get('usage') or {})
    return content, data.get('usage', {})


def tool_turn(config, messages, tools, *, timeout=60, max_tokens=None):
    """Canonical OpenAI messages in/out; convert native Anthropic tool blocks.

    max_tokens=None 用渠道预算默认（output_budget(config, 8000)）；Agent 循环在
    OutputLimitError 后传升级值重试（输出截断升级）。
    """
    from .chat import validate_model_url
    _log_call(config, 'agent_turn', 4096)
    import json
    protocol = config.get('protocol', 'openai')
    body = _turn_body(config, messages, tools, stream=False, max_tokens=max_tokens)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = client.post(validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body)
            response.raise_for_status()
            if len(response.content) > 2000000:
                raise ValueError('响应超过上限')
            data = response.json()
        calls, text = [], ''
        if protocol == 'anthropic':
            for block in data['content']:
                if block['type'] == 'text':
                    text += block['text']
                elif block['type'] == 'tool_use':
                    calls.append(dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=json.dumps(block['input'], ensure_ascii=False))))
        else:
            message = data['choices'][0]['message']
            text = message.get('content') or ''
            calls = message.get('tool_calls') or []
        truncated = ((data.get('stop_reason') == 'max_tokens') if protocol == 'anthropic'
                     else (data.get('choices') or [{}])[0].get('finish_reason') in ('length', 'max_tokens'))
        assistant, usage = _validate_turn(calls, text, data.get('usage', {}), truncated=truncated)
        _log_usage('agent_turn', usage)
        return assistant, usage
    except httpx.HTTPStatusError as exc:
        logger.error('模型调用失败[agent_turn] model=%s status=%s 响应体=%s',
                     config.get('model'), exc.response.status_code, _status_detail(exc))
        message = 'Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。'
        raise (classify_transport_error(exc, message) or ValueError(message)) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        logger.exception('模型调用失败[agent_turn] model=%s', config.get('model'))
        raise ValueError('Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。') from None


def _turn_body(config, messages, tools, *, stream, max_tokens=None):
    """Shared request body for tool turns. OpenAI messages in/out; Anthropic gets native blocks."""
    import json
    protocol = config.get('protocol', 'openai')
    budget = output_budget(config, 8000) if max_tokens is None else max_tokens
    body = dict(model=config['model'], max_tokens=budget, stream=bool(stream))
    if protocol == 'anthropic':
        body['system'] = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        converted = []
        for message in messages:
            role = message['role']
            if role == 'system':
                continue
            if role == 'tool':
                role, blocks = 'user', [dict(type='tool_result', tool_use_id=message['tool_call_id'], content=message['content'])]
            elif role == 'assistant':
                blocks = [dict(type='text', text=message['content'])] if message.get('content') else []
                blocks += [dict(type='tool_use', id=call['id'], name=call['function']['name'], input=json.loads(call['function']['arguments'])) for call in message.get('tool_calls', [])]
            else:
                blocks = [dict(type='text', text=message['content'])]
            if converted and converted[-1]['role'] == role:
                converted[-1]['content'].extend(blocks)
            else:
                converted.append(dict(role=role, content=blocks))
        body.update(messages=converted, tools=[dict(name=t['name'], description=t['description'], input_schema=t['parameters']) for t in tools])
    else:
        body.update(messages=messages, tools=[dict(type='function', function=t) for t in tools])
    return body


def _validate_turn(calls, text, usage, truncated=False):
    """解析层只做格式与 schema 校验（ZCode 对齐）：数量不在这里截断——合法 fan-out 无上限，
    失控检测由 Agent 循环的签名熔断（同名同参连续重复）负责，硬停止只留给资源与安全边界。
    truncated=True 且没有工具调用（正文可能半截或全空）：预算被思考/长输出耗尽的实锤，
    抛 OutputLimitError 让 Agent 循环升级预算重试——与 complete() 的截断语义对齐；
    Agent 侧已流出半截时由 on_restart 重置实时气泡再重打，不会拼接两段。
    正文非空但带工具调用的截断走参数校验的自然失败路径，不在这里掺和。"""
    import json
    if not isinstance(text, str) or not isinstance(calls, list):
        raise ValueError('响应格式无效')
    if truncated and not calls:
        raise OutputLimitError('模型输出触达单次上限（max_tokens），未生成完整回答；与账户余额无关，可重试一次。')
    for call in calls:
        if not isinstance(call['id'], str) or len(call['id']) > 200 or not isinstance(call['function']['name'], str):
            raise ValueError('工具调用结构无效')
        args = call['function']['arguments']
        if not isinstance(args, str) or len(args) > 12000 or not isinstance(json.loads(args), dict):
            raise ValueError('工具参数无效或过大')
    if len({c['id'] for c in calls}) != len(calls) or (not calls and not text.strip()):
        raise ValueError('模型没有返回有效回答或工具调用')
    return dict(role='assistant', content=text[:20000] or None, **({'tool_calls': calls} if calls else {})), usage


def _sse_payloads(response):
    """Yield JSON payloads from an SSE body; stops at the [DONE] sentinel."""
    for line in response.iter_lines():
        if not isinstance(line, str):
            line = line.decode('utf-8', 'replace')
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            return
        if payload:
            yield payload


def stream_turn(config, messages, tools, *, on_delta=None, on_reasoning=None, should_stop=None, timeout=60, register_abort=None, max_tokens=None):
    """Streaming tool_turn: content and reasoning deltas go to the callbacks as they arrive.

    Returns (assistant_message, usage, reasoning) with the same message shape as tool_turn.
    should_stop is polled between chunks: when the user stops the run the stream is
    abandoned immediately; the partial turn either validates truncated or raises,
    and the caller discards the outcome either way.
    register_abort(fn) -> unregister lets the caller close this response from the
    cancelling thread, so a stop also interrupts the wait for the *next* chunk
    (thinking models can sit silent between chunks for tens of seconds).
    """
    from .chat import validate_model_url
    import json
    protocol = config.get('protocol', 'openai')
    body = _turn_body(config, messages, tools, stream=True, max_tokens=max_tokens)
    _log_call(config, 'agent_stream', 4096)
    text, reasoning, usage = '', '', {}
    finish_reason = ''
    calls, openai_calls = [], {}
    anthropic_tools = {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # 流式响应的 body 出了 with 就读不到，且 .text 在未 read() 时抛 ResponseNotRead
                    # （_status_detail 吞成空串，分类词表与排障日志双双失明）——先显式读进来。
                    try:
                        exc.response.read()
                    except Exception:
                        pass
                    logger.error('模型调用失败[agent_stream] model=%s status=%s 响应体=%s',
                                 config.get('model'), exc.response.status_code, _status_detail(exc))
                    message = 'Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。'
                    raise (classify_transport_error(exc, message) or ValueError(message)) from None
                unregister = register_abort(response.close) if register_abort else None
                try:
                    for payload in _sse_payloads(response):
                        if should_stop is not None and should_stop():
                            break
                        data = json.loads(payload)
                        if protocol == 'anthropic':
                            usage = data.get('usage', usage) or usage
                            kind = data.get('type')
                            if kind == 'content_block_start':
                                block = data.get('content_block') or {}
                                if block.get('type') == 'tool_use':
                                    anthropic_tools[data.get('index')] = dict(id=block.get('id'), name=block.get('name'), arguments='')
                            elif kind == 'content_block_delta':
                                delta = data.get('delta') or {}
                                if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                                    text += delta['text']
                                    if on_delta:
                                        on_delta(delta['text'])
                                elif delta.get('type') == 'thinking_delta' and isinstance(delta.get('thinking'), str):
                                    reasoning += delta['thinking']
                                    if on_reasoning:
                                        on_reasoning(delta['thinking'])
                                elif delta.get('type') == 'input_json_delta' and isinstance(delta.get('partial_json'), str):
                                    pending = anthropic_tools.get(data.get('index'))
                                    if pending:
                                        pending['arguments'] += delta['partial_json']
                            elif kind == 'message_delta':
                                finish_reason = data.get('delta', {}).get('stop_reason') or finish_reason
                                usage.update(data.get('usage') or {})
                        else:
                            choices = data.get('choices') or []
                            if data.get('usage'):
                                usage = data['usage']
                            if not choices:
                                continue
                            finish_reason = choices[0].get('finish_reason') or finish_reason
                            delta = choices[0].get('delta') or {}
                            if isinstance(delta.get('content'), str):
                                text += delta['content']
                                if on_delta:
                                    on_delta(delta['content'])
                            for key in ('reasoning_content', 'reasoning'):
                                if isinstance(delta.get(key), str):
                                    reasoning += delta[key]
                                    if on_reasoning:
                                        on_reasoning(delta[key])
                            for item in delta.get('tool_calls') or []:
                                index = item.get('index', 0)
                                slot = openai_calls.setdefault(index, dict(id='', type='function', function=dict(name='', arguments='')))
                                if isinstance(item.get('id'), str):
                                    slot['id'] = item['id']
                                function = item.get('function') or {}
                                if isinstance(function.get('name'), str):
                                    slot['function']['name'] += function['name']
                                if isinstance(function.get('arguments'), str):
                                    slot['function']['arguments'] += function['arguments']
                finally:
                    if unregister:
                        unregister()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, IndexError):
        # 不含裸 ValueError：with 内已把 HTTP 状态错误分类成 TransportStatusError 子类再抛出，
        # 这里再包一层会把类型信息抹掉（429/529/超长的分策就失效了）。分类异常按原样穿透。
        logger.exception('模型调用失败[agent_stream] model=%s', config.get('model'))
        raise ValueError('Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。') from None
    if protocol == 'anthropic':
        for slot in anthropic_tools.values():
            calls.append(dict(id=slot['id'], type='function', function=dict(name=slot['name'], arguments=slot['arguments'])))
    else:
        calls = [openai_calls[index] for index in sorted(openai_calls)]
    assistant, usage = _validate_turn(calls, text, usage, truncated=finish_reason in ('length', 'max_tokens'))
    _log_usage('agent_stream', usage)
    return assistant, usage, reasoning
