import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  cancelAnalyzeRun,
  getAnalyzeRun,
  getJobReport,
  listJobReports,
  listResumeVersions,
  openAnalyzeStream,
  pinResumeVersion,
  startAnalyzeRun,
  updateAnalysisStatus,
} from "../api";
import type {
  Analysis,
  AnalyzeRunStart,
  Job,
  ReportSummary,
  ResumeVersionSummary,
} from "../api";
import { toast } from "../toast";
import type { AnalyzeRunState } from "../AnalysisView";

// 评估适配状态线程（2026-11-28 自 JobResearch.tsx 抽出）：流式 run 生命周期、
// 报告回读/历史、逐条改判与岗位绑定的评估简历选择条。代次守卫沿用组件传入的
// jobGeneration/activeJobId/detailRequest——切走岗位后后台 run 照常跑完落库，
// 只是不再消费事件；SSE 订阅保持命令式（代次守卫 + onerror 探状态），不套
// useRunStream（其依赖数组按 runId 重订阅，与「后跑的 run 续接同一岗位」冲突）。
export function useAnalyzeRun(deps: {
  jobGeneration: RefObject<number>;
  activeJobId: RefObject<string | null>;
  detailRequest: RefObject<number>;
  setTab: (tab: "overview" | "analysis" | "evidence") => void;
  getJob: () => Job | null;
}) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [analyzeRun, setAnalyzeRun] = useState<AnalyzeRunState | null>(null);
  const analyzeSourceRef = useRef<EventSource | null>(null);
  const analyzeActiveRef = useRef(false); // 流式 run 是否在途：后台细节刷新不能盖掉进行中的进度面板
  const [analysisHistory, setAnalysisHistory] = useState<
    ReportSummary[] | null
  >(null);
  const [activeReportId, setActiveReportId] = useState<string | null>(null);
  // 岗位绑定的存档版本：评估简历选择条的数据源（pin 的那份即「评估用」）。
  const [jobVersions, setJobVersions] = useState<ResumeVersionSummary[] | null>(
    null,
  );
  const pinnedVersion =
    (jobVersions ?? []).find((version) => version.pinned) ?? null;

  // 评估报告回读：打开岗位即拉历史摘要与最近一份完整报告（历史数据不丢，刷新/切回都能看到）。
  // 流式 run 在途时只更新历史列表，不用旧报告盖掉进度面板；done 帧会带新报告并刷新此列表。
  function refreshReports(jobId: string) {
    const token = deps.detailRequest.current;
    void listJobReports(jobId)
      .then((result) => {
        if (
          token !== deps.detailRequest.current ||
          jobId !== deps.activeJobId.current
        )
          return;
        setAnalysisHistory(result.reports);
        setActiveReportId(result.reports[0]?.id ?? null);
        if (result.latest && !analyzeActiveRef.current)
          setAnalysis(result.latest);
      })
      .catch(() => {
        /* 回读失败不影响详情展示 */
      });
  }
  // 岗位绑定的存档版本（评估简历选择条数据源）；失败静默——选择条消失即可，不挡详情。
  function loadJobVersions(jobId: string, token: number) {
    void listResumeVersions(jobId)
      .then((versions) => {
        if (
          token === deps.detailRequest.current &&
          jobId === deps.activeJobId.current
        )
          setJobVersions(versions.versions);
      })
      .catch(() => {
        if (token === deps.detailRequest.current) setJobVersions([]);
      });
  }
  async function loadHistoryReport(reportId: string) {
    const id = deps.activeJobId.current;
    if (!id) return;
    try {
      setAnalysis(await getJobReport(id, reportId));
      setActiveReportId(reportId);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }
  // 需求行手动改判：后端按岗位记忆并重算所有已存报告；这里把返回的最新报告换上。
  async function changeRequirementStatus(jdQuote: string, status: string) {
    const id = deps.activeJobId.current;
    if (!id) return;
    try {
      const result = await updateAnalysisStatus(id, jdQuote, status);
      if (result.report) setAnalysis(result.report);
      toast(result.message || "已按你的确认重算，重新评估时自动沿用。");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }
  // 评估简历切换：选存档即 pin 到当前岗位（互斥），选「我的简历」解 pin 回退全局。
  async function switchEvalResume(value: string) {
    const id = deps.activeJobId.current;
    if (!id) return;
    try {
      if (value) {
        await pinResumeVersion(Number(value), true);
        toast("已切换，该岗位评估适配将使用所选存档。");
      } else if (pinnedVersion) {
        await pinResumeVersion(pinnedVersion.id, false);
        toast("已解绑，该岗位评估适配将回退「我的简历」。");
      }
      const versions = await listResumeVersions(id);
      if (id === deps.activeJobId.current) setJobVersions(versions.versions);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }
  // 评估适配走流式运行：拆解/分组匹配/计分进度实时展示，完成时整体替换为正式报告。
  // 重复点击同一岗位时后端返回既有 run（reused），前端从头重放事件即可续接进度。
  async function analyze() {
    const target = deps.getJob();
    if (!target || analyzeRun) return;
    const id = target.id;
    const generation = deps.jobGeneration.current;
    deps.setTab("analysis");
    let started: AnalyzeRunStart;
    try {
      started = await startAnalyzeRun(id);
    } catch (err) {
      toast((err as Error).message, "error");
      return;
    }
    if (generation !== deps.jobGeneration.current) return;
    if (started.report) {
      setAnalysis(started.report);
      refreshReports(id);
      toast("适配分析完成。");
      return;
    } // 未绑定分析模型：规则分析即时返回
    setAnalyzeRun({
      id: started.run_id,
      startedAt: Date.now(),
      extract: { status: "pending", total: null },
      match: { status: "pending", done: 0, total: null },
      score: { status: "pending" },
      items: [],
    });
    analyzeActiveRef.current = true;
    attachAnalyzeStream(started.run_id);
  }
  function attachAnalyzeStream(runId: string) {
    closeAnalyzeStream();
    const generation = deps.jobGeneration.current;
    const source = openAnalyzeStream(runId, (event, data) => {
      // 仅当前代次消费事件；切走岗位后后台 run 照常执行，事件缓冲在服务端。
      if (generation !== deps.jobGeneration.current) return;
      if (event === "stage") {
        const stage = String(data.stage);
        const status: "pending" | "running" | "done" =
          data.status === "done" ? "done" : "running";
        const total = typeof data.total === "number" ? data.total : null;
        setAnalyzeRun((cur) =>
          !cur
            ? cur
            : stage === "extract"
              ? { ...cur, extract: { status, total } }
              : stage === "match"
                ? {
                    ...cur,
                    match: {
                      ...cur.match,
                      status,
                      total: total ?? cur.match.total,
                    },
                  }
                : { ...cur, score: { status } },
        );
      } else if (event === "group") {
        const items = Array.isArray(data.items)
          ? (data.items as Analysis["requirements"])
          : [];
        setAnalyzeRun((cur) =>
          !cur
            ? cur
            : {
                ...cur,
                match: {
                  ...cur.match,
                  status: "running",
                  done: cur.match.done + 1,
                  total:
                    typeof data.total === "number"
                      ? data.total
                      : cur.match.total,
                },
                items: [...cur.items, ...items],
              },
        );
      } else if (event === "done") {
        closeAnalyzeStream();
        setAnalyzeRun(null);
        analyzeActiveRef.current = false;
        const report = data as unknown as Analysis;
        setAnalysis(report);
        refreshReports(report.job_id); // 历史列表刷新，新报告成为最新一份
        toast("适配分析完成。");
      } else if (event === "error") {
        closeAnalyzeStream();
        setAnalyzeRun(null);
        analyzeActiveRef.current = false;
        toast(String(data.message || "评估适配失败"), "error");
      } else if (event === "cancelled" || event === "end") {
        closeAnalyzeStream();
        setAnalyzeRun(null);
        analyzeActiveRef.current = false;
      }
    });
    source.onerror = () => {
      // 断连由 EventSource 自动重连；但后端重启会孤儿化 run（流 404、永不再来事件），
      // 探一次状态：run 已不存在就解灰评估按钮，避免卡死。
      getAnalyzeRun(runId)
        .then((status) => {
          if (status.done) {
            closeAnalyzeStream();
            setAnalyzeRun(null);
            analyzeActiveRef.current = false;
            refreshReports(deps.activeJobId.current || "");
          }
        })
        .catch(() => {
          if (generation !== deps.jobGeneration.current) return;
          closeAnalyzeStream();
          setAnalyzeRun(null);
          analyzeActiveRef.current = false;
          toast(
            "后台评估运行已不存在（服务可能重启过），可重新评估。",
            "error",
          );
        });
    };
    analyzeSourceRef.current = source;
  }
  function closeAnalyzeStream() {
    analyzeSourceRef.current?.close();
    analyzeSourceRef.current = null;
  }
  async function cancelAnalyze() {
    if (!analyzeRun) return;
    try {
      await cancelAnalyzeRun(analyzeRun.id);
    } catch {
      /* 已结束的 run 返回 404，等 done/error 帧收尾即可 */
    }
    // 取消是合作式：保留进度面板直到 cancelled 帧到达（当前小组完成后停止）。
  }
  // 切换岗位的全量复位（selectJob 编排）：关流、清进度与报告，后台 run 继续跑完并落库。
  function resetForJobSwitch() {
    closeAnalyzeStream();
    setAnalyzeRun(null);
    analyzeActiveRef.current = false;
    setAnalysis(null);
    setAnalysisHistory(null);
    setActiveReportId(null);
    setJobVersions(null);
  }
  useEffect(() => closeAnalyzeStream, []);

  return {
    analysis,
    setAnalysis,
    analyzeRun,
    analysisHistory,
    activeReportId,
    jobVersions,
    pinnedVersion,
    refreshReports,
    loadJobVersions,
    loadHistoryReport,
    changeRequirementStatus,
    switchEvalResume,
    analyze,
    cancelAnalyze,
    resetForJobSwitch,
  };
}
