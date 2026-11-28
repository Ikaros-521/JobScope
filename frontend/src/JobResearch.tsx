import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  api,
  post,
  plain,
  date,
  isStale,
  relativeDays,
  patchJobs,
  startCompanyResearch,
} from "./api";
import type {
  Analysis,
  Job,
  Source,
  SourceHealth,
  Task,
  ConversationNavigation,
} from "./api";
import { toast, dismissToast, failedTaskToast, taskSucceeded } from "./toast";
import { Pagination } from "./Pagination";
import { EvidenceCard } from "./EvidenceCard";
import { CityInput } from "./CityInput";
import type { SavedLocation } from "./CityInput";
import { Button, Empty, External } from "./ui";
import { CompanyReference } from "./CompanyReference";
import { ManualJobDescription } from "./ManualJobDescription";
import { JobCard } from "./JobCard";
import { ChatWorkspace } from "./chat/ChatWorkspace";
import { JobResearchDialogs } from "./jobs/JobResearchDialogs";
import { JobEvidence } from "./jobs/JobEvidence";
import { useJobList } from "./jobs/useJobList";
import { useAnalyzeRun } from "./jobs/useAnalyzeRun";
import { useJobDetail } from "./jobs/useJobDetail";
import { jobQuery, needsFullJd, transportNames } from "./jobs/shared";
import { PipelineFollowEditor } from "./PipelineFollowEditor";
import { PIPELINE_NAMES, PIPELINE_ORDER, isDueOverdue } from "./FavoritesView";
import { AnalysisView, AnalyzeProgress } from "./AnalysisView";
import { navigateTabs } from "./tabNavigation";
import { Icon } from "./Icon";
import type { WorkshopPrefill } from "./ResumeWorkshop";
import type { SettingsTab } from "./navigation";
import type { SourcesTab } from "./SourcesView";

// 岗位研究工作区（2026-11-19 自 App.tsx 拆出）：采集检索、档案列表、岗位详情（公司情报 /
// 适配分析 / 证据资料）、岗位对话与全部岗位类弹窗。常驻挂载（hidden 切换），切换视图不丢
// 筛选与选中状态；跨视图联动经三个信号进来：openRequest（收藏/今日概览点名岗位或列表）、
// favoritesTick（收藏变动后按需重刷）、refreshTick（顶栏刷新）。
export type JobOpenRequest =
  { kind: "job"; id: string; token: number } | { kind: "list"; token: number };
export function JobResearch({
  active,
  loading,
  sources,
  locations,
  cityPinyin,
  modelConfigured,
  resumeSaved,
  sourceNames,
  sourceHealth,
  query,
  onQueryChange,
  location,
  onLocationChange,
  source,
  onSourceChange,
  refreshMetadata,
  refreshFavorites,
  onTotalChange,
  onOpenSettings,
  onConfigureModel,
  onOpenSources,
  onOpenCompanyChat,
  onOpenGlobalAgent,
  onOpenCompare,
  onOpenKnowledge,
  onOpenResume,
  onNavigateConversation,
  onConversationOpened,
  onWorkshopPrefill,
  onOpenInterview,
  openRequest,
  onOpenHandled,
  favoritesTick,
  refreshTick,
}: {
  active: boolean;
  loading: boolean;
  sources: Source[];
  locations: SavedLocation[];
  cityPinyin: Record<string, string[]>;
  modelConfigured: boolean;
  resumeSaved: boolean;
  sourceNames: Record<string, string>;
  // 来源健康度（2026-11-19）：App 在 refreshMetadata 里一并拉取；检索区按所选来源显示上次采集结果。
  sourceHealth: SourceHealth[];
  // 采集表单草稿与「数据与采集」页共享同一份（App 持有，两处受控）。
  query: string;
  onQueryChange: (value: string) => void;
  location: string;
  onLocationChange: (value: string) => void;
  source: string;
  onSourceChange: (value: string) => void;
  refreshMetadata: () => Promise<void>;
  refreshFavorites: () => Promise<void>;
  onTotalChange: (total: number) => void;
  onOpenSettings: (tab: SettingsTab) => void;
  onConfigureModel: () => void;
  onOpenSources: (tab: SourcesTab) => void;
  onOpenCompanyChat: (company: string) => void;
  onOpenGlobalAgent: (prompt: string) => void;
  onOpenCompare: () => void;
  onOpenKnowledge: () => void;
  onOpenResume: () => void;
  onOpenInterview: (jobId: string) => void;
  onNavigateConversation: (request: ConversationNavigation) => void;
  onConversationOpened: (requestId: string) => void;
  onWorkshopPrefill: (prefill: WorkshopPrefill) => void;
  openRequest: JobOpenRequest | null;
  onOpenHandled: () => void;
  favoritesTick: number;
  refreshTick: number;
}) {
  const {
    jobs,
    jobsInfo,
    jobsTotal,
    jobsLoading,
    jobFilters,
    loadJobs,
    patchLocalJob,
  } = useJobList({ onTotalChange });
  const booting = loading || jobsLoading;
  const [collectLimit, setCollectLimit] = useState(20);
  const [busy, setBusy] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [archiveDraft, setArchiveDraft] = useState({
    q: "",
    source: "",
    location: "",
    salaryMin: "",
    salaryMax: "",
    starred: false,
  });
  const [batchEdit, setBatchEdit] = useState<{ ids: string[] } | null>(null);
  const [tagsChanged, setTagsChanged] = useState(false);
  const [batchDraft, setBatchDraft] = useState({
    location: "",
    salary: "",
    tags: "",
  });
  const [confirmDelete, setConfirmDelete] = useState<{
    ids: string[];
    label: string;
  } | null>(null);
  // 跟进编辑弹窗目标（2026-11-18）：岗位详情的「跟进」按钮打开。
  const [followJob, setFollowJob] = useState<Job | null>(null);
  const [tab, setTab] = useState<"overview" | "analysis" | "evidence">(
    "overview",
  );
  const detailRequest = useRef(0);
  const activeJobId = useRef<string | null>(null);
  const jobGeneration = useRef(0);
  const {
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
    analyze: startAnalyze,
    cancelAnalyze,
    resetForJobSwitch: resetAnalysisForSwitch,
  } = useAnalyzeRun({
    jobGeneration,
    activeJobId,
    detailRequest,
    setTab,
    getJob: () => detail?.job ?? null,
  });
  const {
    selectedId,
    setSelectedId,
    detail,
    setDetail,
    reliability,
    setReliability,
    reliabilityHistory,
    setReliabilityHistory,
    reliabilityLoading,
    reliabilityAction,
    setReliabilityAction,
    detailLoading,
    loadDetail,
    beginSelect,
    restoreLastJob,
    recomputeReference,
    runReferenceAction,
  } = useJobDetail({
    jobGeneration,
    activeJobId,
    detailRequest,
    refreshReports,
    loadJobVersions,
    perform,
  });
  // 本地派生（不经 hook 返回值）：保持 `job` 与 `detail` 的别名关系，JSX 的
  // `job &&` 守卫才能把 detail 收窄成非空（解构返回值会丢掉这条类型链）。
  const job = detail?.job;
  const [evidenceFocus, setEvidenceFocus] = useState("");
  const [descExpanded, setDescExpanded] = useState(false);
  const [companyPage, setCompanyPage] = useState(0);
  const [paneFocus, setPaneFocus] = useState<"archive" | "info" | "chat">(
    "info",
  );
  const [discoveryOpen, setDiscoveryOpen] = useState(false);
  const jobScroll = useRef({ window: 0, detail: 0, list: 0 });
  const [modal, setModal] = useState<
    "job" | "evidence" | "boss-import" | "batch-edit" | "confirm-delete" | null
  >(null);
  const modalOpener = useRef<HTMLElement | null>(null);
  function openModal(
    value: "job" | "evidence" | "boss-import" | "batch-edit" | "confirm-delete",
  ) {
    modalOpener.current = document.activeElement as HTMLElement | null;
    setModal(value);
  }
  useEffect(() => {
    if (!modal) return;
    const opener = modalOpener.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
      opener?.focus();
    };
  }, [modal]);

  // ── 跨视图信号 ──
  const firstFavoritesTick = useRef(true);
  useEffect(() => {
    if (firstFavoritesTick.current) {
      firstFavoritesTick.current = false;
      return;
    }
    // 收藏变动会同时影响档案页的「只看收藏」过滤结果，按需重刷。
    if (jobFilters.current.starred)
      void perform("filter", async () => {
        await loadJobs(undefined, 1);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [favoritesTick]);
  const firstRefreshTick = useRef(true);
  useEffect(() => {
    if (firstRefreshTick.current) {
      firstRefreshTick.current = false;
      return;
    }
    void perform("refresh", async () => {
      await Promise.all([loadJobs(), refreshMetadata(), refreshFavorites()]);
      if (selectedId) await loadDetail(selectedId);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTick]);
  useEffect(() => {
    if (!openRequest) return;
    if (openRequest.kind === "job") selectJob(openRequest.id);
    else setPaneFocus("info");
    onOpenHandled();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest]);
  // 刷新回到岗位研究时接续上次选中的岗位：先验证还在库里（被删则清掉记忆），再走 selectJob 全量复位。
  useEffect(() => {
    if (!active) return;
    let last: string | null = null;
    try {
      last = sessionStorage.getItem("jobscope-last-job");
    } catch {
      /* 存储不可用忽略 */
    }
    if (!last) return;
    restoreLastJob(last);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选岗 = 评估线程复位 + 详情线程复位 + UI 状态复位（原 selectJob 的编排拆三处执行）。
  function selectJob(id: string) {
    resetAnalysisForSwitch();
    setPaneFocus("info");
    setDiscoveryOpen(false);
    setEvidenceFocus("");
    setDescExpanded(false);
    setCompanyPage(0);
    setTab("overview");
    beginSelect(id);
  }

  function openReferenceEvidence(evidenceId: string) {
    setEvidenceFocus(evidenceId);
    setTab("evidence");
  }
  async function perform(key: string, action: () => Promise<void>) {
    setBusy(key);
    try {
      await action();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy("");
    }
  }
  async function collect(event: FormEvent) {
    event.preventDefault();
    await perform("collect", async () => {
      const data = await post<{ task: Task; jobs: Job[] }>("/collect", {
        source,
        query,
        location,
        limit: collectLimit,
      });
      const name = sources.find((item) => item.id === source)?.name || source;
      if (taskSucceeded(data.task))
        toast(
          data.task.message ||
            `采集结束，新增或更新 ${data.task.count ?? data.jobs?.length ?? 0} 条资料。`,
        );
      else if (
        data.task.status === "needs_login" ||
        data.task.status === "needs_verification"
      ) {
        // 接口直连撞登录墙（boss 等）：写明发生了什么与下一步去哪，红色 20 秒长提示并给出跳转按钮，不能一闪而过。
        toast(
          `${name}需要登录或人工验证，本次未取得岗位。请到「数据与采集 → 浏览器采集」打开专用浏览器，登录后用浏览器入口采集。`,
          "error",
          false,
          { label: "去浏览器采集", onClick: () => onOpenSources("collect") },
          20000,
        );
      } else failedTaskToast(data.task, name);
      await Promise.all([loadJobs(), refreshMetadata()]);
    });
  }
  // 评估按钮禁用原因（hover 提示）：每个条件都说明人话，不再"灰了不说话"。
  function analyzeDisabledReason(j: Job): string {
    if (analyzeRun)
      return "流式分析进行中，进度见「适配分析」页签，可在进度面板取消";
    if (needsFullJd(j))
      return "该岗位只有列表摘要：BOSS/智联需先补全完整职责才能评估（岗位卡片「读取完整职责」或详情页上方「补全未完成岗位」）";
    if (busy) return "有其他任务进行中，结束后可评估";
    return "按完整JD逐项对照简历，模型辅助、代码计分";
  }
  async function collectCompany() {
    if (!job) return;
    const company = job.company;
    await perform("company", async () => {
      const started = await startCompanyResearch(company);
      // 一键完备的进度聚合到一条常驻提示：每完成一步更新计数，失败的步骤单独弹出原因。
      let done = 0;
      let progress = toast(
        `一键完备「${company}」：0/${started.tasks.length} 步完成`,
        "notice",
        true,
      );
      try {
        await Promise.all(
          started.tasks.map(async (initial) => {
            for (let poll = 0; poll < 300; poll++) {
              await new Promise((resolve) => setTimeout(resolve, 2000));
              const { task } = await api<{ task: Task }>(
                `/tasks/${encodeURIComponent(initial.id)}`,
              );
              if (task.status !== "running" && task.status !== "pending") {
                done += 1;
                dismissToast(progress);
                progress = toast(
                  `一键完备「${company}」：${done}/${started.tasks.length} 步完成`,
                  "notice",
                  true,
                );
                const name = sourceNames[task.source] || task.source;
                if (!taskSucceeded(task) && task.status !== "skipped")
                  failedTaskToast(task, name);
                return;
              }
            }
            toast(
              `${sourceNames[initial.source] || initial.source}：等待超时，请到采集记录查看`,
              "error",
            );
          }),
        );
      } finally {
        dismissToast(progress);
        if (done >= started.tasks.length)
          toast(`一键完备完成，求职参考已按最新材料重算。`, "notice", false);
        await Promise.all([
          job.id === activeJobId.current
            ? loadDetail(job.id)
            : Promise.resolve(),
          refreshMetadata(),
        ]);
      }
    });
  }
  async function importRecord(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = Object.fromEntries(form.entries());
    const mode = modal;
    await perform("import", async () => {
      if (mode === "job") {
        const result = await post<{ job: Job }>("/import/job", payload);
        await loadJobs();
        selectJob(result.job.id);
        toast("岗位已导入，来源标记为用户材料。");
      } else {
        await post("/evidence", payload);
        if (job) await loadDetail(job.id);
        toast("资料已保存。用户提供的资料仍需结合来源核实。");
      }
      setModal(null);
    });
  }
  function toggleJobSelection(id: string) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  const allPageSelected =
    jobs.length > 0 && jobs.every((item) => selectedIds.has(item.id));
  function togglePageSelection() {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (jobs.length > 0 && jobs.every((item) => next.has(item.id)))
        jobs.forEach((item) => next.delete(item.id));
      else jobs.forEach((item) => next.add(item.id));
      return next;
    });
  }
  function openBatchEdit(targets: Job[]) {
    setBatchEdit({ ids: targets.map((item) => item.id) });
    setTagsChanged(false);
    setBatchDraft(
      targets.length === 1
        ? {
            location: targets[0].location || "",
            salary: targets[0].salary || "",
            tags: (targets[0].personal_tags ?? targets[0].tags ?? []).join(
              ", ",
            ),
          }
        : { location: "", salary: "", tags: "" },
    );
    openModal("batch-edit");
  }
  function openBatchEditForSelection() {
    // 只勾了一条时预填它的现值；跨页多选则从空白开始（留空字段不改动）。
    const onPage = jobs.filter((item) => selectedIds.has(item.id));
    if (selectedIds.size === 1 && onPage.length === 1) openBatchEdit(onPage);
    else {
      setBatchEdit({ ids: [...selectedIds] });
      setBatchDraft({ location: "", salary: "", tags: "" });
      openModal("batch-edit");
    }
  }
  function askDelete(ids: string[], label: string) {
    setConfirmDelete({ ids, label });
    openModal("confirm-delete");
  }
  async function selectAllFiltered() {
    await perform("select-all", async () => {
      const data = await api<{ ids: string[]; total: number }>(
        `/jobs/ids?${jobQuery(jobFilters.current).toString()}`,
      );
      setSelectedIds(new Set(data.ids));
      toast(`已选择筛选结果全部 ${data.total} 条（含未显示的页）`);
    });
  }
  async function submitBatchEdit(event: FormEvent) {
    event.preventDefault();
    if (!batchEdit) return;
    const payload: Record<string, unknown> = { ids: batchEdit.ids };
    if (batchDraft.location.trim())
      payload.location = batchDraft.location.trim();
    if (batchDraft.salary.trim()) payload.salary = batchDraft.salary.trim();
    const tags = batchDraft.tags
      .split(/[,，]/)
      .map((tag) => tag.trim())
      .filter(Boolean);
    if (tagsChanged) payload.tags = tags;
    await perform("batch-edit", async () => {
      const result = await api<{ updated: number }>("/jobs", {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      toast(`已更新 ${result.updated} 份档案`);
      setModal(null);
      setSelectedIds(new Set());
      await Promise.all([
        loadJobs(),
        selectedId ? loadDetail(selectedId) : Promise.resolve(),
      ]);
    });
  }
  async function submitBatchDelete() {
    if (!confirmDelete) return;
    const targets = confirmDelete;
    await perform("batch-delete", async () => {
      const result = await post<{ deleted: number }>("/jobs/batch-delete", {
        ids: targets.ids,
      });
      toast(`已删除 ${result.deleted} 份岗位档案`);
      setModal(null);
      setConfirmDelete(null);
      setSelectedIds(new Set());
      if (selectedId && targets.ids.includes(selectedId)) {
        setSelectedId(null);
        setDetail(null);
        setAnalysis(null);
      }
      await Promise.all([loadJobs(), refreshMetadata()]);
    });
  }
  const evidenceTotal = detail?.evidence_total ?? 0;
  const selectedSource = sources.find((item) => item.id === source);
  const jobSources = sources.filter((item) => item.kind === "jobs");
  const hasArchiveFilters = Object.values(jobFilters.current).some(Boolean);
  const emptyLibrary =
    !booting && jobsTotal === 0 && !hasArchiveFilters && !job;
  const extraFilterCount = [
    archiveDraft.source,
    archiveDraft.location,
    archiveDraft.salaryMin || archiveDraft.salaryMax,
  ].filter(Boolean).length;
  function clearArchiveFilters() {
    const cleared = {
      q: "",
      source: "",
      location: "",
      salaryMin: "",
      salaryMax: "",
      starred: false,
    };
    setArchiveDraft(cleared);
    document
      .querySelector<HTMLInputElement>(
        ".job-archive-filters .search-input input",
      )
      ?.focus({ preventScroll: true });
    void perform("filter", async () => {
      await loadJobs(cleared, 1);
    });
  }
  // 星标切换：本地先改列表/详情状态再刷角标，PATCH 很快，无需整页重载。
  async function toggleStar(item: Job) {
    await perform("star", async () => {
      const result = await patchJobs([item.id], { starred: !item.starred });
      if (!result.updated) return;
      const starred = !item.starred;
      patchLocalJob(item.id, {
        starred,
        starred_at: starred ? new Date().toISOString() : undefined,
      });
      setDetail((current) =>
        current?.job.id === item.id
          ? {
              ...current,
              job: {
                ...current.job,
                starred,
                starred_at: starred ? new Date().toISOString() : undefined,
              },
            }
          : current,
      );
      void refreshFavorites();
      toast(starred ? "已收藏，可在收藏夹反查与横评" : "已取消收藏");
    });
  }

  // 机会管线（岗位详情快捷操作，2026-11-18）：与收藏夹同一套后端语义——设状态自动收藏，
  // 清状态连带清跟进信息；本地同步刷新列表与详情，避免再拉一次接口。
  async function setPipelineStatus(
    item: Job,
    value:
      "interested" | "applied" | "interviewing" | "offer" | "archived" | "",
  ) {
    await perform("pipeline", async () => {
      await patchJobs([item.id], { pipeline_status: value });
      const local = value
        ? {
            pipeline_status: value as Job["pipeline_status"],
            pipeline_status_changed_at: new Date().toISOString(),
          }
        : {
            pipeline_status: undefined,
            pipeline_status_changed_at: undefined,
            pipeline_note: undefined,
            pipeline_due: undefined,
          };
      patchLocalJob(item.id, local);
      setDetail((current) =>
        current?.job.id === item.id
          ? { ...current, job: { ...current.job, ...local } }
          : current,
      );
      void refreshFavorites();
      toast(value ? `已标记为「${PIPELINE_NAMES[value]}」` : "已移出管线");
    });
  }

  function applyFollowPatch(
    item: Job,
    patch: { pipeline_note?: string; pipeline_due?: string },
  ) {
    patchLocalJob(item.id, patch);
    setDetail((current) =>
      current?.job.id === item.id
        ? { ...current, job: { ...current.job, ...patch } }
        : current,
    );
  }

  // 报告缺口 → 工坊闭环（P1，2026-11-19）：未满足/部分满足的要求与缺口清单组装成优化重点
  // 说明，走既有 prefill 链路（自动选中岗位 + 预填说明），优化产出存档后可回评对比。
  function optimizeFromGaps(report: Analysis) {
    if (!job) return;
    const problemRequirements = report.requirements
      .filter((item) => item.status === "unmet" || item.status === "partial")
      .map((item) => String(item.requirement || "").trim())
      .filter(Boolean);
    const gaps = (report.gaps || []).map((item) => plain(item)).filter(Boolean);
    const lines: string[] = [];
    if (problemRequirements.length)
      lines.push(
        `优先补强以下未满足/部分满足的岗位要求：\n- ${problemRequirements.join("\n- ")}`,
      );
    if (gaps.length) lines.push(`评估指出的缺口：\n- ${gaps.join("\n- ")}`);
    // instructions 后端上限 2000 字符，超长截断而不是报错打回。
    onWorkshopPrefill({
      jobId: job.id,
      company: job.company,
      jobTitle: job.title,
      instructions: lines.join("\n\n").slice(0, 2000),
    });
  }
  async function setStarredSelected(starred: boolean) {
    await perform("star-batch", async () => {
      const result = await patchJobs([...selectedIds], { starred });
      toast(`${starred ? "已收藏" : "已取消收藏"} ${result.updated} 份岗位`);
      setSelectedIds(new Set());
      await loadJobs(undefined, jobsInfo.page);
      if (selectedId) await loadDetail(selectedId);
      void refreshFavorites();
    });
  }
  function openJobChat() {
    jobScroll.current = {
      window: window.scrollY,
      detail: document.querySelector(".detail-panel")?.scrollTop || 0,
      list: document.querySelector(".job-list-panel")?.scrollTop || 0,
    };
    setPaneFocus("chat");
  }
  function returnToJob() {
    setPaneFocus("info");
    requestAnimationFrame(() => {
      window.scrollTo({ top: jobScroll.current.window, behavior: "instant" });
      const detailPane = document.querySelector(".detail-panel");
      const listPane = document.querySelector(".job-list-panel");
      if (detailPane) detailPane.scrollTop = jobScroll.current.detail;
      if (listPane) listPane.scrollTop = jobScroll.current.list;
    });
  }

  return (
    <>
      <div className="jobs-view" hidden={!active}>
        <section className="page-intro compact">
          <h1 className="sr-only">岗位研究</h1>
        </section>
        {paneFocus !== "chat" && (
          <div className="job-entry-actions">
            <button
              className="button primary"
              onClick={() => openModal("boss-import")}
              title="捕获项目浏览器里看到的 Boss 页面、粘贴分享链接，批量导入岗位"
            >
              从 Boss 导入
            </button>
            <button
              className="button secondary"
              onClick={() => openModal("job")}
            >
              粘贴已有岗位 JD
            </button>
            <button
              className="button secondary"
              onClick={() => setDiscoveryOpen(!discoveryOpen)}
              aria-expanded={discoveryOpen}
            >
              查找外部岗位
            </button>
          </div>
        )}
        <section
          className="search-section"
          aria-labelledby="collect-heading"
          hidden={!discoveryOpen || paneFocus === "chat"}
        >
          <div className="search-section-heading">
            <h2 id="collect-heading">
              <Icon name="globe" size={16} />
              发现新岗位
            </h2>
            <button
              className="text-button"
              onClick={() => onOpenSources("collect")}
            >
              管理数据源
              <Icon name="arrow" size={13} />
            </button>
          </div>
          <form className="search-form" onSubmit={collect}>
            <label className="collect-field collect-keyword">
              <span>岗位关键词</span>
              <span className="collect-control">
                <Icon name="search" size={17} />
                <input
                  value={query}
                  onChange={(event) => onQueryChange(event.target.value)}
                  placeholder="岗位、技能或公司"
                  required
                  maxLength={200}
                />
              </span>
            </label>
            <div className="collect-field collect-location">
              <label htmlFor="collect-location">工作地点</label>
              <div className="collect-control">
                <Icon name="pin" size={17} />
                <CityInput
                  id="collect-location"
                  cities={selectedSource?.cities || []}
                  hot={selectedSource?.hot || []}
                  pinyin={cityPinyin}
                  locations={locations}
                  value={location}
                  onChange={onLocationChange}
                  placeholder="不限地点"
                  maxLength={100}
                />
              </div>
            </div>
            <label className="collect-field collect-source">
              <span>岗位数据源</span>
              <select
                value={source}
                onChange={(event) => onSourceChange(event.target.value)}
              >
                {jobSources.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ·{" "}
                    {transportNames[item.transport_mode] || item.transport_mode}
                  </option>
                ))}
              </select>
            </label>
            <label className="collect-field collect-count">
              <span>采集数量</span>
              <span className="collect-control">
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={collectLimit}
                  onChange={(event) =>
                    setCollectLimit(
                      Math.max(
                        1,
                        Math.min(1000, Number(event.target.value) || 20),
                      ),
                    )
                  }
                  inputMode="numeric"
                  aria-label="采集数量"
                />
                <span>条</span>
              </span>
            </label>
            <Button
              className="button primary"
              type="submit"
              busy={busy === "collect"}
              disabled={Boolean(busy)}
            >
              检索岗位
              <Icon name="arrow" size={17} />
            </Button>
          </form>
          {/* 需要登录的平台在提交前就亮明前置条件（独立元素，避免被矮屏下 footnote 的折叠规则藏掉）。 */}
          {selectedSource?.login === "required" && (
            <p className="collect-login-note" role="note">
              「{selectedSource.name}
              」需要登录后才能返回岗位：请先到「数据与采集 →
              浏览器采集」打开专用浏览器并登录，再回到这里用浏览器入口采集；未登录直接检索只会得到登录或验证提示。
            </p>
          )}
          <div className="search-footnote">
            <span>
              <span className="small-dot" />
              {selectedSource?.description ||
                "标注“需浏览器”的来源，需要先在项目 Chrome 中登录。"}
            </span>
          </div>
          {/* 采集前健康预检（2026-11-19）：上次采集的真实结果在提交前就亮出来——被登录墙挡过、
            连续失败时先去处理，而不是点了才知道；只陈述事实与补法，不给诊断结论。 */}
          {(() => {
            const health = sourceHealth.find((item) => item.source === source);
            if (!health || !health.last_run_at) return null;
            const warn = health.last_status !== "completed";
            const statusText =
              {
                completed: "成功",
                needs_login: "被登录墙挡下",
                needs_verification: "需要人工验证",
                failed: "失败",
                backoff: "退避等待中",
                running: "进行中",
                pending: "等待中",
              }[health.last_status] || health.last_status;
            return (
              <p
                className={`source-health-line${warn ? " warn" : ""}`}
                role="note"
                title={health.last_message || undefined}
              >
                上次采集 {statusText} ·{" "}
                {relativeDays(health.last_run_at, date(health.last_run_at))}
                {health.failed ? ` · 近期失败 ${health.failed} 次` : ""}
                {warn ? "。建议先处理再采集：" : ""}
                {warn && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => onOpenSources("health")}
                  >
                    看来源健康
                    <Icon name="arrow" size={12} />
                  </button>
                )}
              </p>
            );
          })()}
        </section>

        {job && paneFocus !== "chat" && (
          <div className="job-reading-actions">
            <span>{job.title}</span>
            <button className="button primary small" onClick={openJobChat}>
              问这份岗位
            </button>
          </div>
        )}
        {!emptyLibrary && (
          <div
            className={`research-layout focus-${paneFocus}${job ? "" : " no-chat"}`}
          >
            <section
              className="job-list-panel"
              aria-labelledby="archive-heading"
            >
              <div className="panel-title">
                <h2 id="archive-heading">
                  岗位档案 <span>{jobsInfo.total.toLocaleString()}</span>
                </h2>
                {hasArchiveFilters && (
                  <button
                    className="text-button archive-reset"
                    disabled={Boolean(busy)}
                    onClick={clearArchiveFilters}
                  >
                    清除筛选
                  </button>
                )}
              </div>
              <form
                className="job-archive-filters"
                aria-label="岗位档案筛选"
                onSubmit={(event) => {
                  event.preventDefault();
                  void perform("filter", async () => {
                    await loadJobs({ ...archiveDraft }, 1);
                  });
                }}
              >
                <label className="search-input">
                  <Icon name="search" size={16} />
                  <span className="sr-only">档案关键词</span>
                  <input
                    value={archiveDraft.q}
                    onChange={(event) =>
                      setArchiveDraft({
                        ...archiveDraft,
                        q: event.target.value,
                      })
                    }
                    placeholder="在已存档案中搜索"
                    maxLength={300}
                  />
                </label>
                <Button
                  className="button secondary small archive-submit"
                  type="submit"
                  busy={busy === "filter"}
                  disabled={Boolean(busy)}
                >
                  筛选
                </Button>
                <label
                  className="agent-checkbox archive-starred"
                  title="只显示收藏过的岗位；收藏不会因重新采集而丢失"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(archiveDraft.starred)}
                    onChange={(event) => {
                      const next = {
                        ...archiveDraft,
                        starred: event.target.checked,
                      };
                      setArchiveDraft(next);
                      void perform("filter", async () => {
                        await loadJobs(next, 1);
                      });
                    }}
                  />
                  只看收藏
                </label>
                <details className="archive-filter-options">
                  <summary>
                    <Icon name="filter" size={16} />
                    筛选条件
                    {extraFilterCount > 0 && (
                      <span className="filter-count">{extraFilterCount}</span>
                    )}
                    <Icon name="chevron" size={14} />
                  </summary>
                  <div className="archive-filter-fields">
                    <label className="archive-field">
                      <span>信息源</span>
                      <select
                        value={archiveDraft.source}
                        onChange={(event) =>
                          setArchiveDraft({
                            ...archiveDraft,
                            source: event.target.value,
                          })
                        }
                      >
                        <option value="">全部来源</option>
                        {jobSources.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                        <option value="user_import">用户导入</option>
                      </select>
                    </label>
                    <div className="archive-field">
                      <label htmlFor="archive-location">地点</label>
                      <CityInput
                        id="archive-location"
                        cities={[]}
                        hot={[]}
                        pinyin={cityPinyin}
                        locations={locations}
                        value={archiveDraft.location}
                        onChange={(value) =>
                          setArchiveDraft({ ...archiveDraft, location: value })
                        }
                        placeholder="不限地点"
                        maxLength={100}
                      />
                    </div>
                    <div className="archive-field archive-salary">
                      <span>
                        月薪范围 <span className="muted">· K/月</span>
                      </span>
                      <div className="salary-range">
                        <input
                          aria-label="月薪下限"
                          type="number"
                          min={0}
                          step="0.5"
                          placeholder="最低"
                          inputMode="decimal"
                          value={archiveDraft.salaryMin}
                          onChange={(event) =>
                            setArchiveDraft({
                              ...archiveDraft,
                              salaryMin: event.target.value,
                            })
                          }
                        />
                        <span>–</span>
                        <input
                          aria-label="月薪上限"
                          type="number"
                          min={0}
                          step="0.5"
                          placeholder="最高"
                          inputMode="decimal"
                          value={archiveDraft.salaryMax}
                          onChange={(event) =>
                            setArchiveDraft({
                              ...archiveDraft,
                              salaryMax: event.target.value,
                            })
                          }
                        />
                      </div>
                    </div>
                  </div>
                  <div className="filter-actions">
                    <button
                      type="button"
                      className="text-button"
                      disabled={Boolean(busy)}
                      onClick={clearArchiveFilters}
                    >
                      清除
                    </button>
                    <Button
                      className="button secondary small"
                      type="submit"
                      busy={busy === "filter"}
                      disabled={Boolean(busy)}
                    >
                      应用条件
                    </Button>
                  </div>
                </details>
              </form>
              {jobs.length > 0 && (
                <div className="job-select-bar">
                  <label className="agent-checkbox">
                    <input
                      type="checkbox"
                      checked={allPageSelected}
                      onChange={togglePageSelection}
                    />
                    本页全选
                  </label>
                  <button
                    className="text-button"
                    disabled={Boolean(busy)}
                    onClick={() => void selectAllFiltered()}
                  >
                    选择全部 {jobsInfo.total} 条
                  </button>
                  {selectedIds.size > 0 && (
                    <div className="selection-actions">
                      <span>已选 {selectedIds.size} 项</span>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        onClick={() => void setStarredSelected(true)}
                      >
                        收藏
                      </Button>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        onClick={() => void setStarredSelected(false)}
                      >
                        取消收藏
                      </Button>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        onClick={openBatchEditForSelection}
                      >
                        编辑
                      </Button>
                      <Button
                        className="button secondary small danger-text"
                        disabled={Boolean(busy)}
                        onClick={() =>
                          askDelete(
                            [...selectedIds],
                            `已选的 ${selectedIds.size} 份岗位档案`,
                          )
                        }
                      >
                        删除
                      </Button>
                      <button
                        className="text-button"
                        onClick={() => setSelectedIds(new Set())}
                      >
                        取消
                      </button>
                    </div>
                  )}
                </div>
              )}
              {/* 首次加载给骨架行占位（2026-11-28）：先看到卡片轮廓再上真数据，不给白屏转圈。 */}
              {booting ? (
                <div className="skeleton-list" aria-hidden="true">
                  {Array.from({ length: 6 }, (_, index) => (
                    <div key={index} className="skeleton-row">
                      <span className="skeleton skeleton-line" />
                      <span className="skeleton skeleton-line" />
                      <span className="skeleton skeleton-line" />
                    </div>
                  ))}
                </div>
              ) : jobs.length === 0 ? (
                <Empty
                  title={
                    hasArchiveFilters ? "没有符合条件的岗位" : "从一个机会开始"
                  }
                  icon="search"
                >
                  {hasArchiveFilters ? (
                    <>
                      试试其他关键词，或
                      <button
                        className="inline-button"
                        disabled={Boolean(busy)}
                        onClick={clearArchiveFilters}
                      >
                        清除筛选
                      </button>
                      查看全部档案。
                    </>
                  ) : (
                    "检索真实岗位，或导入你正在考虑的职位。采集后的岗位会保存在这里。"
                  )}
                </Empty>
              ) : (
                <div
                  className={`job-list${busy === "filter" || busy === "page-jobs" ? " refreshing" : ""}`}
                >
                  {jobs.map((item) => (
                    <JobCard
                      key={item.id}
                      item={item}
                      selected={selectedId === item.id}
                      checked={selectedIds.has(item.id)}
                      onToggleSelection={toggleJobSelection}
                      onOpen={selectJob}
                      onToggleStar={(item) => void toggleStar(item)}
                    />
                  ))}
                </div>
              )}
              <Pagination
                info={jobsInfo}
                label="岗位档案"
                busy={Boolean(busy)}
                onPage={(page) =>
                  void perform("page-jobs", async () => {
                    await loadJobs(undefined, page);
                  })
                }
              />
            </section>

            <section
              className="detail-panel"
              aria-label="岗位详情"
              tabIndex={-1}
            >
              {selectedId && (
                <button
                  className="mobile-back text-button"
                  onClick={() => {
                    document
                      .querySelector(".job-list-panel")
                      ?.scrollIntoView({ block: "start" });
                    document
                      .querySelector<HTMLElement>(
                        '.job-card-body[aria-pressed="true"]',
                      )
                      ?.focus({ preventScroll: true });
                  }}
                >
                  <Icon name="arrow" size={16} />
                  返回岗位列表
                </button>
              )}
              {detailLoading ? (
                <div className="loading-state">
                  <span className="spinner" />
                  正在读取岗位档案…
                </div>
              ) : !job ? (
                <div className="welcome-panel">
                  <h2>先选一份岗位，再逐项核对</h2>
                  <p>选择左侧档案，查看要求、公司资料与简历依据。</p>
                </div>
              ) : (
                <>
                  <div className="detail-heading">
                    <div className="detail-kicker">
                      <span>{sourceNames[job.source] || job.source}</span>
                      <External url={job.url}>查看原始岗位</External>
                    </div>
                    <h2>{job.title}</h2>
                    <div className="detail-company">
                      {job.company}
                      <span>·</span>
                      <Icon name="pin" size={14} />
                      {job.location || "地点待确认"}
                    </div>
                    <div className="detail-heading-bottom">
                      <strong>{job.salary || "薪资未披露"}</strong>
                      {isStale(job.updated_at, 30) && (
                        <span
                          className="stale-hint"
                          title="这份档案已超过 30 天未更新，薪资与职责可能已变化；可在收藏夹「反查」重读源站，或按原条件重新采集。"
                        >
                          数据较旧 · 建议反查
                        </span>
                      )}
                      {job.pipeline_due && (
                        <span
                          className={`follow-due${isDueOverdue(job.pipeline_due) ? " overdue" : ""}`}
                          title="跟进截止日；到期待办出现在「今日概览」"
                        >
                          截止 {job.pipeline_due}
                          {isDueOverdue(job.pipeline_due) ? "（逾期）" : ""}
                        </span>
                      )}
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        title={
                          job.starred
                            ? "取消收藏；收藏不会因重新采集而丢失"
                            : "收藏岗位，可在收藏夹反查与横评"
                        }
                        onClick={() => job && void toggleStar(job)}
                      >
                        <Icon name="star" size={15} />
                        {job.starred ? "已收藏" : "收藏"}
                      </Button>
                      <select
                        className="pipeline-select detail-pipeline-select"
                        value={job.pipeline_status || ""}
                        disabled={Boolean(busy)}
                        aria-label="机会管线状态"
                        title="机会管线状态：标记投递进展；给未收藏的岗位设状态会自动收藏"
                        onChange={(event) =>
                          void setPipelineStatus(
                            job,
                            event.target.value as
                              | "interested"
                              | "applied"
                              | "interviewing"
                              | "offer"
                              | "archived"
                              | "",
                          )
                        }
                      >
                        <option value="">未跟踪</option>
                        {PIPELINE_ORDER.map((status) => (
                          <option key={status} value={status}>
                            {PIPELINE_NAMES[status]}
                          </option>
                        ))}
                      </select>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        title="设置下一步行动与截止日；到期的会出现在「今日概览」待办"
                        onClick={() => setFollowJob(job)}
                      >
                        跟进
                        {job.pipeline_due
                          ? `（${job.pipeline_due.slice(5).replace("-", "/")}）`
                          : ""}
                      </Button>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        onClick={() => openBatchEdit([job])}
                      >
                        编辑档案
                      </Button>
                      <Button
                        className="button secondary small"
                        busy={busy === "analyze" || Boolean(analyzeRun)}
                        title={analyzeDisabledReason(job)}
                        disabled={
                          Boolean(busy) ||
                          needsFullJd(job) ||
                          Boolean(analyzeRun)
                        }
                        onClick={() => void startAnalyze()}
                      >
                        <Icon name="check" size={16} />
                        评估适配
                      </Button>
                      <Button
                        className="button secondary small"
                        disabled={Boolean(busy)}
                        onClick={() =>
                          onWorkshopPrefill({
                            jobId: job.id,
                            company: job.company,
                            jobTitle: job.title,
                          })
                        }
                      >
                        <Icon name="wand" size={16} />
                        优化简历
                      </Button>
                      <Button
                        className="button secondary small"
                        title="用 AI 面试官按这个岗位模拟一轮面试；评估缺口会变成追问重点"
                        onClick={() => onOpenInterview(job.id)}
                      >
                        <Icon name="mic" size={16} />
                        模拟面试
                      </Button>
                    </div>
                  </div>
                  <ManualJobDescription
                    key={job.id}
                    job={job}
                    onSaved={async (saved) => {
                      if (activeJobId.current === saved.id) {
                        await loadDetail(saved.id);
                        setAnalysis(null);
                      }
                      await loadJobs();
                    }}
                  />
                  <div className="jd-state">
                    <div>
                      <strong>
                        {job.description_status === "complete"
                          ? "完整JD已保存"
                          : needsFullJd(job)
                            ? "仅列表摘要 · 待补全"
                            : "岗位材料已保存"}
                      </strong>
                      <p>
                        {job.detail_collected_at
                          ? `正文读取于 ${date(job.detail_collected_at)}`
                          : needsFullJd(job)
                            ? job.source === "boss" || job.source === "zhaopin"
                              ? "需要完整职责与任职要求，才能评估是否适合投递"
                              : "该平台列表未给出完整职责，可在岗位档案补充后再评估"
                            : "请核对岗位材料完整性及原始出处"}
                      </p>
                    </div>
                    {(job.source === "boss" || job.source === "zhaopin") && (
                      <Button
                        className="button secondary small"
                        busy={busy === "job-detail"}
                        disabled={Boolean(busy)}
                        onClick={() =>
                          void perform("job-detail", async () => {
                            const id = job.id,
                              generation = jobGeneration.current;
                            const result = await post<{ task: Task; job: Job }>(
                              `/jobs/${encodeURIComponent(id)}/detail`,
                            );
                            if (taskSucceeded(result.task))
                              toast(result.task.message);
                            else failedTaskToast(result.task, "完整职责读取");
                            await Promise.all([
                              generation === jobGeneration.current
                                ? loadDetail(id)
                                : Promise.resolve(),
                              loadJobs(),
                              refreshMetadata(),
                            ]);
                            if (generation === jobGeneration.current)
                              setAnalysis(null);
                          })
                        }
                      >
                        {job.description_status === "complete"
                          ? "更新完整职责"
                          : "读取完整职责"}
                      </Button>
                    )}
                  </div>
                  <div
                    className="detail-tabs"
                    role="tablist"
                    aria-label="岗位档案内容"
                    onKeyDown={navigateTabs}
                  >
                    {(["overview", "analysis", "evidence"] as const).map(
                      (item) => (
                        <button
                          key={item}
                          id={`job-${item}-tab`}
                          role="tab"
                          aria-controls="job-detail-panel"
                          tabIndex={tab === item ? 0 : -1}
                          aria-selected={tab === item}
                          className={tab === item ? "active" : ""}
                          onClick={() => setTab(item)}
                        >
                          {item === "overview"
                            ? "岗位与公司"
                            : item === "analysis"
                              ? "适配分析"
                              : "证据资料"}
                          {item === "evidence" && <span>{evidenceTotal}</span>}
                        </button>
                      ),
                    )}
                  </div>
                  <div
                    id="job-detail-panel"
                    className="detail-content"
                    role="tabpanel"
                    aria-labelledby={`job-${tab}-tab`}
                  >
                    {tab === "overview" && (
                      <>
                        <div
                          className={`description-text${descExpanded ? "" : " collapsed"}`}
                        >
                          {plain(job.description) ||
                            "尚无岗位描述，请补充原始岗位资料。"}
                        </div>
                        {job.description.length > 600 && (
                          <button
                            className="text-button"
                            onClick={() => setDescExpanded((value) => !value)}
                          >
                            {descExpanded ? "收起描述" : "展开全文"}
                          </button>
                        )}
                        <div className="company-section">
                          <div className="section-heading">
                            <div>
                              <h3>了解 {job.company}</h3>
                            </div>
                            <span className="purpose-heading-actions">
                              <Button
                                className="button secondary small"
                                title="围绕这家公司已保存的全部岗位与资料发起对话（在公司资料库的「公司对话」页打开）"
                                onClick={() => onOpenCompanyChat(job.company)}
                              >
                                公司对话
                                <Icon name="chat" size={14} />
                              </Button>
                              <Button
                                className="button secondary small"
                                title="一键串完单岗完整参考路径：多平台检索岗位与公告 → 快查公开页 → 补读完整职责 → 采集已启用信号源 → 抽取材料数字 → AI 行业判断 → 重算报告；官方页面和付费 API 可在下方「按来源补数据」中选择，遇登录/验证会停下来等你处理"
                                busy={busy === "company"}
                                disabled={Boolean(busy)}
                                onClick={() => void collectCompany()}
                              >
                                一键完备
                                <Icon name="arrow" size={14} />
                              </Button>
                            </span>
                          </div>
                          {detail.company_evidence?.length ? (
                            <>
                              {detail.company_evidence.length > 4 && (
                                <div className="company-pager">
                                  <button
                                    className="text-button"
                                    disabled={companyPage === 0}
                                    onClick={() =>
                                      setCompanyPage((page) => page - 1)
                                    }
                                  >
                                    上一页
                                  </button>
                                  <span className="tiny-label">
                                    {companyPage + 1} /{" "}
                                    {Math.ceil(
                                      detail.company_evidence.length / 4,
                                    )}{" "}
                                    页 · 共 {detail.company_evidence.length} 条
                                  </span>
                                  <button
                                    className="text-button"
                                    disabled={
                                      companyPage >=
                                      Math.ceil(
                                        detail.company_evidence.length / 4,
                                      ) -
                                        1
                                    }
                                    onClick={() =>
                                      setCompanyPage((page) => page + 1)
                                    }
                                  >
                                    下一页
                                  </button>
                                </div>
                              )}
                              <div className="company-evidence-list">
                                {detail.company_evidence
                                  .slice(companyPage * 4, companyPage * 4 + 4)
                                  .map((e) => (
                                    <EvidenceCard key={e.id} evidence={e} />
                                  ))}
                              </div>
                            </>
                          ) : (
                            <div className="inline-empty">
                              尚未收集到该公司的资料。你可以多平台检索公开岗位与公告，或补充已有材料。
                            </div>
                          )}
                          <button
                            className="text-button"
                            onClick={() => openModal("evidence")}
                          >
                            <Icon name="plus" size={14} />
                            补充公司资料
                          </button>
                          <CompanyReference
                            key={job.id}
                            onSignalBusy={(value) =>
                              setBusy(value ? "reliability" : "")
                            }
                            onSignalCollected={(report, history) => {
                              if (job.id !== activeJobId.current) return;
                              setReliability(report);
                              setReliabilityHistory(history);
                              setReliabilityAction(null);
                              void refreshMetadata();
                            }}
                            onConfigureSources={() => onOpenSources("catalog")}
                            onAgentCollect={onOpenGlobalAgent}
                            report={reliability}
                            history={reliabilityHistory}
                            busy={Boolean(busy)}
                            loading={reliabilityLoading}
                            action={reliabilityAction}
                            company={job.company}
                            onOpenCompare={onOpenCompare}
                            onCompute={() => void recomputeReference()}
                            onCoverage={() =>
                              void runReferenceAction("coverage")
                            }
                            onExtract={() => void runReferenceAction("extract")}
                            onOpenEvidence={openReferenceEvidence}
                            onOpenEvidenceModal={() => openModal("evidence")}
                          />
                        </div>
                      </>
                    )}
                    {tab === "analysis" &&
                      jobVersions &&
                      jobVersions.length > 0 && (
                        <div className="eval-resume-bar">
                          <span className="analysis-history-label">
                            评估简历
                          </span>
                          <select
                            value={pinnedVersion?.id ?? ""}
                            aria-label="选择评估用简历"
                            onChange={(event) =>
                              void switchEvalResume(event.target.value)
                            }
                          >
                            <option value="">我的简历（全局）</option>
                            {jobVersions.map((version) => (
                              <option key={version.id} value={version.id}>
                                存档《{version.title}》
                              </option>
                            ))}
                          </select>
                          <span className="tiny-label">
                            评估适配自动使用所选简历；工坊针对该岗位的新产出会自动接管
                          </span>
                        </div>
                      )}
                    {tab === "analysis" &&
                      (!analysis ? (
                        analyzeRun ? (
                          <AnalyzeProgress
                            run={analyzeRun}
                            onCancel={() => void cancelAnalyze()}
                          />
                        ) : (
                          <Empty
                            title={
                              busy === "analyze"
                                ? "正在对应岗位要求…"
                                : "让经历与要求逐项对照"
                            }
                            icon="resume"
                          >
                            {busy === "analyze" ? (
                              "分析完成后，将展示匹配依据、未知项与面试核实问题。"
                            ) : (
                              <>
                                {resumeSaved ? (
                                  "点击“评估适配”，查看有依据的匹配与缺口。"
                                ) : (
                                  <>
                                    先在
                                    <button
                                      className="inline-button"
                                      onClick={onOpenResume}
                                    >
                                      我的简历
                                    </button>
                                    保存个人经历，再生成适配分析。
                                  </>
                                )}
                                分数仅供辅助比较，不代表录用概率。
                              </>
                            )}
                          </Empty>
                        )
                      ) : (
                        <>
                          {analysisHistory && analysisHistory.length > 1 && (
                            <div className="analysis-history">
                              <span className="analysis-history-label">
                                历史评估
                              </span>
                              <div className="analysis-history-list">
                                {analysisHistory.map((item) => (
                                  <button
                                    key={item.id}
                                    className={
                                      item.id === activeReportId ? "active" : ""
                                    }
                                    title={
                                      item.method === "rule_assisted"
                                        ? "规则分析（未绑定分析模型时自动回退）"
                                        : "模型辅助语义分析"
                                    }
                                    onClick={() =>
                                      void loadHistoryReport(item.id)
                                    }
                                  >
                                    {date(item.created_at)}
                                    {item.score != null
                                      ? ` · ${Math.round(item.score)} 分`
                                      : ""}
                                    {item.method === "rule_assisted"
                                      ? " · 规则"
                                      : ""}
                                  </button>
                                ))}
                              </div>
                            </div>
                          )}
                          <AnalysisView
                            analysis={analysis}
                            onStatusChange={(quote, status) =>
                              void changeRequirementStatus(quote, status)
                            }
                            onOptimizeGaps={() => optimizeFromGaps(analysis)}
                            onInterviewGaps={() =>
                              job && onOpenInterview(job.id)
                            }
                          />
                        </>
                      ))}
                    {tab === "evidence" && (
                      <>
                        <div className="section-heading">
                          <div>
                            <h3>证据与出处</h3>
                          </div>
                          <button
                            className="text-button"
                            onClick={() => openModal("evidence")}
                          >
                            <Icon name="plus" size={14} />
                            补充资料
                          </button>
                        </div>
                        {evidenceTotal ? (
                          <JobEvidence
                            key={job.id}
                            jobId={job.id}
                            focus={evidenceFocus}
                          />
                        ) : (
                          <Empty title="尚无证据资料">
                            采集岗位或补充公司材料后，原文与来源会保存在这里。
                          </Empty>
                        )}
                      </>
                    )}
                  </div>
                </>
              )}
            </section>
            {job && (
              <section className="chat-section job-chat">
                <button
                  className="button secondary small job-return"
                  onClick={returnToJob}
                >
                  返回岗位
                </button>
                <ChatWorkspace
                  key={job.id}
                  scope={{
                    kind: "job",
                    jobId: job.id,
                    company: job.company,
                    title: job.title,
                  }}
                  modelConfigured={modelConfigured}
                  onConfigureModel={onConfigureModel}
                  onNavigateConversation={onNavigateConversation}
                  onOpened={onConversationOpened}
                  onManageExtensions={() => onOpenSettings("extensions")}
                  onOpenKnowledge={onOpenKnowledge}
                />
              </section>
            )}
          </div>
        )}
      </div>{" "}
      <JobResearchDialogs
        modal={modal}
        setModal={setModal}
        busy={busy}
        batchEdit={batchEdit}
        batchDraft={batchDraft}
        setBatchDraft={setBatchDraft}
        setTagsChanged={setTagsChanged}
        confirmDelete={confirmDelete}
        importRecord={importRecord}
        submitBatchEdit={submitBatchEdit}
        submitBatchDelete={submitBatchDelete}
        perform={perform}
        job={job}
        onBossImported={() => {
          void loadJobs();
          void refreshFavorites();
          void refreshMetadata();
        }}
      />
      {followJob && (
        <PipelineFollowEditor
          job={followJob}
          onClose={() => setFollowJob(null)}
          onSaved={(patch) => applyFollowPatch(followJob, patch)}
        />
      )}
    </>
  );
}
