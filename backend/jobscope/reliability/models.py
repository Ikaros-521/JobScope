"""Typed contracts shared by collectors, the rubric and the API."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Dimension = Literal['stability', 'pay', 'people', 'professional', 'industry', 'operations']
SourceClass = Literal['official', 'paid_api', 'platform', 'user_material', 'employee', 'media']
# Employee and media wording is a claim about somebody else's experience; it may not settle a dimension on its own.
SOLO_EVIDENCE = {'official', 'paid_api', 'platform', 'user_material'}


def can_stand_alone(source_class: str) -> bool:
    return source_class in SOLO_EVIDENCE


class SignalInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    company_id: str = Field(min_length=4, max_length=60)
    dimension: Dimension
    rule: str = Field(min_length=3, max_length=60, pattern=r'^[a-z0-9_]+$')
    value: dict = Field(default_factory=dict)
    unit: str = Field(default='', max_length=30)
    source_class: SourceClass
    # 计划原文写的 min_length=4 会把测试与后续用例里的占位编号 'E-1' 拒掉；这里只要求非空，真实编号仍是 E- + 24 位摘要。
    evidence_id: str = Field(min_length=1, max_length=80)
    observed_at: str = Field(default='', max_length=40)
    collected_at: str = Field(min_length=4, max_length=40)


class RuleFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rule: str
    dimension: Dimension
    score: float = Field(ge=0, le=100)
    template: str
    data: dict = Field(default_factory=dict)
    evidence_id: str
    source_class: SourceClass
    weight: float = Field(gt=0, le=1)


class ReportFinding(BaseModel):
    """A rendered sentence plus the pointer back to its source. This is what gets persisted."""
    model_config = ConfigDict(extra='forbid')
    text: str
    rule: str
    score: float = Field(ge=0, le=100)
    source_class: SourceClass
    evidence_id: str


class DimensionScore(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dimension: Dimension
    label: str
    score: float | None
    confidence: float = Field(ge=0, le=1)
    findings: list[ReportFinding]
    evidence_ids: list[str]
    missing: list[str]

# 注意命名分工：`rubric.score()` 返回的是 **Task 4 定义的轻量 dataclass** `DimensionOutcome`（`findings` 里装的是未渲染的 `RuleFinding`），
# `DimensionScore` 只在渲染之后由 **Task 6 的 service** 构造，用于持久化与 API。两者不要混用。


class ReliabilityReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    company_id: str
    company: str
    nature: Literal['subjective_reference'] = 'subjective_reference'
    score: float | None
    confidence: float = Field(ge=0, le=1)
    dimensions: list[DimensionScore]
    rubric_version: str
    wording_version: str
    disclaimer_version: str
    computed_at: str
    input_hash: str
    evidence_ids: list[str]
    limitations: list[str]


DISCLAIMER = '本页面是 JobScope 依据有限公开材料生成的主观参考，不构成对该公司的事实认定、信用评价或经营风险结论。每条依据都标出来源与抓取时间，请以可核实的原文为准，并自行在面试与背调中确认。'
DISCLAIMER_VERSION = 'disclaimer-1'
