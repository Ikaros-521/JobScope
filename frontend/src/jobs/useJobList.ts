import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Job, PageInfo } from "../api";
import { jobQuery } from "./shared";
import { emptyPage } from "../Pagination";

// 岗位列表状态线程（2026-11-28 自 JobResearch.tsx 抽出）：筛选、分页、竞态令牌与
// 本地行级补丁。跨视图信号（favoritesTick/refreshTick）仍由组件消费后调 loadJobs。
export type JobFilters = {
  q: string;
  location: string;
  source: string;
  salaryMin: string;
  salaryMax: string;
  starred: boolean;
};

export function useJobList(options: {
  onTotalChange: (total: number) => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsInfo, setJobsInfo] = useState<PageInfo>(emptyPage);
  const [jobsTotal, setJobsTotal] = useState(0);
  // 首次岗位列表未回前保持骨架屏：App 的全局 loading 只覆盖元数据，列表另有自己的就绪位。
  const [jobsLoading, setJobsLoading] = useState(true);
  const jobFilters = useRef<JobFilters>({
    q: "",
    location: "",
    source: "",
    salaryMin: "",
    salaryMax: "",
    starred: false,
  });
  const jobsRequest = useRef(0);

  const loadJobs = useCallback(
    async (filters = jobFilters.current, page = 1) => {
      jobFilters.current = filters;
      const params = jobQuery(filters);
      params.set("page", String(page));
      const token = ++jobsRequest.current;
      const data = await api<PageInfo & { jobs: Job[] }>(
        `/jobs?${params.toString()}`,
      );
      if (token === jobsRequest.current) {
        setJobs(data.jobs);
        setJobsInfo(data);
        const active = filters;
        if (
          !active.q &&
          !active.location &&
          !active.source &&
          !active.salaryMin &&
          !active.salaryMax &&
          !active.starred
        ) {
          setJobsTotal(data.total);
          options.onTotalChange(data.total);
        }
      }
      return data.jobs;
    },
    [options.onTotalChange],
  );

  useEffect(() => {
    void loadJobs().finally(() => setJobsLoading(false));
  }, [loadJobs]);

  // 行级补丁（星标/管线状态/跟进信息）：乐观更新列表条目，详情侧由组件同步。
  function patchLocalJob(id: string, patch: Partial<Job>) {
    setJobs((list) =>
      list.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry)),
    );
  }

  return {
    jobs,
    jobsInfo,
    jobsTotal,
    jobsLoading,
    jobFilters,
    loadJobs,
    patchLocalJob,
  };
}
