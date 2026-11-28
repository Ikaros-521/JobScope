// 模拟面试与真实复盘共用的常量、类型与导出助手。
// （2026-11-28 自 InterviewView.tsx 拆出——单文件 3482 行拆为组件目录。）
import { date, getAttribution, getFailureMuseum } from "../api";
import type {
  AttributionRecord,
  InterviewDrillRef,
  InterviewMessage,
  InterviewSession,
  InterviewWeakPoint,
} from "../api";
// 归因标注导出的取数：标签显示名从分类学解析（失败退回 tag id），未标注过静默缺省。

// 渐进式复盘 feed：start 铺题卡骨架，review 原位填充，done 换终版报告（同模拟面试复盘 feed）。

// ---- 真实复盘工作区：导入面板（未选中时）｜逐字稿工作台「说话人核对 | 复盘报告」 ----
// 与模拟面试并列的第二条链路（2026-11-21）：外部 ASR 文本进来 → 模型初标说话人
// → 用户逐条核对修正（PATCH 全量按序替换）→ 确认后生成结合 JD 的复盘报告。
// 流式与编辑态全部自管，切换选中逐字稿即整体重置。

// 标准格式示例：逐字稿自带「说话人：原话」前缀时导入端直读切分、跳过模型标注（降本增效）。
// 「说话人1/2」类无语义前缀认不出谁是候选人，仍走模型标注——示例刻意用候选人/面试官称呼。

// ---- 错题本选题面板（错题强化官专用，2026-11-24）----
// 既往复盘（模拟 + 真实）里被点了失分点的题，按来源场次分组。列表由后端规则统计秒出；
// 勾选状态在外层（建场入参就是这串引用），这里只负责展示与切换。
// 说话人固定选项（与后端归一化取值一致）：me=候选人，interviewer_N=第 N 位面试官。
// 一场最多重练的错题数（与后端 InterviewCreateInput.drill 上限一致）。

// 渐进式复盘 feed：start 事件铺全部题卡骨架，review 事件按题号原位填充内容，done 换终版报告。
// 复盘等后台 run 的实时进度（SSE progress 事件）：stage=review 逐题点评 done/total，summary=汇总中。
export type StreamState = { runId: string; answer: string } | null;
export type RunProgress = {
  stage: "review" | "summary";
  done: number;
  total: number;
  topic?: string;
} | null;

export type ReportFeedItem = {
  index: number;
  topic: string;
  question: string;
  previous_problems?: string[];
  review?: {
    critique: string;
    missing: string[];
    model_answer: string;
    bad_answer_note: string;
  };
};
export type ReportFeed = {
  items: ReportFeedItem[];
  done: number;
  total: number;
  stage: "review" | "summary";
} | null;
export const STATUS_NAMES: Record<string, string> = {
  planning: "出题中",
  ready: "待开始",
  live: "面试中",
  reporting: "复盘生成中",
  done: "已复盘",
  failed: "出题失败",
};
export const INTENSITY_NAMES: Record<string, string> = {
  gentle: "温和",
  standard: "标准",
  stress: "压力面",
};
export const SOURCE_NAMES: Record<string, string> = {
  jd: "JD",
  gap: "评估缺口",
  resume: "简历",
  general: "通用",
  weakness: "专项强化",
};
export const MAX_DRILLS = 12;
export const TRANSCRIPT_STATUS_NAMES: Record<string, string> = {
  imported: "待标注",
  labeling: "标注中",
  labeled: "待核对",
  reporting: "报告生成中",
  done: "已复盘",
  failed: "标注失败",
};
export const SPEAKER_OPTIONS = [
  "me",
  "interviewer_1",
  "interviewer_2",
  "interviewer_3",
  "interviewer_4",
  "interviewer_5",
];
export function speakerLabel(speaker: string) {
  return speaker === "me"
    ? "候选人"
    : `面试官${speaker.replace("interviewer_", "") || "?"}`;
}
export const DRILL_SOURCE_NAMES: Record<string, string> = {
  mock: "模拟面试",
  real: "真实复盘",
};
export const refKey = (ref: InterviewDrillRef) =>
  `${ref.source}|${ref.session_id}|${ref.index}`;
export type DrillGroup = {
  key: string;
  source: string;
  label: string;
  at: string;
  items: InterviewWeakPoint[];
};
export function groupWeakPoints(items: InterviewWeakPoint[]): DrillGroup[] {
  // items 已按最近出现排好序：首次出现的顺序 = 最近场次在前
  const groups = new Map<string, DrillGroup>();
  for (const item of items) {
    const key = `${item.source}|${item.session_id}`;
    if (!groups.has(key))
      groups.set(key, {
        key,
        source: item.source,
        at: item.last_at,
        items: [],
        label: item.label || `${DRILL_SOURCE_NAMES[item.source]}（未挂岗位）`,
      });
    groups.get(key)!.items.push(item);
  }
  return [...groups.values()];
}
export const FORMAT_EXAMPLE = `面试官：你好，请先简单介绍一下自己。
候选人：好的，我做后端开发五年，主要技术栈是 Python 和 Go。
面试官1：讲讲你最有代表性的项目，你负责哪部分？
候选人：会员增长项目里我负责链路设计，把下单转化率提升了 12%。
面试官1：这个 12% 的口径是怎么统计的？
候选人：A/B 实验，实验组和对照组各跑 30 天得出的。`;
export type TranscriptFeedItem = {
  index: number;
  question: string;
  review?: {
    critique: string;
    good: string[];
    bad: string[];
    suggestion: string;
  };
};
export type TranscriptReportFeed = {
  items: TranscriptFeedItem[];
  done: number;
  total: number;
  stage: "review" | "summary";
} | null;
export function interviewFileName(session: InterviewSession): string {
  const base =
    [session.company, session.job_title].filter(Boolean).join("-") ||
    session.title;
  const d = new Date(session.created_at);
  const stamp = Number.isNaN(d.getTime())
    ? session.id
    : `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}-${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}`;
  return `面试记录-${base}-${stamp}.md`.replace(/[\\/:*?"<>|]/g, "_");
}
export type AttributionBundle = {
  attribution: AttributionRecord | null;
  labels: Map<string, string>;
};
export const ATTRIBUTION_OUTCOME_NAMES: Record<string, string> = {
  self_positive: "自评满意",
  self_neutral: "自评一般",
  self_negative: "自评不满意",
  pass: "通过",
  fail: "未通过",
  unknown: "结果未知",
};
export async function loadAttributionBundle(
  sessionId: string,
): Promise<AttributionBundle> {
  const labels = new Map<string, string>();
  try {
    const museum = await getFailureMuseum();
    for (const def of [...museum.controlled, ...museum.uncontrolled])
      labels.set(def.id, def.label);
  } catch {
    /* 报告照常导出，标签退回 id */
  }
  try {
    return { attribution: await getAttribution(sessionId), labels };
  } catch {
    return { attribution: null, labels };
  }
}
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function buildInterviewMarkdown(
  session: InterviewSession,
  messages: InterviewMessage[],
  attributionBundle: AttributionBundle,
): string {
  const persona = session.persona;
  const plan = session.question_plan;
  const report = session.report;
  const lines: string[] = [];
  const target =
    [session.company, session.job_title].filter(Boolean).join(" · ") ||
    session.title;
  lines.push(`# 模拟面试记录：${target}`, "");
  const meta = [
    `面试官：${persona.name}（${persona.round_label} · ${persona.intensity_label}${persona.company ? ` · ${persona.company}` : ""}）`,
    `开场时间：${date(session.created_at)}`,
    plan ? `题单 ${plan.questions.length} 题` : "",
  ].filter(Boolean);
  lines.push(meta.join(" ｜ "), "");
  if (plan) {
    lines.push("## 题单", "");
    plan.questions.forEach((item, index) => {
      lines.push(
        `${index + 1}. 【${item.topic}】${item.question}${item.focus ? `（考察：${item.focus}）` : ""}`,
      );
    });
    lines.push("");
  }
  if (messages.length) {
    lines.push("## 对话实录", "");
    for (const item of messages) {
      lines.push(
        `**${item.role === "assistant" ? "面试官" : "候选人"}：** ${item.content}`,
        "",
      );
    }
  }
  if (report) {
    lines.push("## 复盘报告", "", "### 总评", "", report.summary, "");
    if (report.drill_stats) {
      lines.push(
        `错题强化对照：重练 ${report.drill_stats.total} 道错题，${report.drill_stats.cleared} 道本次没再被点出缺失要素；缺失要素合计 上次 ${report.drill_stats.before} 条 → 本次 ${report.drill_stats.after} 条。`,
        "",
      );
    }
    if (report.dimensions.length) {
      lines.push("### 维度评分", "");
      for (const item of report.dimensions) {
        lines.push(
          `- ${item.name}：${"★".repeat(item.score)}${"☆".repeat(5 - item.score)} —— ${item.comment}`,
        );
      }
      lines.push("");
    }
    if (report.questions.length) {
      lines.push("### 逐题点评", "");
      report.questions.forEach((item, index) => {
        lines.push(
          `#### ${index + 1}. 【${item.topic}】${item.question}`,
          "",
          `**点评：** ${item.critique}`,
        );
        if (item.previous_problems?.length)
          lines.push(`**上次失分要素：** ${item.previous_problems.join("、")}`);
        if (item.missing.length)
          lines.push(`**缺失要素：** ${item.missing.join("、")}`);
        if (item.model_answer)
          lines.push(`**高分示范：** ${item.model_answer}`);
        if (item.bad_answer_note)
          lines.push(`**踩坑提醒：** ${item.bad_answer_note}`);
        lines.push("");
      });
    }
    if (report.risk_list?.length)
      lines.push(
        "### 高危清单",
        "",
        ...report.risk_list.map((item) => `- ${item}`),
        "",
      );
    if (report.actions?.length)
      lines.push(
        "### 行动建议",
        "",
        ...report.actions.map((item) => `- ${item}`),
        "",
      );
  }
  if (attributionBundle.attribution) {
    const { attribution, labels } = attributionBundle;
    lines.push("## 归因标注（失败博物馆）", "");
    if (attribution.outcome)
      lines.push(
        `- 结果：${ATTRIBUTION_OUTCOME_NAMES[attribution.outcome] ?? attribution.outcome}`,
      );
    for (const [title, scope] of [
      ["可控因素", "controlled"],
      ["不可控因素", "uncontrolled"],
    ] as const) {
      const group = attribution.tags.filter((item) => item.scope === scope);
      if (!group.length) continue;
      lines.push(`### ${title}`, "");
      for (const item of group)
        lines.push(
          `- ${labels.get(item.tag) ?? item.tag}${item.note ? `：${item.note}` : ""}`,
        );
      lines.push("");
    }
    if (attribution.note) lines.push(`**整场备注：** ${attribution.note}`, "");
  }
  return lines.join("\n");
}
