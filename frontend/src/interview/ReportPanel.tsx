// 模拟面试复盘报告（总评/维度/逐题点评 + 归因快捷清单）。
import { Icon } from "../Icon";
import { Markdown } from "../chat/Markdown";
import type { InterviewReport, InterviewSession } from "../api";
// 终版复盘报告。错题强化场次多两样：总评下的计数对照条、每题的「上次失分」行；
// 「重练此题 / 强化重练本场错题」把答得不好的题带去再开一场强化面。

// 逐题复盘底部的「重练此题」：把这题带去「错题强化官」（没被点失分点的题也能练）。

// 错题强化的纯计数对照条：只摆数字不下结论——点评模型几乎每题都会点出可改进处，「补齐」不等于「过关」。
export function DrillStatsLine({
  stats,
}: {
  stats: NonNullable<InterviewReport["drill_stats"]>;
}) {
  return (
    <p className="iv-drill-stats" role="status">
      <Icon name="target" size={14} />
      <span>
        错题强化对照：重练 {stats.total} 道错题，<b>{stats.cleared}</b>{" "}
        道本次没再被点出缺失要素；缺失要素合计 上次 {stats.before} 条 → 本次{" "}
        {stats.after} 条。
      </span>
    </p>
  );
}
export function ReviewDrillAction({ onClick }: { onClick: () => void }) {
  return (
    <div className="iv-review-foot">
      <button
        className="text-button"
        onClick={onClick}
        title="把这题带去「错题强化官」：换问法重考一遍，面试官盯住上次的失分点追问，复盘对照这次的表现"
      >
        <Icon name="mic" size={13} />
        重练此题
      </button>
    </div>
  );
}
export function ReportPanel({
  report,
  persona,
  onRegenerate,
  busy,
  onDrillQuestion,
  onDrillWeak,
}: {
  report: InterviewSession["report"];
  persona: InterviewSession["persona"];
  onRegenerate: () => void;
  busy: boolean;
  onDrillQuestion?: (index: number) => void;
  onDrillWeak?: () => void;
}) {
  if (!report)
    return (
      <div className="iv-state">
        复盘还没有生成。回面试间点「结束并生成复盘」。
      </div>
    );
  const weakCount = report.questions.filter(
    (item) => item.missing?.length,
  ).length;
  return (
    <div className="interview-report">
      <section className="paper-panel">
        <div className="section-heading">
          <h2>本场总评</h2>
          <span className="tiny-label">
            {persona.name} · {persona.round_label} · 复盘了{" "}
            {report.reviewed ?? report.questions.length} 题
          </span>
          <div className="iv-report-actions">
            {onDrillWeak && weakCount > 0 && (
              <button
                className="button primary small"
                onClick={onDrillWeak}
                title="把本场被点了缺失要素的题带去「错题强化官」：换问法重考一遍，复盘逐题对照这次"
              >
                <Icon name="mic" size={14} />
                强化重练本场错题（{weakCount}）
              </button>
            )}
            <button
              className="button secondary small"
              disabled={busy}
              onClick={onRegenerate}
              title="对同一批问答重新跑逐题点评与汇总，覆盖当前报告"
            >
              <Icon name="source" size={14} />
              重新生成
            </button>
          </div>
        </div>
        <p className="iv-summary">{report.summary}</p>
        {report.drill_stats && <DrillStatsLine stats={report.drill_stats} />}
        <div className="iv-dimensions">
          {report.dimensions.map((item) => (
            <div key={item.name} className="iv-dimension">
              <strong>{item.name}</strong>
              <span className="iv-score" data-score={item.score}>
                {"★".repeat(item.score)}
                {"☆".repeat(5 - item.score)}
              </span>
              <p>{item.comment}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="paper-panel">
        <h3>逐题复盘（好答案 vs 踩坑）</h3>
        <div className="iv-reviews">
          {report.questions.map((item, index) => (
            <details key={index} open={index === 0} className="iv-review">
              <summary>
                <span className="iv-topic">{item.topic}</span>
                {item.question}
              </summary>
              {item.previous_problems?.length ? (
                <p className="iv-previous">
                  上次失分：{item.previous_problems.join("、")}
                </p>
              ) : null}
              <p className="iv-critique">
                <Markdown content={item.critique} />
              </p>
              {item.missing.length > 0 && (
                <p className="iv-missing">
                  缺失要素：{item.missing.join("、")}
                </p>
              )}
              {item.model_answer && (
                <div className="iv-model-answer">
                  <strong>高分示范</strong>
                  <Markdown content={item.model_answer} />
                </div>
              )}
              {item.bad_answer_note && (
                <p className="iv-bad-answer">
                  <strong>踩坑提醒</strong>
                  {item.bad_answer_note}
                </p>
              )}
              {onDrillQuestion && (
                <ReviewDrillAction onClick={() => onDrillQuestion(index)} />
              )}
            </details>
          ))}
        </div>
      </section>
      <div className="analysis-columns">
        <AnalysisQuickList
          title="下次真面试前必须补（高危清单）"
          items={report.risk_list}
        />
        <AnalysisQuickList title="行动建议" items={report.actions} />
      </div>
    </div>
  );
}
export function AnalysisQuickList({
  title,
  items,
}: {
  title: string;
  items: string[];
}) {
  return (
    <section className="analysis-list paper-panel">
      <h4>{title}</h4>
      {items?.length ? (
        <ul>
          {items.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">本轮没有特别突出的项。</p>
      )}
    </section>
  );
}
