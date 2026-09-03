import {
  ApartmentOutlined,
  AppstoreOutlined,
  AuditOutlined,
  DatabaseOutlined,
  FunctionOutlined,
  HistoryOutlined,
  LinkOutlined,
  NodeIndexOutlined,
  PlusOutlined,
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
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { EntityEditToolbar, MappingDatasetSelect } from "../components/entity-edit";
import { FieldAuthorityPanel } from "../components/FieldAuthorityPanel";
import { MaterializeModal } from "../components/MaterializeModal";
import { DerivedDefinitionPanel } from "../components/DerivedDefinitionPanel";
import { ObjectLandingPanel } from "../components/ObjectLanding";
import { ObjectRelationGraph } from "../components/ObjectRelationGraph";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { RelationTriples } from "../components/RelationTriples";
import { DecisionEvidencePanel } from "../components/review/DecisionEvidence";
import { useDebouncedCallback } from "../hooks/useApi";
import { extractDataHubBase, resolveDataHubDatasetUrl } from "../utils/datahub";
import { suggestEndpoints } from "../utils/endpointSuggest";
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
import { ROLE_OPTIONS } from "../utils/role";

const { Text } = Typography;

interface BasicForm {
  name: string;
  display_name: string;
  description?: string;
  table_role: string;
  needs_review: boolean;
  segment_id?: string;
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

interface ConvertForm {
  source_object_type_id: string;
  target_object_type_id: string;
  display_name: string;
  structure_type: string;
}

// 从对象展示名提取默认关系谓词：剥离常见的「表/记录/明细/工单/单/流水/台账」等
// 结构性后缀，让「设备维修工单」→「设备维修」这样的动词直接作为关系谓词候选。
function defaultRelationVerb(displayName: string): string {
  const cleaned = displayName.replace(
    /(工单记录|操作记录|台账|流水|工单|明细表|明细|记录|事实表|事实|单据|单|表)$/,
    "",
  );
  return (cleaned.trim() || displayName).slice(0, 8);
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

function propertyRole(property: Property): "主键" | "外键" | "普通属性" {
  const name = (property.name || "").toLowerCase();
  if (name === "id") return "主键";
  if (name.endsWith("_id") || property.semantic_type === "identifier") return "外键";
  return "普通属性";
}

export function ObjectTypeDetailPage() {
  const { objectId, domainId } = useParams<{ objectId: string; domainId?: string }>();
  const navigate = useNavigate();

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
  const [convertModalOpen, setConvertModalOpen] = useState(false);
  const [convertSaving, setConvertSaving] = useState(false);
  const [convertForm] = Form.useForm<ConvertForm>();
  const [peerObjects, setPeerObjects] = useState<ObjectTypeSummary[]>([]);
  const [activeTab, setActiveTab] = useState("basic");
  const [materializeOpen, setMaterializeOpen] = useState(false);
  const [datasetOptions, setDatasetOptions] = useState<DataHubDatasetOption[]>([]);
  const [datasetSearching, setDatasetSearching] = useState(false);
  const [ensuringDataset, setEnsuringDataset] = useState(false);
  const [segments, setSegments] = useState<Array<{ id: string; display_name: string }>>([]);
  const inWorkspace = Boolean(domainId);

  useEffect(() => {
    if (!inWorkspace) setActiveTab("profile");
  }, [inWorkspace]);

  const watchedStructureType = Form.useWatch("structure_type", relationForm) as string | undefined;
  const needsMappingTable =
    watchedStructureType === "bridge_table" || watchedStructureType === "fact_table";

  const loadObject = async () => {
    if (!objectId) return;
    // 非工作区（即本体浏览）只取已发布实体，与 Data Agent 接地集一致，
    // 避免详情图谱/关系表泄露未发布的“建议”状态对象与关系。
    const detail = await api.getObjectType(objectId, !inWorkspace);
    setObj(detail);
    setProperties(detail.properties.map((p) => ({ ...p })));
    form.setFieldsValue({
      name: detail.name,
      display_name: detail.display_name,
      description: detail.description,
      table_role: detail.table_role || "business_object",
      needs_review: Boolean(detail.needs_review),
      segment_id: detail.segment_id || "",
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
          setDatahubBase(config.datahub_frontend_url ?? config.datahub_gms_url);
        }
        if (detail?.ontology_id) {
          try {
            const peers = await api.listObjectTypes({ ontologyId: detail.ontology_id });
            setPeerObjects(peers.items);
          } catch {
            setPeerObjects([]);
          }
          // 加载板块列表
          try {
            const segmentList = await api.listSegments({ ontologyId: detail.ontology_id });
            setSegments(
              segmentList.items.map((s) => ({ id: s.id, display_name: s.display_name })),
            );
          } catch {
            setSegments([]);
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
    api
      .searchDatahubDatasets({ query: keyword, ontologyId: obj.ontology_id })
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
                ? {
                    ...item,
                    object_type_id: newObj.id,
                    object_type_display_name: newObj.display_name,
                  }
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
        rel.structure_type || inferRelationStructureType(rel.description, rel.source_evidence),
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

  // 转为业务关系：把当前被误判为业务对象的事实/明细/动作表，转成一条两端点间的
  // 业务关系（原表作为实现表）。仅工作区可用；转换后原对象降级 bridge，跳转到新关系。
  const endpointCandidates = useMemo(
    () => peerObjects.filter((o) => o.id !== objectId),
    [peerObjects, objectId],
  );
  const endpointOptions = useMemo(
    () => endpointCandidates.map((o) => ({ label: o.display_name, value: o.id })),
    [endpointCandidates],
  );
  // 按本表列名反推候选端点键（真实源零 FK，端点只能从列名匹配对象名得到）。
  const endpointSuggestions = useMemo(
    () => suggestEndpoints(properties, endpointCandidates),
    [properties, endpointCandidates],
  );

  // 点选建议：依次填入尚空的源/目标端点（都已填则替换目标，避免与源重复）。
  const applyEndpointSuggestion = (id: string) => {
    const src = convertForm.getFieldValue("source_object_type_id");
    if (!src) convertForm.setFieldValue("source_object_type_id", id);
    else if (id !== src) convertForm.setFieldValue("target_object_type_id", id);
  };

  const openConvertModal = () => {
    if (!obj) return;
    convertForm.resetFields();
    convertForm.setFieldsValue({
      display_name: defaultRelationVerb(obj.display_name),
      structure_type: "fact_table",
    });
    setConvertModalOpen(true);
  };

  const handleConvert = async () => {
    if (!objectId) return;
    const values = await convertForm.validateFields();
    if (values.source_object_type_id === values.target_object_type_id) {
      message.error("源对象与目标对象不能相同");
      return;
    }
    setConvertSaving(true);
    try {
      const res = await api.convertObjectToRelation(objectId, {
        source_object_type_id: values.source_object_type_id,
        target_object_type_id: values.target_object_type_id,
        display_name: values.display_name,
        structure_type: values.structure_type,
      });
      setConvertModalOpen(false);
      message.success(
        res.promoted_endpoints.length
          ? `已转为业务关系「${res.relation.display_name}」；已将 ${res.promoted_endpoints.join(
              "、",
            )} 提升为业务对象`
          : `已转为业务关系「${res.relation.display_name}」`,
      );
      navigate(relationDetailPath(res.relation.id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "转换失败");
    } finally {
      setConvertSaving(false);
    }
  };

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

  const canPrePublish = obj.status !== "pre_published" && obj.status !== "published";
  // 关系表(bridge)实现(mapping)的业务关系并入计数/列表/图谱：桥表本身非端点，
  // 这些才是它连接的业务对象（供应商→科目 等），否则其关系列表/图谱恒为空。
  const implementedRelations = obj.implemented_relations ?? [];
  const relationCount =
    obj.outgoing_relations.length + obj.incoming_relations.length + implementedRelations.length;

  const relationColumns: ColumnsType<RelationType> = [
    {
      title: "源表",
      key: "source",
      width: 160,
      render: (_, record) => {
        const path =
          objectDetailPath?.(record.source_object_type_id) ??
          `/ontology/${record.source_object_type_id}`;
        return <Link to={path}>{record.source_object_name || record.source_object_type_id}</Link>;
      },
    },
    {
      title: "关系",
      dataIndex: "display_name",
      key: "display_name",
      width: 120,
      render: (_, record) => <Link to={relationDetailPath(record.id)}>{record.display_name}</Link>,
    },
    {
      title: "目标表",
      key: "target",
      width: 160,
      render: (_, record) => {
        const path =
          objectDetailPath?.(record.target_object_type_id) ??
          `/ontology/${record.target_object_type_id}`;
        return <Link to={path}>{record.target_object_name || record.target_object_type_id}</Link>;
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
      render: (_, record) => <Link to={`/business-logic/${record.id}`}>{record.display_name}</Link>,
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

  const allRelations = [
    ...obj.outgoing_relations,
    ...obj.incoming_relations,
    ...implementedRelations,
  ];
  const versionRecords = obj.version_records ?? [];
  const propertyGroups = ["主键", "外键", "普通属性"].map((label) => ({
    label,
    items: properties.filter((property) => propertyRole(property) === label),
  }));

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
              <ProvenanceBadge provenance={obj} />
              {inWorkspace && (
                <FieldAuthorityPanel
                  entityType="object_type"
                  entityId={obj.id}
                  pinnedFields={obj.pinned_fields}
                  onChanged={() => void loadObject()}
                />
              )}
              {obj.table_role === "business_object" && (
                <Tooltip title="把该业务对象物化到目标存储（建表落数，需 publisher 角色）">
                  <Button icon={<DatabaseOutlined />} onClick={() => setMaterializeOpen(true)}>
                    物化
                  </Button>
                </Tooltip>
              )}
              {inWorkspace ? (
                <EntityEditToolbar
                  saving={saving}
                  prePublishing={prePublishing}
                  canPrePublish={canPrePublish}
                  onSave={handleSave}
                  onPrePublish={handlePrePublish}
                />
              ) : obj.domain_context_id ? (
                <Link to={`/workspace/${obj.domain_context_id}/objects/${obj.id}`}>
                  <Button>前往工作区编辑</Button>
                </Link>
              ) : null}
            </Space>
          }
        />

        {obj.table_role === "business_object" && obj.ontology_id && (
          <MaterializeModal
            open={materializeOpen}
            onClose={() => setMaterializeOpen(false)}
            ontologyId={obj.ontology_id}
            // 单实体物化：以该对象自身的发布状态给出草稿警示，
            // 不能用 inWorkspace 一刀切当草稿——工作区里的对象也可能已发布。
            ontologyStatus={obj.status}
            scopeTargetId={obj.id}
            scopeLabel={obj.display_name}
          />
        )}

        {error && (
          <Alert type="error" message={error} showIcon closable onClose={() => setError(null)} />
        )}

        <section className="section-card">
          <Tabs
            className="om-tabs om-tabs--inset"
            activeKey={activeTab}
            onChange={setActiveTab}
            items={[
              ...(!inWorkspace
                ? [
                    {
                      key: "profile",
                      label: (
                        <span>
                          <ApartmentOutlined style={{ marginRight: 6 }} />
                          对象档案
                        </span>
                      ),
                      children: (
                        <div className="object-profile">
                          <section className="object-profile-section">
                            <h3>对象概览</h3>
                            <Descriptions column={{ xs: 1, md: 2, xl: 4 }} size="small">
                              <Descriptions.Item label="数据域">{obj.domain_name || "-"}</Descriptions.Item>
                              <Descriptions.Item label="标识名">{obj.name}</Descriptions.Item>
                              <Descriptions.Item label="所属板块">
                                {obj.segment_id ? (
                                  <Link to={`/segments/${obj.segment_id}?published=1`}>
                                    {obj.segment_name || obj.segment_id}
                                  </Link>
                                ) : "未接入"}
                              </Descriptions.Item>
                              <Descriptions.Item label="复核状态">
                                {obj.needs_review ? <Tag color="orange">待复核</Tag> : <Tag color="green">已确认</Tag>}
                              </Descriptions.Item>
                              <Descriptions.Item label="描述" span={4}>
                                {obj.description || "暂无描述"}
                              </Descriptions.Item>
                            </Descriptions>
                          </section>
                          <section className="object-profile-section">
                            <h3>属性{properties.length > 0 ? ` (${properties.length})` : ""}</h3>
                            {propertyGroups.map((group) => (
                              <div key={group.label} style={{ marginBottom: 12 }}>
                                <div style={{ marginBottom: 6, fontSize: 12, color: "var(--om-text-secondary)" }}>
                                  {group.label} ({group.items.length})
                                </div>
                                <Table
                                  className="om-table"
                                  rowKey="id"
                                  size="small"
                                  columns={readOnlyPropertyColumns}
                                  dataSource={group.items}
                                  pagination={false}
                                />
                              </div>
                            ))}
                          </section>
                          <section className="object-profile-section">
                            <h3>外键关系{relationCount > 0 ? ` (${relationCount})` : ""}</h3>
                            <div className="object-profile-relations">
                              <div>
                                <h4>出向关系 ({obj.outgoing_relations.length})</h4>
                                <RelationTriples
                                  relations={obj.outgoing_relations}
                                  currentObjectId={obj.id}
                                  objectDetailPath={objectDetailPath}
                                  relationDetailPath={relationDetailPath}
                                  limit={0}
                                />
                              </div>
                              <div>
                                <h4>入向关系 ({obj.incoming_relations.length})</h4>
                                <RelationTriples
                                  relations={obj.incoming_relations}
                                  currentObjectId={obj.id}
                                  objectDetailPath={objectDetailPath}
                                  relationDetailPath={relationDetailPath}
                                  limit={0}
                                />
                              </div>
                              {implementedRelations.length > 0 && (
                                <div>
                                  <h4>承载关系 ({implementedRelations.length})</h4>
                                  <RelationTriples
                                    relations={implementedRelations}
                                    currentObjectId={obj.id}
                                    objectDetailPath={objectDetailPath}
                                    relationDetailPath={relationDetailPath}
                                    limit={0}
                                  />
                                </div>
                              )}
                            </div>
                          </section>
                          {relationCount > 0 && (
                            <section className="object-profile-section">
                              <h3>邻域图</h3>
                              <ObjectRelationGraph
                                obj={obj}
                                objectDetailPath={objectDetailPath}
                                relationDetailPath={relationDetailPath}
                                height={520}
                                embedded
                              />
                            </section>
                          )}
                        </div>
                      ),
                    },
                  ]
                : []),
              {
                key: "basic",
                label: (
                  <span>
                    <ApartmentOutlined style={{ marginRight: 6 }} />
                    基本信息
                  </span>
                ),
                children: (
                  <>
                    <div className="om-tab-toolbar">
                      <DataHubSourceLink
                        sourceRef={obj.source_ref}
                        datahubUrl={obj.datahub_url}
                        datahubBase={datahubBase}
                      />
                    </div>
                    <div className="om-tab-body">
                      {inWorkspace ? (
                        <Form form={form} layout="vertical">
                          <Row gutter={20}>
                            <Col xs={24} md={8}>
                              <Form.Item
                                label="显示名称"
                                name="display_name"
                                rules={[{ required: true, message: "请输入显示名称" }]}
                              >
                                <Input />
                              </Form.Item>
                            </Col>
                            <Col xs={24} md={8}>
                              <Form.Item
                                label="标识名"
                                name="name"
                                rules={[{ required: true, message: "请输入标识名" }]}
                              >
                                <Input />
                              </Form.Item>
                            </Col>
                            <Col xs={24} md={8}>
                              <Form.Item label="命名置信度">
                                <Input value={obj.source_confidence?.toFixed(2) ?? "-"} disabled />
                              </Form.Item>
                            </Col>
                          </Row>
                          <Form.Item label="描述" name="description" style={{ marginBottom: 16 }}>
                            <Input.TextArea rows={2} />
                          </Form.Item>
                          <Row gutter={20}>
                            <Col xs={24} md={8}>
                              <Form.Item label="对象类型" name="table_role">
                                <Select
                                  options={ROLE_OPTIONS}
                                  onChange={() => form.setFieldValue("needs_review", false)}
                                />
                              </Form.Item>
                            </Col>
                            <Col xs={24} md={8}>
                              {/* 每个对象恰好属于一个板块，所以这里没有「移出板块」这个选项
                                  ——移出等于让它从所有板块视图里消失。分错了就移到别的板块，
                                  确实不是业务数据的移到「系统表」。 */}
                              <Form.Item
                                label="板块归属"
                                name="segment_id"
                                extra="分错板块就直接移走；不是业务数据的移到「系统表」"
                              >
                                <Select
                                  showSearch
                                  optionFilterProp="label"
                                  placeholder="选择板块"
                                  options={segments.map((s) => ({
                                    label: s.display_name,
                                    value: s.id,
                                  }))}
                                />
                              </Form.Item>
                            </Col>
                            <Col xs={24} md={8}>
                              <Form.Item
                                label="复核状态"
                                name="needs_review"
                                extra="改判对象类型后将自动置为已确认"
                                style={{ marginBottom: 0 }}
                              >
                                <Select
                                  options={[
                                    { label: "已确认", value: false },
                                    { label: "待复核", value: true },
                                  ]}
                                />
                              </Form.Item>
                            </Col>
                          </Row>
                        </Form>
                      ) : (
                        <>
                          <Descriptions column={{ xs: 1, md: 2, xl: 4 }} size="small">
                            <Descriptions.Item label="数据域">
                              {obj.domain_name || "-"}
                            </Descriptions.Item>
                            <Descriptions.Item label="标识名">{obj.name}</Descriptions.Item>
                            <Descriptions.Item label="命名置信度">
                              {obj.source_confidence?.toFixed(2) ?? "-"}
                            </Descriptions.Item>
                            <Descriptions.Item label="所属板块">
                              {obj.segment_id ? (
                                <Link to={`/segments/${obj.segment_id}`}>
                                  {obj.segment_name || obj.segment_id}
                                </Link>
                              ) : (
                                "-"
                              )}
                            </Descriptions.Item>
                            <Descriptions.Item label="描述" span={4}>
                              {obj.description || "暂无描述"}
                            </Descriptions.Item>
                          </Descriptions>
                        </>
                      )}
                      {/* 物化/同步/清洗任务把这个对象落成的物理表。它们不是新的业务对象，
                          故只在这里以落点呈现，不进对象列表。落点是执行结果、不是可编辑
                          字段，所以放在编辑表单之外——工作区与浏览态都要看得见。 */}
                      <ObjectLandingPanel landing={obj.landing} />
                      {/* 派生对象的数据来自别的对象的落点，那条血缘只有这里讲得清。 */}
                      <DerivedDefinitionPanel
                        objectId={obj.id}
                        provenance={obj.source_provenance}
                      />
                      {/* 外键关系：最小可读单元是「对象-关系-对象」，基本信息 Tab
                          必须让人看到完整的一条，不能只给计数。文档 §2.F1 */}
                      {relationCount > 0 && (
                        <div style={{ marginTop: 24 }}>
                          <h3 style={{ marginBottom: 12, fontSize: 14, fontWeight: 600 }}>
                            关系 ({relationCount})
                          </h3>
                          <RelationTriples
                            relations={allRelations}
                            currentObjectId={obj.id}
                            objectDetailPath={objectDetailPath}
                            relationDetailPath={relationDetailPath}
                            limit={10}
                          />
                        </div>
                      )}
                    </div>
                  </>
                ),
              },
              ...(inWorkspace || obj.role_reason || obj.role_signals
                ? [
                    {
                      key: "evidence",
                      label: (
                        <span>
                          <AuditOutlined style={{ marginRight: 6 }} />
                          判定依据
                        </span>
                      ),
                      children: (
                        <>
                          {inWorkspace && canPrePublish && (
                            <div className="om-tab-toolbar">
                              <Button icon={<ShareAltOutlined />} onClick={openConvertModal}>
                                转为业务关系
                              </Button>
                            </div>
                          )}
                          <div className="om-tab-body">
                            <DecisionEvidencePanel obj={obj} />
                          </div>
                        </>
                      ),
                    },
                  ]
                : []),
              {
                key: "properties",
                label: (
                  <span>
                    <AppstoreOutlined style={{ marginRight: 6 }} />
                    属性{properties.length > 0 ? ` (${properties.length})` : ""}
                  </span>
                ),
                children: (
                  <Table
                    className="om-table"
                    rowKey="id"
                    size="small"
                    columns={inWorkspace ? editablePropertyColumns : readOnlyPropertyColumns}
                    dataSource={properties}
                    pagination={false}
                  />
                ),
              },
              {
                key: "relations",
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
                        <Button
                          type="primary"
                          icon={<PlusOutlined />}
                          onClick={openAddRelationModal}
                        >
                          新增关系
                        </Button>
                      </div>
                    )}
                    <div className="om-tab-body">
                      {relationCount === 0 ? (
                        <EmptyState
                          title="暂无关系"
                          description={
                            inWorkspace
                              ? "点击「新增关系」按钮创建关系"
                              : "该对象尚未建立与其他对象的关系。"
                          }
                        />
                      ) : (
                        <Table
                          className="om-table"
                          rowKey="id"
                          size="small"
                          columns={relationColumns}
                          dataSource={allRelations}
                          scroll={{ x: "max-content" }}
                          pagination={false}
                        />
                      )}
                    </div>
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
                children:
                  relationCount === 0 ? (
                    <EmptyState
                      title="暂无关系图谱"
                      description="该对象尚未建立与其他对象的关系。"
                    />
                  ) : (
                    <ObjectRelationGraph
                      obj={obj}
                      objectDetailPath={objectDetailPath}
                      relationDetailPath={relationDetailPath}
                      height={640}
                      embedded
                    />
                  ),
              },
              {
                key: "logic",
                label: (
                  <span>
                    <FunctionOutlined style={{ marginRight: 6 }} />
                    业务逻辑
                    {obj.business_logics.length > 0 ? ` (${obj.business_logics.length})` : ""}
                  </span>
                ),
                children:
                  obj.business_logics.length === 0 ? (
                    <EmptyState title="暂无关联业务逻辑" />
                  ) : (
                    <Table
                      className="om-table"
                      rowKey="id"
                      size="small"
                      columns={logicColumns}
                      dataSource={obj.business_logics}
                      pagination={false}
                    />
                  ),
              },
              ...(!inWorkspace && versionRecords.length > 0
                ? [
                    {
                      key: "versions",
                      label: (
                        <span>
                          <HistoryOutlined style={{ marginRight: 6 }} />
                          版本记录 ({versionRecords.length})
                        </span>
                      ),
                      children: (
                        <Table
                          className="om-table"
                          rowKey="id"
                          size="small"
                          columns={versionColumns}
                          dataSource={versionRecords}
                          pagination={false}
                        />
                      ),
                    },
                  ]
                : []),
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
                options={RELATION_STRUCTURE_OPTIONS.map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
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
                <MappingDatasetSelect
                  options={datasetOptions}
                  searching={datasetSearching}
                  ensuring={ensuringDataset}
                  onSearch={searchDatasets}
                  onSelectUnmapped={(ds) => void handleDatasetSelect(ds)}
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

        <Modal
          title="转为业务关系"
          open={convertModalOpen}
          onOk={handleConvert}
          okText="转换"
          cancelText="取消"
          confirmLoading={convertSaving}
          onCancel={() => setConvertModalOpen(false)}
          width={600}
          destroyOnClose
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="这张表是一次业务事实，不是一个业务对象"
            description={
              <>
                「{obj.display_name}」的每行是一次业务动作/事实（如维修、清算、交易），真正的
                业务对象是它引用的键。转换后：以本表为<b>实现表</b>在下面两个端点对象间建立一条
                业务关系，本对象降级为「关系表」离开业务对象集（可逆）。端点若不是业务对象将被
                自动提升。
              </>
            }
          />
          {properties.length > 0 && (
            <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
              本表字段（供识别端点键）：
              {properties
                .slice(0, 12)
                .map((p) => p.display_name || p.name)
                .join("、")}
              {properties.length > 12 ? " …" : ""}
            </Typography.Paragraph>
          )}
          <Form form={convertForm} layout="vertical">
            <Form.Item
              label="关系语义词"
              name="display_name"
              rules={[...RELATION_TERM_RULES]}
              extra="读成「源对象 [关系词] 目标对象」，如「维修」「清算」；默认由表名推得"
            >
              <Input placeholder="如：维修" maxLength={RELATION_TERM_MAX_LENGTH} showCount />
            </Form.Item>
            {endpointSuggestions.length > 0 && (
              <Form.Item label="建议端点键" extra="据本表列名匹配本体对象推断，点选填入下方端点">
                <Space size={[6, 6]} wrap>
                  {endpointSuggestions.map((s) => (
                    <Tag
                      key={s.object.id}
                      color="blue"
                      style={{ cursor: "pointer", marginInlineEnd: 0 }}
                      onClick={() => applyEndpointSuggestion(s.object.id)}
                      title={`命中列：${s.matchedColumn}`}
                    >
                      {s.object.display_name}
                    </Tag>
                  ))}
                </Space>
              </Form.Item>
            )}
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item
                  label="源端点对象"
                  name="source_object_type_id"
                  rules={[{ required: true, message: "请选择源端点对象" }]}
                  extra="本表引用的一个键，如「设备」"
                >
                  <Select
                    showSearch
                    optionFilterProp="label"
                    options={endpointOptions}
                    placeholder="关系的起点对象"
                    notFoundContent="本体内暂无其它对象可作端点"
                  />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  label="目标端点对象"
                  name="target_object_type_id"
                  rules={[{ required: true, message: "请选择目标端点对象" }]}
                  extra="本表引用的另一个键，如「维修工」"
                >
                  <Select
                    showSearch
                    optionFilterProp="label"
                    options={endpointOptions}
                    placeholder="关系的终点对象"
                    notFoundContent="本体内暂无其它对象可作端点"
                  />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item label="关系结构类型" name="structure_type">
              <Select
                options={RELATION_STRUCTURE_OPTIONS.filter((o) =>
                  ["fact_table", "bridge_table"].includes(o.value),
                ).map((o) => ({ label: o.label, value: o.value }))}
              />
            </Form.Item>
          </Form>
        </Modal>
      </div>
    </PageContainer>
  );
}
