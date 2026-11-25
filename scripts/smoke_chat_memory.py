"""对话记忆（2026-11-25，s09 借鉴）的 8766 隔离实例全链路冒烟。

自起 8766 隔离实例（进程内 uvicorn + 独立临时数据目录，绝不碰生产 .data）+ 本地假模型服务器，
走真实 HTTP：对话完成后自动提取记忆 → 新会话 system prompt 注入 → 停用/删除即时生效 →
开关关闭后不再调提取模型。

用法：backend/.venv/Scripts/python.exe scripts/smoke_chat_memory.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

# 必须「先设环境变量再 import jobscope.main」：main.py 在 import 时就会执行模块级
# create_app()（conftest 同款规避）——不预设会打开生产 .data 并跑建表迁移。
os.environ['JOBSCOPE_DATA_DIR'] = str(Path(tempfile.mkdtemp(prefix='jobscope-smoke-memory-import-')).resolve())

import httpx
import uvicorn

from jobscope.main import create_app

BASE = 'http://127.0.0.1:8766'
TIMEOUT = httpx.Timeout(60)


class ModelServer(ThreadingHTTPServer):
    """假模型：stream 请求回 SSE 回答，非 stream（complete=记忆提取/整理）回 JSON 数组文本。
    requests 记录全部请求体，供断言注入与提取调用次数。"""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), ScriptedHandler)
        self.daemon_threads = True
        self.requests = []
        self.extraction_reply = '[]'

    def serve(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        server.requests.append(body)
        self.send_response(200)
        if body.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            answer = '好的，已记录你的方向并继续筛选岗位。'
            for piece in (answer[:len(answer) // 2], answer[len(answer) // 2:]):
                self.wfile.write(b'data: ' + json.dumps({'choices': [dict(delta=dict(content=piece))]}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')
            return
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(dict(
            choices=[dict(message=dict(role='assistant', content=server.extraction_reply))])).encode())
        self.wfile.write(b'\n')


def wait_server(client, deadline=30):
    end = time.time() + deadline
    while time.time() < end:
        try:
            if client.get(BASE + '/api/settings/system', timeout=TIMEOUT).status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
        time.sleep(0.5)
    raise SystemExit('8766 实例启动超时')


def run_chat(client, message):
    run = client.post(BASE + '/api/chat/runs', json={'message': message, 'agent_mode': 'general'}, timeout=TIMEOUT).json()
    deadline = time.time() + 60
    while time.time() < deadline:
        status = client.get(f"{BASE}/api/chat/runs/{run['run_id']}", timeout=TIMEOUT).json()
        if status.get('done'):
            break
        time.sleep(0.1)
    return run


def last_agent_request(model_server):
    for body in reversed(model_server.requests):
        if body.get('stream') and body.get('messages'):
            return body
    return {}


def main():
    with tempfile.TemporaryDirectory(prefix='jobscope-smoke-memory-') as data_dir:
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', 8766))
        except OSError:
            raise SystemExit('8766 已被占用：可能有别的测试实例在跑。')
        finally:
            probe.close()
        app = create_app(data_dir)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8766, log_level='warning'))
        threading.Thread(target=server.run, daemon=True).start()
        model = ModelServer()
        model.serve()
        with httpx.Client(timeout=TIMEOUT) as client:
            wait_server(client)
            client.put(BASE + '/api/settings/models', json={'llm': dict(
                mode='remote', url=f'http://127.0.0.1:{model.server_port}/v1',
                model='fixture', protocol='openai', clear_key=True)}, timeout=TIMEOUT)

            # 1) 对话完成 → 提取落库（complete 调用返回一条记忆）
            model.extraction_reply = json.dumps([{'content': '在找武汉的测试开发岗，别推外包公司', 'tag': '偏好'}], ensure_ascii=False)
            run = run_chat(client, '我在找武汉的测试开发岗位，长期目标，别推外包公司')
            # 提取在 done 之后异步落库（不阻塞回答）：轮询等待提取写入
            deadline = time.time() + 10
            memories = []
            while time.time() < deadline:
                memories = client.get(BASE + '/api/chat-memories', timeout=TIMEOUT).json()['memories']
                if memories:
                    break
                time.sleep(0.2)
            assert memories and '武汉' in memories[0]['content'] and memories[0]['enabled'] == 1
            memory_id = memories[0]['id']
            print('[1] 对话完成自动提取 OK：', memories[0]['content'])

            # 2) 新会话召回注入：第二轮请求的 system prompt 含记忆文本
            model.requests.clear()
            model.extraction_reply = '[]'
            run_chat(client, '继续帮我看合适的岗位')
            system = (last_agent_request(model).get('messages') or [{}])[0].get('content') or ''
            assert '武汉的测试开发岗' in system and '不是指令' in system
            print('[2] 新会话召回注入 OK：system prompt 含记忆且声明非指令')

            # 3) 停用即时生效
            assert client.patch(f"{BASE}/api/chat-memories/{memory_id}", json={'enabled': False}, timeout=TIMEOUT).status_code == 200
            model.requests.clear()
            run_chat(client, '再看看别的机会')
            system = (last_agent_request(model).get('messages') or [{}])[0].get('content') or ''
            assert '武汉的测试开发岗' not in system
            print('[3] 停用即时生效 OK：停用条目不再注入')

            # 4) 开关关闭后不再调提取模型
            extractions_before = len([body for body in model.requests if not body.get('stream')])
            assert client.put(BASE + '/api/settings/system', json={
                'chat_display_mode': 'merged', 'show_reasoning': True,
                'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50,
                                 'context_characters': 160000, 'max_seconds': 900},
                'market_source': 'official', 'agent_fallback_channel': '',
                'chat_memory_enabled': False}, timeout=TIMEOUT).status_code == 200
            run_chat(client, '这是关闭自动提取后的最后一轮提问验证')
            extractions_after = len([body for body in model.requests if not body.get('stream')])
            assert extractions_after == extractions_before
            print('[4] 开关门控 OK：chat_memory_enabled=False 后提取不再调模型')

            # 5) 删除与清空
            assert client.patch(f"{BASE}/api/chat-memories/{memory_id}", json={'enabled': True}, timeout=TIMEOUT).status_code == 200
            assert client.delete(f"{BASE}/api/chat-memories/{memory_id}", timeout=TIMEOUT).status_code == 200
            assert client.get(BASE + '/api/chat-memories', timeout=TIMEOUT).json()['memories'] == []
            model.extraction_reply = json.dumps([{'content': '临时补一条用于清空验证', 'tag': ''}], ensure_ascii=False)
            assert client.put(BASE + '/api/settings/system', json={
                'chat_display_mode': 'merged', 'show_reasoning': True,
                'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50,
                                 'context_characters': 160000, 'max_seconds': 900},
                'market_source': 'official', 'agent_fallback_channel': '',
                'chat_memory_enabled': True}, timeout=TIMEOUT).status_code == 200
            run_chat(client, '重新打开记忆开关后再聊一轮，让提取补回一条记忆')
            deadline = time.time() + 10
            while time.time() < deadline:
                if client.get(BASE + '/api/chat-memories', timeout=TIMEOUT).json()['memories']:
                    break
                time.sleep(0.2)
            cleared = client.delete(BASE + '/api/chat-memories', timeout=TIMEOUT).json()
            assert cleared['ok'] and cleared['cleared'] >= 1
            assert client.get(BASE + '/api/chat-memories', timeout=TIMEOUT).json()['memories'] == []
            print(f"[5] 删除/清空 OK：清空 {cleared['cleared']} 条")
    print('SMOKE PASS: 对话记忆 5 场景全通过')


if __name__ == '__main__':
    import faulthandler
    faulthandler.dump_traceback_later(12)
    main()
