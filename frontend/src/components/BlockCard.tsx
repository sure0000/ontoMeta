import type { ReactNode } from "react";
import "./BlockCard.css";

export interface BlockCardProps {
  /** 卡片语义类型，控制左边框色与图标色 */
  variant?: "neutral" | "primary" | "success" | "warning" | "error";
  /** 卡片头部（可选） */
  title?: ReactNode;
  /** 头部右侧操作区（可选） */
  actions?: ReactNode;
  /** 卡片主体 */
  children: ReactNode;
  /** 额外 CSS 类 */
  className?: string;
}

/**
 * Data Agent 渲染块统一卡片容器。
 *
 * 所有 21 种渲染块（insight / plan / proposal / task_status / record 等）
 * 共享同一套外观规格：统一圆角/留白/标题栏/分隔线。
 *
 * 语义色只用于左边框强调，背景统一为中性 `--om-surface`。
 */
export function BlockCard({ variant = "neutral", title, actions, children, className }: BlockCardProps) {
  const variantClass = variant !== "neutral" ? `block-card--${variant}` : "";
  const hasHeader = Boolean(title || actions);

  return (
    <div className={`block-card ${variantClass} ${className || ""}`.trim()}>
      {hasHeader && (
        <div className="block-card-header">
          {title && <div className="block-card-title">{title}</div>}
          {actions && <div className="block-card-actions">{actions}</div>}
        </div>
      )}
      <div className="block-card-body">{children}</div>
    </div>
  );
}
