"""Markdown 笔记库读取（Agent 工具用）：把用户指定的本地笔记目录交给全局对话 Agent 提炼导入。

2026-11-18：原规则版 Obsidian 导入（frontmatter/目录别名解析，无模型）退役——对自由格式笔记
效果差。现在由 GeneralAgent 的 vault_list / vault_read / profile_import_write 三个内建工具完成：
LLM 挑笔记、理解内容、提炼结构化条目；本模块只保留确定性部分（遍历/上限/锚点/防穿越）。

边界沿用原导入：只读用户显式指定的目录，不写回、不监听、不联网；Obsidian 程序不参与，
任意 markdown 目录（含 Obsidian 库）均可。幂等锚点 = 规范化库根 + 库内相对路径（存
profile_entries.source）：同一文件重导原位更新；旧规则版导入写入的 `{原始输入}::{rel}` 锚点
在写入时一并接管（命中即沿用条目并改挂新锚点）。

上限沿用原导入：单库 500 个文件、单文件 200KB；跳过 .obsidian/.git 等目录与点开头文件。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

MAX_FILES = 500
MAX_FILE_BYTES = 200_000
SKIP_DIRS = {'.obsidian', '.git', '.trash', '.smart-env', 'node_modules'}
READ_WINDOW_CHARS = 8000


def clean_root(path: str) -> Path:
    """用户输入 → 库根 Path：去引号/尾分隔符（引号可能裹在尾分隔符之外，如 `"D:\\Notes"\\`）；
    不校验存在性（由调用方决定报错口径）。"""
    text = str(path or '').strip()
    for _ in range(2):
        text = text.strip('"').strip("'").rstrip('\\/')
    return Path(text)


def list_vault(root: Path):
    """遍历库目录 → (文件列表, 错误列表)。只收 .md；跳过隐藏目录/文件；超限文件记错误跳过。"""
    files, errors = [], []
    md_files = []
    for path in sorted(root.rglob('*.md')):
        rel = path.relative_to(root)
        parts = set(rel.parts[:-1])
        if parts & SKIP_DIRS or any(part.startswith('.') for part in rel.parts):
            continue
        md_files.append((path, rel.as_posix()))
        if len(md_files) >= MAX_FILES:
            errors.append(f'超过单库读取上限 {MAX_FILES} 个文件，其余未列出。')
            break
    for path, rel in md_files:
        try:
            size = path.stat().st_size
        except OSError:
            errors.append(f'{rel}：读取失败，跳过。')
            continue
        if size > MAX_FILE_BYTES:
            errors.append(f'{rel}：超过单文件 200KB，跳过。')
            continue
        files.append(dict(rel_path=rel, bytes=size))
    logger.info('笔记库扫描完成：%s 个 .md 可读，%s 条跳过/错误', len(files), len(errors))
    return files, errors


def resolve_note_path(root: Path, rel_path: str) -> Path:
    """库内相对路径 → 绝对路径，拒绝越界（绝对路径/../ 盘符）与解引用逃逸。"""
    rel = PurePosixPath(str(rel_path or '').strip().replace('\\', '/'))
    if not rel.parts or rel.is_absolute() or '..' in rel.parts or re.match(r'^[A-Za-z]:', str(rel)):
        raise ValueError(f'rel_path 非法（须为库内相对路径）：{str(rel_path)[:100]}')
    root_res = root.resolve()
    final = (root_res.joinpath(*rel.parts)).resolve()
    root_key = os.path.normcase(str(root_res))
    final_key = os.path.normcase(str(final))
    if final_key != root_key and not final_key.startswith(root_key + os.sep):
        raise ValueError(f'rel_path 越出库目录：{str(rel_path)[:100]}')
    if final.suffix.lower() != '.md':
        raise ValueError('只读取库内的 .md 笔记')
    return final


def read_note(root: Path, rel_path: str, *, offset: int = 0):
    """单篇笔记正文窗口（UTF-8 容错，超长截断由 offset 翻页兜底）。"""
    final = resolve_note_path(root, rel_path)
    try:
        if final.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f'{rel_path}：超过单文件 200KB，跳过。')
        text = final.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        raise ValueError(f'{rel_path}：读取失败（{type(exc).__name__}）。') from exc
    window = text[offset:offset + READ_WINDOW_CHARS]
    if not window:
        raise ValueError('正文窗口为空，请使用更小的offset')
    return dict(rel_path=rel_path, text=window, offset=offset,
                total_characters=len(text), has_more=offset + READ_WINDOW_CHARS < len(text))


def vault_anchor(root: Path, rel_path: str) -> str:
    """幂等锚点：规范化库根 + 库内相对路径。同库重导原位更新，不同库不互撞。"""
    rel = PurePosixPath(str(rel_path or '').strip().replace('\\', '/'))
    return f'{root.resolve()}::{rel.as_posix()}'


def legacy_anchor(root_as_given: Path, rel_path: str) -> str:
    """旧规则版导入的锚点格式（未规范化的输入串）：仅用于接管存量条目。"""
    rel = PurePosixPath(str(rel_path or '').strip().replace('\\', '/'))
    return f'{root_as_given}::{rel.as_posix()}'
