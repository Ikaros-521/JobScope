"""Local-only, bounded resume file extraction. Import never saves the resume."""
import io
import re
import zipfile
import zlib
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .analysis import parse_resume

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 100000
MAX_PAGES = 50
WORD_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
COMPAT_NS = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'


def pdf_text(content):
    import pymupdf
    if not content.lstrip().startswith(b'%PDF-'):
        raise ValueError('文件不是有效 PDF，请重新导出后导入')
    try:
        document = pymupdf.open(stream=content, filetype='pdf')
    except (RuntimeError, ValueError):
        raise ValueError('PDF 文件损坏或无法解析，请重新导出后导入') from None
    with document:
        if document.needs_pass:
            raise ValueError('PDF 有密码保护，请先解除密码再导入')
        if len(document) > MAX_PAGES:
            raise ValueError(f'PDF 超过 {MAX_PAGES} 页，请仅保留简历页面后导入')
        sections, blank = [], []
        for index, page in enumerate(document):
            text = page.get_text('text', sort=True).strip()
            if text:
                sections.append(text)
            else:
                blank.append(str(index + 1))
            if sum(map(len, sections)) > MAX_TEXT_CHARS:
                raise ValueError('提取文本超过 100,000 字符，请缩减文件后导入')
        if not sections:
            raise ValueError('PDF 没有可提取文字，可能是扫描件或图片简历；请先使用 OCR 转为可搜索 PDF，或导入 Word / 文本文件')
        warnings = ['多栏 PDF 的阅读顺序可能与页面不同，请核对技能、日期和项目经历。']
        if blank:
            warnings.append('第 ' + '、'.join(blank) + ' 页未提取到文字，可能为空白页或扫描页，请核对是否缺少经历。')
        return '\n\n'.join(sections), len(document), warnings


def word_xml_text(content):
    # No DTD/entities, relationships, embedded files, macros or external resources.
    try:
        root = SafeET.fromstring(content, forbid_dtd=True)
    except DefusedXmlException:
        raise ValueError('Word XML 包含不支持的文档声明，请重新另存为 .docx') from None
    except (ET.ParseError, ValueError):
        raise ValueError('Word 文档内容损坏，请重新另存为 .docx 后导入') from None
    chunks = []

    def visit(node):
        if node.tag == COMPAT_NS + 'AlternateContent':
            choices = [child for child in node if child.tag == COMPAT_NS + 'Choice' and any(desc.tag == WORD_NS + 't' for desc in child.iter())]
            fallback = node.find(COMPAT_NS + 'Fallback')
            selected = choices[0] if choices else fallback
            if selected is not None:
                visit(selected)
            return
        if node.tag in (WORD_NS + 'del', WORD_NS + 'moveFrom'):
            return
        if node.tag == WORD_NS + 't':
            chunks.append(node.text or '')
        elif node.tag in (WORD_NS + 'tab',):
            chunks.append('\t')
        elif node.tag in (WORD_NS + 'br', WORD_NS + 'cr'):
            chunks.append('\n')
        for child in node:
            visit(child)
        if node.tag == WORD_NS + 'p':
            chunks.append('\n')
        elif node.tag == WORD_NS + 'tc':
            chunks.append('\t')

    try:
        visit(root)
    except RecursionError:
        raise ValueError('Word 文档层级异常，请重新另存为 .docx 后导入') from None
    return ''.join(chunks).strip()


def docx_text(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(info.file_size for info in entries) > 20 * 1024 * 1024:
                raise ValueError('Word 解压后内容过大，请删除大图或缩减文件后导入')
            if any(info.flag_bits & 1 for info in entries):
                raise ValueError('Word 文件有密码保护，请先解除密码再导入')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('文件不是有效 Word .docx，请从 Word 重新另存为 .docx')
            headers = sorted(info.filename for info in entries if re.fullmatch(r'word/header\d+\.xml', info.filename))
            footers = sorted(info.filename for info in entries if re.fullmatch(r'word/footer\d+\.xml', info.filename))
            sections = [word_xml_text(archive.read(name)) for name in [*headers, 'word/document.xml', *footers]]
            return '\n\n'.join(text for text in sections if text), None, ['已提取正文、表格、文本框及页眉页脚；请核对排版转换后的阅读顺序。']
    except (zipfile.BadZipFile, KeyError, NotImplementedError, RuntimeError, OSError, zlib.error):
        raise ValueError('Word 文件损坏、加密或不是有效 .docx，请重新另存后导入') from None


def decode_text(content):
    encodings = ['utf-16'] if content.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'gb18030']
    for encoding in encodings:
        try:
            result = content.decode(encoding)
            if '\x00' in result:
                break
            return result
        except UnicodeError:
            continue
    raise ValueError('文本编码无法识别，请将文件另存为 UTF-8 后导入')


def extract_resume(filename, content):
    name = PurePosixPath(filename.replace('\\', '/')).name
    extension = PurePosixPath(name).suffix.lower()
    if extension == '.doc':
        raise ValueError('旧版 Word .doc 当前不能直接解析，请在 Word / WPS 另存为 .docx 后导入')
    if extension not in {'.pdf', '.docx', '.txt', '.md', '.yaml', '.yml'}:
        raise ValueError('不支持此文件格式，请导入 PDF、Word .docx、TXT、Markdown 或 facts.yaml')
    if not content:
        raise ValueError('文件为空，请选择有内容的简历文件')
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('文件超过 10 MB，请压缩或缩减后导入')
    format = 'facts_yaml' if extension in {'.yaml', '.yml'} else 'text'
    if extension == '.pdf':
        text, pages, warnings = pdf_text(content)
    elif extension == '.docx':
        text, pages, warnings = docx_text(content)
    else:
        text, pages, warnings = decode_text(content), None, []
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '').strip()
    if not text:
        raise ValueError('提取内容为空，请确认文件中有文字；图片简历请先使用 OCR 识别')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('提取文本超过 100,000 字符，请缩减文件后导入')
    try:
        parse_resume(text, format)
    except RecursionError:
        raise ValueError('facts.yaml 层级过深，请简化为 facts 列表后导入') from None
    return dict(filename=name, text=text, format=format, characters=len(text), pages=pages, warnings=warnings)
