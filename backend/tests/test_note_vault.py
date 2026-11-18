"""note_vault：笔记库读取的确定性部分——遍历/上限/防穿越/锚点（LLM 提炼在 general_agent 工具循环里）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import note_vault


def test_list_vault_skips_hidden_dirs_and_oversize_files(tmp_path):
    (tmp_path / '工作').mkdir()
    (tmp_path / '工作' / 'a.md').write_text('# A', encoding='utf-8')
    (tmp_path / '.obsidian').mkdir()
    (tmp_path / '.obsidian' / 'conf.md').write_text('x', encoding='utf-8')
    (tmp_path / '.hidden.md').write_text('x', encoding='utf-8')
    (tmp_path / 'big.md').write_text('x' * (note_vault.MAX_FILE_BYTES + 1), encoding='utf-8')
    files, errors = note_vault.list_vault(tmp_path)
    assert [item['rel_path'] for item in files] == ['工作/a.md']
    assert files[0]['bytes'] == len('# A'.encode('utf-8'))
    assert len(errors) == 1 and 'big.md' in errors[0]


def test_read_note_windows_paging_and_traversal_guard(tmp_path):
    (tmp_path / 'note.md').write_text('正' * (note_vault.READ_WINDOW_CHARS + 1000), encoding='utf-8')
    first = note_vault.read_note(tmp_path, 'note.md')
    assert first['total_characters'] == note_vault.READ_WINDOW_CHARS + 1000 and first['has_more'] is True
    second = note_vault.read_note(tmp_path, 'note.md', offset=note_vault.READ_WINDOW_CHARS)
    assert len(second['text']) == 1000 and second['has_more'] is False
    with pytest.raises(ValueError, match='正文窗口为空'):
        note_vault.read_note(tmp_path, 'note.md', offset=first['total_characters'])
    # 越界读取：相对穿越 / Windows 绝对路径 / 盘符 / 非 .md 一律拒绝。
    outside = tmp_path.parent / 'outside.md'
    outside.write_text('secret', encoding='utf-8')
    with pytest.raises(ValueError):
        note_vault.read_note(tmp_path, '../%s' % outside.name)
    with pytest.raises(ValueError):
        note_vault.read_note(tmp_path, str(outside))
    with pytest.raises(ValueError):
        note_vault.read_note(tmp_path, 'C:/Windows/win.ini')
    with pytest.raises(ValueError):
        note_vault.read_note(tmp_path, 'note.txt')


def test_anchor_formats_normalize_root_and_keep_legacy(tmp_path):
    anchor = note_vault.vault_anchor(tmp_path, '工作/a.md')
    assert anchor == f'{tmp_path.resolve()}::工作/a.md'
    # 反斜杠输入归一为库内正斜杠；legacy 锚点原样保留用户输入串（接管存量条目用）。
    assert note_vault.vault_anchor(tmp_path, '工作\\a.md') == anchor
    legacy = note_vault.legacy_anchor(note_vault.clean_root(str(tmp_path) + '/'), '工作/a.md')
    assert legacy == f'{Path(str(tmp_path))}::工作/a.md'
    # 带引号/尾分隔符的输入清洗后可用。
    assert note_vault.clean_root(f'"{tmp_path}"\\').is_dir()
