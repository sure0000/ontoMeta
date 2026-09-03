// SegmentDetailPage.tsx - 迁移示例
//
// 这个文件展示了如何将 OntologyGraphView 替换为 OntologyDetailGraph
// 原页面只使用详情模式，不需要概览，所以直接使用 OntologyDetailGraph

import { AppstoreOutlined, ArrowLeftOutlined, EditOutlined } from "@ant-design/icons";
import { Alert, Button, Descriptions, Form, Input, Modal, Segmented, Tag, message } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { ClusterMatrixView } from "../components/graph/ClusterMatrixView";
// 迁移变化：从 OntologyGraphView 改为 OntologyDetailGraph
import { OntologyDetailGraph } from "../components/graph/OntologyDetailGraph";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import type { SegmentDetail, ClusterDetail, GraphNode, OntologyGraph } from "../types";

// 稠密板块阈值：成员 > 40 时默认显示矩阵视图
const DENSE_THRESHOLD = 40;

export function SegmentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const publishedOnly = searchParams.get("published") === "1";
  const [viewMode, setViewMode] = useState<"cards" | "matrix">("cards");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const {
    data: segment,
    loading,
    error,
  } = useApi<SegmentDetail>(
    () => (id ? api.getSegment(id, publishedOnly) : Promise.reject(new Error("缺少 id"))),
    [id, publishedOnly],
  );

  useEffect(() => {
    if (segment) {
      form.setFieldsValue({
        name: segment.name,
        display_name: segment.display_name,
        description: segment.description || "",
      });
    }
  }, [segment, form]);

  if (loading) return <PageSkeleton type="detail" />;
  if (error)
    return (
      <PageContainer>
        <Alert type="error" message="加载失败" description={error} showIcon />
      </PageContainer>
    );
  if (!segment) return null;

  const saveSegment = async () => {
    if (!id) return;
    try {
      setSaving(true);
      await api.updateSegment(id, await form.validateFields());
      message.success("板块已更新");
      setEditing(false);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新板块失败");
    } finally {
      setSaving(false);
    }
  };

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

  const segmentGraph: OntologyGraph | null =
    hasEdges && segment.edges
      ? {
          nodes: segment.members.map((member) => ({
            id: member.id,
            label: member.name,
            display_name: member.display_name,
            status: member.status,
          })),
          edges: segment.edges,
        }
      : null;

  return (
    <PageContainer>
      <div style={{ marginBottom: 16 }}>
        <Link
          to={
            publishedOnly
              ? "/ontology"
              : segment.domain_context_id
                ? `/workspace/${segment.domain_context_id}`
                : "/workspace"
          }
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <ArrowLeftOutlined />
          {publishedOnly ? "返回本体浏览" : "返回工作区"}
        </Link>
      </div>

      <PageHeader
        icon={<AppstoreOutlined />}
        title={segment.display_name}
        description={segment.description || "暂无描述"}
        extra={
          <>
            {segment.needs_review && <Tag color="warning">待复核</Tag>}
            {!publishedOnly && (
              <Button icon={<EditOutlined />} onClick={() => setEditing(true)}>
                编辑
              </Button>
            )}
          </>
        }
      />

      <Modal
        title="编辑业务板块"
        open={editing}
        onOk={() => void saveSegment()}
        onCancel={() => setEditing(false)}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="标识名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      <div style={{ marginBottom: 24 }}>
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="标识名">{segment.name}</Descriptions.Item>
          <Descriptions.Item label="成员对象数">{segment.member_count}</Descriptions.Item>
          <Descriptions.Item label="内部关系数">
            {segment.internal_relation_count}
          </Descriptions.Item>
          <Descriptions.Item label="跨板块关系数">
            {segment.cross_relation_count}
          </Descriptions.Item>
          <Descriptions.Item label="更新时间">
            {formatDateTime(segment.updated_at)}
          </Descriptions.Item>
        </Descriptions>
      </div>

      {(segment.relation_sentences ?? []).length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>板块内关系</h3>
          <div style={{ display: "grid", gap: 8 }}>
            {(segment.relation_sentences ?? []).map((sentence) => (
              <div key={sentence} className="entity-card-desc" style={{ padding: "10px 12px" }}>
                {sentence}
              </div>
            ))}
          </div>
        </div>
      )}

      {segmentGraph && (
        <div style={{ marginBottom: 24 }}>
          <SectionCard title="板块关系图" bodyFlush>
            {/* 迁移变化：使用 OntologyDetailGraph 替代 OntologyGraphView */}
            <OntologyDetailGraph
              graph={segmentGraph}
              height={420}
              objectDetailPath={(objectId) =>
                publishedOnly ? `/ontology/${objectId}` : `/workspace/${segment.ontology_id}/objects/${objectId}`
              }
              embedded
            />
          </SectionCard>
        </div>
      )}

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
