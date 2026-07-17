interface Props {
  status: string;
}

const LABELS: Record<string, string> = {
  draft: "草稿",
  in_review: "待审",
  published: "已发布",
  archived: "已归档",
  suggested: "建议",
  edited: "已编辑",
  approved: "已批准",
  pre_published: "预发布",
  deprecated: "已废弃",
  pending: "待确认",
  confirmed: "已确认",
  cancelled: "已取消",
  queued: "排队中",
  running: "进行中",
  completed: "已完成",
  succeeded: "已完成",
  failed: "失败",
  active: "活跃",
  generate_draft: "生成草稿",
  edit: "编辑",
  pre_publish: "预发布",
  publish: "发布",
};

type Tone = "default" | "blue" | "cyan" | "gold" | "green" | "red" | "processing";

const TONES: Record<string, Tone> = {
  draft: "gold",
  in_review: "gold",
  published: "green",
  archived: "default",
  suggested: "blue",
  edited: "cyan",
  approved: "green",
  pre_published: "gold",
  deprecated: "default",
  pending: "gold",
  confirmed: "green",
  cancelled: "red",
  queued: "processing",
  running: "processing",
  completed: "green",
  succeeded: "green",
  failed: "red",
  active: "cyan",
  generate_draft: "gold",
  edit: "cyan",
  pre_publish: "gold",
  publish: "green",
};

export function StatusBadge({ status }: Props) {
  const tone = TONES[status] || "default";
  const label = LABELS[status] || status;
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <span className="status-pill-dot" />
      {label}
    </span>
  );
}
