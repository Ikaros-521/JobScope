"""Local UI acceptance; only touches the isolated service on port 8766."""
import io
import json
import zipfile
from pathlib import Path

import pymupdf
from playwright.sync_api import sync_playwright, expect

OUT = Path(__file__).resolve().parents[1] / 'output' / 'resume-import-ui'
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'http://127.0.0.1:8766'
with pymupdf.open() as document:
    document.new_page().insert_text((72, 72), 'Candidate: Python developer\nAgent / RAG / FastAPI experience')
    pdf = document.tobytes()
with pymupdf.open() as document:
    document.new_page()
    scan = document.tobytes()
word = io.BytesIO()
with zipfile.ZipFile(word, 'w') as archive:
    archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>张三：AI Agent 工程师，Python 与 RAG 项目经历</w:t></w:r></w:p></w:body></w:document>')

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.request.put(BASE + '/api/resume', data={'text': 'Existing resume', 'format': 'text'})
    page.goto(BASE)
    page.get_by_role('navigation', name='主导航').get_by_role('button', name='我的简历', exact=True).click()
    editor = page.get_by_label('简历正文 · 可直接编辑或粘贴', exact=True)
    expect(editor).to_have_value('Existing resume')
    page.get_by_label('简历文件', exact=True).set_input_files({'name': '候选人简历.pdf', 'mimeType': 'application/pdf', 'buffer': pdf})
    expect(page.get_by_label('提取文本预览', exact=True)).to_contain_text('Python')
    expect(editor).to_have_value('Existing resume')
    assert page.request.get(BASE + '/api/resume').json()['text'] == 'Existing resume'
    page.get_by_role('button', name='替换编辑区内容', exact=True).click()
    expect(editor).to_contain_text('Python')
    assert page.request.get(BASE + '/api/resume').json()['text'] == 'Existing resume'
    page.get_by_role('button', name='保存到本地', exact=False).click()
    expect(page.get_by_text('简历已保存在本地。岗位适配分析将使用这个版本。', exact=True)).to_be_visible()
    assert 'Python' in page.request.get(BASE + '/api/resume').json()['text']
    page.get_by_label('简历文件', exact=True).set_input_files({'name': 'scan.pdf', 'mimeType': 'application/pdf', 'buffer': scan})
    expect(page.get_by_role('alert')).to_contain_text('扫描件')
    expect(editor).to_contain_text('Python')
    page.get_by_label('简历文件', exact=True).set_input_files({'name': '旧版.doc', 'mimeType': 'application/msword', 'buffer': b'legacy'})
    expect(page.get_by_role('alert')).to_contain_text('另存为 .docx')
    page.get_by_label('简历文件', exact=True).set_input_files({'name': '张三简历.docx', 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': word.getvalue()})
    expect(page.get_by_label('提取文本预览', exact=True)).to_contain_text('张三')
    page.screenshot(path=str(OUT / '01-desktop-preview.png'), full_page=True)
    page.get_by_role('button', name='取消导入', exact=True).click()
    expect(editor).to_contain_text('Python')
    page.get_by_label('简历文件', exact=True).set_input_files({'name': '张三简历.docx', 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': word.getvalue()})
    expect(page.get_by_label('提取文本预览', exact=True)).to_contain_text('张三')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(OUT / '02-mobile-preview.png'), full_page=True)
    page.get_by_role('button', name='替换编辑区内容', exact=True).click()
    expect(editor).to_contain_text('张三')
    assert not errors, errors
    print(json.dumps(dict(resume_import_ui='passed', pdf_docx='passed', preview_apply_save='passed', cancel_and_failure_preserve='passed', mobile_overflow=False, page_errors=errors)))
    browser.close()
