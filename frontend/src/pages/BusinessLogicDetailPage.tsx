import {
  ApartmentOutlined,
  CodeOutlined,
  DeleteOutlined,
  FunctionOutlined,
  HistoryOutlined,
  LinkOutlined,
  SaveOutlined,
  SendOutlined,
  TableOutlined,
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
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import type {
  BusinessLogicDetail,
  BusinessLogicObjectBinding,
  BusinessLogicPropertyBinding,
  BusinessLogicPropertyOption,
  ObjectTypeSummary,
  Property,
  VersionRecord,
} from "../types";

const { Text } = Typography;

const OBJECT_ROLE_LABEL: Record<string, string> = {
  subject: "主对象",
  dimension: "维度对象",
  output: "产出对象",
};

const PROPERTY_ROLE_LABEL: Record<string, string> = {
  input: "输入",
  output: "输出",
  filter: "过滤",
  group: "分组",
};

const OBJECT_ROLE_OPTIONS = [
  { label: "主对象", value: "subject" },
  { label: "维度对象", value: "dimension" },
  { label: "产出对象", value: "output" },
];

const PROPERTY_ROLE_OPTIONS = [
  { label: "输入", value: "input" },
  { label: "输出", value: "output" },
  { label: "过滤", value: "filter" },
  { label: "分组", value: "group" },
];

const LOGIC_TYPE_OPTIONS = [
  { label: "指标 metric", value: "metric" },
  { label: "标签 tag", value: "tag" },
  { label: "规则 rule", value: "rule" },
];

const EDITABLE_STATUSES = new Set(["suggested", "edited", "pre_published"]);

export function BusinessLogicDetailPage() {
  const { logicId } = useParams<{ logicId: string }>();
  const navigate = useNavigate();
  const [logic, setLogic] = useState<BusinessLogicDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [basicForm] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [prePublishing, setPrePublishing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [addObjectId, setAddObjectId] = useState<string | undefined>();
  const [addObjectRole, setAddObjectRole] = useState<string>("subject");
  const [bindingLoading, setBindingLoading] = useState(false);

  const [addPropertyId, setAddPropertyId] = useState<string | undefined>();
  const [addPropertyRole, setAddPropertyRole] = useState<string>("input");

  const load = () => {
    if (!logicId) return;
    setLoading(true);
    api
      .getBusinessLogic(logicId)
      .then((detail) => {
        setLogic(detail);
        basicForm.setFieldsValue({
          display_name: detail.display_name,
          logic_type: detail.logic_type,
          description: detail.description,
          expression_summary: detail.expression_summary,
        });
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logicId]);

  const editable = logic ? EDITABLE_STATUSES.has(logic.status) : false;

  const handleSaveBasic = async () => {
    if (!logicId) return;
    const values = await basicForm.validateFields();
    setSaving(true);
    try {
      const updated = await api.updateBusinessLogic(logicId, {
        display_name: values.display_name,
        logic_type: values.logic_type,
        description: values.description ?? undefined,
        expression_summary: values.expression_summary ?? undefined,
      });
      setLogic(updated);
      message.success("已保存");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
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

  // --- 对象引用绑定 ---

  const handleAddObjectBinding = async () => {
    if (!logicId || !addObjectId) {
      message.warning("请选择要引用的对象");
      return;
    }
    setBindingLoading(true);
    try {
      await api.bindObjectToLogic(logicId, {
        object_type_id: addObjectId,
        role: addObjectRole,
      });
      setAddObjectId(undefined);
      message.success("已添加引用对象");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "添加失败");
    } finally {
      setBindingLoading(false);
    }
  };

  const handleRemoveObjectBinding = async (bindingId: string) => {
    setBindingLoading(true);
    try {
      await api.unbindObjectFromLogic(bindingId);
      message.success("已移除引用对象");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "移除失败");
    } finally {
      setBindingLoading(false);
    }
  };

  const handleChangeObjectRole = async (
    binding: BusinessLogicObjectBinding,
    newRole: string,
  ) => {
    if (newRole === binding.role) return;
    setBindingLoading(true);
    try {
      await api.unbindObjectFromLogic(binding.id);
      await api.bindObjectToLogic(logicId!, {
        object_type_id: binding.object_type_id,
        role: newRole,
      });
      message.success("已更新角色");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新失败");
      load();
    } finally {
      setBindingLoading(false);
    }
  };

  // --- 字段引用绑定 ---

  const handleAddPropertyBinding = async () => {
    if (!logicId || !addPropertyId) {
      message.warning("请选择要引用的字段");
      return;
    }
    setBindingLoading(true);
    try {
      await api.bindPropertyToLogic(logicId, {
        property_id: addPropertyId,
        role: addPropertyRole,
      });
      setAddPropertyId(undefined);
      message.success("已添加引用字段");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "添加失败");
    } finally {
      setBindingLoading(false);
    }
  };

  const handleRemovePropertyBinding = async (bindingId: string) => {
    setBindingLoading(true);
    try {
      await api.unbindPropertyFromLogic(bindingId);
      message.success("已移除引用字段");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "移除失败");
    } finally {
      setBindingLoading(false);
    }
  };

  const handleChangePropertyRole = async (
    binding: BusinessLogicPropertyBinding,
    newRole: string,
  ) => {
    if (newRole === binding.role) return;
    setBindingLoading(true);
    try {
      await api.unbindPropertyFromLogic(binding.id);
      await api.bindPropertyToLogic(logicId!, {
        property_id: binding.property_id,
        role: newRole,
      });
      message.success("已更新角色");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新失败");
      load();
    } finally {
      setBindingLoading(false);
    }
  };

  const propertyColumns: ColumnsType<Property> = [
    {
      title: "属性",
      dataIndex: "display_name",
      key: "display_name",
      render: (v, r) => (
        <span className="id-link">
          <span>{v}</span>
          <span className="id-link-sub">{r.name}</span>
        </span>
      ),
    },
    {
      title: "类型",
      dataIndex: "data_type",
      key: "data_type",
      width: 140,
      render: (v) => v || <span className="om-muted">-</span>,
    },
  ];

  const objectBindingColumns: ColumnsType<BusinessLogicObjectBinding> = [
    {
      title: "对象",
      dataIndex: "object_type_display_name",
      key: "object_type_display_name",
      render: (v, r) => (
        <Link to={`/ontology/${r.object_type_id}`}>
          {v || r.object_type_name || r.object_type_id}
        </Link>
      ),
    },
    {
      title: "标识名",
      dataIndex: "object_type_name",
      key: "object_type_name",
      render: (v) => <span className="id-link-sub">{v}</span>,
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: editable ? 160 : 120,
      render: (v, r) =>
        editable ? (
          <Select
            size="small"
            value={v}
            onChange={(newRole) => handleChangeObjectRole(r, newRole)}
            options={OBJECT_ROLE_OPTIONS}
            style={{ width: 140 }}
          />
        ) : (
          OBJECT_ROLE_LABEL[v] || v
        ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 110,
      render: (v) => (v === "manual" ? <Tag color="blue">人工</Tag> : <Tag>推断</Tag>),
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_, r) =>
        editable ? (
          <Popconfirm
            title="确认移除该引用对象?"
            onConfirm={() => handleRemoveObjectBinding(r.id)}
          >
            <Button type="link" size="small" danger>
              移除
            </Button>
          </Popconfirm>
        ) : null,
    },
  ];

  const propertyBindingColumns: ColumnsType<BusinessLogicPropertyBinding> = [
    {
      title: "字段",
      dataIndex: "property_display_name",
      key: "property_display_name",
      render: (v, r) => v || r.property_name || r.property_id,
    },
    {
      title: "标识名",
      dataIndex: "property_name",
      key: "property_name",
      render: (v) => <span className="id-link-sub">{v}</span>,
    },
    {
      title: "所属对象",
      dataIndex: "object_type_name",
      key: "object_type_name",
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: editable ? 160 : 110,
      render: (v, r) =>
        editable ? (
          <Select
            size="small"
            value={v}
            onChange={(newRole) => handleChangePropertyRole(r, newRole)}
            options={PROPERTY_ROLE_OPTIONS}
            style={{ width: 140 }}
          />
        ) : (
          PROPERTY_ROLE_LABEL[v] || v
        ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 110,
      render: (v) => (v === "manual" ? <Tag color="blue">人工</Tag> : <Tag>推断</Tag>),
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_, r) =>
        editable ? (
          <Popconfirm
            title="确认移除该引用字段?"
            onConfirm={() => handleRemovePropertyBinding(r.id)}
          >
            <Button type="link" size="small" danger>
              移除
            </Button>
          </Popconfirm>
        ) : null,
    },
  ];

  const versionColumns: ColumnsType<VersionRecord> = [
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 90,
      render: (v) => `v${v}`,
    },
    {
      title: "类型",
      dataIndex: "entity_type",
      key: "entity_type",
      width: 130,
    },
    {
      title: "摘要",
      dataIndex: "diff_summary",
      key: "diff_summary",
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v) => new Date(v).toLocaleString(),
    },
  ];

  if (loading) return <PageSkeleton type="detail" />;

  if (!logic) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "业务逻辑不存在"} showIcon />
      </PageContainer>
    );
  }

  const objectOptions: { label: string; value: string }[] = (logic.available_object_types ?? []).map(
    (o: ObjectTypeSummary) => ({
      label: `${o.display_name}（${o.name}）`,
      value: o.id,
    }),
  );
  const propertyOptions: { label: string; value: string }[] = (logic.available_properties ?? []).map(
    (p: BusinessLogicPropertyOption) => ({
      label: `${p.object_type_display_name || p.object_type_name}.${p.property_display_name || p.property_name}`,
      value: p.property_id,
    }),
  );

  const workspacePath = logic.domain_context_id
    ? `/workspace/${logic.domain_context_id}`
    : "/workspace";

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title={logic.display_name}
        description={logic.description || "暂无描述"}
        extra={
          <Space wrap>
            <StatusBadge status={logic.status} />
            {editable && (
              <>
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
              </>
            )}
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
          style={{ marginBottom: 12 }}
        />
      )}

      {editable && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="该业务逻辑处于草稿/编辑状态"
          description="可编辑定义、从已发布本体挑选引用对象与字段;发布后引用将固化为与已发布本体的正式绑定。"
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
              ) : null
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
          <SectionCard title="计算规则" icon={<CodeOutlined />}>
            {editable ? (
              <Form form={basicForm} layout="vertical">
                <Form.Item label="表达式摘要" name="expression_summary">
                  <Input.TextArea
                    rows={8}
                    style={{ fontFamily: "monospace" }}
                    placeholder="如 SUM(amount) WHERE status='paid'"
                  />
                </Form.Item>
              </Form>
            ) : (
              <pre className="code-block">
                {logic.expression_summary || "暂无规则表达式"}
              </pre>
            )}
          </SectionCard>
        </Col>
      </Row>

      <SectionCard
        title="引用对象"
        count={logic.object_bindings?.length ?? 0}
        icon={<TableOutlined />}
        bodyFlush
      >
        {(logic.object_bindings?.length ?? 0) === 0 ? (
          <EmptyState title="暂无引用对象" />
        ) : (
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={objectBindingColumns}
            dataSource={logic.object_bindings}
            pagination={false}
          />
        )}

        {editable && (
          <div
            style={{
              padding: "16px 20px",
              borderTop: "1px dashed var(--om-border)",
              background: "var(--om-surface-muted)",
            }}
          >
            <Space wrap>
              <Select
                style={{ minWidth: 280 }}
                placeholder="从已发布本体选择对象"
                value={addObjectId}
                onChange={setAddObjectId}
                showSearch
                optionFilterProp="label"
                options={objectOptions}
                notFoundContent={
                  objectOptions.length === 0 ? "已发布本体下暂无对象" : undefined
                }
              />
              <Select
                style={{ width: 150 }}
                value={addObjectRole}
                onChange={setAddObjectRole}
                options={OBJECT_ROLE_OPTIONS}
              />
              <Button type="primary" loading={bindingLoading} onClick={handleAddObjectBinding}>
                添加引用对象
              </Button>
            </Space>
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                从已发布本体中挑选对象作为主对象 / 维度对象 / 产出对象;发布后固化为绑定。
              </Text>
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard
        title="引用字段"
        count={logic.property_bindings?.length ?? 0}
        icon={<TableOutlined />}
        bodyFlush
      >
        {(logic.property_bindings?.length ?? 0) === 0 ? (
          <EmptyState title="暂无引用字段" />
        ) : (
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={propertyBindingColumns}
            dataSource={logic.property_bindings}
            pagination={false}
          />
        )}

        {editable && (
          <div
            style={{
              padding: "16px 20px",
              borderTop: "1px dashed var(--om-border)",
              background: "var(--om-surface-muted)",
            }}
          >
            <Space wrap>
              <Select
                style={{ minWidth: 320 }}
                placeholder="从已发布本体选择字段"
                value={addPropertyId}
                onChange={setAddPropertyId}
                showSearch
                optionFilterProp="label"
                options={propertyOptions}
                notFoundContent={
                  propertyOptions.length === 0 ? "已发布本体下暂无字段" : undefined
                }
              />
              <Select
                style={{ width: 150 }}
                value={addPropertyRole}
                onChange={setAddPropertyRole}
                options={PROPERTY_ROLE_OPTIONS}
              />
              <Button type="primary" loading={bindingLoading} onClick={handleAddPropertyBinding}>
                添加引用字段
              </Button>
            </Space>
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                引用字段角色:输入 / 输出 / 过滤 / 分组。
              </Text>
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard
        title="关联对象"
        count={logic.related_object_types.length}
        icon={<ApartmentOutlined />}
      >
        {logic.related_object_types.length === 0 ? (
          <EmptyState title="暂无关联对象" />
        ) : (
          <Row gutter={[16, 16]}>
            {logic.related_object_types.map((obj) => (
              <Col key={obj.id} xs={24} sm={12} md={8}>
                <Link to={`/ontology/${obj.id}`} className="om-card-link">
                  <div className="entity-card" style={{ padding: 14 }}>
                    <div className="entity-card-head">
                      <div style={{ minWidth: 0 }}>
                        <div className="entity-card-title">{obj.display_name}</div>
                        <div className="entity-card-subtitle">{obj.name}</div>
                      </div>
                      <StatusBadge status={obj.status} />
                    </div>
                    <div className="entity-card-desc">
                      {obj.description || "暂无描述"}
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
        )}
      </SectionCard>

      {(logic.related_properties?.length ?? 0) > 0 && (
        <SectionCard
          title="依赖字段"
          count={logic.related_properties?.length ?? 0}
          icon={<TableOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={propertyColumns}
            dataSource={logic.related_properties}
            pagination={false}
          />
        </SectionCard>
      )}

      {(logic.version_records?.length ?? 0) > 0 && (
        <SectionCard
          title="版本记录"
          count={logic.version_records?.length ?? 0}
          icon={<HistoryOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={versionColumns}
            dataSource={logic.version_records}
            pagination={false}
          />
        </SectionCard>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Link to={workspacePath}>
          <Button icon={<LinkOutlined />}>查看所属数据域</Button>
        </Link>
      </div>
    </PageContainer>
  );
}
