import { InboxOutlined } from "@ant-design/icons";
import type { ReactNode } from "react";

interface Props {
  title?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({
  title = "暂无数据",
  description,
  action,
}: Props) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon">
        <InboxOutlined />
      </div>
      <div className="empty-state-title">{title}</div>
      {description && <div className="empty-state-desc">{description}</div>}
      {action && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  );
}
