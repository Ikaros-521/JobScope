import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { toast } from "../toast";
import { EvidenceCard } from "../EvidenceCard";
import { Pagination } from "../Pagination";
import { emptyPage } from "../Pagination";
import type { Evidence, PageInfo } from "../api";

// 详情页「证据资料」子列表：按岗位拉取证据并分页（自 JobResearch.tsx 拆出）。
export function JobEvidence({
  jobId,
  focus = "",
}: {
  jobId: string;
  focus?: string;
}) {
  const [items, setItems] = useState<Evidence[]>([]);
  const [info, setInfo] = useState<PageInfo>(emptyPage);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);
  async function load(page = 1) {
    const token = ++request.current;
    setBusy(true);
    try {
      const data = await api<PageInfo & { evidence: Evidence[] }>(
        `/jobs/${encodeURIComponent(jobId)}/evidence?page=${page}&page_size=10`,
      );
      if (token === request.current) {
        setItems(data.evidence);
        setInfo(data);
      }
    } catch (err) {
      if (token === request.current) toast((err as Error).message, "error");
    } finally {
      if (token === request.current) setBusy(false);
    }
  }
  useEffect(() => {
    void load();
    return () => {
      request.current++;
    };
  }, [jobId]);
  // 求职参考里的「出处」跳到这里：命中那条证据时展开并滚过去，让它自己开口说来源。
  useEffect(() => {
    if (!focus) return;
    const target = document.getElementById(`evidence-entry-${focus}`);
    if (!target) return;
    target.querySelector("details")?.setAttribute("open", "");
    target.scrollIntoView({ block: "center" });
  }, [focus, items]);
  return (
    <>
      {items.map((item) => (
        <div
          key={item.id}
          id={`evidence-entry-${item.id}`}
          className={item.id === focus ? "evidence-focus" : undefined}
        >
          <EvidenceCard evidence={item} />
        </div>
      ))}
      <Pagination
        info={info}
        label="岗位证据"
        busy={busy}
        onPage={(page) => void load(page)}
      />
    </>
  );
}
