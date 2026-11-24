"""Validated public request contracts.

Credential fields are write-only: they are accepted here, stored locally and never
echoed back by any read endpoint (see `/api/reliability/sources`).
"""

import re
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


class InterviewDrillRef(RequestModel):
    """错题引用：来源记录（mock 模拟面试场次 / real 真实面试逐字稿）+ 该记录 report.questions 的下标。"""
    source: Literal['mock', 'real']
    session_id: str = Field(min_length=1, max_length=100)
    index: int = Field(ge=0, le=500)


class InterviewCreateInput(RequestModel):
    """模拟面试开场合：岗位专家卡 × 公司风格 × 轮次 × 强度。

    目标三选一：岗位库 job_id / 粘贴 jd_text / 纯自由练（custom 卡必须给 job_title 作岗位锚点）；
    company 供自由练/粘贴 JD 时拉公司资料库情报。
    """
    persona_id: str = Field(min_length=1, max_length=60)
    company_style: str = Field(default='generic', max_length=30)
    round: str = Field(default='', max_length=30)  # 空=用画像卡默认轮次
    intensity: Literal['gentle', 'standard', 'stress'] = 'standard'
    question_count: int = Field(default=8, ge=4, le=12)
    job_id: str | None = Field(default=None, max_length=100)
    jd_text: str = Field(default='', max_length=20000)
    job_title: str = Field(default='', max_length=120)
    company: str = Field(default='', max_length=120)
    focus: str = Field(default='', max_length=120)  # 备战计划指定的专项主题，注入出题 weak_focus
    # 错题强化（仅 reinforce 卡）：要重练的错题引用，每道错题出一道变式题，题量随错题数。
    drill: list[InterviewDrillRef] = Field(default_factory=list, max_length=12)


class InterviewTurnInput(RequestModel):
    answer: str = Field(default='', max_length=8000)  # 空=开场（面试官自我介绍+第一题）


class PrepPlanGenerateInput(RequestModel):
    """面试备战计划：周期 + 自定义重点。同日同数据同参数幂等返回原计划。"""
    days: int = Field(default=14, ge=7, le=28)
    goal: str = Field(default='', max_length=300)  # 用户自填的备战重点，注入模型分析
    job_id: str = Field(default='', max_length=100)  # 目标岗位：JD/评估缺口进材料，任务深链预选
    minutes_per_day: int = Field(default=20, ge=10, le=120)
    use_llm: bool = True  # 关掉则强制走规则版（模型不可用时也自动回退）
    force: bool = False  # 跳过同日幂等：对话调整里用户确认整轮重排后使用


class PrepAdjustInput(RequestModel):
    """备战计划对话调整：用户用自然语言说要怎么改，模型产出结构化修改指令。"""
    message: str = Field(min_length=1, max_length=2000)


class PrepTaskNoteInput(RequestModel):
    """备战任务打卡备注：可空，服务端再按 500 字护栏截断。"""
    note: str = Field(default='', max_length=10000)  # 超长由服务层截到 500 字，不在这里 422


class TranscriptImportInput(RequestModel):
    """真实面试逐字稿导入：外部 ASR 工具转写的纯文本，必选绑定岗位库岗位。"""
    job_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=40000)


class TranscriptSegmentInput(RequestModel):
    """人工核对后的一段：说话人归一化取值 me / interviewer_1..interviewer_5。

    text 允许空串（全局 str_strip_whitespace 会把纯空白剥成空）：服务端丢弃空段，
    不让用户清空某段时吃 422。
    """
    speaker: str = Field(min_length=1, max_length=20)
    text: str = Field(default='', max_length=4000)


class TranscriptSegmentsInput(RequestModel):
    """人工核对保存：全量段落按序替换（index 由服务端按顺序重编）。"""
    segments: list[TranscriptSegmentInput] = Field(min_length=1, max_length=400)


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
    # 跟进信息（2026-11-18）：下一步行动与截止日，今日概览据此生成待办；'' 显式清空，None 不改。
    pipeline_note: str | None = Field(default=None, max_length=300)
    pipeline_due: str | None = Field(default=None, max_length=10)
    # 求职线（2026-11-22 P1）：main=主线保底 / shift=转型线，漏斗按线分母；'' 显式清空，None 不改。
    pipeline_track: Literal['main', 'shift', ''] | None = Field(default=None)

    @field_validator("pipeline_due")
    @classmethod
    def due_date_format(cls, value):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("截止日须为 YYYY-MM-DD")
        return value


class JobBatchDelete(RequestModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class OutreachInput(RequestModel):
    """触达话术生成（2026-11-22）：greeting=BOSS 首条打招呼，followup=投后跟进催进展。"""
    kind: Literal['greeting', 'followup'] = 'greeting'


class JobDedupMark(RequestModel):
    """岗位去重标记（2026-11-19）：primary_id 是保留的主记录，其余标记为其重复项。

    标记不删除任何档案：重复项默认不再出现在列表与统计里，可随时拆开还原。"""
    primary_id: str = Field(min_length=1, max_length=100)
    duplicate_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=50)


class JobDedupRelease(RequestModel):
    """拆开重复组：被标记的岗位恢复独立身份，重新出现在列表与统计。"""
    ids: list[str] = Field(min_length=1, max_length=200)


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
    typography: dict | None = None


class ResumeVersionPatch(RequestModel):
    """二次编辑：缺省字段保持不变，template_id 需为内置模板之一。"""
    title: str | None = Field(default=None, min_length=1, max_length=100)
    markdown: str | None = Field(default=None, min_length=1, max_length=60000)
    template_id: str | None = Field(default=None, max_length=50)
    notes: list[Annotated[str, Field(max_length=600)]] | None = None
    typography: dict | None = None


class ResumePreviewInput(RequestModel):
    markdown: str = Field(min_length=1, max_length=60000)
    template_id: str = Field(default='classic', max_length=50)
    typography: dict = Field(default_factory=dict)


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
    """个人资料库条目：完整版个人材料 + 求职偏好申报（preference 类，启用即注入评估事实池）
    + 空窗经历（gap 类，启用即进入空窗期叙事/面试专题的素材池）。"""
    category: Literal['project', 'work', 'gap', 'education', 'skill', 'cert', 'award', 'preference', 'other'] = 'project'
    title: str = Field(min_length=1, max_length=200)
    org: str = Field(default='', max_length=200)
    period: str = Field(default='', max_length=100)
    content: str = Field(min_length=1, max_length=20000)
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] | None = Field(default=None, max_length=20)
    enabled: bool = True


class ProfileEntryPatch(RequestModel):
    """部分更新：启停开关、内容微调；缺省字段保持不变。"""
    category: Literal['project', 'work', 'gap', 'education', 'skill', 'cert', 'award', 'preference', 'other'] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    org: str | None = Field(default=None, max_length=200)
    period: str | None = Field(default=None, max_length=100)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


class GapNarrativeInput(RequestModel):
    """空窗期叙事生成：素材固定取资料库启用的 gap 条目；目标岗位可选（对齐 JD 用语与措辞）。"""
    job_id: str = Field(default='', max_length=100)
    jd_text: str = Field(default='', max_length=20000)
    job_title: str = Field(default='', max_length=300)


class RunwayInput(RequestModel):
    """生存跑道档案（2026-11-22 P2）：全部可选、只存本机、绝不进模型素材池。
    金额只在本页展示，跨页只出推导值（跑道月数/断缴月数）。"""
    monthly_expense: float | None = Field(default=None, ge=0, le=100000000)
    savings: float | None = Field(default=None, ge=0, le=100000000)
    social_security_last_month: str = Field(default='', pattern=r'^(?:\d{4}-(?:0[1-9]|1[0-2]))?$')
    unemployment_claim: Literal['', 'not_claimed', 'claimed', 'ineligible'] = ''
    notes: str = Field(default='', max_length=500)


class JdAssessmentResponse(RequestModel):
    """反向JD测评单题作答：trial_id 对应题库序列；best / worst 是该题 items 里的下标
    （最想投 / 最不想投，不能相同——相同由计分入口拒绝）；rt_ms 反应时（过快判无效，进质检）。"""
    trial_id: str = Field(min_length=1, max_length=10)
    best: int = Field(ge=0, le=3)
    worst: int = Field(ge=0, le=3)
    rt_ms: int = Field(default=0, ge=0, le=600000)


class JdAssessmentInput(RequestModel):
    """反向JD测评提交：全量作答一次落库，服务端确定性计分并生成报告（无 LLM）。
    seed 是取题时的会话种子（决定题序/变体/锚点配对），须与 GET trials 一致；省略走默认卷。"""
    responses: list[JdAssessmentResponse] = Field(min_length=1, max_length=120)
    seed: int | None = Field(default=None, ge=0, le=2147483647)


class StyleAssessmentResponse(RequestModel):
    """工作风格卷单句作答（2026-11-24）：item_id 对应卷面题目编号；value 是五级选项下标 0–4
    （非常不同意 … 非常同意；布尔值会被宽松转换成整数，所以计分入口再按「必须是 int」拒绝一遍）；
    rt_ms 本句反应时（过快进质检）。"""
    item_id: str = Field(min_length=1, max_length=10)
    value: int = Field(ge=0, le=4)
    rt_ms: int = Field(default=0, ge=0, le=600000)


class StyleAssessmentInput(RequestModel):
    """工作风格卷提交：全量作答一次落库，服务端确定性计分并生成报告（无 LLM）。
    seed 是取卷时的会话种子（只决定题序），计分按题目编号，与题序无关；省略走默认卷。"""
    responses: list[StyleAssessmentResponse] = Field(min_length=1, max_length=120)
    seed: int | None = Field(default=None, ge=0, le=2147483647)


class AttributionTagInput(RequestModel):
    """失败博物馆单条归因标注：scope=controlled/uncontrolled，tag 对应分类学常量 id。"""
    scope: Literal['controlled', 'uncontrolled']
    tag: str = Field(min_length=1, max_length=40)
    note: str = Field(default='', max_length=200)


class AttributionInput(RequestModel):
    """失败博物馆整场归档：一场面试一组标注（按 session_id upsert，可返回修改）。

    outcome：mock 取 self_positive/self_neutral/self_negative（自评），
    real 取 pass/fail/unknown（真实结果），均可留空不参与结果对比。
    """
    source: Literal['mock', 'real']
    session_id: str = Field(min_length=1, max_length=100)
    tags: list[AttributionTagInput] = Field(min_length=1, max_length=12)
    note: str = Field(default='', max_length=500)
    outcome: Literal['', 'self_positive', 'self_neutral', 'self_negative', 'pass', 'fail', 'unknown'] = ''


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


class AnalysisStatusInput(RequestModel):
    """评估适配需求行手动改判：按 jd_quote 摘要记忆，重评自动沿用。"""
    jd_quote: str = Field(min_length=1, max_length=1500)
    status: Literal['met', 'partial', 'unmet', 'unknown']
    note: str = Field(default='', max_length=500)


class ResumeVersionPinInput(RequestModel):
    """岗位绑定「评估用」简历的开关；仅绑定了岗位的存档版本可 pin。"""
    pinned: bool
