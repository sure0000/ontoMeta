import {
  ApartmentOutlined,
  AppstoreOutlined,
  FunctionOutlined,
  HistoryOutlined,
  LinkOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  SaveOutlined,
  SendOutlined,
  ShareAltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Col,
  Descriptions,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tabs,
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
import { useDebouncedCallback } from "../hooks/useApi";
import { extractDataHubBase, resolveDataHubDatasetUrl } from "../utils/datahub";
import {
  CARDINALITY_OPTIONS,
  RELATION_STRUCTURE_OPTIONS,
  RELATION_TERM_MAX_LENGTH,
  RELATION_TERM_RULES,
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
  normalizeCardinality,
} from "../utils/relation";
import type {
  BusinessLogic,
  DataHubDatasetOption,
  ObjectTypeDetail,
  ObjectTypeSummary,
  Property,
  RelationType,
  VersionRecord,
} from "../types";

const { Text } = Typography;

interface BasicForm {
  name: string;
  display_name: string;
  description?: string;
}

interface RelationForm {
  display_name: string;
  description?: string;
  cardinality?: string;
  structure_type: string;
  source_object_type_id: string;
  target_object_type_id: string;
  mapping_object_type_id?: string | null;
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

  if (!url) {
    return <Text type="secondary">无关联 DataHub 表</Text>;
  }

  return (
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
  );
}

export function ObjectTypeDetailPage() {
  const { objectId, domainId } = useParams<{ objectId: string; domainId?: string }>();

  const [obj, setObj] = useState<ObjectTypeDetail | null>(null);
  const [datahubBase, setDatahubBase] = useState<string | undefined>();
  const [properties, setProperties] = useState<Property[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [prePublishing, setPrePublishing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form] = Form.useForm<BasicForm>();
  const [relationForm] = Form.useForm<RelationForm>();
  const [relationModalOpen, setRelationModalOpen] = useState(false);
  const [editingRelation, setEditingRelation] = useState<RelationType | null>(null);
  const [relationSaving, setRelationSaving] = useState(false);
  const [peerObjects, setPeerObjects] = useState<ObjectTypeSummary[]>([]);
  const [relationTab, setRelationTab] = useState("list");
  const [datasetOptions, setDatasetOptions] = useState<DataHubDatasetOption[]>([]);
  const [datasetSearching, setDatasetSearching] = useState(false);
  const [ensuringDataset, setEnsuringDataset] = useState(false);
  const inWorkspace = Boolean(domainId);

  const watchedStructureType = Form.useWatch("structure_type", relationForm) as string | undefined;
  const needsMappingTable = watchedStructureType === "bridge_table" || watchedStructureType === "fact_table";

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
            const peers = await api.listObjectTypes({ ontologyId: detail.ontology_id });
            setPeerObjects(peers);
          } catch {
            setPeerObjects([]);
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

  const handleSave = () => {
    Modal.confirm({
      title: "确认保存",
      content: "将保存当前对象类型及属性的修改。",
      okText: "确认保存",
      cancelText: "取消",
      onOk: async () => {
        setSaving(true);
        try {
          await persistChanges();
          message.success("保存成功");
        } catch (err) {
          message.error(err instanceof Error ? err.message : "保存失败");
        } finally {
          setSaving(false);
        }
      },
    });
  };

  const handlePrePublish = () => {
    if (!objectId || !inWorkspace) return;
    Modal.confirm({
      title: "确认预发布",
      content: "预发布后将把当前草稿固化为预发布状态，对外可见。此操作需要二次确认。",
      okText: "确认预发布",
      cancelText: "取消",
      onOk: async () => {
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
      },
    });
  };

  const updateProperty = (id: string, patch: Partial<Property>) => {
    setProperties((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  };

  const searchDatasets = useDebouncedCallback((keyword: string) => {
    if (!obj?.ontology_id) return;
    setDatasetSearching(true);
    api.searchDatahubDatasets({ query: keyword, ontologyId: obj.ontology_id })
      .then(setDatasetOptions)
      .catch((err) => message.error(err instanceof Error ? err.message : "搜索 DataHub 表失败"))
      .finally(() => setDatasetSearching(false));
  }, 300);

  const handleDatasetSelect = (option: DataHubDatasetOption) => {
    if (!obj?.ontology_id) return;
    if (option.object_type_id) {
      relationForm.setFieldValue("mapping_object_type_id", option.object_type_id);
      return;
    }
    Modal.confirm({
      title: "确认创建承载表对象",
      content: `将基于 DataHub 数据表「${option.display_name || option.name}」创建新的对象类型作为承载表。`,
      okText: "确认创建",
      cancelText: "取消",
      onOk: async () => {
        setEnsuringDataset(true);
        try {
          const newObj = await api.ensureObjectTypeFromDataset({
            ontology_id: obj!.ontology_id!,
            dataset_urn: option.urn,
          });
          relationForm.setFieldValue("mapping_object_type_id", newObj.id);
          setDatasetOptions((prev) =>
            prev.map((item) =>
              item.urn === option.urn
                ? { ...item, object_type_id: newObj.id, object_type_display_name: newObj.display_name }
                : item,
            ),
          );
          setPeerObjects((prev) =>
            prev.some((p) => p.id === newObj.id) ? prev : [...prev, newObj],
          );
        } catch (err) {
          message.error(err instanceof Error ? err.message : "创建承载表对象失败");
        } finally {
          setEnsuringDataset(false);
        }
      },
    });
  };

  const openAddRelationModal = () => {
    setEditingRelation(null);
    relationForm.resetFields();
    setDatasetOptions([]);
    relationForm.setFieldsValue({
      source_object_type_id: objectId,
      structure_type: "foreign_key",
    });
    setRelationModalOpen(true);
  };

  const openEditRelationModal = (rel: RelationType) => {
    setEditingRelation(rel);
    setDatasetOptions([]);
    relationForm.setFieldsValue({
      display_name: rel.display_name,
      description: rel.description,
      cardinality: normalizeCardinality(rel.cardinality),
      structure_type:
        rel.structure_type ||
        inferRelationStructureType(rel.description, rel.source_evidence),
      source_object_type_id: rel.source_object_type_id,
      target_object_type_id: rel.target_object_type_id,
      mapping_object_type_id: rel.mapping_object_type_id ?? undefined,
    });
    if (rel.mapping_object_type_id && rel.mapping_object_name) {
      setDatasetOptions([
        {
          urn: "",
          name: rel.mapping_object_name,
          display_name: rel.mapping_object_name,
          object_type_id: rel.mapping_object_type_id,
          object_type_display_name: rel.mapping_object_name,
        },
      ]);
    }
    setRelationModalOpen(true);
  };

  const handleRelationSave = async () => {
    const values = await relationForm.validateFields();
    setRelationSaving(true);
    try {
      const payload = {
        ...values,
        mapping_object_type_id:
          typeof values.mapping_object_type_id === "string" &&
          values.mapping_object_type_id.startsWith("dataset:")
            ? null
            : values.mapping_object_type_id,
      };
      if (editingRelation) {
        await api.updateRelationType(editingRelation.id, payload);
        message.success("关系已更新");
      } else if (obj?.ontology_id) {
        await api.createRelationType({
          ontology_id: obj.ontology_id,
          ...payload,
        });
        message.success("关系已创建");
      }
      setRelationModalOpen(false);
      await loadObject();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setRelationSaving(false);
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
      title: "源表",
      key: "source",
      width: 160,
      render: (_, record) => {
        const path = objectDetailPath?.(record.source_object_type_id) ?? `/ontology/${record.source_object_type_id}`;
        return (
          <Link to={path}>
            {record.source_object_name || record.source_object_type_id}
          </Link>
        );
      },
    },
    {
      title: "关系",
      dataIndex: "display_name",
      key: "display_name",
      width: 120,
      render: (_, record) => (
        <Link to={relationDetailPath(record.id)}>{record.display_name}</Link>
      ),
    },
    {
      title: "目标表",
      key: "target",
      width: 160,
      render: (_, record) => {
        const path = objectDetailPath?.(record.target_object_type_id) ?? `/ontology/${record.target_object_type_id}`;
        return (
          <Link to={path}>
            {record.target_object_name || record.target_object_type_id}
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
      render: (v) => normalizeCardinality(v) || <span className="om-muted">-</span>,
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
    ...(inWorkspace
      ? [
          {
            title: "操作",
            key: "action",
            width: 100,
            render: (_: unknown, record: RelationType) => (
              <Button type="link" size="small" onClick={() => openEditRelationModal(record)}>
                编辑
              </Button>
            ),
          } as ColumnsType<RelationType>[number],
        ]
      : []),
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
      <div className="om-stack">
      <PageHeader
        icon={<ApartmentOutlined />}
        title={obj.display_name}
        description={inWorkspace ? "编辑对象类型" : obj.description || "暂无描述"}
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

      <section className="section-card">
        <Tabs
          className="om-tabs om-tabs--inset"
          activeKey={relationTab}
          onChange={setRelationTab}
          items={[
            {
              key: "list",
              label: (
                <span>
                  <ShareAltOutlined style={{ marginRight: 6 }} />
                  关系列表{relationCount > 0 ? ` (${relationCount})` : ""}
                </span>
              ),
              children: (
                <>
                  {inWorkspace && (
                    <div className="om-tab-toolbar">
                      <Button type="primary" icon={<PlusOutlined />} onClick={openAddRelationModal}>
                        新增关系
                      </Button>
                    </div>
                  )}
                  {relationCount === 0 ? (
                    <EmptyState title="暂无关系" description={inWorkspace ? "点击「新增关系」按钮创建关系" : "该对象尚未建立与其他对象的关系。"} />
                  ) : (
                    <Table
                      className="om-table"
                      rowKey="id"
                      size="middle"
                      columns={relationColumns}
                      dataSource={allRelations}
                      scroll={{ x: "max-content" }}
                      pagination={false}
                    />
                  )}
                </>
              ),
            },
            {
              key: "graph",
              label: (
                <span>
                  <NodeIndexOutlined style={{ marginRight: 6 }} />
                  关系图谱
                </span>
              ),
              children: (
                relationCount === 0 ? (
                  <EmptyState title="暂无关系图谱" description="该对象尚未建立与其他对象的关系。" />
                ) : (
                  <ObjectRelationGraph
                    obj={obj}
                    objectDetailPath={objectDetailPath}
                    relationDetailPath={relationDetailPath}
                    defaultLayout="dagre"
                    embedded
                  />
                )
              ),
            },
          ]}
        />
      </section>

      <Modal
        title={editingRelation ? "编辑关系" : "新增关系"}
        open={relationModalOpen}
        onOk={handleRelationSave}
        okText={editingRelation ? "保存" : "创建"}
        cancelText="取消"
        confirmLoading={relationSaving}
        onCancel={() => setRelationModalOpen(false)}
        width={600}
        destroyOnClose
      >
        <Form form={relationForm} layout="vertical">
          <Form.Item
            label="关系语义词"
            name="display_name"
            rules={[...RELATION_TERM_RULES]}
            extra="填写 2-8 字动词或动宾短语，如「属于」「包含」「下单」"
          >
            <Input placeholder="如：属于" maxLength={RELATION_TERM_MAX_LENGTH} showCount />
          </Form.Item>
          <Form.Item label="语义描述" name="description">
            <Input.TextArea rows={3} placeholder="描述该关系的业务含义" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="源对象"
                name="source_object_type_id"
                rules={[{ required: true, message: "请选择源对象" }]}
              >
                <Select
                  options={peerObjects.map((o) => ({ label: o.display_name, value: o.id }))}
                  placeholder="关系的起点对象"
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="目标对象"
                name="target_object_type_id"
                rules={[{ required: true, message: "请选择目标对象" }]}
              >
                <Select
                  options={peerObjects.map((o) => ({ label: o.display_name, value: o.id }))}
                  placeholder="关系的终点对象"
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            label="关系结构类型"
            name="structure_type"
            rules={[{ required: true, message: "请选择关系结构类型" }]}
          >
            <Select
              options={RELATION_STRUCTURE_OPTIONS.map((o) => ({ label: o.label, value: o.value }))}
              placeholder="选择关系结构类型"
            />
          </Form.Item>
          {needsMappingTable && (
            <Form.Item
              label="映射表（承载表）"
              name="mapping_object_type_id"
              rules={[{ required: true, message: "请搜索并选择承载该关系的表" }]}
              extra={
                watchedStructureType === "bridge_table"
                  ? "桥表自身作为多对多关系的承载表"
                  : "事实表承载多个对象之间的关联"
              }
            >
              <Select
                showSearch
                allowClear
                loading={datasetSearching || ensuringDataset}
                placeholder="输入表名搜索 DataHub 表"
                optionFilterProp="label"
                filterOption={false}
                onSearch={searchDatasets}
                notFoundContent={
                  datasetSearching ? "搜索中..." : "输入关键字搜索 DataHub 表"
                }
                options={datasetOptions.map((ds) => ({
                  label: ds.display_name || ds.name,
                  value: ds.object_type_id ?? `dataset:${ds.urn}`,
                  dataset: ds,
                }))}
                onSelect={(_value, option) => {
                  const ds = (option as { dataset?: DataHubDatasetOption }).dataset;
                  if (ds && !ds.object_type_id) {
                    void handleDatasetSelect(ds);
                  }
                }}
                optionRender={(option) => {
                  const ds = (option as { dataset?: DataHubDatasetOption }).dataset;
                  if (!ds) return option.label;
                  return (
                    <Space size={6}>
                      <Text strong>{ds.display_name || ds.name}</Text>
                      {ds.object_type_id ? <Tag color="green">已映射</Tag> : <Tag color="blue">将创建</Tag>}
                    </Space>
                  );
                }}
              />
            </Form.Item>
          )}
          <Form.Item label="基数" name="cardinality">
            <Select
              allowClear
              options={CARDINALITY_OPTIONS.map((o) => ({ label: o.label, value: o.value }))}
              placeholder="选择关系基数"
            />
          </Form.Item>
        </Form>
      </Modal>

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
      </div>
    </PageContainer>
  );
}
