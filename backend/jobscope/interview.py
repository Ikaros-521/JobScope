"""模拟面试：面试官画像（三段式人设）、出题、导演规则与复盘报告。

面试官是一个「无工具的对话 agent」+ 一个服务端消费的调度工具（advance）：
可见回复保持自然对话，调度决策（追问/换题/收尾）走结构化 tool_calls，服务端
据此推进题单进度并收集弱点信号——可见文本与内心调度分离，复盘报告逐题归档。

出题弹药三源合一（相对开源方案的差异化）：岗位库 JD + 适配评估的 gaps/低分
维度 + 简历存档事实。模型配置复用「分析」功能绑定（同为大任务模型，后续需要
独立绑定再扩 PURPOSES）。
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .settings import get_active_configs, use_config

logger = logging.getLogger(__name__)

# 面试官每轮回复的输出预算：思考 token 与输出共用（GLM 语义），低于 2000 会被思考吃穿。
TURN_MAX_TOKENS = 8000
PLAN_MAX_TOKENS = 16000
REVIEW_MAX_TOKENS = 4000
SUMMARY_MAX_TOKENS = 6000

# ---- 面试官画像：三层叠加（公司风格 × 岗位类型 × 轮次）编译成三段式人设 ----
# 借鉴 WorkBuddy「专家」与 interview-skills 的公司画像思路，但内容由本仓库沉淀：
# 角色说明（是谁/口吻）、工作方法（怎么追问）、任务要求（流程约束）。

COMPANY_STYLES = {
    'alibaba': dict(
        label='阿里巴巴',
        values=['客户第一、业务结果导向', '数据驱动（指标定义 → 归因 → 反事实推演）', '复杂协同推动力', '结构性思考与判断力', '价值观契合（真诚, 不背口号）'],
        probing='连环 WHY：为什么做、为什么这么做、为什么是你；对任何数字追问口径与归因；给业务场景假设题现场拆解。',
    ),
    'bigtech': dict(
        label='一线互联网大厂',
        values=['结果与 impact 量化', '方法论清晰、能抽象可迁移', '节奏快、接受挑战式追问', '落地执行力与取舍意识'],
        probing='快节奏追问：先抓回答里的模糊处逼量化，再问方案代价与替代项；不接受名词堆砌，要求讲「为什么这么设计」。',
    ),
    'generic': dict(
        label='通用（按材料推断风格）',
        values=['经历真实、表达清晰', '岗位核心能力匹配', '学习与成长潜力'],
        probing='由浅入深，先听完整回答再挑最模糊的一处追问；对没有量化的经历追问数据。',
    ),
}

# 题型配比挂在角色层：同一家公司面产品与面数据分析师，考的东西完全不同。
ROLE_FAMILIES = {
    'product': dict(
        label='产品经理 / 产品专家',
        focus='业务理解、数据敏感度、方案取舍、跨团队推动',
        data_rule='候选人讲到的每个项目/动作都必须有量化前后对比；缺失即追问「结果怎么衡量、变化了多少」。',
        mix=[('项目深挖（STAR + 数据）', .40), ('业务与行业理解', .20), ('指标体系设计', .15), ('方案设计/场景拆解', .15), ('协作冲突与价值观', .10)],
    ),
    'operations': dict(
        label='运营 / 用户增长',
        focus='指标与转化、活动策划与复盘、渠道与用户分层、执行落地',
        data_rule='活动与增长动作必须有转化率/ROI/留存的前后对比；缺失即追问口径与基数。',
        mix=[('项目/活动深挖（STAR + 数据）', .35), ('指标体系与增长', .20), ('渠道与用户运营', .15), ('方案设计/场景拆解', .15), ('协作与执行', .15)],
    ),
    'data': dict(
        label='数据分析师',
        focus='指标口径、取数与分析能力、实验设计（A/B）、业务归因',
        data_rule='讲任何分析结论都要能说清数据口径、样本与可信度；含糊即追问「这个数怎么算出来的」。',
        mix=[('项目/分析深挖（STAR + 数据）', .35), ('指标口径与实验设计', .25), ('业务归因与洞察', .20), ('工具与方法（SQL等）', .10), ('协作与推动', .10)],
    ),
    'dev': dict(
        label='软件研发工程师',
        focus='系统与方案设计、工程取舍、线上问题排查、工程质量',
        data_rule='性能/稳定性/规模必须有数字（QPS、RT、错误率）；「很快/很稳」一律追问基准。',
        mix=[('项目深挖（STAR + 数据）', .35), ('系统设计', .25), ('工程取舍与难题攻坚', .20), ('线上排查/场景假设', .10), ('协作与工程习惯', .10)],
    ),
    'design': dict(
        label='设计师',
        focus='设计判断与决策依据、用户价值与业务约束的平衡、协作影响力',
        data_rule='设计结果要有验证方式（数据、可用性测试、反馈）；「我觉得好」追问依据。',
        mix=[('项目深挖（决策与依据）', .40), ('设计判断与方法', .20), ('用户价值与业务平衡', .20), ('协作与影响力', .20)],
    ),
    'hr': dict(
        label='HR 面试官',
        focus='动机真实性、稳定性、自我认知、价值观行为化（BEI）',
        data_rule='动机与规划要能落到具体行为与事实；空泛表态（热爱/感兴趣）追问背后的具体事件。',
        mix=[('动机核实（离职/求职原因）', .30), ('价值观 BEI 行为化', .25), ('稳定性与规划', .15), ('薪资预期', .15), ('自我认知', .15)],
    ),
    'general': dict(
        label='通用岗位',
        focus='岗位核心能力、项目真实度、协作与成长',
        data_rule='没有量化的经历一律追问数据来源与口径。',
        mix=[('项目深挖（STAR + 数据）', .40), ('岗位核心能力', .25), ('业务理解', .15), ('协作与动机', .20)],
    ),
}

ROUND_STYLES = {
    'business': dict(label='业务面（一面）', focus='重项目深挖与业务理解，常见于未来直属主管，判断「事能不能干、是不是真做过」'),
    'cross': dict(label='交叉面（二面）', focus='重通用能力与案例迁移：其他团队面试官拿类比例子考你，判断能力是不是岗位通用'),
    'director': dict(label='总监/GM 面', focus='重判断力、规划与格局：少问执行细节，多问为什么这么做、重来会怎么做、三年后你想成为什么'),
    'hr': dict(label='HR 面', focus='动机、稳定性、薪资预期、价值观行为化'),
}

INTENSITY = {
    'gentle': dict(label='温和', rule='以倾听为主，回答含糊时只追问一次，语气友善；不打断，允许候选人组织语言。'),
    'standard': dict(label='标准', rule='真实大厂节奏：回答缺数据或缺个人动作时追问到底，语气专业克制，不给情绪反馈。'),
    'stress': dict(label='压力面', rule='主动质疑与挑战：对泛泛而谈直接打断并要求给具体例子；对漂亮话追问反事实（如果没成呢）；保持职业素养但不容含糊。'),
}

# 卡片 = 岗位专家（role family）；公司风格是独立开关（前端 styles），二者自由组合。
# 具体岗位名（job_title）与目标公司（company）由建场入参带入——「具体岗位都不确定怎么面试」。
PERSONA_PRESETS = [
    dict(id='product', name='产品专家', role_family='product',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='项目深挖 + 指标体系 + 方案设计，产品岗全轮次通吃', tags=['项目深挖', '指标体系', '方案设计']),
    dict(id='operations', name='运营专家', role_family='operations',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='活动复盘 + 增长指标 + 用户分层，运营岗的转化与 ROI 追问', tags=['转化率', 'ROI', '活动复盘']),
    dict(id='data', name='数据分析师', role_family='data',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='指标口径 + A/B 实验设计 + 业务归因，专治「这个数怎么算的」', tags=['口径', 'A/B 实验', '归因']),
    dict(id='dev', name='软件研发工程师', role_family='dev',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='系统设计 + 工程取舍 + 线上排查，技术面的深度追问', tags=['系统设计', '工程取舍', '排查']),
    dict(id='design', name='设计师', role_family='design',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='设计决策依据 + 用户价值与业务平衡，追问「凭什么这么设计」', tags=['设计判断', '验证依据']),
    dict(id='hr', name='HR 面试官', role_family='hr',
         rounds=['hr'], default_round='hr',
         blurb='离职原因、薪资预期、职业规划与价值观行为化追问，专练 HR 关卡', tags=['动机核实', 'BEI', '谈薪']),
    dict(id='custom', name='自定义岗位', role_family='general',
         rounds=['business', 'cross', 'director', 'hr'], default_round='business',
         blurb='填上具体岗位名称再开面；按 JD 与岗位名自动推断考什么', tags=['按 JD 推断', '任意岗位']),
]


def persona_cards():
    """前端开面面板：岗位专家卡 + 公司风格开关（二者自由组合）。"""
    cards = [dict(id=preset['id'], name=preset['name'], blurb=preset['blurb'], tags=preset['tags'],
                  rounds=[dict(id=round_id, label=ROUND_STYLES[round_id]['label']) for round_id in preset['rounds']],
                  default_round=preset['default_round'])
             for preset in PERSONA_PRESETS]
    styles = [dict(id=style_id, label=style['label']) for style_id, style in COMPANY_STYLES.items()]
    return cards, styles


def get_preset(preset_id):
    for preset in PERSONA_PRESETS:
        if preset['id'] == preset_id:
            return preset
    raise ValueError('未知的面试官岗位画像')


def get_company_style(style_id):
    style = COMPANY_STYLES.get(style_id)
    if style is None:
        raise ValueError('未知的公司面试风格')
    return style


def build_persona(preset_id, round_id, intensity='standard', company_style='generic', job_title='', company_name=''):
    """把岗位卡 × 公司风格 × 轮次 × 强度编译成三段式人设（角色说明/工作方法/任务要求的素材）。"""
    preset = get_preset(preset_id)
    if round_id not in preset['rounds']:
        round_id = preset['default_round']
    style = get_company_style(company_style)
    role = ROLE_FAMILIES[preset['role_family']]
    round_style = ROUND_STYLES[round_id]
    return dict(
        preset_id=preset_id, name=preset['name'], role_label=role['label'],
        company=style['label'], company_style_key=company_style,
        job_title=(job_title or '').strip(), company_name=(company_name or '').strip(),
        round=round_id, round_label=round_style['label'], intensity=intensity,
        intensity_label=INTENSITY[intensity]['label'],
        role=role, company_style=style, round_style=round_style,
    )


# ---- 调度工具：服务端消费，候选人不可见 ----

ADVANCE_TOOL = dict(
    name='advance',
    description='每轮回复都必须调用一次：上报你作为面试官的调度决策（候选人看不到这个调用）。先输出对话文本，再调用本工具。',
    parameters=dict(type='object', properties=dict(
        verdict=dict(type='string', enum=['followup', 'next', 'wrapup'],
                     description='followup=继续追问本题；next=本题问透，进入题单下一题；wrapup=题单走完，进入反问与收尾'),
        question_index=dict(type='integer', description='当前题单题号（0 基）'),
        weakness=dict(type='array', items=dict(type='string'),
                      description='本轮候选人回答暴露的弱点信号，取值：无量化数据/无个人动作/无结果/STAR缺失/含糊其辞/知识硬伤'),
        note=dict(type='string', description='一句话面试官内心评价（候选人不可见，仅进复盘）'),
    ), required=['verdict', 'question_index']),
)


def _progress_directive(plan, phase, progress):
    """实时进度块（hidden direction）：模型对长对话数不清追问轮数，必须喂系统记账。

    追问层 = 本题回答数-1；达到 3 层下一轮硬性要求 verdict=next；反问环节超 2 轮硬性要求告别。
    """
    if not progress:
        return ''
    questions = plan.get('questions') or []
    total = len(questions)
    segments = progress.get('segments') or []
    if phase == 'wrapup':
        wrap = next((seg for seg in reversed(segments) if not seg.get('reviewable')), {})
        asked = len(wrap.get('answers', []))
        lines = [f'- 反问环节已进行 {asked} 轮。',
                 '- 反问至多 2 轮：达到后即输出简短告别（感谢＋一句总括），明确说「今天的面试就到这里」，此后不再提任何问题。']
        if asked >= 4:
            lines.append('【硬性要求】反问已超 2 轮：本轮必须直接告别收尾，不再提任何新问题。')
        return '\n【实时进度（系统记账，以此为准）】\n' + '\n'.join(lines) + '\n'
    index = min(max(int(progress.get('current_index', 0)), 0), max(total - 1, 0))
    answers = len(segments[-1].get('answers', [])) if segments else 0
    followups = max(answers - 1, 0)
    topic = (questions[index].get('topic') if 0 <= index < total else '') or ''
    lines = [f'- 当前第 {index + 1}/{total} 题{f"「{topic}」" if topic else ""}，候选人已答 {answers} 次（追问层 {followups}/3）。']
    if followups >= 3:
        lines.append('【硬性要求】本题追问层已用完：本轮必须 verdict=next 进入下一题（可先一句简短过渡再抛下一题）。')
    else:
        lines.append('- 本题追问层用完必须 verdict=next 换题；全部题走完 verdict=wrapup 进入反问环节。')
    return '\n【实时进度（系统记账，以此为准）】\n' + '\n'.join(lines) + '\n'


def build_interviewer_system(persona, material, plan, *, phase='opening', progress=None):
    """面试官 system prompt：三段式人设 + 导演规则 + 题单 + 候选材料（含公司情报）。

    导演规则（hidden direction）：追问优先级 无量化数据 > 无个人动作 > 无结果 > STAR 缺失；
    答透即换题不恋战；每题最多追问 3 层；不剧透参考答案、不实时点评——评价全部留给复盘。
    progress 是场次实时记账（current_index/segments）：模型面对长对话数不清自己追问了几轮，
    必须把系统侧的准确进度喂回去，追问超限换题、反问超轮收尾才有硬依据。
    """
    questions_text = '\n'.join(
        f'{index}. [{item["topic"]}]（来源:{item["source"]}）{item["question"]}\n   考察点:{item["focus"]} 追问路径:{"/".join(item["followups"])}'
        for index, item in enumerate(plan['questions']))
    progress_block = _progress_directive(plan, phase, progress)
    gaps_text = '\n'.join(f'- {gap}' for gap in material.get('gaps', [])[:8]) or '（本场没有适配评估数据）'
    target_line = persona['job_title'] or material.get('job_title') or '（未指明具体岗位，按画像与材料推断）'
    company_line = material.get('company') or persona['company_name'] or '（未指明公司）'
    brief_text = (material.get('company_brief') or '').strip()
    company_section = f'\n公司情报（可自然融入提问，如结合业务场景出方案题；不确定的信息不要当事实引用）：\n{brief_text}' if brief_text else ''
    return f"""你是本场模拟面试的面试官，全程保持角色，绝不提及自己是 AI 或在「模拟」。

【角色说明】
- 面试风格：{persona['company']}。看重：{'；'.join(persona['company_style']['values'])}。
- 岗位视角：{persona['role_label']}，关注 {persona['role']['focus']}。本次招聘岗位：{target_line}；目标公司：{company_line}。
- 本轮次：{persona['round_label']}。{persona['round_style']['focus']}。
- 面试强度：{persona['intensity_label']}。{INTENSITY[persona['intensity']]['rule']}
- 口吻：直接、不客套、有深度，像真实大厂面试官；提问由浅入深直到找到能力边界。

【工作方法（导演规则——候选人不可见）】
1. 严格按【本场题单】顺序推进，可微调问法但不出题单范围。
2. 追问优先级：无量化数据 > 无个人动作 > 无结果 > STAR 缺失。{persona['role'].get('data_rule', '')}
3. 每题最多追问 3 层；答透即简短过渡（如「嗯，明白」「OK」）换下一题，不恋战不评价。
4. 候选人回答过短或答非所问：追问「能展开讲讲吗」「具体是你做的哪部分」。
5. 候选人明确说「跳过/换一题/不知道」：允许跳题，weakness 里记录「含糊其辞」。
6. 题单走完或候选人要求结束：verdict=wrapup，做反问环节（「你有什么想问我的」），再简短收尾。

【任务要求（硬约束）】
- 每轮回复：先输出面试官的对话文本（自然口语，中文，一般不超过 5 句），然后必须调用 advance 工具上报调度决策。
- 不剧透参考答案；不点评候选人表现好坏；不解释出题意图；不用 emoji；不复述题目全文。
- 一次只问一个问题；问题要具体到场景，不要问「你有什么问题想问我」以外的开放闲聊。
{progress_block}
【本场题单】（当前阶段：{ {'opening': '待开场：先用一句自我介绍开场，然后抛出第 0 题', 'questioning': '逐题推进中', 'wrapup': '反问与收尾'}[phase]}）
{questions_text}

【候选材料】
JD 关键信息：
{material.get('jd_summary') or '（未提供 JD，按画像通用出题）'}

适配评估缺口（面试官重点核实这些——评估说不达标的能力点，真实面试官也会追着打）：
{gaps_text}

简历要点：
{material.get('resume_digest') or '（未提供简历，只按 JD 与通用题提问）'}{company_section}"""


# ---- 出题：三源合一 → 结构化题单 ----

class PlanQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    topic: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=5, max_length=600)
    focus: str = Field(min_length=1, max_length=300)
    followups: list[str] = Field(min_length=1, max_length=4)
    source: str = Field(default='jd', max_length=20)  # jd | gap | resume | general


class QuestionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    opening: str = Field(min_length=2, max_length=300)
    questions: list[PlanQuestion] = Field(min_length=1, max_length=12)


_PLAN_SYSTEM = '''你是大厂面试官的首席出题官。根据面试官画像、JD、适配评估缺口和候选人简历，生成一场模拟面试的题单。
要求：
1. 题量按给定的 question_count；题型配比参考画像里的权重。
2. 评估缺口（gaps）里至少一半要转化成「核实型问题」——真实面试官最爱追问简历薄弱点；source 标 gap。
3. 项目深挖题从简历要点里选最能被追问的项目；source 标 resume。
4. JD 里明确要求但简历证据弱的能力要出题；source 标 jd。
5. 每题给 focus（面试官想听到什么）和 2-3 个 followups（追问路径）。
6. question 用面试官的真实口吻写，具体到场景，不要八股。
只输出JSON：{"opening":"面试官开场白（一句话自我介绍+面试说明）","questions":[{"topic":"题型","question":"问题","focus":"考察点","followups":["追问1","追问2"],"source":"jd|gap|resume|general"}]}'''


def _parse_json(content):
    content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
    return json.loads(content)


def build_question_plan(persona, material, *, question_count=8):
    """出题：一次非流式大预算调用（分析绑定），Pydantic 校验失败直接报错给前端。"""
    from .model_transport import complete
    payload = dict(
        persona=dict(company=persona['company'], round=persona['round_label'], role=persona['role_label'],
                     job_title=persona['job_title'], focus=persona['role']['focus'],
                     mix=[dict(topic=name, weight=weight) for name, weight in persona['role']['mix']],
                     probing=persona['company_style']['probing']),
        question_count=question_count,
        jd=material.get('jd_summary') or '',
        gaps=material.get('gaps') or [],
        resume=material.get('resume_digest') or '',
        company_brief=material.get('company_brief') or '',
    )
    content, _ = complete(get_config(), _PLAN_SYSTEM, json.dumps(payload, ensure_ascii=False),
                          temperature=.3, max_tokens=PLAN_MAX_TOKENS, timeout=300)
    try:
        plan = QuestionPlan.model_validate(_parse_json(content))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning('模拟面试出题返回无效：%s', exc)
        raise ValueError('出题结果无效，请重试') from None
    result = plan.model_dump()
    result['questions'] = result['questions'][:question_count]
    return result


# ---- 复盘报告：两阶段（逐题并行点评 → 汇总），逐题含好差答案对比 ----

JD_SUMMARY_CHARS = 3000
RESUME_DIGEST_CHARS = 4000
COMPANY_BRIEF_CHARS = 1800


def company_brief_from(repo, company_name, *, limit=5, per_item_chars=300):
    """公司资料库 → 面试官的公司情报摘要（非岗位 postings 的证据，新→旧取前几条）。"""
    if not (company_name or '').strip():
        return ''
    items = repo.evidence(company=company_name.strip(), include_job_postings=False)
    parts = []
    for item in items[:limit]:
        text = ' '.join(str(item.get('text') or '').split())[:per_item_chars]
        if text:
            parts.append(f"[{item.get('kind', '材料')}] {item.get('title', '')}：{text}")
    return '\n'.join(parts)[:COMPANY_BRIEF_CHARS]


def assemble_material(job, resume, report=None, company_brief=''):
    """出题与面试官共用的候选材料包：JD 摘录 + 简历存档摘录 + 适配评估缺口 + 公司情报。

    gaps 是模拟面试相对开源方案的差异化弹药：评估判定 partial/unmet/unknown 的
    要求清单（requirement：rationale），出题时至少一半转成核实型问题。
    """
    gaps = list((report or {}).get('gaps') or [])
    dimensions = (report or {}).get('dimensions') or {}
    for name, info in dimensions.items():
        if isinstance(info, dict) and info.get('score') is not None and info['score'] < 60:
            gaps.append(f'维度「{name}」评估得分偏低（{info["score"]}），面试官大概率重点核实。')
    return dict(
        job_id=(job or {}).get('id', ''), company=(job or {}).get('company', ''), job_title=(job or {}).get('title', ''),
        jd_summary=((job or {}).get('description') or '').strip()[:JD_SUMMARY_CHARS],
        resume_digest=((resume or {}).get('text') or '').strip()[:RESUME_DIGEST_CHARS],
        gaps=gaps[:10], report_id=(report or {}).get('id', ''), resume_label=(resume or {}).get('label', ''),
        company_brief=(company_brief or '').strip(),
    )


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    critique: str = Field(min_length=1, max_length=2000)
    missing: list[str] = Field(default_factory=list, max_length=6)
    model_answer: str = Field(min_length=1, max_length=3000)
    bad_answer_note: str = Field(min_length=1, max_length=1000)


class DimensionScore(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=30)
    score: int = Field(ge=1, le=5)
    comment: str = Field(min_length=1, max_length=500)


REVIEW_SYSTEM = '''你是面试教练。根据题单元信息（题目/考察点/追问路径）和候选人的实际回答，逐题点评。
要求：critique 以面试官视角点评这段回答（好在哪、差在哪，引用原话）；missing 列出缺失的要素（如 量化数据/我的动作/结果/背景S/任务T）；model_answer 给一段针对本题的高分示范答法（结合候选人材料改写，不是通用模板）；bad_answer_note 一句话点出典型的低分踩坑写法。
只输出JSON：{"critique":"...","missing":["..."],"model_answer":"...","bad_answer_note":"..."}'''

_SUMMARY_SYSTEM = '''你是面试教练主管。根据逐题点评汇总本场模拟面试的复盘总评。
dimensions 从这些维度打分（1-5）：结构化表达、数据与量化、业务理解、个人贡献清晰度、抗压追问。
summary 给总评（整体表现、最突出的 1-2 个优点、最致命的问题、与岗位面试官预期的差距），risk_list 给「下次真面试前必须补的高危问题」，actions 给具体可执行动作（可指向改简历/补材料/换说法）。
只输出JSON：{"summary":"...","dimensions":[{"name":"结构化表达","score":3,"comment":"..."}],"risk_list":["..."],"actions":["..."]}'''


def get_config():
    """面试官/出题/复盘统一走「分析」功能绑定（effective() 已含回落逻辑）。"""
    from .settings import get_purpose_config
    return get_purpose_config('analysis')


def review_segment(persona, segment):
    """阶段1单题：一个题单片段（题目+回答+追问过程）→ 逐题点评。"""
    from .model_transport import complete
    payload = dict(persona=dict(company=persona['company'], round=persona['round_label'], role=persona['role']['label']),
                   topic=segment['question'].get('topic', ''), question=segment['question'].get('question', ''),
                   focus=segment['question'].get('focus', ''), followups=segment['question'].get('followups', []),
                   answers=segment['answers'], interviewer_notes=segment.get('notes', []))
    content, _ = complete(get_config(), REVIEW_SYSTEM, json.dumps(payload, ensure_ascii=False),
                          temperature=.3, max_tokens=REVIEW_MAX_TOKENS, timeout=300)
    try:
        return QuestionReview.model_validate(_parse_json(content)).model_dump()
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning('逐题点评返回无效（%s…）：%s', segment['question'].get('topic', '')[:20], exc)
        return dict(critique='本条点评生成失败，跳过。', missing=[], model_answer='', bad_answer_note='')


def _coerce_summary(raw):
    """汇总软校验：模型多给几条 risk/actions 或字段超长很常见——截断保住整份报告，别作废。

    结构真不对（非对象、dimensions 全无效、summary/risk 缺失）才 ValueError 交给上层重试。
    """
    if not isinstance(raw, dict):
        raise ValueError('汇总不是 JSON 对象')

    def _text(value, limit):
        return str(value or '').strip()[:limit]

    def _score(value):
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 3

    dimensions = []
    for item in raw.get('dimensions') or []:
        if not isinstance(item, dict):
            continue
        try:
            dimensions.append(DimensionScore.model_validate(dict(
                name=_text(item.get('name'), 30) or '维度', score=_score(item.get('score')),
                comment=_text(item.get('comment'), 500) or '（无点评）')).model_dump())
        except ValidationError:
            continue
        if len(dimensions) >= 8:
            break
    if not dimensions:
        raise ValueError('dimensions 缺失或全部无效')
    risks = [text for text in (_text(item, 200) for item in raw.get('risk_list') or []) if text][:8]
    actions = [text for text in (_text(item, 200) for item in raw.get('actions') or []) if text][:6]
    summary = _text(raw.get('summary'), 3000)
    if not summary or not risks:
        raise ValueError('summary/risk_list 缺失')
    return dict(summary=summary, dimensions=dimensions, risk_list=risks, actions=actions)


def generate_report(persona, segments, material, should_stop=None, on_progress=None):
    """复盘全流程：逐题并行（线程池）→ 汇总总评/维度/高危清单；取消在题间边界生效。

    on_progress(dict) 在每条逐题点评完成与进入汇总时回调——SSE 推给前端做实时进度，
    后台任务不再黑盒傻等（用户看不到进展就会以为卡死）。
    """
    from concurrent.futures import as_completed
    from .semantic import RunCancelled
    if should_stop is not None and should_stop():
        raise RunCancelled()
    configs = get_active_configs()
    reviews = [None] * len(segments)
    with ThreadPoolExecutor(max_workers=3) as pool:
        # contextvar 不跨线程：逐题点评跑在池子线程，use_config 挂在复盘 worker 上取不到，
        # 必须把生效全典显式带进任务重新挂（挂单 purpose 会让池内 get_purpose_config KeyError）。
        def _review(segment):
            with use_config(configs):
                return review_segment(persona, segment)
        futures = {pool.submit(_review, segment): index for index, segment in enumerate(segments)}
        # as_completed：谁先点评完先报进度，不等提交顺序。
        for done_count, future in enumerate(as_completed(list(futures)), start=1):
            index = futures[future]
            reviews[index] = future.result()
            if on_progress:
                on_progress(dict(stage='review', done=done_count, total=len(segments),
                                 topic=(segments[index].get('question') or {}).get('topic', '')))
    if should_stop is not None and should_stop():
        raise RunCancelled()
    if on_progress:
        on_progress(dict(stage='summary', done=len(segments), total=len(segments)))
    payload = dict(
        persona=dict(company=persona['company'], round=persona['round_label'], role=persona['role']['label']),
        reviews=[dict(topic=seg['question'].get('topic', ''), question=seg['question'].get('question', ''), review=review)
                 for seg, review in zip(segments, reviews)],
        evaluation_gaps=material.get('gaps') or [],
    )
    from .model_transport import complete
    for attempt in (1, 2):  # 软校验已兜住超长/多条；这里只剩真垃圾，自动重试一次再报错
        content, _ = complete(get_config(), _SUMMARY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                              temperature=.3, max_tokens=SUMMARY_MAX_TOKENS, timeout=300)
        try:
            summary = _coerce_summary(_parse_json(content))
            break
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning('复盘总评返回无效（第 %s 次）：%s', attempt, exc)
            if attempt == 2:
                raise ValueError('复盘报告汇总无效，请重试') from None
    return dict(summary=summary['summary'], dimensions=summary['dimensions'], risk_list=summary['risk_list'],
                actions=summary['actions'], questions=[
                    dict(topic=seg['question'].get('topic', ''), question=seg['question'].get('question', ''), **review)
                    for seg, review in zip(segments, reviews)])
