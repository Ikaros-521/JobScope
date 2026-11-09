"""Collect -> normalise -> score -> word -> persist. The model never touches the number."""
import json

from ..repository import digest, now
from . import wording
from .models import (DISCLAIMER, DISCLAIMER_VERSION, DimensionScore, ReliabilityReport, ReportFinding,
                     SignalInput)
from .rubric import ASSESSMENT, RULES, RUBRIC_VERSION, score


def _report_finding(item):
    """material 类依据只出文字不出数字：那个数字量的是本项目的材料覆盖度。"""
    kind = RULES[item.rule].kind
    return ReportFinding(text=wording.render(item), rule=item.rule, kind=kind,
                         score=item.score if kind == ASSESSMENT else None,
                         source_class=item.source_class, evidence_id=item.evidence_id)


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
                                     findings=[_report_finding(finding) for finding in item.findings],
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
        """会确保实体存在的读法：resolve 可能写出 companies 行，只读场景请用 peek()。"""
        company = self.repo.resolve_company(name, credit_code)
        return self.repo.latest_reliability(company['id'])

    def peek(self, name, credit_code=''):
        """没有副作用的读入口：公司不存在或从没算过都返回 None，不写任何一行。"""
        company = self.repo.find_company(name, credit_code)
        return None if company is None else self.repo.latest_reliability(company['id'])
