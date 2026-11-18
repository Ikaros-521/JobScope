"""数据备份与恢复（2026-11-18）：快照一致性、恢复安全网与结构校验。

全部 tmp_path 隔离，不触碰真实 .data。
"""
import io
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.backup import BackupStore, BackupError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def make_job(title='Python工程师', company='甲公司', **extra):
    return dict(title=title, company=company, description='熟悉 Python 与 SQL', salary='20-30K', location='北京', **extra)


def test_create_backup_is_consistent_zip(client):
    repo = client.app.state.repo
    repo.save_job(make_job(external_id='ext-1'), 'boss')
    result = client.post('/api/backup').json()
    backup = result['backup']
    assert backup['id'].startswith('jobscope-')
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/backup/{backup['id']}/download").content)) as archive:
        assert archive.namelist() == ['jobscope.sqlite3']
        import sqlite3
        raw = archive.read('jobscope.sqlite3')
        assert raw[:16] == b'SQLite format 3\x00'
        # 解出的快照本身可用，且包含刚才那份岗位：在线备份拿到的是一致状态。
        snapshot = client.app.state.repo.data_dir / 'check.sqlite3'
        snapshot.write_bytes(raw)
        conn = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
        try:
            assert conn.execute("SELECT count(*) FROM jobs WHERE json_extract(payload,'$.title')='Python工程师'").fetchone()[0] == 1
        finally:
            conn.close()
            snapshot.unlink()
    status = client.get('/api/backup').json()
    assert status['last_backup_at'] and status['days_since'] is not None


def test_restore_round_trip_replaces_content_and_keeps_safety_backup(client, tmp_path):
    repo = client.app.state.repo
    repo.save_job(make_job(external_id='ext-a', title='岗位A'), 'boss')
    backup_bytes = client.get(f"/api/backup/{client.post('/api/backup').json()['backup']['id']}/download").content

    # 第二阶段：换一个库（模拟换机/误删后恢复），恢复后岗位A回来了。
    other = tmp_path / 'other-data'
    with TestClient(create_app(other)) as second:
        second_repo = second.app.state.repo
        second_repo.save_job(make_job(external_id='ext-b', title='岗位B', company='乙公司'), 'boss')
        result = second.post('/api/backup/restore', content=backup_bytes, headers={'Content-Type': 'application/octet-stream'})
        assert result.status_code == 200, result.text
        assert result.json()['jobs'] == 1
        titles = [job['title'] for job in second_repo.jobs()]
        assert titles == ['岗位A']
        # 恢复前自动落了安全备份：岗位B 那份旧库还在备份目录里，可反悔。
        names = [item['name'] for item in second.get('/api/backup').json()['backups']]
        assert any('pre-restore' in name for name in names)


def test_restore_rejects_foreign_archives(client):
    # 随便一个 zip（结构不符）
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('readme.txt', '不是备份')
    assert client.post('/api/backup/restore', content=buffer.getvalue()).status_code == 422
    # 塞了 SQLite 头但不是库
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('jobscope.sqlite3', b'SQLite format 3\x00' + b'\x00' * 4096)
    assert client.post('/api/backup/restore', content=buffer.getvalue()).status_code == 422
    # 空 body
    assert client.post('/api/backup/restore').status_code == 422


def test_retention_prunes_oldest_regular_backups(tmp_path):
    from jobscope.repository import Repository
    repo = Repository(tmp_path)
    store = BackupStore(repo, tmp_path)
    for index in range(12):
        store.create(label=f'run{index:02d}')
    remaining = [item for item in store.list() if not item['pre_restore']]
    assert len(remaining) == 10
    # 保留的是最新的：最早的两份（run00/run01）被裁掉。
    assert not any('run00' in item['name'] or 'run01' in item['name'] for item in remaining)
    assert any('run11' in item['name'] for item in remaining)


def test_backup_id_traversal_rejected(tmp_path):
    from jobscope.repository import Repository
    repo = Repository(tmp_path)
    store = BackupStore(repo, tmp_path)
    with pytest.raises(BackupError):
        store.read('../../jobscope-20260101-000000')
    with pytest.raises(BackupError):
        store.delete('jobscope-..-x')


def test_delete_backup(client):
    backup_id = client.post('/api/backup').json()['backup']['id']
    assert client.delete(f'/api/backup/{backup_id}').json()['ok'] is True
    assert client.get(f'/api/backup/{backup_id}/download').status_code == 404
