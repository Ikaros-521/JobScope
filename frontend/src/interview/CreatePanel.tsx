// 开场配置面板：场次画像/强度/题量/材料来源 + 错题强化选题。
import { Icon } from "../Icon";
import type {
  InterviewDrillRef,
  InterviewPersonaCard,
  InterviewStyle,
  InterviewWeakPoints,
} from "../api";
import { INTENSITY_NAMES } from "./shared";
import { DrillPicker, JobPicker } from "./pickers";
export function CreatePanel({
  cards,
  styles,
  card,
  onCard,
  style,
  onStyle,
  round,
  onRound,
  intensity,
  onIntensity,
  questionCount,
  onQuestionCount,
  mode,
  onMode,
  jobId,
  jobLabel,
  onPickJob,
  jobTitle,
  onJobTitle,
  companyName,
  onCompanyName,
  jdText,
  onJdText,
  modelConfigured,
  onConfigureModel,
  drillSelection,
  onDrillSelection,
  onDrillLoaded,
  onUseJob,
  busy,
  starting,
  onStart,
}: {
  cards: InterviewPersonaCard[];
  styles: InterviewStyle[];
  card: string;
  onCard: (value: string) => void;
  style: string;
  onStyle: (value: string) => void;
  round: string;
  onRound: (value: string) => void;
  intensity: "gentle" | "standard" | "stress";
  onIntensity: (value: "gentle" | "standard" | "stress") => void;
  questionCount: number;
  onQuestionCount: (value: number) => void;
  mode: "job" | "jd" | "free";
  onMode: (value: "job" | "jd" | "free") => void;
  jobId: string;
  jobLabel: string;
  onPickJob: (id: string, label: string) => void;
  jobTitle: string;
  onJobTitle: (value: string) => void;
  companyName: string;
  onCompanyName: (value: string) => void;
  jdText: string;
  onJdText: (value: string) => void;
  modelConfigured: boolean;
  onConfigureModel: () => void;
  // 错题强化（错题强化官卡片）：选中的错题引用、选题面板加载完的回调、一键带上错题来源岗位。
  drillSelection: InterviewDrillRef[];
  onDrillSelection: (next: InterviewDrillRef[]) => void;
  onDrillLoaded: (book: InterviewWeakPoints) => void;
  onUseJob: (id: string, label: string) => void;
  busy: boolean;
  starting: boolean;
  onStart: () => void;
}) {
  const active = cards.find((item) => item.id === card);
  const customNeedsTitle =
    card === "custom" && mode === "free" && !jobTitle.trim();
  // 错题强化：题量随选中的错题数（每道一道变式题），至少选一道才能开面。
  const drilling = card === "reinforce";
  const startDisabled =
    !card ||
    busy ||
    !modelConfigured ||
    customNeedsTitle ||
    (mode === "job" && !jobId) ||
    (drilling && !drillSelection.length);
  return (
    <section className="interview-create">
      <h2 className="sr-only">新建模拟面试</h2>
      {!modelConfigured && (
        <div className="iv-model-hint">
          模拟面试需要先绑定分析模型。
          <button className="inline-button" onClick={onConfigureModel}>
            去设置中心
          </button>
        </div>
      )}
      <div className="iv-cards">
        {cards.map((item) => (
          <button
            key={item.id}
            className={`iv-card${item.id === card ? " active" : ""}`}
            onClick={() => onCard(item.id)}
          >
            <strong>{item.name}</strong>
            <p>{item.blurb}</p>
            <span className="iv-card-tags">
              {item.tags.map((tag) => (
                <em key={tag}>{tag}</em>
              ))}
            </span>
          </button>
        ))}
      </div>
      {active && (
        <div className="iv-config">
          <label>
            公司风格
            <select
              value={style}
              onChange={(event) => onStyle(event.target.value)}
              aria-label="公司面试风格"
            >
              {styles.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            轮次
            <select
              value={round}
              onChange={(event) => onRound(event.target.value)}
              aria-label="面试轮次"
            >
              {active.rounds.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            强度
            <select
              value={intensity}
              onChange={(event) =>
                onIntensity(
                  event.target.value as "gentle" | "standard" | "stress",
                )
              }
              aria-label="面试强度"
            >
              {(["gentle", "standard", "stress"] as const).map((value) => (
                <option key={value} value={value}>
                  {INTENSITY_NAMES[value]}
                </option>
              ))}
            </select>
          </label>
          {drilling ? (
            <span className="tiny-label iv-drill-count" role="status">
              题量 = 已选 <b>{drillSelection.length}</b>{" "}
              道错题，每道出一道变式题
            </span>
          ) : (
            <label>
              题量
              <select
                value={questionCount}
                onChange={(event) =>
                  onQuestionCount(Number(event.target.value))
                }
                aria-label="题量"
              >
                {[6, 8, 10].map((value) => (
                  <option key={value} value={value}>
                    {value} 题
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}
      {drilling && (
        <DrillPicker
          selection={drillSelection}
          onSelection={onDrillSelection}
          onLoaded={onDrillLoaded}
          jobId={jobId}
          onUseJob={onUseJob}
        />
      )}
      <div className="iv-modes" role="tablist" aria-label="目标岗位来源">
        {(
          [
            ["job", "从岗位库选"],
            ["jd", "粘贴 JD"],
            ["free", "自由练"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            className={mode === value ? "active" : ""}
            onClick={() => onMode(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="iv-target">
        {mode === "job" && (
          <>
            <JobPicker value={jobId} label={jobLabel} onSelect={onPickJob} />
            {jobId && (
              <span className="tiny-label">
                将自动带上该岗位的完整 JD、评估缺口、简历存档与公司资料库情报
              </span>
            )}
          </>
        )}
        {mode === "jd" && (
          <>
            <textarea
              className="iv-jd"
              value={jdText}
              onChange={(event) => onJdText(event.target.value)}
              rows={4}
              maxLength={20000}
              placeholder="粘贴完整的职位描述（职责 + 任职要求），出题与追问都以它为准"
            />
            <div className="iv-target-row">
              <input
                className="iv-mini-input"
                value={jobTitle}
                onChange={(event) => onJobTitle(event.target.value)}
                maxLength={120}
                placeholder="岗位名称（可选，如：数据分析师）"
                aria-label="岗位名称"
              />
              <input
                className="iv-mini-input"
                value={companyName}
                onChange={(event) => onCompanyName(event.target.value)}
                maxLength={120}
                placeholder="公司名称（可选，用于带上公司资料）"
                aria-label="公司名称"
              />
            </div>
          </>
        )}
        {mode === "free" && (
          <div className="iv-target-row">
            {card === "custom" && (
              <input
                className="iv-mini-input"
                value={jobTitle}
                onChange={(event) => onJobTitle(event.target.value)}
                maxLength={120}
                placeholder="目标岗位名称（必填，如：数据分析师）"
                aria-label="目标岗位名称"
              />
            )}
            <input
              className="iv-mini-input"
              value={companyName}
              onChange={(event) => onCompanyName(event.target.value)}
              maxLength={120}
              placeholder="公司名称（可选，用于带上公司资料）"
              aria-label="公司名称"
            />
          </div>
        )}
      </div>
      <div className="iv-create-actions">
        <button
          className="button primary"
          disabled={startDisabled}
          onClick={onStart}
        >
          {starting ? (
            <span className="spinner" />
          ) : (
            <Icon name="mic" size={16} />
          )}
          开始出题
        </button>
        {customNeedsTitle ? (
          <span className="tiny-label iv-warn">
            先填目标岗位名称，面试官才知道面什么
          </span>
        ) : drilling ? (
          <span className="tiny-label">
            {drillSelection.length
              ? "出题约需 1-2 分钟：每道错题换问法出一道变式题，面试中不提示，复盘时逐题对照上次"
              : "先从下面的错题本勾选要重练的题"}
          </span>
        ) : (
          <span className="tiny-label">
            出题约需 1-3 分钟（岗位 JD + 评估缺口 + 简历 + 公司资料
            四源合一），完成后进入面试间
          </span>
        )}
      </div>
    </section>
  );
}
