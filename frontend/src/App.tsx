import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, getSourceHealth } from "./api";
import type {
  BrowserStatus,
  ConversationNavigation,
  PageInfo,
  PipelinePrescription,
  Source,
  SourceHealth,
  Task,
} from "./api";
import { signalTaskNames } from "./SignalSources";
import { toast } from "./toast";
import { emptyPage } from "./Pagination";
import { EvidenceWorkspace } from "./EvidenceWorkspace";
import { Button } from "./ui";
import { ResumeImport } from "./ResumeImport";
import { TodayView } from "./TodayView";
import { JobResearch } from "./JobResearch";
import type { JobOpenRequest } from "./JobResearch";
import { navigateTabs } from "./tabNavigation";
import Tutorial, { tutorialSeenVersion, TUTORIAL_VERSION } from "./Tutorial";
import { useCompareSelection } from "./compareSelectionStore";
import { ChatWorkspace } from "./chat/ChatWorkspace";
import { Icon } from "./Icon";
import type { InterviewDeepLink } from "./InterviewView";
import type { InterviewDrillRef } from "./api";
import {
  NAV_GROUPS,
  NAV_STORAGE_KEY,
  hashQuery,
  viewFromHash,
} from "./navigation";
import { GlobalSearch } from "./GlobalSearch";
import type { SettingsTab, ViewId } from "./navigation";
import type { SourcesTab } from "./SourcesView";
import type { WorkshopPrefill } from "./ResumeWorkshop";

// 按视图代码分割（2026-11-28 审计 P1-7）：单包 1.69MB（gzip 534KB）里 echarts 与
// 面试/横评/工坊等大视图占大头。常驻视图（岗位研究/公司资料库）与首屏（今日）保持
// eager，其余视图懒加载——首次切到该视图时才拉对应 chunk（浏览器缓存后秒开）。
// 命名导出经 .then(m => ({ default: m.X })) 归一成 lazy 需要的形态。
const CompanyCompare = lazy(() =>
  import("./CompanyCompare").then((m) => ({ default: m.CompanyCompare })),
);
const SourcesView = lazy(() =>
  import("./SourcesView").then((m) => ({ default: m.SourcesView })),
);
const AgentExtensions = lazy(() =>
  import("./AgentExtensions").then((m) => ({ default: m.AgentExtensions })),
);
const ModelSettings = lazy(() =>
  import("./ModelSettings").then((m) => ({ default: m.ModelSettings })),
);
const SystemSettings = lazy(() =>
  import("./SystemSettings").then((m) => ({ default: m.SystemSettings })),
);
const ResumeWorkshop = lazy(() =>
  import("./ResumeWorkshop").then((m) => ({ default: m.ResumeWorkshop })),
);
const ProfileView = lazy(() =>
  import("./ProfileView").then((m) => ({ default: m.ProfileView })),
);
const RunwayView = lazy(() =>
  import("./RunwayView").then((m) => ({ default: m.RunwayView })),
);
const JdAssessmentView = lazy(() =>
  import("./JdAssessmentView").then((m) => ({ default: m.JdAssessmentView })),
);
const FailureMuseumView = lazy(() =>
  import("./FailureMuseumView").then((m) => ({ default: m.FailureMuseumView })),
);
const KnowledgeView = lazy(() =>
  import("./KnowledgeView").then((m) => ({ default: m.KnowledgeView })),
);
const LogView = lazy(() =>
  import("./LogView").then((m) => ({ default: m.LogView })),
);
const FavoritesView = lazy(() =>
  import("./FavoritesView").then((m) => ({ default: m.FavoritesView })),
);
const InterviewView = lazy(() => import("./InterviewView"));
const PrepPlanView = lazy(() =>
  import("./PrepPlanView").then((m) => ({ default: m.PrepPlanView })),
);

// 懒视图的加载占位：本地 chunk 秒级到位，占位只是防瞬间白屏。
function LazyPane({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="loading-state">
          <span className="spinner" />
          正在加载工作区…
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

// App 壳（2026-11-19 拆分）：导航、视图路由（#/ 深链）、跨视图信号与各工作区装配。
// 岗位研究的全部状态与交互在 JobResearch.tsx；适配报告组件簇在 AnalysisView.tsx；
// 视图/导航共享定义在 navigation.ts。
export default function App() {
  // 今日概览（2026-11-18）是每天的第一屏：待办、新增与备份提醒都在这里。
  // 带 #/<view> 深链打开时落深链视图（刷新/书签保位）。
  const [view, setView] = useState<ViewId>(() => viewFromHash() ?? "today");
  // 导航组展开状态：默认全展开（首次访问能看到全部功能）；切换视图自动展开所在组，从不自动收起。
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(() => {
    try {
      const stored = localStorage.getItem(NAV_STORAGE_KEY);
      return stored
        ? new Set(JSON.parse(stored) as string[])
        : new Set(NAV_GROUPS.map((group) => group.id));
    } catch {
      return new Set(NAV_GROUPS.map((group) => group.id));
    }
  });
  // 知识库侧栏角标：轻请求取 total，增删改经 KnowledgeView 回写。
  const [knowledgeTotal, setKnowledgeTotal] = useState(0);
  function toggleGroup(id: string) {
    setExpandedGroups((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      try {
        localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...next]));
      } catch {
        /* 存储不可用时仅本次会话生效 */
      }
      return next;
    });
  }
  useEffect(() => {
    const group = NAV_GROUPS.find((item) =>
      item.items.some((nav) => nav.id === view),
    );
    if (!group) return;
    setExpandedGroups((previous) => {
      if (previous.has(group.id)) return previous;
      const next = new Set([...previous, group.id]);
      try {
        localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...next]));
      } catch {
        /* 同上 */
      }
      return next;
    });
  }, [view]);
  // 地址栏 ↔ 视图双向同步：切换压入历史（浏览器回退可走回上个视图），回退/前进/手改地址经 hashchange 生效。
  // 带查询参数的深链（#/interview?job=<id>）在进视图时转成请求信号，由目标工作区消费。
  const [interviewRequest, setInterviewRequest] =
    useState<InterviewDeepLink>(null);
  // focus 来自备战计划的当日专项：带着它进面试间，出题会定向覆盖这个主题（weak_focus）。
  // drill 来自失败博物馆/备战计划任务卡的「错题强化」直达：数组存在即进错题强化官（空数组=用户自选）。
  function openInterview(jobId = "", focus = "", drill?: InterviewDrillRef[]) {
    setInterviewRequest(
      jobId || focus || drill
        ? { jobId, focus, drill, token: Date.now() }
        : null,
    );
    setView("interview");
  }
  useEffect(() => {
    const onHashChange = () => {
      const next = viewFromHash();
      if (next) setView(next);
      if (next === "interview") {
        const query = hashQuery();
        const job = query.get("job") || "";
        const focus = query.get("focus") || "";
        setInterviewRequest(
          job || focus ? { jobId: job, focus, token: Date.now() } : null,
        );
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  useEffect(() => {
    const target = `#/${view}`;
    const current = window.location.hash;
    // 带查询参数的深链（如 #/interview?job=x）视为已同步：改写会丢参数并误触发 hashchange。
    if (current === target || current.startsWith(`${target}?`)) return;
    // 无 hash 的首次进入只替换当前历史项（避免多压一条「空 → #/today」）；后续切换压栈，回退键可走回上个视图。
    if (!window.location.hash) window.history.replaceState(null, "", target);
    else window.location.hash = `/${view}`;
  }, [view]);
  // 直接以深链打开应用（书签/刷新）时同样消费一次 #/interview?job=<id>。
  // 空 job/focus 不发请求信号：普通刷新后进面试视图不该被误当深链（曾导致刷新后
  // 点场次卡在新建面板、面试数据不加载）。
  useEffect(() => {
    if (view !== "interview") return;
    const query = hashQuery();
    const job = query.get("job") || "";
    const focus = query.get("focus") || "";
    if (!job && !focus) return;
    setInterviewRequest({ jobId: job, focus, token: Date.now() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 岗位详情「优化简历」跳转工坊时的预填（工坊消费后清空，避免切回时重复生效）。
  const [workshopPrefill, setWorkshopPrefill] =
    useState<WorkshopPrefill | null>(null);
  function openWorkshop(prefill: WorkshopPrefill) {
    setWorkshopPrefill(prefill);
    setView("workshop");
  }
  // 横评已选列表（共享 store）：侧栏徽标用；资料库/岗位详情「加入横评」直接写这份 store。
  const compareSelected = useCompareSelection();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [chatNavigation, setChatNavigation] =
    useState<ConversationNavigation | null>(null);
  // 全局对话的预填指令（如求职参考的「让 Agent 协同补齐」、资料库导入）：token 保证同一段指令可
  // 重复触发；newSession 让这类任务入口先落到全新会话，不混进用户上次停留的旧会话。工作区应用后
  // 回调置空，避免用户之后每次进入全局对话都被这段旧指令重新打断。
  const [chatPrompt, setChatPrompt] = useState<{
    text: string;
    token: string;
    newSession?: boolean;
  } | null>(null);
  const openGlobalAgent = useCallback((prompt: string) => {
    setChatPrompt({
      text: prompt,
      token: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      newSession: true,
    });
    setView("global");
  }, []);
  const navigateConversation = useCallback(
    (request: ConversationNavigation) => {
      if (request.scope.kind === "job")
        throw new Error("扩大范围只能前往公司或全局对话");
      setChatNavigation(request);
      setView(request.scope.kind === "global" ? "global" : "evidence");
      setNavigationOpen(false);
    },
    [],
  );
  const chatOpened = useCallback((requestId: string) => {
    setChatNavigation((current) =>
      current?.requestId === requestId ? null : current,
    );
    toast("范围已扩大，可继续提问");
  }, []);
  const [sourcesTab, setSourcesTab] = useState<SourcesTab>("collect");
  // 公司对话入口前移（2026-11-17）：岗位详情直接跳公司对话；token 变化驱动 EvidenceWorkspace 锁主体。
  const [companyChatRequest, setCompanyChatRequest] = useState<{
    company: string;
    token: number;
  } | null>(null);
  function openCompanyChat(company: string) {
    setCompanyChatRequest({ company, token: Date.now() });
    setView("evidence");
  }
  // 岗位研究常驻挂载（hidden 切换），跨视图点名岗位（收藏/今日概览）走 openRequest 信号；
  // token 保证同一目标可重复触发。
  const [jobOpenRequest, setJobOpenRequest] = useState<JobOpenRequest | null>(
    null,
  );
  function openJobFrom(id: string) {
    setJobOpenRequest({ kind: "job", id, token: Date.now() });
    setView("jobs");
  }
  // 行动处方（2026-11-22）的跳转目标 → 视图切换：今日页与收藏页共用同一份映射。
  function openPrescriptionTarget(target: PipelinePrescription["target"]) {
    if (target === "workshop") setView("workshop");
    else if (target === "interview") openInterview();
    else if (target === "profile") setView("profile");
    else if (target === "jobs") {
      setJobOpenRequest({ kind: "list", token: Date.now() });
      setView("jobs");
    } else setView("favorites");
  }
  // 收藏变动 / 顶栏刷新：计数器信号由 JobResearch 消费（前者仅在「只看收藏」过滤时重刷列表）。
  const [favoritesTick, setFavoritesTick] = useState(0);
  const [refreshTick, setRefreshTick] = useState(0);
  const [jobsTotal, setJobsTotal] = useState(0);
  // 收藏数只做侧栏角标：独立轻请求取 total，不与档案列表混用（收藏变动后单独刷新）。
  const [favTotal, setFavTotal] = useState(0);
  const [tasksInfo, setTasksInfo] = useState<PageInfo>(emptyPage);
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceHealth, setSourceHealth] = useState<SourceHealth[]>([]);
  const [locations, setLocations] = useState<
    { location: string; jobs: number }[]
  >([]);
  const [cityPinyin, setCityPinyin] = useState<Record<string, string[]>>({});
  const [tasks, setTasks] = useState<Task[]>([]);
  // 采集表单草稿：岗位研究的检索区与「数据与采集」页共享同一份（App 持有，两处受控）。
  const [query, setQuery] = useState("");
  const [location, setLocation] = useState("");
  const [source, setSource] = useState("remotive");
  const [browserSource, setBrowserSource] = useState("boss");
  const [browserLimit, setBrowserLimit] = useState(20);
  const [browser, setBrowser] = useState<BrowserStatus | null>(null);
  const [online, setOnline] = useState(false);
  const [modelConfigured, setModelConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  // 壳层异步动作（数据与采集页的按钮、我的简历保存）共用一个 busy 键。
  const [shellBusy, setShellBusy] = useState("");
  async function perform(key: string, action: () => Promise<void>) {
    setShellBusy(key);
    try {
      await action();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setShellBusy("");
    }
  }
  // 使用教程（遮罩指引）：首次访问（以及教程改版后第一次）自动弹出，看完或跳过即按版本
  // 记住，不再自动出现；侧栏底部「使用教程」可随时重看。播放期间展开全部分组，保证步骤
  // 指向的导航元素可见。
  const [tutorialOpen, setTutorialOpen] = useState(false);
  useEffect(() => {
    if (!loading && tutorialSeenVersion() < TUTORIAL_VERSION)
      setTutorialOpen(true);
  }, [loading]);
  useEffect(() => {
    if (!tutorialOpen) return;
    setExpandedGroups((previous) => {
      const all = new Set(NAV_GROUPS.map((group) => group.id));
      if (NAV_GROUPS.every((group) => previous.has(group.id))) return previous;
      try {
        localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...all]));
      } catch {
        /* 存储不可用时仅本次会话生效 */
      }
      return all;
    });
  }, [tutorialOpen]);
  const [settingsReturn, setSettingsReturn] = useState<ViewId>("jobs");
  // 设置中心（2026-11-17）：模型/系统/Agent 扩展三页合一，视图内分标签。原独立视图的跳转点
  // 统一改走 openSettings(tab)，深链行为不变（记录来源工作区，返回键原路回跳）。
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("model");
  function openSettings(tab: SettingsTab) {
    setSettingsReturn(view);
    setSettingsTab(tab);
    setView("settings");
  }
  function configureModel() {
    openSettings("model");
  }
  const [resumeText, setResumeText] = useState("");
  const [resumeFormat, setResumeFormat] = useState("text");
  const [resumeRevision, setResumeRevision] = useState<number | null>(null);
  const [resumeSaved, setResumeSaved] = useState(false);

  const refreshFavCount = useCallback(async () => {
    // 收藏角标的唯一数据源；失败静默——角标缺一个数字不值得打断主流程。
    try {
      setFavTotal(
        (await api<PageInfo>("/jobs?starred=true&page_size=1")).total,
      );
    } catch {
      /* 角标静默 */
    }
  }, []);
  async function loadTasks(page: number) {
    const data = await api<PageInfo & { tasks: Task[] }>(
      `/tasks?page=${page}&page_size=10`,
    );
    setTasks(data.tasks);
    setTasksInfo(data);
  }
  const refreshMetadata = useCallback(async () => {
    const results = await Promise.allSettled([
      api<{ status: string; model_configured: boolean }>("/health"),
      api<{ sources: Source[] }>("/sources"),
      api<PageInfo & { tasks: Task[] }>("/tasks?page_size=10"),
      api<BrowserStatus>("/browser/status"),
      api<{ locations: { location: string; jobs: number }[] }>("/locations"),
      api<{ pinyin: Record<string, string[]> }>("/cities"),
      api<PageInfo>("/knowledge?page_size=1"),
      getSourceHealth(),
    ]);
    if (results[0].status === "fulfilled") {
      setOnline(true);
      setModelConfigured(results[0].value.model_configured);
    } else setOnline(false);
    if (results[1].status === "fulfilled") setSources(results[1].value.sources);
    if (results[2].status === "fulfilled") {
      setTasks(results[2].value.tasks);
      setTasksInfo(results[2].value);
    }
    if (results[3].status === "fulfilled") setBrowser(results[3].value);
    if (results[4].status === "fulfilled")
      setLocations(results[4].value.locations);
    if (results[5].status === "fulfilled")
      setCityPinyin(results[5].value.pinyin);
    if (results[6].status === "fulfilled")
      setKnowledgeTotal(results[6].value.total);
    if (results[7].status === "fulfilled")
      setSourceHealth(results[7].value.sources);
  }, []);
  useEffect(() => {
    let active = true;
    async function init() {
      try {
        await Promise.all([refreshMetadata(), refreshFavCount()]);
        const resume = await api<{
          text: string;
          format: string;
          revision?: number;
        }>("/resume");
        if (active) {
          setResumeText(resume.text || "");
          setResumeFormat(resume.format || "text");
          setResumeRevision(resume.revision || null);
          setResumeSaved(Boolean(resume.text));
        }
      } catch (err) {
        if (active) toast((err as Error).message, "error");
      } finally {
        if (active) setLoading(false);
      }
    }
    void init();
    return () => {
      active = false;
    };
  }, [refreshMetadata, refreshFavCount]);
  async function saveResume(event: FormEvent) {
    event.preventDefault();
    await perform("resume", async () => {
      const data = await api<{ revision: number }>("/resume", {
        method: "PUT",
        body: JSON.stringify({ text: resumeText, format: resumeFormat }),
      });
      setResumeRevision(data.revision);
      setResumeSaved(true);
      toast("简历已保存在本地。岗位适配分析将使用这个版本。");
    });
  }
  const sourceNames: Record<string, string> = {
    ...Object.fromEntries(sources.map((item) => [item.id, item.name])),
    ...signalTaskNames,
    knowledge_import: "知识库导入",
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace-main">
        跳到工作区
      </a>
      <aside className={`sidebar${navigationOpen ? " nav-open" : ""}`}>
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            setView("today");
          }}
        >
          <span className="brand-symbol" aria-hidden="true">
            <i />
            <i />
          </span>
          <span>
            知根<span className="brand-english">JobScope</span>
          </span>
        </a>
        <button
          className="mobile-nav-toggle"
          aria-expanded={navigationOpen}
          aria-controls="workspace-navigation"
          onClick={() => setNavigationOpen((value) => !value)}
        >
          <Icon name={navigationOpen ? "close" : "menu"} />
          {navigationOpen ? "收起导航" : "工作区导航"}
        </button>
        <nav
          id="workspace-navigation"
          aria-label="主导航"
          onClick={(event) => {
            // 组头切换不收起移动端导航：用户只是折叠分组，不是完成了页面跳转。
            const button = (event.target as HTMLElement).closest("button");
            if (!button || button.classList.contains("nav-group-toggle"))
              return;
            setNavigationOpen(false);
            if (window.matchMedia("(max-width: 800px)").matches)
              document.getElementById("workspace-main")?.focus();
          }}
        >
          {NAV_GROUPS.map((group) => {
            const expanded = expandedGroups.has(group.id);
            return (
              <div
                key={group.id}
                className={`nav-group${expanded ? " expanded" : ""}`}
              >
                <button
                  type="button"
                  className="nav-group-toggle"
                  aria-expanded={expanded}
                  aria-controls={`nav-group-${group.id}`}
                  onClick={() => toggleGroup(group.id)}
                >
                  {group.label}
                  <Icon name="chevron" size={13} />
                </button>
                <div id={`nav-group-${group.id}`} className="nav-group-items">
                  {group.items.map((item) => {
                    const badge =
                      item.id === "jobs"
                        ? jobsTotal
                        : item.id === "favorites"
                          ? favTotal
                          : item.id === "compare"
                            ? compareSelected.length
                            : item.id === "knowledge"
                              ? knowledgeTotal
                              : 0;
                    return (
                      <button
                        key={item.id}
                        aria-current={view === item.id ? "page" : undefined}
                        className={view === item.id ? "active" : ""}
                        onClick={() => setView(item.id)}
                      >
                        <Icon name={item.icon} />
                        {item.label}
                        {item.id === "resume"
                          ? resumeSaved && <span className="nav-dot" />
                          : badge > 0 && (
                              <span className="nav-count">{badge}</span>
                            )}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </nav>

        <div className="sidebar-bottom">
          <span className={`connection-dot ${online ? "connected" : ""}`} />
          {online ? "本地工作区已连接" : "等待本地服务"}
          <button
            className="text-button sidebar-tutorial-button"
            onClick={() => setTutorialOpen(true)}
          >
            使用教程
          </button>
          <span className="version">v0.1</span>
        </div>
      </aside>

      <main
        id="workspace-main"
        tabIndex={-1}
        className={
          view === "jobs" || view === "evidence"
            ? "main main-workbench"
            : view === "sources" ||
                view === "settings" ||
                view === "compare" ||
                view === "favorites" ||
                view === "knowledge" ||
                view === "logs" ||
                view === "profile" ||
                view === "today" ||
                view === "jd-assessment" ||
                view === "failure-museum" ||
                view === "prep-plan"
              ? "main main-compact"
              : "main"
        }
      >
        <header className="topbar">
          <div className="breadcrumb">
            工作区<span>/</span>
            {view === "today"
              ? "今日概览"
              : view === "jobs"
                ? "岗位研究"
                : view === "favorites"
                  ? "收藏"
                  : view === "interview"
                    ? "模拟面试"
                    : view === "jd-assessment"
                      ? "反向JD测评"
                      : view === "failure-museum"
                        ? "失败博物馆"
                        : view === "prep-plan"
                          ? "备战计划"
                          : view === "resume"
                            ? "我的简历"
                            : view === "profile"
                              ? "个人资料库"
                              : view === "workshop"
                                ? "简历工坊"
                                : view === "runway"
                                  ? "生存跑道"
                                  : view === "evidence"
                                    ? "公司资料库"
                                    : view === "compare"
                                      ? "公司对比"
                                      : view === "global"
                                        ? "全局对话"
                                        : view === "knowledge"
                                          ? "知识库"
                                          : view === "logs"
                                            ? "运行日志"
                                            : view === "settings"
                                              ? "设置中心"
                                              : "数据与采集"}
          </div>
          <div className="topbar-right">
            <button
              className="icon-button"
              aria-label="全局搜索"
              title="全局搜索（Ctrl/⌘+K）"
              onClick={() =>
                document.dispatchEvent(
                  new KeyboardEvent("keydown", {
                    key: "k",
                    metaKey: true,
                    bubbles: true,
                  }),
                )
              }
            >
              <Icon name="search" size={17} />
            </button>
            <button
              className="icon-button"
              aria-label="刷新工作区"
              title="刷新档案、元数据与收藏角标"
              onClick={() => setRefreshTick((tick) => tick + 1)}
            >
              <Icon name="source" size={17} />
            </button>
          </div>
        </header>
        <div hidden={view !== "evidence"} className="evidence-view">
          <EvidenceWorkspace
            active={view === "evidence"}
            refreshMetadata={refreshMetadata}
            modelConfigured={modelConfigured}
            onConfigureModel={configureModel}
            navigation={chatNavigation}
            onNavigateConversation={navigateConversation}
            onOpened={chatOpened}
            onManageExtensions={() => openSettings("extensions")}
            onOpenCompare={() => setView("compare")}
            onOpenKnowledge={() => setView("knowledge")}
            openCompanyChat={companyChatRequest}
            onOpenJob={openJobFrom}
            onWorkshop={openWorkshop}
          />
        </div>

        {/* 岗位研究常驻挂载（hidden 切换）：筛选与选中状态在视图切换间保留；跨视图联动经下方信号。 */}
        <JobResearch
          active={view === "jobs"}
          loading={loading}
          sources={sources}
          locations={locations}
          cityPinyin={cityPinyin}
          modelConfigured={modelConfigured}
          resumeSaved={resumeSaved}
          sourceNames={sourceNames}
          sourceHealth={sourceHealth}
          query={query}
          onQueryChange={setQuery}
          location={location}
          onLocationChange={setLocation}
          source={source}
          onSourceChange={setSource}
          refreshMetadata={refreshMetadata}
          refreshFavorites={refreshFavCount}
          onTotalChange={setJobsTotal}
          onOpenSettings={openSettings}
          onConfigureModel={configureModel}
          onOpenSources={(tab) => {
            setSourcesTab(tab);
            setView("sources");
          }}
          onOpenCompanyChat={openCompanyChat}
          onOpenGlobalAgent={openGlobalAgent}
          onOpenCompare={() => setView("compare")}
          onOpenKnowledge={() => setView("knowledge")}
          onOpenResume={() => setView("resume")}
          onOpenInterview={(jobId) => openInterview(jobId)}
          onNavigateConversation={navigateConversation}
          onConversationOpened={chatOpened}
          onWorkshopPrefill={openWorkshop}
          openRequest={jobOpenRequest}
          onOpenHandled={() => setJobOpenRequest(null)}
          favoritesTick={favoritesTick}
          refreshTick={refreshTick}
        />

        {view === "today" && (
          <TodayView
            onOpenJob={openJobFrom}
            onOpenFavorites={() => setView("favorites")}
            onOpenSources={() => {
              setSourcesTab("collect");
              setView("sources");
            }}
            onOpenJobs={() => {
              setJobOpenRequest({ kind: "list", token: Date.now() });
              setView("jobs");
            }}
            onOpenSettings={() => openSettings("system")}
            onPrescriptionNavigate={openPrescriptionTarget}
            onOpenPrepPlan={() => setView("prep-plan")}
          />
        )}

        {view === "knowledge" && (
          <LazyPane>
            <KnowledgeView onCountChange={setKnowledgeTotal} />
          </LazyPane>
        )}

        {view === "global" && (
          <>
            <h1 className="sr-only">全局对话</h1>
            <ChatWorkspace
              scope={{ kind: "global" }}
              modelConfigured={modelConfigured}
              onConfigureModel={configureModel}
              onManageExtensions={() => openSettings("extensions")}
              onOpenKnowledge={() => setView("knowledge")}
              navigation={
                chatNavigation?.scope.kind === "global" ? chatNavigation : null
              }
              onNavigateConversation={navigateConversation}
              onOpened={chatOpened}
              prefill={chatPrompt}
              onPrefillConsumed={() => setChatPrompt(null)}
              onOpenJob={openJobFrom}
              onWorkshop={openWorkshop}
            />
          </>
        )}

        {view === "compare" && (
          <LazyPane>
            <CompanyCompare />
          </LazyPane>
        )}

        {view === "interview" && (
          <LazyPane>
            <InterviewView
              deepLink={interviewRequest}
              modelConfigured={modelConfigured}
              onConfigureModel={configureModel}
            />
          </LazyPane>
        )}

        {view === "prep-plan" && (
          <LazyPane>
            <PrepPlanView onOpenInterview={openInterview} />
          </LazyPane>
        )}

        {view === "favorites" && (
          <LazyPane>
            <FavoritesView
              onOpenJob={openJobFrom}
              onOpenCompare={() => setView("compare")}
              onPrescriptionNavigate={openPrescriptionTarget}
              onOpenRunway={() => setView("runway")}
              onChange={() => {
                // 收藏变动：刷新角标并通知岗位研究（后者仅在「只看收藏」过滤时重刷列表）。
                void refreshFavCount();
                setFavoritesTick((tick) => tick + 1);
              }}
            />
          </LazyPane>
        )}

        {view === "resume" && (
          <>
            <section className="page-intro compact">
              <h1 className="sr-only">我的简历</h1>
              {resumeRevision != null && (
                <span className="revision-label">版本 {resumeRevision}</span>
              )}
            </section>
            <div className="resume-layout">
              <form className="paper-panel resume-editor" onSubmit={saveResume}>
                <div className="section-heading">
                  <h2>简历与事实档案</h2>
                  <label className="format-select">
                    <span className="sr-only">简历格式</span>
                    <select
                      value={resumeFormat}
                      onChange={(event) => {
                        setResumeFormat(event.target.value);
                        setResumeSaved(false);
                      }}
                    >
                      <option value="text">简历纯文本 / Markdown</option>
                      <option value="facts_yaml">ResumeLoom facts.yaml</option>
                    </select>
                  </label>
                </div>
                <ResumeImport
                  disabled={Boolean(shellBusy) || loading}
                  hasText={Boolean(resumeText.trim())}
                  onApply={(text, format) => {
                    setResumeText(text);
                    setResumeFormat(format);
                    setResumeSaved(false);
                    toast("已提取并填入编辑区，请核对内容后保存。");
                  }}
                />
                <label htmlFor="resume-text" className="field-label">
                  简历正文
                </label>
                <textarea
                  className="resume-textarea"
                  id="resume-text"
                  value={resumeText}
                  onChange={(event) => {
                    setResumeText(event.target.value);
                    setResumeSaved(false);
                  }}
                  required
                  maxLength={100000}
                  spellCheck={false}
                  placeholder={
                    resumeFormat === "facts_yaml"
                      ? "schema_version: 1\nfacts:\n  ..."
                      : "填写技能、工作经历、项目经验，以及地点和薪资等求职偏好。\n\n建议保留具体职责、项目背景和经过确认的成果。"
                  }
                />
                <div className="editor-footer">
                  <span className={resumeSaved ? "" : "editor-unsaved"}>
                    {resumeText.length.toLocaleString()} 字符 ·{" "}
                    {resumeSaved ? "已保存" : "未保存的修改"}
                  </span>
                  <Button
                    className="button primary"
                    busy={shellBusy === "resume"}
                    disabled={Boolean(shellBusy)}
                    type="submit"
                  >
                    保存到本地
                    <Icon name="check" size={17} />
                  </Button>
                </div>
                <div className="privacy-note">
                  <Icon name="book" size={18} />
                  <p>
                    文件在本机提取文字，确认保存后仅保留文本。启用外部模型分析时，相关资料可能发送至你配置的模型服务。
                  </p>
                </div>
              </form>
            </div>
          </>
        )}

        {view === "profile" && (
          <LazyPane>
            <ProfileView onImportViaChat={openGlobalAgent} />
          </LazyPane>
        )}

        {view === "workshop" && (
          <LazyPane>
            <ResumeWorkshop
              prefill={workshopPrefill}
              onPrefillConsumed={() => setWorkshopPrefill(null)}
              onOpenResume={() => setView("resume")}
            />
          </LazyPane>
        )}

        {view === "runway" && (
          <LazyPane>
            <RunwayView />
          </LazyPane>
        )}

        {view === "jd-assessment" && (
          <LazyPane>
            <JdAssessmentView />
          </LazyPane>
        )}

        {view === "failure-museum" && (
          <LazyPane>
            <FailureMuseumView onOpenInterview={openInterview} />
          </LazyPane>
        )}

        {view === "sources" && (
          <LazyPane>
            <SourcesView
              active={view === "sources"}
              tab={sourcesTab}
              onTabChange={setSourcesTab}
              sources={sources}
              loading={loading}
              jobsTotal={jobsTotal}
              browser={browser}
              onBrowserStatus={setBrowser}
              tasks={tasks}
              tasksInfo={tasksInfo}
              loadTasks={loadTasks}
              busy={shellBusy}
              perform={perform}
              refreshMetadata={refreshMetadata}
              onJobsChanged={() => {
                setRefreshTick((tick) => tick + 1);
                void refreshFavCount();
                return Promise.resolve();
              }}
              onOpenJobs={() => {
                setJobOpenRequest({ kind: "list", token: Date.now() });
                setView("jobs");
              }}
              query={query}
              onQueryChange={setQuery}
              location={location}
              onLocationChange={setLocation}
              browserSource={browserSource}
              onBrowserSourceChange={setBrowserSource}
              browserLimit={browserLimit}
              onBrowserLimitChange={setBrowserLimit}
              cityPinyin={cityPinyin}
              locations={locations}
              sourceNames={sourceNames}
            />
          </LazyPane>
        )}

        {view === "logs" && (
          <LazyPane>
            <LogView />
          </LazyPane>
        )}

        {view === "settings" && (
          <>
            <button
              className="button secondary settings-return"
              onClick={() => setView(settingsReturn)}
            >
              返回原工作区
            </button>
            {/* 设置中心：模型 / 系统 / Agent 扩展三页合一。标签沿用 management-tabs 的键盘导航。 */}
            <div
              className="management-tabs"
              role="tablist"
              aria-label="设置分区"
              onKeyDown={navigateTabs}
            >
              {(
                [
                  ["model", "模型设置"],
                  ["system", "系统设置"],
                  ["extensions", "Agent 扩展"],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  id={`settings-${id}-tab`}
                  role="tab"
                  aria-controls={`settings-${id}-panel`}
                  aria-selected={settingsTab === id}
                  tabIndex={settingsTab === id ? 0 : -1}
                  onClick={() => setSettingsTab(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div
              id="settings-model-panel"
              role="tabpanel"
              aria-labelledby="settings-model-tab"
              hidden={settingsTab !== "model"}
            >
              <LazyPane>
                <ModelSettings onSaved={refreshMetadata} />
              </LazyPane>
            </div>
            <div
              id="settings-system-panel"
              role="tabpanel"
              aria-labelledby="settings-system-tab"
              hidden={settingsTab !== "system"}
            >
              <LazyPane>
                <SystemSettings />
              </LazyPane>
            </div>
            <div
              id="settings-extensions-panel"
              role="tabpanel"
              aria-labelledby="settings-extensions-tab"
              hidden={settingsTab !== "extensions"}
              className="extensions-view"
            >
              <LazyPane>
                <AgentExtensions />
              </LazyPane>
            </div>
          </>
        )}
      </main>
      <Tutorial open={tutorialOpen} onClose={() => setTutorialOpen(false)} />
    <GlobalSearch
      onOpenJob={openJobFrom}
      onOpenCompany={openCompanyChat}
      onNavigateView={(view: "knowledge") => setView(view)}
    />
    </div>
  );
}
