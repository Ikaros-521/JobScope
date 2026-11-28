import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import AttributionPanel from "./AttributionPanel";
import { toast } from "./toast";
import {
  api,
  cancelInterviewRun,
  createInterviewSession,
  date,
  deleteInterviewSession,
  deleteInterviewTranscript,
  finishInterview,
  getInterviewMessages,
  getInterviewSession,
  listInterviewPersonas,
  listInterviewSessions,
  listInterviewTranscripts,
  openInterviewStream,
  sendInterviewTurn,
} from "./api";
import type {
  InterviewDrillRef,
  InterviewMessage,
  InterviewPersonaCard,
  InterviewSession,
  InterviewSessionSummary,
  InterviewStyle,
  InterviewReview,
  InterviewWeakPoints,
  TranscriptSummary,
} from "./api";
import type {
  StreamState,
  RunProgress,
  ReportFeed,
  ReportFeedItem,
} from "./interview/shared";
import {
  STATUS_NAMES,
  TRANSCRIPT_STATUS_NAMES,
  MAX_DRILLS,
  loadAttributionBundle,
  copyText,
  interviewFileName,
  buildInterviewMarkdown,
} from "./interview/shared";
import { CreatePanel } from "./interview/CreatePanel";
import { RoomPanel, ProgressiveReport } from "./interview/RoomPanel";
import { ReportPanel } from "./interview/ReportPanel";
import { RealWorkspace } from "./interview/RealWorkspace";

// 模拟面试工作区（2026-11-19）：左侧场次列表 + 右侧「面试间 | 复盘报告」。
// 面试官是纯拟真的无工具 agent：对话中零提示零点评，评价全部在复盘页签。
// 流式渲染只复用 Markdown，不复用 ChatWorkspace（模型下拉/工具选项不适用面试场景）。
// 2026-11-21 增加并列的第二条链路「真实复盘」：外部 ASR 逐字稿导入 → 模型初标说话人
// → 人工核对修正 → 结合 JD 的复盘报告（侧栏顶部分段切换，两套工作区互不干扰）。
// 2026-11-28 拆分：常量/类型/导出助手在 interview/shared，配置面板/选题器/面试间/
// 复盘报告/真实复盘工作区各自成文件——本文件只留主容器与跨区编排。

export type InterviewDeepLink = {
  jobId: string;
  focus: string;
  drill?: InterviewDrillRef[];
  token: number;
} | null;

export default function InterviewView({
  deepLink,
  modelConfigured,
  onConfigureModel,
}: {
  deepLink: InterviewDeepLink;
  modelConfigured: boolean;
  onConfigureModel: () => void;
}) {
  const [personas, setPersonas] = useState<InterviewPersonaCard[]>([]);
  const [sessions, setSessions] = useState<InterviewSessionSummary[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [session, setSession] = useState<InterviewSession | null>(null);
  const [messages, setMessages] = useState<InterviewMessage[]>([]);
  const [stream, setStream] = useState<StreamState>(null);
  // 提交回答到 SSE 首字之间的空窗：请求在途时 streamRef 还没挂上，单独记一笔让"生成中"动画立刻出现，同时防重复提交。
  const [awaitingReply, setAwaitingReply] = useState(false);
  const [runProgress, setRunProgress] = useState<RunProgress>(null);
  const [reportFeed, setReportFeed] = useState<ReportFeed>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"room" | "report" | "attribution">("room");
  // 新建面板：卡片=岗位专家（细分到岗位类型），公司风格是独立开关；目标岗位三模式：
  // 岗位库选择（自动带 JD/评估缺口/简历存档/公司资料）｜粘贴 JD｜自由练（自定义卡必须填岗位名）。
  const [creating, setCreating] = useState(false);
  const [card, setCard] = useState<string>("");
  const [styles, setStyles] = useState<InterviewStyle[]>([]);
  const [style, setStyle] = useState("generic");
  const [round, setRound] = useState("");
  const [intensity, setIntensity] = useState<"gentle" | "standard" | "stress">(
    "standard",
  );
  const [questionCount, setQuestionCount] = useState(8);
  const [mode, setMode] = useState<"job" | "jd" | "free">("free");
  const [jobId, setJobId] = useState("");
  const [jobLabel, setJobLabel] = useState("");
  const [jobTitle, setJobTitle] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [jdText, setJdText] = useState("");
  const [answer, setAnswer] = useState("");
  // 备战计划带来的当日专项：出题时作为 weak_focus 定向覆盖，并在新建面板顶部明示。
  const [focusHint, setFocusHint] = useState("");
  // 错题强化：选中的错题引用（来源记录 + report.questions 下标）。由「错题强化官」卡片的错题本面板维护；
  // 复盘页的「重练此题 / 强化重练本场错题」也带着引用打开新建面板（真实复盘页入口会切回模拟侧）。
  const [drillSelection, setDrillSelection] = useState<InterviewDrillRef[]>([]);
  // starting：点「开始出题」到场次建好之间的一次性闸门，防连点重场，也让按钮立刻进加载态。
  const [starting, setStarting] = useState(false);
  // 真实复盘：侧栏顶部分段切换 模拟/真实；逐字稿列表与选中 id 挂在本层，
  // 导入面板/标注核对/复盘报告的工作态由 RealWorkspace 自管（自带流与编辑副本）。
  const [side, setSide] = useState<"mock" | "real">("mock");
  const [transcripts, setTranscripts] = useState<TranscriptSummary[]>([]);
  const [selectedTranscript, setSelectedTranscript] = useState("");
  const streamRef = useRef<EventSource | null>(null);
  const selectedRef = useRef("");
  selectedRef.current = selected;
  const threadRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions((await listInterviewSessions()).sessions);
    } catch {
      /* 列表失败不阻塞面试间 */
    }
  }, []);

  const refreshTranscripts = useCallback(async () => {
    try {
      setTranscripts((await listInterviewTranscripts()).transcripts);
    } catch {
      /* 列表失败不阻塞工作区 */
    }
  }, []);

  useEffect(() => {
    if (side === "real") void refreshTranscripts();
  }, [side, refreshTranscripts]);

  async function removeTranscript(id: string) {
    try {
      await deleteInterviewTranscript(id);
      if (selectedTranscript === id) setSelectedTranscript("");
      void refreshTranscripts();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    setStream(null);
    setRunProgress(null);
    setReportFeed(null);
    setAwaitingReply(false);
  }, []);

  // 待首装场次的 id：新建后先拉 planning 态场次当等待区；若 SSE done 抢先换装了题单，
  // 迟到的 GET 结果不得拿旧 planning 态把就绪场覆盖回去（否则等待区永远卡住）。
  const planningRef = useRef("");

  const loadSession = useCallback(async (id: string) => {
    try {
      const detail = await getInterviewSession(id);
      planningRef.current = "";
      setSession(detail.session);
      if (detail.session.conversation_id) {
        const data = await getInterviewMessages(id);
        if (selectedRef.current === id) setMessages(data.messages);
      } else if (selectedRef.current === id) {
        setMessages([]);
      }
      // 后台仍在跑（切走再回来）：重连事件流续播，无需用户操作。
      if (
        detail.active_run &&
        !detail.active_run.done &&
        streamRef.current === null
      ) {
        attach(detail.active_run.run_id);
      }
      return detail.session;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const attach = useCallback(
    (runId: string) => {
      closeStream();
      setStream({ runId, answer: "" });
      const source = openInterviewStream(runId, (event, data) => {
        if (event === "delta") {
          setStream((current) =>
            current && current.runId === runId
              ? { ...current, answer: current.answer + String(data.text ?? "") }
              : current,
          );
          return;
        }
        if (event === "progress") {
          // 复盘渐进事件三段：start 铺骨架 → review 原位填充 → summary 汇总中。
          // 事件流重连会从 0 重放，按题号折叠天然幂等。
          if (data.stage === "start" && Array.isArray(data.items)) {
            setReportFeed({
              items: (data.items as Record<string, unknown>[]).map((item) => ({
                index: Number(item.index ?? 0),
                topic: String(item.topic ?? ""),
                question: String(item.question ?? ""),
                // 错题强化场次：骨架卡先带上次失分点，点评填入后即可对照（重放幂等：整表重建）。
                previous_problems: Array.isArray(item.previous_problems)
                  ? (item.previous_problems as string[])
                  : undefined,
              })),
              done: 0,
              total: Number(data.total ?? (data.items as unknown[]).length),
              stage: "review",
            });
          } else if (data.stage === "review" && data.index != null) {
            const index = Number(data.index);
            setReportFeed((current) =>
              current
                ? {
                    ...current,
                    done: Number(data.done ?? current.done),
                    stage: "review",
                    items: current.items.map((item) =>
                      item.index === index
                        ? {
                            ...item,
                            review: data.review as ReportFeedItem["review"],
                          }
                        : item,
                    ),
                  }
                : current,
            );
          } else if (data.stage === "summary") {
            setReportFeed((current) =>
              current ? { ...current, stage: "summary" } : current,
            );
          }
          setRunProgress({
            stage: data.stage === "summary" ? "summary" : "review",
            done: Number(data.done ?? 0),
            total: Number(data.total ?? 0),
            topic: data.topic ? String(data.topic) : undefined,
          });
          return;
        }
        if (event === "done") {
          closeStream();
          planningRef.current = "";
          const next = data.session as InterviewSession | undefined;
          if (selectedRef.current && next && next.id === selectedRef.current) {
            setSession(next);
            void getInterviewMessages(next.id)
              .then((item) => {
                if (selectedRef.current === next.id) setMessages(item.messages);
              })
              .catch(() => {});
          } else if (next) {
            setSelected(next.id);
            selectedRef.current = next.id;
            setSession(next);
            void getInterviewMessages(next.id)
              .then((item) => {
                if (selectedRef.current === next.id) setMessages(item.messages);
              })
              .catch(() => {});
          }
          if (next?.status === "done") setTab("report");
          void refreshSessions();
          return;
        }
        if (event === "error") {
          closeStream();
          setError(String(data.message ?? "生成失败"));
          if (selectedRef.current) void loadSession(selectedRef.current);
          void refreshSessions();
          return;
        }
        if (event === "cancelled") {
          closeStream();
          setError("已停止本轮生成");
          if (selectedRef.current) void loadSession(selectedRef.current);
          void refreshSessions();
          return;
        }
        if (event === "end") {
          // done 未达而流终止（服务重启等）：以服务端状态为准解锁。
          closeStream();
          if (selectedRef.current) void loadSession(selectedRef.current);
        }
      });
      source.onerror = () => {
        // 短暂网络故障由 EventSource 自动重连续播事件日志；致命断流（后端重启，流 404）进入
        // CLOSED 后不会再有任何帧——error/end 都没有——必须就地解锁并以服务端状态为准。
        if (source.readyState !== EventSource.CLOSED) return;
        closeStream();
        setError("生成连接已中断，请重试");
        if (selectedRef.current) void loadSession(selectedRef.current);
        void refreshSessions();
      };
      streamRef.current = source;
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [closeStream, refreshSessions],
  );

  useEffect(() => {
    void listInterviewPersonas()
      .then((data) => {
        setPersonas(data.cards);
        setStyles(data.styles);
      })
      .catch((err) => setError((err as Error).message));
    void refreshSessions();
    return closeStream;
  }, [refreshSessions, closeStream]);

  // 岗位详情/评估缺口/备战计划深链：打开新建面板并预选岗位；focus 是备战专项，注入出题 weak_focus。
  // drill（失败博物馆/备战计划任务卡的「错题强化」直达）进「错题强化官」并预选错题——数组字段
  // 存在即算深链，空数组=这场没有可预选的错题，让用户在错题本里自选。
  // 三者全空的深链不算深链（普通刷新后可能收到），忽略之，避免误开新建面板挡住面试间。
  useEffect(() => {
    if (!deepLink) return;
    const drills = deepLink.drill;
    if (!deepLink.jobId && !deepLink.focus && !drills) return;
    setCreating(true);
    setMode(deepLink.jobId ? "job" : "free");
    setJobId(deepLink.jobId);
    setJobLabel("");
    if (drills) {
      setFocusHint("");
      setSide("mock");
      setCard("reinforce");
      setRound("business");
      setError("");
      setDrillSelection(drills.slice(0, MAX_DRILLS));
    } else {
      setFocusHint(deepLink.focus || "");
      setCard("");
    }
    if (!deepLink.jobId) return;
    void api<{ job: { title: string; company: string } }>(
      `/jobs/${encodeURIComponent(deepLink.jobId)}`,
    )
      .then((data) => setJobLabel(`${data.job.company} · ${data.job.title}`))
      .catch(() => setJobLabel(""));
  }, [deepLink]);

  // 流式期间贴底：新 delta 与新消息都把面试间滚到最下。
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, stream?.answer, awaitingReply]);

  const selectSession = useCallback(
    (id: string) => {
      closeStream();
      setError("");
      setAnswer("");
      setTab("room");
      setCreating(false);
      setSelected(id);
      selectedRef.current = id;
      setSession(null);
      setMessages([]);
      void loadSession(id);
    },
    [closeStream, loadSession],
  );

  async function startSession() {
    if (starting || busy) return;
    setError("");
    if (!card) {
      setError("请先选择面试官岗位");
      return;
    }
    const targetJobId = mode === "job" ? jobId : "";
    if (mode === "job" && !targetJobId) {
      setError("请从岗位库选择一个岗位");
      return;
    }
    if (card === "custom" && mode === "free" && !jobTitle.trim()) {
      setError("自定义岗位需要先填写目标岗位名称");
      return;
    }
    if (card === "reinforce" && !drillSelection.length) {
      setError("请先选择要重练的错题");
      return;
    }
    setStarting(true);
    try {
      const started = await createInterviewSession({
        persona_id: card,
        company_style: style,
        round,
        intensity,
        question_count: questionCount,
        ...(targetJobId ? { job_id: targetJobId } : {}),
        ...(mode === "jd"
          ? {
              jd_text: jdText.trim(),
              company: companyName.trim(),
              ...(jobTitle.trim() ? { job_title: jobTitle.trim() } : {}),
            }
          : {}),
        ...(mode === "free"
          ? {
              company: companyName.trim(),
              ...(card === "custom" && jobTitle.trim()
                ? { job_title: jobTitle.trim() }
                : {}),
            }
          : {}),
        ...(focusHint ? { focus: focusHint } : {}),
        // 错题强化：每道错题出一道变式题，题量随错题数（question_count 由服务端覆盖）。
        ...(card === "reinforce" ? { drill: drillSelection } : {}),
      });
      setCreating(false);
      setJdText("");
      setJobTitle("");
      setCompanyName("");
      setFocusHint("");
      setDrillSelection([]);
      setTab("room");
      setSelected(started.session_id);
      selectedRef.current = started.session_id;
      setSession(null);
      setMessages([]);
      attach(started.run_id);
      // 立刻把 planning 态场次拉进面试间当等待区（出题在后台跑）：不再让表单原地干等 1-3 分钟。
      planningRef.current = started.session_id;
      try {
        const detail = await getInterviewSession(started.session_id);
        if (
          selectedRef.current === started.session_id &&
          planningRef.current === started.session_id
        )
          setSession(detail.session);
      } catch {
        /* 拉取失败不打断：SSE done 事件兜底换装 */
      }
      void refreshSessions();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setStarting(false);
    }
  }

  async function send() {
    if (!session || streamRef.current || awaitingReply) return;
    const content = answer.trim();
    if (session.status === "ready" ? false : !content) return;
    const optimistic = content;
    setAwaitingReply(true);
    setAnswer("");
    if (optimistic)
      setMessages((current) => [
        ...current,
        { id: `tmp-${Date.now()}`, ts: "", role: "user", content: optimistic },
      ]);
    try {
      const started = await sendInterviewTurn(session.id, optimistic);
      attach(started.run_id);
    } catch (err) {
      setError((err as Error).message);
      if (optimistic)
        setMessages((current) =>
          current.filter((item) => !item.id.startsWith("tmp-")),
        );
    } finally {
      // attach 已挂上流：后续由 busy + 首字驱动动画，这里只收请求在途的旗标。
      setAwaitingReply(false);
    }
  }

  async function finish() {
    if (!session || streamRef.current) return;
    try {
      const started = await finishInterview(session.id);
      // 乐观置 reporting：finish 只返回 run_id，不置的话客户端 session 停在 ready/done，
      // 渐进复盘视图永远不亮（done 事件会用服务端权威状态覆盖）。
      setSession((current) =>
        current ? { ...current, status: "reporting" } : current,
      );
      attach(started.run_id);
      // 点结束即进复盘页签看渐进生成（骨架→逐题填充→汇总→终版），不在面试间干等进度条。
      setTab("report");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function stop() {
    if (!stream) return;
    try {
      await cancelInterviewRun(stream.runId);
    } catch {
      /* 等服务端 cancelled 事件兜底 */
    }
  }

  async function removeSession(id: string) {
    try {
      await deleteInterviewSession(id);
      if (selectedRef.current === id) {
        closeStream();
        setSelected("");
        selectedRef.current = "";
        setSession(null);
        setMessages([]);
      }
      void refreshSessions();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  // ---- 错题强化：从复盘页带着错题打开「错题强化官」的新建面板（真实复盘页入口会切回模拟侧） ----
  function openDrill(
    refs: InterviewDrillRef[],
    job?: { id: string; label: string },
  ) {
    if (!refs.length) return;
    setError("");
    setFocusHint("");
    setSide("mock");
    setCreating(true);
    setCard("reinforce");
    setRound("business");
    setDrillSelection(refs.slice(0, MAX_DRILLS));
    // 来源岗位直接带上：同一岗位的 JD 再问一遍，变式题才贴岗；没有岗位锚点则留自由练。
    if (job?.id) {
      setMode("job");
      setJobId(job.id);
      setJobLabel(job.label);
    } else {
      setMode("free");
      setJobId("");
      setJobLabel("");
    }
  }

  function defaultDrillSelection(book: InterviewWeakPoints) {
    // 直接点卡片进来（没带预设）：默认勾最近一场复盘的错题，优先还没补齐的——读完复盘顺手就能练。
    const first = book.items[0];
    if (!first) return [];
    const group = book.items.filter(
      (item) =>
        item.source === first.source && item.session_id === first.session_id,
    );
    const pending = group.filter((item) => item.practice?.after !== 0);
    return (pending.length ? pending : group)
      .flatMap((item) => item.refs.slice(0, 1))
      .slice(0, MAX_DRILLS);
  }

  // 场次复盘里一道题的错题引用。错题强化场次的题带 drill_ref（它重练的原错题）：再练一轮练的是
  // 原题——强化场自己的变式题不进错题本，指过来会被服务端判为失效引用。
  function drillTargetOf(
    session: InterviewSession,
    item: InterviewReview | undefined,
    index: number,
  ): InterviewDrillRef {
    return item?.drill_ref ?? { source: "mock", session_id: session.id, index };
  }

  const plan = session?.question_plan;
  const progress = session?.progress;
  const askedCount = progress
    ? Math.min(progress.current_index + 1, plan?.questions.length ?? 0)
    : 0;
  const busy = streamRef.current !== null;
  const report = session?.report;
  // 错题强化入口只在后端支持时出现：dist 重建后前端先于后端生效（8765 后端要重启才换代码），
  // 旧后端没有「错题强化官」，露出按钮只会点了报错。
  const drillReady = personas.some((item) => item.id === "reinforce");
  // 场次记录导出（题单+对话实录+复盘+归因标注）：组装 Markdown，不经过后端。
  const exportable = Boolean(
    session && (session.question_plan || messages.length),
  );

  async function copyRecord() {
    if (!session) return;
    const bundle = await loadAttributionBundle(session.id);
    const ok = await copyText(
      buildInterviewMarkdown(session, messages, bundle),
    );
    toast(
      ok ? "面试记录已复制到剪贴板" : "复制失败，可改用「导出MD」",
      ok ? "notice" : "error",
    );
  }

  async function downloadRecord() {
    if (!session) return;
    const bundle = await loadAttributionBundle(session.id);
    const url = URL.createObjectURL(
      new Blob([buildInterviewMarkdown(session, messages, bundle)], {
        type: "text/markdown;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = interviewFileName(session);
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="interview-page">
      <aside className="interview-side">
        <div className="iv-side-modes" role="tablist" aria-label="面试模式">
          <button
            role="tab"
            aria-selected={side === "mock"}
            className={side === "mock" ? "active" : ""}
            onClick={() => setSide("mock")}
          >
            模拟面试
          </button>
          <button
            role="tab"
            aria-selected={side === "real"}
            className={side === "real" ? "active" : ""}
            onClick={() => setSide("real")}
          >
            真实复盘
          </button>
        </div>
        {side === "mock" && (
          <>
            <div className="interview-side-head">
              <span>面试场次</span>
              <button
                className="button primary small"
                onClick={() => {
                  setCreating((value) => !value);
                  setCard("");
                  setError("");
                }}
              >
                <Icon name="plus" size={14} />
                新建
              </button>
            </div>
            <div className="interview-session-list">
              {sessions.length === 0 && (
                <p className="muted body-small">
                  还没有场次。选一位面试官开一场。
                </p>
              )}
              {sessions.map((item) => (
                <div
                  key={item.id}
                  className={`interview-session-item${item.id === selected ? " active" : ""}`}
                  onClick={() => selectSession(item.id)}
                >
                  <strong>{item.title}</strong>
                  <span className="tiny-label">
                    {item.persona_name || "自定义"} · {item.round_label}
                  </span>
                  <span className={`iv-status ${item.status}`}>
                    {STATUS_NAMES[item.status] || item.status}
                    {item.has_report ? " · 有复盘" : ""}
                  </span>
                  <button
                    className="iv-session-delete"
                    aria-label="删除场次"
                    title="删除场次"
                    onClick={(event) => {
                      event.stopPropagation();
                      void removeSession(item.id);
                    }}
                  >
                    <Icon name="close" size={13} />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
        {side === "real" && (
          <>
            <div className="interview-side-head">
              <span>逐字稿</span>
              <button
                className="button primary small"
                onClick={() => {
                  setSelectedTranscript("");
                  setError("");
                }}
              >
                <Icon name="plus" size={14} />
                导入逐字稿
              </button>
            </div>
            <div className="interview-session-list">
              {transcripts.length === 0 && (
                <p className="muted body-small">
                  还没有逐字稿。把外部 ASR 转写的面试文本粘进来开始复盘。
                </p>
              )}
              {transcripts.map((item) => (
                <div
                  key={item.id}
                  className={`interview-session-item${item.id === selectedTranscript ? " active" : ""}`}
                  onClick={() => {
                    setSelectedTranscript(item.id);
                    setError("");
                  }}
                >
                  <strong>{item.job_title || "未命名岗位"}</strong>
                  <span className="tiny-label">
                    {[item.company, date(item.created_at)]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                  <span className={`iv-status ${item.status}`}>
                    {TRANSCRIPT_STATUS_NAMES[item.status] || item.status}
                    {item.has_report ? " · 有报告" : ""}
                  </span>
                  <button
                    className="iv-session-delete"
                    aria-label="删除逐字稿"
                    title="删除逐字稿"
                    onClick={(event) => {
                      event.stopPropagation();
                      void removeTranscript(item.id);
                    }}
                  >
                    <Icon name="close" size={13} />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
      </aside>
      <div className="interview-main">
        {error && (
          <div className="iv-error" role="alert">
            <span>{error}</span>
            <button
              className="iv-error-close"
              onClick={() => setError("")}
              aria-label="关闭提示"
            >
              <Icon name="close" size={14} />
            </button>
          </div>
        )}
        {side === "real" ? (
          <RealWorkspace
            transcriptId={selectedTranscript}
            modelConfigured={modelConfigured}
            onConfigureModel={onConfigureModel}
            onCreated={setSelectedTranscript}
            onChanged={refreshTranscripts}
            onDrill={drillReady ? openDrill : undefined}
          />
        ) : (
          <>
            {focusHint && (
              <div className="iv-focus-banner" role="status">
                <Icon name="target" size={16} />
                <span>
                  备战计划专项：<b>{focusHint}</b>——本场出题会定向覆盖这个主题。
                </span>
                <button
                  className="text-button"
                  onClick={() => setFocusHint("")}
                >
                  移除
                </button>
              </div>
            )}
            {(!session || creating) && (
              <CreatePanel
                cards={personas}
                styles={styles}
                card={card}
                onCard={(value) => {
                  setCard(value);
                  const preset = personas.find((item) => item.id === value);
                  setRound(preset?.default_round ?? "");
                }}
                style={style}
                onStyle={setStyle}
                round={round}
                onRound={setRound}
                intensity={intensity}
                onIntensity={setIntensity}
                questionCount={questionCount}
                onQuestionCount={setQuestionCount}
                mode={mode}
                onMode={setMode}
                jobId={jobId}
                jobLabel={jobLabel}
                onPickJob={(id, label) => {
                  setJobId(id);
                  setJobLabel(label);
                }}
                jobTitle={jobTitle}
                onJobTitle={setJobTitle}
                companyName={companyName}
                onCompanyName={setCompanyName}
                jdText={jdText}
                onJdText={setJdText}
                modelConfigured={modelConfigured}
                onConfigureModel={onConfigureModel}
                drillSelection={drillSelection}
                onDrillSelection={setDrillSelection}
                onDrillLoaded={(book) => {
                  if (!drillSelection.length)
                    setDrillSelection(defaultDrillSelection(book));
                }}
                onUseJob={(id, label) => {
                  setMode("job");
                  setJobId(id);
                  setJobLabel(label);
                }}
                busy={busy || starting}
                onStart={() => void startSession()}
                starting={starting}
              />
            )}
            {session && !creating && (
              <>
                <div className="iv-subtab-row">
                  <div
                    className="interview-subtabs"
                    role="tablist"
                    aria-label="面试分区"
                  >
                    <button
                      role="tab"
                      aria-selected={tab === "room"}
                      className={tab === "room" ? "active" : ""}
                      onClick={() => setTab("room")}
                    >
                      面试间
                    </button>
                    <button
                      role="tab"
                      aria-selected={tab === "report"}
                      className={tab === "report" ? "active" : ""}
                      onClick={() => setTab("report")}
                      disabled={!report}
                    >
                      复盘报告{report ? "" : "（结束后生成）"}
                    </button>
                    <button
                      role="tab"
                      aria-selected={tab === "attribution"}
                      className={tab === "attribution" ? "active" : ""}
                      onClick={() => setTab("attribution")}
                      disabled={!report}
                    >
                      归因标注{report ? "" : "（结束后可标注）"}
                    </button>
                  </div>
                  <div className="iv-subtabs-actions">
                    <button
                      className="button secondary small"
                      disabled={!exportable}
                      onClick={() => void copyRecord()}
                      title="复制完整面试记录（题单+对话实录+复盘）为 Markdown，可直接粘贴给其他 AI 分析"
                    >
                      复制记录
                    </button>
                    <button
                      className="button secondary small"
                      disabled={!exportable}
                      onClick={downloadRecord}
                      title="下载完整面试记录的 .md 文件"
                    >
                      导出MD
                    </button>
                  </div>
                </div>
                {tab === "room" ? (
                  <RoomPanel
                    session={session}
                    plan={plan ?? null}
                    progress={
                      progress ?? {
                        phase: "opening",
                        current_index: 0,
                        rounds: 0,
                        segments: [],
                      }
                    }
                    askedCount={askedCount}
                    messages={messages}
                    streamAnswer={stream?.answer ?? ""}
                    busy={busy}
                    awaitingReply={awaitingReply}
                    answer={answer}
                    onAnswer={setAnswer}
                    onSend={() => void send()}
                    onFinish={() => void finish()}
                    onStop={() => void stop()}
                    threadRef={threadRef}
                    reportProgress={runProgress}
                  />
                ) : tab === "attribution" ? (
                  <AttributionPanel
                    source="mock"
                    sessionId={session.id}
                    className="attr-standalone"
                    signals={(progress?.segments ?? []).flatMap(
                      (item) => item.signals ?? [],
                    )}
                  />
                ) : session.status === "reporting" ? (
                  <ProgressiveReport feed={reportFeed} />
                ) : (
                  <ReportPanel
                    report={report ?? null}
                    persona={session.persona}
                    onRegenerate={() => void finish()}
                    busy={busy}
                    onDrillQuestion={
                      drillReady
                        ? (index) =>
                            openDrill(
                              [
                                drillTargetOf(
                                  session,
                                  report?.questions[index],
                                  index,
                                ),
                              ],
                              {
                                id: session.job_id,
                                label: [session.company, session.job_title]
                                  .filter(Boolean)
                                  .join(" · "),
                              },
                            )
                        : undefined
                    }
                    onDrillWeak={
                      drillReady
                        ? () =>
                            openDrill(
                              (session.report?.questions ?? [])
                                .map((item, index) =>
                                  item.missing.length
                                    ? drillTargetOf(session, item, index)
                                    : null,
                                )
                                .filter(
                                  (ref): ref is InterviewDrillRef =>
                                    ref !== null,
                                ),
                              {
                                id: session.job_id,
                                label: [session.company, session.job_title]
                                  .filter(Boolean)
                                  .join(" · "),
                              },
                            )
                        : undefined
                    }
                  />
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
