import { AppstoreOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import { Alert, Descriptions, Tag } from "antd";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import type { SegmentDetail } from "../types";

export function SegmentDetailPage() {
  const { id } = useParams<{ id: string }>();

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

      <div style={{ marginBottom: 16 }}>
        <h3 style={{ fontSize: 16, fontWeight: 600 }}>成员对象</h3>
      </div>

      {segment.members.length === 0 ? (
        <EmptyState title="暂无成员对象" description="此板块尚未分配任何成员对象。" />
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
