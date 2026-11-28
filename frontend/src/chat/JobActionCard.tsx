import type { AgentJobAction } from "../api";

// Agent 内部动作卡（2026-11-23）：assess_job / optimize_resume。
// 卡片随消息落库、自包含岗位信息；点击直接导航到岗位研究/简历工坊，
// 真正的评估与优化在目标页由用户二次发起——卡片只是跳转授权点。
export function JobActionCard({
  action,
  disabled,
  onOpenJob,
  onWorkshop,
}: {
  action: AgentJobAction;
  disabled?: boolean;
  onOpenJob?: (jobId: string) => void;
  onWorkshop?: (prefill: {
    jobId: string;
    company: string;
    jobTitle: string;
    instructions?: string;
  }) => void;
}) {
  const isAssess = action.type === "assess_job";
  const blocked = disabled || (isAssess ? !onOpenJob : !onWorkshop);
  return (
    <section
      className="scope-expansion-card"
      aria-label={isAssess ? "适配评估卡片" : "简历优化卡片"}
    >
      <div className="scope-expansion-heading">
        <span aria-hidden="true">{isAssess ? "◎" : "✎"}</span>
        <strong>{isAssess ? "适配评估卡片" : "简历优化卡片"}</strong>
      </div>
      <span className="scope-expansion-target">
        {action.company || "未标注公司"} · {action.title || action.job_id}
      </span>
      {(isAssess ? action.reason : action.instructions) && (
        <p>{isAssess ? action.reason : action.instructions}</p>
      )}
      <button
        type="button"
        className="button secondary small"
        disabled={blocked}
        onClick={() =>
          isAssess
            ? onOpenJob?.(action.job_id)
            : onWorkshop?.({
                jobId: action.job_id,
                company: action.company,
                jobTitle: action.title,
                instructions: action.instructions,
              })
        }
      >
        {isAssess
          ? "前往岗位研究 · 启动适配评估"
          : "前往简历工坊 · 按该岗位优化"}
      </button>
      <small>
        {isAssess
          ? "评估在岗位研究页执行（消耗模型调用），本对话内未运行"
          : "优化产出需在工坊逐条审阅确认后才保存，不会直接改写简历"}
      </small>
    </section>
  );
}
