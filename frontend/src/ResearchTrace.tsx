import type { ResearchRun, SalaryMetrics } from './api'

const toolNames: Record<string, string> = { index_sync: '同步资料索引', query_plan: '理解问题与范围', hybrid_search: '检索证据', supplemental_search: '补充检索', salary_query: '查询全部符合条件的岗位', answer: '生成并校验回答', agent_model: '模型选择工具或生成回答', project_search: '检索项目资料', project_read: '读取证据正文', project_salary: '查询全库岗位薪资', resume_read: '读取已确认简历事实', skill_read: '加载 Skill 指令', skill_resource: '读取 Skill 资源', tool_approval: '等待工具确认', tool_denied: '用户拒绝调用', tool_error: '工具调用失败' }
export function ResearchTrace({ run, metrics }: { run?: ResearchRun; metrics?: SalaryMetrics | null }) {
  if (!run) return null
  const plan = run.plan
  return <div className="research-result">
    {metrics && <section className="salary-metrics" aria-label="薪资查询结果"><div><strong>{metrics.matched_jobs}</strong><span>符合条件的岗位</span></div><div><strong>{metrics.known_monthly_jobs}</strong><span>可解析月薪</span></div><div><strong>{metrics.unknown_monthly_jobs}</strong><span>未知或口径不支持</span></div><p>人民币 · 招聘税前月薪{metrics.threshold != null && ` · 按${metrics.threshold_basis === 'monthly_min' ? '区间下限' : '区间上限'}筛选`}{metrics.location && ` · ${metrics.location}`}{metrics.skills?.length ? ` · ${metrics.skills.join('、')}` : ''}。统计覆盖全部符合条件的岗位，展示样本最多8条。招聘报价不代表实际到手工资。</p></section>}
    <details className="research-trace"><summary><span>{run.strategy === 'agent' ? '查看 Agent 执行过程' : '查看检索过程'}</span><span>{run.strategy === 'agent' ? '工具调用循环' : run.strategy === 'hybrid' ? 'BM25 + 向量' : run.strategy === 'clarification' ? '需要澄清' : 'BM25 全文'} · {Math.round(run.duration_ms)} ms</span></summary><div className="trace-body">
      {plan && <p><b>独立问题：</b>{plan.query}</p>}<p><b>范围：</b>{run.scope.kind === 'global' ? '全项目资料' : run.scope.company}{plan?.companies?.length ? ` · 查询公司：${plan.companies.join('、')}` : ''}</p>
      <p><b>执行：</b>{run.strategy === 'agent' ? `模型自主选工具 · ${run.status === 'awaiting_approval' ? '等待确认' : run.status === 'completed' ? '已完成' : run.status}` : plan?.planner === 'model' ? '模型解析，参数经服务端校验' : '确定性领域规则'}{plan?.concepts?.length ? ` · ${plan.concepts.join(' / ')}` : ''}</p>
      <ol>{run.events.map(event => <li key={event.step}><span>{toolNames[event.tool] || event.label || event.tool}{event.tool === 'tool_approval' && event.label ? ` · ${event.label}` : ''}</span><span>{event.status === 'error' ? '失败 · ' : ''}{event.selected != null ? `${event.selected}份证据 · ` : ''}{event.matched_jobs != null ? `${event.matched_jobs}个岗位 · ` : ''}{event.duration_ms.toFixed(1)} ms</span></li>)}</ol>
      {run.degraded?.map((reason, index) => <p className="trace-degraded" key={index}>{reason}</p>)}
      <p className="tiny-label">工具上下文 {run.context_characters?.toLocaleString() || 0} / {(run.limits?.context_characters || 24000).toLocaleString()} 字符 · 检索结果不是全库穷尽审阅 · 未展示内部思维链</p><code>{run.id}</code>
    </div></details>
  </div>
}
