import { AppstoreOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import { Alert, Descriptions, Segmented, Tag } from "antd";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { ClusterMatrixView } from "../components/graph/ClusterMatrixView";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import type { SegmentDetail, ClusterDetail, GraphNode } from "../types";

// 稠密板块阈值：成员 > 40 时默认显示矩阵视图
const DENSE_THRESHOLD = 40;

export function SegmentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [viewMode, setViewMode] = useState<"cards" | "matrix">("cards");

  const {
    data: segment,
    loading,
    error,
  } = useApi<SegmentDetail>(
    () => (id ? api.getSegment(id) : Promise.reject(new Error("缺少 id"))),
    [id],
  );

  if (loading) return <PageSkeleton type="detail" />;
  if (error)
    return (
      <PageContainer>
        <Alert type="error" message="加载失败" description={error} showIcon />
      </PageContainer>
    );
  if (!segment) return null;

  const isDense = segment.members.length > DENSE_THRESHOLD;
  const hasEdges = Boolean(segment.edges && segment.edges.length > 0);

  // 如果是稠密板块且有边数据，默认使用矩阵视图
  const effectiveMode = isDense && hasEdges && viewMode === "cards" ? "matrix" : viewMode;

  // 将 SegmentDetail 转换为 ClusterDetail 以供 ClusterMatrixView 使用
  const clusterDetail: ClusterDetail | null =
    hasEdges && segment.edges
      ? {
          id: segment.id,
          name: segment.display_name,
          node_count: segment.members.length,
          nodes: segment.members.map(
            (m): GraphNode => ({
              id: m.id,
              label: m.name,
              display_name: m.display_name,
              status: m.status,
              table_role: m.table_role,
              needs_review: m.needs_review || false,
            }),
          ),
          edges: segment.edges,
        }
      : null;

  return (
    <PageContainer>
      <div style={{ marginBottom: 16 }}>
        <Link
          to={`/workspace/${segment.ontology_id}`}
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <ArrowLeftOutlined />
          返回工作区
        </Link>
      </div>

      <PageHeader
        icon={<AppstoreOutlined />}
        title={segment.display_name}
        description={segment.description || "暂无描述"}
        extra={segment.needs_review && <Tag color="warning">待复核</Tag>}
      />

      <div style={{ marginBottom: 24 }}>
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="标识名">{segment.name}</Descriptions.Item>
          <Descriptions.Item label="成员对象数">{segment.member_count}</Descriptions.Item>
          <Descriptions.Item label="内部关系数">
            {segment.internal_relation_count}
          </Descriptions.Item>
          <Descriptions.Item label="更新时间">
            {formatDateTime(segment.updated_at)}
          </Descriptions.Item>
        </Descriptions>
      </div>

      <div style={{ marginBottom: 16, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
          成员对象
          {isDense && <Tag color="blue" style={{ marginLeft: 8 }}>稠密板块</Tag>}
        </h3>
        {isDense && hasEdges && (
          <Segmented
            value={viewMode}
            onChange={(v) => setViewMode(v as "cards" | "matrix")}
            options={[
              { label: "卡片视图", value: "cards" },
              { label: "矩阵视图", value: "matrix" },
            ]}
          />
        )}
      </div>

      {segment.members.length === 0 ? (
        <EmptyState title="暂无成员对象" description="此板块尚未分配任何成员对象。" />
      ) : effectiveMode === "matrix" && clusterDetail ? (
        <ClusterMatrixView detail={clusterDetail} />
      ) : (
        <div className="workspace-domain-grid">
          {segment.members.map((member) => (
            <Link key={member.id} to={`/object-types/${member.id}`} className="om-card-link">
              <div className="entity-card">
                <div className="entity-card-head">
                  <div className="entity-card-head-main">
                    <div className="entity-card-icon entity-card-icon--primary">○</div>
                    <div style={{ minWidth: 0 }}>
                      <div className="entity-card-title">{member.display_name}</div>
                      <div className="entity-card-subtitle">{member.name}</div>
                    </div>
                  </div>
                </div>
                <div className="entity-card-desc">{member.description || "暂无描述"}</div>
                <div className="entity-card-foot">
                  <div className="entity-card-foot-stats">
                    <span className="entity-card-foot-item">
                      <strong>{member.property_count}</strong> 属性
                    </span>
                    <span className="entity-card-foot-item">
                      <strong>{member.relation_count}</strong> 关系
                    </span>
                  </div>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </PageContainer>
  );
}
