import {
  ApartmentOutlined,
  AppstoreOutlined,
  BranchesOutlined,
  FunctionOutlined,
  HistoryOutlined,
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
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { ObjectRelationGraph } from "../components/ObjectRelationGraph";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import { extractDataHubBase, resolveDataHubDatasetUrl } from "../utils/datahub";
import {
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
} from "../utils/relation";
import type {
  BusinessLogic,
  ObjectTypeDetail,
  ObjectTypeLogicBinding,
  Property,
  RelationType,
  VersionRecord,
} from "../types";

const { Text } = Typography;

const OBJECT_ROLE_LABEL: Record<string, string> = {
  subject: "主对象",
  dimension: "维度对象",
  output: "产出对象",
};

const OBJECT_ROLE_OPTIONS = [
  { label: "主对象", value: "subject" },
  { label: "维度对象", value: "dimension" },
  { label: "产出对象", value: "output" },
];

interface BasicForm {
  name: string;
  display_name: string;
  description?: string;
}

function DataHubSourceLink({
  sourceRef,
  datahubUrl,
  datahubBase,
}: {
  sourceRef?: string;
  datahubUrl?: string;
  datahubBase?: string;
}) {
  const url = resolveDataHubDatasetUrl(sourceRef, datahubUrl, datahubBase);

  if (!sourceRef && !url) {
    return <Text type="secondary">无关联 DataHub 表</Text>;
  }

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      {sourceRef && (
        <Text code copyable style={{ wordBreak: "break-all" }}>
          {sourceRef}
        </Text>
      )}
      {url ? (
        <Button
          type="primary"
          ghost
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          icon={<LinkOutlined />}
        >
          在 DataHub 中查看表详情
        </Button>
      ) : (
        <Text type="secondary">无法生成 DataHub 链接</Text>
      )}
    </Space>
  );
}

export function ObjectTypeDetailPage() {
  const { objectId, domainId } = useParams<{ objectId: string; domainId?: string }>();

  const [obj, setObj] = useState<ObjectTypeDetail | null>(null);
  const [datahubBase, setDatahubBase] = useState<string | undefined>();
  const [properties, setProperties] = useState<Property[]>([]);
  const [availableLogics, setAvailableLogics] = useState<BusinessLogic[]>([]);
  const [bindingLogicId, setBindingLogicId] = useState<string | undefined>();
  const [bindingRole, setBindingRole] = useState<string>("subject");
  const [bindingLoading, setBindingLoading] = useState(false);
  const [unbindingId, setUnbindingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [prePublishing, setPrePublishing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form] = Form.useForm<BasicForm>();
  const inWorkspace = Boolean(domainId);

  const loadObject = async () => {
    if (!objectId) return;
    const detail = await api.getObjectType(objectId);
    setObj(detail);
    setProperties(detail.properties.map((p) => ({ ...p })));
    form.setFieldsValue({
      name: detail.name,
      display_name: detail.display_name,
      description: detail.description,
    });
    return detail;
  };

  useEffect(() => {
    if (!objectId) return;
    setLoading(true);
    (async () => {
      try {
        const detail = await loadObject();
        if (domainId) {
          const domain = await api.getDomain(domainId);
          setDatahubBase(extractDataHubBase(domain.datahub_url));
        } else {
          const config = await api.getConfig();
          setDatahubBase(
            config.datahub_frontend_url ?? config.datahub_gms_url,
          );
        }
        if (detail?.ontology_id) {
          try {
            const logics = await api.listBusinessLogics({
              ontologyId: detail.ontology_id,
            });
            setAvailableLogics(logics);
          } catch {
            setAvailableLogics([]);
          }
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objectId, domainId]);

  const persistChanges = async () => {
    if (!objectId || !inWorkspace) return;
    const values = await form.validateFields();
    await api.updateObjectType(objectId, values);
    await Promise.all(
      properties.map((prop) =>
        api.updateProperty(prop.id, {
          display_name: prop.display_name,
          description: prop.description,
          data_type: prop.data_type,
          semantic_type: prop.semantic_type,
        }),
      ),
    );
    await loadObject();
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await persistChanges();
      message.success("保存成功");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handlePrePublish = async () => {
    if (!objectId || !inWorkspace) return;
    setPrePublishing(true);
    try {
      await persistChanges();
      const updated = await api.prePublishObjectType(objectId);
      setObj((prev) => (prev ? { ...prev, status: updated.status } : prev));
      message.success("已预发布");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预发布失败");
    } finally {
      setPrePublishing(false);
    }
  };

  const updateProperty = (id: string, patch: Partial<Property>) => {
    setProperties((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  };

  const handleAddBinding = async () => {
    if (!bindingLogicId) {
      message.warning("请选择要绑定的业务逻辑");
      return;
    }
    setBindingLoading(true);
    try {
      await api.bindObjectToLogic(bindingLogicId, {
        object_type_id: objectId!,
        role: bindingRole,
      });
      message.success("已绑定业务逻辑");
      setBindingLogicId(undefined);
      await loadObject();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "绑定失败");
    } finally {
      setBindingLoading(false);
    }
  };

  const handleUnbind = async (bindingId: string) => {
    setUnbindingId(bindingId);
    try {
      await api.unbindObjectFromLogic(bindingId);
      message.success("已解除绑定");
      await loadObject();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "解绑失败");
    } finally {
      setUnbindingId(null);
    }
  };

  const objectDetailPath = useMemo(() => {
    if (!domainId) return undefined;
    return (id: string) => `/workspace/${domainId}/objects/${id}`;
  }, [domainId]);

  const relationDetailPath = useMemo(() => {
    if (!domainId) return (id: string) => `/ontology/relations/${id}`;
    return (id: string) => `/workspace/${domainId}/relations/${id}`;
  }, [domainId]);

  const readOnlyPropertyColumns: ColumnsType<Property> = [
    {
      title: "名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <span className="id-link">
          <span>{record.display_name}</span>
          <span className="id-link-sub">{record.name}</span>
        </span>
      ),
    },
    {
      title: "类型",
      dataIndex: "data_type",
      key: "data_type",
      width: 130,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "语义类型",
      dataIndex: "semantic_type",
      key: "semantic_type",
      width: 130,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
  ];

  const editablePropertyColumns: ColumnsType<Property> = [
    {
      title: "显示名称",
      key: "display_name",
      render: (_, record) => (
        <Input
          value={record.display_name}
          onChange={(e) => updateProperty(record.id, { display_name: e.target.value })}
        />
      ),
    },
    {
      title: "标识名",
      dataIndex: "name",
      key: "name",
      render: (name) => <Text type="secondary">{name}</Text>,
    },
    {
      title: "类型",
      key: "data_type",
      width: 130,
      render: (_, record) => (
        <Input
          value={record.data_type || ""}
          onChange={(e) => updateProperty(record.id, { data_type: e.target.value })}
        />
      ),
    },
    {
      title: "语义类型",
      key: "semantic_type",
      width: 140,
      render: (_, record) => (
        <Input
          value={record.semantic_type || ""}
          onChange={(e) => updateProperty(record.id, { semantic_type: e.target.value })}
        />
      ),
    },
  ];

  if (loading) return <PageSkeleton type="detail" />;

  if (!obj) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "对象不存在"} showIcon />
      </PageContainer>
    );
  }

  const canPrePublish =
    obj.status !== "pre_published" && obj.status !== "published";
  const relationCount =
    obj.outgoing_relations.length + obj.incoming_relations.length;

  const relationColumns: ColumnsType<RelationType> = [
    {
      title: "关系",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <Link to={relationDetailPath(record.id)}>{record.display_name}</Link>
      ),
    },
    {
      title: "方向",
      key: "direction",
      width: 90,
      render: (_, record) =>
        record.source_object_type_id === obj.id ? "出向" : "入向",
    },
    {
      title: "关联对象",
      key: "peer",
      render: (_, record) => {
        const peerId =
          record.source_object_type_id === obj.id
            ? record.target_object_type_id
            : record.source_object_type_id;
        const peerName =
          record.source_object_type_id === obj.id
            ? record.target_object_name
            : record.source_object_name;
        return (
          <Link to={objectDetailPath?.(peerId) ?? `/ontology/${peerId}`}>
            {peerName}
          </Link>
        );
      },
    },
    {
      title: "结构类型",
      dataIndex: "structure_type",
      key: "structure_type",
      width: 110,
      render: (value, record) =>
        getRelationStructureLabel(
          value || inferRelationStructureType(record.description, record.source_evidence),
        ),
    },
    {
      title: "基数",
      dataIndex: "cardinality",
      key: "cardinality",
      width: 90,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "证据",
      key: "evidence",
      width: 110,
      render: (_, record) =>
        inferRelationEvidenceType(record.source_evidence || record.description),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
  ];

  const logicColumns: ColumnsType<BusinessLogic> = [
    {
      title: "逻辑名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <Link to={`/business-logic/${record.id}`}>{record.display_name}</Link>
      ),
    },
    {
      title: "类型",
      dataIndex: "logic_type",
      key: "logic_type",
      width: 110,
    },
    {
      title: "绑定对象数",
      dataIndex: "bound_object_count",
      key: "bound_object_count",
      width: 110,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
    },
    {
      title: "绑定字段数",
      dataIndex: "bound_property_count",
      key: "bound_property_count",
      width: 110,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
  ];

  const logicBindingColumns: ColumnsType<ObjectTypeLogicBinding> = [
    {
      title: "业务逻辑",
      dataIndex: "logic_display_name",
      key: "logic_display_name",
      render: (_, record) => (
        <Link to={`/business-logic/${record.logic_id}`}>
          {record.logic_display_name}
        </Link>
      ),
    },
    {
      title: "标识名",
      dataIndex: "logic_name",
      key: "logic_name",
      render: (v) => <span className="id-link-sub">{v}</span>,
    },
    {
      title: "类型",
      dataIndex: "logic_type",
      key: "logic_type",
      width: 100,
    },
    {
      title: "本对象角色",
      dataIndex: "role",
      key: "role",
      width: 120,
      render: (v) => OBJECT_ROLE_LABEL[v] || v,
    },
    {
      title: "绑定来源",
      dataIndex: "source",
      key: "source",
      width: 110,
      render: (v) => (v === "manual" ? <Tag color="blue">人工</Tag> : <Tag>推断</Tag>),
    },
    {
      title: "状态",
      dataIndex: "logic_status",
      key: "logic_status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
    ...(inWorkspace
      ? [
          {
            title: "操作",
            key: "action",
            width: 100,
            render: (_: unknown, record: ObjectTypeLogicBinding) => (
              <Popconfirm
                title="确认解除该业务逻辑绑定？"
                onConfirm={() => handleUnbind(record.binding_id)}
                okButtonProps={{ loading: unbindingId === record.binding_id }}
              >
                <Button type="link" danger size="small">
                  解绑
                </Button>
              </Popconfirm>
            ),
          } as ColumnsType<ObjectTypeLogicBinding>[number],
        ]
      : []),
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

  const allRelations = [...obj.outgoing_relations, ...obj.incoming_relations];
  const versionRecords = obj.version_records ?? [];

  return (
    <PageContainer full>
      <PageHeader
        icon={<ApartmentOutlined />}
        title={inWorkspace ? "编辑对象类型" : obj.display_name}
        description={inWorkspace ? obj.display_name : obj.description || "暂无描述"}
        extra={
          <Space>
            <StatusBadge status={obj.status} />
            {inWorkspace ? (
              <>
                <Button
                  loading={saving}
                  onClick={handleSave}
                  icon={<SaveOutlined />}
                >
                  保存
                </Button>
                <Button
                  type="primary"
                  loading={prePublishing}
                  disabled={!canPrePublish}
                  onClick={handlePrePublish}
                  icon={<SendOutlined />}
                >
                  预发布
                </Button>
              </>
            ) : obj.domain_context_id ? (
              <Link to={`/workspace/${obj.domain_context_id}/objects/${obj.id}`}>
                <Button>前往工作区编辑</Button>
              </Link>
            ) : null}
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
        <Col xs={24} lg={10}>
          <SectionCard title="基本信息" icon={<ApartmentOutlined />}>
            <DataHubSourceLink
              sourceRef={obj.source_ref}
              datahubUrl={obj.datahub_url}
              datahubBase={datahubBase}
            />

            {inWorkspace ? (
              <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
                <Form.Item
                  label="显示名称"
                  name="display_name"
                  rules={[{ required: true, message: "请输入显示名称" }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item
                  label="标识名"
                  name="name"
                  rules={[{ required: true, message: "请输入标识名" }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item label="描述" name="description">
                  <Input.TextArea rows={3} />
                </Form.Item>
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="置信度">
                    {obj.source_confidence?.toFixed(2) ?? "-"}
                  </Descriptions.Item>
                </Descriptions>
              </Form>
            ) : (
              <Descriptions
                column={1}
                size="small"
                style={{ marginTop: 16 }}
                labelStyle={{ width: 96 }}
              >
                <Descriptions.Item label="数据域">
                  {obj.domain_name || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="标识名">{obj.name}</Descriptions.Item>
                <Descriptions.Item label="描述">
                  {obj.description || "暂无描述"}
                </Descriptions.Item>
                <Descriptions.Item label="置信度">
                  {obj.source_confidence?.toFixed(2) ?? "-"}
                </Descriptions.Item>
              </Descriptions>
            )}
          </SectionCard>
        </Col>

        <Col xs={24} lg={14}>
          <SectionCard
            title="属性"
            count={properties.length}
            countPrimary
            icon={<AppstoreOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={inWorkspace ? editablePropertyColumns : readOnlyPropertyColumns}
              dataSource={properties}
              pagination={false}
            />
          </SectionCard>
        </Col>
      </Row>

      <SectionCard
        title="关系图谱"
        count={relationCount}
        icon={<BranchesOutlined />}
        bodyFlush
      >
        {relationCount === 0 ? (
          <EmptyState title="暂无关系" description="该对象尚未建立与其他对象的关系。" />
        ) : (
          <ObjectRelationGraph
            obj={obj}
            objectDetailPath={objectDetailPath}
            relationDetailPath={relationDetailPath}
          />
        )}
      </SectionCard>

      {allRelations.length > 0 && (
        <SectionCard
          title="关系列表"
          count={allRelations.length}
          icon={<BranchesOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={relationColumns}
            dataSource={allRelations}
            pagination={false}
          />
        </SectionCard>
      )}

      <SectionCard
        title="绑定的业务逻辑"
        count={obj.business_logic_bindings?.length ?? 0}
        icon={<FunctionOutlined />}
        bodyFlush
      >
        {(obj.business_logic_bindings?.length ?? 0) === 0 ? (
          <EmptyState
            title={
              inWorkspace ? "该对象尚未绑定业务逻辑" : "暂无绑定业务逻辑"
            }
            description={
              inWorkspace
                ? "可在下方手动添加绑定，将对象作为主对象 / 维度对象 / 产出对象关联到业务逻辑。"
                : undefined
            }
          />
        ) : (
          <Table
            className="om-table"
            rowKey="binding_id"
            size="middle"
            columns={logicBindingColumns}
            dataSource={obj.business_logic_bindings}
            pagination={false}
          />
        )}

        {inWorkspace && (
          <div
            style={{
              padding: "16px 20px",
              borderTop: "1px dashed var(--om-border)",
              background: "var(--om-surface-muted)",
            }}
          >
            <Space wrap>
              <Select
                style={{ minWidth: 260 }}
                placeholder="选择要绑定的业务逻辑"
                value={bindingLogicId}
                onChange={setBindingLogicId}
                showSearch
                optionFilterProp="label"
                options={availableLogics.map((l) => ({
                  label: `${l.display_name}（${l.logic_type}）`,
                  value: l.id,
                }))}
                notFoundContent={
                  availableLogics.length === 0 ? "本本体下暂无业务逻辑" : undefined
                }
              />
              <Select
                style={{ width: 150 }}
                value={bindingRole}
                onChange={setBindingRole}
                options={OBJECT_ROLE_OPTIONS}
              />
              <Button
                type="primary"
                loading={bindingLoading}
                onClick={handleAddBinding}
              >
                添加绑定
              </Button>
            </Space>
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                将该对象作为所选角色绑定到业务逻辑，类似于数仓中表绑定到指标 / 标签。
              </Text>
            </div>
          </div>
        )}
      </SectionCard>

      {!inWorkspace && (
        <SectionCard
          title="关联业务逻辑"
          count={obj.business_logics.length}
          icon={<FunctionOutlined />}
          bodyFlush
        >
          {obj.business_logics.length === 0 ? (
            <EmptyState title="暂无关联业务逻辑" />
          ) : (
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={logicColumns}
              dataSource={obj.business_logics}
              pagination={false}
            />
          )}
        </SectionCard>
      )}

      {!inWorkspace && versionRecords.length > 0 && (
        <SectionCard
          title="版本记录"
          count={versionRecords.length}
          icon={<HistoryOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={versionColumns}
            dataSource={versionRecords}
            pagination={false}
          />
        </SectionCard>
      )}
    </PageContainer>
  );
}
