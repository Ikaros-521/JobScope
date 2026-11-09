import type { PageInfo } from './api'

export const emptyPage: PageInfo = { page: 1, page_size: 10, total: 0, pages: 1 }

export function Pagination({ info, label, busy, onPage }: { info: PageInfo; label: string; busy?: boolean; onPage: (page: number) => void }) {
  const start = info.total ? (info.page - 1) * info.page_size + 1 : 0
  const end = Math.min(info.page * info.page_size, info.total)
  return <nav className="pagination" aria-label={`${label}分页`}>
    <span aria-live="polite">{start}–{end} / {info.total} 条</span>
    <div><button aria-label={`${label}上一页`} disabled={busy || info.page <= 1} onClick={() => onPage(info.page - 1)}>上一页</button>
      <span>{info.page} / {info.pages} 页</span>
      <button aria-label={`${label}下一页`} disabled={busy || info.page >= info.pages} onClick={() => onPage(info.page + 1)}>下一页</button></div>
  </nav>
}
