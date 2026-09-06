import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

/**
 * 边界本身要有测试，理由很直接：它是**只在别的东西坏掉时才运行**的代码。
 * 平时永远不执行，坏了也没人发现——直到真出事那天，白屏照旧。
 */

function Boom({ when = true }: { when?: boolean }) {
  if (when) throw new Error("图谱数据缺少 nodes 字段");
  return <div>正常内容</div>;
}

/** 边界工作时 React 必然向 console.error 写一大段；静音掉，别淹没真失败。 */
function silenceReactErrorLog() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

describe("ErrorBoundary", () => {
  it("放行没有异常的子树", () => {
    render(
      <ErrorBoundary>
        <div>正常内容</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("接住渲染异常，给出兜底界面而不是白屏", () => {
    silenceReactErrorLog();
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面加载失败")).toBeInTheDocument();
    // 原始报错要露出来：只说「出错了」的兜底页等于把线索也一起吞了
    expect(screen.getByText(/图谱数据缺少 nodes 字段/)).toBeInTheDocument();
  });

  it("带 scope 时说清是哪一块崩了", () => {
    silenceReactErrorLog();
    render(
      <ErrorBoundary scope="业务地图">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("业务地图加载失败")).toBeInTheDocument();
  });

  it("重试按钮能让恢复正常的子树重新渲染", async () => {
    silenceReactErrorLog();
    const user = userEvent.setup();

    // 从组件外部控制成败，模拟「重试确实能好」的那类瞬时故障（接口抖一下）。
    // 不用组件内部状态计数：那会随 StrictMode 的双渲染而变，测试就成了掷骰子。
    const control = { shouldFail: true };
    function Controlled() {
      return <Boom when={control.shouldFail} />;
    }

    render(
      <ErrorBoundary>
        <Controlled />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面加载失败")).toBeInTheDocument();

    control.shouldFail = false;
    await user.click(screen.getByRole("button", { name: /重\s*试/ }));
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("resetKey 变化时自动恢复（换路由即清掉上一页的错误）", () => {
    silenceReactErrorLog();
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面加载失败")).toBeInTheDocument();

    // 换页：子树换成好的，同时 resetKey 变。不重置的话用户走到新页面仍然看到旧错误。
    rerender(
      <ErrorBoundary resetKey="/b">
        <Boom when={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });
});
