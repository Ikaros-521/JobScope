import { useState } from "react";
import type { ReactNode } from "react";
import { api, date, isStale, plain, safeUrl } from "./api";
import type { Evidence } from "./api";
import { sourceNames } from "./JobCard";

export const kindNames: Record<string, string> = {
  job_posting: "岗位原文",
  disclosure_metadata: "公告索引",
  disclosure: "披露正文",
  company_statement: "企业自述",
  employee_statement: "员工自述",
  user_material: "用户材料",
  media_report: "媒体报道",
  announcement: "公告",
  web_company_profile: "企业信息快照",
  llm_judgment: "模型研判",
  local_computation: "本机计算",
  paid_api_record: "付费接口记录",
};

// 证据来源中文名（2026-11-27）：岗位来源沿用 JobCard 的映射，补上可靠性信号源——
// 此前 tyc_web / jobscope_jobs / llm_industry 这类内部键原样露给用户。
export const evidenceSourceNames: Record<string, string> = {
  ...sourceNames,
  qcc_web: "企查查",
  tyc_web: "天眼查",
  aiqicha_web: "爱企查",
  qcc: "企查查开放平台",
  tyc: "天眼查开放平台",
  gsxt: "国家企业信用信息公示系统",
  credit_china: "信用中国",
  court_records: "中国裁判文书网",
  jobscope_jobs: "本机岗位档案",
  llm_industry: "AI 行业研判",
  local: "本机计算",
  manual: "手动补充",
  official: "官方渠道",
  upload: "上传文档",
  knowledge: "知识库",
};

// 信号证据的标题是「公司名 规则键」（reliability/service.py persist_outcome 用 item['rule']
// 拼接），展示时把尾部的规则键换成中文名——历史落库的英文标题也在展示层一并矫正。
const RULE_LABELS: Record<string, string> = {
  company_age: "成立年限",
  insured_headcount_trend: "参保人数走势",
  judicial_case_ratio: "司法案件密度",
  soe_status: "国资背景",
  credit_risk: "信用风险",
  admin_penalty: "行政处罚",
  registration_status: "登记状态",
  salary_quote_coverage: "薪资报价覆盖",
  pay_vs_city_median: "薪资与城中位对比",
  jd_specificity: "JD 具体度",
  team_size_statement: "团队规模自述",
  disclosure_revenue_trend: "营收趋势",
  disclosure_headcount_trend: "披露人数趋势",
  employee_review_sample: "员工评价样本",
  disclosure_available: "披露可得性",
  industry_media_count: "行业媒体声量",
  industry_trend_llm: "行业趋势研判",
};
export function friendlyTitle(title: string): string {
  const match = / ([a-z_0-9]+)$/.exec(title);
  return match && RULE_LABELS[match[1]]
    ? title.slice(0, match.index) + " " + RULE_LABELS[match[1]]
    : title;
}

export function searchTerms(query: string) {
  return query.trim().split(/\s+/).filter(Boolean);
}

function escapePattern(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function highlight(text: string, words: string[]): ReactNode {
  if (!words.length) return text;
  return text
    .split(new RegExp(`(${words.map(escapePattern).join("|")})`, "gi"))
    .map((part, index) =>
      index % 2 ? (
        <mark className="search-hit" key={index}>
          {part}
        </mark>
      ) : (
        part
      ),
    );
}

// 命中位置常在长报告中段，取命中处的窗口文字，避免逐条展开才能确认
export function excerpt(text: string, words: string[]) {
  const positions = words
    .map((word) => text.toLowerCase().indexOf(word.toLowerCase()))
    .filter((index) => index >= 0);
  if (!positions.length) return "";
  const at = Math.min(...positions);
  const start = Math.max(0, at - 60);
  const end = Math.min(text.length, at + 200);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`.replace(
    /\s+/g,
    " ",
  );
}

export function EvidenceCard({
  evidence,
  query = "",
}: {
  evidence: Evidence;
  query?: string;
}) {
  const url = safeUrl(evidence.url);
  const words = searchTerms(query);
  // 列表接口对长正文做了裁剪：展开时按需取全文；摘要窗口优先用后端基于全文算出的 text_hit
  // （命中可能落在长报告中段，裁剪后的本地 text 找不到）。
  const [fullText, setFullText] = useState<string | null>(null);
  const [fetching, setFetching] = useState(false);
  const content = plain(fullText ?? evidence.text);
  const hit = evidence.text_hit || excerpt(content, words);
  async function ensureFull(event: React.SyntheticEvent<HTMLDetailsElement>) {
    if (
      !event.currentTarget.open ||
      !evidence.text_truncated ||
      fullText !== null ||
      fetching
    )
      return;
    setFetching(true);
    try {
      const data = await api<{ evidence: Evidence }>(
        `/evidence/${encodeURIComponent(evidence.id)}`,
      );
      setFullText(data.evidence.text);
    } catch {
      /* 取全文失败：展开区保留已裁剪的正文 */
    } finally {
      setFetching(false);
    }
  }
  return (
    <details className="evidence-card" onToggle={ensureFull}>
      <summary>
        <div>
          <strong>{highlight(friendlyTitle(evidence.title), words)}</strong>
          <span>
            {evidenceSourceNames[evidence.source] || evidence.source} ·{" "}
            {date(evidence.published_at || evidence.collected_at)} ·{" "}
            {kindNames[evidence.kind] || evidence.kind}
          </span>
          {hit && <span className="evidence-hit">{highlight(hit, words)}</span>}
        </div>
        <span className="expand-sign" aria-hidden="true">
          +
        </span>
      </summary>
      <div className="evidence-body">
        {evidence.text_truncated && fullText === null && fetching ? (
          <p className="muted">正在读取全文…</p>
        ) : (
          <p>{highlight(content, words)}</p>
        )}
        <div className="evidence-meta">
          <span>证据 {evidence.id}</span>
          {url ? (
            <a
              className="external-link"
              href={url}
              target="_blank"
              rel="noreferrer"
            >
              出处
            </a>
          ) : (
            <span>未提供原始链接</span>
          )}
        </div>
        <div className="tiny-label">
          采集于 {date(evidence.collected_at)}
          {!evidence.published_at && " · 原文发布时间未知"}
          {isStale(evidence.collected_at, 90) &&
            " · 采集已超过 90 天，公司情况可能已变化，建议重采或核对原文"}
        </div>
      </div>
    </details>
  );
}
