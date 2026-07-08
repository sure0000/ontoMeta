import {
  ApartmentOutlined,
  CodeOutlined,
  DeleteOutlined,
  EditOutlined,
  FunctionOutlined,
  LinkOutlined,
  SaveOutlined,
  SendOutlined,

} from "@ant-design/icons";
import {
  Alert,
  Button,
  Col,
  Descriptions,
  Form,
  Input,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from "antd";

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { ExpressionJsonPreview } from "../components/ExpressionJsonPreview";
import { ExpressionRichEditor } from "../components/ExpressionRichEditor";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import type {
  BusinessLogicDetail,
  ExpressionDraft,
  ExpressionJson,
} from "../types";


const OBJECT_ROLE_LABEL: Record<string, string> = {
  subject: "主对象",
  dimension: "维度对象",
  output: "产出对象",
};

const { Text } = Typography;

const LOGIC_TYPE_OPTIONS = [
  { label: "指标 metric", value: "metric" },
  { label: "标签 tag", value: "tag" },
  { label: "规则 rule", value: "rule" },
];

export function BusinessLogicDetailPage() {
  const { logicId } = useParams<{ logicId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const editable = searchParams.get("edit") === "true";
  const [logic, setLogic] = useState<BusinessLogicDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [basicForm] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewJson, setPreviewJson] = useState<ExpressionJson | null>(null);
  const [previewModalOpen, setPreviewModalOpen] = useState(false);
  const [prePublishing, setPrePublishing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const load = () => {
    if (!logicId) return;
    setLoading(true);
    api
      .getBusinessLogic(logicId)
      .then((detail) => {
        setLogic(detail);
        const draft: ExpressionDraft =
          detail.expression_draft && detail.expression_draft.segments
            ? detail.expression_draft
            : detail.expression_summary
              ? { segments: [{ type: "text", value: detail.expression_summary }] }
              : { segments: [] };
        basicForm.setFieldsValue({
          display_name: detail.display_name,
          logic_type: detail.logic_type,
          description: detail.description,
          expression_draft: draft,
        });
        setPreviewJson(detail.expression_json ?? null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logicId]);

  const handleSaveBasic = async () => {
    if (!logicId) return;
    const values = await basicForm.validateFields();
    const draft = values.expression_draft as ExpressionDraft | undefined;
    setSaving(true);
    try {
      let expressionJson: ExpressionJson | undefined;
      let expressionSummary: string | undefined;
      if (draft && draft.segments && draft.segments.length > 0) {
        const formatted = await api.formatExpression({
          domain_id: logic!.domain_context_id!,
          expression_draft: draft,
          logic_type: values.logic_type,
          description: values.description,
        });
        expressionJson = formatted.expression_json;
        expressionSummary = formatted.expression_summary;
      }
      const updated = await api.updateBusinessLogic(logicId, {
        display_name: values.display_name,
        logic_type: values.logic_type,
        description: values.description ?? undefined,
        expression_draft: draft,
        expression_json: expressionJson,
        expression_summary: expressionSummary,
      });
      setLogic(updated);
      setPreviewJson(updated.expression_json ?? expressionJson ?? null);
      message.success("已保存");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    if (!logicId || !logic?.domain_context_id) return;
    const values = await basicForm.validateFields();
    const draft = values.expression_draft as ExpressionDraft | undefined;
    if (!draft || !draft.segments || draft.segments.length === 0) {
      message.warning("请先填写表达式");
      return;
    }
    setPreviewing(true);
    setPreviewModalOpen(true);
    try {
      const res = await api.formatExpression({
        domain_id: logic.domain_context_id,
        expression_draft: draft,
        logic_type: values.logic_type,
        description: values.description,
      });
      setPreviewJson(res.expression_json);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预览失败");
      setPreviewModalOpen(false);
    } finally {
      setPreviewing(false);
    }
  };

  const handlePrePublish = () => {
    if (!logicId) return;
    Modal.confirm({
      title: "确认预发布",
      content: "预发布后该业务逻辑将进入待发布状态,仍可继续编辑。",
      okText: "确认预发布",
      cancelText: "取消",
      onOk: async () => {
        setPrePublishing(true);
        try {
          await api.prePublishBusinessLogic(logicId);
          message.success("已预发布");
          load();
        } catch (err) {
          message.error(err instanceof Error ? err.message : "预发布失败");
        } finally {
          setPrePublishing(false);
        }
      },
    });
  };

  const handlePublish = () => {
    if (!logicId) return;
    Modal.confirm({
      title: "确认发布业务逻辑",
      content:
        "发布后该业务逻辑将固化为正式版本,其引用的对象/字段绑定即与已发布本体正式关联。此操作需要二次确认。",
      okText: "确认发布",
      cancelText: "取消",
      onOk: async () => {
        setPublishing(true);
        try {
          await api.publishBusinessLogic(logicId);
          message.success("发布成功");
          load();
        } catch (err) {
          message.error(err instanceof Error ? err.message : "发布失败");
        } finally {
          setPublishing(false);
        }
      },
    });
  };

  const handleDelete = async () => {
    if (!logicId) return;
    setDeleting(true);
    try {
      await api.deleteBusinessLogic(logicId);
      message.success("已删除业务逻辑");
      navigate("/business-logic");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    } finally {
      setDeleting(false);
    }
  };

  // 合并所有关联对象 (来自 object_bindings / property_bindings / related_object_types)
  const mergedObjects = useMemo(() => {
    if (!logic) return [];
    const map = new Map<string, {
      id: string;
      display_name: string;
      name: string;
      description?: string;
      status: string;
      roles: Set<string>;
      sources: Set<string>;
      propertyCount: number;
    }>();

    const addRole = (id: string, role: string) => {
      const entry = map.get(id);
      if (entry && role) entry.roles.add(role);
    };

    // From related_object_types (most complete data)
    for (const obj of logic.related_object_types ?? []) {
      map.set(obj.id, {
        id: obj.id,
        display_name: obj.display_name,
        name: obj.name,
        description: obj.description,
        status: obj.status,
        roles: new Set(),
        sources: new Set(),
        propertyCount: 0,
      });
    }

    // Enrich with object_bindings
    for (const b of logic.object_bindings ?? []) {
      const oid = b.object_type_id;
      if (!map.has(oid)) {
        map.set(oid, {
          id: oid,
          display_name: b.object_type_display_name ?? b.object_type_name ?? oid,
          name: b.object_type_name ?? oid,
          status: "published",
          roles: new Set(),
          sources: new Set(),
          propertyCount: 0,
        });
      }
      addRole(oid, b.role);
      if (b.source) map.get(oid)!.sources.add(b.source);
    }

    // Enrich with property_bindings
    for (const b of logic.property_bindings ?? []) {
      const oid = b.object_type_id;
      if (!oid) continue;
      if (!map.has(oid)) {
        map.set(oid, {
          id: oid,
          display_name: b.object_type_name ?? oid,
          name: b.object_type_name ?? oid,
          status: "published",
          roles: new Set(),
          sources: new Set(),
          propertyCount: 0,
        });
      }
      map.get(oid)!.propertyCount += 1;
      addRole(oid, b.role);
      if (b.source) map.get(oid)!.sources.add(b.source);
    }

    return Array.from(map.values());
  }, [logic]);

  if (loading) return <PageSkeleton type="detail" />;

  if (!logic) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "业务逻辑不存在"} showIcon />
      </PageContainer>
    );
  }

  const workspacePath = logic.domain_context_id
    ? `/workspace/${logic.domain_context_id}`
    : "/workspace";

  return (
    <PageContainer full>
      <div className="om-stack">
      <PageHeader
        icon={<FunctionOutlined />}
        title={logic.display_name}
        description={logic.description || "暂无描述"}
        extra={
          <Space wrap>
            <StatusBadge status={logic.status} />
            <Button
              icon={<SendOutlined />}
              loading={prePublishing}
              onClick={handlePrePublish}
            >
              预发布
            </Button>
            <Button type="primary" icon={<SendOutlined />} loading={publishing} onClick={handlePublish}>
              发布
            </Button>
            <Popconfirm
              title="确认删除该业务逻辑?此操作需要二次确认。"
              onConfirm={handleDelete}
              disabled={deleting}
            >
              <Button danger icon={<DeleteOutlined />} loading={deleting}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        }
      />

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setError(null)}
        />
      )}

      <Row gutter={[20, 20]}>
        <Col xs={24} lg={12}>
          <SectionCard
            title="逻辑定义"
            icon={<FunctionOutlined />}
            extra={
              editable ? (
                <Button
                  type="primary"
                  size="small"
                  icon={<SaveOutlined />}
                  loading={saving}
                  onClick={handleSaveBasic}
                >
                  保存
                </Button>
              ) : (
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => navigate(`/business-logic/${logicId}?edit=true`, { replace: true })}
                >
                  编辑
                </Button>
              )
            }
          >
            {editable ? (
              <Form form={basicForm} layout="vertical">
                <Form.Item label="显示名" name="display_name" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
                <Form.Item label="逻辑类型" name="logic_type" rules={[{ required: true }]}>
                  <Select options={LOGIC_TYPE_OPTIONS} />
                </Form.Item>
                <Form.Item label="数据域">
                  <Input disabled value={logic.domain_name || "-"} />
                </Form.Item>
                <Form.Item label="描述" name="description">
                  <Input.TextArea rows={2} />
                </Form.Item>
              </Form>
            ) : (
              <Descriptions column={1} size="small" labelStyle={{ width: 96 }}>
                <Descriptions.Item label="数据域">
                  {logic.domain_name || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="类型">{logic.logic_type}</Descriptions.Item>
                <Descriptions.Item label="来源类型">
                  {logic.source_type || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="来源引用">
                  <Text code copyable={!!logic.source_ref}>
                    {logic.source_ref || "-"}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="置信度">
                  {logic.source_confidence?.toFixed(2) ?? "-"}
                </Descriptions.Item>
              </Descriptions>
            )}
          </SectionCard>
        </Col>

        <Col xs={24} lg={12}>
          <SectionCard
            title="计算规则"
            icon={<CodeOutlined />}
            extra={
              editable ? (
                <Space>
                  <Button size="small" onClick={handlePreview} loading={previewing}>
                    JSON 预览
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    icon={<SaveOutlined />}
                    loading={saving}
                    onClick={handleSaveBasic}
                  >
                    保存
                  </Button>
                </Space>
              ) : null
            }
          >
            {editable ? (
              <Form form={basicForm} layout="vertical">
                <Form.Item
                  label="表达式"
                  name="expression_draft"
                  extra={
                    <span style={{ fontSize: 12 }}>
                      用自然语言书写,输入 <code>@</code> 引用对象,紧接 <code>.</code> 引用其字段
                    </span>
                  }
                >
                  <ExpressionRichEditor
                    domainId={logic.domain_context_id}
                    placeholder="例如:统计 SUM(@订单.金额) 万元,其中 @订单.状态 为「已支付」"
                    minHeight={220}
                  />
                </Form.Item>
              </Form>
            ) : (
              <pre className="code-block code-block--bounded">
                {logic.expression_summary || "暂无规则表达式"}
              </pre>
            )}
          </SectionCard>
        </Col>
      </Row>

      <SectionCard title="表达式 JSON" icon={<CodeOutlined />} bodyFlush>
        <ExpressionJsonPreview
          json={logic.expression_json}
          loading={false}
          emptyHint="暂未保存格式化结果,点击「JSON 预览」可先查看,或直接「保存」生成"
          embedded
        />
      </SectionCard>

      <SectionCard
        title="关联对象"
        count={mergedObjects.length}
        icon={<ApartmentOutlined />}
      >
        {mergedObjects.length === 0 ? (
          <EmptyState title="暂无关联对象" />
        ) : (
          <Row gutter={[12, 12]}>
            {mergedObjects.map((obj) => {
              const objLogics = logic.related_object_logics?.[obj.id] ?? [];
              return (
              <Col key={obj.id} xs={24} sm={12} md={8} lg={6}>
                <Link to={`/ontology/${obj.id}`} className="om-card-link">
                  <div className="entity-card" style={{ padding: 14 }}>
                    <div className="entity-card-head">
                      <div style={{ minWidth: 0 }}>
                        <div className="entity-card-title">{obj.display_name}</div>
                        <div className="entity-card-subtitle">{obj.name}</div>
                      </div>
                      {obj.status && <StatusBadge status={obj.status} />}
                    </div>
                    <div className="entity-card-desc">
                      {obj.description || "暂无描述"}
                    </div>
                    {(obj.roles.size > 0 || obj.sources.size > 0 || obj.propertyCount > 0) && (
                      <div className="entity-card-foot">
                        {Array.from(obj.roles).map((r) => (
                          <Tag key={r} color="blue">{OBJECT_ROLE_LABEL[r] || r}</Tag>
                        ))}
                        {obj.propertyCount > 0 && (
                          <span className="entity-card-foot-item">
                            {obj.propertyCount} 字段
                          </span>
                        )}
                        {Array.from(obj.sources).map((s) => (
                          <Tag key={s} color={s === "manual" ? "blue" : undefined}>
                            {s === "manual" ? "人工" : "推断"}
                          </Tag>
                        ))}
                      </div>
                    )}
                    {objLogics.length > 0 && (
                      <div className="entity-card-foot" style={{ marginTop: 6, flexWrap: "wrap" }}>
                        <span style={{ fontSize: 11, color: "#8c8c8c", marginRight: 4, lineHeight: "22px" }}>
                          业务逻辑:
                        </span>
                        {objLogics.map((bl) => {
                          const typeColor =
                            bl.logic_type === "metric" ? "blue" :
                            bl.logic_type === "tag" ? "green" : "orange";
                          return (
                            <Link
                              key={bl.id}
                              to={`/business-logic/${bl.id}`}
                              onClick={(e) => e.stopPropagation()}
                              style={{ maxWidth: "100%" }}
                            >
                              <Tag
                                color={typeColor}
                                style={{ marginBottom: 2, maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis" }}
                              >
                                {bl.display_name || bl.name}
                              </Tag>
                            </Link>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </Link>
              </Col>
              );
            })}
          </Row>
        )}

      </SectionCard>

      <div className="om-detail-footer">
        <Link to={workspacePath}>
          <Button icon={<LinkOutlined />}>查看所属数据域</Button>
        </Link>
      </div>
      </div>

      <Modal
        title="表达式 JSON 预览"
        open={previewModalOpen}
        onCancel={() => setPreviewModalOpen(false)}
        footer={[
          <Button key="close" onClick={() => setPreviewModalOpen(false)}>
            关闭
          </Button>,
        ]}
        width={720}
        destroyOnClose
      >
        <ExpressionJsonPreview
          json={previewJson}
          loading={previewing}
          title="AST 结构"
          emptyHint="LLM 正在将自然语言 + 引用格式化为统一的 AST JSON…"
        />
      </Modal>
    </PageContainer>
  );
}
