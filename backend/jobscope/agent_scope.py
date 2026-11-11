"""Server-owned retrieval boundaries for the unified agent's project tools."""
from dataclasses import dataclass


class ScopeBoundaryError(ValueError):
    """A project tool attempted to read outside the conversation boundary."""


CROSS_COMPANY_RULES = frozenset({'pay_vs_city_median'})
# These local computations use this company's records only. Unknown derived
# records have no reliable provenance and are not admitted in a narrow scope.
COMPANY_LOCAL_RULES = frozenset({'salary_quote_coverage', 'disclosure_available',
                                'industry_media_count', 'jd_specificity'})


@dataclass(frozen=True)
class AgentScope:
    kind: str = 'global'
    company: str = ''
    job_id: str = ''

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or value.get('kind') not in {'global', 'company', 'job'}:
            raise ValueError('无效的会话范围')
        kind = value['kind']
        company = value.get('company', '')
        job_id = value.get('job_id', '')
        if kind != 'global' and (not isinstance(company, str) or not company.strip()):
            raise ValueError('会话范围缺少公司主体')
        if kind == 'job' and (not isinstance(job_id, str) or not job_id.strip()):
            raise ValueError('会话范围缺少岗位ID')
        # Legacy conversations may carry agent_mode; mode grants no data access.
        return cls(kind, company if kind != 'global' else '', job_id if kind == 'job' else '')

    def as_dict(self):
        result = dict(kind=self.kind)
        if self.kind != 'global':
            result['company'] = self.company
        if self.kind == 'job':
            result['job_id'] = self.job_id
        return result

    def boundary_message(self):
        label = '全项目' if self.kind == 'global' else f'公司「{self.company}」'
        if self.kind == 'job':
            label += f'的当前岗位「{self.job_id}」及同公司非岗位来源'
        return (f'当前会话范围为{label}，该项目资料或衍生统计超出范围，未执行读取。'
                '如需扩大范围，请调用 request_scope_expansion 申请公司或全局范围，等待用户点击卡片后在新会话继续。')

    def require_company(self, company):
        if self.kind != 'global' and company != self.company:
            raise ScopeBoundaryError(self.boundary_message())

    def allows_evidence(self, repo, item):
        if self.kind == 'global':
            return True
        if not item or item.get('company') != self.company:
            return False
        kind = item.get('kind')
        if kind == 'job_posting' and self.kind == 'job':
            # A deduplicated posting can belong to multiple jobs. The mapping,
            # not research_docs.job_id or the historical job_evidence links,
            # identifies the current posting for this job.
            with repo.connection() as conn:
                if not conn.execute('SELECT 1 FROM research_job_documents WHERE job_id=? AND evidence_id=?',
                                    (self.job_id, item['id'])).fetchone():
                    return False
        with repo.connection() as conn:
            rules = {row[0] for row in conn.execute('SELECT rule FROM company_signals WHERE evidence_id=?', (item['id'],))}
        if rules & CROSS_COMPANY_RULES:
            return False
        if kind == 'local_computation':
            if self.kind == 'job':
                return False
            # Only structured collector provenance can admit a derived record;
            # a rule name in untrusted source text cannot grant access.
            return bool(rules) and rules <= COMPANY_LOCAL_RULES
        return True

    def require_evidence(self, repo, item):
        if not self.allows_evidence(repo, item):
            raise ScopeBoundaryError(self.boundary_message())

    def allows_report(self, repo, report):
        if self.kind == 'global':
            return True
        if report.get('company') != self.company:
            return False
        evidence_ids = set(report.get('evidence_ids', []))
        for line in report.get('dimensions', []):
            evidence_ids.update(line.get('evidence_ids', []))
            for finding in line.get('findings', []):
                if finding.get('rule') in CROSS_COMPANY_RULES:
                    return False
                if finding.get('evidence_id'):
                    evidence_ids.add(finding['evidence_id'])
        # Never display a precomputed score after dropping one of its inputs.
        return all(self.allows_evidence(repo, repo.get_evidence(eid)) for eid in evidence_ids)


def resolve_scope(repo, conversation_id, scope=None):
    stored = AgentScope.parse(repo.get_conversation(conversation_id)['scope']) if conversation_id else None
    resolved = AgentScope.parse(scope) if scope is not None else stored or AgentScope()
    if stored is not None and stored != resolved:
        raise ValueError('会话范围不匹配，请为当前范围新建对话')
    if resolved.kind == 'job':
        job = repo.job(resolved.job_id)
        if not job or job['company'] != resolved.company:
            raise ValueError('岗位不存在或会话公司范围不匹配')
    return resolved
