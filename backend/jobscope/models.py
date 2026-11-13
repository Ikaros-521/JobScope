"""Validated public request contracts.

Credential fields are write-only: they are accepted here, stored locally and never
echoed back by any read endpoint (see `/api/reliability/sources`).
"""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .reliability.sources import SignalSourceInput as SourceSettingsInput


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
    # 岗位渠道的采集上限；默认 10 与岗位详情页一致，横评补数据传更大值。
    limit: int = Field(default=10, ge=1, le=200)
    # None selects callable, verified free sources; [] opts out of signal collection.
    signal_sources: list[Annotated[str, Field(min_length=2, max_length=40)]] | None = Field(default=None, max_length=6)


class MergeInput(RequestModel):
    """手动合并两个公司主体：company_a 为主名（吸收对方），primary 可反转方向。"""
    company_a: str = Field(min_length=1, max_length=300)
    company_b: str = Field(min_length=1, max_length=300)
    primary: Literal["a", "b"] = "a"


class MergeAcceptInput(RequestModel):
    """接受合并建议：primary=auto 时按已存岗位数取多者为主名。"""
    primary: Literal["a", "b", "auto"] = "auto"


class SplitInput(RequestModel):
    """把别名从主体拆出去独立成新实体。"""
    alias: str = Field(min_length=1, max_length=300)


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


class ModelBindingInput(RequestModel):
    """功能级模型绑定：channel 指向已存渠道+模型；embedding 另有 local；抽取/分析可 disabled/inherit。"""
    purpose: Literal["agent", "embedding", "extraction", "analysis"]
    kind: Literal["channel", "local", "disabled", "inherit"]
    channel_id: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=300)
    local_model: str | None = Field(default=None, max_length=1000)


class DetailBatchInput(RequestModel):
    limit: int = Field(default=500, ge=1, le=500)
    # ids 给出时按指定岗位反查（不限 complete 状态——反查就是要重读）；缺省照旧补采全部待完整 JD。
    ids: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(default=None, min_length=1, max_length=200)


class JobBatchUpdate(RequestModel):
    """批量规范化字段：缺省的字段保持不变，显式传空串视为清空。"""
    ids: list[str] = Field(min_length=1, max_length=1000)
    location: str | None = Field(default=None, max_length=300)
    salary: str | None = Field(default=None, max_length=300)
    tags: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=50)
    starred: bool | None = Field(default=None)


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
    agent_mode: Literal['research', 'general'] = 'general'
    conversation_id: str | None = Field(default=None, max_length=100)


class ChatRunInput(RequestModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=100)
    agent_mode: Literal['research', 'general'] = 'general'
    # 省略=沿用会话已保存的模型/渠道；空串=重置为渠道默认或全局默认。
    model: str | None = Field(default=None, max_length=200)
    llm_channel: str | None = Field(default=None, max_length=100)
    # 工具权限档位：省略=沿用会话已保存值（新会话默认 autonomous）。
    permission_mode: Literal['confirm', 'autonomous', 'full'] | None = None
    edit_message_id: str | None = Field(default=None, max_length=50)
    company: str = Field(default='', max_length=300)
    job_id: str | None = Field(default=None, max_length=100)


class ScopeExpansionInput(RequestModel):
    target: Literal['company', 'global']
    reason: str = Field(default='', max_length=2000)


class ConversationPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal['research', 'general'] | None = None
    model: str | None = Field(default=None, max_length=200)
    # 空串=跟随全局配置；非空=使用已保存渠道，llm_channels 表里必须存在
    llm_channel: str | None = Field(default=None, max_length=100)
    # 工具权限档位：confirm=逐次确认 autonomous=只读/白名单自动 full=全部自动
    permission_mode: Literal['confirm', 'autonomous', 'full'] | None = None


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


class SignalSourceInput(SourceSettingsInput):
    """Public and internal settings share validation; auth_prefix whitespace is significant."""


class ResumeVersionInput(RequestModel):
    """新建投递版本：可直接从「我的简历」复制起步，也承载优化结果的落库。"""
    title: str = Field(min_length=1, max_length=100)
    markdown: str = Field(min_length=1, max_length=60000)
    template_id: str = Field(default='classic', max_length=50)
    job_id: str = Field(default='', max_length=100)
    company: str = Field(default='', max_length=300)
    job_title: str = Field(default='', max_length=300)
    jd_digest: str = Field(default='', max_length=20000)
    base_text: str = Field(default='', max_length=100000)
    notes: list[Annotated[str, Field(max_length=600)]] = Field(default_factory=list, max_length=12)


class ResumeVersionPatch(RequestModel):
    """二次编辑：缺省字段保持不变，template_id 需为内置模板之一。"""
    title: str | None = Field(default=None, min_length=1, max_length=100)
    markdown: str | None = Field(default=None, min_length=1, max_length=60000)
    template_id: str | None = Field(default=None, max_length=50)
    notes: list[Annotated[str, Field(max_length=600)]] | None = None


class ResumePreviewInput(RequestModel):
    markdown: str = Field(min_length=1, max_length=60000)
    template_id: str = Field(default='classic', max_length=50)


class OptimizeResumeInput(RequestModel):
    """JD 定向优化：基础简历缺省取「我的简历」，JD 来自岗位档案或手动粘贴。"""
    resume_text: str = Field(default='', max_length=100000)
    job_id: str = Field(default='', max_length=100)
    jd_text: str = Field(default='', max_length=20000)
    company: str = Field(default='', max_length=300)
    job_title: str = Field(default='', max_length=300)
    instructions: str = Field(default='', max_length=2000)


class ManualDescriptionInput(RequestModel):
    description: str = Field(min_length=1, max_length=100000)
    source_note: str = Field(min_length=1, max_length=1000)
