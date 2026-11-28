// 岗位选择器与错题选题面板（CreatePanel / RealWorkspace 共用）。
import { useEffect, useRef, useState } from "react";
import { Icon } from "../Icon";
import { api, date, listInterviewWeakPoints } from "../api";
import type {
  InterviewDrillRef,
  InterviewWeakPoint,
  InterviewWeakPoints,
} from "../api";
import type { DrillGroup } from "./shared";
import {
  MAX_DRILLS,
  DRILL_SOURCE_NAMES,
  refKey,
  groupWeakPoints,
} from "./shared";
// 岗位检索下拉（combobox）：输入走服务端 /jobs?q= 实时检索（title/company/description 子串匹配），
// 空查询给最近更新的 20 条；岗位多时靠关键词收窄，不靠翻页。

export function DrillPicker({
  selection,
  onSelection,
  onLoaded,
  jobId,
  onUseJob,
}: {
  selection: InterviewDrillRef[];
  onSelection: (next: InterviewDrillRef[]) => void;
  onLoaded: (book: InterviewWeakPoints) => void;
  jobId: string;
  onUseJob: (id: string, label: string) => void;
}) {
  const [book, setBook] = useState<InterviewWeakPoints | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let alive = true;
    listInterviewWeakPoints()
      .then((data) => {
        if (alive) {
          setBook(data);
          onLoaded(data);
        }
      })
      .catch((err) => {
        if (alive) setError((err as Error).message);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const items = book?.items ?? [];
  const selected = new Set(selection.map(refKey));
  const isSelected = (item: InterviewWeakPoint) =>
    item.refs.some((ref) => selected.has(refKey(ref)));
  const visible = filter
    ? items.filter((item) => item.tags.includes(filter))
    : items;
  const groups = groupWeakPoints(visible);
  const listed = new Set(items.flatMap((item) => item.refs.map(refKey)));
  // 带进来的引用指向没被点过失分点的题（「重练此题」）：不在错题本里，单独计数说明。
  const extra = selection.filter((ref) => !listed.has(refKey(ref))).length;
  const full = selection.length >= MAX_DRILLS;
  const tagLabels = Object.fromEntries(
    (book?.tags ?? []).map((tag) => [tag.id, tag.label]),
  );

  const toggleItem = (item: InterviewWeakPoint) => {
    const keys = item.refs.map(refKey);
    if (isSelected(item))
      onSelection(selection.filter((ref) => !keys.includes(refKey(ref))));
    else if (!full) onSelection([...selection, item.refs[0]]);
  };
  const toggleGroup = (group: DrillGroup) => {
    const keys = group.items.flatMap((item) => item.refs.map(refKey));
    if (group.items.every(isSelected))
      onSelection(selection.filter((ref) => !keys.includes(refKey(ref))));
    else
      onSelection(
        [
          ...selection,
          ...group.items
            .filter((item) => !isSelected(item))
            .map((item) => item.refs[0]),
        ].slice(0, MAX_DRILLS),
      );
  };
  // 全选/清空只作用于当前筛出来的题：被筛掉的分组里已勾的保留。
  const visibleKeys = new Set(visible.flatMap((item) => item.refs.map(refKey)));
  const selectVisible = () =>
    onSelection(
      [
        ...selection,
        ...visible
          .filter((item) => !isSelected(item))
          .map((item) => item.refs[0]),
      ].slice(0, MAX_DRILLS),
    );
  const clearVisible = () =>
    onSelection(selection.filter((ref) => !visibleKeys.has(refKey(ref))));

  // 选中的错题都来自同一个岗位而目标还没选：一键带上来源岗位，变式题才贴着原 JD。
  const chosen = items.filter(isSelected);
  const commonJob =
    chosen.length && chosen.every((item) => item.job_id === chosen[0].job_id)
      ? chosen[0]
      : null;
  const sessionCount = new Set(
    items.map((item) => `${item.source}|${item.session_id}`),
  ).size;

  return (
    <section className="iv-drill" aria-label="错题本">
      <div className="iv-drill-head">
        <strong>错题本</strong>
        {book && (
          <span className="tiny-label">
            共 {book.total} 题 · 来自 {sessionCount} 份复盘
            {book.limitations.length
              ? `（${book.limitations.join("；")}）`
              : ""}
          </span>
        )}
        <span className="iv-progress iv-drill-selected" role="status">
          已选 {selection.length}/{MAX_DRILLS}
        </span>
        {items.length > 0 && (
          <span className="iv-drill-bulk">
            <button className="text-button" onClick={selectVisible}>
              全选
            </button>
            <button
              className="text-button"
              onClick={clearVisible}
              disabled={!visible.some(isSelected)}
            >
              清空
            </button>
          </span>
        )}
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
      {book && book.patterns.length > 0 && (
        <div
          className="iv-drill-filters"
          role="group"
          aria-label="按失分要素筛选"
        >
          <span className="tiny-label">失分要素</span>
          <button
            className={!filter ? "active" : ""}
            onClick={() => setFilter("")}
          >
            全部
          </button>
          {book.patterns.map((item) => (
            <button
              key={item.tag}
              className={filter === item.tag ? "active" : ""}
              title={`在 ${item.count} 道错题里被点到的失分要素`}
              onClick={() => setFilter(filter === item.tag ? "" : item.tag)}
            >
              {item.label} {item.count}
            </button>
          ))}
        </div>
      )}
      {!jobId && commonJob?.job_id && (
        <button
          className="iv-drill-job"
          onClick={() =>
            onUseJob(
              commonJob.job_id,
              [commonJob.company, commonJob.job_title]
                .filter(Boolean)
                .join(" · "),
            )
          }
          title="带上来源岗位的 JD、评估缺口与简历存档出变式题"
        >
          <Icon name="target" size={14} />
          选中的错题都来自「
          {[commonJob.company, commonJob.job_title].filter(Boolean).join(" · ")}
          」，带上它的 JD 重练
        </button>
      )}
      <div className="iv-drill-list">
        {!book && !error && (
          <div className="iv-state">
            正在整理错题本…
            <span className="spinner" />
          </div>
        )}
        {book && items.length === 0 && (
          <div className="iv-drill-empty">
            还没有可强化的错题。完成一场模拟面试，或在「真实复盘」导入逐字稿并生成报告后，答得不好的题会自动收进这里。
          </div>
        )}
        {book && items.length > 0 && groups.length === 0 && (
          <div className="iv-drill-empty">没有命中这项失分要素的错题。</div>
        )}
        {groups.map((group) => (
          <div key={group.key} className="iv-drill-group">
            <label className="iv-drill-group-head">
              <input
                type="checkbox"
                checked={group.items.every(isSelected)}
                onChange={() => toggleGroup(group)}
                aria-label={`选择「${group.label}」的全部错题`}
              />
              <strong>{group.label}</strong>
              <span className="tiny-label">
                {DRILL_SOURCE_NAMES[group.source]} · {date(group.at)} ·{" "}
                {group.items.length} 题
              </span>
            </label>
            {group.items.map((item) => (
              <label
                key={item.id}
                className={`iv-drill-item${isSelected(item) ? " selected" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={isSelected(item)}
                  disabled={!isSelected(item) && full}
                  onChange={() => toggleItem(item)}
                  aria-label={`重练：${item.question}`}
                />
                <span className="iv-drill-body">
                  <span className="iv-drill-q">
                    <span className="iv-drill-text">{item.question}</span>
                    {item.occurrences > 1 && (
                      <em
                        className="iv-drill-badge repeat"
                        title="同一道题在多份复盘里都被点了失分点"
                      >
                        {item.occurrences} 份复盘都出现
                      </em>
                    )}
                    {item.practice && (
                      <em
                        className={`iv-drill-badge ${item.practice.after === 0 ? "cleared" : "again"}`}
                        title={`最近一次重练（${date(item.practice.at)}）：缺失要素 上次 ${item.practice.before} 条 → 本次 ${item.practice.after} 条${item.practice.count > 1 ? `，共重练 ${item.practice.count} 次` : ""}`}
                      >
                        {item.practice.after === 0
                          ? `重练过 · 本次没再被点出缺失`
                          : `重练过 · 缺失要素 ${item.practice.before}→${item.practice.after}`}
                      </em>
                    )}
                  </span>
                  <span
                    className="iv-drill-problems"
                    title={item.problems.join("；")}
                  >
                    {item.problems.length ? (
                      <>
                        上次失分：{item.problems.slice(0, 2).join("；")}
                        {item.problems.length > 2
                          ? ` 等 ${item.problems.length} 条`
                          : ""}
                      </>
                    ) : (
                      "这道题没有被点出失分点，按原题考察点出变式题"
                    )}
                  </span>
                  {item.tags.length > 0 && (
                    <span className="iv-drill-tags">
                      {item.tags.slice(0, 3).map((tagId) => (
                        <i key={tagId}>{tagLabels[tagId] ?? tagId}</i>
                      ))}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </div>
        ))}
        {extra > 0 && (
          <p className="tiny-label iv-drill-extra">
            另有 {extra}{" "}
            道题不在错题本里（没被点过失分点），已按「重练此题」带入。
          </p>
        )}
      </div>
    </section>
  );
}
export function JobPicker({
  value,
  label,
  onSelect,
}: {
  value: string;
  label: string;
  onSelect: (id: string, label: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [focused, setFocused] = useState(false);
  const [results, setResults] = useState<
    { id: string; label: string; meta: string }[]
  >([]);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const tokenRef = useRef(0);

  useEffect(() => {
    const timer = setTimeout(() => {
      const ticket = ++tokenRef.current;
      const params = new URLSearchParams({ page: "1", page_size: "20" });
      if (query.trim()) params.set("q", query.trim());
      setLoading(true);
      api<{
        jobs: {
          id: string;
          title: string;
          company: string;
          salary?: string;
          location?: string;
        }[];
      }>(`/jobs?${params.toString()}`)
        .then((data) => {
          if (tokenRef.current !== ticket) return;
          setResults(
            data.jobs.map((item) => ({
              id: item.id,
              label: `${item.company} · ${item.title}`,
              meta: [item.salary, item.location].filter(Boolean).join(" ｜ "),
            })),
          );
        })
        .catch(() => {
          if (tokenRef.current === ticket) setResults([]);
        })
        .finally(() => {
          if (tokenRef.current === ticket) setLoading(false);
        });
    }, 250);
    return () => clearTimeout(timer);
  }, [query]);

  // 点外面才收下拉：列表项用 onMouseDown preventDefault，避免先失焦把选中吞掉。
  useEffect(() => {
    function onDocMouseDown(event: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(event.target as Node))
        setFocused(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, []);

  return (
    <div className="iv-picker" ref={boxRef}>
      <input
        className="iv-picker-input"
        value={focused ? query : label}
        placeholder="输入公司或岗位关键词检索（如：阿里 数据），空输入给最近岗位"
        onFocus={() => {
          setFocused(true);
          setQuery("");
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") setFocused(false);
        }}
        onChange={(event) => setQuery(event.target.value)}
        aria-label="检索目标岗位"
      />
      {focused && (
        <div
          className="iv-picker-list"
          role="listbox"
          aria-label="岗位检索结果"
        >
          {loading && results.length === 0 && (
            <div className="iv-picker-empty">检索中…</div>
          )}
          {!loading && results.length === 0 && (
            <div className="iv-picker-empty">
              没有匹配的岗位——可切「粘贴 JD」或「自由练」
            </div>
          )}
          {results.map((item) => (
            <button
              key={item.id}
              className={`iv-picker-item${item.id === value ? " active" : ""}`}
              title={item.label}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onSelect(item.id, item.label);
                setQuery("");
                setFocused(false);
              }}
            >
              <span>{item.label}</span>
              {item.meta && <em>{item.meta}</em>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
