"""运行日志环形缓冲与 /api/logs 端点：关键词、等级过滤与 after 增量。"""
import logging
import uuid

from fastapi.testclient import TestClient

from jobscope.logs import RING_SIZE, recent_logs
from jobscope.main import create_app


def test_recent_logs_filter_and_increment(tmp_path):
    client = TestClient(create_app(tmp_path))
    marker = f'ring-marker-{uuid.uuid4().hex}'
    logging.getLogger('jobscope.logs_test').warning('标记载体 %s', marker)

    hits = client.get('/api/logs', params={'q': marker}).json()
    assert hits['entries'], '关键词应命中刚写入的日志'
    hit = hits['entries'][-1]
    assert hit['level'] == 'WARNING' and marker in hit['message']

    # 增量：after 之后的记录 seq 严格递增；latest 是单调计数器——两次请求之间 httpx 的
    # INFO 访问行本身也会入缓冲，所以只断言不减，不断言相等。
    seq = hit['seq']
    incremental = client.get('/api/logs', params={'after': seq}).json()
    assert all(entry['seq'] > seq for entry in incremental['entries'])
    all_entries = client.get('/api/logs', params={'limit': 2000}).json()
    assert incremental['latest'] >= seq and all_entries['latest'] >= incremental['latest']

    errors = client.get('/api/logs', params={'level': 'ERROR', 'limit': 100}).json()
    assert all(entry['level'] == 'ERROR' for entry in errors['entries'])

    limited = client.get('/api/logs', params={'limit': 2}).json()
    assert len(limited['entries']) <= 2

    # 非法等级被参数校验拦下，环形缓冲上限不会被端点撑破。
    assert client.get('/api/logs', params={'level': 'TRACE'}).status_code == 422
    assert len(_ring_snapshot()) <= RING_SIZE


def _ring_snapshot():
    from jobscope.logs import _RING
    return list(_RING)


def test_recent_logs_direct_call(tmp_path):
    create_app(tmp_path)  # 装配根 Logger（幂等）；端点函数与直调共用同一份缓冲。
    marker = f'direct-{uuid.uuid4().hex}'
    logging.getLogger('jobscope.logs_test').info('直调验证 %s', marker)
    payload = recent_logs(q=marker, limit=10)
    assert any(marker in entry['message'] for entry in payload['entries'])
