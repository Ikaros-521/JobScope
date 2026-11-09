"""Collect -> normalise -> score -> word -> persist. The model never touches the number."""
import json

from ..repository import digest, now
from . import wording
from .models import (DISCLAIMER, DISCLAIMER_VERSION, DimensionScore, ReliabilityReport, ReportFinding,
                     SignalInput)
from .rubric import RULES, RUBRIC_VERSION, score


class ReliabilityService:
    def __init__(self, repo):
        self.repo = repo

    def company(self, name):
        return self.repo.resolve_company(name)

    def recompute(self, name, credit_code=''):
        company = self.repo.resolve_company(name, credit_code)
        stored = self.repo.signals(company['id'], ttl_days={rule: spec.ttl_days for rule, spec in RULES.items()})
        stale = {item['rule'] for item in stored if item['stale']}
        parsed = [SignalInput(**{key: value for key, value in item.items() if key in SignalInput.model_fields}) for item in stored]
        result = score(parsed, stale_rules=stale)
        wording.assert_clean(result)
        # 措辞在这里且只在这里渲染成句子；再算一次同样输入会得到同样的文字。
        dimensions = [DimensionScore(dimension=item.dimension, label=item.label, score=item.score,
                                     confidence=item.confidence,
                                     findings=[ReportFinding(text=wording.render(finding), rule=finding.rule,
                                                             score=finding.score, source_class=finding.source_class,
                                                             evidence_id=finding.evidence_id) for finding in item.findings],
                                     evidence_ids=item.evidence_ids, missing=item.missing) for item in result.dimensions]
        report = ReliabilityReport(
            company_id=company['id'], company=company['name'], score=result.score, confidence=result.confidence,
            dimensions=dimensions,
            rubric_version=RUBRIC_VERSION, wording_version=wording.WORDING_VERSION,
            disclaimer_version=DISCLAIMER_VERSION, computed_at=now(),
            input_hash=digest(json.dumps([[item['id'], item['payload']] for item in self.repo.raw_signals(company['id'])], sort_keys=True, ensure_ascii=False)),
            evidence_ids=list(dict.fromkeys([item.evidence_id for item in result.findings])),
            limitations=['分数只反映 JobScope 当前已保存材料能说明的范围，样本量小、来源单一或口径不一致时会明显偏高或偏低。',
                         '参保人数、涉诉数量等公开数字有多种成因，本工具不给因果结论。'])
        payload = {**report.model_dump(), 'summary': wording.overall_text(report.score, report.confidence),
                   'disclaimer': DISCLAIMER}
        self.repo.save_reliability(payload)
        return payload

    def latest(self, name, credit_code=''):
        company = self.repo.resolve_company(name, credit_code)
        return self.repo.latest_reliability(company['id'])
