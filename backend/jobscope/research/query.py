"""Typed query plans; an LLM may interpret language, never grant data access."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONCEPTS = {
    'salary': ('工资', '薪资', '薪酬', '待遇', '月薪', '年薪', '奖金', '几薪', 'salary', 'compensation'),
    'employees': ('员工', '团队', '人数', '人员', '规模', 'headcount', '在职人员', '员工总数'),
    'business': ('营收', '营业收入', '利润', '业绩', '财报', '经营', 'revenue'),
    'agent': ('agent', '智能体', '工具调用', 'function calling'),
    'rag': ('rag', '检索增强', '知识库', '向量检索'),
    'requirements': ('技能', '职责', '任职', '要求', '熟悉', '掌握', '负责', '经验', '学历', '本科'),
}
STOP = set('这家 家公 公司 怎么 么样 情况 哪些 什么 多少 是否 有没有 以及 和的 的岗 岗位 要求 介绍 请问 看看 关于 那么 可以 这个 一下 呢的'.split())


def tokenize(text):
    words = re.findall(r'[a-z0-9_+#.]+|[\u4e00-\u9fff]+', text.lower())
    tokens = []
    for word in words:
        if re.search('[\u4e00-\u9fff]', word):
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return list(dict.fromkeys(t for t in tokens if t not in STOP and len(t) > 1))


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1500)
    intent: Literal['evidence', 'salary', 'comparison', 'fit'] = 'evidence'
    companies: list[str] = Field(default_factory=list, max_length=8)
    concepts: list[str] = Field(default_factory=list, max_length=8)
    location: str = Field(default='', max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=12)
    skills_operator: Literal['all', 'any'] = 'all'
    threshold: float | None = Field(default=None, ge=0, le=10000000)
    threshold_basis: Literal['monthly_min', 'monthly_max'] = 'monthly_min'
    operator: Literal['gt', 'ge', 'lt', 'le'] = 'ge'
    actual_salary: bool = False
    explanation: str = Field(default='', max_length=500)
    planner: str = 'rules'
    clarification: str = Field(default='', max_length=500)

    def terms(self):
        text = self.query
        for company in self.companies:
            text = text.replace(company, ' ')
        for concept in self.concepts:
            text += ' ' + ' '.join(CONCEPTS.get(concept, ()))
        return tokenize(text)[:128]


def enforce_scope(plan, scope, companies):
    # Scope is supplied by the server, never inferred from source documents.
    if scope['kind'] != 'global':
        plan.companies = [scope['company']]
    else:
        plan.companies = [c for c in plan.companies if c in companies][:8]
    plan.concepts = [c for c in plan.concepts if c in CONCEPTS]
    return plan


def unknown_company_names(message, companies):
    text = message
    for company in sorted(set(companies), key=len, reverse=True):
        text = text.replace(company, ' ')
    names = []
    for match in re.findall(r'([\u4e00-\u9fffA-Za-z0-9]{1,30}(?:公司|集团))', text):
        name = re.sub(r'^(?:请|帮我|先|比较|对比|查询|看看|了解|关于|还有|那么|那|以及|和|与|及)+', '', match)
        if re.fullmatch(r'(?:(?:[一二三四五六七八九十百两几\d]+家)|已保存|已采集|当前|全项目|所有|全部|各个|各家|这家|那家|这几家)?公司', name):
            continue
        if name in {'公司', '集团', '哪个公司', '哪些公司', '所有公司', '全部公司', '各个公司', '各家公司', '两家公司', '这家公司', '那家公司', '这几家公司', '公司和', '公司与'}:
            continue
        if name:
            names.append(name)
    # Conservative subject detection for common "品牌 + 工资/员工/业绩" phrasing.
    # This is not an entity recognizer: ambiguous subjects require selection.
    prefix = re.split(r'工资|薪资|薪酬|待遇|月薪|年薪|员工|团队|营业收入|营收|业绩|净利润|利润|财报|技能|职责|要求', text, maxsplit=1)[0]
    if prefix != text and not names:
        prefix = re.sub(r'^(?:请|帮我|先|比较|对比|查询|看看|了解|关于|还有|那么|那|继续)+', '', prefix)
        prefix = re.sub(r'取消金额限制|取消金额|不限薪资|不限制工资|不限地点|全国|所有城市|不限技能|取消技能|所有岗位|呢', '', prefix)
        prefix = re.sub(r'北京|上海|杭州|深圳|广州|成都|南京|苏州|武汉|宁波|远程|(?:RAG|Agent|Python|JavaScript|Java|SQL|Go|React|C\+\+)', '', prefix, flags=re.I)
        prefix = re.sub(r'已保存|已采集|全项目|所有|全部|当前|范围内|哪些|什么|几个|这家|那家|项目|公司|集团|岗位|招聘|实际|到手|实发|税后|税前|月薪|按|的|和|与|及|或|[一二三四五六七八九十百两几\d]+家', '', prefix)
        prefix = prefix.strip(' ，,。？?：:')
        if re.fullmatch(r'[\u4e00-\u9fffA-Za-z]{2,20}', prefix):
            names.append(prefix)
    return names


def execution_query(plan, original):
    """Render checked fields for retrieval; never trust an unvalidated model rewrite."""
    parts = [original, *plan.companies, plan.location, *plan.skills]
    parts.extend(CONCEPTS[c][0] for c in plan.concepts)
    if plan.threshold is not None:
        parts.append(f'{plan.threshold_basis} {plan.operator} {plan.threshold:g}元')
    return ' '.join(dict.fromkeys(p for p in parts if p))[:1500]


def plan_query(message, history, scope, companies=()):
    query = message.strip()
    user_turns = [h for h in history if h.get('role') == 'user'][-6:]
    previous = user_turns[-1]['content'] if user_turns else ''
    is_followup = bool(re.search(r'^(那|那么|它|他们|这家|还有|继续)|呢[？?]?$|^(超过|低于|至少|只看)', query))
    inherited_plan = None
    if user_turns and is_followup:
        inherited_plan = plan_query(previous, user_turns[:-1], scope, companies)
    if previous and is_followup:
        # Only prior user intent is inherited, not assistant claims or instructions.
        inherited = inherited_plan.concepts
        query += ' ' + ' '.join(CONCEPTS[key][0] for key in inherited)
    lower = query.lower()
    concepts = [key for key, words in CONCEPTS.items() if any(w in lower for w in words)]
    explicit = [c for c in companies if c and c in message]
    # Prefer the most specific known spelling; do not alias a subsidiary to a parent.
    explicit = [c for c in explicit if not any(c != other and c in other for other in explicit)]
    explicit_current = bool(explicit)
    if not explicit and previous and is_followup:
        explicit = inherited_plan.companies
    intent = 'salary' if 'salary' in concepts else 'fit' if re.search('适合|适配|匹配|简历', lower) else 'comparison' if re.search('比较|对比|哪家|谁更', lower) else 'evidence'
    amount = re.search(r'(超过|高于|大于|至少|不低于|以上|低于|少于|不超过|最多)\s*(\d+(?:\.\d+)?)\s*([kK万千元])', message)
    if not amount:
        amount = re.search(r'(\d+(?:\.\d+)?)\s*([kK万千元])\s*(以上|以下)', message)
        direction, value, unit = (amount[3], amount[1], amount[2]) if amount else ('', '', '')
    else:
        direction, value, unit = amount[1], amount[2], amount[3]
    threshold = float(value) * {'k': 1000, '万': 10000, '千': 1000, '元': 1}[unit.lower()] if amount else None
    operator = 'gt' if direction in {'超过', '高于', '大于'} else 'lt' if direction in {'低于', '少于'} else 'le' if direction in {'以下', '不超过', '最多'} else 'ge'
    location = next((city for city in ('北京', '上海', '杭州', '深圳', '广州', '成都', '南京', '苏州', '武汉', '宁波', '远程') if city in message), '')
    plan = QueryPlan(query=query[:1500], intent=intent, companies=explicit, concepts=concepts, location=location,
                     threshold=threshold, operator=operator,
                     threshold_basis='monthly_max' if re.search('上限|最高月薪|最高能', message) else 'monthly_min',
                     actual_salary=bool(re.search('实际|到手|实发|税后', message)),
                     explanation='领域概念扩展；金额按招聘月薪区间下限过滤，上限需显式指定')
    skills = ('Python', 'Java', 'JavaScript', 'TypeScript', 'RAG', 'Agent', 'SQL', 'Go', 'React', 'C++')
    plan.skills = [s for s in skills if re.search(r'(?<![a-z0-9])' + re.escape(s) + r'(?![a-z0-9])', message, re.I)]
    plan.skills_operator = 'any' if re.search(r'或|任一|\bor\b', message, re.I) else 'all'
    if inherited_plan:
        if threshold is None and not re.search('不限薪资|取消金额|不限制工资', message):
            for field in ('threshold', 'operator', 'threshold_basis'):
                setattr(plan, field, getattr(inherited_plan, field))
        if not location and not re.search('不限地点|全国|所有城市', message):
            plan.location = inherited_plan.location
        if not plan.skills and not re.search('所有岗位|不限技能|取消技能', message):
            plan.skills, plan.skills_operator = inherited_plan.skills, inherited_plan.skills_operator
        plan.actual_salary = plan.actual_salary or inherited_plan.actual_salary
        if inherited_plan.clarification:
            annual = '当前金额筛选只支持' in inherited_plan.clarification
            monthly_correction = (threshold is not None and '月薪' in message and not re.search('年薪|年包|到手|税后', message)) or bool(re.search('不限薪资|取消金额|不限制工资', message))
            global_correction = bool(re.search('已保存公司|全部公司|全项目|所有公司', message))
            if (annual and not monthly_correction) or (not annual and not explicit_current and not global_correction):
                plan.clarification = inherited_plan.clarification
    # An unknown named company must not silently broaden an entity question.
    if scope['kind'] == 'global':
        entities = unknown_company_names(query, companies)
        if entities:
            plan.clarification = '资料库未识别到这个精确公司主体：' + '、'.join(entities) + '。请选择已保存公司或补充资料，本次没有扩大到全项目统计。'
    if threshold is not None and re.search('年薪|年包|年收入|税后|到手', message):
        plan.clarification = '当前金额筛选只支持招聘税前月薪。年薪或到手工资需要确认保证薪资月数、奖金或税费条件；请明确月薪条件，不能把年薪直接作为月薪过滤。'
    plan = enforce_scope(plan, scope, companies)
    plan.query = execution_query(plan, query)
    return plan
