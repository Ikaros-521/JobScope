import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.analysis import analyze, parse_salary
from jobscope.chat import answer_question, validate_model_url
from jobscope.repository import Repository


def test_job_dedup_and_changed_evidence_are_retained(tmp_path):
    repo = Repository(tmp_path)
    payload = dict(title="Python工程师", company="甲公司", description="Python SQL", url="https://example.com/jobs/1")
    first = repo.save_job(payload, "user_import")
    again = repo.save_job(payload, "user_import")
    changed = repo.save_job({**payload, "description": "Python SQL Redis"}, "user_import")
    assert first["id"] == again["id"] == changed["id"]
    assert len(repo.jobs()) == 1
    assert len(repo.job_evidence(first["id"])) == 2


def test_company_filter_is_exact_and_does_not_merge_subsidiary(tmp_path):
    repo = Repository(tmp_path)
    repo.save_evidence(dict(title="母公司", company="甲公司", text="集团营收", kind="disclosure"))
    repo.save_evidence(dict(title="子公司", company="甲公司科技", text="团队人数", kind="user_material"))
    assert [e["title"] for e in repo.evidence(company="甲公司")] == ["母公司"]


def test_resume_revision_changes_only_with_content(tmp_path):
    repo = Repository(tmp_path)
    first = repo.save_resume("Python", "text")
    assert repo.save_resume("Python", "text")["revision"] == first["revision"]
    assert repo.save_resume("Python SQL", "text")["revision"] == first["revision"] + 1


def test_unconfirmed_yaml_facts_do_not_match_requirements():
    text = "facts:\n  - id: F-1\n    status: unconfirmed\n    statement: 熟悉 Python\n"
    report = analyze(dict(id="J-1", title="工程师", company="甲", description="熟悉 Python、SQL、Redis", salary=""), dict(text=text, format="facts_yaml", revision=1), [])
    assert all(item["status"] == "unknown" for item in report["requirements"])
    assert report["score"] is None
    assert report["coverage"] == 0


def test_rules_match_confirmed_facts_but_missing_skills_remain_unknown():
    text = "facts:\n  - id: F-1\n    status: confirmed\n    statement: 熟悉 Python 和 SQL\n"
    report = analyze(dict(id="J-1", title="工程师", company="甲", description="熟悉 Python、SQL、Redis", salary=""), dict(text=text, format="facts_yaml", revision=1), [])
    results = {r["requirement"].lower(): r for r in report["requirements"]}
    assert results["python"]["status"] == "met"
    assert "F-1" in results["python"]["evidence"]
    assert results["redis"]["status"] == "unknown"
    assert report["method"] == "rule_assisted"
    assert report["coverage"] == pytest.approx(2 / 3, abs=0.01)


def test_salary_does_not_count_maximum_months_as_guaranteed():
    maximum = parse_salary("20-30K·最高16薪")
    guaranteed = parse_salary("20-30K，固定13薪")
    assert maximum["guaranteed_annual_min"] is None
    assert maximum["guaranteed_months"] is None
    assert guaranteed["guaranteed_annual_min"] == 260000
    assert guaranteed["guaranteed_annual_max"] == 390000
    assert parse_salary("面议")["monthly_min"] is None
    assert parse_salary("200-300元/天")["guaranteed_annual_min"] is None


@pytest.mark.parametrize("currency", ["HKD", "GBP", "AUD", "SGD", "JPY", "CAD", "港币", "港元", "美元", "欧元", "日元", "英镑"])
def test_non_cny_salaries_are_not_labeled_cny(currency):
    result = parse_salary(f"20-30K {currency}/月")
    assert result["currency"] is None
    assert result["monthly_min"] is None


def test_chat_without_model_is_explicit_evidence_search(monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    evidence = [dict(id="E-1", title="年度公告", url="https://example.com", text="营收增长10%，期间为2025年度", company="甲", kind="disclosure")]
    result = answer_question("营收情况", evidence, resume=None)
    assert result["mode"] == "evidence_search"
    assert result["citations"][0]["id"] == "E-1"
    assert "未配置" in result["answer"]


def test_model_url_rejects_plain_http_remote():
    with pytest.raises(ValueError):
        validate_model_url("http://example.com/v1/chat/completions")
    assert validate_model_url("http://127.0.0.1:8001/v1/chat/completions")


def test_model_chat_rejects_out_of_scope_citations(monkeypatch):
    import httpx
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test-model")
    def fake_post(self, *args, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"泄漏 [E-foreign]","evidence_ids":["E-foreign"]}'}}]}, request=httpx.Request("POST", args[0]))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(ValueError, match="引用"):
        answer_question("情况", [dict(id="E-1", title="当前资料", text="内容", url="", company="甲")], resume=None)


def test_search_can_retrieve_later_pdf_pages_with_page_marker(monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    evidence = [dict(id="E-1", title="年度报告", url="https://example.com", text="【第 1 页】\n" + "一般介绍。" * 2000 + "\n【第 9 页】\n员工总数为1200人，研发人员800人。", company="甲", kind="disclosure")]
    result = answer_question("员工总数", evidence)
    assert "员工总数为1200人" in result["answer"]
    assert "第 9 页" in result["citations"][0]["excerpt"]


def test_model_chat_null_content_is_a_controlled_failure(monkeypatch):
    import httpx
    monkeypatch.setenv("JOBSCOPE_LLM_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("JOBSCOPE_LLM_MODEL", "test-model")
    def fake_post(self, *args, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]}, request=httpx.Request("POST", args[0]))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(ValueError, match="格式"):
        answer_question("情况", [dict(id="E-1", title="当前资料", text="内容", url="", company="甲")])
