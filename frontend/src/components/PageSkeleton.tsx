import { Skeleton } from "antd";

interface Props {
  type?: "list" | "detail" | "cards";
}

export function PageSkeleton({ type = "list" }: Props) {
  if (type === "detail") {
    return (
      <div className="page-container">
        <div className="page-header">
          <div className="page-header-main">
            <Skeleton.Avatar active shape="square" size={40} />
            <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 240 }}>
              <Skeleton.Input active size="small" style={{ width: 220 }} />
              <Skeleton paragraph={{ rows: 1, width: 320 }} active />
            </div>
          </div>
          <Skeleton.Button active size="small" />
        </div>
        <div className="om-stack">
          <div className="om-skeleton-card">
            <Skeleton paragraph={{ rows: 4 }} active />
          </div>
          <div className="om-skeleton-card">
            <Skeleton paragraph={{ rows: 6 }} active />
          </div>
        </div>
      </div>
    );
  }

  if (type === "cards") {
    return (
      <div className="page-container">
        <Skeleton paragraph={{ rows: 1, width: 200 }} active style={{ marginBottom: 20 }} />
        <div className="stat-row" style={{ marginBottom: 20 }}>
          {Array.from({ length: 4 }).map((_, i) => (
            <div className="stat-card" key={i}>
              <Skeleton.Avatar active shape="square" size={38} />
              <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1 }}>
                <Skeleton.Input active size="small" style={{ width: 80 }} />
                <Skeleton.Input active size="small" style={{ width: 120 }} />
              </div>
            </div>
          ))}
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: 16,
          }}
        >
          {Array.from({ length: 6 }).map((_, i) => (
            <div className="om-skeleton-card" key={i} style={{ height: 160 }}>
              <Skeleton active paragraph={{ rows: 3 }} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="page-container">
      <Skeleton paragraph={{ rows: 1, width: 200 }} active style={{ marginBottom: 20 }} />
      <div className="om-skeleton-card">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    </div>
  );
}
