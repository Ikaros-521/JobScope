import type { Job } from "../api";

// 岗位研究页的纯助手：检索参数序列化与来源展示名（2026-11-28 自 JobResearch.tsx 拆出）。
export function jobQuery(filters: {
  q: string;
  location: string;
  source: string;
  salaryMin: string;
  salaryMax: string;
  starred?: boolean;
}) {
  const params = new URLSearchParams({
    q: filters.q,
    location: filters.location,
    source: filters.source,
  });
  if (filters.salaryMin)
    params.set(
      "salary_min",
      String(Math.round(Number(filters.salaryMin) * 1000)),
    );
  if (filters.salaryMax)
    params.set(
      "salary_max",
      String(Math.round(Number(filters.salaryMax) * 1000)),
    );
  if (filters.starred) params.set("starred", "true");
  return params;
}
export const transportNames: Record<string, string> = {
  http_api: "接口",
  browser_page: "需浏览器",
  company_web: "网页快查",
  manual: "手动",
};
export function needsFullJd(job: Job) {
  return (
    job.description_status === "listing" ||
    (["boss", "zhaopin"].includes(job.source) &&
      job.description_status !== "complete")
  );
}
