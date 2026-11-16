"""Bounded knowledge-file extraction; parsers reused from resume_import (pymupdf / docx-XML / GB18030)."""

from html.parser import HTMLParser
from pathlib import PurePosixPath

from .resume_import import MAX_FILE_BYTES, decode_text, docx_text, pdf_text

# 知识文档上限比简历宽：300 页 / 20 万字符（kb-kit 同思路的防超界口径）。
MAX_PAGES = 300
MAX_TEXT_CHARS = 200000

TEXT_EXTENSIONS = {'.txt', '.text', '.md', '.markdown', '.mdx', '.csv', '.tsv', '.json', '.jsonl',
                   '.log', '.html', '.htm', '.xml', '.yaml', '.yml', '.ini', '.conf', '.properties',
                   '.srt', '.vtt', '.rst', '.py', '.js', '.ts', '.java', '.sql', '.sh'}


def safe_name(filename):
    return PurePosixPath(str(filename or '').replace('\\', '/')).name


def title_from_filename(name):
    base = safe_name(name).strip()
    extension = PurePosixPath(base).suffix.lower()
    title = base[:-len(extension)] if extension else base
    return title.strip() or '未命名资料'


def normalize_text(text):
    return (text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
            .strip())


class _TagStripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        if tag in ('p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(content):
    """HTML 去标签（kb-kit Lite 同口径）；解析失败按纯文本兜底。"""
    try:
        stripper = _TagStripper()
        stripper.feed(content.decode('utf-8-sig', errors='replace'))
        return ''.join(stripper.parts)
    except ValueError:
        return decode_text(content)


def extract_file(filename, content):
    """上传字节 → 纯文本；格式校验、大小与提取上限统一在这里收口。"""
    name = safe_name(filename)
    extension = PurePosixPath(name).suffix.lower()
    if extension == '.doc':
        raise ValueError('旧版 Word .doc 不能直接解析，请在 Word / WPS 另存为 .docx 后导入')
    if extension not in {'.pdf', '.docx', *TEXT_EXTENSIONS}:
        supported = 'PDF、Word .docx、Markdown、HTML 及纯文本格式'
        raise ValueError(f'不支持此文件格式（{extension or "无扩展名"}），知识库支持：{supported}')
    if not content:
        raise ValueError('文件为空，请选择有内容的文件')
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('文件超过 10 MB，请压缩或缩减后导入')
    if extension == '.pdf':
        text, _pages, warnings = pdf_text(content, max_pages=MAX_PAGES, max_chars=MAX_TEXT_CHARS)
    elif extension == '.docx':
        text, _pages, warnings = docx_text(content)
    elif extension in {'.html', '.htm'}:
        text, warnings = html_text(content), []
    else:
        text, warnings = decode_text(content), []
    text = normalize_text(text)
    if not text:
        raise ValueError('提取内容为空，请确认文件中有文字；扫描件请先使用 OCR 识别')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f'提取文本超过 {MAX_TEXT_CHARS:,} 字符，请缩减文件后导入')
    return dict(text=text, characters=len(text), file_name=name, file_size=len(content),
                warnings=warnings)
