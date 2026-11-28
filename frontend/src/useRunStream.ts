import { useEffect } from "react";

type RunStreamEvent = (event: string, data: Record<string, unknown>) => void;

/**
 * 后台 run 的 SSE 订阅收尾纪律，单一来源（2026-11-28 自五处手写收敛；审计 P1-3 前端侧）：
 * - runId 变化建流 / 清空或卸载时关流；
 * - done/error 帧的业务处理在 onEvent（站点自行清 runId，cleanup 顺带关流）；
 * - 致命断流（readyState === CLOSED，后端重启或流 404）不会再有任何帧——连 error/end
 *   都没有，onFatal 就地按失败收尾；短暂网络抖动（CONNECTING）交给 EventSource
 *   自动重连续播事件日志。
 *
 * 依赖刻意只有 runId：onEvent/onFatal 每轮渲染重建，重订阅会打断进行中的流（既有
 * 各站点同款约定）。命令式管理流的三处不适用本钩子：面试间（streamRef + attach）、
 * 评估适配（代次守卫 + onerror 探状态）、对话主流（审批/取消协议）。
 */
export function useRunStream(
  runId: string | "",
  open: (runId: string, onEvent: RunStreamEvent) => EventSource,
  onEvent: RunStreamEvent,
  onFatal: () => void,
) {
  useEffect(() => {
    if (!runId) return;
    const source = open(runId, onEvent);
    source.onerror = () => {
      if (source.readyState !== EventSource.CLOSED) return;
      onFatal();
    };
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);
}
