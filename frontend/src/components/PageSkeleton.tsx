import { Col, Row, Skeleton } from "antd";

interface Props {
  type?: "list" | "detail" | "cards";
}

export function PageSkeleton({ type = "list" }: Props) {
  if (type === "detail") {
    return (
      <div className="page-container--full">
        <div className="page-header" style={{ marginBottom: 20 }}>
          <div className="page-header-main">
            <Skeleton.Avatar active shape="square" size={40} />
            <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 240 }}>
              <Skeleton.Input active size="small" style={{ width: 220 }} />
              <Skeleton paragraph={{ rows: 1, width: 320 }} active />
            </div>
          </div>
          <Skeleton.Button active size="small" />
        </div>
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
        <div className="section-card">
          <div className="section-card-head">
            <Skeleton.Input active size="small" style={{ width: 180 }} />
          </div>
          <div className="section-card-body--flush">
            <Skeleton active paragraph={{ rows: 6 }} />
          </div>
        </div>
      </div>
    );
  }

  if (type === "cards") {
    return (
      <div className="page-container">
        <Skeleton paragraph={{ rows: 1, width: 200 }} active style={{ marginBottom: 20 }} />
        <Row gutter={[16, 16]}>
          {Array.from({ length: 6 }).map((_, i) => (
            <Col key={i} xs={24} sm={12} lg={8} xl={6}>
              <div className="om-skeleton-card">
                <div
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 12,
                    marginBottom: 10,
                  }}
                >
                  <Skeleton.Avatar active shape="square" size={36} />
                  <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
                    <Skeleton.Input active size="small" style={{ width: "60%" }} />
                    <Skeleton.Input active size="small" style={{ width: 120 }} />
                  </div>
                </div>
                <Skeleton active paragraph={{ rows: 2, width: "100%" }} title={false} />
                <div
                  style={{
                    marginTop: 14,
                    paddingTop: 12,
                    borderTop: "1px dashed var(--om-border, #eef1f6)",
                  }}
                >
                  <Skeleton.Input active size="small" style={{ width: 100 }} />
                </div>
              </div>
            </Col>
          ))}
        </Row>
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
