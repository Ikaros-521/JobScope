"""Bounded plan → tools → coverage → synthesis. Events are actions, not reasoning."""
import json
import time
import uuid

from ..chat import answer_question, citation, model_configured
from ..repository import now
from .index import ResearchIndex, scope_clause
from .providers import EmbeddingProvider, model_plan
from .query import plan_query
from .retrieval import retrieve
from .tools import salary_query, salary_text


class RunCancelled(Exception):
    """Raised at pipeline checkpoints after the user stopped the run; results are discarded."""


class ResearchAgent:
    def __init__(self, repo, synthesizer=None):
        self.repo = repo
        self.index = ResearchIndex(repo)
        self.synthesizer = synthesizer or answer_question

    def run(self, run_id):
        with self.repo.connection() as c:
            row = c.execute('SELECT payload FROM research_runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, run):
        with self.repo.connection() as c:
            c.execute('INSERT OR REPLACE INTO research_runs VALUES (?,?,?)', (run['id'], json.dumps(run, ensure_ascii=False), run['created_at']))

    def answer(self, message, scope, resume=None, history=None, on_event=None, should_stop=None, on_delta=None):
        start = time.perf_counter()
        run = dict(id='R-' + uuid.uuid4().hex, status='running', scope=scope, question=message,
                   created_at=now(), events=[], limits=dict(max_retrieval_calls=2, max_salary_calls=1, max_model_calls=2, context_characters=24000))
        self._save(run)

        def stop_requested():
            return bool(should_stop and should_stop())

        def event(tool, started, **data):
            item = dict(step=len(run['events']) + 1, tool=tool,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2), **data)
            run['events'].append(item)
            self._save(run)
            if on_event:
                on_event(item)
        try:
            if stop_requested():
                raise RunCancelled('已停止')
            t = time.perf_counter()
            synced = self.index.sync()
            event('index_sync', t, documents=synced['documents'], chunks=synced['chunks'], updated=synced['updated'])
            if stop_requested():
                raise RunCancelled('已停止')
            with self.repo.connection() as c:
                if scope['kind'] == 'global':
                    # Candidate names only; never inject company documents into the planner.
                    names = [r[0] for r in c.execute('SELECT DISTINCT company FROM research_docs ORDER BY company')]
                else:
                    names = [scope['company']]
            t = time.perf_counter()
            baseline = plan_query(message, history or [], scope, names)
            plan, planner_info = model_plan(message, history or [], baseline, scope, names)
            event('query_plan', t, plan=plan.model_dump(), provider=planner_info)
            if stop_requested():
                raise RunCancelled('已停止')
            if plan.clarification:
                coverage = dict(status='clarification_required', sources=0, companies=0, exhaustive=False)
                run.update(status='completed', plan=plan.model_dump(), strategy='clarification', coverage=coverage,
                           degraded=[], duration_ms=round((time.perf_counter() - start) * 1000, 2), context_characters=0, metrics=None, citation_ids=[])
                self._save(run)
                return dict(answer=plan.clarification, citations=[], mode='evidence_search', run_id=run['id'], trace=run, metrics=None, coverage=coverage)
            t = time.perf_counter()
            retrieved = retrieve(self.index, plan, scope, provider=EmbeddingProvider())
            event('hybrid_search', t, strategy=retrieved['strategy'], candidates=retrieved['candidates'],
                  lexical_candidates=retrieved['lexical_candidates'], dense_candidates=retrieved['dense_candidates'],
                  selected=len(retrieved['evidence']), terms=retrieved['terms'], degraded=retrieved['degraded'])
            if stop_requested():
                raise RunCancelled('已停止')
            evidence = retrieved['evidence']
            metrics = None
            if plan.intent == 'salary':
                t = time.perf_counter()
                metrics = salary_query(self.index, plan, scope)
                # Numeric quotations may not contain salary words. Read the exact
                # current source of analytical sample jobs, under the same scope.
                clause, values = scope_clause(scope, plan)
                with self.repo.connection() as c:
                    for sample in metrics['samples']:
                        eid = sample['evidence_id']
                        if not eid or any(e['id'] == eid for e in evidence):
                            continue
                        row = c.execute('''SELECT c.*,d.* FROM research_chunks c JOIN research_docs d ON d.evidence_id=c.evidence_id
                            WHERE d.evidence_id=? AND ''' + clause + ' ORDER BY c.start LIMIT 1', [eid, *values]).fetchone()
                        if row:
                            item = dict(row)
                            evidence.append(dict(id=eid, title=item['title'], company=item['company'], kind=item['kind'],
                                url=item['url'], source=item['source'], text=item['text'], published_at=item['published_at'],
                                collected_at=item['collected_at'], chunk_ids=[item['id']], locators=[dict(start=item['start'], end=item['end'], page=item['page'])]))
                # Salary-filtered results precede generic mentions of compensation.
                sample_ids = {s['evidence_id'] for s in metrics['samples']}
                evidence.sort(key=lambda e: e['id'] not in sample_ids)
                evidence = evidence[:8]
                event('salary_query', t, total_jobs=metrics['total_jobs'], matched_jobs=metrics['matched_jobs'],
                      known_monthly_jobs=metrics['known_monthly_jobs'], unknown_monthly_jobs=metrics['unknown_monthly_jobs'],
                      basis=metrics['threshold_basis'], aggregation='all_filtered_jobs')
            # One recovery: relax generated concept expansions, never scope, entity or numeric filters.
            if not evidence and plan.intent != 'salary' and plan.query != message:
                if stop_requested():
                    raise RunCancelled('已停止')
                t = time.perf_counter()
                recovery = plan.model_copy(update={'query': message[:1500]})
                recovered = retrieve(self.index, recovery, scope)
                evidence = recovered['evidence']
                event('supplemental_search', t, selected=len(evidence), strategy='bm25', reason='首次检索无证据，使用原始问题复查')
            # Hard budget applies to tool-read sources as well as search results.
            remaining = 24000
            for item in evidence:
                item['text'] = item['text'][:max(0, remaining)]
                remaining -= len(item['text'])
            evidence = [e for e in evidence if e['text']]
            coverage = dict(status='sufficient' if evidence or (metrics and metrics['known_monthly_jobs']) else 'insufficient',
                            sources=len(evidence), companies=len({e['company'] for e in evidence}),
                            scope='selected_evidence', exhaustive=False,
                            limitation='检索命中不是穷尽审阅；统计仅覆盖当前筛选下结构化可解析的招聘月薪')
            t = time.perf_counter()
            degraded = list(retrieved['degraded'])
            if planner_info and planner_info.get('degraded'):
                degraded.append(planner_info['degraded'])
            if evidence:
                try:
                    if self.synthesizer is answer_question:
                        result = self.synthesizer(message, evidence, resume=resume, history=history, preselected=True,
                                                  tool_context=dict(salary=metrics, coverage=coverage), on_delta=on_delta)
                    else:
                        # Compatibility seam for application-level injected providers/tests.
                        result = self.synthesizer(message, evidence, resume=resume, history=history)
                except ValueError:
                    degraded.append('回答模型失败或引用无效，展示已核实工具结果与证据摘录')
                    result = dict(answer='模型回答未通过校验。以下为证据摘录：\n\n' + '\n\n'.join(e['text'][:650] for e in evidence),
                                  citations=[citation(e) for e in evidence], mode='evidence_search')
            else:
                result = dict(answer='当前范围内没有找到相关的已保存证据。缺少资料，无法判断；可以补充来源或明确公司与问题。',
                              citations=[], mode='evidence_search')
            if metrics:
                result['answer'] = salary_text(metrics, plan) + '\n\n' + result['answer']
            allowed = {e['id'] for e in evidence}
            if any(c['id'] not in allowed for c in result.get('citations', [])):
                raise ValueError('研究回答引用超出工具返回范围')
            event('answer', t, mode=result['mode'], citations=len(result['citations']), coverage=coverage['status'])
            run.update(status='completed', plan=plan.model_dump(), strategy=retrieved['strategy'],
                       duration_ms=round((time.perf_counter() - start) * 1000, 2), coverage=coverage,
                       degraded=degraded, citation_ids=[c['id'] for c in result['citations']],
                       context_characters=24000 - remaining, metrics=metrics)
            self._save(run)
            return dict(**result, run_id=run['id'], trace=run, metrics=metrics, coverage=coverage)
        except RunCancelled:
            run.update(status='cancelled', duration_ms=round((time.perf_counter() - start) * 1000, 2))
            self._save(run)
            raise
        except Exception:
            run.update(status='failed', error='研究执行失败，未展示未经校验的答案', duration_ms=round((time.perf_counter() - start) * 1000, 2))
            self._save(run)
            raise
