# -*- coding: utf-8 -*-
"""JobScope 行为数据挖掘（双钻 Discover 取证）。

严格只读：以 sqlite3 URI mode=ro 打开生产库，绝不写入。
默认目标：.data/jobscope.sqlite3（可传 argv[1] 换任意库副本）。
输出：stdout 打印 markdown 报告，并落一份到 output/usage_report_<date>.md。

用法：
    ./.venv/Scripts/python.exe scripts/usage_mining.py [db路径]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "jobscope.sqlite3"


def ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def d(text: str | None) -> date | None:
    v = ts(text)
    return v.date() if v else None


class Mine:
    def __init__(self, db_path: Path):
        uri = f"file:{db_path.as_posix()}?mode=ro"
        self.con = sqlite3.connect(uri, uri=True)
        self.con.row_factory = sqlite3.Row
        self.tables = {
            r["name"]
            for r in self.con.execute(
                "select name from sqlite_master where type='table'"
            )
        }

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def has(self, table: str) -> bool:
        return table in self.tables

    def col(self, table: str, column: str, where: str = "1=1") -> list[str | None]:
        if not self.has(table):
            return []
        return [r[0] for r in self.rows(f"select {column} from {table} where {where}")]


def fmt_day(v: date | None) -> str:
    return v.isoformat() if v else "-"


def span_days(first: date | None, last: date | None) -> str:
    if not first or not last:
        return "-"
    return str((last - first).days + 1)


# ---------------------------------------------------------------- 模块事件流
def module_streams(m: Mine) -> dict[str, list[datetime]]:
    """每个功能模块一条时间流（事件粒度可粗可细，够画节奏即可）。"""
    streams: dict[str, list[datetime]] = {}

    def add(name: str, values: list[str | None]):
        streams[name] = sorted(v for v in (ts(x) for x in values) if v)

    add("岗位采集 jobs", m.col("jobs", "updated_at"))
    add("公司信号 signals", m.col("company_signals", "created_at"))
    add("可信度评分 reliability", m.col("company_reliability", "created_at"))
    add("适配分析 reports", m.col("reports", "created_at"))
    add("行级改判 overrides", m.col("requirement_overrides", "updated_at"))
    add("对话 conversations", m.col("conversations", "created_at"))
    add("简历版本 resume_versions", m.col("resume_versions", "created_at"))
    add("资料库 profile", m.col("profile_entries", "created_at"))
    add("模拟面试 sessions", m.col("interview_sessions", "updated_at"))
    add("面试复盘 transcripts", m.col("interview_transcripts", "updated_at"))
    add("触达话术 outreach", m.col("outreach_versions", "created_at"))
    add("空窗叙事 gap", m.col("gap_narratives", "created_at"))
    add("调研 runs", m.col("research_runs", "created_at"))
    if m.has("jd_assessments"):
        # 两种考卷同表：payload.kind = bws（JD 取舍卷）/ style（工作风格卷）；改版前的记录没有 kind，按 bws 算。
        kind = "coalesce(json_extract(payload,'$.kind'),'bws')"
        add("反向测评 jd", m.col("jd_assessments", "created_at", f"{kind}='bws'"))
        add("工作风格测评 style", m.col("jd_assessments", "created_at", f"{kind}='style'"))
    return streams


def section_touch(m: Mine, streams: dict[str, list[datetime]]) -> list[str]:
    lines = ["## A. 功能触达率", "", "| 模块 | 总量 | 首次 | 最近 | 活跃天数 | 距今(天) |", "|---|---|---|---|---|---|"]
    today = date.today()
    for name, evs in streams.items():
        if not evs:
            lines.append(f"| {name} | 0 | - | - | 0 | - |")
            continue
        days = {e.date() for e in evs}
        stale = (today - evs[-1].date()).days
        lines.append(
            f"| {name} | {len(evs)} | {fmt_day(evs[0].date())} | {fmt_day(evs[-1].date())} "
            f"| {len(days)} | {stale} |"
        )
    return lines


def section_weekly(m: Mine, streams: dict[str, list[datetime]]) -> list[str]:
    all_days = sorted({e.date() for evs in streams.values() for e in evs})
    if not all_days:
        return ["## B. 周节奏", "", "（无数据）"]
    week0 = all_days[0] - timedelta(days=all_days[0].weekday())
    weeks: list[date] = []
    w = week0
    while w <= all_days[-1]:
        weeks.append(w)
        w += timedelta(days=7)
    names = list(streams)
    per_week_module: dict[date, Counter[str]] = {wk: Counter() for wk in weeks}
    for name, evs in streams.items():
        for e in evs:
            wk = e.date() - timedelta(days=e.date().weekday())
            per_week_module[wk][name] += 1
    lines = [
        "## B. 周节奏（ISO 周一起始；空格=0）",
        "",
        "| 周↓ / 模块→ | " + " | ".join(n.split()[0] for n in names) + " | 合计 |",
        "|---" * (len(names) + 2) + "|",
    ]
    for wk in weeks:
        cells = [per_week_module[wk][n] or "" for n in names]
        total = sum(per_week_module[wk].values())
        mark = " **←断点**" if total == 0 else ""
        lines.append(f"| {wk.isoformat()} | " + " | ".join(str(c) for c in cells) + f" | {total}{mark} |")
    return lines


def section_funnel(m: Mine, streams: dict[str, list[datetime]] | None = None) -> list[str]:
    lines = ["## C. 环路完成率（采集→评估→投递→面试→终局）", ""]
    total_jobs = len(m.col("jobs", "updated_at")) if m.has("jobs") else 0
    reported_jobs = {r[0] for r in m.rows("select distinct job_id from reports")} if m.has("reports") else set()
    funnel_jobs: dict[str, set[str]] = {}
    if m.has("job_pipeline_events"):
        for r in m.rows("select job_id, to_status, created_at from job_pipeline_events order by created_at"):
            funnel_jobs.setdefault(r["to_status"], set()).add(r["job_id"])
    applied = funnel_jobs.get("applied", set())
    interviewing = set()
    for status in ("interviewing", "offer", "rejected", "ended"):
        interviewing |= funnel_jobs.get(status, set())
    lines += [
        f"- 采集岗位：**{total_jobs}**",
        f"- 已出适配报告：{len(reported_jobs)}（{len(reported_jobs) / total_jobs:.0%}）" if total_jobs else "- 已出适配报告：0",
        f"- 已投递：{len(applied)}（占采集 {len(applied) / total_jobs:.0%}）" if total_jobs else f"- 已投递：{len(applied)}",
        f"- 进入面试及以后：{len(interviewing)}",
    ]
    if m.has("job_pipeline_events"):
        lines += ["", "| 转移 | 次数 | 最近一次 |", "|---|---|---|"]
        agg: Counter[str] = Counter()
        last: dict[str, str] = {}
        for r in m.rows("select from_status, to_status, created_at from job_pipeline_events order by created_at"):
            key = f"{r['from_status'] or '∅'} → {r['to_status']}"
            agg[key] += 1
            last[key] = r["created_at"][:10]
        for key, n in sorted(agg.items()):
            lines.append(f"| {key} | {n} | {last[key]} |")
    return lines


def section_first_value(m: Mine, streams: dict[str, list[datetime]]) -> list[str]:
    lines = ["## D. 首次价值间隔（H1：第一口甜头要多久）", ""]
    keys = ["岗位采集 jobs", "适配分析 reports", "简历版本 resume_versions", "对话 conversations"]
    anchors = {k: streams.get(k, [None])[0] if streams.get(k) else None for k in keys}
    base = anchors["岗位采集 jobs"]
    for k in keys:
        v = anchors[k]
        if not v:
            lines.append(f"- {k.split()[0]}：未使用")
            continue
        gap = f"（采集后 +{(v - base).days if base and v >= base else '?'} 天）" if base and k != "岗位采集 jobs" else ""
        lines.append(f"- {k.split()[0]} 首次：{fmt_day(v.date())}{gap}")
    return lines


def section_trust(m: Mine, streams: dict[str, list[datetime]] | None = None) -> list[str]:
    lines = ["## E. 信任信号（H4：改判/覆盖情况）", ""]
    if not m.has("requirement_overrides"):
        return lines + ["- 无 overrides 表"]
    ov = m.rows("select status, count(*) n from requirement_overrides group by status")
    jobs_with = {r[0] for r in m.rows("select distinct job_id from requirement_overrides")}
    rep_jobs = {r[0] for r in m.rows("select distinct job_id from reports")} if m.has("reports") else set()
    lines.append(f"- 改判条目：{sum(r['n'] for r in ov)}，状态分布 {[dict((r['status'], r['n']) for r in ov)]}")
    if rep_jobs:
        lines.append(f"- 有报告的岗位中动手改判的比例：{len(jobs_with & rep_jobs)}/{len(rep_jobs)}")
    return lines


def section_misc(m: Mine, streams: dict[str, list[datetime]] | None = None) -> list[str]:
    lines = ["## F. 其他信号", ""]
    if m.has("tasks"):
        statuses = Counter()
        sources = Counter()
        created: list[datetime] = []
        for raw in m.col("tasks", "payload"):
            try:
                p = json.loads(raw)
            except (TypeError, ValueError):
                continue
            statuses[p.get("status", "?")] += 1
            sources[p.get("source", "?")] += 1
            v = ts(p.get("created_at"))
            if v:
                created.append(v)
        done = statuses.get("completed", 0) + statuses.get("partial", 0)
        total = sum(statuses.values())
        lines.append(
            f"- 采集任务 tasks：共 {total} 条（后台运行日志，非用户待办），完成 {statuses.get('completed', 0)}、"
            f"部分 {statuses.get('partial', 0)}、失败 {statuses.get('failed', 0)}、需登录/验证 "
            f"{statuses.get('needs_login', 0) + statuses.get('needs_verification', 0)}；来源 {len(sources)} 类"
        )
        if created:
            lines.append(f"  - 时间跨度 {fmt_day(min(created).date())} ~ {fmt_day(max(created).date())}")
    if m.has("conversations"):
        n_msg = 0
        for raw in m.col("conversations", "messages"):
            try:
                n_msg += len(json.loads(raw))
            except (TypeError, ValueError):
                pass
        lines.append(f"- 对话消息总量（估算）：{n_msg}")
    if m.has("outreach_versions"):
        used = m.rows("select count(*) n from outreach_versions where used_at != ''")[0]["n"]
        total = m.rows("select count(*) n from outreach_versions")[0]["n"]
        lines.append(f"- 触达话术：生成 {total}，标记已使用 {used}")
    if m.has("runway_profile"):
        # 表在但一行没有（从没存过跑道档案）时不能直接取 [0]，否则整份报告在这里崩掉。
        runway = m.rows("select updated_at from runway_profile")
        if runway:
            lines.append(f"- 跑道档案最近更新：{runway[0]['updated_at'][:10]}")
        else:
            lines.append("- 跑道档案：尚未填写")
    return lines


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        raise SystemExit(1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    m = Mine(db_path)
    streams = module_streams(m)
    out: list[str] = [
        f"# JobScope 行为挖掘报告",
        "",
        f"- 数据源：`{db_path}`（只读）",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    for section in (
        section_touch,
        section_weekly,
        section_funnel,
        section_first_value,
        section_trust,
        section_misc,
    ):
        out += section(m, streams)
        out += ["", ""]
    text = "\n".join(out).rstrip() + "\n"
    print(text)
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"usage_report_{date.today().isoformat()}.md"
    target.write_text(text, encoding="utf-8")
    print(f"已写入 {target}", file=sys.stderr)


if __name__ == "__main__":
    main()
