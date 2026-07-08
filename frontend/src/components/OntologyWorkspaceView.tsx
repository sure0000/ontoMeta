import {
  AppstoreOutlined,
  BarsOutlined,
  NodeIndexOutlined,
  ApartmentOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { Input, Pagination, Row, Col, Segmented, Space, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { OntologyGraphView } from "./graph";
import { SectionCard } from "./SectionCard";
import { StatusBadge } from "./StatusBadge";
import { EmptyState } from "./EmptyState";
import {
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
} from "../utils/relation";
import type { ObjectTypeSummary, OntologyGraph, RelationType } from "../types";

type EntityTab = "objects" | "relations";
type ObjectViewMode = "list" | "cards" | "graph";

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

function normalizeQuery(input: string) {
  return input.trim().toLowerCase();
}

function matchObject(obj: ObjectTypeSummary, q: string) {
  if (!q) return true;
  if (obj.name?.toLowerCase().includes(q)) return true;
  if (obj.display_name?.toLowerCase().includes(q)) return true;
  if (obj.description?.toLowerCase().includes(q)) return true;
  return false;
}

function matchRelation(rel: RelationType, q: string) {
  if (!q) return true;
  if (rel.name?.toLowerCase().includes(q)) return true;
  if (rel.display_name?.toLowerCase().includes(q)) return true;
  if (rel.description?.toLowerCase().includes(q)) return true;
  if (rel.source_object_name?.toLowerCase().includes(q)) return true;
  if (rel.target_object_name?.toLowerCase().includes(q)) return true;
  return false;
}

interface Props {
  objects: ObjectTypeSummary[];
  relations: RelationType[];
  graph: OntologyGraph | null;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  workspaceMode?: boolean;
}

export const OntologyWorkspaceView = memo(function OntologyWorkspaceView({
  objects,
  relations,
  graph,
  objectDetailPath = (id) => `/ontology/${id}`,
  relationDetailPath,
  workspaceMode = false,
}: Props) {
  const [entityTab, setEntityTab] = useState<EntityTab>("objects");
  const [objectView, setObjectView] = useState<ObjectViewMode>("cards");
  const [query, setQuery] = useState("");
  const [objectPage, setObjectPage] = useState(1);
  const [objectPageSize, setObjectPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [relationPage, setRelationPage] = useState(1);
  const [relationPageSize, setRelationPageSize] = useState(DEFAULT_PAGE_SIZE);

  const normalizedQuery = normalizeQuery(query);

  const filteredObjects = useMemo(
    () => objects.filter((o) => matchObject(o, normalizedQuery)),
    [objects, normalizedQuery],
  );

  const filteredRelations = useMemo(
    () => relations.filter((r) => matchRelation(r, normalizedQuery)),
    [relations, normalizedQuery],
  );

  // 切换 tab 或搜索时重置分页
  useEffect(() => {
    setObjectPage(1);
    setRelationPage(1);
  }, [entityTab, normalizedQuery]);

  const pagedObjects = useMemo(() => {
    if (objectView === "cards") {
      const start = (objectPage - 1) * objectPageSize;
      return filteredObjects.slice(start, start + objectPageSize);
    }
    return filteredObjects;
  }, [filteredObjects, objectPage, objectPageSize, objectView]);

  const objectColumns: ColumnsType<ObjectTypeSummary> = useMemo(
    () => [
      {
        title: "对象名称",
        dataIndex: "display_name",
        key: "display_name",
        render: (_, record) => (
          <Link to={objectDetailPath(record.id)} className="id-link">
            <span>{record.display_name}</span>
            <span className="id-link-sub">{record.name}</span>
          </Link>
        ),
      },
      {
        title: "属性",
        dataIndex: "property_count",
        key: "property_count",
        width: 80,
        align: "right",
      },
      {
        title: "关系",
        dataIndex: "relation_count",
        key: "relation_count",
        width: 80,
        align: "right",
      },
      {
        title: "绑定逻辑",
        dataIndex: "bound_logic_count",
        key: "bound_logic_count",
        width: 110,
        render: (v?: number) =>
          v ? <Tag color="blue">{v}</Tag> : <span className="om-muted">0</span>,
      },
      {
        title: "状态",
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (status) => <StatusBadge status={status} />,
      },
      {
        title: "置信度",
        dataIndex: "source_confidence",
        key: "source_confidence",
        width: 100,
        align: "right",
        render: (value?: number) =>
          value?.toFixed(2) ?? <span className="om-muted">-</span>,
      },
    ],
    [objectDetailPath],
  );

  const relationColumns: ColumnsType<RelationType> = useMemo(
    () => [
      {
        title: "关系语义",
        dataIndex: "display_name",
        key: "display_name",
        render: (_, record) =>
          relationDetailPath ? (
            <Link to={relationDetailPath(record.id)} className="id-link">
              <span>{record.display_name}</span>
              <span className="id-link-sub">{record.name}</span>
            </Link>
          ) : (
            <span className="id-link">
              <span>{record.display_name}</span>
              <span className="id-link-sub">{record.name}</span>
            </span>
          ),
      },
      {
        title: "源对象 → 目标对象",
        key: "objects",
        render: (_, record) => (
          <Space size={6} wrap>
            {record.source_object_name ? (
              <Link to={objectDetailPath(record.source_object_type_id)}>
                {record.source_object_name}
              </Link>
            ) : (
              <span className="om-muted">-</span>
            )}
            <span className="om-muted">→</span>
            {record.target_object_name ? (
              <Link to={objectDetailPath(record.target_object_type_id)}>
                {record.target_object_name}
              </Link>
            ) : (
              <span className="om-muted">-</span>
            )}
          </Space>
        ),
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
        title: "证据来源",
        key: "evidence",
        width: 120,
        render: (_, record) => (
          <Tag>{inferRelationEvidenceType(record.source_evidence || record.description)}</Tag>
        ),
      },
      {
        title: "状态",
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (status) => <StatusBadge status={status} />,
      },
      {
        title: "置信度",
        dataIndex: "source_confidence",
        key: "source_confidence",
        width: 90,
        align: "right",
        render: (value?: number) =>
          value?.toFixed(2) ?? <span className="om-muted">-</span>,
      },
    ],
    [objectDetailPath, relationDetailPath],
  );

  const handleEntityTab = useCallback((value: string) => setEntityTab(value as EntityTab), []);
  const handleObjectView = useCallback(
    (value: string) => setObjectView(value as ObjectViewMode),
    [],
  );
  const handleQueryChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value),
    [],
  );

  if (objects.length === 0 && relations.length === 0) {
    return (
      <SectionCard title="本体草稿" icon={<AppstoreOutlined />} bodyFlush>
        <EmptyState
          title="暂无本体草稿"
          description="尚未生成本体草稿，请在工作区发起生成后查看对象与关系。"
        />
      </SectionCard>
    );
  }

  const showSearch = entityTab !== "objects" || objectView !== "graph";

  const entitySwitcher = workspaceMode ? (
    <Segmented
      value={entityTab}
      onChange={handleEntityTab}
      options={[
        {
          label: (
            <span>
              <AppstoreOutlined style={{ marginRight: 6 }} />
              对象 {objects.length}
            </span>
          ),
          value: "objects",
        },
        {
          label: (
            <span>
              <ApartmentOutlined style={{ marginRight: 6 }} />
              关系 {relations.length}
            </span>
          ),
          value: "relations",
        },
      ]}
    />
  ) : null;

  const searchInput = showSearch ? (
    <Input
      allowClear
      prefix={<SearchOutlined style={{ color: "var(--om-text-secondary, #94a3b8)" }} />}
      placeholder={
        entityTab === "relations" ? "搜索关系名称、描述、对象" : "搜索对象名称、描述"
      }
      value={query}
      onChange={handleQueryChange}
      className="ontology-workspace-search"
    />
  ) : null;

  const objectViewSwitcher =
    entityTab === "objects" ? (
      <Segmented
        value={objectView}
        onChange={handleObjectView}
        options={[
          {
            label: (
              <Tooltip title="列表视图">
                <BarsOutlined />
              </Tooltip>
            ),
            value: "list",
          },
          {
            label: (
              <Tooltip title="卡片视图">
                <AppstoreOutlined />
              </Tooltip>
            ),
            value: "cards",
          },
          {
            label: (
              <Tooltip title="图谱视图">
                <NodeIndexOutlined />
              </Tooltip>
            ),
            value: "graph",
          },
        ]}
      />
    ) : null;

  return (
    <div className="om-stack">
      <div className="toolbar">
        <div className="toolbar-left">
          {entitySwitcher}
          {searchInput}
        </div>
        <div className="toolbar-right">
          {objectViewSwitcher}
          {entityTab === "objects" && objectView === "graph" && (
            <span className="toolbar-text">
              {graph ? `${graph.nodes.length} 节点 · ${graph.edges.length} 关系` : "图谱生成中"}
            </span>
          )}
        </div>
      </div>

      {entityTab === "relations" ? (
        relations.length === 0 ? (
          <SectionCard title="关系列表" icon={<ApartmentOutlined />} bodyFlush>
            <EmptyState
              title="暂无关系类型"
              description="生成草稿后将自动识别外键与血缘关系，也可在对象详情中手动补充。"
            />
          </SectionCard>
        ) : filteredRelations.length === 0 ? (
          <SectionCard title="关系列表" icon={<ApartmentOutlined />} bodyFlush>
            <EmptyState title="未匹配到关系" description="尝试调整搜索关键词。" />
          </SectionCard>
        ) : (
          <SectionCard
            title="关系列表"
            count={filteredRelations.length}
            countPrimary
            icon={<ApartmentOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={relationColumns}
              dataSource={filteredRelations}
              scroll={{ x: "max-content" }}
              pagination={{
                current: relationPage,
                pageSize: relationPageSize,
                total: filteredRelations.length,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (total) => `共 ${total} 条`,
                onChange: (page, pageSize) => {
                  setRelationPage(page);
                  setRelationPageSize(pageSize);
                },
              }}
            />
          </SectionCard>
        )
      ) : objects.length === 0 ? (
        <SectionCard title="对象列表" icon={<AppstoreOutlined />} bodyFlush>
          <EmptyState title="暂无业务对象" />
        </SectionCard>
      ) : objectView === "graph" && graph ? (
        <SectionCard
          title="对象图谱"
          count={objects.length}
          countPrimary
          icon={<NodeIndexOutlined />}
          bodyFlush
        >
          <OntologyGraphView
            graph={graph}
            objectDetailPath={objectDetailPath}
            relationDetailPath={relationDetailPath}
          />
        </SectionCard>
      ) : objectView === "list" ? (
        filteredObjects.length === 0 ? (
          <SectionCard title="对象列表" icon={<AppstoreOutlined />} bodyFlush>
            <EmptyState title="未匹配到对象" description="尝试调整搜索关键词。" />
          </SectionCard>
        ) : (
          <SectionCard
            title="对象列表"
            count={filteredObjects.length}
            countPrimary
            icon={<AppstoreOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={objectColumns}
              dataSource={filteredObjects}
              scroll={{ x: "max-content" }}
              pagination={{
                current: objectPage,
                pageSize: objectPageSize,
                total: filteredObjects.length,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (total) => `共 ${total} 条`,
                onChange: (page, pageSize) => {
                  setObjectPage(page);
                  setObjectPageSize(pageSize);
                },
              }}
            />
          </SectionCard>
        )
      ) : filteredObjects.length === 0 ? (
        <SectionCard title="对象列表" icon={<AppstoreOutlined />} bodyFlush>
          <EmptyState title="未匹配到对象" description="尝试调整搜索关键词。" />
        </SectionCard>
      ) : (
        <div>
          <Row gutter={[16, 16]}>
            {pagedObjects.map((obj) => (
              <Col key={obj.id} xs={24} sm={12} lg={8} xl={6}>
                <Link to={objectDetailPath(obj.id)} className="om-card-link">
                  <div className="entity-card">
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
                    <div className="entity-card-foot">
                      <span className="entity-card-foot-item">
                        <strong>{obj.property_count}</strong> 属性
                      </span>
                      <span className="entity-card-foot-item">
                        <strong>{obj.relation_count}</strong> 关系
                      </span>
                      <span className="entity-card-foot-item">
                        绑定 <strong>{obj.bound_logic_count ?? 0}</strong> 逻辑
                      </span>
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
          <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end" }}>
            <Pagination
              current={objectPage}
              pageSize={objectPageSize}
              total={filteredObjects.length}
              showSizeChanger
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              showTotal={(total) => `共 ${total} 条`}
              onChange={(page, pageSize) => {
                setObjectPage(page);
                setObjectPageSize(pageSize);
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
});
