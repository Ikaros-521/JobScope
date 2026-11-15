"""Collect -> normalise -> score -> word -> persist. The model never touches the number."""
import json

from ..repository import Repository, digest, now
from . import wording
from .models import (DISCLAIMER, DISCLAIMER_VERSION, DimensionScore, ReliabilityReport, ReportFinding,
                     SignalInput)
from .rubric import ASSESSMENT, RULES, RUBRIC_VERSION, score

# 重新计算时默认连带刷新的本地来源。零联网、零成本、幂等（证据身份含内容哈希、信号 INSERT OR
# IGNORE），但仍然尊重设置里的关闭状态——用户关掉就是不想要，哪怕它免费。
LOCAL_SOURCE = 'jobscope_jobs'


def persist_outcome(repo, identity, source_id, outcome, evidence_kind):
    """采集结果先落证据再落信号：证据仍是唯一的原始层，端点与自动刷新共用同一段落库代码。"""
    saved = []
    for item in outcome['signals']:
        evidence = repo.save_evidence(dict(title=f"{identity['name']} {item['rule']}", company=identity['name'],
                                           kind=evidence_kind,
                                           text=item.get('text') or json.dumps(item, sort_keys=True, ensure_ascii=False),
                                           url=item.get('url', ''), published_at=item.get('observed_at') or None), source_id)
        saved.append(repo.save_signal(SignalInput(
            company_id=identity['id'], dimension=item['dimension'], rule=item['rule'], value=item['value'],
            unit=item.get('unit', ''), source_class=item['source_class'], evidence_id=evidence['id'],
            observed_at=item.get('observed_at', ''), collected_at=item.get('collected_at') or now())))
    return saved


def company_materials(repo, identity):
    """该公司已保存的非岗位材料。名称按实体规范化比对，否则 '甲 公司' 存的证据永远配不上 '甲公司'。

    两类「自己写下的记录」都不算材料：本地计算记录会让计数随点击自增，付费接口原文则是
    取数结果而不是证据来源，喂给抽取器等于让厂商字段第二轮变成「材料」。
    """
    from .local import DERIVED_KIND
    from .paid import PAID_KIND
    known = {identity['name'], *(identity.get('aliases') or [])}
    return [item for item in repo.evidence(include_job_postings=False)
            if Repository.normalise_name(item['company']) in known
            and item['kind'] not in (DERIVED_KIND, PAID_KIND)]


def company_jd_materials(repo, identity):
    """完整 JD 的岗位证据（P1 起 team_size_statement 要从岗位描述里抽「团队 N 人」）。

    只有 description_status == 'complete' 的岗位进材料：半截 JD 不能用来评估，这是 P0
    在 /api/analyze 里就立过的规矩。证据行仍是 save_job 落的那条真实 job_posting 记录，
    evidence_id 可回查原文；列表摘要与别家公司照样不进。
    """
    known = {identity['name'], *(identity.get('aliases') or [])}
    complete = [item for item in repo.jobs()
                if Repository.normalise_name(item['company']) in known
                and item.get('description_status') == 'complete']
    wanted = {evidence_id for job in complete for evidence_id in job.get('evidence_ids', [])}
    return [item for item in repo.evidence(include_job_postings=True)
            if item['kind'] == 'job_posting' and item['id'] in wanted and item['text'].strip()]


def run_signal_extract(repo, company, credit_code='', *, limit=12, max_tokens=1200):
    """把已存材料喂给抽取器：手动导入的公告与材料不会自己变成信号。

    页面端点 /signals/extract 与 Agent 的 signal_extract 工具共用这一条管线：材料口径、
    team_size_statement 的多条自述聚合、任务记录只写一处。公司不存在/名称为空时抛出的
    ValueError 是写给用户看的中文说明，HTTP 层与 Agent 工具各自转述。
    """
    from . import extract
    from .extract import aggregate_team_size
    from .models import SignalInput
    from .rubric import RULES
    identity = repo.resolve_company(company, credit_code)
    # 非岗位材料 + 完整 JD 的岗位证据，按证据编号去重后一起进抽取器；只有完整职责可被评估。
    materials = {item['id']: item for item in [*company_materials(repo, identity), *company_jd_materials(repo, identity)]
                 if item['text'].strip()}
    materials = list(materials.values())[:limit]
    outcome = extract.extract(identity['name'], materials, max_tokens=max_tokens) if materials \
        else dict(status='skipped', message='该公司还没有可抽取的非岗位材料。', signals=[], raw='')
    task = repo.create_task('reliability_extract', identity['name'])
    kinds = {item['id']: item['kind'] for item in materials}
    # team_size_statement 的多条原始自述在这里聚合成一条信号：中位数与相容数由代码算，
    # 模型只交原始数字；repository.signals() 每条规则只留最新一条，聚合必须发生在落库前。
    size_signals = [item for item in outcome['signals'] if item.rule == 'team_size_statement']
    other_signals = [item for item in outcome['signals'] if item.rule != 'team_size_statement']
    merged, participants = aggregate_team_size(size_signals)
    aggregation_note = ''
    if merged is not None:
        aggregation_note = (f'team_size_statement 由 {len(participants)} 条自述聚合（中位数与相容数由代码计算，'
                            f'锚条编号 {merged.evidence_id}，参与编号：{"、".join(participants)}）。')
    saved = []
    # 聚合成功 → 只落合并后的一条；不足两条时原始自述照常落库（闸门会在报告里解释为什么不出分）。
    for item in ([*other_signals, merged] if merged is not None else outcome['signals']):
        spec = RULES[item.rule]
        saved.append(repo.save_signal(SignalInput(
            company_id=identity['id'], dimension=item.dimension, rule=item.rule, value=item.value,
            unit=spec.unit, source_class=extract.KIND_TO_SOURCE_CLASS.get(kinds.get(item.evidence_id, ''), 'user_material'),
            evidence_id=item.evidence_id, observed_at='', collected_at=now())))
    # 结构化状态与 raw 原文一起进 tasks，前端才显示得出真实状态、事后也查得到模型说了什么。
    # 聚合说明也写进任务：未成为锚条的参与编号在这里留档，出处链才查得回去。
    message = outcome['message'] + aggregation_note
    task.update(kind='reliability_extract', status=outcome['status'], message=message,
                count=len(saved), raw=str(outcome['raw'])[:4000])
    repo.update_task(task)
    return dict(status=outcome['status'], message=message, accepted=len(saved),
                signals=saved, evidence=len(materials), task=task, identity=identity)


def _report_finding(item):
    """material 类依据只出文字不出数字：那个数字量的是本项目的材料覆盖度。"""
    kind = RULES[item.rule].kind
    return ReportFinding(text=wording.render(item), rule=item.rule, kind=kind,
                         score=item.score if kind == ASSESSMENT else None,
                         source_class=item.source_class, evidence_id=item.evidence_id,
                         collected_at=item.collected_at, data=dict(item.data))


class ReliabilityService:
    def __init__(self, repo):
        self.repo = repo

    def company(self, name):
        return self.repo.resolve_company(name)

    def refresh_local(self, company):
        """重新计算前的本地源自动刷新。「重新计算」是用户最强的直觉动作，只对旧信号重打分会让
        一家明明存了十几条岗位的公司永远显示「尚无来源」——本地统计不联网不限速，刷新它再打分，
        缺口文案才会从「尚无来源」变成具体的样本闸门说明。"""
        from . import local
        from .sources import SignalSourceStore
        if not SignalSourceStore(self.repo).enabled(LOCAL_SOURCE):
            return None
        outcome = local.collect(LOCAL_SOURCE, company['name'], repo=self.repo, identity=company)
        saved = persist_outcome(self.repo, company, LOCAL_SOURCE, outcome, local.DERIVED_KIND)
        return dict(source=LOCAL_SOURCE, status=outcome['status'], message=outcome['message'], count=len(saved))

    def recompute(self, name, credit_code='', *, refresh_local=True):
        company = self.repo.resolve_company(name, credit_code)
        # Research has already run its selected local task; do not run it twice or bypass selection.
        collection = self.refresh_local(company) if refresh_local else None
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
                   'disclaimer': DISCLAIMER, 'collection': collection}
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

    def history(self, name, credit_code=''):
        """单公司历史快照与相邻差分。只对比**同一家公司自己**的两次计算：没有跨公司对比、
        没有排名、没有红黑榜——这个边界钉在红线测试里，diff 的输入永远来自同一个 company_id。"""
        company = self.repo.find_company(name, credit_code)
        if company is None:
            return None
        snapshots = self.repo.reliability_history(company['id'])
        if not snapshots:
            return None
        items = [_snapshot_summary(item) for item in snapshots]
        return dict(company=company['name'], company_id=company['id'], items=items,
                    diffs=[snapshot_diff(before, after) for before, after in zip(items, items[1:])])


def _snapshot_summary(report):
    """快照的紧凑视图：分数、覆盖度、各维度分、每条 assessment 规则的分数，以及 material 登记名单。
    material 的 score 恒为 None，所以它的「变化」只能体现在名单出现/消失上，绝不借用数字。"""
    return dict(computed_at=report['computed_at'], input_hash=report['input_hash'],
                score=report['score'], confidence=report['confidence'],
                dimensions={item['dimension']: item['score'] for item in report['dimensions']},
                rules={item['rule']: item['score'] for line in report['dimensions']
                       for item in line['findings'] if item['score'] is not None},
                materials=sorted(item['rule'] for line in report['dimensions']
                                 for item in line['findings'] if item['score'] is None))


def snapshot_diff(before, after):
    """把两次快照之间的变化归因到具体规则：assessment 看分数变化，material 看登记出现/消失。
    规则没变就不列；diff 的输入永远来自同一家公司的相邻两次快照。"""
    changed = []
    for rule in sorted(set(before['rules']) | set(after['rules'])):
        old, new = before['rules'].get(rule), after['rules'].get(rule)
        if old != new:
            changed.append(dict(rule=rule, before=old, after=new))
    appeared = sorted(set(after['materials']) - set(before['materials']))
    disappeared = sorted(set(before['materials']) - set(after['materials']))
    return dict(before=before['computed_at'], after=after['computed_at'],
                score_before=before['score'], score=after['score'], rules=changed,
                materials=[dict(rule=name, appeared=True) for name in appeared]
                          + [dict(rule=name, appeared=False) for name in disappeared])
