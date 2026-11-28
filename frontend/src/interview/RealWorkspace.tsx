// 真实复盘工作区：逐字稿导入 → 说话人核对 → JD 复盘报告。
import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "../Icon";
import { Markdown } from "../chat/Markdown";
import { ConfirmDialog } from "../ConfirmDialog";
import { toast } from "../toast";
import AttributionPanel from "../AttributionPanel";
import {
  cancelTranscriptRun,
  generateTranscriptReport,
  getInterviewTranscript,
  importInterviewTranscript,
  openTranscriptRunStream,
  relabelInterviewTranscript,
  saveTranscriptSegments,
} from "../api";
import type {
  InterviewDrillRef,
  Transcript,
  TranscriptReport,
  TranscriptSegment,
} from "../api";
import type { TranscriptFeedItem, TranscriptReportFeed } from "./shared";
import { SPEAKER_OPTIONS, speakerLabel, FORMAT_EXAMPLE } from "./shared";
import { JobPicker } from "./pickers";
import { ReviewDrillAction, AnalysisQuickList } from "./ReportPanel";
// 终版复盘报告：总评分/一句话总评 → 岗位匹配度 → 优点/问题两栏 → 逐题问答拆解 → 行动清单。
// 「重练此题 / 强化重练本场错题」把真实面试里答得不好的题带去模拟侧开错题强化场。

// 渐进式复盘报告：start 铺题卡骨架 → review 原位填充 → 终版无缝替换（同模拟面试复盘）。

// 计时（锚在 created_at 上）：标注等待区与出题等待区同款，切走再回来不归零。

// 标注流式实况列表：段落逐条 fade-in 点亮，贴底滚动，尾部 shimmer 表示仍在生成。
// 刷新/断线由 SSE 重放重建（start/reset 清零后逐段追加，天然幂等）。

// 标注核对页签：流式实况（标注中，段落逐条点亮）/ 失败重试 / 段落编辑器（角色下拉 + 文本框）。

export function RealWorkspace({
  transcriptId,
  modelConfigured,
  onConfigureModel,
  onCreated,
  onChanged,
  onDrill,
}: {
  transcriptId: string;
  modelConfigured: boolean;
  onConfigureModel: () => void;
  onCreated: (id: string) => void;
  onChanged: () => void;
  // 错题强化入口：带着真实复盘里的错题引用切到模拟侧开强化场（由外层 InterviewView 落地）；后端不支持时为 undefined。
  onDrill?: (
    refs: InterviewDrillRef[],
    job?: { id: string; label: string },
  ) => void;
}) {
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [error, setError] = useState("");
  const [run, setRun] = useState(""); // 挂接中的逐字稿 run id（'' = 无流）
  const [feed, setFeed] = useState<TranscriptReportFeed>(null);
  const [labelSegs, setLabelSegs] = useState<TranscriptSegment[]>([]); // 标注流式实况：段闭合即点亮
  const [tab, setTab] = useState<"labels" | "report" | "attribution">("labels");
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [confirmRelabel, setConfirmRelabel] = useState(false);
  const [jobId, setJobId] = useState("");
  const [jobLabel, setJobLabel] = useState("");
  const [text, setText] = useState("");
  const streamRef = useRef<EventSource | null>(null);
  const idRef = useRef("");
  idRef.current = transcriptId;
  // 待首装逐字稿：导入/重标后先拉进行中状态当等待区；SSE done 抢先换装后，
  // 迟到的 GET 结果不得拿旧状态把新段落覆盖回去（同模拟面试 planningRef 手法）。
  const pendingRef = useRef("");

  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    setRun("");
    setFeed(null);
  }, []);

  const loadTranscript = useCallback(async (id: string) => {
    try {
      const detail = await getInterviewTranscript(id);
      pendingRef.current = "";
      setTranscript(detail.transcript);
      setSegments(detail.transcript.segments);
      setLabelSegs([]);
      setDirty(false);
      // 后台仍在跑（切走再回来）：重连事件流续播，无需用户操作。
      if (
        detail.active_run &&
        !detail.active_run.done &&
        streamRef.current === null
      )
        attach(detail.active_run.run_id);
    } catch (err) {
      setError((err as Error).message);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const attach = useCallback(
    (runId: string) => {
      closeStream();
      setRun(runId);
      const source = openTranscriptRunStream(runId, (event, data) => {
        if (event === "start") {
          setLabelSegs([]); // 事件重放从 0 开始：先清流式实况保证重放幂等
          return;
        }
        if (event === "progress") {
          // 标注流式事件：reset 清空（模型重试前），segment 逐段点亮（重放按 start/reset 清零后追加幂等）。
          if (data.stage === "label_reset") {
            setLabelSegs([]);
            return;
          }
          if (data.stage === "label_segment" && data.segment) {
            const seg = data.segment as { speaker: string; text: string };
            setLabelSegs((current) => [
              ...current,
              {
                index: current.length,
                speaker: String(seg.speaker ?? ""),
                text: String(seg.text ?? ""),
              },
            ]);
            return;
          }
          // 复盘渐进事件三段：start 铺骨架 → review 原位填充 → summary 汇总中；重连重放按题号折叠幂等。
          if (data.stage === "start" && Array.isArray(data.items)) {
            setFeed({
              items: (data.items as Record<string, unknown>[]).map((item) => ({
                index: Number(item.index ?? 0),
                question: String(item.question ?? ""),
              })),
              done: 0,
              total: Number(data.total ?? (data.items as unknown[]).length),
              stage: "review",
            });
          } else if (data.stage === "review" && data.index != null) {
            const index = Number(data.index);
            setFeed((current) =>
              current
                ? {
                    ...current,
                    done: Number(data.done ?? current.done),
                    stage: "review",
                    items: current.items.map((item) =>
                      item.index === index
                        ? {
                            ...item,
                            review: data.review as TranscriptFeedItem["review"],
                          }
                        : item,
                    ),
                  }
                : current,
            );
          } else if (data.stage === "summary") {
            setFeed((current) =>
              current ? { ...current, stage: "summary" } : current,
            );
          }
          return;
        }
        if (event === "done") {
          closeStream();
          pendingRef.current = "";
          setLabelSegs([]);
          const next = data.transcript as Transcript | undefined;
          if (next && next.id === idRef.current) {
            setTranscript(next);
            setSegments(next.segments);
            setDirty(false);
            if (data.kind === "report") setTab("report");
          }
          onChanged();
          return;
        }
        if (event === "error") {
          closeStream();
          setError(String(data.message ?? "生成失败"));
          if (idRef.current) void loadTranscript(idRef.current);
          onChanged();
          return;
        }
        if (event === "cancelled") {
          closeStream();
          setError("已停止本次生成");
          if (idRef.current) void loadTranscript(idRef.current);
          onChanged();
          return;
        }
        if (event === "end") {
          // done 未达而流终止（服务重启等）：以服务端状态为准解锁。
          closeStream();
          if (idRef.current) void loadTranscript(idRef.current);
        }
      });
      source.onerror = () => {
        // 同对话流：致命断流（CLOSED）不会再有 end 帧，就地关流并按服务端状态解锁。
        if (source.readyState !== EventSource.CLOSED) return;
        closeStream();
        if (idRef.current) void loadTranscript(idRef.current);
        onChanged();
      };
      streamRef.current = source;
    },
    [closeStream, onChanged, loadTranscript],
  );

  // 切换选中逐字稿：清工作态并拉详情；未选中（导入面板）时清空一切。卸载收流。
  useEffect(() => {
    setError("");
    setTab("labels");
    setFeed(null);
    setLabelSegs([]);
    setText("");
    setJobId("");
    setJobLabel("");
    if (!transcriptId) {
      closeStream();
      setTranscript(null);
      setSegments([]);
      setDirty(false);
      return;
    }
    void loadTranscript(transcriptId);
    return closeStream;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcriptId]);

  async function doImport() {
    if (importing || run) return;
    const payload = text.trim();
    if (!jobId) {
      setError("请先选择本次面试对应的岗位");
      return;
    }
    if (payload.length < 50) {
      setError("逐字稿太短（至少 50 字），请确认粘贴的是面试转写文本");
      return;
    }
    setError("");
    setImporting(true);
    try {
      const started = await importInterviewTranscript(jobId, payload);
      setText("");
      setJobId("");
      setJobLabel("");
      onCreated(started.transcript_id);
      onChanged();
      if (started.run_id) {
        attach(started.run_id);
        // 立刻把标注中状态拉进来当等待区（done 事件兜底换装）。
        pendingRef.current = started.transcript_id;
        try {
          const detail = await getInterviewTranscript(started.transcript_id);
          if (pendingRef.current === started.transcript_id) {
            setTranscript(detail.transcript);
            setSegments(detail.transcript.segments);
            setDirty(false);
          }
        } catch {
          /* 拉取失败不打断：SSE done 兜底 */
        }
      } else {
        // 标准格式直读：无标注 run，切选中后 useEffect 拉详情直接进核对页签。
        toast("已按标准格式直接导入（未调用模型），请核对说话人", "notice");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setImporting(false);
    }
  }

  async function saveSegments() {
    if (!transcript || saving || run) return;
    setSaving(true);
    try {
      const saved = await saveTranscriptSegments(
        transcript.id,
        segments.map((item) => ({ speaker: item.speaker, text: item.text })),
      );
      setTranscript(saved.transcript);
      setSegments(saved.transcript.segments);
      setDirty(false);
      toast("标注已保存", "notice");
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function startReport() {
    if (!transcript || run) return;
    if (dirty) {
      setError("标注有未保存的修改，请先保存再生成报告");
      return;
    }
    try {
      const started = await generateTranscriptReport(transcript.id);
      // 乐观置 reporting：渐进视图立即可见（done 事件用服务端权威状态覆盖）。
      setTranscript((current) =>
        current ? { ...current, status: "reporting" } : current,
      );
      setTab("report");
      attach(started.run_id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function relabel() {
    if (!transcript || run) return;
    setConfirmRelabel(false);
    try {
      const started = await relabelInterviewTranscript(transcript.id);
      pendingRef.current = transcript.id;
      attach(started.run_id);
      try {
        const detail = await getInterviewTranscript(transcript.id);
        if (pendingRef.current === transcript.id) {
          setTranscript(detail.transcript);
          setSegments(detail.transcript.segments);
          setDirty(false);
        }
      } catch {
        /* 拉取失败不打断：SSE done 兜底 */
      }
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function stopRun() {
    if (!run) return;
    try {
      await cancelTranscriptRun(run);
    } catch {
      /* 服务端 cancelled 事件兜底 */
    }
  }

  const busy = run !== "";
  if (!transcriptId) {
    return (
      <section className="interview-create iv-import">
        <h2 className="sr-only">导入真实面试逐字稿</h2>
        {!modelConfigured && (
          <div className="iv-model-hint">
            未绑定分析模型：标准格式逐字稿（带候选人/面试官前缀）可直接导入，生成复盘报告前需绑定。
            <button className="inline-button" onClick={onConfigureModel}>
              去设置中心
            </button>
          </div>
        )}
        <p className="iv-import-intro">
          把外部 ASR
          工具（通义听悟、飞书妙记、讯飞等）转写的面试逐字稿粘进来：自带「候选人
          /
          面试官」前缀的标准格式直接切分入库（免模型标注），其余格式由模型按段标注说话人，逐条核对修正后生成结合岗位
          JD 的复盘报告。
        </p>
        <JobPicker
          value={jobId}
          label={jobLabel}
          onSelect={(id, label) => {
            setJobId(id);
            setJobLabel(label);
          }}
        />
        {!jobId && (
          <span className="tiny-label">
            必选：先选这次面试对应的岗位，复盘报告会结合 JD 评估回答匹配度
          </span>
        )}
        <details className="iv-format-example">
          <summary>
            标准格式示例（按此格式粘贴可跳过模型标注，直接导入）
          </summary>
          <pre>{FORMAT_EXAMPLE}</pre>
          <p className="tiny-label">
            一行一句：「我/候选人/求职者：」是你，「面试官/考官/面试官N：」是面试官，允许时间戳前缀。非匹配行并入上一段；「说话人1/2」这类认不出谁是候选人的格式仍走模型标注。
          </p>
          <button
            className="inline-button"
            onClick={() => setText(FORMAT_EXAMPLE)}
          >
            插入示例文本试一试
          </button>
        </details>
        <textarea
          className="iv-jd iv-transcript-text"
          value={text}
          rows={10}
          maxLength={40000}
          placeholder="粘贴面试逐字稿。每行带「候选人：/面试官：」前缀（见格式示例）可直接导入；无前缀或「说话人N」格式由模型标注，原文不改写不删字。"
          onChange={(event) => setText(event.target.value)}
          aria-label="逐字稿文本"
        />
        <div className="iv-create-actions">
          <button
            className="button primary"
            disabled={!jobId || text.trim().length < 50 || importing}
            onClick={() => void doImport()}
          >
            {importing ? (
              <span className="spinner" />
            ) : (
              <Icon name="resume" size={16} />
            )}
            导入并标注说话人
          </button>
          <span className="tiny-label">
            {text.length}/40000 ·{" "}
            {modelConfigured
              ? "标准格式直接导入，其余由模型标注（约 1-2 分钟）"
              : "标准格式直接导入无需模型"}
          </span>
        </div>
      </section>
    );
  }
  if (!transcript)
    return (
      <div className="iv-state">
        逐字稿加载中…
        <span className="spinner" />
      </div>
    );
  const hasReport = Boolean(transcript.report);
  const reportStale = hasReport && transcript.status !== "done"; // done 上改过标注 → 报告过期待重生成
  const jobLabelFull = [transcript.company, transcript.job_title]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="iv-real">
      <div className="iv-subtab-row">
        <div
          className="interview-subtabs"
          role="tablist"
          aria-label="逐字稿分区"
        >
          <button
            role="tab"
            aria-selected={tab === "labels"}
            className={tab === "labels" ? "active" : ""}
            onClick={() => setTab("labels")}
          >
            说话人核对
          </button>
          <button
            role="tab"
            aria-selected={tab === "report"}
            className={tab === "report" ? "active" : ""}
            onClick={() => setTab("report")}
            disabled={!hasReport}
          >
            复盘报告{hasReport ? "" : "（核对后生成）"}
          </button>
          <button
            role="tab"
            aria-selected={tab === "attribution"}
            className={tab === "attribution" ? "active" : ""}
            onClick={() => setTab("attribution")}
            disabled={!hasReport}
          >
            归因标注{hasReport ? "" : "（生成后标注）"}
          </button>
        </div>
        <div className="iv-subtabs-actions">
          {busy && (
            <button
              className="button secondary small"
              onClick={() => void stopRun()}
            >
              停止
            </button>
          )}
        </div>
      </div>
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
      {tab === "labels" ? (
        <LabelReviewPanel
          transcript={transcript}
          segments={segments}
          live={labelSegs}
          dirty={dirty}
          saving={saving}
          busy={busy}
          onSegment={(index, patch) => {
            setSegments((current) =>
              current.map((item) =>
                item.index === index ? { ...item, ...patch } : item,
              ),
            );
            setDirty(true);
          }}
          onSave={() => void saveSegments()}
          onReport={() => void startReport()}
          onRelabel={() => setConfirmRelabel(true)}
        />
      ) : tab === "attribution" ? (
        <AttributionPanel
          source="real"
          sessionId={transcript.id}
          className="attr-standalone"
          onSaved={onChanged}
        />
      ) : transcript.status === "reporting" ? (
        <TranscriptProgressiveReport feed={feed} />
      ) : (
        <TranscriptReportPanel
          report={transcript.report}
          jobLabel={jobLabelFull}
          stale={reportStale}
          onRegenerate={() => void startReport()}
          busy={busy}
          onDrillQuestion={
            onDrill
              ? (index) =>
                  onDrill(
                    [{ source: "real", session_id: transcript.id, index }],
                    { id: transcript.job_id, label: jobLabelFull },
                  )
              : undefined
          }
          onDrillWeak={
            onDrill
              ? () =>
                  onDrill(
                    (transcript.report?.questions ?? [])
                      .map((item, index) => (item.bad.length ? index : -1))
                      .filter((index) => index >= 0)
                      .map((index) => ({
                        source: "real" as const,
                        session_id: transcript.id,
                        index,
                      })),
                    { id: transcript.job_id, label: jobLabelFull },
                  )
              : undefined
          }
        />
      )}
      {confirmRelabel && (
        <ConfirmDialog
          danger
          title="重新标注"
          message="重新标注会覆盖当前段落，含已做的人工修正。"
          confirmLabel="重新标注"
          onConfirm={() => void relabel()}
          onCancel={() => setConfirmRelabel(false)}
        />
      )}
    </div>
  );
}
export function LabelReviewPanel({
  transcript,
  segments,
  live,
  dirty,
  saving,
  busy,
  onSegment,
  onSave,
  onReport,
  onRelabel,
}: {
  transcript: Transcript;
  segments: TranscriptSegment[];
  live: TranscriptSegment[];
  dirty: boolean;
  saving: boolean;
  busy: boolean;
  onSegment: (
    index: number,
    patch: { speaker?: string; text?: string },
  ) => void;
  onSave: () => void;
  onReport: () => void;
  onRelabel: () => void;
}) {
  const status = transcript.status;
  const canReport = status === "labeled" || status === "done";
  return (
    <div className="iv-label-panel">
      {status === "labeling" && (
        <div className="iv-waiting iv-labeling-live">
          <div className="iv-waiting-head">
            <span className="spinner" />
            正在标注说话人…
            <ElapsedSince createdAt={transcript.created_at} />
            {live.length > 0 && (
              <span className="iv-progress">已标 {live.length} 段</span>
            )}
          </div>
          <LabelLiveList segments={live} />
          <p className="iv-waiting-note">
            模型边输出边标注，段落逐条点亮（候选人 /
            多位面试官，原文逐字保留）。中途关闭或刷新页面都不丢：后台继续标注，回来从左侧列表点开接着看。完成后逐条核对。
          </p>
        </div>
      )}
      {status === "failed" && (
        <div className="iv-state">
          标注失败：{transcript.error || "模型返回无效"}
          <button
            className="button secondary small"
            disabled={busy}
            onClick={onRelabel}
          >
            重新标注
          </button>
        </div>
      )}
      {status === "imported" && (
        <div className="iv-state">
          尚未标注。
          <button
            className="button secondary small"
            disabled={busy}
            onClick={onRelabel}
          >
            开始标注
          </button>
        </div>
      )}
      {status !== "labeling" &&
        status !== "imported" &&
        status !== "failed" && (
          <>
            <div className="iv-label-toolbar">
              <span className="tiny-label">
                {status === "reporting"
                  ? "报告生成中，仍可修正标注（保存后需重新生成报告）"
                  : dirty
                    ? "有未保存的修改"
                    : status === "done"
                      ? "报告已生成；改完标注记得重新生成"
                      : "逐条核对说话人与文本，改完保存"}
              </span>
              <div className="iv-label-actions">
                <button
                  className="button secondary small"
                  disabled={busy || saving || !dirty}
                  onClick={onSave}
                >
                  {saving ? <span className="spinner" /> : null}保存修正
                </button>
                <button
                  className="button secondary small"
                  disabled={busy}
                  onClick={onRelabel}
                  title="重新用模型标注，会覆盖人工修正"
                >
                  重新标注
                </button>
                <button
                  className="button primary small"
                  disabled={busy || !canReport}
                  onClick={onReport}
                >
                  <Icon name="chart" size={14} />
                  生成复盘报告
                </button>
              </div>
            </div>
            {transcript.labeling_meta?.method === "format" && (
              <p className="tiny-label iv-label-note">
                本稿按标准格式直接导入（未调用模型），说话人取自原文前缀，核对无误即可生成报告。
              </p>
            )}
            <div className="iv-segments">
              {segments.map((item) => (
                <div
                  key={item.index}
                  className={`iv-segment ${item.speaker === "me" ? "me" : "other"}`}
                >
                  <select
                    value={item.speaker}
                    aria-label={`第 ${item.index + 1} 段说话人`}
                    disabled={busy}
                    onChange={(event) =>
                      onSegment(item.index, { speaker: event.target.value })
                    }
                  >
                    {SPEAKER_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {speakerLabel(option)}
                      </option>
                    ))}
                  </select>
                  <textarea
                    value={item.text}
                    rows={Math.min(
                      6,
                      Math.max(2, Math.ceil(item.text.length / 60)),
                    )}
                    disabled={busy}
                    onChange={(event) =>
                      onSegment(item.index, { text: event.target.value })
                    }
                    aria-label={`第 ${item.index + 1} 段内容`}
                  />
                </div>
              ))}
              {segments.length === 0 && (
                <p className="muted">还没有标注段落。</p>
              )}
            </div>
            <p className="tiny-label iv-label-note">
              报告以这里确认过的逐字稿为准：候选人
              =「候选人」，面试官按编号区分；同一人连续的话已合并成段。
            </p>
          </>
        )}
    </div>
  );
}
export function LabelLiveList({ segments }: { segments: TranscriptSegment[] }) {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [segments.length]);
  if (!segments.length)
    return (
      <div className="iv-live-labels iv-live-empty">
        <span className="iv-skeleton">
          <i />
          <i />
          <i />
        </span>
      </div>
    );
  return (
    <div className="iv-live-labels" ref={boxRef}>
      {segments.map((item) => (
        <div
          key={item.index}
          className={`iv-live-seg ${item.speaker === "me" ? "me" : "other"}`}
        >
          <em>{speakerLabel(item.speaker)}</em>
          <p>{item.text}</p>
        </div>
      ))}
      <div className="iv-live-tail">
        <span className="iv-skeleton">
          <i />
        </span>
      </div>
    </div>
  );
}
export function ElapsedSince({ createdAt }: { createdAt: string }) {
  const created = new Date(createdAt).getTime();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const elapsed = Number.isFinite(created)
    ? Math.max(0, Math.round((now - created) / 1000))
    : 0;
  return (
    <span>
      已等待{" "}
      {elapsed >= 60
        ? `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`
        : `${elapsed} 秒`}
    </span>
  );
}
export function TranscriptProgressiveReport({
  feed,
}: {
  feed: TranscriptReportFeed;
}) {
  return (
    <div className="interview-report iv-report-live">
      <div className="iv-live-head">
        <span className="spinner" />
        <strong>复盘报告生成中</strong>
        {feed && (
          <span className="iv-progress">
            {feed.done}/{feed.total}
          </span>
        )}
        <span className="tiny-label">
          {feed?.stage === "summary"
            ? "汇总评分与岗位匹配…"
            : "逐题点评完成即展示，可先读起来"}
        </span>
      </div>
      {feed?.items.map((item) =>
        item.review ? (
          <details key={item.index} open className="iv-review filled">
            <summary>{item.question}</summary>
            <p className="iv-critique">
              <Markdown content={item.review.critique} />
            </p>
            {item.review.good.length > 0 && (
              <p className="iv-good">亮点：{item.review.good.join("；")}</p>
            )}
            {item.review.bad.length > 0 && (
              <p className="iv-missing">问题：{item.review.bad.join("；")}</p>
            )}
            {item.review.suggestion && (
              <p className="iv-bad-answer">
                <strong>下次这样答</strong>
                {item.review.suggestion}
              </p>
            )}
          </details>
        ) : (
          <div key={item.index} className="iv-review pending">
            <summary>{item.question}</summary>
            <p className="iv-pending-note">点评生成中…</p>
            <span className="iv-skeleton">
              <i />
              <i />
              <i />
            </span>
          </div>
        ),
      )}
      {!feed && (
        <div className="iv-state">
          复盘生成中：问答拆解 → 逐题点评 → 汇总评分与岗位匹配…
          <span className="spinner" />
        </div>
      )}
    </div>
  );
}
export function TranscriptReportPanel({
  report,
  jobLabel,
  stale,
  onRegenerate,
  busy,
  onDrillQuestion,
  onDrillWeak,
}: {
  report: TranscriptReport | null;
  jobLabel: string;
  stale: boolean;
  onRegenerate: () => void;
  busy: boolean;
  onDrillQuestion?: (index: number) => void;
  onDrillWeak?: () => void;
}) {
  if (!report)
    return (
      <div className="iv-state">
        复盘报告还没有生成。回「说话人核对」页签点「生成复盘报告」。
      </div>
    );
  const match = report.jd_match ?? { fit_score: 0, evidence: [], gaps: [] };
  const weakCount = report.questions.filter((item) => item.bad?.length).length;
  return (
    <div className="interview-report">
      <section className="paper-panel">
        <div className="section-heading">
          <h2>本场总评</h2>
          <span className="tiny-label">
            {jobLabel}
            {report.reviewed ? ` · 拆解了 ${report.reviewed} 题问答` : ""}
          </span>
          <div className="iv-report-actions">
            {onDrillWeak && weakCount > 0 && (
              <button
                className="button primary small"
                onClick={onDrillWeak}
                title="把本场被点了问题的题带去「错题强化官」：换问法重考一遍，复盘逐题对照这次"
              >
                <Icon name="mic" size={14} />
                强化重练本场错题（{weakCount}）
              </button>
            )}
            <button
              className="button secondary small"
              disabled={busy}
              onClick={onRegenerate}
              title="基于当前标注重新生成复盘报告"
            >
              <Icon name="source" size={14} />
              重新生成
            </button>
          </div>
        </div>
        <p className="iv-summary">
          <span className="iv-score" data-score={report.overall_score}>
            {"★".repeat(Math.max(report.overall_score, 0))}
            {"☆".repeat(5 - Math.max(report.overall_score, 0))}
          </span>
          {report.summary}
        </p>
        {report.limitations?.length ? (
          <p className="iv-missing">{report.limitations.join("；")}</p>
        ) : null}
      </section>
      <section className="paper-panel">
        <div className="section-heading">
          <h3>岗位匹配度</h3>
          <span className="iv-score" data-score={match.fit_score}>
            {"★".repeat(Math.max(match.fit_score, 0))}
            {"☆".repeat(5 - Math.max(match.fit_score, 0))}
          </span>
        </div>
        {match.evidence.length || match.gaps.length ? (
          <div className="analysis-columns">
            <AnalysisQuickList
              title="回答中的匹配证据"
              items={match.evidence}
            />
            <AnalysisQuickList title="暴露的差距" items={match.gaps} />
          </div>
        ) : (
          <p className="muted">没有可展示的匹配证据与差距（可能未结合 JD）。</p>
        )}
      </section>
      <div className="analysis-columns">
        <AnalysisQuickList title="整体亮点" items={report.strengths} />
        <AnalysisQuickList title="整体问题" items={report.problems} />
      </div>
      <section className="paper-panel">
        <h3>逐题问答拆解</h3>
        <div className="iv-reviews">
          {report.questions.map((item, index) => (
            <details key={index} open={index === 0} className="iv-review">
              <summary>{item.question}</summary>
              <p className="iv-critique">
                <Markdown content={item.critique} />
              </p>
              {item.good.length > 0 && (
                <p className="iv-good">亮点：{item.good.join("；")}</p>
              )}
              {item.bad.length > 0 && (
                <p className="iv-missing">问题：{item.bad.join("；")}</p>
              )}
              {item.suggestion && (
                <p className="iv-bad-answer">
                  <strong>下次这样答</strong>
                  {item.suggestion}
                </p>
              )}
              {onDrillQuestion && (
                <ReviewDrillAction onClick={() => onDrillQuestion(index)} />
              )}
            </details>
          ))}
        </div>
      </section>
      <AnalysisQuickList title="下次同类面试行动清单" items={report.actions} />
      {stale && (
        <p className="tiny-label iv-warn">
          报告生成后标注有过修改：以上报告基于旧标注，重新生成以更新。
        </p>
      )}
    </div>
  );
}

// ---- 记录导出：把一场面试拼成自足的 Markdown（题单 → 对话实录 → 复盘），复制或落盘给其他 AI 复用 ----
