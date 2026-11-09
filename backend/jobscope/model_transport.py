"""Shared OpenAI Chat Completions / Anthropic Messages transport."""
from urllib.parse import urlsplit, urlunsplit

import httpx


class OutputLimitError(ValueError):
    """Provider responded, but exhausted its output budget before final text."""


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


def complete(config, system, user, *, max_tokens=1800, temperature=None, timeout=45):
    from .chat import validate_model_url
    protocol = config.get('protocol', 'openai')
    body = dict(model=config['model'], messages=[dict(role='user', content=user)], max_tokens=max_tokens)
    if protocol == 'anthropic':
        body['system'] = system
    elif system:
        body['messages'].insert(0, dict(role='system', content=system))
    if temperature is not None:
        body['temperature'] = temperature
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = client.post(validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body)
        response.raise_for_status()
        if len(response.content) > 2000000:
            raise ValueError('模型响应超过上限')
        data = response.json()
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
    if not isinstance(content, str) or not content.strip():
        truncated = data.get('stop_reason') == 'max_tokens' if protocol == 'anthropic' else data['choices'][0].get('finish_reason') == 'length'
        if truncated:
            raise OutputLimitError('模型已响应，但输出额度耗尽，尚未生成最终答案')
        raise ValueError('模型返回格式无效')
    return content, data.get('usage', {})


def tool_turn(config, messages, tools, *, timeout=60):
    """Canonical OpenAI messages in/out; convert native Anthropic tool blocks."""
    from .chat import validate_model_url
    import json
    protocol = config.get('protocol', 'openai')
    body = dict(model=config['model'], max_tokens=4096)
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
        if not isinstance(text, str) or not isinstance(calls, list) or len(calls) > 4:
            raise ValueError('响应格式无效或工具调用过多')
        for call in calls:
            if not isinstance(call['id'], str) or len(call['id']) > 200 or not isinstance(call['function']['name'], str):
                raise ValueError('工具调用结构无效')
            args = call['function']['arguments']
            if not isinstance(args, str) or len(args) > 12000 or not isinstance(json.loads(args), dict):
                raise ValueError('工具参数无效或过大')
        if len({c['id'] for c in calls}) != len(calls) or (not calls and not text.strip()):
            raise ValueError('模型没有返回有效回答或工具调用')
        return dict(role='assistant', content=text[:20000] or None, **({'tool_calls': calls} if calls else {})), data.get('usage', {})
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        raise ValueError('Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。') from None


def _turn_body(config, messages, tools, *, stream):
    """Shared request body for tool turns. OpenAI messages in/out; Anthropic gets native blocks."""
    import json
    protocol = config.get('protocol', 'openai')
    body = dict(model=config['model'], max_tokens=4096, stream=bool(stream))
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


def _validate_turn(calls, text, usage):
    import json
    if not isinstance(text, str) or not isinstance(calls, list) or len(calls) > 4:
        raise ValueError('响应格式无效或工具调用过多')
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


def stream_turn(config, messages, tools, *, on_delta=None, on_reasoning=None, timeout=60):
    """Streaming tool_turn: content and reasoning deltas go to the callbacks as they arrive.

    Returns (assistant_message, usage, reasoning) with the same message shape as tool_turn.
    """
    from .chat import validate_model_url
    import json
    protocol = config.get('protocol', 'openai')
    body = _turn_body(config, messages, tools, stream=True)
    text, reasoning, usage = '', '', {}
    calls, openai_calls = [], {}
    anthropic_tools = {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', validate_model_url(endpoint(config['url'], protocol)), headers=headers(config), json=body) as response:
                response.raise_for_status()
                for payload in _sse_payloads(response):
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
                            usage.update(data.get('usage') or {})
                    else:
                        choices = data.get('choices') or []
                        if data.get('usage'):
                            usage = data['usage']
                        if not choices:
                            continue
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
                            slot = openai_calls.setdefault(index, dict(id='', function=dict(name='', arguments='')))
                            if isinstance(item.get('id'), str):
                                slot['id'] = item['id']
                            function = item.get('function') or {}
                            if isinstance(function.get('name'), str):
                                slot['function']['name'] += function['name']
                            if isinstance(function.get('arguments'), str):
                                slot['function']['arguments'] += function['arguments']
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError, TypeError, IndexError):
        raise ValueError('Agent 模型调用失败或返回格式无效；请确认所选模型支持工具调用及当前协议。') from None
    if protocol == 'anthropic':
        for slot in anthropic_tools.values():
            calls.append(dict(id=slot['id'], type='function', function=dict(name=slot['name'], arguments=slot['arguments'])))
    else:
        calls = [openai_calls[index] for index in sorted(openai_calls)]
    assistant, usage = _validate_turn(calls, text, usage)
    return assistant, usage, reasoning
