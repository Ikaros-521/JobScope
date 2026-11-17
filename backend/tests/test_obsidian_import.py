import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


NOTE_FULL = """---
类别: 项目
标题: 电商大促网关
公司: 甲科技
时间段: 2023-2024
标签: [后端, 高并发]
---

# 电商大促网关

独立设计限流方案，峰值 4.2 万 QPS 零故障。#网关 #灰度发布
"""

NOTE_PLAIN = '乙公司结算服务重构：账务幂等改造，日对账 2000 万条。'


def make_vault(tmp_path):
    root = tmp_path / 'vault'
    (root / '项目').mkdir(parents=True)
    (root / '项目' / '电商大促网关.md').write_text(NOTE_FULL, encoding='utf-8')
    (root / '工作').mkdir(parents=True)
    (root / '工作' / '工作经历.md').write_text(NOTE_PLAIN, encoding='utf-8')
    (root / '.obsidian' / 'config.json').parent.mkdir(parents=True)
    (root / '.obsidian' / 'config.json').write_text('{}', encoding='utf-8')
    (root / '草稿.md').write_text('', encoding='utf-8')
    return root


def test_obsidian_parse_covers_frontmatter_folder_and_inline_tags(tmp_path):
    from jobscope.obsidian_import import parse_vault
    root = make_vault(tmp_path)
    entries, errors = parse_vault(root)
    assert len(entries) == 2 and len(errors) == 1  # 草稿空文件记一条跳过
    by_title = {item['title']: item for item in entries}
    full = by_title['电商大促网关']
    assert full['category'] == 'project' and full['org'] == '甲科技' and full['period'] == '2023-2024'
    assert set(full['tags']) == {'后端', '高并发', '网关', '灰度发布'}  # frontmatter + 内联合并去重
    assert full['content'].startswith('独立设计限流方案')  # H1 进了 title，正文不带重复标题
    assert full['source'] == '项目/电商大促网关.md'
    plain = by_title['工作经历']  # 无 frontmatter：标题取文件名、类别取顶层目录别名
    assert plain['category'] == 'work' and plain['org'] == '' and plain['source'] == '工作/工作经历.md'
    assert '.obsidian' not in ' '.join(item['source'] for item in entries)


def test_obsidian_import_is_idempotent_by_source(client, tmp_path):
    root = make_vault(tmp_path)
    first = client.post('/api/profile-entries/import-obsidian', json={'path': str(root)}).json()
    assert first == {'imported': 2, 'updated': 0, 'total': 2, 'errors': [pytest.approx(first['errors'][0])]}
    listed = client.get('/api/profile-entries').json()['entries']
    assert len(listed) == 2 and all(item['source'] for item in listed)

    # 改库文件后重导：原位更新（同一条目 id），不产生重复。
    (root / '工作' / '工作经历.md').write_text(NOTE_PLAIN + '；新增对账口径说明。', encoding='utf-8')
    second = client.post('/api/profile-entries/import-obsidian', json={'path': str(root)}).json()
    assert second['imported'] == 0 and second['updated'] == 2
    assert len(client.get('/api/profile-entries').json()['entries']) == 2
    updated = next(item for item in client.get('/api/profile-entries').json()['entries'] if item['title'] == '工作经历')
    assert '对账口径' in updated['content']

    # 重复导入路径不同时互不覆盖：另一目录的同名文件是独立条目。
    other = tmp_path / 'vault2'
    other.mkdir()
    (other / '工作经历.md').write_text('另一份工作笔记', encoding='utf-8')
    third = client.post('/api/profile-entries/import-obsidian', json={'path': str(other)}).json()
    assert third['imported'] == 1
    assert len(client.get('/api/profile-entries').json()['entries']) == 3


def test_obsidian_dry_run_previews_without_saving(client, tmp_path):
    root = make_vault(tmp_path)
    data = client.post('/api/profile-entries/import-obsidian',
                       json={'path': str(root), 'dry_run': True}).json()
    assert data['total'] == 2
    # 遍历顺序取决于文件系统排序：预览内容用集合比对，不假设先后。
    assert {item['title'] for item in data['preview']} == {'电商大促网关', '工作经历'}
    assert client.get('/api/profile-entries').json()['entries'] == []  # 预览不落库


def test_obsidian_import_rejects_missing_dir(client, tmp_path):
    result = client.post('/api/profile-entries/import-obsidian', json={'path': str(tmp_path / 'nope')})
    assert result.status_code == 400 and '目录不存在' in result.json()['detail']
