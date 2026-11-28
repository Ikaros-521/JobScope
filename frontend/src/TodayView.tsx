import { useCallback, useEffect, useState } from "react";
import { createBackup, date, getDashboard, relativeDays } from "./api";
import type {
  Dashboard,
  DashboardBriefing,
  DashboardDue,
  PipelineFunnel,
  PipelineFunnelBucket,
  PipelinePrescription,
  PipelineWeeklyWeek,
  PrepBrief,
  TodayPlan,
} from "./api";
import { toast } from "./toast";
import { Icon } from "./Icon";
import { sourceNames } from "./JobCard";

// 今日概览（2026-11-18）：打开工作区的第一屏。研究工具的通病是「要用才打开」，
// 这一页把每天真正要做的事拉到眼前：到期/逾期的投递待办、超期未跟进、近 7 天新岗位、
// 采集近况与备份提醒。全部只读本机数据，不发起联网采集；具体操作仍落回对应工作区。

const PIPELINE_NAMES: Record<string, string> = {
  interested: "意向",
  applied: "已投递",
  interviewing: "面试中",
  offer: "Offer",
  archived: "归档",
};
const TASK_STATUS_NAMES: Record<string, string> = {
  running: "进行中",
  pending: "等待中",
  completed: "已完成",
  failed: "失败",
  needs_login: "需要登录",
  needs_verification: "需要验证",
  backoff: "退避等待",
};
const BACKUP_REMIND_DAYS = 7;
// 最低功耗阈值（2026-11-23）：静默 ≥4 天且有过管线历史 → 温和重入模式（跳过周末 2-3 天的正常间隔）。
const LOW_POWER_DAYS = 4;

export function TodayView({
  onOpenJob,
  onOpenFavorites,
  onOpenSources,
  onOpenJobs,
  onOpenSettings,
  onPrescriptionNavigate,
  onOpenPrepPlan,
}: {
  onOpenJob: (jobId: string) => void;
  onOpenFavorites: () => void;
  onOpenSources: () => void;
  onOpenJobs: () => void;
  onOpenSettings: () => void;
  onPrescriptionNavigate?: (target: PipelinePrescription["target"]) => void;
  onOpenPrepPlan: () => void;
}) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await getDashboard());
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function backupNow() {
    setBusy(true);
    void createBackup()
      .then((result) => {
        toast(`已备份：${result.backup.name}`);
        return load();
      })
      .catch((err: Error) => toast(err.message, "error"))
      .finally(() => setBusy(false));
  }

  if (error)
    return (
      <section className="today-view" aria-label="今日概览">
        <div className="paper-panel">
          <div className="empty">
            <h3>暂时读不到概览</h3>
            <p>{error}</p>
          </div>
        </div>
      </section>
    );
  if (!data)
    return (
      <section className="today-view" aria-label="今日概览">
        <div className="loading-state">
          <span className="spinner" />
          正在读取本机数据…
        </div>
      </section>
    );

  const {
    counts,
    pipeline,
    due,
    weekly,
    briefing,
    prescriptions,
    new_jobs: newJobs,
    backup,
    recent_tasks: recentTasks,
  } = data;
  const backupStale =
    backup.days_since === null || backup.days_since > BACKUP_REMIND_DAYS;
  const staleFollowUps = pipeline.follow_ups;
  // 静默天数只认用户动作（推进∪收藏）；空库无历史不进此模式，走入门引导。
  const quietDays = briefing?.days_since_last_move ?? 0;
  const lowPower =
    quietDays >= LOW_POWER_DAYS && Object.keys(pipeline.totals).length > 0;

  return (
    <section className="today-view" aria-label="今日概览">
      {counts.jobs === 0 ? (
        <section className="paper-panel today-panel">
          <div className="empty">
            <div className="empty-icon">
              <Icon name="jobs" />
            </div>
            <h3>档案库还是空的</h3>
            <p>
              从采集或导入第一份岗位开始：采集的每份档案都保留原文、出处与时间。
            </p>
            <div className="today-empty-actions">
              <button className="button primary" onClick={onOpenJobs}>
                去岗位研究
                <Icon name="arrow" size={14} />
              </button>
              <button className="button secondary" onClick={onOpenSources}>
                数据与采集
                <Icon name="source" size={14} />
              </button>
            </div>
          </div>
        </section>
      ) : (
        <>
          {backupStale && (
            <div className="today-backup-banner" role="status">
              <Icon name="book" size={16} />
              <span>
                {backup.days_since === null
                  ? "还没有做过数据备份——档案与证据都在这一个库里，建议先备一份。"
                  : `已 ${Math.floor(backup.days_since)} 天未备份（${date(backup.last_backup_at)}）。`}
              </span>
              <button
                className="button secondary small"
                disabled={busy}
                onClick={backupNow}
              >
                {busy ? "正在备份…" : "立即备份"}
              </button>
              <button className="text-button" onClick={onOpenSettings}>
                管理备份
              </button>
            </div>
          )}

          {/* 晨间简报（2026-11-23）：打开第一眼先看「自昨天以来发生了什么」，再决定今天做什么。 */}
          {briefing && (
            <BriefingStrip briefing={briefing} onOpenSources={onOpenSources} />
          )}

          <div className="today-grid">
            <section
              className="paper-panel today-panel today-todo"
              aria-label="待跟进"
            >
              <div className="section-heading">
                <div>
                  <h2>今日待跟进</h2>
                  <p className="body-small muted">
                    {lowPower
                      ? `有 ${due.length + staleFollowUps.length} 条在等——不急，节奏回来了再处理。`
                      : `截止日到期的管线岗位优先；下面是超过 ${pipeline.stale_days} 天没有状态变化的提醒。`}
                  </p>
                </div>
              </div>
              {lowPower ? (
                <button
                  className="text-button today-panel-link"
                  onClick={onOpenFavorites}
                >
                  去收藏夹看一眼
                  <Icon name="arrow" size={13} />
                </button>
              ) : due.length === 0 && staleFollowUps.length === 0 ? (
                <div className="inline-empty">
                  没有到期待办。在收藏夹给岗位设「截止日」，或推进状态后这里会提醒你。
                </div>
              ) : (
                <ul className="today-due-list">
                  {due.map((item) => (
                    <DueRow key={item.id} item={item} onOpen={onOpenJob} />
                  ))}
                  {staleFollowUps
                    .filter((item) => !due.some((d) => d.id === item.id))
                    .map((item) => (
                      <li key={item.id} className="today-due-item stale">
                        <button
                          type="button"
                          className="text-button today-due-title"
                          onClick={() => onOpenJob(item.id)}
                        >
                          {item.title} · {item.company}
                        </button>
                        <span className="tiny-label">
                          「{PIPELINE_NAMES[item.status] || item.status}」已{" "}
                          {relativeDays(item.changed_at, "多日")}
                          未变化，看一眼进展
                        </span>
                      </li>
                    ))}
                </ul>
              )}
              <button
                className="text-button today-panel-link"
                onClick={onOpenFavorites}
              >
                去收藏夹推进管线
                <Icon name="arrow" size={13} />
              </button>
            </section>

            <section className="paper-panel today-panel" aria-label="数据一览">
              <div className="section-heading">
                <div>
                  <h2>数据一览</h2>
                </div>
              </div>
              <div className="today-stats">
                <button
                  type="button"
                  className="today-stat"
                  onClick={onOpenJobs}
                >
                  <b>{counts.jobs.toLocaleString()}</b>
                  <span>岗位档案</span>
                </button>
                <button
                  type="button"
                  className="today-stat"
                  onClick={onOpenFavorites}
                >
                  <b>{counts.favorites.toLocaleString()}</b>
                  <span>收藏</span>
                </button>
                <div className="today-stat">
                  <b>{counts.tracked.toLocaleString()}</b>
                  <span>管线中</span>
                </div>
                <div className="today-stat">
                  <b>{counts.companies.toLocaleString()}</b>
                  <span>公司主体</span>
                </div>
                <div className="today-stat">
                  <b>{counts.evidence.toLocaleString()}</b>
                  <span>证据资料</span>
                </div>
              </div>
              <div className="section-heading today-second-heading">
                <div>
                  <h2>本周节奏</h2>
                  <p className="body-small muted">
                    按周聚合管线推进，环比上周——节奏比单日爆发更可靠。
                  </p>
                </div>
              </div>
              {weekly && (
                <WeeklyRhythm
                  weeks={weekly.weeks}
                  onOpenFavorites={onOpenFavorites}
                  lowPower={lowPower}
                />
              )}
              <div className="section-heading today-second-heading">
                <div>
                  <h2>开战战绩</h2>
                  <p className="body-small muted">
                    累计曾到达口径，约面含拿 Offer 的岗位。
                  </p>
                </div>
              </div>
              {pipeline.funnel && <RecordBoard funnel={pipeline.funnel} />}
              <div className="section-heading today-second-heading">
                <div>
                  <h2>近 7 天新入库</h2>
                </div>
              </div>
              {newJobs.length === 0 ? (
                <div className="inline-empty">近 7 天没有新岗位入库。</div>
              ) : (
                <ul className="today-new-list">
                  {newJobs.map((item) => (
                    <li key={item.id}>
                      <button
                        type="button"
                        className="text-button today-due-title"
                        onClick={() => onOpenJob(item.id)}
                      >
                        {item.title} · {item.company}
                      </button>
                      <span className="tiny-label">
                        {sourceNames[item.source] || item.source} ·{" "}
                        {relativeDays(item.first_seen_at, "本周")}入库
                        {item.starred ? " · 已收藏" : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          {/* 备战与行动（2026-11-28 重排）：并排收窄放次屏，待跟进/数据一览前置。 */}
          <div className="today-duo">
            {/* 面试备战（2026-11-24）：今日页只展示今天的必做进度与连续天数，完整计划在工作区。 */}
            <PrepCard brief={data.prep_plan} onOpen={onOpenPrepPlan} />

            {/* 最低功耗模式（2026-11-23）：静默 ≥4 天后的温和重入——替下目标行与处方卡，不弹欠账。 */}
            {lowPower ? (
              <ReentryCard days={quietDays} onOpenFavorites={onOpenFavorites} />
            ) : (
              prescriptions &&
              (prescriptions.prescriptions.length > 0 ||
                prescriptions.today_plan) && (
                <ActionBoard
                  plan={prescriptions.today_plan}
                  items={prescriptions.prescriptions.slice(0, 3)}
                  total={prescriptions.prescriptions.length}
                  onNavigate={onPrescriptionNavigate}
                />
              )
            )}
          </div>

          <section className="paper-panel today-panel" aria-label="最近采集">
            <div className="section-heading">
              <div>
                <h2>最近采集</h2>
              </div>
              <span className="purpose-heading-actions">
                <button className="text-button" onClick={onOpenSources}>
                  数据与采集
                  <Icon name="arrow" size={13} />
                </button>
              </span>
            </div>
            {recentTasks.length === 0 ? (
              <div className="inline-empty">还没有采集记录。</div>
            ) : (
              <ul className="today-task-list">
                {recentTasks.map((task) => (
                  <li key={task.id}>
                    <span className={`today-task-status ${task.status}`}>
                      {TASK_STATUS_NAMES[task.status] || task.status}
                    </span>
                    <span className="today-task-query">{task.query}</span>
                    <span className="tiny-label">
                      {task.count ? `${task.count} 条 · ` : ""}
                      {relativeDays(task.created_at, date(task.created_at))}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  );
}

function DueRow({
  item,
  onOpen,
}: {
  item: DashboardDue;
  onOpen: (id: string) => void;
}) {
  return (
    <li className={`today-due-item${item.overdue ? " overdue" : ""}`}>
      <button
        type="button"
        className="text-button today-due-title"
        onClick={() => onOpen(item.id)}
      >
        {item.title} · {item.company}
      </button>
      <span className="tiny-label">
        <b className={item.overdue ? "follow-due overdue" : "follow-due"}>
          截止 {item.due}
          {item.overdue ? "（逾期）" : ""}
        </b>
        {PIPELINE_NAMES[item.status] || item.status}
        {item.note ? ` · ${item.note}` : ""}
      </span>
    </li>
  );
}

// 面试备战（2026-11-24）：今日页只报「今天必做 X/Y」与连续天数，不摆整张计划欠账。
// 没有计划时给一句入口说明——计划是主动选择，不在后台偷偷生成。
function PrepCard({
  brief,
  onOpen,
}: {
  brief?: PrepBrief | null;
  onOpen: () => void;
}) {
  if (!brief)
    return (
      <section
        className="paper-panel today-panel today-prep"
        aria-label="面试备战"
      >
        <div className="section-heading">
          <div>
            <h2>面试备战</h2>
            <p className="body-small muted">
              机会来了接不住，是最可惜的失败。把面试暴露的短板变成每天一两件小事。
            </p>
          </div>
        </div>
        <button className="button secondary small" onClick={onOpen}>
          看看备战计划
          <Icon name="arrow" size={13} />
        </button>
      </section>
    );
  const next =
    brief.today.tasks.find((task) => task.status === "pending") ||
    brief.today.overdue[0];
  const allDone =
    brief.today.required_total > 0 &&
    brief.today.required_done >= brief.today.required_total;
  return (
    <section
      className="paper-panel today-panel today-prep"
      aria-label="面试备战"
    >
      <div className="section-heading">
        <div>
          <h2>今日备战</h2>
          <p className="body-small muted">
            每天一场模拟面试，短板会自己浮出来——今天做一件就算推进。
          </p>
        </div>
        <span
          className="pp-streak"
          title={`历史最长连续 ${brief.progress.best_streak} 天`}
        >
          连续 {brief.progress.streak} 天
        </span>
      </div>
      <div className="pp-today-line">
        <span className={allDone ? "done" : ""}>
          必做 {brief.today.required_done}/{brief.today.required_total}
        </span>
        {next && <span className="pp-next">{next.title}</span>}
        {allDone && (
          <span className="tiny-label">今天的必做已完成，做得不错。</span>
        )}
      </div>
      <button className="text-button today-panel-link" onClick={onOpen}>
        打开备战计划
        <Icon name="arrow" size={13} />
      </button>
    </section>
  );
}

// 今日行动（2026-11-22）：行动处方引擎的今日页入口。目标数字与已投数并排，
// 处方卡只取前 3 条（全量在收藏页漏斗区）——今日页管「现在做什么」，不堆全部诊断。
function ActionBoard({
  plan,
  items,
  total,
  onNavigate,
}: {
  plan: TodayPlan;
  items: PipelinePrescription[];
  total: number;
  onNavigate?: (target: PipelinePrescription["target"]) => void;
}) {
  return (
    <section
      className="paper-panel today-panel today-action"
      aria-label="今日行动"
    >
      <div className="section-heading">
        <div>
          <h2>今日行动</h2>
          <p className="body-small muted">
            目标按你近一周的节奏定，处方由本地漏斗数据自动生成——指路不施压。
          </p>
        </div>
        {plan.follow_ups > 0 && (
          <span className="tiny-label">逾期跟进 {plan.follow_ups} 条</span>
        )}
      </div>
      <div className="today-action-row">
        <span className="today-action-target">
          <b className={plan.done.apply >= plan.targets.apply ? "done" : ""}>
            {plan.done.apply}
          </b>
          <span className="today-action-divider">/ {plan.targets.apply}</span>
          今日已投
        </span>
        <span className="tiny-label">{plan.targets.apply_basis}</span>
      </div>
      {items.length > 0 && (
        <ul className="prescription-list">
          {items.map((item) => (
            <li key={item.id} className={`prescription-item ${item.level}`}>
              <div className="prescription-head">
                <b>{item.title}</b>
                {onNavigate && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => onNavigate(item.target)}
                  >
                    {item.cta}
                    <Icon name="arrow" size={12} />
                  </button>
                )}
              </div>
              <p className="prescription-action">{item.action}</p>
            </li>
          ))}
        </ul>
      )}
      {total > items.length && (
        <span className="tiny-label">
          还有 {total - items.length} 条处方，在收藏页的漏斗区。
        </span>
      )}
    </section>
  );
}

// 最低功耗模式（2026-11-23）：静默 ≥4 天后的温和重入。替下「0/N 今日已投」的目标行与处方卡
// ——欠账清单和进度数字是回归的阻力，一件今天能完成的小事才是入口。中断很常见，回到节奏比补进度重要。
function ReentryCard({
  days,
  onOpenFavorites,
}: {
  days: number;
  onOpenFavorites: () => void;
}) {
  return (
    <section
      className="paper-panel today-panel today-action"
      aria-label="重新开始"
    >
      <div className="section-heading">
        <div>
          <h2>重新开始</h2>
          <p className="body-small muted">
            距上次推进已 {days} 天——求职中断很常见，今天从一件小事开始就行。
          </p>
        </div>
      </div>
      <ul className="prescription-list">
        <li className="prescription-item info">
          <div className="prescription-head">
            <b>今天只做一件：去收藏夹挑一家投出去</b>
            <button
              type="button"
              className="text-button"
              onClick={onOpenFavorites}
            >
              去收藏夹
              <Icon name="arrow" size={12} />
            </button>
          </div>
          <p className="prescription-action">
            不用清积压、不用补进度——投完这一家，今天就算赢。
          </p>
        </li>
      </ul>
    </section>
  );
}

// 晨间简报（2026-11-23）：近 24 小时增量单行条。数字来自 dashboard.briefing（全量计数不截断）；
// 全零的安静早晨整条隐藏，不占首屏；采集通道异常是唯一「要处理」信号，可跳数据与采集。
function BriefingStrip({
  briefing,
  onOpenSources,
}: {
  briefing: DashboardBriefing;
  onOpenSources: () => void;
}) {
  const parts: string[] = [];
  if (briefing.new_jobs) parts.push(`新入库 ${briefing.new_jobs}`);
  if (briefing.new_favorites) parts.push(`新收藏 ${briefing.new_favorites}`);
  const moveBits = ["interested", "applied", "interviewing", "offer"]
    .map((status) =>
      briefing.moves[status]
        ? `${PIPELINE_NAMES[status]} ${briefing.moves[status]}`
        : "",
    )
    .filter(Boolean);
  if (moveBits.length) parts.push(`推进 ${moveBits.join("、")}`);
  if (!parts.length && !briefing.collect_attention) return null;
  return (
    <div className="today-briefing" role="status">
      <Icon name="book" size={15} />
      <span>近 24 小时：{parts.length ? parts.join(" · ") : "没有新动静"}</span>
      {briefing.collect_attention > 0 && (
        <button type="button" className="text-button" onClick={onOpenSources}>
          采集通道 {briefing.collect_attention} 条要处理
          <Icon name="arrow" size={12} />
        </button>
      )}
    </div>
  );
}

// 开战战绩（2026-11-23）：累计战果从收藏页漏斗区上移到每天第一屏——认可度不在功能多少，
// 在回报看得见。全部复用 dashboard 既有载荷：简历自对照与评分校准即漏斗归因切片
// （约面 = 进面试 ∪ 拿 Offer），批次里程碑只看近 6 批窗口，不冒充全历史；样本不足如实标注。
function RecordBoard({ funnel }: { funnel: PipelineFunnel }) {
  const stages: Record<string, number> = {};
  for (const stage of funnel.stages) stages[stage.status] = stage.count;
  const applied = stages.applied || 0;
  if (applied === 0)
    return (
      <div className="inline-empty">
        还没有投递记录——推进第一个「已投递」后，这里开始累计战绩。
      </div>
    );
  const interviewed = (stages.interviewing || 0) + (stages.offer || 0);
  const perHundred = Math.round((interviewed / applied) * 100);
  const share = (bucket: PipelineFunnelBucket) =>
    `${bucket.label} ${bucket.interviewing}/${bucket.applied}` +
    (bucket.interview_rate === null
      ? ""
      : `（${Math.round(bucket.interview_rate * 100)}%）`);
  const resumeBuckets = funnel.attribution.by_resume.filter(
    (bucket) => bucket.applied > 0,
  );
  const bandBuckets = funnel.attribution.by_reliability.filter(
    (bucket) => bucket.applied > 0,
  );
  const cohorts = funnel.cohorts || [];
  const firstBatch = cohorts[cohorts.length - 1];
  const latestHitBatch = cohorts.find((cohort) => cohort.interviewing > 0);
  return (
    <div>
      <div className="today-weekly">
        <div className="today-weekly-stat">
          <b>{applied}</b>
          <span>投递</span>
        </div>
        <div className="today-weekly-stat">
          <b>{interviewed}</b>
          <span>约面</span>
        </div>
        <div className="today-weekly-stat">
          <b>{stages.offer || 0}</b>
          <span>Offer</span>
        </div>
        <div className="today-weekly-stat">
          <b>{perHundred}</b>
          <span>每百投约面</span>
        </div>
      </div>
      <div className="today-record-notes">
        {applied < 10 && (
          <span className="tiny-label">
            投递样本 {applied} 份，还不足 10，数字参考有限。
          </span>
        )}
        {resumeBuckets.length > 0 && (
          <span
            className="tiny-label"
            title="同一批岗位里，定制简历与通用简历的约面对比——自己跟自己比"
          >
            简历自对照：{resumeBuckets.map(share).join(" · ")}
            {resumeBuckets.length < 2 ? "（另一组还没有样本）" : ""}
          </span>
        )}
        {bandBuckets.length > 1 && (
          <span
            className="tiny-label"
            title="高分公司的约面率长期不高于低分时，评分规则该校准了"
          >
            评分校准：{bandBuckets.map(share).join(" · ")}
          </span>
        )}
        {firstBatch && (
          <span className="tiny-label" title="批次窗口只统计最近 6 批">
            首批投递 {firstBatch.week_start} 那周起步
            {latestHitBatch ? ` · 最近约面批 ${latestHitBatch.week_start}` : ""}
          </span>
        )}
      </div>
    </div>
  );
}

// 本周节奏（2026-11-22 P1）：末两周环比。只陈述数字与变化，不做好坏评价；
// 全 0 时是「这周还没动」的事实提醒，不是催办。
function WeeklyRhythm({
  weeks,
  onOpenFavorites,
  lowPower = false,
}: {
  weeks: PipelineWeeklyWeek[];
  onOpenFavorites: () => void;
  lowPower?: boolean;
}) {
  if (weeks.length === 0) return null;
  const [current, previous] = [
    weeks[weeks.length - 1],
    weeks[weeks.length - 2] || null,
  ];
  const metrics = [
    { key: "applied", label: "投递" },
    { key: "interviewing", label: "约面" },
    { key: "offer", label: "Offer" },
    { key: "new_favorites", label: "新收藏" },
  ] as const;
  return (
    <div className="today-weekly">
      {metrics.map((metric) => {
        const now = current[metric.key];
        const before = previous ? previous[metric.key] : null;
        const delta =
          before === null
            ? ""
            : now === before
              ? "持平"
              : now > before
                ? `+${now - before}`
                : `${now - before}`;
        return (
          <div
            key={metric.key}
            className="today-weekly-stat"
            title={before === null ? undefined : `上周 ${before}`}
          >
            <b>{now}</b>
            <span>{metric.label}</span>
            {delta && (
              <em
                className={
                  delta.startsWith("+") ? "up" : delta === "持平" ? "" : "down"
                }
              >
                {delta}
              </em>
            )}
          </div>
        );
      })}
      {current.applied === 0 && current.new_favorites === 0 ? (
        <span className="tiny-label">
          {lowPower
            ? "这周还没有推进。"
            : "这周还没有推进——去收藏夹挑两家投出去。"}
        </span>
      ) : (
        <button type="button" className="text-button" onClick={onOpenFavorites}>
          去推进
          <Icon name="arrow" size={12} />
        </button>
      )}
    </div>
  );
}
