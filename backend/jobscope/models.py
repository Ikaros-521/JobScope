"""Validated public request contracts; no credential input is accepted here."""

from typing import Literal
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
    source: Literal["remotive", "boss", "cninfo"]
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=100)
    limit: int = Field(default=20, ge=1, le=50)


class BrowserCollectInput(RequestModel):
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=100)
    limit: int = Field(default=20, ge=1, le=50)


class AnalyzeInput(RequestModel):
    job_id: str = Field(min_length=1, max_length=100)


class DetailBatchInput(RequestModel):
    limit: int = Field(default=500, ge=1, le=500)


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


class ConversationPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal['research', 'general'] | None = None
    model: str | None = Field(default=None, max_length=200)
