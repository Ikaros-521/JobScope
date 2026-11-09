"""The only place a number is produced. Pure functions, no clock, no network, no model."""
from dataclasses import dataclass, field

from .models import Dimension, RuleFinding, SignalInput, can_stand_alone

RUBRIC_VERSION = 'rubric-1'


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
    scorer: object = field(default=None, compare=False)


def band(value, table, fallback):
    for minimum, points in table:
        if value >= minimum:
            return points
    return fallback


RULES: dict[str, RuleSpec] = {
    'company_age': RuleSpec('stability', '年', 365, 'company_age',
                            scorer=lambda v: min(1.0, (v.get('years') or 0) / 10)),
    'insured_headcount_trend': RuleSpec('stability', '人', 400, 'headcount_trend',
                                        scorer=lambda v: max(0.0, min(1.0, 0.5 + ((v.get('after', 0) - v.get('before', 0)) / max(v.get('before', 1), 1)) / 2))),
    'judicial_case_ratio': RuleSpec('stability', '件/万人', 180, 'judicial_cases',
                                    scorer=lambda v: band(v.get('ratio', 0), [(25, 0.3), (10, 0.55), (3, 0.75), (0.0001, 0.9)], 1.0)),
    'salary_quote_coverage': RuleSpec('pay', '%', 120, 'salary_quote_coverage',
                                      scorer=lambda v: (v.get('known') or 0) / max(v.get('total') or 1, 1)),
    'employee_review_sample': RuleSpec('people', '条', 180, 'employee_review_sample',
                                       scorer=lambda v: min(1.0, (v.get('samples') or 0) / 10)),
    'disclosure_available': RuleSpec('operations', '份', 365, 'disclosure_available',
                                     scorer=lambda v: min(1.0, (v.get('count') or 0) / 4)),
}


@dataclass(frozen=True)
class DimensionOutcome:
    dimension: str
    label: str
    score: float | None
    confidence: float
    findings: list[RuleFinding]
    evidence_ids: list[str]
    missing: list[str]


RULES_BY_DIMENSION = {name: [rule for rule, spec in RULES.items() if spec.dimension == name] for name in DIMENSIONS}


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
    value = {key: number for key, number in signal.value.items() if isinstance(number, (int, float))}
    if not value:
        return None
    points = spec.scorer(value)
    if points is None:
        return None
    return RuleFinding(rule=signal.rule, dimension=spec.dimension, score=round(100 * max(0.0, min(1.0, points)), 1),
                       template=spec.template, data=value, evidence_id=signal.evidence_id,
                       source_class=signal.source_class, weight=spec.weight)


def score(signals, stale_rules=()):
    """Aggregate observed rules only. Unobserved weight is removed from both sides of the average."""
    findings, missing = [], {}
    for signal in signals:
        spec = RULES.get(signal.rule)
        if spec is None:
            continue
        if signal.rule in stale_rules:
            missing.setdefault(spec.dimension, []).append(f'{signal.rule}（已过期，超出 {spec.ttl_days} 天）')
            continue
        finding = _for_signal(signal, spec)
        if finding is None:
            missing.setdefault(spec.dimension, []).append(f'{signal.rule}（数值不完整）')
            continue
        findings.append(finding)

    unobserved = {name: [rule for rule in RULES_BY_DIMENSION[name] if rule not in {item.rule for item in findings}] for name in DIMENSIONS}
    dimensions, per_dimension = [], {}
    for name, spec in DIMENSIONS.items():
        observed = [item for item in findings if item.dimension == name]
        usable = [item for item in observed if can_stand_alone(item.source_class)]
        blocked = [item for item in observed if item not in usable]
        # 自述类依据照样展示原文与出处，只是不参与算分。
        if blocked and not usable:
            missing.setdefault(name, []).append('仅有未经独立核实的自述材料，不计入分数')
        if usable:
            weight_sum = sum(item.weight for item in usable)
            value = round(sum(item.score * item.weight for item in usable) / weight_sum, 1)
            per_dimension[name] = value
            confidence = round(min(1.0, weight_sum / len(RULES_BY_DIMENSION[name])), 3)
        else:
            value, confidence = None, 0.0
        dimensions.append(DimensionOutcome(
            dimension=name, label=spec.label, score=value, confidence=confidence,
            findings=usable + blocked,
            evidence_ids=list(dict.fromkeys([item.evidence_id for item in usable + blocked])),
            missing=missing.get(name, [f'{rule}（尚无来源）' for rule in unobserved[name]])))
    weighted = sum(per_dimension[name] * DIMENSIONS[name].weight for name in per_dimension)
    denominator = sum(DIMENSIONS[name].weight for name in per_dimension)
    overall = round(weighted / denominator, 1) if denominator else None
    confidence = round(denominator, 3)
    return RubricResult(score=overall, confidence=confidence, dimensions=dimensions, findings=findings,
                        **{f'{name}_score': per_dimension.get(name) for name in DIMENSIONS})
