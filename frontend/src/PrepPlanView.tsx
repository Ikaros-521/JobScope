import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  archivePrepPlan,
  donePrepTask,
  getPrepAdjustConversation,
  getPrepPlan,
  listInterviewWeakPoints,
  openPrepAdjustStream,
  openPrepPlanStream,
  reopenPrepTask,
  skipPrepTask,
  startPrepAdjust,
  startPrepPlanRun,
} from "./api";
import type {
  InterviewDrillRef,
  Job,
  PageInfo,
  PrepAdjustApplied,
  PrepAdjustTurn,
  PrepFocus,
  PrepSnapshot,
  PrepTask,
} from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import { useRunStream } from "./useRunStream";
import { Icon } from "./Icon";
import { toast } from "./toast";

// 面试备战计划（2026-11-24）：模型分析短板与练法，规则负责排期与打卡闭环。
// 首屏只有「今天做什么」；诊断、周视图与短板清单折叠/后置，避免变成一张大欠账表。
// 完成/跳过/撤销无破坏性；只有「重新生成 / 归档」会覆盖当前计划，走 ConfirmDialog。
// 生成走后台 run + SSE：阶段清单逐项点亮、诊断逐字流式、短板逐条浮现（同评估适配管线）。
// 生成后可继续跟教练对话调整（2026-11-24 第四轮）：回复逐字流、修改逐条亮出；
// 「整轮重排」是覆盖类操作，模型只提请、用户在确认框点头后才走强制重新生成。

const CREATED_FROM_LABELS: Record<string, string> = {
  interview_report: "依据最近模拟面试报告中暴露的短板",
  failure_museum: "优先依据失败博物馆里重复出现的可控失分点",
  jd_gaps: "依据岗位评估里标为未知的要求（未知不等于不会）",
  generic: "本机还没有面试数据，先给一份通用入门计划",
};
const ORIGIN_LABELS: Record<string, string> = {
  model: "AI 定制",
  rules: "规则模板",
};
const KIND_LABELS: Record<string, string> = {
  mock: "模拟面试",
  study: "补充学习",
  drill: "表达训练",
  review: "每周回看",
};

type PrepForm = {
  goal: string;
  job_id: string;
  minutes_per_day: number;
  use_llm: boolean;
};
const DEFAULT_FORM: PrepForm = {
  goal: "",
  job_id: "",
  minutes_per_day: 20,
  use_llm: true,
};

// ---- 生成进度（SSE 事件驱动的纯函数状态机；done/error 在订阅回调里收尾） ----
type GenState = {
  days: number;
  focusList: PrepFocus[];
  focusCount: number;
  createdFrom: string;
  diagnosis: string;
  diagnosisDone: boolean;
  fallback: string;
  taskCount: number;
  error: string;
};
const BLANK_GEN: GenState = {
  days: 14,
  focusList: [],
  focusCount: 0,
  createdFrom: "",
  diagnosis: "",
  diagnosisDone: false,
  fallback: "",
  taskCount: 0,
  error: "",
};

function reduceGen(
  prev: GenState | null,
  event: string,
  data: Record<string, unknown>,
): GenState {
  const state = prev ?? BLANK_GEN;
  switch (event) {
    case "start":
      return { ...state, days: Number(data.days) || state.days };
    case "focus": {
      const focus = (data.focus as PrepFocus[] | undefined) || [];
      return {
        ...state,
        focusList: focus,
        focusCount: focus.length,
        createdFrom: String(data.created_from || ""),
      };
    }
    case "model_start":
      return state;
    case "diagnosis_delta":
      return { ...state, diagnosis: state.diagnosis + String(data.text || "") };
    case "model_done": {
      const count = Number(data.count) || 0;
      return {
        ...state,
        diagnosis: String(data.diagnosis || state.diagnosis),
        diagnosisDone: true,
        focusList: state.focusList.slice(0, count),
      };
    }
    case "model_fallback":
      return {
        ...state,
        fallback: String(data.message || ""),
        diagnosisDone: true,
      };
    case "focus_item": {
      const item = data.item as PrepFocus | undefined;
      if (!item) return state;
      const list = [...state.focusList];
      list[Number(data.index) || list.length] = item;
      return { ...state, focusList: list.filter(Boolean) };
    }
    case "scheduled":
      return {
        ...state,
        taskCount: Number(data.task_count) || 0,
        days: Number(data.days) || state.days,
      };
    case "error":
      return {
        ...state,
        error: String((data as { message?: string }).message || "生成失败"),
      };
    default:
      return state;
  }
}

// ---- 调整对话（SSE 事件驱动的草稿态；done 收尾时并入正式轮次） ----
type AdjustDraft = {
  reply: string;
  applied: PrepAdjustApplied[];
  skipped: PrepAdjustApplied[];
  error: string;
};
const BLANK_DRAFT: AdjustDraft = {
  reply: "",
  applied: [],
  skipped: [],
  error: "",
};

export function PrepPlanView({
  onOpenInterview,
}: {
  // drill：错题强化直达（题目级）——带着错题引用进「错题强化官」，空数组=用户自选。
  onOpenInterview: (
    jobId?: string,
    focus?: string,
    drill?: InterviewDrillRef[],
  ) => void;
}) {
  const [data, setData] = useState<PrepSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [days, setDays] = useState(14);
  const [form, setForm] = useState<PrepForm>(DEFAULT_FORM);
  const [editing, setEditing] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [confirm, setConfirm] = useState<"regenerate" | "archive" | null>(null);
  const [gen, setGen] = useState<GenState | null>(null);
  const [runId, setRunId] = useState("");
  const [turns, setTurns] = useState<PrepAdjustTurn[]>([]);
  const [draft, setDraft] = useState<AdjustDraft | null>(null);
  const [adjustRunId, setAdjustRunId] = useState("");
  const [adjustInput, setAdjustInput] = useState("");
  const [rebuildAsk, setRebuildAsk] = useState("");
  const pendingTurn = useRef<PrepAdjustTurn | null>(null); // 发出但还没落库的用户消息，done 时并入轮次
  // 失败/断流路径的收尾：这条消息服务端没有落库（成功才 append），就地显回轮次，
  // 既不丢失上下文，也不会串进下一轮成功 done 的气泡里。
  function flushPendingTurn() {
    const sent = pendingTurn.current;
    if (!sent) return;
    pendingTurn.current = null;
    setTurns((prev) => [...prev, sent]);
  }

  const load = useCallback(async () => {
    try {
      const next = await getPrepPlan();
      setData(next);
      setError("");
      // 生成中途刷新/切页回来：续接活动 run，SSE 从头重放事件即可重建进度。
      if (next.active_run && !next.active_run.done && next.active_run.run_id) {
        setGen(null);
        setRunId(next.active_run.run_id);
      }
      if (
        next.active_adjust &&
        !next.active_adjust.done &&
        next.active_adjust.run_id
      ) {
        setDraft(null);
        setAdjustRunId(next.active_adjust.run_id);
      }
      void getPrepAdjustConversation()
        .then((result) => setTurns(result.messages || []))
        .catch(() => setTurns([]));
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    void api<PageInfo & { jobs: Job[] }>("/jobs?page=1&page_size=100")
      .then((result) => setJobs(result.jobs || []))
      .catch(() => setJobs([]));
  }, []);
  useEffect(() => {
    if (!data?.plan) return;
    const prefs = data.plan.prefs || DEFAULT_FORM;
    setForm({
      goal: prefs.goal || "",
      job_id: prefs.job_id || "",
      minutes_per_day: prefs.minutes_per_day || 20,
      use_llm: prefs.use_llm !== false,
    });
  }, [data?.plan?.id]);

  useRunStream(
    runId,
    openPrepPlanStream,
    (event, frame) => {
      if (event === "done") {
        const snapshot = frame as unknown as PrepSnapshot;
        setData(snapshot);
        setGen(null);
        setRunId("");
        toast(
          snapshot.plan?.origin === "model"
            ? "AI 定制计划已生成。"
            : "模型分析没有成功，已按本机材料生成规则版计划。",
        );
        return;
      }
      if (event === "error") {
        // 失败即刻解锁（2026-11-27 审计 P0）：runId 只在 done 清空曾让页面永久「生成中」，
        // 所有按钮禁用且 startGeneration 的 if (runId) return 挡住重试。gen.error 已由
        // reduceGen 写入、进度面板自带「重试」；这里清 runId 即可（cleanup 顺带关流，
        // 服务端随后补发的 end 帧不再需要）。
        setRunId("");
        return;
      }
      if (event !== "end") setGen((prev) => reduceGen(prev, event, frame));
    },
    () => {
      // 致命断流（后端重启，流 404）：按失败收尾解锁（钩子负责 CLOSED 判定与关流）。
      setRunId("");
      setGen((prev) =>
        prev
          ? { ...prev, error: prev.error || "生成连接中断了，可重试" }
          : prev,
      );
    },
  );

  useRunStream(
    adjustRunId,
    openPrepAdjustStream,
    (event, frame) => {
      if (event === "done") {
        const payload = frame as unknown as {
          reply: string;
          applied: PrepAdjustApplied[];
          snapshot: PrepSnapshot;
          confirm_required: { reason: string } | null;
        };
        setData(payload.snapshot);
        setDraft(null);
        setAdjustRunId("");
        setTurns((prev) => [
          ...prev,
          ...(pendingTurn.current ? [pendingTurn.current] : []),
          {
            role: "assistant",
            content: payload.reply,
            changes:
              payload.applied
                .map((item) => item.summary || "")
                .filter(Boolean)
                .join("；") || undefined,
          },
        ]);
        pendingTurn.current = null;
        return;
      }
      if (event === "confirm_required") {
        setRebuildAsk(
          String(
            (frame as { reason?: string }).reason ||
              "按最新资料重新生成整轮计划",
          ),
        );
        return;
      }
      if (event === "delta") {
        setDraft((prev) => ({
          ...(prev ?? BLANK_DRAFT),
          reply:
            (prev?.reply || "") +
            String((frame as { text?: string }).text || ""),
        }));
        return;
      }
      if (event === "applied" || event === "skipped") {
        setDraft((prev) => {
          const next = prev ?? BLANK_DRAFT;
          const item = frame as unknown as PrepAdjustApplied;
          return event === "applied"
            ? { ...next, applied: [...next.applied, item] }
            : { ...next, skipped: [...next.skipped, item] };
        });
        return;
      }
      if (event === "error") {
        // 失败即刻解锁（2026-11-27 审计 P0）：adjustRunId 只在 done 清空曾让输入框永久禁用。
        // 这条用户消息服务端没落库（成功才 append），就地显回轮次里；错误挂在教练气泡上。
        flushPendingTurn();
        setDraft((prev) => ({
          ...(prev ?? BLANK_DRAFT),
          error: String(
            (frame as { message?: string }).message || "调整对话失败",
          ),
        }));
        setAdjustRunId("");
        return;
      }
    },
    () => {
      // 致命断流（CLOSED）：同 done/error 收尾纪律，就地解锁。
      flushPendingTurn();
      setDraft((prev) =>
        prev
          ? { ...prev, error: prev.error || "调整连接中断了，请重试" }
          : prev,
      );
      setAdjustRunId("");
    },
  );

  async function act(key: string, action: () => Promise<PrepSnapshot>) {
    setBusy(key);
    try {
      setData(await action());
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy("");
    }
  }

  function onDone(task: PrepTask) {
    // 备注是可选的；不在交互里弹原生窗口（AGENTS.md 禁用 prompt），需要时可在后端扩展。
    void act(`done-${task.id}`, () => donePrepTask(task.id));
  }

  // 错题强化直达（题目级）：按任务来源场次预选错题；拉不到/没有就空选进面板，用户在错题本自选。
  async function drillTask(task: PrepTask) {
    let refs: InterviewDrillRef[] = [];
    try {
      const book = await listInterviewWeakPoints();
      refs = book.items
        .flatMap((item) => item.refs)
        .filter((ref) => ref.session_id === task.session_id);
    } catch {
      /* 错题本拉不到不拦跳转 */
    }
    onOpenInterview(task.job_id || "", "", refs);
  }

  const payload = { days, ...form };
  async function startGeneration(force = false) {
    if (runId) return; // 生成已在进行中：避免重复发起把进度面板打回重放起点
    try {
      const start = await startPrepPlanRun({ ...payload, force });
      if (start.run_id) {
        setGen(null);
        setRunId(start.run_id);
        setEditing(false);
      } else if (start.snapshot) {
        // 同步快路径：幂等命中或规则版秒级完成，不需要 SSE。
        setData(start.snapshot);
        setEditing(false);
        toast(
          start.reused
            ? "今天的计划已存在，直接沿用。"
            : "已生成新计划（规则版）。",
        );
      }
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function sendAdjust() {
    const message = adjustInput.trim();
    if (!message || adjustRunId || runId) return;
    try {
      const start = await startPrepAdjust(message);
      setAdjustInput("");
      if (!start.reused) {
        pendingTurn.current = { role: "user", content: message };
        setDraft(null);
        setAdjustRunId(start.run_id);
      }
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function confirmRebuild() {
    setRebuildAsk("");
    // 重排只带 days + force：prefs 用服务端存的已调整值（表单可能是旧状态）。
    const planDays = data?.plan?.days || 14;
    try {
      const start = await startPrepPlanRun({ days: planDays, force: true });
      if (start.run_id) {
        setGen(null);
        setRunId(start.run_id);
        setEditing(false);
      } else if (start.snapshot) {
        setData(start.snapshot);
        setEditing(false);
      }
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function confirmAction() {
    if (!data?.plan) return;
    const planId = data.plan.id;
    setBusy("confirm");
    try {
      if (confirm === "regenerate") {
        setConfirm(null);
        setBusy("");
        await startGeneration();
        return;
      }
      await archivePrepPlan(planId);
      await load();
      setEditing(false);
      toast("计划已归档，历史打卡记录仍会保留。");
      setConfirm(null);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy("");
    }
  }

  const generating = Boolean(runId);

  if (error)
    return (
      <section className="prep-view" aria-label="面试备战计划">
        <div className="paper-panel">
          <div className="empty">
            <h3>暂时读不到备战计划</h3>
            <p>{error}</p>
            <button className="button secondary" onClick={() => void load()}>
              重试
            </button>
          </div>
        </div>
      </section>
    );
  if (!data)
    return (
      <section className="prep-view">
        <div className="loading-state">
          <span className="spinner" />
          正在读取本机计划…
        </div>
      </section>
    );

  const plan = data.plan;
  const progress = data.progress;
  const activeTasks = [...data.today.tasks, ...data.today.overdue];
  const configured = data.model_configured;

  const genPanel = gen && (
    <div className="paper-panel pp-gen" aria-live="polite">
      <div className="pp-gen-head">
        <b>正在生成备战计划</b>
        <span className="tiny-label">{gen.days} 天一轮 · 边分析边出结果</span>
      </div>
      <ol className="pp-gen-steps">
        <li className={gen.focusCount > 0 ? "done" : "active"}>
          <span className="pp-gen-dot" />
          读取本机资料
          {gen.focusCount > 0 && (
            <span className="pp-gen-note">
              发现 {gen.focusCount} 项短板
              {gen.createdFrom &&
                ` · ${CREATED_FROM_LABELS[gen.createdFrom] || ""}`}
            </span>
          )}
        </li>
        <li
          className={
            gen.diagnosisDone ? "done" : gen.focusCount > 0 ? "active" : "wait"
          }
        >
          <span className="pp-gen-dot" />
          AI 教练诊断
          {gen.fallback && <span className="pp-gen-note">{gen.fallback}</span>}
        </li>
        <li
          className={
            gen.taskCount > 0 ? "done" : gen.diagnosisDone ? "active" : "wait"
          }
        >
          <span className="pp-gen-dot" />
          排期 {gen.days} 天任务
          {gen.taskCount > 0 && (
            <span className="pp-gen-note">
              共 {gen.taskCount} 项，写入后立即可打卡
            </span>
          )}
        </li>
      </ol>
      {gen.diagnosis && (
        <p className="pp-gen-diagnosis">
          {gen.diagnosis}
          {!gen.diagnosisDone && <span className="pp-gen-caret" />}
        </p>
      )}
      {gen.focusList.length > 0 && (
        <div className="pp-gen-focus">
          {gen.focusList.map((item, index) => (
            <span key={`${item.topic}-${index}`} className="pp-gen-chip">
              {item.topic}
            </span>
          ))}
        </div>
      )}
      {gen.error && (
        <div className="inline-empty pp-gen-error">
          {gen.error}
          <button
            className="button secondary small"
            onClick={() => void startGeneration()}
          >
            重试
          </button>
        </div>
      )}
    </div>
  );

  return (
    <section className="prep-view" aria-label="面试备战计划">
      <div className="paper-panel pp-intro">
        <div className="section-heading">
          <div>
            <h1>面试备战计划</h1>
            <p className="body-small muted">
              怕的不是没面试，是机会来了接不住。填上重点与目标岗位，教练会读你本机已有的面试复盘、失败归因与岗位资料，给出针对你的短板与今天就能做的练法。
            </p>
          </div>
          <span className="tiny-label">
            材料只取自本机 · 模型未配置时自动退规则版
          </span>
        </div>
        <p className="pp-intro-note">
          <Icon name="book" size={15} />
          每条都写清依据；没有面试数据时明示为通用入门，不会假装知道你的薄弱项。
        </p>
        {!configured && (
          <p className="pp-intro-note">
            当前未配置分析模型，生成会按规则版排期；到「设置 →
            模型」配置后重新生成即可得到 AI 定制版。
          </p>
        )}
      </div>

      {genPanel}

      {!plan ? (
        <div className="paper-panel pp-onboard">
          <div className="empty">
            <div className="empty-icon">
              <Icon name="target" />
            </div>
            <h3>还没有备战计划</h3>
            <p>
              生成后每天固定一场模拟面试，加一件针对短板的小练习；复盘完成会自动打卡。
            </p>
          </div>
          <PrepConfig
            days={days}
            onDays={setDays}
            form={form}
            onForm={setForm}
            jobs={jobs}
            configured={configured}
          />
          <div className="pp-generate">
            <button
              className="button primary"
              disabled={generating || Boolean(busy)}
              onClick={() => void startGeneration()}
            >
              {generating ? "正在生成，见上方进度…" : "生成我的备战计划"}
              <Icon name="arrow" size={15} />
            </button>
          </div>
          <p className="tiny-label">
            计划可以随时重新生成；旧计划归档保留，历史打卡不会被抹掉。
          </p>
        </div>
      ) : (
        <>
          {plan.diagnosis && (
            <div className="paper-panel pp-diagnosis">
              <div className="section-heading">
                <div>
                  <h2>教练诊断</h2>
                  <p className="body-small muted">
                    基于你已存下的材料生成，不是通用鸡汤。
                  </p>
                </div>
                <span className="ppm-tag">AI 分析</span>
              </div>
              <p className="pp-diagnosis-text">{plan.diagnosis}</p>
            </div>
          )}

          <div className="paper-panel pp-progress">
            <div className="section-heading">
              <div>
                <h2>这一轮的进度</h2>
                <p className="body-small muted">
                  {ORIGIN_LABELS[plan.origin] || "规则模板"} ·{" "}
                  {CREATED_FROM_LABELS[plan.created_from] || "按本机数据生成"} ·{" "}
                  {plan.start_date} 至 {plan.end_date}
                </p>
              </div>
              <span
                className="pp-streak"
                title={`历史最长连续 ${progress.best_streak} 天`}
              >
                连续 {progress.streak} 天
              </span>
            </div>
            <div className="pp-progress-grid">
              <div className="pp-stat">
                <b>
                  {progress.required_done}/{progress.required_total}
                </b>
                <span>必做完成</span>
              </div>
              <div className="pp-stat">
                <b>
                  {progress.done}/{progress.total}
                </b>
                <span>全部任务</span>
              </div>
              <div className="pp-stat">
                <b>{Math.round(progress.rate * 100)}%</b>
                <span>完成比例</span>
              </div>
              <div className="pp-stat">
                <b>{progress.best_streak}</b>
                <span>最长连续</span>
              </div>
            </div>
            <div
              className="pp-progress-bar"
              role="img"
              aria-label={`完成 ${Math.round(progress.rate * 100)}%`}
            >
              <span style={{ width: `${Math.round(progress.rate * 100)}%` }} />
            </div>
            <p className="tiny-label">
              {progress.last_done_at
                ? `最近一次打卡：${progress.last_done_at.slice(0, 16).replace("T", " ")}`
                : "还没有打卡记录——今天从一件小事开始就行。"}
              {progress.streak === 0 && progress.done > 0
                ? " 中断很常见，今天重新开始就好。"
                : ""}
            </p>
            <div className="pp-progress-actions">
              <button
                className="button secondary small"
                disabled={generating || Boolean(busy)}
                onClick={() => setEditing((value) => !value)}
              >
                {editing ? "收起设置" : "调整重点并重新生成"}
              </button>
              <button
                className="button secondary small"
                disabled={generating || Boolean(busy)}
                onClick={() => setConfirm("archive")}
              >
                归档本轮
              </button>
            </div>
            {editing && (
              <div className="pp-edit">
                <PrepConfig
                  days={days}
                  onDays={setDays}
                  form={form}
                  onForm={setForm}
                  jobs={jobs}
                  configured={configured}
                />
                <div className="pp-edit-actions">
                  <button
                    className="button primary small"
                    disabled={generating || Boolean(busy)}
                    onClick={() => setConfirm("regenerate")}
                  >
                    按这些设置重新生成
                  </button>
                  <button
                    className="text-button"
                    onClick={() => setEditing(false)}
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="paper-panel pp-today">
            <div className="section-heading">
              <div>
                <h2>今天做什么</h2>
                <p className="body-small muted">
                  {data.today.date} · 只看今天，不摆后面的欠账。
                </p>
              </div>
              <span className="tiny-label">
                必做 {data.today.required_done}/{data.today.required_total}
              </span>
            </div>
            {activeTasks.length === 0 ? (
              <div className="inline-empty">
                今天没有安排任务。可以重新生成一轮，或等明天再看。
              </div>
            ) : (
              <ul className="pp-task-list">
                {activeTasks.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    busy={busy}
                    onDone={() => onDone(task)}
                    onSkip={() =>
                      void act(`skip-${task.id}`, () => skipPrepTask(task.id))
                    }
                    onReopen={() =>
                      void act(`reopen-${task.id}`, () =>
                        reopenPrepTask(task.id),
                      )
                    }
                    onOpenInterview={() =>
                      onOpenInterview(task.job_id || "", task.topic)
                    }
                    onDrill={() => void drillTask(task)}
                  />
                ))}
              </ul>
            )}
          </div>

          {plan && (
            <div className="paper-panel pp-adjust">
              <div className="section-heading">
                <div>
                  <h2>跟教练说，随时调整</h2>
                  <p className="body-small muted">
                    觉得哪天太重、哪条不对、想换个重点，直接说；改完立刻生效，打卡记录不动。
                  </p>
                </div>
                <span className="tiny-label">大改（整轮重排）会先跟你确认</span>
              </div>
              {(turns.length > 0 || draft) && (
                <ul className="pp-adjust-turns">
                  {turns.map((item, index) => (
                    <li
                      key={item.id || `t-${index}`}
                      className={`pp-adjust-turn ${item.role}`}
                    >
                      <span className="pp-adjust-role">
                        {item.role === "user" ? "你" : "教练"}
                      </span>
                      <div className="pp-adjust-body">
                        <p>{item.content}</p>
                        {item.changes && (
                          <div className="pp-adjust-applied">
                            <Icon name="check" size={13} />
                            {item.changes}
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                  {draft && (
                    <li className="pp-adjust-turn assistant streaming">
                      <span className="pp-adjust-role">教练</span>
                      <div className="pp-adjust-body">
                        {draft.reply && (
                          <p>
                            {draft.reply}
                            {!draft.error && <span className="pp-gen-caret" />}
                          </p>
                        )}
                        {draft.applied.length > 0 && (
                          <div className="pp-adjust-applied">
                            <Icon name="check" size={13} />
                            {draft.applied
                              .map((item) => item.summary || "")
                              .filter(Boolean)
                              .join("；")}
                          </div>
                        )}
                        {draft.skipped.length > 0 && (
                          <div className="pp-adjust-skipped">
                            {draft.skipped
                              .map((item, index) => (
                                <span key={index}>{item.note}</span>
                              ))
                              .join("；")}
                          </div>
                        )}
                        {draft.error && (
                          <div className="pp-adjust-error">{draft.error}</div>
                        )}
                      </div>
                    </li>
                  )}
                </ul>
              )}
              <div className="pp-adjust-input">
                <textarea
                  rows={1}
                  value={adjustInput}
                  maxLength={2000}
                  placeholder={
                    configured
                      ? "例：周三太忙换到周五 / 每天只想花 10 分钟 / 重新聊聊项目深挖怎么练"
                      : "调整对话需要先在「设置 → 模型」绑定分析模型"
                  }
                  disabled={!configured || generating || Boolean(adjustRunId)}
                  onChange={(event) => setAdjustInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void sendAdjust();
                    }
                  }}
                />
                <button
                  className="button primary small"
                  disabled={
                    !configured ||
                    !adjustInput.trim() ||
                    generating ||
                    Boolean(adjustRunId)
                  }
                  onClick={() => void sendAdjust()}
                >
                  发送
                </button>
              </div>
            </div>
          )}

          {data.week.length > 0 && (
            <details className="paper-panel pp-week">
              <summary>
                <Icon name="chevron" size={14} />
                整周安排（{data.week.length} 天，折叠查看）
              </summary>
              <ul className="pp-week-list">
                {data.week.map((day) => (
                  <li
                    key={day.date}
                    className={day.date === data.today.date ? "current" : ""}
                  >
                    <span className="pp-week-date">
                      第 {day.day_index} 天 · {day.date.slice(5)}
                    </span>
                    <span className="pp-week-count">
                      {day.done}/{day.total} 已完成
                    </span>
                    <span className="pp-week-titles">
                      {day.tasks
                        .map(
                          (task) =>
                            `${KIND_LABELS[task.kind] || task.kind}：${task.title}`,
                        )
                        .join(" · ")}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}

          <div className="paper-panel pp-focus">
            <div className="section-heading">
              <div>
                <h2>这轮针对的短板</h2>
                <p className="body-small muted">
                  每条都写清了依据；重复出现的失分点会排在最前。
                </p>
              </div>
            </div>
            <ul className="pp-focus-list">
              {plan.focus.map((item, index) => (
                <li key={`${item.topic}-${index}`}>
                  <div className="pp-focus-head">
                    <b>{item.topic}</b>
                    <span className="ppm-tag">
                      {item.weight === 3
                        ? "重复失分"
                        : item.weight
                          ? "面试暴露"
                          : "通用入门"}
                    </span>
                  </div>
                  {item.detail && <p className="body-small">{item.detail}</p>}
                  {item.drill && (
                    <p className="pp-focus-drill">具体练法：{item.drill}</p>
                  )}
                  <span className="tiny-label">来源：{item.source}</span>
                </li>
              ))}
            </ul>
          </div>

          {data.history.length > 1 && (
            <div className="paper-panel pp-history">
              <div className="section-heading">
                <div>
                  <h2>历史计划</h2>
                </div>
              </div>
              <ul className="pp-history-list">
                {data.history.map((item) => (
                  <li key={item.id}>
                    <span>
                      {item.start_date} 起 · {item.days} 天 ·{" "}
                      {CREATED_FROM_LABELS[item.created_from] ||
                        item.created_from}
                    </span>
                    <span className={`pp-history-status ${item.status}`}>
                      {item.status === "active" ? "进行中" : "已归档"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {confirm && (
        <ConfirmDialog
          title={confirm === "regenerate" ? "重新生成备战计划" : "归档本轮计划"}
          message={
            confirm === "regenerate"
              ? "会按你填的备战重点、目标岗位和本机最新资料重新分析并生成一轮，当前计划归档保留、历史打卡不删除。"
              : "归档后这一轮不再显示任务，历史记录仍保留在库里。"
          }
          confirmLabel={confirm === "regenerate" ? "重新生成" : "归档"}
          danger={confirm === "archive"}
          busy={busy === "confirm"}
          onConfirm={() => void confirmAction()}
          onCancel={() => setConfirm(null)}
        />
      )}

      {rebuildAsk && (
        <ConfirmDialog
          title="整轮重排备战计划"
          message={`教练建议按最新资料重新生成整轮计划（${rebuildAsk}）。当前计划会归档保留，历史打卡不删除，但未完成的任务安排会重新排。`}
          confirmLabel="确认重排"
          busy={generating}
          onConfirm={() => void confirmRebuild()}
          onCancel={() => setRebuildAsk("")}
        />
      )}
    </section>
  );
}

function PrepConfig({
  days,
  onDays,
  form,
  onForm,
  jobs,
  configured,
}: {
  days: number;
  onDays: (value: number) => void;
  form: PrepForm;
  onForm: (value: PrepForm) => void;
  jobs: Job[];
  configured: boolean;
}) {
  return (
    <div className="pp-config">
      <label className="pp-field">
        <span>备战重点（选填）</span>
        <textarea
          value={form.goal}
          rows={2}
          maxLength={300}
          placeholder="例：两周后终面，重点补项目深挖与量化表达"
          onChange={(event) => onForm({ ...form, goal: event.target.value })}
        />
      </label>
      <div className="pp-config-row">
        <label className="pp-field">
          <span>目标岗位（选填）</span>
          <select
            value={form.job_id}
            onChange={(event) =>
              onForm({ ...form, job_id: event.target.value })
            }
          >
            <option value="">不指定 · 用全部本机资料</option>
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                {job.company} · {job.title}
              </option>
            ))}
          </select>
        </label>
        <label className="pp-field">
          <span>每天可投入</span>
          <select
            value={form.minutes_per_day}
            onChange={(event) =>
              onForm({ ...form, minutes_per_day: Number(event.target.value) })
            }
          >
            <option value={10}>10 分钟（轻量）</option>
            <option value={20}>20 分钟（默认）</option>
            <option value={40}>40 分钟</option>
            <option value={60}>60 分钟</option>
          </select>
        </label>
      </div>
      <div className="pp-config-row">
        <label className="pp-field">
          <span>计划周期</span>
          <select
            value={days}
            onChange={(event) => onDays(Number(event.target.value))}
          >
            <option value={7}>7 天（先试一周）</option>
            <option value={14}>14 天（默认）</option>
            <option value={28}>28 天（完整一轮）</option>
          </select>
        </label>
        <label className="pp-check">
          <input
            type="checkbox"
            checked={form.use_llm}
            onChange={(event) =>
              onForm({ ...form, use_llm: event.target.checked })
            }
          />
          用模型分析{configured ? "" : "（当前未配置，将按规则生成）"}
        </label>
      </div>
    </div>
  );
}

function TaskCard({
  task,
  busy,
  onDone,
  onSkip,
  onReopen,
  onOpenInterview,
  onDrill,
}: {
  task: PrepTask;
  busy: string;
  onDone: () => void;
  onSkip: () => void;
  onReopen: () => void;
  onOpenInterview: () => void;
  onDrill: () => void;
}) {
  const pending = task.status === "pending";
  return (
    <li
      className={`pp-task ${task.kind}${pending ? "" : " " + task.status}${task.overdue ? " overdue" : ""}`}
    >
      <div className="pp-task-main">
        <div className="pp-task-head">
          <span className="ppm-tag">{KIND_LABELS[task.kind] || task.kind}</span>
          {task.required ? (
            <span className="pp-required">必做</span>
          ) : (
            <span className="tiny-label">选做</span>
          )}
          <span className="tiny-label">约 {task.minutes} 分钟</span>
          {task.overdue && <span className="pp-overdue-label">补做也算</span>}
        </div>
        <b className="pp-task-title">{task.title}</b>
        <p className="body-small">{task.detail}</p>
        <span className="tiny-label">依据：{task.source}</span>
        {task.note && <p className="pp-task-note">打卡备注：{task.note}</p>}
      </div>
      <div className="pp-task-actions">
        {pending ? (
          <>
            {task.kind === "mock" && (
              <>
                <button
                  className="button primary small"
                  disabled={Boolean(busy)}
                  onClick={onOpenInterview}
                >
                  <Icon name="mic" size={14} />
                  开始模拟面试
                </button>
                <button
                  className="button secondary small"
                  disabled={Boolean(busy)}
                  title="把这场复盘里答得不好的题带去「错题强化官」，换问法重考一遍"
                  onClick={onDrill}
                >
                  错题强化
                </button>
              </>
            )}
            <button
              className="button secondary small"
              disabled={Boolean(busy)}
              onClick={onDone}
            >
              完成
            </button>
            <button
              className="text-button"
              disabled={Boolean(busy)}
              onClick={onSkip}
            >
              跳过
            </button>
          </>
        ) : (
          <>
            <span className="pp-task-state">
              {task.status === "done" ? "已完成" : "已跳过"}
            </span>
            <button
              className="text-button"
              disabled={Boolean(busy)}
              onClick={onReopen}
            >
              撤销
            </button>
          </>
        )}
      </div>
    </li>
  );
}
