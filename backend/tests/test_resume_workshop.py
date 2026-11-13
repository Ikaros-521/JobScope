import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


BASE_RESUME = "# 张三\n\n13800000000 · zhangsan@example.com\n\n## 专业技能\n\n- Python、SQL、Redis\n\n## 工作经历\n\n**2020-2024 某公司 后端开发**\n\n- 主导缓存重构，响应时间下降 42%"
MODEL_REPLY = "<<<RESUME>>>\n# 张三\n\n13800000000\n\n## 专业技能\n\n- Python、SQL、Redis 高并发服务\n\n## 工作经历\n\n**2020-2024 某公司 后端开发**\n\n- 主导缓存重构，响应时间下降 42%\n<<<NOTES>>>\n- 前置了与岗位对齐的技能表述\n- 删减了与该岗位无关的次要经历"


def configure_model(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')


def mock_optimize_model(monkeypatch, content=MODEL_REPLY):
    # TestClient 也基于 httpx.Client：无 messages 的请求（如 API 自身调用）原样放行。
    captured = {}
    original = httpx.Client.post

    def post(self, url, **kwargs):
        payload = kwargs.get('json') or {}
        if 'messages' not in payload:
            return original(self, url, **kwargs)
        captured['system'] = payload['messages'][0]['content']
        captured['user'] = payload['messages'][1]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    return captured


def test_templates_listed_and_preview_renders(client):
    templates = client.get("/api/resume-templates").json()["templates"]
    assert [item["id"] for item in templates] == ["classic", "modern", "compact"]
    preview = client.post("/api/resume-templates/preview", json=dict(markdown="# 张三\n\n138\n\n## 技能\n\n- Python", template_id="modern"))
    assert preview.status_code == 200
    html = preview.json()["html"]
    assert "张三" in html and 'class="contact"' in html and "resume-page" in html
    # 模板差异确实体现在产物里（modern 的蓝色侧标 CSS）
    assert "0b5cbd" in html
    assert client.post("/api/resume-templates/preview", json=dict(markdown="# x", template_id="unknown")).status_code == 400


def test_version_crud_roundtrip(client):
    created = client.post("/api/resume-versions", json=dict(title="张三 · 甲公司", markdown=BASE_RESUME, template_id="modern", company="甲公司", notes=["优化说明"]))
    assert created.status_code == 200
    version = created.json()
    assert version["title"] == "张三 · 甲公司" and version["notes"] == ["优化说明"]
    listing = client.get("/api/resume-versions").json()["versions"]
    assert len(listing) == 1 and listing[0]["characters"] == len(BASE_RESUME)
    updated = client.put(f"/api/resume-versions/{version['id']}", json=dict(markdown="# 李四\n\n内容更新", template_id="compact"))
    assert updated.status_code == 200
    assert updated.json()["template_id"] == "compact" and updated.json()["title"] == "张三 · 甲公司"
    detail = client.get(f"/api/resume-versions/{version['id']}")
    assert "# 李四" in detail.json()["markdown"]
    assert client.put("/api/resume-versions/999", json=dict(markdown="# x")).status_code == 404
    assert client.delete(f"/api/resume-versions/{version['id']}").json()["ok"] is True
    assert client.get(f"/api/resume-versions/{version['id']}").status_code == 404


def test_optimize_creates_version_with_notes_and_redline_prompt(client, monkeypatch):
    configure_model(monkeypatch)
    captured = mock_optimize_model(monkeypatch)
    client.put("/api/resume", json=dict(text=BASE_RESUME, format="text"))
    result = client.post("/api/resume-versions/optimize", json=dict(
        job_id="", jd_text="负责高并发后端服务，熟悉 Python 与缓存优化", company="字节跳动", job_title="后端开发工程师", instructions="突出高并发"))
    assert result.status_code == 200
    version = result.json()
    assert version["title"] == "后端开发工程师 · 字节跳动"
    assert version["markdown"].startswith("# 张三")
    assert version["notes"] == ["前置了与岗位对齐的技能表述", "删减了与该岗位无关的次要经历"]
    # 红线文案必须进入 system 提示；JD 与基础简历必须完整投喂。
    assert "严禁虚构" in captured["system"] and "不得写入" in captured["system"]
    payload = json.loads(captured["user"])
    assert payload["base_resume"] == BASE_RESUME
    assert "高并发后端服务" in payload["target"]["requirements"]
    # 落库可查，优化说明随版本保留。
    stored = client.get(f"/api/resume-versions/{version['id']}").json()
    assert stored["notes"] == version["notes"] and stored["jd_digest"]


def test_optimize_from_saved_job_and_input_guards(client, monkeypatch):
    configure_model(monkeypatch)
    mock_optimize_model(monkeypatch)
    job = client.post("/api/import/job", json=dict(title="后端开发", company="乙公司", description="负责分布式服务，熟悉 SQL 优化")).json()["job"]
    # 未保存简历且未粘贴 → 400 引导；无 JD → 400。
    assert client.post("/api/resume-versions/optimize", json=dict(job_id=job["id"])).status_code == 400
    client.put("/api/resume", json=dict(text=BASE_RESUME, format="text"))
    assert client.post("/api/resume-versions/optimize", json=dict()).status_code == 400
    # 选岗位后自动取 JD 与公司岗位名。
    result = client.post("/api/resume-versions/optimize", json=dict(job_id=job["id"]))
    assert result.status_code == 200
    assert result.json()["company"] == "乙公司" and result.json()["title"] == "后端开发 · 乙公司"


def test_optimize_without_model_or_malformed_reply(client, monkeypatch):
    # 未配置分析模型 → 明确引导，而非 5xx 崩溃（先备好简历，避免先撞简历守卫）。
    client.put("/api/resume", json=dict(text=BASE_RESUME, format="text"))
    guard = client.post("/api/resume-versions/optimize", json=dict(jd_text="熟悉 Python"))
    assert guard.status_code == 400 and "模型设置" in guard.json()["detail"]
    # 模型返回缺分隔符：全文当简历、说明为空，不失败。
    configure_model(monkeypatch)
    mock_optimize_model(monkeypatch, "# 王五\n\n没有分隔符的裸简历内容，但长度足够视为有效。")
    client.put("/api/resume", json=dict(text=BASE_RESUME, format="text"))
    result = client.post("/api/resume-versions/optimize", json=dict(jd_text="熟悉 Python"))
    assert result.status_code == 200
    assert result.json()["markdown"].startswith("# 王五") and result.json()["notes"] == []
    # 模型返回过短内容 → 502 可读错误。
    mock_optimize_model(monkeypatch, "<<<RESUME>>>\n太短\n<<<NOTES>>>")
    assert client.post("/api/resume-versions/optimize", json=dict(jd_text="熟悉 Python")).status_code == 502


def test_export_all_formats(client):
    version = client.post("/api/resume-versions", json=dict(title="张三 · 甲公司", markdown=BASE_RESUME, template_id="modern", company="甲公司")).json()
    export = client.get(f"/api/resume-versions/{version['id']}/export?format=md")
    assert export.status_code == 200 and export.text == BASE_RESUME
    assert export.headers["content-disposition"].startswith("attachment")
    html = client.get(f"/api/resume-versions/{version['id']}/export?format=html")
    assert "resume-page" in html.text and "张三" in html.text
    pdf = client.get(f"/api/resume-versions/{version['id']}/export?format=pdf")
    assert pdf.content[:5] == b"%PDF-"
    import pymupdf
    with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "张三" in text and "响应时间下降 42%" in text and "订单" not in text
    docx = client.get(f"/api/resume-versions/{version['id']}/export?format=docx")
    assert docx.content[:2] == b"PK"
    import io
    from docx import Document
    document = Document(io.BytesIO(docx.content))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    assert any("张三" in item for item in paragraphs)
    assert any("Python、SQL、Redis" in item for item in paragraphs)
    assert client.get(f"/api/resume-versions/{version['id']}/export?format=exe").status_code == 422
    assert client.get("/api/resume-versions/999/export?format=pdf").status_code == 404
