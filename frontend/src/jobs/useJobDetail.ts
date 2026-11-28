import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  api,
  computeReliability,
  collectCompanySignals,
  extractCompanySignals,
  getReliability,
  getReliabilityHistory,
} from "../api";
import type {
  Evidence,
  Job,
  ReliabilityHistory,
  ReliabilityReport,
} from "../api";
import { toast } from "../toast";

// 岗位详情状态线程（2026-11-28 自 JobResearch.tsx 抽出）：详情加载（含求职参考
// 自动重算链）与选岗复位。detailRequest/activeJobId/jobGeneration 三个 ref 由组件
// 创建、与 useAnalyzeRun 共享——报告回读与事件消费的代次守卫是同一条时间线。
export function useJobDetail(deps: {
  jobGeneration: RefObject<number>;
  activeJobId: RefObject<string | null>;
  detailRequest: RefObject<number>;
  refreshReports: (jobId: string) => void;
  loadJobVersions: (jobId: string, token: number) => void;
  perform: (key: string, action: () => Promise<void>) => Promise<void>;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<{
    job: Job;
    evidence: Evidence[];
    company_evidence: Evidence[];
    evidence_total: number;
  } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const scrollToSelectedDetail = useRef(false);
  const job = detail?.job;
  const [reliability, setReliability] = useState<ReliabilityReport | null>(
    null,
  );
  const [reliabilityHistory, setReliabilityHistory] =
    useState<ReliabilityHistory | null>(null);
  // 求职参考是详情页里的慢请求：读取中与「还没生成过」必须可区分
  const [reliabilityLoading, setReliabilityLoading] = useState(false);
  const [reliabilityAction, setReliabilityAction] = useState<{
    label: string;
    status: string;
    message: string;
  } | null>(null);

  const loadDetail = useCallback(
    async (id: string) => {
      const token = ++deps.detailRequest.current;
      setDetailLoading(true);
      try {
        const data = await api<{
          job: Job;
          evidence: Evidence[];
          company_evidence: Evidence[];
          evidence_total: number;
        }>(`/jobs/${encodeURIComponent(id)}`);
        if (token === deps.detailRequest.current) {
          setDetail(data);
          deps.refreshReports(data.job.id);
          deps.loadJobVersions(data.job.id, token);
          // 先清空上一家的报告，再读这一家的：GET 走后端的 peek，查不到也只是不显示，不会写出 companies 行。
          setReliability(null);
          setReliabilityHistory(null);
          setReliabilityAction(null);
          const company = data.job.company;
          setReliabilityLoading(true);
          void Promise.allSettled([
            getReliability(company).then((result) => {
              if (
                token === deps.detailRequest.current &&
                data.job.id === deps.activeJobId.current
              )
                setReliability(result.report);
              return result.report; // 带出去给自动重算做「输入有没有变」的对照；条件不满足时 undefined 同样按「没有已存报告」处理
            }), // 没有生成过参考时这里什么都不显示；失败同样只结束 loading，不算「已生成」
            getReliabilityHistory(company).then((result) => {
              if (
                token === deps.detailRequest.current &&
                data.job.id === deps.activeJobId.current
              )
                setReliabilityHistory(result.history);
            }), // 没有历史快照时趋势区不显示
          ]).then(async (settled) => {
            // 打开详情即自动重算：打分全程是本地代码（本地统计 → 规则打分 → 措辞模板），不联网、
            // 不用模型；输入没变时信号与报告落库都是幂等写入，不产生重复历史快照。采集完「还得
            // 手动点一下生成」的点击就省在这里；「生成参考」按钮保留给手动刷新与失败重试。
            if (
              token !== deps.detailRequest.current ||
              data.job.id !== deps.activeJobId.current
            )
              return;
            const stored =
              settled[0].status === "fulfilled" ? settled[0].value : null;
            try {
              const report = await computeReliability(company);
              if (
                token !== deps.detailRequest.current ||
                data.job.id !== deps.activeJobId.current
              )
                return;
              setReliability(report);
              // 输入没变就收掉状态行，别在每次打开岗位时留常驻提示；变了才说明本地统计刷新了什么。
              const changed =
                !stored ||
                stored.input_hash !== report.input_hash ||
                stored.rubric_version !== report.rubric_version ||
                stored.wording_version !== report.wording_version;
              setReliabilityAction(
                changed && report.collection
                  ? {
                      label: "已按已存数据自动重算",
                      status: report.collection.status,
                      message: report.collection.message,
                    }
                  : null,
              );
              const history = await getReliabilityHistory(company);
              if (
                token === deps.detailRequest.current &&
                data.job.id === deps.activeJobId.current
              )
                setReliabilityHistory(history.history);
            } catch (err) {
              if (
                token !== deps.detailRequest.current ||
                data.job.id !== deps.activeJobId.current
              )
                return;
              // 自动路径失败不弹 toast：已有报告就照常显示上次结果，只留一行可行动的失败说明。
              setReliabilityAction({
                label: "自动重算未完成",
                status: "failed",
                message: `${(err as Error).message}；${stored ? "仍显示上次报告，可点「重新计算」重试。" : "可点「生成参考」重试。"}`,
              });
            } finally {
              // 首次生成期间保持「正在读取」占位；结束后才让「暂不出分 / 还没生成过」的兜底文案露面。
              if (
                token === deps.detailRequest.current &&
                data.job.id === deps.activeJobId.current
              )
                setReliabilityLoading(false);
            }
          });
        }
      } catch (err) {
        if (token === deps.detailRequest.current)
          toast((err as Error).message, "error");
      } finally {
        if (token === deps.detailRequest.current) setDetailLoading(false);
      }
    },
    [deps],
  );

  // 选岗的详情侧编排：代次推进、详情/参考复位、记忆与滚动；评估线程与 UI 状态
  // （页签/面板焦点等）由组件的 selectJob 一并处置。
  function beginSelect(id: string) {
    deps.jobGeneration.current++;
    deps.activeJobId.current = id;
    setSelectedId(id);
    setDetail(null);
    setReliability(null);
    setReliabilityHistory(null);
    setReliabilityLoading(false);
    setReliabilityAction(null);
    try {
      sessionStorage.setItem("jobscope-last-job", id);
    } catch {
      /* 存储不可用时仅本次会话生效 */
    }
    const panel = document.querySelector<HTMLElement>(".detail-panel");
    panel?.scrollTo({ top: 0 });
    scrollToSelectedDetail.current = window.matchMedia(
      "(max-width: 1040px)",
    ).matches;
    void loadDetail(id);
  }
  // 刷新回到岗位研究时接续上次选中的岗位：先验证还在库里（被删则清掉记忆），再走 selectJob 全量复位。
  function restoreLastJob(id: string) {
    void api<Job>(`/jobs/${encodeURIComponent(id)}`)
      .then(() => beginSelect(id))
      .catch(() => {
        try {
          sessionStorage.removeItem("jobscope-last-job");
        } catch {
          /* 同上 */
        }
      });
  }
  useEffect(() => {
    if (!scrollToSelectedDetail.current) return;
    const panel = document.querySelector<HTMLElement>(".detail-panel");
    panel?.scrollIntoView({ block: "start" });
    panel?.focus({ preventScroll: true });
    if (!detailLoading) scrollToSelectedDetail.current = false;
  }, [selectedId, detailLoading]);

  async function recomputeReference() {
    if (!job) return;
    const company = job.company,
      id = job.id,
      generation = deps.jobGeneration.current;
    await deps.perform("reliability", async () => {
      const result = await computeReliability(company);
      if (
        generation === deps.jobGeneration.current &&
        id === deps.activeJobId.current
      ) {
        setReliability(result);
        // 重算会自动刷新本地统计；把这段反馈亮出来，用户才知道分数用上了哪些已存数据。
        setReliabilityAction(
          result.collection
            ? {
                label: "已自动刷新本地统计",
                status: result.collection.status,
                message: result.collection.message,
              }
            : null,
        );
        try {
          const history = await getReliabilityHistory(company);
          if (
            generation === deps.jobGeneration.current &&
            id === deps.activeJobId.current
          )
            setReliabilityHistory(history.history);
        } catch {
          toast("求职参考已重算，历史记录暂时读取失败。", "error");
        }
      }
    });
  }
  async function runReferenceAction(kind: "coverage" | "extract") {
    if (!job) return;
    const company = job.company,
      id = job.id,
      generation = deps.jobGeneration.current;
    const label = kind === "coverage" ? "盘点已有资料" : "提取资料数字";
    await deps.perform("reliability", async () => {
      // 采集与抽取的 status、message 一律照后端原文显示，不改写、不美化。
      const result =
        kind === "coverage"
          ? await collectCompanySignals(company, "jobscope_jobs")
          : await extractCompanySignals(company);
      if (
        generation !== deps.jobGeneration.current ||
        id !== deps.activeJobId.current
      )
        return;
      setReliabilityAction({
        label,
        status: result.status,
        message: result.message,
      });
      if (kind === "extract" && result.status === "skipped") return;
      const report = await computeReliability(company);
      if (
        generation !== deps.jobGeneration.current ||
        id !== deps.activeJobId.current
      )
        return;
      setReliability(report);
      try {
        const history = await getReliabilityHistory(company);
        if (
          generation === deps.jobGeneration.current &&
          id === deps.activeJobId.current
        )
          setReliabilityHistory(history.history);
      } catch {
        toast("求职参考已重算，历史记录暂时读取失败。", "error");
      }
    });
  }

  return {
    selectedId,
    setSelectedId,
    detail,
    setDetail,
    job,
    detailLoading,
    reliability,
    setReliability,
    reliabilityHistory,
    setReliabilityHistory,
    reliabilityLoading,
    setReliabilityLoading,
    reliabilityAction,
    setReliabilityAction,
    loadDetail,
    beginSelect,
    restoreLastJob,
    recomputeReference,
    runReferenceAction,
  };
}
