import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def module():
    assert importlib.util.find_spec('jobscope.documents'), '报告正文提取尚未实现'
    from jobscope import documents
    return documents


@pytest.mark.parametrize('url', [
    'http://static.cninfo.com.cn/finalpage/a.PDF',
    'https://example.com/a.PDF',
    'https://static.cninfo.com.cn.evil.test/finalpage/a.PDF',
    'https://static.cninfo.com.cn:9443/finalpage/a.PDF',
    'https://static.cninfo.com.cn/finalpage/../a.PDF',
    'https://static.cninfo.com.cn/finalpage/%2e%2e/a.PDF',
])
def test_document_url_must_be_fixed_public_disclosure_host(url):
    with pytest.raises(ValueError):
        module().extract_announcement(url)


def pdf_bytes(pages=2):
    fitz = pytest.importorskip('pymupdf')
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f'Annual report page {n + 1}: revenue 100 million.')
    return doc.tobytes()


def test_pdf_text_retains_page_locations_and_truncation():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pdf_bytes())))
    result = module().extract_announcement('https://static.cninfo.com.cn/finalpage/2026-01-01/a.PDF', client=client, max_pages=1)
    assert result['total_pages'] == 2
    assert result['pages'] == 1
    assert result['truncated'] is True
    assert '第 1 页' in result['text']
    assert 'revenue 100 million' in result['text']
    assert 'page 2' not in result['text']


def test_document_redirect_is_not_followed():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(ValueError, match='重定向'):
        module().extract_announcement('https://static.cninfo.com.cn/finalpage/a.PDF', client=client)
    assert len(requests) == 1


def test_html_and_large_documents_are_rejected():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'<html>verify</html>')))
    with pytest.raises(ValueError, match='PDF'):
        module().extract_announcement('https://static.cninfo.com.cn/finalpage/a.PDF', client=client)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'%PDF-' + b'x' * 1024)))
    with pytest.raises(ValueError, match='大小'):
        module().extract_announcement('https://static.cninfo.com.cn/finalpage/a.PDF', client=client, max_bytes=100)


def test_many_blank_pages_are_not_saved_as_report_body():
    fitz = pytest.importorskip('pymupdf')
    doc = fitz.open()
    for _ in range(30):
        doc.new_page()
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=doc.tobytes())))
    with pytest.raises(ValueError, match='文本'):
        module().extract_announcement('https://static.cninfo.com.cn/finalpage/a.PDF', client=client)
