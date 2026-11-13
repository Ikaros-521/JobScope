"""Scoped BM25 + optional cosine recall, RRF, version/content dedup, budgets."""
import json
import math
import re
from collections import defaultdict

from ..repository import digest
from .index import scope_clause, validate_vectors
from .query import tokenize


def retrieve(index, plan, scope, provider=None, limit=8, budget=24000):
    if plan.clarification:
        return dict(evidence=[], strategy='clarification', candidates=0, lexical_candidates=0,
                    dense_candidates=0, terms=[], degraded=[plan.clarification])
    index.sync()
    clause, params = scope_clause(scope, plan)
    terms = plan.terms()
    # The relevance floor counts original query terms only; company tokens injected
    # below widen recall inside a narrow scope, they never raise the bar.
    minimum_hits = 1 if plan.concepts else max(1, math.ceil(len(terms) * .4))
    company_tokens = set()
    if scope['kind'] != 'global':
        # Colloquial prompts ("这是个什么公司") may carry zero searchable terms. The SQL
        # clause already restricts candidates to this company, so its own name is the
        # strongest safe recall signal there.
        company_tokens = set(tokenize(scope['company']))
        terms += [token for token in company_tokens if token not in terms]
    hits = []
    with index.repo.connection() as c:
        if terms:
            # Tokens are quoted; raw user syntax never enters MATCH or SQL.
            expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
            hits = [dict(row) for row in c.execute('''SELECT c.*,d.company,d.kind,d.job_id,d.title,d.url,d.source,d.published_at,d.collected_at,
                bm25(research_fts,1.0,2.0,2.5) AS distance FROM research_fts
                JOIN research_chunks c ON c.id=research_fts.rowid JOIN research_docs d ON d.evidence_id=c.evidence_id
                WHERE research_fts MATCH ? AND ''' + clause + ' ORDER BY distance,c.id LIMIT 80', [expression, *params])]
            # Entity names constrain scope; they are not proof of content relevance.
            # A company-name hit is definitive inside its own scope, though.
            def relevant(item):
                matched = set(terms) & set(tokenize(item['title'] + ' ' + item['text']))
                return len(matched) >= minimum_hits or (company_tokens and matched & company_tokens)
            hits = [item for item in hits if relevant(item)]
        vectors = []
        if provider and provider.configured:
            vectors = [dict(row) for row in c.execute('''SELECT c.*,d.company,d.kind,d.job_id,d.title,d.url,d.source,d.published_at,d.collected_at,v.vector
                FROM research_chunks c JOIN research_docs d ON d.evidence_id=c.evidence_id
                JOIN research_vectors v ON v.content_hash=c.content_hash AND v.model=? WHERE ''' + clause, [provider.identity, *params])]
    ranks, records = defaultdict(float), {}
    channels = defaultdict(set)
    for rank, item in enumerate(hits, 1):
        ranks[item['id']] += 1 / (60 + rank)
        records[item['id']] = item
        channels[item['id']].add('bm25')
    degraded, dense_hits = [], []
    strategy = 'bm25'
    if provider and provider.configured:
        if not vectors:
            degraded.append('向量索引尚未建立；请在数据与采集页构建，当前使用BM25')
        else:
            try:
                query_vector = provider.embed([plan.query])
                validate_vectors(query_vector, 1)
                query_vector = query_vector[0]
                qnorm = math.sqrt(sum(x * x for x in query_vector))
                for item in vectors:
                    vector = json.loads(item['vector'])
                    validate_vectors([vector], 1)
                    if len(vector) != len(query_vector):
                        raise ValueError('向量维度不一致')
                    similarity = sum(a * b for a, b in zip(vector, query_vector)) / (qnorm * math.sqrt(sum(x * x for x in vector)))
                    if similarity >= .60:
                        dense_hits.append((similarity, item))
                dense_hits.sort(key=lambda pair: pair[0], reverse=True)
                # A conservative absolute floor and relative band prevent weak,
                # generic nearest neighbours from being treated as evidence.
                if dense_hits:
                    dense_hits = [pair for pair in dense_hits if pair[0] >= dense_hits[0][0] - .08]
                for rank, (_, item) in enumerate(dense_hits[:80], 1):
                    ranks[item['id']] += 1 / (60 + rank)
                    records[item['id']] = item
                    channels[item['id']].add('dense')
                strategy = 'hybrid'
            except (ValueError, TypeError, ArithmeticError):
                degraded.append('向量召回失败或维度无效，已降级为BM25')
    def rerank(key):
        item = records[key]
        boost = 1.0
        if 'employees' in plan.concepts:
            if re.search(r'(员工总数|在职人员|人员共|人数|headcount).{0,12}\d|\d+\s*(人|名员工)', item['text'], re.I):
                boost += .35
            if item['kind'] == 'disclosure':
                boost += .15
        if 'business' in plan.concepts and item['kind'] == 'disclosure':
            boost += .15
        return ranks[key] * boost
    candidates = sorted(ranks, key=rerank, reverse=True)
    selected, seen_hashes, company_counts, used = {}, set(), defaultdict(int), 0
    for key in candidates:
        item = records[key]
        eid = item['evidence_id']
        content_key = (item['company'], digest(item['text']))
        if content_key in seen_hashes:
            continue
        if eid not in selected and len(selected) >= limit:
            continue
        # Avoid one prolific company occupying all slots in broad comparisons.
        if scope['kind'] == 'global' and len(plan.companies) != 1 and eid not in selected and company_counts[item['company']] >= 3:
            continue
        if eid in selected and len(selected[eid]['chunk_ids']) >= 2:
            continue
        text = item['text'][:min(3500, budget - used)]
        if not text:
            break
        if eid not in selected:
            selected[eid] = {k: item[k] for k in ('company', 'title', 'url', 'source', 'kind', 'published_at', 'collected_at')}
            selected[eid].update(id=eid, text='', chunk_ids=[], locators=[], retrieval_score=round(ranks[key], 6), channels=[])
            company_counts[item['company']] += 1
        record = selected[eid]
        record['text'] += ('\n…\n' if record['text'] else '') + text
        record['chunk_ids'].append(item['id'])
        record['locators'].append(dict(start=item['start'], end=item['end'], page=item['page']))
        record['channels'] = sorted(set(record['channels']) | channels[key])
        seen_hashes.add(content_key)
        used += len(text)
    return dict(evidence=list(selected.values()), strategy=strategy, degraded=degraded,
                candidates=len(records), lexical_candidates=len(hits), dense_candidates=min(80, len(dense_hits)),
                context_characters=used, budget=budget, terms=terms)
