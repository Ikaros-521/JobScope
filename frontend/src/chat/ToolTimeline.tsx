import { readable } from '../api'
import type { ToolStep } from '../api'

const toolNames: Record<string, string> = {
  index_sync: '同步资料索引', query_plan: '理解问题与范围', hybrid_search: '检索证据', supplemental_search: '补充检索',
  salary_query: '查询全部符合条件的岗位', answer: '生成并校验回答', agent_model: '模型思考 / 选择工具',
  project_search: '检索项目资料', project_read: '读取证据正文', project_salary: '查询全库岗位薪资',
  resume_read: '读取已确认简历事实', skill_read: '加载 Skill 指令', skill_resource: '读取 Skill 资源',
  tool_approval: '等待工具确认', tool_denied: '用户拒绝调用', tool_error: '工具调用失败',
}

function stepName(step: ToolStep) {
  return step.label || toolNames[step.tool] || step.tool
}

function StepRow({ step, open }: { step: ToolStep; open: boolean }) {
  const failed = step.status === 'error'
  const waiting = step.status === 'waiting'
  return <details className="tool-step" open={open}>
    <summary>
      <span className={`tool-step-dot ${failed ? 'error' : waiting ? 'waiting' : 'ok'}`} aria-hidden="true" />
      <span className="tool-step-name">{stepName(step)}</span>
      <span className="tool-step-meta">
        {failed ? '失败 · ' : ''}{step.selected != null ? `${step.selected}份证据 · ` : ''}{step.matched_jobs != null ? `${step.matched_jobs}个岗位 · ` : ''}{step.duration_ms < 1 ? '<1' : Math.round(step.duration_ms)} ms
      </span>
    </summary>
    {(step.arguments != null || step.result_preview) && <div className="tool-step-body">
      {step.arguments != null && <div><b>调用参数</b><pre>{JSON.stringify(step.arguments, null, 2)}</pre></div>}
      {step.result_preview && <div><b>返回摘要</b><pre>{readable(step.result_preview)}</pre></div>}
    </div>}
  </details>
}

export function ToolTimeline({ steps, running }: { steps: ToolStep[]; running?: boolean }) {
  if (!steps.length) return null
  return <details className="tool-timeline" open={running}>
    <summary>
      <span>{running ? '正在执行' : '执行过程'}</span>
      <span className="tool-timeline-meta">{steps.length} 次调用{running && <span className="spinner" aria-hidden="true" />}</span>
    </summary>
    <div className="tool-timeline-body">{steps.map(step => <StepRow key={step.step} step={step} open={false} />)}</div>
  </details>
}
