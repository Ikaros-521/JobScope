import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, RadarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

// 按需注册：横评页只用条形图与雷达图，全量 echarts 不打进 bundle。
echarts.use([BarChart, RadarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

export type ChartOption = echarts.EChartsCoreOption

/** 单实例封装：init 只跑一次，option 变更走 setOption，容器尺寸变化跟着 resize。 */
export function EChart({ option, className }: { option: ChartOption; className?: string }) {
  const holder = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    if (!holder.current) return
    chart.current = echarts.init(holder.current)
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(holder.current)
    return () => { observer.disconnect(); chart.current?.dispose(); chart.current = null }
  }, [])
  useEffect(() => { chart.current?.setOption(option, true) }, [option])
  return <div className={className} ref={holder} />
}
