// 视图与导航的共享定义（2026-11-19 自 App.tsx 拆出）：App 壳做路由与侧栏，
// JobResearch 等工作区组件需要引用同一套视图集合与深链解析，放独立模块避免循环导入。

export type ViewId = 'today' | 'jobs' | 'favorites' | 'resume' | 'profile' | 'workshop' | 'sources' | 'evidence' | 'compare' | 'interview' | 'global' | 'knowledge' | 'logs' | 'settings'
export type SettingsTab = 'model' | 'system' | 'extensions'

export const NAV_GROUPS: { id: string; label: string; items: { id: ViewId; icon: string; label: string }[] }[] = [
  { id: 'research', label: '岗位研究', items: [
    { id: 'today', icon: 'sun', label: '今日概览' },
    { id: 'jobs', icon: 'jobs', label: '岗位研究' },
    { id: 'favorites', icon: 'star', label: '收藏' },
    { id: 'interview', icon: 'mic', label: '模拟面试' },
    { id: 'evidence', icon: 'book', label: '公司资料库' },
    { id: 'compare', icon: 'chart', label: '数据分析' },
  ] },
  { id: 'dialogue', label: '对话与知识', items: [
    { id: 'global', icon: 'chat', label: '全局对话' },
    { id: 'knowledge', icon: 'knowledge', label: '知识库' },
  ] },
  { id: 'profile', label: '个人材料', items: [
    { id: 'resume', icon: 'resume', label: '我的简历' },
    { id: 'profile', icon: 'book', label: '个人资料库' },
    { id: 'workshop', icon: 'wand', label: '简历工坊' },
  ] },
  { id: 'manage', label: '系统管理', items: [
    { id: 'sources', icon: 'source', label: '数据与采集' },
    { id: 'logs', icon: 'menu', label: '运行日志' },
    { id: 'settings', icon: 'settings', label: '设置中心' },
  ] },
]

export const NAV_STORAGE_KEY = 'jobscope-nav-expanded'

// 视图级深链（P1，2026-11-19）：#/<view> 双向同步——刷新/书签保位，浏览器回退走回上个视图。
// 用 #/ 前缀避开与页面内元素 id 的锚点冲突；不认识的 hash 落回今日概览。
export const VIEW_IDS: readonly ViewId[] = NAV_GROUPS.flatMap(group => group.items.map(item => item.id))
export function viewFromHash(): ViewId | null {
  const hash = window.location.hash
  if (!hash.startsWith('#/')) return null
  const candidate = hash.slice(2).split('?')[0]
  return (VIEW_IDS as readonly string[]).includes(candidate) ? candidate as ViewId : null
}
// 视图级深链可带查询参数（如 #/interview?job=<id> 预选岗位开模拟面试）；仅同视图刷新/回退时有意义。
export function hashQuery(): URLSearchParams {
  const hash = window.location.hash
  const query = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : ''
  return new URLSearchParams(query)
}
