"""漏斗转化与归因统计（2026-11-28 自 repository 迁出，审计 P1-5 职责过载）。

repository 是最底层持久层，此前内嵌 150 行纯分析；照 prescription.build(repo) 的
形态拆到这里：分析模块持 repo 自取数据，Repository 回归纯存取。入口保持
repo.pipeline_funnel()（薄委托），调用方与测试零改动——build 的口径 docstring
即原方法的口径说明，迁移未改动任何一行口径逻辑。
"""
import json
from datetime import datetime

from .repository import iso_week_start


def build(repo):
    """漏斗转化率、推进周期与归因切片。

    口径是「曾到达」：事件流（job_pipeline_events）∪ 当前状态——事件流上线前的老数据
    只有当前状态，归档且全程无事件的岗位历史未知，不计入转化分母（数量单列如实呈现，
    不冒充零转化）。归因三组：来源、是否定制简历（resume_versions.job_id 关联）、
    公司可靠度分带——分带组同时就是「评分 vs 实际结果」的校准报告：约面率明显偏离
    分数高低序时，规则该调了。求职线（2026-11-22 P1）：主线/转型线各自的 stages/steps
    单列（tracks），两类岗位的转化率不进同一个分母。"""
    with repo.connection() as conn:
        jobs = [json.loads(row['payload']) for row in
                conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.pipeline_status') IS NOT NULL")]
        events: dict[str, dict[str, str]] = {}
        for row in conn.execute('SELECT job_id, to_status, created_at FROM job_pipeline_events ORDER BY job_id, id'):
            per_job = events.setdefault(row['job_id'], {})
            # 同一段多次往返取最早到达时间：转化计数用集合，周期用首次时间戳。
            if row['to_status'] and row['to_status'] not in per_job:
                per_job[row['to_status']] = row['created_at']
        custom_jobs = {row['job_id'] for row in conn.execute("SELECT DISTINCT job_id FROM resume_versions WHERE job_id!=''")}
        reliability = repo._reliability_score_index(conn)

    # 曾到达集合：当前状态本身算到达（事件流上线前的老数据唯一线索），事件补历史。
    def stages_of(job):
        stages = set()
        current = str(job.get('pipeline_status') or '')
        if current:
            stages.add(current)
        stages.update(events.get(job.get('id'), {}).keys())
        return stages

    def first_ts(job, stage):
        return events.get(job.get('id'), {}).get(stage)

    def ever_and_steps(group):
        """一组岗位的曾到达计数与三步转化（主线/转型线分组复用同一口径）。"""
        ever = {stage: 0 for stage in repo.PIPELINE_STAGES}
        for job in group:
            for stage in stages_of(job) & set(repo.PIPELINE_STAGES):
                ever[stage] += 1
        steps = []
        for source, target in repo.PIPELINE_STEPS:
            rate = round(ever[target] / ever[source], 3) if ever[source] else None
            samples = []
            for job in group:
                start, end = first_ts(job, source), first_ts(job, target)
                if not start or not end:
                    continue
                try:
                    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
                except (TypeError, ValueError):
                    continue
                if seconds >= 0:
                    samples.append(seconds / 86400)
            p25, median, p75, count = repo._percentiles(samples)
            steps.append({'from': source, 'to': target, 'rate': rate,
                          'cycle_days': round(sum(samples) / count, 1) if count else None, 'cycle_n': count,
                          'median_days': median, 'p25_days': p25, 'p75_days': p75})
        return ever, steps

    ever, steps = ever_and_steps(jobs)
    archived_untracked = 0
    for job in jobs:
        if not events.get(job.get('id')) and job.get('pipeline_status') == 'archived':
            archived_untracked += 1

    # 求职线分组（2026-11-22 P1）：主线（现有经验保底）与转型线（新叙事）分开看转化，
    # 两类岗位混在一个池子里会让「最低转化环节」失真。只列有岗位的线；未设线的岗位只进总口径。
    tracks = []
    for track_key, label in (('main', '主线'), ('shift', '转型线')):
        group = [job for job in jobs if job.get('pipeline_track') == track_key]
        if not group:
            continue
        track_ever, track_steps = ever_and_steps(group)
        tracks.append(dict(track=track_key, label=label,
                           stages=[{'status': stage, 'count': track_ever[stage]} for stage in repo.PIPELINE_STAGES],
                           steps=track_steps))

    def bucket_slices(keyfn, labelfn):
        buckets: dict[str, dict] = {}
        for job in jobs:
            stages = stages_of(job)
            applied = bool(stages & {'applied', 'interviewing', 'offer'})
            interviewing = bool(stages & {'interviewing', 'offer'})
            offer = 'offer' in stages
            key = keyfn(job)
            bucket = buckets.setdefault(key, {'key': key, 'label': labelfn(key), 'applied': 0,
                                              'interviewing': 0, 'offer': 0})
            bucket['applied'] += applied
            bucket['interviewing'] += interviewing
            bucket['offer'] += offer
        result = []
        for bucket in buckets.values():
            bucket['interview_rate'] = round(bucket['interviewing'] / bucket['applied'], 3) if bucket['applied'] else None
            result.append(bucket)
        return sorted(result, key=lambda item: -item['applied'])

    def band_label(key):
        return f'可靠度{key}' if key != 'unassessed' else '未评估'

    def reliability_key(job):
        score = reliability.get(str(job.get('company') or '').strip())
        return repo._reliability_band(score) if score is not None else 'unassessed'

    attribution = {
        'by_source': bucket_slices(lambda job: str(job.get('source') or 'unknown'), lambda key: key),
        'by_resume': bucket_slices(lambda job: 'custom' if job.get('id') in custom_jobs else 'general',
                                   lambda key: '定制简历' if key == 'custom' else '通用简历'),
        'by_reliability': bucket_slices(reliability_key, band_label),
    }

    # 投递批次 cohort（P2）：每批 = 同一 ISO 周内「首次投递」的岗位集合，追踪这批累计
    # 走到 约面/Offer 的数量与仍在推进的存量。批次只列有投递事件的周，新→旧，最多 6 批。
    first_applied: dict[str, str] = {}
    for job_id, per_job in events.items():
        if per_job.get('applied'):
            first_applied[job_id] = per_job['applied']
    stage_sets = {job.get('id'): stages_of(job) for job in jobs}
    current_status = {job.get('id'): str(job.get('pipeline_status') or '') for job in jobs}
    cohort_groups: dict[str, list] = {}
    for job_id, stamp in first_applied.items():
        start = iso_week_start(stamp)
        if start:
            cohort_groups.setdefault(start.date().isoformat(), []).append(job_id)
    cohorts = []
    for week_start, ids in sorted(cohort_groups.items(), reverse=True)[:6]:
        sets = [stage_sets.get(job_id, set()) for job_id in ids]
        cohorts.append({'week_start': week_start, 'applied': len(ids),
                        'interviewing': sum(1 for stages in sets if stages & {'interviewing', 'offer'}),
                        'offer': sum(1 for stages in sets if 'offer' in stages),
                        'active': sum(1 for job_id in ids if current_status.get(job_id) in ('applied', 'interviewing'))})

    # 周几投递（P2）：按首次投递的星期几统计约面率。1 条样本就算，n 小由前端标注参考有限。
    weekday_stats = {day: {'weekday': day, 'applied': 0, 'interviewing': 0} for day in range(1, 8)}
    for job_id, stamp in first_applied.items():
        try:
            weekday = datetime.fromisoformat(stamp).isoweekday()
        except (TypeError, ValueError):
            continue
        weekday_stats[weekday]['applied'] += 1
        if stage_sets.get(job_id, set()) & {'interviewing', 'offer'}:
            weekday_stats[weekday]['interviewing'] += 1
    by_weekday = []
    for item in weekday_stats.values():
        item['rate'] = round(item['interviewing'] / item['applied'], 3) if item['applied'] else None
        by_weekday.append(item)

    return {'stages': [{'status': stage, 'count': ever[stage]} for stage in repo.PIPELINE_STAGES],
            'steps': steps, 'attribution': attribution, 'archived_untracked': archived_untracked,
            'tracks': tracks, 'cohorts': cohorts, 'by_weekday': by_weekday}
