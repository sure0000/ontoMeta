import {
  AppstoreOutlined,
  ApartmentOutlined,
  DatabaseOutlined,
  SearchOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import {
  Button,
  Checkbox,
  Input,
  Pagination,
  Row,
  Col,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { DatasetCatalogPanel } from "./DatasetCatalog";
import { LandingBadge } from "./ObjectLanding";
import { SectionCard } from "./SectionCard";
import { StatusBadge } from "./StatusBadge";
import { EmptyState } from "./EmptyState";
import { RelationGroupList, type RelationScope } from "./RelationGroupList";
import {
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
} from "../utils/relation";
import type { ObjectTypeSummary, RelationType } from "../types";
import { getRoleMeta } from "../utils/role";

/** 视图 Tab：三类对象（业务对象/数据表/技术·系统表）+ 业务关系去重列表 + 数仓落点。
 *  关系表(bridge) 不作为对象展示。Tab 顺序：业务对象 → 业务关系 → 数据表 → 技术/系统表 → 数仓落点。
 *
 *  数仓落点排在最后且**是一个 Tab 而不是页面下方的一块**：它与对象列表争的是同一片
 *  垂直空间，摆在下面会把主内容（对象网格）挤成两行。 */
type ViewTab = "relations" | "business_object" | "data_table" | "technical" | "datasets";

/** 三个按角色分的对象 Tab。关系与落点各有自己的渲染路径，不走对象网格。 */
type ObjectTab = Exclude<ViewTab, "relations" | "datasets">;

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

export interface ServerPaging {
  total: number;
  page: number;
  pageSize: number;
  onChange: (page: number, pageSize: number) => void;
}

function normalizeQuery(input: string) {
  return input.trim().toLowerCase();
}

// 卡片角色斜角标：右上角彩色色带表示角色（悬停出全称+置信度）。
// 待复核不放角落，改到「标识名」下方以更清晰地显示（见卡片 flags 行）。
function CardCorner({ role, confidence }: { role?: string; confidence?: number }) {
  const meta = getRoleMeta(role);
  const tip = [meta.label, confidence != null ? `置信度 ${(confidence * 100).toFixed(0)}%` : null]
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

// 对象类型多选项（与 ROLE_META 对齐）。
const TYPE_FILTER_OPTIONS = [
  { label: "业务对象", value: "business_object" },
  { label: "数据表", value: "data_table" },
  { label: "关系表", value: "bridge" },
  { label: "技术/系统表", value: "technical" },
];

// 组合筛选（本地兜底，服务端已过滤时不走此路径）：
// 对象类型多选（table_role）AND 仅看待复核（role_reason 带 [待复核]）。
function matchObjectFilters(
  obj: ObjectTypeSummary,
  typeFilter: string[],
  needsReviewOnly: boolean,
) {
  if (typeFilter.length && !typeFilter.includes(obj.table_role || "business_object")) {
    return false;
  }
  if (needsReviewOnly && !(obj.role_reason ?? "").includes("待复核")) return false;
  return true;
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
  relations?: RelationType[];
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  /** 关系去重列表的数据范围（本体/域/是否仅已发布），供关系 Tab 自取分组。 */
  relationScope?: RelationScope;
  /** 去重关系行 → 关系详情页路径（已内置 scope）。传入即启用关系去重列表。 */
  relationGroupDetailPath?: (displayName: string) => string;
  workspaceMode?: boolean;
  /** 服务端分页：开启后 objects/relations 视为当前页数据 */
  objectPaging?: ServerPaging;
  relationPaging?: ServerPaging;
  /** 服务端搜索受控；未传则本地过滤 */
  searchQuery?: string;
  onSearchChange?: (q: string) => void;
  /** 对象类型多选筛选受控；未传则本地过滤 */
  objectTypeFilter?: string[];
  onObjectTypeFilterChange?: (roles: string[]) => void;
  /** 仅看待复核开关受控；未传则本地过滤 */
  needsReviewOnly?: boolean;
  onNeedsReviewOnlyChange?: (v: boolean) => void;
  /** 传入即多一个「数仓落点」Tab：该本体的对象/口径落到数仓的哪些物理表。 */
  datasetOntologyId?: string;
  /** 从落点派生出新对象后刷新对象列表（否则新对象要等下次进页面才看得见）。 */
  onDerivedObjectCreated?: () => void;
  /** 是否展示对象角色分类（类型列/斜角标/待复核/类型筛选）。浏览已发布本体时置 false。 */
  showRoleClassification?: boolean;
  /** 批量修改对象角色/复核状态。传入即开启对象卡片多选批量操作（仅工作区）。 */
  onBatchUpdateObjects?: (
    ids: string[],
    patch: { table_role?: string; needs_review?: boolean },
  ) => Promise<void>;
}

export const OntologyWorkspaceView = memo(function OntologyWorkspaceView({
  objects,
  relations = [],
  objectDetailPath = (id) => `/ontology/${id}`,
  relationDetailPath,
  relationScope,
  relationGroupDetailPath,
  datasetOntologyId,
  onDerivedObjectCreated,
  objectPaging,
  relationPaging,
  searchQuery,
  onSearchChange,
  objectTypeFilter,
  onObjectTypeFilterChange,
  needsReviewOnly,
  onNeedsReviewOnlyChange,
  showRoleClassification = true,
  onBatchUpdateObjects,
}: Props) {
  const serverMode = Boolean(objectPaging || relationPaging);
  // 关系去重列表：传入 scope + 详情路径即启用（否则回退旧的逐条关系表）。
  const useRelationGroups = Boolean(relationScope && relationGroupDetailPath);
  const [viewTab, setViewTab] = useState<ViewTab>("business_object");
  const [localQuery, setLocalQuery] = useState("");
  const [localTypeFilter, setLocalTypeFilter] = useState<string[]>([]);
  const [localNeedsReview, setLocalNeedsReview] = useState(false);
  const [objectPage, setObjectPage] = useState(1);
  const [objectPageSize, setObjectPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [relationPage, setRelationPage] = useState(1);
  const [relationPageSize, setRelationPageSize] = useState(DEFAULT_PAGE_SIZE);

  // 批量修改（仅对象 Tab、工作区角色分类可见时可用）。
  const batchEnabled = Boolean(onBatchUpdateObjects) && showRoleClassification;
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [batchRole, setBatchRole] = useState<string | undefined>(undefined);
  const [batchReview, setBatchReview] = useState<string | undefined>(undefined);
  const [applying, setApplying] = useState(false);

  const exitBatch = useCallback(() => {
    setBatchMode(false);
    setSelectedIds([]);
    setBatchRole(undefined);
    setBatchReview(undefined);
  }, []);

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }, []);

  const query = onSearchChange ? (searchQuery ?? "") : localQuery;
  const normalizedQuery = normalizeQuery(query);
  const typeFilter = useMemo(
    () => (onObjectTypeFilterChange ? (objectTypeFilter ?? []) : localTypeFilter),
    [onObjectTypeFilterChange, objectTypeFilter, localTypeFilter],
  );
  const needsReview = onNeedsReviewOnlyChange ? (needsReviewOnly ?? false) : localNeedsReview;
  const filterKey = `${typeFilter.join(",")}|${needsReview}`;

  const filteredObjects = useMemo(() => {
    // 卡片按显示名称字典序排列（缺显示名时退回标识名），稳定可预期。
    const byDisplayName = (a: ObjectTypeSummary, b: ObjectTypeSummary) =>
      (a.display_name || a.name || "").localeCompare(b.display_name || b.name || "", undefined, {
        numeric: true,
      });
    // 服务端已按 role_in/needs_review 过滤，本地只处理未受控场景。
    // 防御性角色过滤：对象 Tab 只显示与当前 Tab 角色一致的对象——即便服务端首帧
    // 竞态返回了其它角色(如切换 Tab 触发的 role_in 重查尚未回来)，也不会混入
    // 关系表/技术表，避免「刷新后才正常归属」。
    if (serverMode) {
      const sorted = [...objects].sort(byDisplayName);
      return viewTab !== "relations"
        ? sorted.filter((o) => (o.table_role || "business_object") === viewTab)
        : sorted;
    }
    return objects
      .filter(
        (o) => matchObject(o, normalizedQuery) && matchObjectFilters(o, typeFilter, needsReview),
      )
      .sort(byDisplayName);
  }, [objects, normalizedQuery, typeFilter, needsReview, serverMode, viewTab]);

  const filteredRelations = useMemo(() => {
    if (serverMode) return relations;
    return relations.filter((r) => matchRelation(r, normalizedQuery));
  }, [relations, normalizedQuery, serverMode]);

  // 数仓落点既不是对象也不是关系：搜索框、批量、角色筛选都不该跟着它走。
  const isDatasetTab = viewTab === "datasets";
  const isObjectTab = viewTab !== "relations" && !isDatasetTab;

  useEffect(() => {
    if (!serverMode) {
      setObjectPage(1);
      setRelationPage(1);
    }
  }, [viewTab, normalizedQuery, filterKey, serverMode]);

  // 激活某个对象 Tab 时，把该角色作为唯一的对象类型过滤条件同步给上层
  // （受控则驱动服务端 role_in 重查，否则走本地过滤）。关系表(bridge) 不设 Tab。
  useEffect(() => {
    if (!isObjectTab) return;
    const roles = [viewTab];
    if (onObjectTypeFilterChange) {
      if ((objectTypeFilter ?? []).join(",") !== roles.join(",")) {
        onObjectTypeFilterChange(roles);
      }
    } else {
      setLocalTypeFilter((prev) => (prev.join(",") === roles.join(",") ? prev : roles));
    }
  }, [viewTab, isObjectTab, onObjectTypeFilterChange, objectTypeFilter]);

  const effectiveObjectPage = objectPaging?.page ?? objectPage;
  const effectiveObjectPageSize = objectPaging?.pageSize ?? objectPageSize;
  const effectiveObjectTotal = objectPaging?.total ?? filteredObjects.length;
  const effectiveRelationPage = relationPaging?.page ?? relationPage;
  const effectiveRelationPageSize = relationPaging?.pageSize ?? relationPageSize;
  const effectiveRelationTotal = relationPaging?.total ?? filteredRelations.length;

  const pagedObjects = useMemo(() => {
    if (serverMode) return filteredObjects;
    const start = (objectPage - 1) * objectPageSize;
    return filteredObjects.slice(start, start + objectPageSize);
  }, [filteredObjects, objectPage, objectPageSize, serverMode]);

  // 批量选择只作用于当前页（服务端分页，跨页选择不保证一致）。
  const pageIds = useMemo(() => pagedObjects.map((o) => o.id), [pagedObjects]);
  const selectedOnPage = useMemo(
    () => pageIds.filter((id) => selectedIds.includes(id)),
    [pageIds, selectedIds],
  );
  const allPageSelected = pageIds.length > 0 && selectedOnPage.length === pageIds.length;

  const toggleSelectAllPage = useCallback(() => {
    setSelectedIds((prev) =>
      allPageSelected
        ? prev.filter((id) => !pageIds.includes(id))
        : Array.from(new Set([...prev, ...pageIds])),
    );
  }, [allPageSelected, pageIds]);

  const applyBatch = useCallback(async () => {
    if (!onBatchUpdateObjects || selectedIds.length === 0) return;
    const patch: { table_role?: string; needs_review?: boolean } = {};
    if (batchRole) patch.table_role = batchRole;
    if (batchReview) patch.needs_review = batchReview === "review";
    if (patch.table_role === undefined && patch.needs_review === undefined) return;
    setApplying(true);
    try {
      await onBatchUpdateObjects(selectedIds, patch);
      exitBatch();
    } finally {
      setApplying(false);
    }
  }, [onBatchUpdateObjects, selectedIds, batchRole, batchReview, exitBatch]);

  // 切到关系 Tab 或批量能力关闭时，退出批量态。
  useEffect(() => {
    if (!isObjectTab || !batchEnabled) exitBatch();
  }, [isObjectTab, batchEnabled, exitBatch]);

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
        render: (value?: number) => value?.toFixed(2) ?? <span className="om-muted">-</span>,
      },
    ],
    [objectDetailPath, relationDetailPath],
  );

  const handleViewTab = useCallback((value: string) => setViewTab(value as ViewTab), []);
  const handleQueryChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const next = e.target.value;
      if (onSearchChange) onSearchChange(next);
      else setLocalQuery(next);
    },
    [onSearchChange],
  );

  const handleNeedsReviewChange = useCallback(
    (checked: boolean) => {
      if (onNeedsReviewOnlyChange) onNeedsReviewOnlyChange(checked);
      else setLocalNeedsReview(checked);
    },
    [onNeedsReviewOnlyChange],
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

  // 「暂无本体草稿」是整页占位，会连同 Tab/搜索框一起替换掉——因此只在「确实没有草稿」
  // 时展示：一旦有搜索词或类型/复核过滤在生效，空结果应由各 Tab 内部的空态承载，
  // 保留 Tab 与搜索框，否则用户搜到空后连搜索框都消失、无法清空关键词。
  const hasActiveFilter = Boolean(normalizedQuery) || typeFilter.length > 0 || needsReview;
  if (
    !hasActiveFilter &&
    objects.length === 0 &&
    relations.length === 0 &&
    effectiveObjectTotal === 0 &&
    effectiveRelationTotal === 0
  ) {
    return (
      <SectionCard title="本体草稿" icon={<AppstoreOutlined />} bodyFlush>
        <EmptyState
          title="暂无本体草稿"
          description="尚未生成本体草稿，请在工作区发起生成后查看对象与关系。"
        />
      </SectionCard>
    );
  }

  const useVirtualTable = effectiveObjectPageSize >= 50 || effectiveRelationPageSize >= 50;

  const OBJECT_TAB_META: Record<ObjectTab, { label: string; icon: React.ReactNode }> = {
    business_object: { label: "业务对象", icon: <AppstoreOutlined /> },
    data_table: { label: "数据表", icon: <DatabaseOutlined /> },
    technical: { label: "技术/系统表", icon: <ToolOutlined /> },
  };

  const objectTabItem = (role: ObjectTab) => ({
    key: role,
    label: (
      <span>
        <span style={{ marginRight: 6 }}>{OBJECT_TAB_META[role].icon}</span>
        {OBJECT_TAB_META[role].label}
      </span>
    ),
  });
  const datasetTabItem = {
    key: "datasets",
    label: (
      <span>
        <DatabaseOutlined style={{ marginRight: 6 }} />
        数仓落点
      </span>
    ),
  };
  const relationTabItem = {
    key: "relations",
    label: (
      <span>
        <ApartmentOutlined style={{ marginRight: 6 }} />
        业务关系
      </span>
    ),
  };

  const tabSwitcher = (
    <Tabs
      className="om-tabs om-tabs--switcher"
      activeKey={viewTab}
      onChange={handleViewTab}
      // 顺序：业务对象 → 业务关系 → 数据表 → 技术/系统表
      items={[
        objectTabItem("business_object"),
        relationTabItem,
        objectTabItem("data_table"),
        objectTabItem("technical"),
        ...(datasetOntologyId ? [datasetTabItem] : []),
      ]}
    />
  );

  const searchInput = (
    <Input
      allowClear
      prefix={<SearchOutlined style={{ color: "var(--om-text-secondary)" }} />}
      placeholder={isObjectTab ? "搜索对象名称、描述" : "搜索关系名称、描述"}
      value={query}
      onChange={handleQueryChange}
      className="ontology-workspace-search"
    />
  );

  const needsReviewSwitcher =
    showRoleClassification && isObjectTab ? (
      <Checkbox checked={needsReview} onChange={(e) => handleNeedsReviewChange(e.target.checked)}>
        仅看待复核
      </Checkbox>
    ) : null;

  const batchToggle =
    batchEnabled && isObjectTab ? (
      <Button onClick={() => (batchMode ? exitBatch() : setBatchMode(true))}>
        {batchMode ? "退出批量" : "批量修改"}
      </Button>
    ) : null;

  const batchBar =
    batchMode && isObjectTab ? (
      <div className="toolbar">
        <div className="toolbar-left">
          <Space size={8} wrap>
            <Checkbox
              checked={allPageSelected}
              indeterminate={selectedOnPage.length > 0 && !allPageSelected}
              onChange={toggleSelectAllPage}
            >
              全选本页
            </Checkbox>
            <span className="om-muted">已选 {selectedIds.length}</span>
            <Select
              allowClear
              value={batchRole}
              onChange={setBatchRole}
              options={TYPE_FILTER_OPTIONS}
              placeholder="设为对象类型"
              style={{ minWidth: 160 }}
            />
            <Select
              allowClear
              value={batchReview}
              onChange={setBatchReview}
              options={[
                { label: "设为待复核", value: "review" },
                { label: "设为已确认", value: "confirmed" },
              ]}
              placeholder="复核状态"
              style={{ minWidth: 140 }}
            />
            <Button
              type="primary"
              loading={applying}
              disabled={selectedIds.length === 0 || (!batchRole && !batchReview)}
              onClick={applyBatch}
            >
              应用
            </Button>
            <Button onClick={exitBatch}>取消</Button>
          </Space>
        </div>
      </div>
    ) : null;

  return (
    <div className="om-stack">
      {tabSwitcher}
      {/* 落点面板自带搜索与分层筛选；再摆一个搜不到东西的框只会让人以为它坏了。 */}
      {!isDatasetTab && (
        <div className="toolbar">
          <div className="toolbar-left">
            {searchInput}
            {needsReviewSwitcher}
            {batchToggle}
          </div>
        </div>
      )}
      {batchBar}

      {isDatasetTab && datasetOntologyId ? (
        <DatasetCatalogPanel
          ontologyId={datasetOntologyId}
          objectDetailPath={objectDetailPath}
          onObjectCreated={onDerivedObjectCreated}
        />
      ) : viewTab === "relations" ? (
        useRelationGroups ? (
          <SectionCard title="关系列表（去重）" icon={<ApartmentOutlined />} bodyFlush>
            <RelationGroupList
              scope={relationScope!}
              query={query}
              detailPath={relationGroupDetailPath!}
              objectDetailPath={objectDetailPath}
            />
          </SectionCard>
        ) : effectiveRelationTotal === 0 && filteredRelations.length === 0 ? (
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
      ) : filteredObjects.length === 0 ? (
        <SectionCard title="对象列表" icon={<AppstoreOutlined />} bodyFlush>
          <EmptyState title="未匹配到对象" description="尝试调整搜索关键词。" />
        </SectionCard>
      ) : (
        <div>
          <Row gutter={[12, 12]} align="stretch">
            {pagedObjects.map((obj) => {
              const selected = selectedIds.includes(obj.id);
              const cardInner = (
                <div
                  className={`entity-card${batchMode ? " entity-card--batch" : ""}${
                    batchMode && selected ? " entity-card--selected" : ""
                  }`}
                >
                  {batchMode && (
                    <Checkbox
                      checked={selected}
                      onChange={() => toggleSelect(obj.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="entity-card-check"
                    />
                  )}
                  {showRoleClassification && (
                    <CardCorner role={obj.table_role} confidence={obj.role_confidence} />
                  )}
                  <div className="entity-card-head">
                    <div className="entity-card-title" title={obj.display_name}>
                      {obj.display_name}
                    </div>
                  </div>
                  <div className="entity-card-subtitle" title={obj.name}>
                    {obj.name}
                  </div>
                  <div className="entity-card-flags">
                    {showRoleClassification && (obj.role_reason ?? "").includes("待复核") && (
                      <Tooltip title={(obj.role_reason ?? "").replace(/^\[待复核\]\s*/, "")}>
                        <span className="entity-card-review">待复核</span>
                      </Tooltip>
                    )}
                    {/* 物理落点：任务把这个对象落成了哪张表。没有登记就不占位——
                        整域近千个对象里多数还没物化，逐个显示「未落地」只是噪声。 */}
                    <LandingBadge landing={obj.landing} />
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
              );
              return (
                <Col key={obj.id} xs={24} sm={12} md={8} lg={6} xl={4} xxl={4}>
                  {batchMode ? (
                    <div
                      className="om-card-link"
                      role="button"
                      tabIndex={0}
                      style={{ cursor: "pointer" }}
                      onClick={() => toggleSelect(obj.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggleSelect(obj.id);
                        }
                      }}
                    >
                      {cardInner}
                    </div>
                  ) : (
                    <Link to={objectDetailPath(obj.id)} className="om-card-link">
                      {cardInner}
                    </Link>
                  )}
                </Col>
              );
            })}
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
