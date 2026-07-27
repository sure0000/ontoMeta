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
import { OntologyGraphView, type GraphMode } from "./graph";
import { SectionCard } from "./SectionCard";
import { StatusBadge } from "./StatusBadge";
import { EmptyState } from "./EmptyState";
import {
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
} from "../utils/relation";
import type { ObjectTypeSummary, OntologyGraph, OntologyGroupedGraph, RelationType } from "../types";

type EntityTab = "objects" | "relations";
type ObjectViewMode = "list" | "cards" | "graph";
type ObjectRoleFilter = "all" | "business" | "common" | "review";

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;
// 对象数达到该规模才提供"域概览"切换：小域没有必要聚类，避免徒增复杂度
const OVERVIEW_MIN_OBJECTS = 30;

export interface ServerPaging {
  total: number;
  page: number;
  pageSize: number;
  onChange: (page: number, pageSize: number) => void;
}

function normalizeQuery(input: string) {
  return input.trim().toLowerCase();
}

// 对象角色徐章：区分“业务对象 / 普通数据表 / 桥接表”（预生成时标注，待人工确认）。
const ROLE_META: Record<
  string,
  { label: string; color: string; short: string; cls: string }
> = {
  business_object: { label: "业务对象", color: "green", short: "业务", cls: "business" },
  data_table: { label: "数据表", color: "blue", short: "数据", cls: "data" },
  bridge: { label: "关系表", color: "purple", short: "关系", cls: "bridge" },
  technical: { label: "技术/系统表", color: "default", short: "技术", cls: "technical" },
};

function RoleBadge({
  role,
  reason,
  confidence,
}: {
  role?: string;
  reason?: string;
  confidence?: number;
}) {
  const meta = ROLE_META[role || "business_object"] ?? ROLE_META.business_object;
  const needsReview = (reason ?? "").includes("待复核");
  const tip = [
    reason,
    confidence != null ? `置信度 ${(confidence * 100).toFixed(0)}%` : null,
  ]
    .filter(Boolean)
    .join("｜");
  const content = (
    <span className="role-badge">
      <Tag color={meta.color}>{meta.label}</Tag>
      {needsReview && (
        <Tag color="gold" className="role-badge-review">
          待复核
        </Tag>
      )}
    </span>
  );
  return tip ? <Tooltip title={tip}>{content}</Tooltip> : content;
}

// 卡片角色斜角标：右上角彩色色带表示角色（悬停出全称+置信度）。
// 待复核不放角落，改到「标识名」下方以更清晰地显示（见卡片 flags 行）。
function CardCorner({
  role,
  confidence,
}: {
  role?: string;
  confidence?: number;
}) {
  const meta = ROLE_META[role || "business_object"] ?? ROLE_META.business_object;
  const tip = [
    meta.label,
    confidence != null ? `置信度 ${(confidence * 100).toFixed(0)}%` : null,
  ]
    .filter(Boolean)
    .join("｜");
  return (
    <Tooltip title={tip}>
      <span
        className={`entity-card-corner entity-card-corner--${meta.cls}`}
        aria-label={meta.label}
      >
        <span className="entity-card-corner-band">{meta.short}</span>
      </span>
    </Tooltip>
  );
}

function matchObject(obj: ObjectTypeSummary, q: string) {
  if (!q) return true;
  if (obj.name?.toLowerCase().includes(q)) return true;
  if (obj.display_name?.toLowerCase().includes(q)) return true;
  if (obj.description?.toLowerCase().includes(q)) return true;
  return false;
}

// 业务对象 = business_object；普通对象 = 非业务对象（数据表 / 关系表）；
// 待复核 = role_reason 带 [待复核] 标记，角色需人工确认。
function matchObjectRole(obj: ObjectTypeSummary, filter: ObjectRoleFilter) {
  if (filter === "all") return true;
  if (filter === "review") return (obj.role_reason ?? "").includes("待复核");
  const isBusiness = (obj.table_role || "business_object") === "business_object";
  return filter === "business" ? isBusiness : !isBusiness;
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
  /** 服务端分页：开启后 objects/relations 视为当前页数据 */
  objectPaging?: ServerPaging;
  relationPaging?: ServerPaging;
  /** 服务端搜索受控；未传则本地过滤 */
  searchQuery?: string;
  onSearchChange?: (q: string) => void;
  /** 业务对象/普通对象筛选受控；未传则本地过滤 */
  objectRoleFilter?: ObjectRoleFilter;
  onObjectRoleFilterChange?: (filter: ObjectRoleFilter) => void;
  /** 图谱邻域展开 */
  onExpandGraphNode?: (objectId: string) => void;
  graphExpanding?: boolean;
  /** 域层级概览图（按需懒加载）：未提供 onGraphModeChange 时不展示 详情/概览 切换 */
  groupedGraph?: OntologyGroupedGraph | null;
  groupedGraphLoading?: boolean;
  graphMode?: GraphMode;
  onGraphModeChange?: (mode: GraphMode) => void;
}

export const OntologyWorkspaceView = memo(function OntologyWorkspaceView({
  objects,
  relations,
  graph,
  objectDetailPath = (id) => `/ontology/${id}`,
  relationDetailPath,
  workspaceMode = false,
  objectPaging,
  relationPaging,
  searchQuery,
  onSearchChange,
  objectRoleFilter,
  onObjectRoleFilterChange,
  onExpandGraphNode,
  graphExpanding = false,
  groupedGraph,
  groupedGraphLoading = false,
  graphMode,
  onGraphModeChange,
}: Props) {
  const serverMode = Boolean(objectPaging || relationPaging);
  const [entityTab, setEntityTab] = useState<EntityTab>("objects");
  const [objectView, setObjectView] = useState<ObjectViewMode>("cards");
  const [localQuery, setLocalQuery] = useState("");
  const [localRoleFilter, setLocalRoleFilter] = useState<ObjectRoleFilter>("all");
  const [objectPage, setObjectPage] = useState(1);
  const [objectPageSize, setObjectPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [relationPage, setRelationPage] = useState(1);
  const [relationPageSize, setRelationPageSize] = useState(DEFAULT_PAGE_SIZE);

  const query = onSearchChange ? (searchQuery ?? "") : localQuery;
  const normalizedQuery = normalizeQuery(query);
  const roleFilter = onObjectRoleFilterChange ? (objectRoleFilter ?? "all") : localRoleFilter;

  const filteredObjects = useMemo(() => {
    // 服务端已按 role_filter 过滤，本地只处理未受控场景
    if (serverMode) return objects;
    return objects.filter(
      (o) => matchObject(o, normalizedQuery) && matchObjectRole(o, roleFilter),
    );
  }, [objects, normalizedQuery, roleFilter, serverMode]);

  const filteredRelations = useMemo(() => {
    if (serverMode) return relations;
    return relations.filter((r) => matchRelation(r, normalizedQuery));
  }, [relations, normalizedQuery, serverMode]);

  useEffect(() => {
    if (!serverMode) {
      setObjectPage(1);
      setRelationPage(1);
    }
  }, [entityTab, normalizedQuery, roleFilter, serverMode]);

  const effectiveObjectPage = objectPaging?.page ?? objectPage;
  const effectiveObjectPageSize = objectPaging?.pageSize ?? objectPageSize;
  const effectiveObjectTotal = objectPaging?.total ?? filteredObjects.length;
  const effectiveRelationPage = relationPaging?.page ?? relationPage;
  const effectiveRelationPageSize = relationPaging?.pageSize ?? relationPageSize;
  const effectiveRelationTotal = relationPaging?.total ?? filteredRelations.length;

  const pagedObjects = useMemo(() => {
    if (serverMode || objectView !== "cards") return filteredObjects;
    const start = (objectPage - 1) * objectPageSize;
    return filteredObjects.slice(start, start + objectPageSize);
  }, [filteredObjects, objectPage, objectPageSize, objectView, serverMode]);

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
        title: "类型",
        dataIndex: "table_role",
        key: "table_role",
        width: 110,
        render: (_, record) => (
          <RoleBadge
            role={record.table_role}
            reason={record.role_reason}
            confidence={record.role_confidence}
          />
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
        title: "逻辑",
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
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const next = e.target.value;
      if (onSearchChange) onSearchChange(next);
      else setLocalQuery(next);
    },
    [onSearchChange],
  );

  const handleRoleFilterChange = useCallback(
    (value: string) => {
      const next = value as ObjectRoleFilter;
      if (onObjectRoleFilterChange) onObjectRoleFilterChange(next);
      else setLocalRoleFilter(next);
    },
    [onObjectRoleFilterChange],
  );

  const handleObjectPageChange = useCallback(
    (page: number, pageSize: number) => {
      if (objectPaging) objectPaging.onChange(page, pageSize);
      else {
        setObjectPage(page);
        setObjectPageSize(pageSize);
      }
    },
    [objectPaging],
  );

  const handleRelationPageChange = useCallback(
    (page: number, pageSize: number) => {
      if (relationPaging) relationPaging.onChange(page, pageSize);
      else {
        setRelationPage(page);
        setRelationPageSize(pageSize);
      }
    },
    [relationPaging],
  );

  if (objects.length === 0 && relations.length === 0 && effectiveObjectTotal === 0 && effectiveRelationTotal === 0) {
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
  const objectCountLabel = serverMode ? effectiveObjectTotal : objects.length;
  const relationCountLabel = serverMode ? effectiveRelationTotal : relations.length;
  const useVirtualTable = effectiveObjectPageSize >= 50 || effectiveRelationPageSize >= 50;

  const entitySwitcher = workspaceMode ? (
    <Segmented
      value={entityTab}
      onChange={handleEntityTab}
      options={[
        {
          label: (
            <span>
              <AppstoreOutlined style={{ marginRight: 6 }} />
              对象 {objectCountLabel}
            </span>
          ),
          value: "objects",
        },
        {
          label: (
            <span>
              <ApartmentOutlined style={{ marginRight: 6 }} />
              关系 {relationCountLabel}
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
      prefix={<SearchOutlined style={{ color: "var(--om-text-secondary)" }} />}
      placeholder={
        entityTab === "relations" ? "搜索关系名称、描述、对象" : "搜索对象名称、描述"
      }
      value={query}
      onChange={handleQueryChange}
      className="ontology-workspace-search"
    />
  ) : null;

  const roleFilterSwitcher =
    entityTab === "objects" && objectView !== "graph" ? (
      <Segmented
        value={roleFilter}
        onChange={handleRoleFilterChange}
        options={[
          { label: "全部", value: "all" },
          { label: "业务对象", value: "business" },
          { label: "普通对象", value: "common" },
          { label: "待复核", value: "review" },
        ]}
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

  const graphMeta =
    graph?.truncated || (graph?.total_object_count && graph.total_object_count > graph.nodes.length)
      ? `局部 ${graph.nodes.length}/${graph.total_object_count ?? "?"} 节点 · ${graph.edges.length} 关系`
      : graph
        ? `${graph.nodes.length} 节点 · ${graph.edges.length} 关系`
        : "图谱生成中";

  const totalObjectCount = graph?.total_object_count ?? objects.length;
  const overviewEligible = Boolean(onGraphModeChange) && totalObjectCount >= OVERVIEW_MIN_OBJECTS;

  return (
    <div className="om-stack">
      <div className="toolbar">
        <div className="toolbar-left">
          {entitySwitcher}
          {searchInput}
          {roleFilterSwitcher}
        </div>
        <div className="toolbar-right">
          {objectViewSwitcher}
          {entityTab === "objects" && objectView === "graph" && (
            <span className="toolbar-text">{graphMeta}</span>
          )}
        </div>
      </div>

      {entityTab === "relations" ? (
        effectiveRelationTotal === 0 && filteredRelations.length === 0 ? (
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
            count={effectiveRelationTotal}
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
              scroll={{ x: "max-content", y: useVirtualTable ? 560 : undefined }}
              virtual={useVirtualTable}
              pagination={{
                current: effectiveRelationPage,
                pageSize: effectiveRelationPageSize,
                total: effectiveRelationTotal,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (total) => `共 ${total} 条`,
                onChange: handleRelationPageChange,
              }}
            />
          </SectionCard>
        )
      ) : effectiveObjectTotal === 0 && objects.length === 0 ? (
        <SectionCard title="对象列表" icon={<AppstoreOutlined />} bodyFlush>
          <EmptyState title="暂无业务对象" />
        </SectionCard>
      ) : objectView === "graph" && graph ? (
        <SectionCard
          title="对象图谱"
          count={graph.total_object_count ?? objects.length}
          countPrimary
          icon={<NodeIndexOutlined />}
          bodyFlush
        >
          <OntologyGraphView
            graph={graph}
            height={720}
            objectDetailPath={objectDetailPath}
            relationDetailPath={relationDetailPath}
            onExpandNode={onExpandGraphNode}
            expanding={graphExpanding}
            centerNodeId={graph.center_id ?? undefined}
            hint={
              graph.truncated
                ? "双击展开邻域 · Shift+单击查看详情 · 拖拽重排"
                : undefined
            }
            embedded
            groupedGraph={groupedGraph}
            groupedGraphLoading={groupedGraphLoading}
            graphMode={graphMode}
            onGraphModeChange={overviewEligible ? onGraphModeChange : undefined}
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
            count={effectiveObjectTotal}
            countPrimary
            icon={<AppstoreOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={objectColumns}
              dataSource={serverMode ? objects : filteredObjects}
              scroll={{ x: "max-content", y: useVirtualTable ? 560 : undefined }}
              virtual={useVirtualTable}
              pagination={{
                current: effectiveObjectPage,
                pageSize: effectiveObjectPageSize,
                total: effectiveObjectTotal,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (total) => `共 ${total} 条`,
                onChange: handleObjectPageChange,
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
          <Row gutter={[12, 12]} align="stretch">
            {pagedObjects.map((obj) => (
              <Col key={obj.id} xs={24} sm={12} md={8} lg={6} xl={4} xxl={4}>
                <Link to={objectDetailPath(obj.id)} className="om-card-link">
                  <div className="entity-card">
                    <CardCorner
                      role={obj.table_role}
                      confidence={obj.role_confidence}
                    />
                    <div className="entity-card-head">
                      <div className="entity-card-title" title={obj.display_name}>
                        {obj.display_name}
                      </div>
                    </div>
                    <div className="entity-card-subtitle" title={obj.name}>
                      {obj.name}
                    </div>
                    <div className="entity-card-flags">
                      {(obj.role_reason ?? "").includes("待复核") && (
                        <Tooltip
                          title={(obj.role_reason ?? "").replace(/^\[待复核\]\s*/, "")}
                        >
                          <span className="entity-card-review">待复核</span>
                        </Tooltip>
                      )}
                    </div>
                    <div className="entity-card-foot">
                      <span className="entity-card-foot-item">
                        <strong>{obj.property_count}</strong> 属性
                      </span>
                      <span className="entity-card-foot-item">
                        <strong>{obj.relation_count}</strong> 关系
                      </span>
                      <span className="entity-card-foot-item">
                        <strong>{obj.bound_logic_count ?? 0}</strong> 逻辑
                      </span>
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
          <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end" }}>
            <Pagination
              current={effectiveObjectPage}
              pageSize={effectiveObjectPageSize}
              total={effectiveObjectTotal}
              showSizeChanger
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              showTotal={(total) => `共 ${total} 条`}
              onChange={handleObjectPageChange}
            />
          </div>
        </div>
      )}
    </div>
  );
});
