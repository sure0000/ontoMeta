import {
  ArrowRightOutlined,
  BranchesOutlined,
  BulbOutlined,
  DatabaseOutlined,
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
  Row,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { OntologyGraphView } from "../components/graph";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
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
  DataHubDatasetOption,
  ObjectTypeSummary,
  OntologyGraph,
  RelationTypeDetail,
} from "../types";

const { Text, Paragraph } = Typography;

interface RelationForm {
  display_name: string;
  description?: string;
  cardinality?: string;
  structure_type: string;
  mapping_object_type_id?: string | null;
  source_object_type_id: string;
  target_object_type_id: string;
}

const STRUCTURE_TYPES_REQUIRING_MAPPING_TABLE = new Set([
  "bridge_table",
  "fact_table",
]);

function ObjectTableCard({
  title,
  objectRef,
  datahubBase,
  detailPath,
}: {
  title: string;
  objectRef?: RelationTypeDetail["source_object"];
  datahubBase?: string;
  detailPath?: string;
}) {
  const url = resolveDataHubDatasetUrl(
    objectRef?.source_ref,
    objectRef?.datahub_url,
    datahubBase,
  );

  return (
    <div className="section-card" style={{ height: "100%" }}>
      <div className="section-card-head">
        <div className="section-card-head-title">{title}</div>
      </div>
      <div className="section-card-body" style={{ padding: 16 }}>
        {!objectRef ? (
          <Text type="secondary">未关联对象</Text>
        ) : (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {detailPath ? (
              <Link to={detailPath}>
                <Text strong>{objectRef.display_name}</Text>
              </Link>
            ) : (
              <Text strong>{objectRef.display_name}</Text>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {objectRef.name}
            </Text>
            {objectRef.source_ref ? (
              <Text
                code
                copyable
                style={{ wordBreak: "break-all", fontSize: 12 }}
              >
                {objectRef.source_ref}
              </Text>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                无关联 DataHub 表
              </Text>
            )}
            {url && (
              <Button
                type="link"
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ padding: 0, height: "auto" }}
                icon={<LinkOutlined />}
              >
                在 DataHub 中查看表
              </Button>
            )}
          </Space>
        )}
      </div>
    </div>
  );
}

function buildRelationGraph(rel: RelationTypeDetail): OntologyGraph | null {
  if (!rel.source_object || !rel.target_object) return null;

  const nodes: OntologyGraph["nodes"] = [
    {
      id: rel.source_object.id,
      label: rel.source_object.name,
      display_name: rel.source_object.display_name,
      status: rel.status,
    },
    {
      id: rel.target_object.id,
      label: rel.target_object.name,
      display_name: rel.target_object.display_name,
      status: rel.status,
    },
  ];
  if (rel.mapping_object) {
    nodes.push({
      id: rel.mapping_object.id,
      label: rel.mapping_object.name,
      display_name: rel.mapping_object.display_name,
      status: rel.status,
    });
  }

  const edges: OntologyGraph["edges"] = [
    {
      id: rel.id,
      relationId: rel.id,
      source: rel.source_object_type_id,
      target: rel.target_object_type_id,
      label: rel.display_name,
      cardinality: rel.cardinality,
    },
  ];
  if (rel.mapping_object) {
    edges.push({
      id: `${rel.id}-mapping-source`,
      source: rel.source_object_type_id,
      target: rel.mapping_object.id,
      label: "经由",
    });
    edges.push({
      id: `${rel.id}-mapping-target`,
      source: rel.mapping_object.id,
      target: rel.target_object_type_id,
      label: "抵达",
    });
  }

  return { nodes, edges };
}

export function RelationTypeDetailPage() {
  const { relationId, domainId } = useParams<{
    relationId: string;
    domainId?: string;
  }>();
  const inWorkspace = Boolean(domainId);

  const [rel, setRel] = useState<RelationTypeDetail | null>(null);
  const [peerObjects, setPeerObjects] = useState<ObjectTypeSummary[]>([]);
  const [datahubBase, setDatahubBase] = useState<string | undefined>();
  const [datasetOptions, setDatasetOptions] = useState<DataHubDatasetOption[]>([]);
  const [datasetSearching, setDatasetSearching] = useState(false);
  const [ensuringDataset, setEnsuringDataset] = useState(false);
  const datasetSearchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [prePublishing, setPrePublishing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form] = Form.useForm<RelationForm>();

  const watchedSource = Form.useWatch("source_object_type_id", form);
  const watchedTarget = Form.useWatch("target_object_type_id", form);
  const watchedStructureType = Form.useWatch("structure_type", form);
  const needsMappingTable = STRUCTURE_TYPES_REQUIRING_MAPPING_TABLE.has(
    watchedStructureType as string,
  );

  const loadRelation = async () => {
    if (!relationId) return;
    const detail = await api.getRelationType(relationId);
    setRel(detail);
    form.setFieldsValue({
      display_name: detail.display_name,
      description: detail.description,
      cardinality: normalizeCardinality(detail.cardinality),
      structure_type:
        detail.structure_type ||
        inferRelationStructureType(detail.description, detail.source_evidence),
      mapping_object_type_id: detail.mapping_object_type_id ?? null,
      source_object_type_id: detail.source_object_type_id,
      target_object_type_id: detail.target_object_type_id,
    });
    if (detail.mapping_object_type_id && detail.mapping_object) {
      setDatasetOptions((prev) => {
        if (prev.some((o) => o.object_type_id === detail.mapping_object_type_id)) {
          return prev;
        }
        return [
          {
            urn: detail.mapping_object!.source_ref || "",
            name: detail.mapping_object!.name,
            display_name: detail.mapping_object!.display_name,
            object_type_id: detail.mapping_object_type_id,
            object_type_display_name: detail.mapping_object!.display_name,
          },
          ...prev,
        ];
      });
    }
    return detail;
  };

  useEffect(() => {
    if (!relationId) return;
    setLoading(true);
    (async () => {
      try {
        const detail = await loadRelation();
        if (!detail) return;

        const [peers, domainOrConfig] = await Promise.all([
          domainId
            ? api.listObjectTypes({ domainId })
            : api.listObjectTypes({ ontologyId: detail.ontology_id }),
          domainId ? api.getDomain(domainId) : api.getConfig(),
        ]);
        setPeerObjects(peers);
        setDatahubBase(
          domainId
            ? extractDataHubBase(
                (domainOrConfig as { datahub_url?: string }).datahub_url,
              )
            : (
                (domainOrConfig as { datahub_frontend_url?: string; datahub_gms_url: string })
                  .datahub_frontend_url ??
                (domainOrConfig as { datahub_gms_url: string }).datahub_gms_url
              ),
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relationId, domainId]);

  const ontologyId = rel?.ontology_id;

  const searchDatasets = useMemo(
    () => (keyword: string) => {
      if (!ontologyId) return;
      if (datasetSearchTimer.current) {
        clearTimeout(datasetSearchTimer.current);
      }
      datasetSearchTimer.current = setTimeout(async () => {
        setDatasetSearching(true);
        try {
          const result = await api.searchDatahubDatasets({
            query: keyword,
            ontologyId,
          });
          setDatasetOptions(result);
        } catch (err) {
          message.error(
            err instanceof Error ? err.message : "搜索 DataHub 表失败",
          );
        } finally {
          setDatasetSearching(false);
        }
      }, 300);
    },
    [ontologyId],
  );

  const handleDatasetSelect = async (option: DataHubDatasetOption) => {
    if (!ontologyId) return;
    if (option.object_type_id) {
      form.setFieldValue("mapping_object_type_id", option.object_type_id);
      return;
    }
    setEnsuringDataset(true);
    try {
      const obj = await api.ensureObjectTypeFromDataset({
        ontology_id: ontologyId,
        dataset_urn: option.urn,
      });
      form.setFieldValue("mapping_object_type_id", obj.id);
      setDatasetOptions((prev) =>
        prev.map((item) =>
          item.urn === option.urn
            ? {
                ...item,
                object_type_id: obj.id,
                object_type_display_name: obj.display_name,
              }
            : item,
        ),
      );
      const newPeer: ObjectTypeSummary = {
        id: obj.id,
        name: obj.name,
        display_name: obj.display_name,
        description: obj.description,
        status: obj.status,
        property_count: obj.property_count,
        relation_count: obj.relation_count,
        business_logic_count: obj.business_logic_count,
        source_confidence: obj.source_confidence,
        updated_at: obj.updated_at,
      };
      setPeerObjects((prev) =>
        prev.some((p) => p.id === obj.id) ? prev : [...prev, newPeer],
      );
    } catch (err) {
      message.error(
        err instanceof Error ? err.message : "创建承载表对象失败",
      );
    } finally {
      setEnsuringDataset(false);
    }
  };

  const objectDetailPath = useMemo(() => {
    if (!domainId) return (id: string) => `/ontology/${id}`;
    return (id: string) => `/workspace/${domainId}/objects/${id}`;
  }, [domainId]);

  const handleSave = async () => {
    if (!relationId || !inWorkspace) return;
    setSaving(true);
    try {
      const values = await form.validateFields();
      const payload: typeof values = {
        ...values,
        mapping_object_type_id:
          typeof values.mapping_object_type_id === "string" &&
          values.mapping_object_type_id.startsWith("dataset:")
            ? null
            : values.mapping_object_type_id,
      };
      await api.updateRelationType(relationId, payload);
      await loadRelation();
      message.success("保存成功");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handlePrePublish = async () => {
    if (!relationId || !inWorkspace) return;
    setPrePublishing(true);
    try {
      const values = await form.validateFields();
      const payload: typeof values = {
        ...values,
        mapping_object_type_id:
          typeof values.mapping_object_type_id === "string" &&
          values.mapping_object_type_id.startsWith("dataset:")
            ? null
            : values.mapping_object_type_id,
      };
      await api.updateRelationType(relationId, payload);
      const updated = await api.prePublishRelationType(relationId);
      setRel((prev) => (prev ? { ...prev, status: updated.status } : prev));
      message.success("关系已预发布");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预发布失败");
    } finally {
      setPrePublishing(false);
    }
  };

  const graph = useMemo(() => (rel ? buildRelationGraph(rel) : null), [rel]);

  if (loading) return <PageSkeleton type="detail" />;

  if (!rel) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "关系不存在"} showIcon />
      </PageContainer>
    );
  }

  const objectOptions = peerObjects.map((o) => ({
    label: o.display_name,
    value: o.id,
  }));
  const sourceOptions = objectOptions.filter((o) => o.value !== watchedTarget);
  const targetOptions = objectOptions.filter((o) => o.value !== watchedSource);
  const mappingObjectRef = rel.mapping_object;
  const evidenceType = inferRelationEvidenceType(
    rel.source_evidence || rel.description,
  );
  const canPrePublish =
    rel.status !== "pre_published" && rel.status !== "published";
  const workspaceBackPath = domainId ? `/workspace/${domainId}` : "/workspace";

  return (
    <PageContainer full>
      <PageHeader
        icon={<BranchesOutlined />}
        title={inWorkspace ? "编辑关系类型" : rel.display_name}
        description={
          inWorkspace
            ? "修正关系语义词，用简短动词表达对象之间的业务关联"
            : rel.description || "暂无描述"
        }
        extra={
          <Space>
            <StatusBadge status={rel.status} />
            {inWorkspace ? (
              <>
                <Link to={workspaceBackPath}>
                  <Button>返回工作区</Button>
                </Link>
                <Button loading={saving} onClick={handleSave} icon={<SaveOutlined />}>
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

      <Form form={form} layout="vertical">
        <Row gutter={[20, 20]}>
          <Col xs={24} lg={12}>
            <div className="om-stack">
              <SectionCard title="关系语义" icon={<BulbOutlined />}>
                {inWorkspace ? (
                  <>
                    <Form.Item
                      label="关系语义词"
                      name="display_name"
                      rules={[...RELATION_TERM_RULES]}
                      extra="填写 2-8 字动词或动宾短语，如「属于」「包含」「下单」；完整说明写在下方语义描述"
                    >
                      <Input
                        placeholder="如：属于"
                        maxLength={RELATION_TERM_MAX_LENGTH}
                        showCount
                      />
                    </Form.Item>
                    <Form.Item
                      label="语义描述"
                      name="description"
                      extra="补充该关系的业务背景与依据，可写完整句子"
                    >
                      <Input.TextArea
                        rows={4}
                        placeholder="描述该关系的业务含义"
                      />
                    </Form.Item>
                    <Form.Item label="关系类型标识">
                      <Input value={rel.name} disabled />
                    </Form.Item>
                    <Form.Item
                      label="关系结构类型"
                      name="structure_type"
                      rules={[{ required: true, message: "请选择关系结构类型" }]}
                      extra="对应 SSOT 中的外键关系、桥表、事实表等数据平台结构"
                    >
                      <Select
                        placeholder="选择关系结构类型"
                        options={RELATION_STRUCTURE_OPTIONS.map((o) => ({
                          label: o.label,
                          value: o.value,
                        }))}
                      />
                    </Form.Item>
                    {needsMappingTable ? (
                      <Form.Item
                        label="承载表（DataHub 数据表）"
                        name="mapping_object_type_id"
                        rules={[
                          { required: true, message: "请搜索并选择承载该关系的表" },
                        ]}
                        extra={
                          watchedStructureType === "bridge_table"
                            ? "桥表自身作为多对多关系的承载表，从 DataHub 搜索对应的表"
                            : "事实表承载多个对象之间的关联，从 DataHub 搜索对应的事实表"
                        }
                      >
                        <Select
                          showSearch
                          allowClear
                          loading={datasetSearching || ensuringDataset}
                          placeholder="输入表名 / 显示名搜索 DataHub 表"
                          optionFilterProp="label"
                          optionLabelProp="label"
                          filterOption={false}
                          onSearch={searchDatasets}
                          notFoundContent={
                            datasetSearching
                              ? "搜索中..."
                              : "输入关键字搜索 DataHub 表"
                          }
                          options={datasetOptions.map((ds) => ({
                            label: ds.display_name || ds.name,
                            value: ds.object_type_id ?? `dataset:${ds.urn}`,
                            dataset: ds,
                          }))}
                          onSelect={(_value, option) => {
                            const ds = (option as { dataset?: DataHubDatasetOption })
                              .dataset;
                            if (ds && !ds.object_type_id) {
                              void handleDatasetSelect(ds);
                            }
                          }}
                          optionRender={(option) => {
                            const ds = (option as { dataset?: DataHubDatasetOption })
                              .dataset;
                            if (!ds) return option.label;
                            return (
                              <Space direction="vertical" size={0}>
                                <Space size={6}>
                                  <Text strong>{ds.display_name || ds.name}</Text>
                                  {ds.platform ? <Tag>{ds.platform}</Tag> : null}
                                  {ds.object_type_id ? (
                                    <Tag color="green">已映射</Tag>
                                  ) : (
                                    <Tag color="blue">将创建</Tag>
                                  )}
                                </Space>
                                {ds.description ? (
                                  <Text type="secondary" style={{ fontSize: 12 }}>
                                    {ds.description}
                                  </Text>
                                ) : null}
                                <Text type="secondary" code style={{ fontSize: 11 }}>
                                  {ds.urn}
                                </Text>
                              </Space>
                            );
                          }}
                        />
                      </Form.Item>
                    ) : null}
                    <Form.Item label="基数" name="cardinality">
                      <Select
                        allowClear
                        placeholder="选择关系基数"
                        options={CARDINALITY_OPTIONS.map((o) => ({
                          label: o.label,
                          value: o.value,
                        }))}
                      />
                    </Form.Item>
                    <Descriptions column={1} size="small" labelStyle={{ width: 96 }}>
                      <Descriptions.Item label="置信度">
                        {rel.source_confidence?.toFixed(2) ?? "-"}
                      </Descriptions.Item>
                    </Descriptions>
                  </>
                ) : (
                  <Descriptions
                    column={1}
                    size="small"
                    labelStyle={{ width: 110 }}
                  >
                    <Descriptions.Item label="标识名">{rel.name}</Descriptions.Item>
                    <Descriptions.Item label="关系结构类型">
                      {getRelationStructureLabel(
                        rel.structure_type ||
                          inferRelationStructureType(
                            rel.description,
                            rel.source_evidence,
                          ),
                      )}
                    </Descriptions.Item>
                    {rel.mapping_object ? (
                      <Descriptions.Item label="承载表">
                        <Link to={objectDetailPath(rel.mapping_object.id)}>
                          {rel.mapping_object.display_name}
                        </Link>
                      </Descriptions.Item>
                    ) : null}
                    <Descriptions.Item label="描述">
                      {rel.description || "暂无描述"}
                    </Descriptions.Item>
                    <Descriptions.Item label="基数">
                      {normalizeCardinality(rel.cardinality) || "-"}
                    </Descriptions.Item>
                    <Descriptions.Item label="置信度">
                      {rel.source_confidence?.toFixed(2) ?? "-"}
                    </Descriptions.Item>
                  </Descriptions>
                )}
              </SectionCard>

              <SectionCard title="来源证据" icon={<LinkOutlined />}>
                <Space direction="vertical" size={10}>
                  <Tag color="blue">{evidenceType}</Tag>
                  {rel.source_evidence ? (
                    <Paragraph style={{ marginBottom: 0 }}>
                      <Text
                        code
                        copyable
                        style={{
                          wordBreak: "break-all",
                          whiteSpace: "pre-wrap",
                          fontSize: 12,
                        }}
                      >
                        {rel.source_evidence}
                      </Text>
                    </Paragraph>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 13 }}>
                      暂无 DataHub 证据引用，请通过语义描述补充业务依据
                    </Text>
                  )}
                </Space>
              </SectionCard>
            </div>
          </Col>

          <Col xs={24} lg={12}>
            <SectionCard title="关联对象与表" icon={<DatabaseOutlined />}>
              {inWorkspace && (
                <Row gutter={12} style={{ marginBottom: 16 }}>
                  <Col span={12}>
                    <Form.Item
                      label="源对象"
                      name="source_object_type_id"
                      rules={[{ required: true, message: "请选择源对象" }]}
                    >
                      <Select options={sourceOptions} placeholder="关系的起点对象" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      label="目标对象"
                      name="target_object_type_id"
                      rules={[{ required: true, message: "请选择目标对象" }]}
                    >
                      <Select options={targetOptions} placeholder="关系的终点对象" />
                    </Form.Item>
                  </Col>
                </Row>
              )}
              <Row gutter={[12, 12]}>
                <Col span={12}>
                  <ObjectTableCard
                    title="源对象 / 表"
                    objectRef={rel.source_object}
                    datahubBase={datahubBase}
                    detailPath={objectDetailPath(rel.source_object_type_id)}
                  />
                </Col>
                <Col span={12}>
                  <ObjectTableCard
                    title="目标对象 / 表"
                    objectRef={rel.target_object}
                    datahubBase={datahubBase}
                    detailPath={objectDetailPath(rel.target_object_type_id)}
                  />
                </Col>
                {mappingObjectRef ? (
                  <Col span={24}>
                    <ObjectTableCard
                      title="承载表 / 映射对象"
                      objectRef={mappingObjectRef}
                      datahubBase={datahubBase}
                      detailPath={objectDetailPath(mappingObjectRef.id)}
                    />
                  </Col>
                ) : null}
              </Row>
            </SectionCard>
          </Col>
        </Row>
      </Form>

      {graph && (
        <SectionCard
          title="关系方向预览"
          icon={<ArrowRightOutlined />}
          bodyFlush
        >
          <OntologyGraphView
            graph={graph}
            height={340}
            objectDetailPath={objectDetailPath}
            defaultLayout="dagre"
            hint="箭头方向表示关系语义：源对象 → 目标对象"
          />
        </SectionCard>
      )}
    </PageContainer>
  );
}
