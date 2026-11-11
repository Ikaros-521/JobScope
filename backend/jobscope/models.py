"""Validated public request contracts.

Credential fields are write-only: they are accepted here, stored locally and never
echoed back by any read endpoint (see `/api/reliability/sources`).
"""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JobInput(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=100000)
    location: str = Field(default="", max_length=300)
    salary: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=2000)

    @field_validator("url")
    @classmethod
    def valid_link(cls, value):
        if value and (urlsplit(value).scheme not in {"https", "http"} or not urlsplit(value).hostname):
            raise ValueError("资料链接必须是 HTTP(S) URL")
        if value and "@" in urlsplit(value).netloc:
            raise ValueError("资料链接不能包含账号密码")
        return value


class EvidenceInput(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=300)
    kind: Literal["disclosure", "company_statement", "employee_statement", "media_report", "user_material", "announcement"] = "user_material"
    text: str = Field(min_length=1, max_length=150000)
    url: str = Field(default="", max_length=2000)
    published_at: str | None = Field(default=None, max_length=100)
    valid_link = field_validator("url")(JobInput.valid_link.__func__)


class ResumeInput(RequestModel):
    text: str = Field(max_length=150000)
    format: Literal["text", "facts_yaml"] = "text"


class CollectInput(RequestModel):
    source: Literal["remotive", "boss", "cninfo", "liepin", "zhaopin", "51job"]
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=100)
    limit: int = Field(default=20, ge=1, le=1000)


class CompanyResearchInput(RequestModel):
    """公司情报多平台检索：以精确公司名为关键词跑公告与岗位渠道。"""
    company: str = Field(min_length=1, max_length=100)


class BrowserCollectInput(RequestModel):
    source: Literal["boss", "zhaopin", "51job"] = "boss"
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=100)
    limit: int = Field(default=20, ge=1, le=1000)


class CompanyLookupInput(RequestModel):
    """公司快查：公开网页快照，渠道与公司名都从严校验。"""
    channel: Literal["qcc", "tyc"]
    company: str = Field(min_length=1, max_length=120)


class AnalyzeInput(RequestModel):
    job_id: str = Field(min_length=1, max_length=100)


class DetailBatchInput(RequestModel):
    limit: int = Field(default=500, ge=1, le=500)


class JobBatchUpdate(RequestModel):
    """批量规范化字段：缺省的字段保持不变，显式传空串视为清空。"""
    ids: list[str] = Field(min_length=1, max_length=1000)
    location: str | None = Field(default=None, max_length=300)
    salary: str | None = Field(default=None, max_length=300)
    tags: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=50)


class JobBatchDelete(RequestModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class IndexInput(RequestModel):
    operation: Literal['sync', 'rebuild', 'embed'] = 'sync'


class HistoryItem(RequestModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12000)


class ChatInput(RequestModel):
    message: str = Field(min_length=1, max_length=12000)
    job_id: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=300)
    history: list[HistoryItem] = Field(default_factory=list, max_length=12)
    scope: Literal['scoped', 'global'] = 'scoped'
    agent_mode: Literal['research', 'general'] = 'research'
    conversation_id: str | None = Field(default=None, max_length=100)


class ChatRunInput(RequestModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=100)
    agent_mode: Literal['research', 'general'] = 'research'
    model: str = Field(default='', max_length=200)
    edit_message_id: str | None = Field(default=None, max_length=50)
    company: str = Field(default='', max_length=300)
    job_id: str | None = Field(default=None, max_length=100)


class ConversationPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal['research', 'general'] | None = None
    model: str | None = Field(default=None, max_length=200)


class ReliabilityInput(RequestModel):
    # 统一社会信用代码是实体归一的强键；留空时按规范化名称匹配，绝不模糊合并。
    credit_code: str = Field(default='', max_length=40)


class SignalCollectInput(RequestModel):
    source: str = Field(min_length=2, max_length=40)
    credit_code: str = Field(default='', max_length=40)


class SignalExtractInput(RequestModel):
    limit: int = Field(default=12, ge=1, le=40)
    max_tokens: int = Field(default=1200, ge=256, le=4000)
    credit_code: str = Field(default='', max_length=40)


class SignalSourceInput(RequestModel):
    """Paid tiers stay opt-in; `key` is accepted on write only and never returned."""
    mode: Literal['disabled', 'enabled']
    key: str = Field(default='', max_length=4000)
    url: str = Field(default='', max_length=2000)
