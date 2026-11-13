import type { ReactNode } from 'react'

// 从 App.tsx 抽出的图标表：岗位卡片（JobCard）与收藏页也要用图标，
// 放独立文件避免 App→JobCard→App 的循环导入。新增图标直接加在 paths 里。
const paths: Record<string, ReactNode> = {
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
  jobs: <><rect x="3" y="7" width="18" height="14" rx="2" /><path d="M8 7V4h8v3M3 12h18M10 12v3h4v-3" /></>,
  resume: <><path d="M14 3H5v18h14V8Z" /><path d="M14 3v5h5M8 12h8M8 16h6" /></>,
  source: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0" /></>,
  arrow: <><path d="M5 12h14M14 7l5 5-5 5" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  external: <><path d="M14 4h6v6M20 4l-9 9M10 4H4v16h16v-6" /></>,
  pin: <><path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z" /><circle cx="12" cy="10" r="2" /></>,
  chat: <path d="M21 4H3v14h5l4 4v-4h9Z" />,
  check: <path d="m5 12 4 4L19 6" />,
  settings: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="currentColor" /><circle cx="15" cy="17" r="3" fill="currentColor" /></>,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  filter: <><path d="M4 7h16M7 12h10M10 17h4" /></>,
  chevron: <path d="m9 5 7 7-7 7" />,
  book: <><path d="M12 5C8 2 3 3 3 3v16s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2ZM12 5v16" /></>,
  globe: <><circle cx="12" cy="12" r="9" /><ellipse cx="12" cy="12" rx="4" ry="9" /><path d="M3 12h18" /></>,
  chart: <><path d="M4 20h16" /><path d="M7 16v-5M12 16V5M17 16v-8" /></>,
  wand: <><path d="M4 20 13.5 10.5" /><path d="m15 3.5.8 1.7 1.7.8-1.7.8-.8 1.7-.8-1.7-1.7-.8 1.7-.8Z" /><path d="m19.5 9.5.5 1.2 1.2.5-1.2.5-.5 1.2-.5-1.2-1.2-.5 1.2-.5Z" /></>,
  star: <path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1.1 5.8L12 16.9l-5.3 2.7 1.1-5.8-4.3-4.1 5.9-.8Z" />,
}

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.book}</svg>
}
