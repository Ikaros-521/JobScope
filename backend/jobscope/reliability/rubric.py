"""The only place a number is produced. Pure functions, no clock, no network, no model."""
from dataclasses import dataclass, field

from .models import Dimension, RuleFinding, SignalInput, can_stand_alone

RUBRIC_VERSION = 'rubric-4'

# assessment 才允许产生维度分。material 衡量的只是本项目手上有多少材料：
# 把「9/10 条岗位能解析出月薪」算成薪资待遇 90 分，用户读到的是「薪资给得好」，这是静默的谎言。
ASSESSMENT = 'assessment'
MATERIAL = 'material'


@dataclass(frozen=True)
class DimensionSpec:
    label: str
    weight: float


DIMENSIONS: dict[str, DimensionSpec] = {
    'stability': DimensionSpec('稳定性', 0.22),
    'pay': DimensionSpec('薪资待遇', 0.20),
    'operations': DimensionSpec('实际经营', 0.18),
    'people': DimensionSpec('人员情况', 0.16),
    'professional': DimensionSpec('专业性', 0.12),
    'industry': DimensionSpec('行业前景', 0.12),
}


@dataclass(frozen=True)
class RuleSpec:
    dimension: Dimension
    unit: str
    ttl_days: int
    template: str
    weight: float = 1.0
    max_points: float = 1.0
    kind: str = ASSESSMENT
    # scorer 返回 None 时 missing 里的缺口文案：要说清缺了什么，而不是笼统一句「数字不足」。
    gate_note: str = ''
    # 只给 extract 的规则清单用：声明该规则 value 里的字段名与取数约束，加规则不用改提示词。
    extract_hint: str = ''
    scorer: object = field(default=None, compare=False)


def band(value, table, fallback):
    for minimum, points in table:
        if value >= minimum:
            return points
    return fallback


def _pay_points(value):
    """报价中位数分层比值。比值由统计层按「同城+同职类+同经验档+同学历档」分层加权得出；
    付费接口只给聚合中位数时退回未分层比值（口径不同，出处说明里写明来源）。
    闸门不满足就返回 None，靠 gate_note 说清缺什么，绝不硬算。"""
    ratio = value.get('ratio')
    if not ratio:
        company_median, city_median = value.get('company_median'), value.get('city_median')
        ratio = company_median / city_median if company_median and city_median and city_median > 0 else None
    if not ratio or ratio <= 0:
        return None
    if (value.get('company_jobs') or 0) < 3 or (value.get('city_jobs') or 0) < 20:
        return None
    return band(ratio, [(1.2, 0.95), (1.0, 0.8), (0.85, 0.6), (0.7, 0.45)], 0.3)


def _jd_points(value):
    """岗位描述具体程度。归一上限写在这里：600 字 / 8 条职责 / 8 项技术栈是「把要做什么讲清楚」的常见量级，
    权重 0.25/0.30/0.25/0.20 合计 1.0；量的是描述具体与否，不是技术实力。"""
    if (value.get('jobs') or 0) < 3:
        return None
    chars = min(1.0, (value.get('chars_mean') or 0) / 600)
    items = min(1.0, (value.get('items_mean') or 0) / 8)
    skills = min(1.0, (value.get('skills_mean') or 0) / 8)
    share = min(1.0, max(0.0, value.get('quantified_share') or 0))
    return 0.25 * chars + 0.30 * items + 0.25 * skills + 0.20 * share


def _team_size_points(value):
    """团队规模自述的一致性。分数与规模大小完全无关：大公司写 5000-10000 人不是矛盾。
    compatible 是聚合层算出的「最大相互兼容自述子集」条数，share 只衡量自述之间说不说得拢。"""
    statements, compatible = value.get('statements') or 0, value.get('compatible') or 0
    if statements < 2 or not value.get('median_size'):
        return None
    share = compatible / statements
    return 0.9 if share >= 1 else (0.6 if share >= 0.66 else 0.3)


def _same_material_trend_points(value):
    """同一份公告里本期与上期的同比。上期缺失或非正就不出分——同比没有分母就是没有口径。"""
    current, previous = value.get('current'), value.get('previous')
    if current is None or previous is None or previous <= 0:
        return None
    return band((current - previous) / previous,
                [(0.2, 0.95), (0.05, 0.8), (-0.05, 0.65), (-0.2, 0.45)], 0.25)


RULES: dict[str, RuleSpec] = {
    'company_age': RuleSpec('stability', '年', 365, 'company_age',
                            scorer=lambda v: min(1.0, (v.get('years') or 0) / 10)),
    'insured_headcount_trend': RuleSpec('stability', '人', 400, 'headcount_trend',
                                        scorer=lambda v: max(0.0, min(1.0, 0.5 + ((v.get('after', 0) - v.get('before', 0)) / max(v.get('before', 1), 1)) / 2))),
    # 检索不到案件可能是覆盖面不够，不是正面结论；ratio 为 0 时不产出分数。
    'judicial_case_ratio': RuleSpec('stability', '件/万人', 180, 'judicial_cases',
                                    gate_note='检索不到案件数可能是检索覆盖不足，案件数为 0 不作为正面结论',
                                    scorer=lambda v: band(v['ratio'], [(25, 0.3), (10, 0.55), (3, 0.75)], 0.9) if v.get('ratio') else None),
    'salary_quote_coverage': RuleSpec('pay', '%', 120, 'salary_quote_coverage', kind=MATERIAL,
                                      scorer=lambda v: (v.get('known') or 0) / max(v.get('total') or 1, 1)),
    # R1：同城同层（职类/经验档/学历档一致）的报价中位数分层比值。样本闸门分两层：统计层
    # 每层本公司 ≥3 条、同层 ≥10 条，scorer 合计 ≥3/≥20；分层凑不齐就不出分——混层中位数
    # 是拿低层级岗位对标高层级基线，比不出分更误导。
    'pay_vs_city_median': RuleSpec('pay', '元', 90, 'pay_vs_city_median',
                                   gate_note='出分需要可比分层：同城、同职类、同经验与学历要求下本公司 ≥3 条、同层 ≥10 条，且合计本公司 ≥3 条、同层 ≥20 条；分层凑不齐就不比，缺口会写明缺哪一层',
                                   scorer=_pay_points),
    # R2：只统计完整 JD（description_status=complete），列表摘要不参与——半截描述评不了「讲清楚没有」。
    'jd_specificity': RuleSpec('professional', '条', 180, 'jd_specificity',
                               gate_note='出分需要 ≥3 条完整岗位描述（完整职责未采集、只有列表摘要的岗位不参与）；样本不够就不评',
                               scorer=_jd_points),
    # R3：公司口径的规模自述一致程度。value 由抽取端把多条自述聚合成一条（median_size/statements/compatible）。
    'team_size_statement': RuleSpec('people', '人', 240, 'team_size_statement',
                                    gate_note='至少需要 2 条团队规模自述（如「团队 N 人」）才能比较它们是否一致；只有一条时不判断',
                                    extract_hint='每条材料单独一条，value 只填 {"size": 数字}；材料写的是区间（如 5000-10000 人）时另加 "size_max"；聚合由本项目代码完成，不要自己算中位数',
                                    scorer=_team_size_points),
    # R4/R5 刻意拆成两条而不是用 metric 字符串参数区分：_for_signal() 只保留数字字段，字符串塞不进去。
    'disclosure_revenue_trend': RuleSpec('operations', '元', 365, 'disclosure_revenue_trend',
                                         gate_note='需要同一份公告里的本期与上期营收数字，且上期大于 0，才能算同比',
                                         extract_hint='value 填 {"current": 本期数字, "previous": 上期数字}，两个数字必须来自同一份公告，禁止跨材料或跨年份拼接',
                                         scorer=_same_material_trend_points),
    'disclosure_headcount_trend': RuleSpec('operations', '人', 365, 'disclosure_headcount_trend',
                                           gate_note='需要同一份公告里的本期与上期在职人数，且上期大于 0，才能算同比',
                                           extract_hint='value 填 {"current": 本期数字, "previous": 上期数字}，两个数字必须来自同一份公告，禁止跨材料或跨年份拼接',
                                           scorer=_same_material_trend_points),
    'employee_review_sample': RuleSpec('people', '条', 180, 'employee_review_sample', kind=MATERIAL,
                                       scorer=lambda v: min(1.0, (v.get('samples') or 0) / 10)),
    'disclosure_available': RuleSpec('operations', '份', 365, 'disclosure_available', kind=MATERIAL,
                                     scorer=lambda v: min(1.0, (v.get('count') or 0) / 4)),
    # R6：industry 维度 P1 不出分，只登记手上有几条行业与媒体材料，让缺口从「什么都没有」变成「有材料但无可评价依据」。
    'industry_media_count': RuleSpec('industry', '条', 365, 'industry_media_count', kind=MATERIAL,
                                     scorer=lambda v: min(1.0, (v.get('count') or 0) / 4)),
}

ASSESSING_BY_DIMENSION = {name: [rule for rule, spec in RULES.items() if spec.dimension == name and spec.kind == ASSESSMENT] for name in DIMENSIONS}


@dataclass(frozen=True)
class DimensionOutcome:
    dimension: str
    label: str
    score: float | None
    confidence: float
    findings: list[RuleFinding]
    evidence_ids: list[str]
    missing: list[str]


@dataclass(frozen=True)
class RubricResult:
    score: float | None
    confidence: float
    dimensions: list[DimensionOutcome]
    findings: list[RuleFinding]
    stability_score: float | None = None
    pay_score: float | None = None
    operations_score: float | None = None
    people_score: float | None = None
    professional_score: float | None = None
    industry_score: float | None = None


def _for_signal(signal: SignalInput, spec: RuleSpec):
    """Returns a finding, or a gap sentence explaining why this signal cannot settle anything."""
    value = {key: number for key, number in signal.value.items() if isinstance(number, (int, float)) and not isinstance(number, bool)}
    if not value:
        return None, f'{signal.rule}（数值不完整，不计分）'
    points = spec.scorer(value)
    if points is None:
        # 缺口文案必须说清缺了什么：gate_note 按规则写死阈值，不再笼统一句「数字不足」。
        return None, f'{signal.rule}（{spec.gate_note or "现有数字不足以判断"}）'
    return RuleFinding(rule=signal.rule, dimension=spec.dimension, score=round(100 * max(0.0, min(1.0, points)), 1),
                       template=spec.template, data=value, evidence_id=signal.evidence_id,
                       source_class=signal.source_class, weight=spec.weight,
                       collected_at=signal.collected_at), ''


def score(signals, stale_rules=()):
    """Aggregate observed assessment rules only. Everything else is removed from both sides of the average."""
    findings, notes, sourced = [], {}, set()
    for signal in signals:
        spec = RULES.get(signal.rule)
        if spec is None:
            continue
        # 「尚无来源」只留给真的没有信号的规则：有信号但过期/闸门不满足的，缺口已有
        # 专属说明（已过期 / gate_note），不再叠加一句自相矛盾的「尚无来源」。
        sourced.add(signal.rule)
        if signal.rule in stale_rules:
            notes.setdefault(spec.dimension, []).append(f'{signal.rule}（已过期，超出 {spec.ttl_days} 天）')
            continue
        finding, note = _for_signal(signal, spec)
        if finding is None:
            notes.setdefault(spec.dimension, []).append(note)
            continue
        findings.append(finding)

    dimensions, per_dimension = [], {}
    for name, spec in DIMENSIONS.items():
        observed = [item for item in findings if item.dimension == name]
        usable = [item for item in observed if can_stand_alone(item.source_class)]
        blocked = [item for item in observed if item not in usable]
        rated = [item for item in usable if RULES[item.rule].kind == ASSESSMENT]
        # 自述与材料类依据照样展示文字和出处，只是不进加权。
        if blocked and not usable:
            notes.setdefault(name, []).append('仅有未经独立核实的自述材料，不计入分数')
        if rated:
            weight_sum = sum(item.weight for item in rated)
            value = round(sum(item.score * item.weight for item in rated) / weight_sum, 1)
            per_dimension[name] = value
            confidence = round(min(1.0, weight_sum / len(ASSESSING_BY_DIMENSION[name])), 3)
        else:
            value, confidence = None, 0.0
            if not ASSESSING_BY_DIMENSION[name]:
                # 没有可评价的规则时也要留下可行动的说明，否则用户看到一个空白维度。
                notes.setdefault(name, []).append('该维度还没有可评价的规则，下面只是已收集到的材料')
        gaps = [f'{rule}（尚无来源）' for rule in ASSESSING_BY_DIMENSION[name] if rule not in sourced]
        dimensions.append(DimensionOutcome(
            dimension=name, label=spec.label, score=value, confidence=confidence,
            findings=usable + blocked,
            evidence_ids=list(dict.fromkeys([item.evidence_id for item in usable + blocked])),
            missing=[*notes.get(name, []), *gaps]))
    weighted = sum(per_dimension[name] * DIMENSIONS[name].weight for name in per_dimension)
    denominator = sum(DIMENSIONS[name].weight for name in per_dimension)
    overall = round(weighted / denominator, 1) if denominator else None
    return RubricResult(score=overall, confidence=round(denominator, 3), dimensions=dimensions, findings=findings,
                        **{f'{name}_score': per_dimension.get(name) for name in DIMENSIONS})
