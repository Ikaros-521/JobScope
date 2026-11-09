"""Reproducible retrieval ablation, isolated synthetic corpus, no model calls."""
import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from jobscope.chat import rank_evidence
from jobscope.repository import Repository
from jobscope.research.index import ResearchIndex
from jobscope.research.query import plan_query
from jobscope.research.retrieval import retrieve
from jobscope.research.providers import EmbeddingProvider
from jobscope.research.tools import salary_query


def evaluate(dense=False):
    dataset = json.loads((Path(__file__).with_name('retrieval.json')).read_text(encoding='utf-8'))
    directory = ROOT / 'output' / ('eval-' + uuid.uuid4().hex[:10])
    repo = Repository(directory)
    keys = {}
    for document in dataset['documents']:
        data = {k: v for k, v in document.items() if k != 'key'}
        if data['kind'] == 'job_posting':
            saved = repo.save_job(dict(title=data['title'], company=data['company'], description=data['text'],
                                      external_id=document['key']), 'eval')
            eid = saved['evidence_ids'][-1]
        else:
            eid = repo.save_evidence(data, 'eval')['id']
        keys[document['key']] = eid
    for i in range(dataset['distractors']):
        repo.save_evidence(dict(title=f'公司概况{i}', company='北辰科技' if i % 2 else '南屿科技',
                               text='公司介绍，公司业务，公司文化，公司欢迎人才加入。'), 'eval')
    index = ResearchIndex(repo)
    index.sync()
    companies = [d['company'] for d in dataset['documents']]
    strategies = {'legacy_keywords': [], 'bm25_concepts': []}
    provider = EmbeddingProvider() if dense else None
    if dense:
        if not provider.configured:
            raise ValueError('--dense需要配置真实Embedding')
        index.embed(provider)
        strategies['hybrid_dense'] = []
    for case in dataset['cases']:
        scope = case['scope']
        expected = {keys[k] for k in case['relevant']}
        raw = repo.evidence(company=scope.get('company'))
        for strategy in strategies:
            start = time.perf_counter()
            if strategy == 'legacy_keywords':
                result = rank_evidence(case['question'], raw)
            else:
                plan = plan_query(case['question'], case.get('history', []), scope, companies)
                result = [] if plan.clarification else retrieve(index, plan, scope, provider=provider if strategy == 'hybrid_dense' else None)['evidence']
            elapsed = (time.perf_counter() - start) * 1000
            ids = [e['id'] for e in result[:8]]
            recall = len(expected & set(ids)) / len(expected) if expected else None
            first = next((1 / (i + 1) for i, eid in enumerate(ids) if eid in expected), 0) if expected else None
            leak = sum(e['company'] != scope['company'] for e in result) if scope['kind'] != 'global' else 0
            strategies[strategy].append(dict(id=case['id'], recall_at_8=recall, reciprocal_rank=first,
                precision_at_8=len(expected & set(ids)) / len(ids) if ids and expected else (1 if not ids and not expected else 0),
                correct_empty=not ids if not expected else None, scope_leaks=leak,
                latency_ms=round(elapsed, 3), returned=ids, expected=sorted(expected)))
    report = dict(dataset=dataset['version'], provenance=dataset['provenance'], cases=len(dataset['cases']),
                  corpus_documents=len(repo.evidence()), k=8, methods={},
                  dense=dict(status='run' if dense else 'not_run', model=provider.identity if provider else None, reason='真实模型编码与精确余弦召回' if dense else '本轮未配置真实Embedding；不把测试向量作为语义质量证据'),
                  model_answer_quality=dict(status='not_run', reason='本评测只评价检索和范围，不评价模型生成事实支持度'))
    for strategy, cases in strategies.items():
        latencies = sorted(c['latency_ms'] for c in cases)
        report['methods'][strategy] = dict(recall_at_8=round(statistics.mean(c['recall_at_8'] for c in cases if c['recall_at_8'] is not None), 4),
            mrr=round(statistics.mean(c['reciprocal_rank'] for c in cases if c['reciprocal_rank'] is not None), 4),
            mean_precision_at_8=round(statistics.mean(c['precision_at_8'] for c in cases), 4),
            no_result_accuracy=statistics.mean(c['correct_empty'] for c in cases if c['correct_empty'] is not None),
            scope_leaks=sum(c['scope_leaks'] for c in cases), latency_p50_ms=statistics.median(latencies),
            latency_p95_ms=latencies[min(len(latencies) - 1, int(len(latencies) * .95))], cases=cases)
    # Exact salary checks use all facts, independently of retrieval k.
    for i in range(12):
        repo.save_job(dict(title='Python岗位', company='北辰科技', external_id='salary-' + str(i), location='杭州', salary='26-35K·最高16薪', description='RAG研发'), 'eval')
    plan = plan_query('杭州RAG岗位工资超过25K有多少', [], {'kind': 'global'}, companies)
    salary = salary_query(index, plan, {'kind': 'global'})
    report['salary_accuracy'] = dict(cases=1, passed=int(salary['matched_jobs'] == 12), accuracy=int(salary['matched_jobs'] == 12), expected=12, actual=salary['matched_jobs'])
    path = ROOT / 'output' / ('research-evaluation-hybrid.json' if dense else 'research-evaluation.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'methods'}, ensure_ascii=False))
    print(json.dumps({name: {k: v for k, v in value.items() if k != 'cases'} for name, value in report['methods'].items()}, ensure_ascii=False))
    print(str(path))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--dense', action='store_true')
    args = parser.parse_args()
    report = evaluate(args.dense)
    metrics = report['methods']['bm25_concepts']
    hybrid = report['methods'].get('hybrid_dense', metrics)
    if args.check and (metrics['recall_at_8'] < .85 or metrics['no_result_accuracy'] != 1 or metrics['scope_leaks'] or report['salary_accuracy']['accuracy'] != 1 or hybrid['no_result_accuracy'] != 1):
        raise SystemExit('Retrieval quality gate failed; inspect per-case report')
