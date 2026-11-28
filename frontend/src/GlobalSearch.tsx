import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

// ⌘K 全局搜索（2026-11-28 批次 4）：顶栏入口 + 快捷键，一框横跨岗位/公司/知识库。
// 知识库无文档级深链——点击知识条目落到知识库视图由用户复搜；岗位与公司均可直达。
type SearchState = {
  jobs: { id: string; title: string; company: string; salary: string }[];
  companies: { company: string; evidence_count: number }[];
  knowledge: { id: string; title: string }[];
};

const EMPTY: SearchState = { jobs: [], companies: [], knowledge: [] };

export function GlobalSearch({
  onOpenJob,
  onOpenCompany,
  onNavigateView,
}: {
  onOpenJob: (jobId: string) => void;
  onOpenCompany: (company: string) => void;
  onNavigateView: (view: "knowledge") => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchState>(EMPTY);
  const [searching, setSearching] = useState(false);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Ctrl/Cmd+K 开关；Esc 关闭（挂在 always-mounted 本组件上）。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 打开时聚焦并清空上次查询
  useEffect(() => {
    if (open) {
      setQuery("");
      setResults(EMPTY);
      setCursor(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  // 250ms 防抖并行三路检索；竞态用忽略标记守卫。
  useEffect(() => {
    const q = query.trim();
    if (!open || !q) {
      setResults(EMPTY);
      setSearching(false);
      return;
    }
    setSearching(true);
    let alive = true;
    const timer = window.setTimeout(() => {
      void Promise.allSettled([
        api<{ jobs: { id: string; title: string; company: string; salary: string }[] }>(
          `/jobs?q=${encodeURIComponent(q)}&page_size=8`,
        ),
        api<{ companies: { company: string; evidence_count: number }[] }>(
          `/companies?q=${encodeURIComponent(q)}&page_size=6`,
        ),
        api<{ items: { id: string; title: string }[] }>(
          `/knowledge?q=${encodeURIComponent(q)}&page_size=6`,
        ),
      ]).then(([jobs, companies, knowledge]) => {
        if (!alive) return;
        setResults({
          jobs: jobs.status === "fulfilled" ? jobs.value.jobs : [],
          companies:
            companies.status === "fulfilled" ? companies.value.companies : [],
          knowledge:
            knowledge.status === "fulfilled"
              ? knowledge.value.items
              : [],
        });
        setSearching(false);
        setCursor(0);
      });
    }, 250);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [query, open]);

  const groups = useMemo(
    () =>
      [
        { key: "job", label: "岗位", items: results.jobs as unknown[] },
        { key: "company", label: "公司", items: results.companies as unknown[] },
        { key: "knowledge", label: "知识库", items: results.knowledge as unknown[] },
      ].filter((group) => group.items.length > 0),
    [results],
  );
  const flat = useMemo(
    () =>
      groups.flatMap((group) =>
        group.items.map((item) => ({ group: group.key, item })),
      ),
    [groups],
  );

  const activate = (group: string, item: unknown) => {
    setOpen(false);
    if (group === "job") onOpenJob((item as unknown as { id: string }).id);
    else if (group === "company")
      onOpenCompany((item as unknown as { company: string }).company);
    else onNavigateView("knowledge");
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((value) => Math.min(value + 1, Math.max(0, flat.length - 1)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((value) => Math.max(value - 1, 0));
    } else if (event.key === "Enter" && flat[cursor]) {
      activate(flat[cursor].group, flat[cursor].item);
    }
  };

  if (!open) return null;
  let index = -1;
  return (
    <div
      className="global-search-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div className="global-search" role="dialog" aria-modal="true" aria-label="全局搜索">
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="搜索岗位、公司、知识库…"
          maxLength={200}
          spellCheck={false}
        />
        {searching && <p className="tiny-label">搜索中…</p>}
        {!searching && query.trim() && flat.length === 0 && (
          <p className="tiny-label">没有匹配结果。</p>
        )}
        {groups.map((group) => (
          <div key={group.key} className="global-search-group">
            <span className="tiny-label">{group.label}</span>
            <ul>
              {group.items.map((item) => {
                index += 1;
                const active = index === cursor;
                const key =
                  group.key === "job"
                    ? (item as unknown as { id: string }).id
                    : group.key === "company"
                      ? (item as unknown as { company: string }).company
                      : (item as unknown as { id: string }).id;
                return (
                  <li key={key}>
                    <button
                      type="button"
                      className={active ? "active" : ""}
                      onClick={() => activate(group.key, item as Record<string, unknown>)}
                      onMouseEnter={() => setCursor(index)}
                    >
                      {group.key === "job" && (
                        <>
                          <b>{(item as unknown as { title: string }).title}</b>
                          <span className="tiny-label">
                            {(item as unknown as { company: string }).company}
                            {(item as unknown as { salary: string }).salary
                              ? ` · ${(item as unknown as { salary: string }).salary}`
                              : ""}
                          </span>
                        </>
                      )}
                      {group.key === "company" && (
                        <>
                          <b>{(item as unknown as { company: string }).company}</b>
                          <span className="tiny-label">
                            {(item as unknown as { evidence_count: number }).evidence_count} 条资料
                          </span>
                        </>
                      )}
                      {group.key === "knowledge" && (
                        <b>{(item as unknown as { title: string }).title}</b>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
        <p className="tiny-label global-search-hint">
          ↑↓ 选择 · Enter 打开 · Esc 关闭
        </p>
      </div>
    </div>
  );
}
