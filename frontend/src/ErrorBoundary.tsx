import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Icon } from "./Icon";

// 根级错误边界（2026-11-27 审计 P0）：此前任一视图渲染抛错会卸载整棵树白屏。
// 只兜渲染期异常；事件回调里的异常仍走各处 toast，不被这里吞掉。
// 「尝试恢复」清掉边界状态重挂 App（视图状态会重置）；数据都在本机，刷新同样安全。
type BoundaryState = { error: Error | null };

export class ErrorBoundary extends Component<
  { children: ReactNode },
  BoundaryState
> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("视图渲染崩溃", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="error-boundary">
        <div className="paper-panel error-boundary-panel">
          <div className="empty">
            <div className="empty-icon">
              <Icon name="museum" />
            </div>
            <h3>界面渲染出错了</h3>
            <p>
              {this.state.error.message || "未知错误"}
              。数据都在本机没有丢失——先试「尝试恢复」，不行再刷新页面。
            </p>
            <div className="today-empty-actions">
              <button
                className="button secondary"
                onClick={() => this.setState({ error: null })}
              >
                尝试恢复
              </button>
              <button
                className="button primary"
                onClick={() => location.reload()}
              >
                刷新页面
                <Icon name="source" size={15} />
              </button>
            </div>
          </div>
        </div>
      </main>
    );
  }
}
