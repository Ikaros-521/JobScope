"""Typed read-only analytical tools. Aggregation never operates on retrieval top-k."""
import json
import re

from .index import scope_clause


def salary_query(index, plan, scope):
    if plan.clarification:
        raise ValueError(plan.clarification)
    index.sync()
    # Job facts use their own explicit scope. Never execute model-authored SQL.
    where, params = [], []
    if scope['kind'] != 'global':
        where.append('company=?'); params.append(scope['company'])
    if scope['kind'] == 'job':
        where.append('job_id=?'); params.append(scope['job_id'])
    if plan.companies:
        where.append('company IN (' + ','.join('?' for _ in plan.companies) + ')'); params.extend(plan.companies)
    if plan.location:
        where.append('instr(location,?)>0'); params.append(plan.location)
    if plan.skills:
        # Token boundaries preserve Java vs JavaScript, Go vs Google.
        clauses = ["skill_present(json_extract(payload,'$.search_text'),?)=1" for _ in plan.skills]
        where.append('(' + (' OR ' if plan.skills_operator == 'any' else ' AND ').join(clauses) + ')')
        params.extend(plan.skills)
    clause = ' AND '.join(where) or '1=1'
    predicate = ''
    values = list(params)
    if plan.threshold is not None:
        operator = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}[plan.operator]
        predicate = f' AND {plan.threshold_basis}{operator}?'
        values.append(plan.threshold)
    with index.repo.connection() as c:
        c.create_function('skill_present', 2, lambda text, skill: int(bool(re.search(r'(?<![a-z0-9])' + re.escape(skill) + r'(?![a-z0-9])', text or '', re.I))))
        total = c.execute('SELECT count(*) FROM research_jobs WHERE ' + clause, params).fetchone()[0]
        known = c.execute('SELECT count(*) FROM research_jobs WHERE ' + clause + ' AND monthly_min IS NOT NULL', params).fetchone()[0]
        counts = c.execute('''SELECT count(*) AS matched,min(monthly_min) AS lowest,max(monthly_max) AS highest,
            avg(monthly_min) AS mean_min,avg(monthly_max) AS mean_max FROM research_jobs WHERE ''' + clause + ' AND monthly_min IS NOT NULL' + predicate, values).fetchone()
        rows = c.execute('SELECT payload FROM research_jobs WHERE ' + clause + ' AND monthly_min IS NOT NULL' + predicate + ' ORDER BY monthly_min DESC,job_id LIMIT 8', values).fetchall()
        groups = c.execute('''SELECT company,count(*) AS jobs,min(monthly_min) AS min_quote,max(monthly_max) AS max_quote
            FROM research_jobs WHERE ''' + clause + ' AND monthly_min IS NOT NULL' + predicate + ' GROUP BY company ORDER BY jobs DESC,company LIMIT 20', values).fetchall()
    return dict(total_jobs=total, known_monthly_jobs=known, unknown_monthly_jobs=total - known,
                matched_jobs=counts['matched'], min_quote=counts['lowest'], max_quote=counts['highest'],
                mean_lower_bound=round(counts['mean_min'], 2) if counts['mean_min'] is not None else None,
                mean_upper_bound=round(counts['mean_max'], 2) if counts['mean_max'] is not None else None,
                companies=[dict(row) for row in groups], company_groups_truncated=len(groups) == 20,
                samples=[{key: value for key, value in json.loads(row[0]).items() if key != 'search_text'} for row in rows], sample_limit=8,
                threshold=plan.threshold, threshold_basis=plan.threshold_basis, operator=plan.operator,
                location=plan.location, skills=plan.skills, skills_operator=plan.skills_operator, currency='CNY', period='monthly', is_actual_salary=False,
                actual_salary_samples=0, aggregation='all_filtered_jobs',
                note='招聘税前月薪报价；不是实际发薪或到手工资。均值为岗位区间端点均值，不是员工工资均值。未知/非人民币月薪不参与金额统计，最高几薪不视为保证。')


def salary_text(metrics, plan):
    basis = '区间上限' if plan.threshold_basis == 'monthly_max' else '区间下限'
    operator = {'gt': '>', 'ge': '≥', 'lt': '<', 'le': '≤'}[plan.operator]
    heading = f"薪资查询：城市为{metrics['location'] or '不限地点'}；范围内{metrics['total_jobs']}个岗位，其中{metrics['known_monthly_jobs']}个有可解析的人民币月薪，{metrics['unknown_monthly_jobs']}个未知或口径不支持。"
    if plan.actual_salary:
        heading = '实际/到手工资未知：当前结构化数据没有经过独立核实的实际发薪样本，不能从招聘报价推断。\n\n' + heading
    if plan.threshold is not None:
        heading += f"\n按招聘月薪{basis} {operator} {plan.threshold:,.0f}元筛选，共{metrics['matched_jobs']}个岗位。"
    elif metrics['matched_jobs']:
        heading += f"\n已知招聘月薪报价总体跨度：{metrics['min_quote']:,.0f}–{metrics['max_quote']:,.0f}元；不同岗位和职级不可直接视为同一薪资分布。"
    return heading + '\n' + metrics['note']
