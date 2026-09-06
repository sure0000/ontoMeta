import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Result, Typography } from "antd";

/**
 * 渲染异常的兜底。
 *
 * **为什么必须有**：React 的默认行为是「渲染期抛出未被捕获的异常 → 卸载整棵树」，
 * 也就是整页白屏，控制台之外没有任何线索。本仓 104 个组件、45k 行前端此前一个边界
 * 都没有，最可能的入口是图谱页拿到畸形数据、或详情页读到后端新增/改名的字段。
 *
 * 边界打两层（见 App.tsx）：外层裹住整个应用兜底，内层按路由裹住页面——这样一个页面
 * 崩掉时侧边栏与导航还在，用户能自己走开，而不是只剩一块白。
 */

type Props = {
  children: ReactNode;
  /** 出现在标题里，让用户知道是哪一块崩了（"业务地图"）。缺省为泛指。 */
  scope?: string;
  /** 换 key 时重置边界。路由切换传 pathname，换页即自动恢复。 */
  resetKey?: string;
};

type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留原始堆栈：Result 上只给一句人话，排查要的东西在控制台。
    console.error("[ErrorBoundary]", this.props.scope ?? "app", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  private handleRetry = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    const scope = this.props.scope;
    return (
      <Result
        status="error"
        title={scope ? `${scope}加载失败` : "页面加载失败"}
        subTitle="这一部分没能渲染出来。可以重试；如果反复出现，请把下面这行信息一起反馈。"
        extra={[
          <Button type="primary" key="retry" onClick={this.handleRetry}>
            重试
          </Button>,
          <Button key="reload" onClick={() => window.location.reload()}>
            刷新页面
          </Button>,
        ]}
      >
        <Typography.Paragraph
          copyable={{ text: `${error.name}: ${error.message}` }}
          type="secondary"
          style={{ marginBottom: 0, fontSize: 12 }}
        >
          {error.name}: {error.message}
        </Typography.Paragraph>
      </Result>
    );
  }
}
