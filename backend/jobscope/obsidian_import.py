"""Obsidian 库导入（2026-11-17）：把用户已有的 markdown 笔记提炼进个人资料库。

设计边界（与产品纪律一致）：
- 只读用户显式指定的本地目录，单向导入，不写回、不监听、不联网；Obsidian 程序本身不参与。
- 笔记是用户手写的第一手事实，与手写条目同一口径，进个人资料库（不进知识检索体系）。
- 幂等锚点 = 库内相对路径（存 profile_entries.source）：同一文件重导在原位更新，不产生重复。

笔记约定（全部可选，缺什么用什么兜底）：
- frontmatter（--- 围栏内的 `key: value` 行，不做完整 YAML）：category/title/org/period/tags；
  中文别名同样接受（类别/标题/公司/组织/时间段/标签）。
- category 兜底顺序：frontmatter 显式 > 中文/英文别名 > 顶层目录名别名 > other。
- title：frontmatter > 正文首个 H1 > 文件名。
- 内联标签（#标签，紧跟词首无空格）并入 tags；`# 一级标题` 因带空格不会误判。

上限：单次 500 个文件、单文件 200KB；跳过 .obsidian/.git 等目录与点开头文件。
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_FILES = 500
MAX_FILE_BYTES = 200_000
SKIP_DIRS = {'.obsidian', '.git', '.trash', '.smart-env', 'node_modules'}

CATEGORY_ALIASES: dict[str, str] = {}
for _canonical, _aliases in {
    'project': ('项目', '项目经历', '项目经验', 'project', 'projects'),
    'work': ('工作', '工作经历', '经历', 'work', 'experience'),
    'education': ('教育', '教育背景', '学历', 'education'),
    'skill': ('技能', '专业技能', '技术栈', 'skill', 'skills'),
    'cert': ('证书', '认证', 'cert', 'certificate'),
    'award': ('奖项', '荣誉', '获奖', 'award', 'awards'),
}.items():
    for _alias in _aliases:
        CATEGORY_ALIASES[_alias] = _canonical

FIELD_ALIASES = {'类别': 'category', '标题': 'title', '公司': 'org', '组织': 'org',
                 '时间段': 'period', '时间': 'period', '标签': 'tags', '标题别': 'title'}

FRONTMATTER_RE = re.compile(r'\A---\s*\n(.*?)\n---\s*\n?', re.S)
H1_RE = re.compile(r'^#\s+(.+)$', re.M)
INLINE_TAG_RE = re.compile(r'(?<![\w#])#([\w\u4e00-\u9fa5][\w\u4e00-\u9fa5/-]{0,30})')


def _split_list(value):
    """`[a, b]` / `a, b` / `- a` 多行的最小解析：够 frontmatter 常见写法即可。"""
    text = str(value).strip().strip('[]')
    return [item.strip().strip('"\'') for item in re.split(r'[,，]', text) if item.strip()]


def _parse_frontmatter(text):
    """返回 (元数据 dict, 去掉围栏后的正文)。键名过 FIELD_ALIASES 归一。"""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    meta = {}
    for line in match[1].splitlines():
        if re.match(r'^\s*-\s+\S', line):
            continue  # 列表项：tags 走单行写法即可，多行列表暂不展开
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        canonical = FIELD_ALIASES.get(key.strip().lower(), key.strip().lower())
        meta[canonical] = value.strip()
    return meta, text[match.end():]


def _map_category(value, folder):
    """category 兜底顺序：显式值（含中文别名）> 顶层目录名 > other。"""
    for candidate in (str(value or '').strip(), str(folder or '').strip()):
        mapped = CATEGORY_ALIASES.get(candidate.lower()) or CATEGORY_ALIASES.get(candidate)
        if mapped:
            return mapped
    return 'other'


def parse_note(text, *, rel_path):
    """单篇笔记 → 条目 dict（或 None：没有可提取的标题与正文）。"""
    meta, body = _parse_frontmatter(text)
    body = body.strip()
    title = str(meta.get('title') or '').strip()
    h1 = H1_RE.search(body)
    if h1:
        if not title:
            title = h1[1].strip()
        # 文档首个 H1 与标题同义，正文不再重复带一行（frontmatter 已给标题时同样剥掉）。
        body = (body[:h1.start()] + body[h1.end():]).strip()
    if not title:
        title = Path(rel_path).stem
    content = body.strip()
    if not content:
        return None
    tags = _split_list(meta.get('tags') or '')
    tags.extend(INLINE_TAG_RE.findall(content))
    folder = rel_path.replace('\\', '/').split('/')[0] if '/' in rel_path.replace('\\', '/') else ''
    return dict(category=_map_category(meta.get('category'), folder),
                title=title[:200], org=str(meta.get('org') or '').strip()[:200],
                period=str(meta.get('period') or '').strip()[:100],
                content=content[:20000], tags=[tag for tag in dict.fromkeys(tags) if len(tag) <= 50],
                enabled=True, source=rel_path)


def parse_vault(root: Path):
    """遍历库目录 → (条目列表, 错误列表)。只收 .md；跳过隐藏目录/文件；超限文件记错误跳过。"""
    entries, errors = [], []
    md_files = []
    for path in sorted(root.rglob('*.md')):
        rel = path.relative_to(root)
        parts = set(rel.parts[:-1])
        if parts & SKIP_DIRS or any(part.startswith('.') for part in rel.parts):
            continue
        md_files.append((path, rel.as_posix()))
        if len(md_files) >= MAX_FILES:
            errors.append(f'超过单次导入上限 {MAX_FILES} 个文件，其余未处理。')
            break
    for path, rel in md_files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                errors.append(f'{rel}：超过单文件 200KB，跳过。')
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            errors.append(f'{rel}：读取失败（{type(exc).__name__}）。')
            continue
        entry = parse_note(text, rel_path=rel)
        if entry is None:
            errors.append(f'{rel}：没有可提取的正文，跳过。')
            continue
        entries.append(entry)
    return entries, errors
