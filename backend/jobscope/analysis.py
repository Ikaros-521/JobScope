"""Conservative, explainable lexical matching; this is not semantic assessment."""

import re

import yaml

SKILLS = ("Python", "JavaScript", "TypeScript", "Java", "C++", "C#", "Golang", "Go", "Rust", "SQL", "MySQL", "PostgreSQL", "Redis", "MongoDB", "Docker", "Kubernetes", "Linux", "React", "Vue", "FastAPI", "Django", "Flask", "Spring", "Kafka", "Spark", "AWS", "Azure", "Git", "LLM", "RAG", "机器学习", "深度学习", "数据分析", "项目管理", "产品设计", "用户研究")


def parse_resume(text, format):
    if format == "text":
        return [{"id": "RESUME", "statement": text}] if text.strip() else []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("facts.yaml 语法无效") from exc
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise ValueError("facts.yaml 必须包含 facts 列表")
    if len(data["facts"]) > 2000 or any(not isinstance(fact, dict) for fact in data["facts"]):
        raise ValueError("facts 列表格式或数量无效")
    facts = []
    for fact in data["facts"]:
        if fact.get("status") == "confirmed":
            if not isinstance(fact.get("statement"), str) or not isinstance(fact.get("id"), str):
                raise ValueError("已确认事实必须包含字符串 id 与 statement")
            facts.append({"id": fact["id"], "statement": fact["statement"]})
    return facts


def contains(text, token):
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", text, re.I))


def parse_salary(text):
    result = dict(raw=text or "", currency=None, monthly_min=None, monthly_max=None, guaranteed_months=None, guaranteed_annual_min=None, guaranteed_annual_max=None, actual_salary=None, sample_count=0, note="招聘报价不等于实际到手薪资；缺少员工独立样本。")
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-–~至]\s*(\d+(?:\.\d+)?)\s*([kK万千])", text or "")
    if not match or re.search(r"[/／]\s*(天|日|小时|时)|年薪|[/／]\s*年|per\s+(year|hour)|annual|hourly|\$|\b(?:USD|EUR|HKD|GBP|AUD|SGD|JPY|CAD)\b|港币|港元|美元|欧元|日元|英镑|澳元|加元|新加坡元|€|£", text or "", re.I):
        return result
    factor = {"k": 1000, "万": 10000, "千": 1000}[match[3].lower()]
    low, high = float(match[1]) * factor, float(match[2]) * factor
    if low > high:
        return result
    result.update(currency="CNY", monthly_min=low, monthly_max=high)
    # Numeric months alone is not proof of guarantee; only explicit wording counts.
    guarantee = re.search(r"(?:固定|保证|保底)\s*(\d{2})\s*薪", text)
    if guarantee and not re.search(r"最高|最多|视绩效|不保证|不保底|非固定", text):
        months = int(guarantee[1])
        if 12 <= months <= 24:
            result.update(guaranteed_months=months, guaranteed_annual_min=low * months, guaranteed_annual_max=high * months)
    return result


def analyze(job, resume, company_evidence):
    facts = parse_resume(resume["text"], resume["format"])
    description = job["description"]
    terms = [skill for skill in SKILLS if contains(description, skill)]
    # Java in JavaScript and SQL in MySQL are avoided by token boundaries.
    requirements = []
    for term in terms:
        matches = [fact for fact in facts if contains(fact["statement"], term) and not re.search(r"(?:不会|未掌握|不熟悉|没有.{0,3}经验)\s*" + re.escape(term), fact["statement"], re.I)]
        requirements.append(dict(requirement=term, status="met" if matches else "unknown", evidence=[fact["id"] for fact in matches], dimension="skill"))
    met = [r for r in requirements if r["status"] == "met"]
    unknown = [r for r in requirements if r["status"] == "unknown"]
    coverage = len(met) / len(requirements) if requirements else 0
    # A score covering only keywords would hide unexamined hard requirements.
    # Keep aggregate score withheld until semantic/structured requirements exist.
    requirements.extend(dict(requirement=line.strip(" -•\t"), status="unknown", evidence=[], dimension="experience") for line in description.splitlines() if re.search(r"\d+\s*年|本科|硕士|博士|大专|必须|硬性|years? of|degree", line, re.I))
    if requirements:
        coverage = len(met) / len(requirements)
    evidence_ids = list(job.get("evidence_ids", [])) + [e["id"] for e in company_evidence]
    return dict(
        job_id=job["id"], resume_revision=resume["revision"], score=None,
        coverage=round(coverage, 4), method="rule_assisted", rule_version="lexical-1",
        requirements=requirements,
        strengths=[f"简历材料包含 {r['requirement']}；仅确认词项出现，熟练程度需进一步核实。" for r in met],
        gaps=[f"{r['requirement']}：现有已确认材料未找到对应词项，标为未知。" for r in unknown],
        questions=[f"请核实岗位对 {r['requirement']} 的实际要求及你的对应项目证据。" for r in unknown[:6]] + ["请核实薪资固定部分、奖金条件、岗位主体、团队人数及加班安排。"],
        salary=parse_salary(job.get("salary", "")),
        company_summary="已保存公司资料，需阅读原文核实口径；公告元信息不等于已解析财报正文。" if company_evidence else "尚无该公司独立资料，员工情况、业绩和前景未知。",
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        limitations=["首版使用词项匹配，未对职责、行业、偏好和能力深度进行语义判断，因此不生成综合适配分数或录用概率。", "未出现的能力按未知处理；不据此认定你不具备。"],
    )
