import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'

// 使用教程（遮罩指引）：首次打开工作区自动弹出，每步高亮对应界面元素，可随时跳过；
// 看完或跳过后按版本记忆不再自动出现，侧栏底部「使用教程」可随时重看。
//
// 【维护约定】后续功能更新影响了教程里描述的流程、入口名称或指向的界面元素时，
// 必须同步修改下方 STEPS，并递增 TUTORIAL_VERSION：版本号变化会让看过旧版教程
// 的用户重新看到一次，避免教程与实际界面脱节。
export const TUTORIAL_VERSION = 9
const TUTORIAL_STORAGE_KEY = 'jobscope-tutorial-done'

// App 用它判断是否需要自动弹出；已看版本低于当前版本即视为「首次/改版后第一次」。
export function tutorialSeenVersion(): number {
  try { return Number(localStorage.getItem(TUTORIAL_STORAGE_KEY)) || 0 } catch { return 0 }
}
function markTutorialSeen() {
  try { localStorage.setItem(TUTORIAL_STORAGE_KEY, String(TUTORIAL_VERSION)) } catch { /* 存储不可用时仅本次会话生效 */ }
}

type TutorialStep = {
  selector?: string            // 要高亮的元素；缺省或找不到（含不在视口内）时显示居中卡片
  placement?: 'auto' | 'right' // right：卡片放目标右侧（适合窄高的侧栏目标）
  title: string
  body?: string
  list?: string[]
}

// 步骤只指向默认「岗位研究」页上稳定存在的元素（空档案的新用户也能看到）：
// 导入入口 + 侧栏四个导航分组（教程打开时 App 会展开全部分组）。
// 按实际使用流程排序：导入岗位 → 各功能区 → 评估适配闭环。
const STEPS: TutorialStep[] = [
  { title: '欢迎使用知根 JobScope', body: '这是一个本地岗位研究工作台：把岗位要求、公司证据和你的经历逐项核对，辅助投递决策。核心流程三步：导入岗位 → 保存简历与偏好 → 评估适配。接下来用 7 步带你过一遍，可随时跳过。' },
  { selector: '.job-entry-actions', title: '第一步：把岗位收进档案', body: '岗位研究的起点，三种导入方式：', list: ['从 Boss 导入：捕获你在项目浏览器里看到的页面，或粘贴分享链接', '粘贴已有岗位 JD：把手头现成的职位描述存为档案', '查找外部岗位：从外部数据源检索并采集。所有档案保留原文与出处'] },
  { selector: '#nav-group-research', placement: 'right', title: '岗位研究：概览与档案', list: ['今日概览：每天的第一屏——投递待办、近 7 天新岗位与备份提醒', '岗位档案：筛选、批量编辑、星标收藏，卡片点开即详情', '收藏：投递管线跟进（意向 / 已投递 / 面试中…），可反查刷新过期数据', '模拟面试：AI 面试官按岗位画像逐题追问（评估缺口=追问重点），结束后给逐题复盘与好差答案对比；页内还有「真实复盘」——把外部 ASR 转写的真面试逐字稿粘进来（自带「候选人/面试官」前缀的标准格式直接导入，免模型标注），核对说话人后生成结合 JD 的复盘报告', '反向JD测评：48 组真实 JD 片段二选一，反推你的隐性筛选条件——报告给的是每类片段的选中率差异和优先/回避筛选关键词，不是人格标签', '失败博物馆：面试复盘页底部的可控/不可控归因标注，跨场次重复失分点在这里自动点名并给下一场准备建议；不可控因素只定位不评判', '公司资料库：公司对话与背景资料沉淀', '数据分析：把多份岗位放进同一张表横评对比'] },
  { selector: '#nav-group-profile', placement: 'right', title: '个人材料：简历与偏好申报', list: ['我的简历：保存投递版简历，是评估适配的依据', '个人资料库：存完整版材料（项目 / 工作 / 技能 / 证书 / 空窗经历…）；其中「求职偏好」类可提前申报硬性条件——到岗方式、排班作息、薪资期望、学历门槛等，启用后评估适配会自动作为已确认事实判上；「空窗经历」类供空窗期叙事生成与面试专题取材', '简历工坊：从资料库取材，针对岗位产出优化简历', '生存跑道：月支出 / 存款 / 社保 / 失业金的私人参考面板——只存本机、不进模型分析，金额不出本页；Offer 对比旁有跳转入口'] },
  { selector: '#nav-group-dialogue', placement: 'right', title: '对话与知识', list: ['全局对话：跨岗位、跨公司检索项目资料提问，回答标注公司与出处', '知识库：沉淀你自己的参考材料，所有对话范围可用'] },
  { selector: '#nav-group-manage', placement: 'right', title: '系统管理', list: ['数据与采集：外部岗位检索；Boss 等需登录平台先在这里打开专用浏览器登录，再用浏览器入口采集', '设置中心：首次使用先在「模型设置」配置分析模型', '运行日志：排查采集与分析问题的第一现场'] },
  { title: '第二步：评估适配', body: '选中一份岗位档案，点「评估适配」：系统按 JD 逐项对照你的简历与偏好，给出满足 / 部分满足 / 未满足 / 待确认。「待确认」项可在判断列手动改判，按你的确认重算并跨重评沿用。岗位详情里还有求职参考（公司背景评分）与一键完备（自动收集公司资料）。' },
  { title: '准备好了', body: '分数仅供辅助比较，不代表录用概率；所有资料保留原始出处，未知就留作未知。左侧导航随时可折叠，本教程可在侧栏底部「使用教程」重看。' },
]

const CARD_WIDTH = 380

// 按目标位置摆卡片：优先目标下方，空间不足放上方，水平夹在视口内；placement=right 放右侧。
function cardPosition(rect: DOMRect, placement: 'auto' | 'right'): CSSProperties {
  const width = Math.min(CARD_WIDTH, window.innerWidth - 32)
  const estimatedHeight = 250
  const clampTop = (top: number) => Math.min(Math.max(16, top), Math.max(16, window.innerHeight - estimatedHeight - 16))
  if (placement === 'right' && rect.right + width + 42 <= window.innerWidth) {
    return { top: clampTop(rect.top + rect.height / 2 - estimatedHeight / 2), left: rect.right + 14, width }
  }
  const left = Math.min(Math.max(16, rect.left + rect.width / 2 - width / 2), window.innerWidth - width - 16)
  if (rect.bottom + estimatedHeight + 24 <= window.innerHeight || rect.bottom <= window.innerHeight / 2) {
    return { top: clampTop(rect.bottom + 14), left, width }
  }
  return { top: clampTop(rect.top - estimatedHeight - 14), left, width }
}

export default function Tutorial({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [index, setIndex] = useState(0)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const step = STEPS[index]
  const last = index === STEPS.length - 1

  // 打开即记版本：看过就是看过，中途刷新/关闭也不再自动弹出（重看走侧栏入口）。
  useEffect(() => {
    if (!open) return
    setIndex(0)
    markTutorialSeen()
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = previousOverflow }
  }, [open])

  // Escape 跳过教程；capture 阶段拦截，避免按键漏给底层弹窗逻辑。
  useEffect(() => {
    if (!open) return
    const onKey = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, onClose])

  const measure = useCallback(() => {
    const target = step.selector ? document.querySelector<HTMLElement>(step.selector) : null
    if (!target) { setRect(null); return }
    const box = target.getBoundingClientRect()
    // 元素不在当前视口（被折叠、移动端隐藏的侧栏等）时回退为居中卡片，不给屏幕外目标画框。
    const visible = box.width > 0 && box.height > 0 && box.bottom > 0 && box.top < window.innerHeight && box.right > 0 && box.left < window.innerWidth
    setRect(visible ? box : null)
  }, [step.selector])

  // 换步、窗口尺寸变化、页面/内部面板滚动时，高亮框跟随目标重算。
  useLayoutEffect(() => {
    if (!open) return
    const target = step.selector ? document.querySelector<HTMLElement>(step.selector) : null
    target?.scrollIntoView({ block: 'nearest' })
    measure()
  }, [open, index, measure])
  useEffect(() => {
    if (!open) return
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => { window.removeEventListener('resize', measure); window.removeEventListener('scroll', measure, true) }
  }, [open, measure])

  if (!open) return null
  const style = rect ? cardPosition(rect, step.placement ?? 'auto') : null
  const stop = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }
  return <div className="tutorial-overlay" role="dialog" aria-modal="true" aria-label="使用教程" onKeyDown={stop}>
    {!rect && <div className="tutorial-dim" />}
    {rect && <div className="tutorial-highlight" style={{ top: rect.top - 6, left: rect.left - 6, width: rect.width + 12, height: rect.height + 12 }} />}
    <section className={`tutorial-card${rect ? '' : ' centered'}`} style={style ?? undefined}>
      <h2>{step.title}</h2>
      {step.body && <p>{step.body}</p>}
      {step.list && <ul>{step.list.map(item => <li key={item}>{item}</li>)}</ul>}
      <div className="tutorial-footer">
        <span className="tutorial-progress">{index + 1} / {STEPS.length}</span>
        <div className="tutorial-actions">
          {!last && <button className="text-button" onClick={onClose}>跳过教程</button>}
          {index > 0 && <button className="button secondary small" onClick={() => setIndex(value => value - 1)}>上一步</button>}
          <button className="button primary small" autoFocus onClick={() => { if (last) onClose(); else setIndex(value => value + 1) }}>{last ? '开始使用' : '下一步'}</button>
        </div>
      </div>
    </section>
  </div>
}
