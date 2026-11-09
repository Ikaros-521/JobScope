"""Bounded text extraction from the fixed CNInfo disclosure PDF host."""

import importlib
from urllib.parse import unquote, urlsplit

import httpx


def extract_announcement(url, *, client=None, max_bytes=20 * 1024 * 1024, max_pages=60, max_chars=120000):
    parts = urlsplit(url)
    path = unquote(parts.path)
    if (parts.scheme != 'https' or parts.hostname != 'static.cninfo.com.cn'
            or parts.port not in (None, 443) or parts.username or parts.password
            or not path.startswith('/finalpage/') or not path.lower().endswith('.pdf')
            or '..' in path.split('/') or '\\' in path or parts.query or parts.fragment):
        raise ValueError('仅支持巨潮资讯官方披露 PDF 链接')
    try:
        fitz = importlib.import_module('pymupdf')
    except ImportError as exc:
        raise ImportError('尚未安装报告提取依赖，请安装 PyMuPDF') from exc

    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(40, connect=10), follow_redirects=False)
    chunks = []
    size = 0
    try:
        with client.stream('GET', url, follow_redirects=False) as response:
            if response.is_redirect:
                raise ValueError('报告链接发生重定向，已停止提取')
            response.raise_for_status()
            length = response.headers.get('content-length', '')
            if length.isdigit() and int(length) > max_bytes:
                raise ValueError('报告超过允许的大小')
            for chunk in response.iter_bytes(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('报告超过允许的大小')
                chunks.append(chunk)
    finally:
        if owned:
            client.close()
    content = b''.join(chunks)
    if not content.lstrip().startswith(b'%PDF-'):
        raise ValueError('来源未返回有效 PDF，可能需要人工访问')
    try:
        document = fitz.open(stream=content, filetype='pdf')
    except Exception as exc:
        raise ValueError('PDF 无法解析，请人工查看原文') from exc
    with document:
        if document.needs_pass:
            raise ValueError('PDF 有密码保护，请人工查看原文')
        total_pages = len(document)
        sections = []
        chars = 0
        text_chars = 0
        pages = 0
        truncated = False
        for index in range(min(total_pages, max_pages)):
            text = document[index].get_text('text').strip()
            section = f'【第 {index + 1} 页】\n{text}'
            remaining = max_chars - chars
            if remaining <= 0:
                truncated = True
                break
            if len(section) > remaining:
                section = section[:remaining]
                truncated = True
            sections.append(section)
            text_chars += len(text.strip())
            chars += len(section)
            pages += 1
            if truncated:
                break
        truncated = truncated or pages < total_pages
        text = '\n\n'.join(sections)
        if text_chars < 20:
            raise ValueError('PDF 未取得可用文本，可能为扫描件；当前不包含 OCR')
        if truncated:
            text += f'\n\n【提取范围受限：已处理前 {pages} 页，文档共 {total_pages} 页，文本可能在页内截断。】'
        return dict(text=text, pages=pages, total_pages=total_pages, truncated=truncated)
