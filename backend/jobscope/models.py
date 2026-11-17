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


class KnowledgeInput(RequestModel):
    """用户录入的知识文档：Agent 对话经 knowledge_search 检索引用（K-…）。上限与文件导入一致。"""
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200000)
    tags: list[Annotated[str, Field(max_length=30)]] = Field(default_factory=list, max_length=10)


class KnowledgePatch(RequestModel):
    """二次编辑：缺省字段保持不变；content 变更按该文档原策略重切。"""
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=200000)
    tags: list[Annotated[str, Field(max_length=30)]] | None = None


class KnowledgeImportInput(RequestModel):
    """批量导入任务登记：只登记文件名（前端随后逐个上传），单批最多 20 个。"""
    names: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(min_length=1, max_length=20)


class KnowledgeSettingsInput(RequestModel):
    """分块与检索设置：全部可选，缺省字段保持现值；钳制在服务端再核一道。"""
    strategy: Literal['recursive', 'hard', 'punct', 'semantic', 'llm'] | None = None
    size: int | None = Field(default=None, ge=64, le=2000)
    overlap: int | None = Field(default=None, ge=0, le=2000)
    separators: list[Annotated[str, Field(max_length=20)]] | None = Field(default=None, max_length=10)
    retrieval_mode: Literal['lexical', 'hybrid'] | None = None


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
    # 一键完备的两个收尾步骤（2026-11-17）：材料抽取把公告/完整JD里可抽的数字变成信号，
    # AI 行业判断补行业前景维度。两者默认执行；来源被禁用时任务照常收尾为 skipped，不静默。
    with_extract: bool = Field(default=True)
    with_industry_llm: bool = Field(default=True)


class WageBaselineImportInput(RequestModel):
    """人社局工资指导价位 CSV 导入（2026-11-17）：文本粘贴或文件读取后的正文，逗号/制表符分隔。"""
    csv: str = Field(min_length=1, max_length=300000)


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
    # 机会管线状态（2026-11-17）：与收藏共用一个篮子。'' 显式移出管线；None 表示不改。
    pipeline_status: Literal['interested', 'applied', 'interviewing', 'offer', 'archived', ''] | None = Field(default=None)


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
    """JD 定向优化：基础简历缺省取「我的简历」，JD 来自岗位档案或手动粘贴。
    profile_entry_ids 指定本次取材的「个人资料库」条目（完整版材料）；空列表＝不取材。"""
    resume_text: str = Field(default='', max_length=100000)
    job_id: str = Field(default='', max_length=100)
    jd_text: str = Field(default='', max_length=20000)
    company: str = Field(default='', max_length=300)
    job_title: str = Field(default='', max_length=300)
    instructions: str = Field(default='', max_length=2000)
    profile_entry_ids: list[Annotated[str, Field(min_length=1, max_length=40)]] | None = Field(default=None, max_length=30)


class ProfileEntryInput(RequestModel):
    """个人资料库条目：完整版个人材料（项目经历等工作事实的第一手记录）。"""
    category: Literal['project', 'work', 'education', 'skill', 'cert', 'award', 'other'] = 'project'
    title: str = Field(min_length=1, max_length=200)
    org: str = Field(default='', max_length=200)
    period: str = Field(default='', max_length=100)
    content: str = Field(min_length=1, max_length=20000)
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] | None = Field(default=None, max_length=20)
    enabled: bool = True


class ProfileEntryPatch(RequestModel):
    """部分更新：启停开关、内容微调；缺省字段保持不变。"""
    category: Literal['project', 'work', 'education', 'skill', 'cert', 'award', 'other'] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    org: str | None = Field(default=None, max_length=200)
    period: str | None = Field(default=None, max_length=100)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


class ObsidianImportInput(RequestModel):
    """Obsidian 库目录导入：只读该目录下的 .md，frontmatter/目录名映射类别，幂等重导原位更新。"""
    path: str = Field(min_length=2, max_length=500)
    dry_run: bool = Field(default=False)


class ManualDescriptionInput(RequestModel):
    description: str = Field(min_length=1, max_length=100000)
    source_note: str = Field(min_length=1, max_length=1000)


class ImportLinksInput(RequestModel):
    """Boss 链接粘贴导入：App 分享文本或整页复制下来的多行链接。"""
    text: str = Field(min_length=1, max_length=50000)


class BossTabPageInput(RequestModel):
    """「捕获当前页」所选的项目浏览器 Boss 标签页。"""
    tab_id: str = Field(min_length=1, max_length=100)


class ImportConfirmInput(RequestModel):
    """捕获篮确认入库：ids 缺省 = 导入篮内全部；auto_star 入库后统一加星。"""
    ids: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(default=None, max_length=500)
    auto_star: bool = False


class BossOpenInput(RequestModel):
    """在项目浏览器打开 Boss 上某公司的岗位检索（公司快查页联动入口）。"""
    company: str = Field(min_length=1, max_length=120)


class RemoteBasketInput(RequestModel):
    """油猴脚本推送：卡片字段在服务端清洗重建，page_url 用于来源校验与溯源。"""
    page_url: str = Field(min_length=1, max_length=1000)
    items: list[dict] = Field(min_length=1, max_length=200)


class JobPreferenceInput(RequestModel):
    """求职偏好申报：硬性条件（到岗方式/排班/薪资期望等）的提前答案，评估时作为已确认事实注入。"""
    dimension: Literal['skill', 'experience', 'industry', 'preference'] = 'preference'
    statement: str = Field(min_length=2, max_length=1000)
    enabled: bool = True


class JobPreferencePatch(RequestModel):
    """部分更新：启停开关、内容微调；缺省字段保持不变。"""
    dimension: Literal['skill', 'experience', 'industry', 'preference'] | None = None
    statement: str | None = Field(default=None, min_length=2, max_length=1000)
    enabled: bool | None = None


class AnalysisStatusInput(RequestModel):
    """评估适配需求行手动改判：按 jd_quote 摘要记忆，重评自动沿用。"""
    jd_quote: str = Field(min_length=1, max_length=1500)
    status: Literal['met', 'partial', 'unmet', 'unknown']
    note: str = Field(default='', max_length=500)
