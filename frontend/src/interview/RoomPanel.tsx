// 面试间：对话区 + 等待区 + 渐进式复盘骨架。
import { useEffect, useState } from "react";
import { Icon } from "../Icon";
import { Markdown } from "../chat/Markdown";
import type { InterviewMessage, InterviewSession } from "../api";
import type { RunProgress, ReportFeed } from "./shared";
import { STATUS_NAMES, SOURCE_NAMES } from "./shared";
// 渐进式复盘：start 事件铺全部题卡（shimmer 骨架），review 事件按题号原位填充真内容，
// summary 阶段顶部提示汇总中；done 事件无缝替换为终版报告。等待变成提前阅读。

// 出题等待区（2026-11-20）：点「开始出题」立刻跳进来接管主区——材料清单 + 真实计时，
// 表单不再原地干等。计时锚在场次 created_at 上，切场次再回来不归零。

// 复盘进度行：首次生成与「重新生成」共用（SSE progress 实时刷新）。
export function ReportStatusLine({ progress }: { progress: RunProgress }) {
  return (
    <div className="iv-state">
      {progress?.stage === "summary" ? (
        <>逐题点评完成，正在汇总维度与高危清单…</>
      ) : progress ? (
        <>
          逐题点评 {progress.done}/{progress.total}
          {progress.topic ? ` · ${progress.topic}` : ""}…
        </>
      ) : (
        <>复盘生成中：逐题点评（含好差答案对比）→ 汇总维度与高危清单…</>
      )}
      <span className="spinner" />
    </div>
  );
}
export function PlanningWait({ session }: { session: InterviewSession }) {
  const createdAt = new Date(session.created_at).getTime();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const elapsed = Number.isFinite(createdAt)
    ? Math.max(0, Math.round((now - createdAt) / 1000))
    : 0;
  const elapsedLabel =
    elapsed >= 60
      ? `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`
      : `${elapsed} 秒`;
  const material = session.material ?? {};
  const sources = [
    material.drills?.length ? `错题 ${material.drills.length} 道` : "",
    material.jd_summary ? "岗位 JD" : "",
    material.gaps?.length ? "评估缺口" : "",
    material.resume_digest
      ? material.resume_label
        ? `简历存档《${material.resume_label}》`
        : "简历"
      : "",
    material.company_brief ? "公司资料" : "",
  ].filter(Boolean);
  return (
    <div className="iv-waiting">
      <div className="iv-waiting-head">
        <span className="spinner" />
        正在出题…已等待 {elapsedLabel}
      </div>
      {sources.length > 0 && (
        <div className="iv-waiting-sources">
          出题材料已就位：
          {sources.map((item) => (
            <em key={item}>{item}</em>
          ))}
        </div>
      )}
      <p className="iv-waiting-note">
        {material.drills?.length
          ? `面试官正为 ${material.drills.length} 道错题各出一道变式题（换问法，防背答案），约 1-2 分钟。`
          : `岗位专家正按材料定制 ${session.config.question_count} 道题，约 1-3 分钟。`}
        留在本页出完自动进面试间；切去别的页面后，从左侧场次列表点开本场继续。
      </p>
    </div>
  );
}
export function RoomPanel({
  session,
  plan,
  progress,
  askedCount,
  messages,
  streamAnswer,
  busy,
  awaitingReply,
  answer,
  onAnswer,
  onSend,
  onFinish,
  onStop,
  threadRef,
  reportProgress,
}: {
  session: InterviewSession;
  plan: InterviewSession["question_plan"];
  progress: InterviewSession["progress"];
  askedCount: number;
  messages: InterviewMessage[];
  streamAnswer: string;
  busy: boolean;
  awaitingReply: boolean;
  answer: string;
  onAnswer: (value: string) => void;
  onSend: () => void;
  onFinish: () => void;
  onStop: () => void;
  threadRef: React.RefObject<HTMLDivElement | null>;
  reportProgress: RunProgress;
}) {
  const total = plan?.questions.length ?? 0;
  const ready = session.status === "ready";
  // 错题强化场次：题单里的 weakness 题标「错题重练」；失分点不进题单预览（面试中零提示，对照留给复盘）。
  const drilling = Boolean(session.material?.drills?.length);
  const sourceName = (item: { source: string }) =>
    drilling && item.source === "weakness"
      ? "错题重练"
      : SOURCE_NAMES[item.source] || "通用";
  // 首字到达前给个可见的"对方在生成"信号：请求在途看 awaitingReply，流已挂上看 busy。
  // 出题/复盘阶段各有自己的等待区，不在这里重复提示。
  const generating =
    !streamAnswer &&
    (awaitingReply || busy) &&
    (ready || session.status === "live");
  return (
    <div className="interview-room">
      <div className="interview-room-head">
        <div className="iv-room-meta">
          <strong>{session.persona.name}</strong>
          <span>
            {session.persona.round_label} · {session.persona.intensity_label}
          </span>
          {session.status === "live" && total > 0 && (
            <span className="iv-progress">
              Q{Math.min(askedCount, total)}/{total}
            </span>
          )}
          <span className={`iv-status ${session.status}`}>
            {STATUS_NAMES[session.status] || session.status}
          </span>
        </div>
        <div className="iv-room-actions">
          {busy && (
            <button className="button secondary small" onClick={onStop}>
              停止
            </button>
          )}
          {(session.status === "ready" || session.status === "live") && (
            <button
              className="button secondary small"
              disabled={busy}
              onClick={onFinish}
            >
              <Icon name="check" size={14} />
              结束并生成复盘
            </button>
          )}
        </div>
      </div>
      {session.status === "planning" && <PlanningWait session={session} />}
      {session.status === "failed" && (
        <div className="iv-state">出题失败，删除本场后重新开一场即可。</div>
      )}
      {session.status === "reporting" && (
        <ReportStatusLine progress={reportProgress} />
      )}
      <div className="interview-thread" ref={threadRef}>
        {ready && plan && (
          <div className="iv-plan">
            <p className="iv-plan-opening">{plan.opening}</p>
            <ol>
              {plan.questions.map((item, index) => (
                <li key={index}>
                  <span className="iv-topic">{item.topic}</span>
                  <span className="iv-source">{sourceName(item)}</span>
                  <p>{item.question}</p>
                </li>
              ))}
            </ol>
            <div className="iv-plan-note">
              {drilling
                ? "错题强化：每道题是一道错题的变式题，面试官会盯住你上次没答到位的地方追问（面试中不提示）；复盘时逐题对照上次。"
                : "面试官会按这份题单逐题追问（每题最多 3 层）；回答越真实，复盘越有价值。"}
            </div>
            {plan.fallback && (
              <p className="tiny-label iv-warn">
                出题模型暂时不可用，本场按原题重问——追问仍会对准上次失分点。
              </p>
            )}
            <button className="button primary" onClick={onSend}>
              <Icon name="mic" size={16} />
              开始面试
            </button>
          </div>
        )}
        {messages.map((item) => (
          <div key={item.id} className={`iv-msg ${item.role}`}>
            <span className="iv-who">
              {item.role === "assistant" ? session.persona.name : "你"}
            </span>
            {item.role === "assistant" ? (
              <div className="iv-content">
                <Markdown content={item.content} />
              </div>
            ) : (
              <div className="iv-content">{item.content}</div>
            )}
          </div>
        ))}
        {streamAnswer && (
          <div className="iv-msg assistant streaming">
            <span className="iv-who">{session.persona.name}</span>
            <div className="iv-content">
              <Markdown content={streamAnswer} />
            </div>
          </div>
        )}
        {generating && (
          <div
            className="iv-msg assistant generating"
            role="status"
            aria-live="polite"
          >
            <span className="iv-who">{session.persona.name}</span>
            <div className="iv-content">
              <span className="spinner" />
              <span>正在生成回答…</span>
              <span className="iv-typing-dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </div>
          </div>
        )}
      </div>
      {progress?.phase === "wrapup" && session.status === "live" && (
        <div className="iv-wrapup-bar">
          <span>面试官已进入反问收尾，随时可结束本场</span>
          <button
            className="button primary small"
            disabled={busy}
            onClick={onFinish}
          >
            <Icon name="check" size={14} />
            结束并生成复盘
          </button>
        </div>
      )}
      {!ready && session.status === "live" && (
        <div className="interview-composer">
          <textarea
            value={answer}
            onChange={(event) => onAnswer(event.target.value)}
            placeholder="像真面试一样回答（讲具体项目、给数据）；Ctrl+Enter 提交"
            rows={3}
            maxLength={8000}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                onSend();
              }
            }}
          />
          <div className="iv-composer-actions">
            <span className="tiny-label">面试中不剧透不点评 · 评价看复盘</span>
            <button
              className="button primary"
              disabled={busy || awaitingReply || !answer.trim()}
              onClick={onSend}
            >
              提交回答
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
export function ProgressiveReport({ feed }: { feed: ReportFeed }) {
  return (
    <div className="interview-report iv-report-live">
      <div className="iv-live-head">
        <span className="spinner" />
        <strong>复盘生成中</strong>
        {feed && (
          <span className="iv-progress">
            {feed.done}/{feed.total}
          </span>
        )}
        <span className="tiny-label">
          {feed?.stage === "summary"
            ? "汇总总评与高危清单…"
            : "逐题点评完成即展示，可先读起来"}
        </span>
      </div>
      {feed?.items.map((item) =>
        item.review ? (
          <details key={item.index} open className="iv-review filled">
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
              <Markdown content={item.review.critique} />
            </p>
            {item.review.missing?.length > 0 && (
              <p className="iv-missing">
                缺失要素：{item.review.missing.join("、")}
              </p>
            )}
            {item.review.model_answer && (
              <div className="iv-model-answer">
                <strong>高分示范</strong>
                <Markdown content={item.review.model_answer} />
              </div>
            )}
            {item.review.bad_answer_note && (
              <p className="iv-bad-answer">
                <strong>踩坑提醒</strong>
                {item.review.bad_answer_note}
              </p>
            )}
          </details>
        ) : (
          <div key={item.index} className="iv-review pending">
            <summary>
              <span className="iv-topic">{item.topic}</span>
              {item.question}
            </summary>
            {item.previous_problems?.length ? (
              <p className="iv-previous">
                上次失分：{item.previous_problems.join("、")}
              </p>
            ) : null}
            <p className="iv-pending-note">点评生成中…</p>
            <span className="iv-skeleton">
              <i />
              <i />
              <i />
            </span>
          </div>
        ),
      )}
      {!feed && <ReportStatusLine progress={null} />}
    </div>
  );
}
