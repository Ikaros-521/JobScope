import { date } from './api'
import type { Job } from './api'
import { Icon } from './Icon'

// 来源中文名：档案页用动态注册表，卡片这里用静态子集即可（新来源进来先显示原名，不出错）。
const sourceNames: Record<string, string> = {
  boss: 'BOSS直聘', zhaopin: '智联招聘', '51job': '前程无忧', liepin: '猎聘',
  remotive: 'Remotive', user_import: '用户导入', cninfo: '巨潮资讯',
}

// 岗位卡：岗位档案与收藏夹两个列表共用同一份卡片。
// 卡片本体（勾选框 + 摘要 + 星标）在这里；两个视图各自附加的操作行经 actions/footer 插槽传入。
export function JobCard({ item, selected, checked, busy, onToggleSelection, onOpen, onToggleStar, actions, footer }: {
  item: Job
  selected: boolean
  checked: boolean
  busy?: boolean
  onToggleSelection: (id: string) => void
  onOpen: (id: string) => void
  onToggleStar: (item: Job) => void
  actions?: React.ReactNode
  footer?: React.ReactNode
}) {
  return <div className={`job-card ${selected ? 'selected' : ''}${checked ? ' checked' : ''}`}>
    <label className="job-card-check"><input type="checkbox" checked={checked} onChange={() => onToggleSelection(item.id)} /><span className="sr-only">选择岗位：{item.title} · {item.company}</span></label>
    <button className="job-card-body" aria-pressed={selected} onClick={() => onOpen(item.id)} title={`${item.title} · ${item.company}`}>
      <h3>{item.title}</h3>
      <span className="job-company">{item.company}</span>
      <div className="job-card-meta"><span className="job-salary">{item.salary || '薪资未披露'}</span><span className="job-location"><Icon name="pin" size={12} />{item.location || '地点待确认'}</span>{(item.tags || []).slice(0, 1).map(tag => <span className="job-tags" key={tag}><span>{tag}</span></span>)}</div>
      <div className="job-card-bottom"><span className="job-source">{sourceNames[item.source] || item.source}</span><time>{date(item.updated_at)}</time></div>
    </button>
    <button type="button" className={`job-card-star${item.starred ? ' starred' : ''}`} aria-pressed={Boolean(item.starred)} disabled={Boolean(busy)}
      title={item.starred ? '取消收藏' : '收藏岗位，可稍后在收藏夹反查与横评'}
      onClick={event => { event.stopPropagation(); onToggleStar(item) }}>
      <Icon name="star" size={15} />
    </button>
    {(actions || footer) && <div className="job-card-extra">{actions}{footer}</div>}
  </div>
}
