import io
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as value:
        yield value


def upload(client, filename, content):
    return client.post('/api/resume/import', params={'filename': filename}, content=content, headers={'Content-Type': 'application/octet-stream'})


def docx_bytes(xml=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', xml or '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>张三 · AI Agent 工程师</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Python / RAG</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:del><w:r><w:delText>删除的经历</w:delText></w:r></w:del><w:r><w:t>项目经验</w:t></w:r></w:p></w:body></w:document>''')
        archive.writestr('word/header1.xml', '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>简历页眉</w:t></w:r></w:p></w:hdr>')
    return out.getvalue()


def test_docx_extracts_body_table_and_header_without_saving(client):
    client.put('/api/resume', json={'text': '原简历', 'format': 'text'})
    response = upload(client, '简历.DOCX', docx_bytes())
    assert response.status_code == 200
    result = response.json()
    assert all(text in result['text'] for text in ['张三', 'Python / RAG', '项目经验', '简历页眉'])
    assert '删除的经历' not in result['text']
    assert result['format'] == 'text' and result['characters'] == len(result['text'])
    assert client.get('/api/resume').json()['text'] == '原简历'
    client.put('/api/resume', json={'text': result['text'], 'format': result['format']})
    assert 'Python' in client.get('/api/resume').json()['text']


def test_pdf_extracts_pages_and_reports_empty_scan(client):
    import pymupdf
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), 'Python developer / Agent experience')
        document.new_page().insert_text((72, 72), 'RAG project and FastAPI')
        result = upload(client, 'resume.pdf', document.tobytes())
        assert result.status_code == 200
        assert 'FastAPI' in result.json()['text'] and result.json()['pages'] == 2
    with pymupdf.open() as document:
        document.new_page()
        result = upload(client, 'scan.pdf', document.tobytes())
        assert result.status_code == 422 and '扫描' in result.json()['detail']


@pytest.mark.parametrize('filename,content,expected', [
    ('resume.txt', '中文简历 Python'.encode('gb18030'), '中文简历'),
    ('resume.md', '# 简历\nPython'.encode('utf-8-sig'), '# 简历'),
    ('resume.txt', '开发经历'.encode('utf-16'), '开发经历'),
    ('facts.yaml', b'facts:\n  - id: F-1\n    status: confirmed\n    statement: Python', 'confirmed'),
], ids=['gb18030', 'markdown', 'utf16', 'facts'])
def test_text_encodings_and_facts(client, filename, content, expected):
    result = upload(client, filename, content)
    assert result.status_code == 200 and expected in result.json()['text']
    assert result.json()['format'] == ('facts_yaml' if filename.endswith('.yaml') else 'text')


@pytest.mark.parametrize('filename,content,reason', [
    ('bad.pdf', b'not pdf', 'PDF'), ('bad.docx', b'not zip', 'Word'),
    ('old.doc', b'old word', '.docx'), ('a.exe', b'xyz', '格式'),
    ('empty.txt', b'   ', '为空'), ('a.yaml', b'bad: data', 'facts'),
    ('long.txt', b'a' * 100001, '100,000'),
], ids=['invalid-pdf', 'invalid-docx', 'legacy-doc', 'unsupported', 'empty', 'invalid-facts', 'long-text'])
def test_invalid_import_is_actionable_and_preserves_saved_resume(client, filename, content, reason):
    client.put('/api/resume', json={'text': 'keep me', 'format': 'text'})
    result = upload(client, filename, content)
    assert result.status_code == 422 and reason in result.json()['detail']
    assert client.get('/api/resume').json()['text'] == 'keep me'


def test_upload_size_and_docx_inflation_are_bounded(client):
    result = upload(client, 'large.pdf', b'x' * (10 * 1024 * 1024 + 1))
    assert result.status_code == 413 and '10 MB' in result.json()['detail']
    result = upload(client, 'inflated.docx', docx_bytes(' ' * (21 * 1024 * 1024)))
    assert result.status_code == 422 and '解压' in result.json()['detail']


def test_encrypted_pdf_has_specific_message(client):
    import pymupdf
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), 'Protected resume')
        content = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner-fixture', user_pw='user-fixture')
    result = upload(client, 'protected.pdf', content)
    assert result.status_code == 422 and '密码' in result.json()['detail']


def test_stream_without_length_is_bounded_and_cross_origin_is_blocked(client):
    response = client.post('/api/resume/import?filename=large.pdf', content=(b'x' * (1024 * 1024) for _ in range(11)), headers={'Content-Type': 'application/octet-stream'})
    assert response.status_code == 413
    response = client.post('/api/resume/import?filename=a.txt', content=b'Python', headers={'Origin': 'https://other.example'})
    assert response.status_code == 403


def test_alternate_content_textbox_is_extracted_once(client):
    xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body><mc:AlternateContent><mc:Choice Requires="wps"><w:p><w:r><w:t>Python项目经历</w:t></w:r></w:p></mc:Choice><mc:Fallback><w:p><w:r><w:t>Python项目经历</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent></w:body></w:document>'''
    result = upload(client, 'textbox.docx', docx_bytes(xml))
    assert result.status_code == 200 and result.json()['text'].count('Python项目经历') == 1


def test_deep_yaml_and_corrupt_compression_have_clear_errors(client, monkeypatch):
    nested = b'facts: ' + b'[' * 1500 + b']' * 1500
    response = upload(client, 'facts.yaml', nested)
    assert response.status_code == 422 and '层级' in response.json()['detail']
    import zlib
    def broken(*args, **kwargs):
        raise zlib.error('fixture invalid compressed content')
    monkeypatch.setattr(zipfile.ZipFile, 'read', broken)
    response = upload(client, 'broken.docx', docx_bytes())
    assert response.status_code == 422 and 'Word' in response.json()['detail']


def test_utf16_word_entity_declarations_are_rejected(client):
    xml = '''<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE document [<!ENTITY text "Python">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&text;</w:t></w:r></w:p></w:body></w:document>'''.encode('utf-16')
    result = upload(client, 'entity.docx', docx_bytes(xml))
    assert result.status_code == 422 and '声明' in result.json()['detail']
