"""全局对话错误恢复分层与压缩存档的 SSE 全链路冒烟（2026-11-25，learn-claude-code s15 经验落地）。

自起 8766 隔离实例（进程内 uvicorn + 独立临时数据目录，绝不碰生产 .data），打真实 HTTP+SSE：
  1) 429 限流：两次退避重试后完成，SSE 可见 model_retry 步骤（带「429」字样）；
  2) 输出截断：finish_reason=length 触发预算升级（8000→16000），半截先重发 start 重置实时气泡；
  3) 529 过载：连续 3 次后自动切到「对话备用渠道」，后续请求打到备用模型端口，SSE 可见切换步骤；
  4) 超长 400：响应式压缩长工具结果并落档，archive_read 取回原文，SSE 可见「上下文压缩」步骤。

用法：backend/.venv/Scripts/python.exe scripts/smoke_chat_resilience.py
8766 被占用时直接拒绝启动（不与别人的测试实例抢端口）。
"""
import json
import os
import socket
import sys
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

# 必须「先设环境变量再 import jobscope.main」：main.py 在 import 时执行模块级 create_app()
# （conftest 同款规避）——不预设会打开生产 .data 并跑建表迁移。
os.environ['JOBSCOPE_DATA_DIR'] = str(Path(tempfile.mkdtemp(prefix='jobscope-smoke-resilience-import-')).resolve())

import httpx
import uvicorn

from jobscope.main import create_app

BASE = 'http://127.0.0.1:8766'
TIMEOUT = httpx.Timeout(60)


class ModelServer(ThreadingHTTPServer):
    """按端口脚本化应答的假模型：记录每个请求体（max_tokens 断言升级用）；
    脚本项为 ('error', status, body) 或 ('sse', chunks)；脚本耗尽后回最终回答。"""

    def __init__(self, port, script):
        super().__init__(('127.0.0.1', port), ScriptedHandler)
        self.daemon_threads = True
        self.script = list(script)
        self.requests = []


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server  # ThreadingHTTPServer 把实例挂在 self.server 上
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        server.requests.append(body)
        item = server.script.pop(0) if server.script else ('final', '模型脚本已耗尽，按脚本兜底回答。')
        if item[0] == 'error':
            _, status, text = item
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'message': text}}).encode())
            return
        if item[0] == 'sse':
            chunks = item[1]
        else:
            _, answer, reasoning = item if len(item) == 3 else ('final', item[1], '推理完成。')
            step = max(1, len(answer) // 3)
            chunks = [dict(delta=dict(reasoning_content=reasoning))]
            chunks += [dict(delta=dict(content=answer[i:i + step])) for i in range(0, len(answer), step)]
            chunks.append(dict(delta={}, finish_reason='stop'))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b'data: ' + json.dumps({'choices': [chunk]}, ensure_ascii=False).encode() + b'\n\n')
        self.wfile.write(b'data: [DONE]\n\n')


def wait_server(client, deadline=30):
    end = time.time() + deadline
    while time.time() < end:
        try:
            if client.get(BASE + '/api/settings/system').status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
    raise SystemExit('8766 实例启动超时')


def read_sse(client, run_id):
    frames = []
    with client.stream('GET', f'{BASE}/api/chat/runs/{run_id}/stream', timeout=TIMEOUT) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith('event: '):
                event = line[7:].strip()
            elif line.startswith('data: '):
                frames.append((event, json.loads(line[6:])))
    return frames


def run_scenario(client, model_server, message):
    client.put(BASE + '/api/settings/models', json={'llm': dict(
        mode='remote', url=f'http://127.0.0.1:{model_server.server_port}/v1',
        model='fixture', protocol='openai', clear_key=True)})
    run = client.post(BASE + '/api/chat/runs', json={'message': message, 'agent_mode': 'general'}, timeout=TIMEOUT).json()
    deadline = time.time() + 60
    while time.time() < deadline:
        status = client.get(f"{BASE}/api/chat/runs/{run['run_id']}", timeout=TIMEOUT).json()
        if status.get('done'):
            break
        time.sleep(0.1)
    frames = read_sse(client, run['run_id'])
    conversation = client.get(f"{BASE}/api/conversations/{run['conversation_id']}", timeout=TIMEOUT).json()
    return run, frames, conversation


def main():
    with tempfile.TemporaryDirectory(prefix='jobscope-smoke-resilience-') as data_dir:
        # 先探测端口：8766 可能驻着别人的测试实例，抢绑会给出莫名其妙的超时——直接说明并退出。
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', 8766))
        except OSError:
            raise SystemExit('8766 已被占用：可能有别的测试实例在跑，先确认归属再跑本冒烟（本脚本必须独占端口自起隔离实例）。')
        finally:
            probe.close()
        app = create_app(data_dir)
        config = uvicorn.Config(app, host='127.0.0.1', port=8766, log_level='warning')
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()

        # 场景一：429×2 → 完成
        primary = ModelServer(0, [('error', 429, 'slow down'), ('error', 429, 'slow down'),
                                  ('final', '限流退避后的完整回答。')])
        threading.Thread(target=primary.serve_forever, daemon=True).start()
        with httpx.Client(timeout=TIMEOUT) as client:
            wait_server(client)
            run, frames, conversation = run_scenario(client, primary, '限流场景')
            kinds = [kind for kind, _ in frames]
            steps = [payload for kind, payload in frames if kind == 'step' and payload.get('tool') == 'model_retry']
            stored = conversation['messages'][-1]
            assert kinds.count('start') == 1 and kinds[-1] == 'end' and kinds.count('done') == 1
            assert not stored.get('failed') and '限流退避后的完整回答。' in stored['content']
            assert len(steps) == 2 and all('429' in step['note'] for step in steps)
            print(f'[1] 429 退避重试 OK：model_retry×2，SSE 帧 {len(kinds)} 个')

            # 场景二：输出截断 → 预算升级 + start 重置
            primary.script = [('sse', [dict(delta=dict(reasoning_content='长思考')),
                                       dict(delta=dict(content='半截回答')),
                                       dict(delta={}, finish_reason='length')]),
                               ('final', '升级预算后的完整回答。')]
            primary.requests.clear()
            run, frames, conversation = run_scenario(client, primary, '截断场景')
            kinds = [kind for kind, _ in frames]
            steps = [payload for kind, payload in frames if kind == 'step' and payload.get('tool') == 'model_retry']
            stored = conversation['messages'][-1]
            assert kinds.count('start') == 2 and kinds.count('done') == 1
            assert '升级预算后的完整回答。' in stored['content'] and not stored.get('failed')
            assert any('提升到' in step['note'] for step in steps)
            assert primary.requests[-1]['max_tokens'] == 16000
            print('[2] 输出截断升级 OK：max_tokens 8000→16000，SSE 双 start 重置实时气泡')

            # 场景三：529×3 → 备用渠道切换
            backup = ModelServer(0, [('final', '备用渠道完成的回答。')])
            threading.Thread(target=backup.serve_forever, daemon=True).start()
            channel = client.post(BASE + '/api/settings/llm-channels', json=dict(
                name='备用渠道', channel='custom', protocol='openai',
                url=f'http://127.0.0.1:{backup.server_port}/v1', model='backup-model', key='k2'), timeout=TIMEOUT).json()['channel']
            assert client.put(BASE + '/api/settings/system', json={
                'chat_display_mode': 'merged', 'show_reasoning': True,
                'agent_limits': {'max_model_calls': 60, 'max_tool_calls': 50,
                                 'context_characters': 160000, 'max_seconds': 900},
                'market_source': 'official', 'agent_fallback_channel': channel['id']}, timeout=TIMEOUT).status_code == 200
            primary.script = [('error', 529, 'overloaded')] * 3 + [('final', '主渠道不应走到这一步。')]
            run, frames, conversation = run_scenario(client, primary, '过载降级场景')
            steps = [payload for kind, payload in frames if kind == 'step' and payload.get('tool') == 'model_retry']
            stored = conversation['messages'][-1]
            assert not stored.get('failed') and '备用渠道完成的回答。' in stored['content']
            assert any('切换备用渠道' in step['note'] for step in steps)
            assert len(backup.requests) == 1
            print('[3] 529 备用渠道降级 OK：主渠道 3 连过载后切换，请求落在备用端口')

            # 场景四：超长 400 → 压缩存档 → archive_read 取回。
            # 预算保持默认 160000：7 条 6000 字符读结果（约 4 万字符）不会触顶，
            # 上游 400 超长是唯一信号——走 ContextOverflowError 兜底（而非 drain 阶段的预防性压缩）。
            description = '岗位职责：' + '负责大模型检索增强与多轮对话系统设计落地。' * 200
            job = client.post(BASE + '/api/import/job', json=dict(
                title='长文岗位', company='长文公司', description=description, salary='20-30K'), timeout=TIMEOUT).json()['job']
            evidence_id = job['evidence_ids'][0]
            read_calls = [{'index': i, 'id': f'batch{i}', 'type': 'function',
                           'function': {'name': 'project_read',
                                        'arguments': json.dumps({'evidence_id': evidence_id})}} for i in range(7)]
            primary.script = [('sse', [dict(delta=dict(tool_calls=read_calls))]),
                              ('error', 400, "This model's maximum context length is 8192 tokens"),
                              ('sse', [dict(delta=dict(tool_calls=[dict(index=0, id='arc1', type='function',
                                                                        function=dict(name='archive_read', arguments=json.dumps({'tool_call_id': 'batch0'})))]))]),
                              ('final', '已取回存档原文并继续完成回答。')]
            run, frames, conversation = run_scenario(client, primary, '超长压缩场景')
            steps = [payload for kind, payload in frames if kind == 'step']
            stored = conversation['messages'][-1]
            archived = app.state.repo.read_tool_archive(run['conversation_id'], 'batch0')
            assert not stored.get('failed') and '继续完成回答' in stored['content']
            assert any(step.get('tool') == 'model_retry' and '超长' in step['note'] for step in steps)
            assert any(step.get('tool') == 'context_compact' for step in steps)
            assert any(step.get('tool') == 'archive_read' for step in steps)
            assert archived and '大模型检索增强' in archived
            print('[4] 超长压缩存档 OK：400 识别→压缩落档→archive_read 取回，SSE 全程可见')

        server.should_exit = True
        time.sleep(0.5)
    print('SMOKE PASS: 4 场景全通过')


if __name__ == '__main__':
    main()
