import { Alert, Empty, Spin, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { ObjectTypeSummary, RelationGroup } from "../types";
import { getRelationStructureLabel } from "../utils/relation";
import { StatusBadge } from "./StatusBadge";

export interface RelationScope {
  ontologyId?: string;
  domainId?: string;
  publishedOnly?: boolean;
}

interface Props {
  scope: RelationScope;
  /** 受控搜索词，透传给后端（按 name/display_name/description 过滤）。 */
  query?: string;
  /** 关系去重行 → 关系三元组详情页路径（已内置 scope）。 */
  detailPath: (displayName: string) => string;
  /** 关系表(bridge) 行 → 对象详情页路径。 */
  objectDetailPath?: (objectId: string) => string;
}

const STATUS_LABEL: Record<string, string> = {
  suggested: "待复核",
  edited: "已编辑",
  published: "已发布",
  deprecated: "已弃用",
};

/** 统一行：关系去重组（RelationType 边）或关系表(bridge ObjectType）。 */
type UnifiedRow =
  | { kind: "group"; key: string; group: RelationGroup }
  | { kind: "bridge"; key: string; obj: ObjectTypeSummary };

function renderConfidence(min?: number | null, max?: number | null) {
  if (min == null && max == null) return <span className="om-muted">-</span>;
  if (min != null && max != null && min !== max) {
    return `${min.toFixed(2)}–${max.toFixed(2)}`;
  }
  const v = (min ?? max) as number;
  return v.toFixed(2);
}

/**
 * 业务关系列表：一张表混排两类「关系」——
 * 1) RelationType 按 display_name 去重的关系组（点进看源→目标三元组）；
 * 2) 被判定为「关系表/业务事实」的 bridge 对象表（含待复核，点进对象详情）。
 * 数据来自 GET /api/relation-groups 与 GET /api/object-types?role_in=bridge。
 */
export function RelationGroupList({ scope, query, detailPath, objectDetailPath }: Props) {
  const [groups, setGroups] = useState<RelationGroup[]>([]);
  const [bridges, setBridges] = useState<ObjectTypeSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { ontologyId, domainId, publishedOnly } = scope;

  // 输入去抖：避免逐键请求，且慢/断网时不闪空。
  const [debouncedQuery, setDebouncedQuery] = useState(query ?? "");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query ?? ""), 300);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    if (!ontologyId && !domainId) {
      setGroups([]);
      setBridges([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const q = debouncedQuery || undefined;
    Promise.all([
      api.listRelationGroups({ ontologyId, domainId, publishedOnly, q }),
      api.listObjectTypes({
        ontologyId,
        domainId,
        publishedOnly,
        q,
        roleIn: ["bridge"],
        limit: 500,
      }),
    ])
      .then(([grp, br]) => {
        if (cancelled) return;
        setGroups(grp);
        setBridges(br.items);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "加载关系失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [ontologyId, domainId, publishedOnly, debouncedQuery]);

  // 去重组在前、关系表(bridge)在后，混排一张表。
  const rows = useMemo<UnifiedRow[]>(
    () => [
      ...groups.map((g) => ({ kind: "group" as const, key: `g:${g.display_name}`, group: g })),
      ...bridges.map((o) => ({ kind: "bridge" as const, key: `b:${o.id}`, obj: o })),
    ],
    [groups, bridges],
  );

  const columns: ColumnsType<UnifiedRow> = useMemo(
    () => [
      {
        title: "关系名称",
        key: "name",
        render: (_, row) => {
          if (row.kind === "group") {
            const g = row.group;
            return (
              <Link to={detailPath(g.display_name)} className="id-link">
                <span>
                  {g.display_name || <span className="om-muted">（未命名）</span>}
                  <Tag style={{ marginInlineStart: 8 }}>×{g.count}</Tag>
                  {g.needs_review_count > 0 && (
                    <Tag color="orange" style={{ marginInlineStart: 8 }}>
                      {g.needs_review_count} 待复核
                    </Tag>
                  )}
                  {(g.target_groups ?? []).length > 0 && (
                    <Tooltip
                      title={(g.target_groups ?? [])
                        .slice(0, 8)
                        .map((target) => `${target.display_name} ${target.count}`)
                        .join("、")}
                    >
                      <Tag color="blue" style={{ marginInlineStart: 8 }}>
                        {(g.target_groups ?? []).length} 类目标
                      </Tag>
                    </Tooltip>
                  )}
                </span>
              </Link>
            );
          }
          const o = row.obj;
          const inner = (
            <span>
              {o.display_name || o.name}
              <Tag color="purple" style={{ marginInlineStart: 8 }}>
                关系表
              </Tag>
            </span>
          );
          return objectDetailPath ? (
            <Link to={objectDetailPath(o.id)} className="id-link">
              {inner}
            </Link>
          ) : (
            <span className="id-link">{inner}</span>
          );
        },
      },
      {
        title: "关系描述",
        key: "description",
        ellipsis: true,
        render: (_, row) => {
          const desc = row.kind === "group" ? row.group.description : row.obj.description;
          return desc || <span className="om-muted">-</span>;
        },
      },
      {
        title: "关系类型",
        key: "type",
        width: 130,
        render: (_, row) => {
          if (row.kind === "bridge") return <Tag color="purple">关系表(桥表)</Tag>;
          const labels = row.group.structure_types.map(getRelationStructureLabel);
          if (labels.length === 0) return <span className="om-muted">-</span>;
          if (labels.length === 1) return labels[0];
          return (
            <Tooltip title={labels.join("、")}>
              <span>多种 ({labels.length})</span>
            </Tooltip>
          );
        },
      },
      {
        title: "基数",
        key: "cardinality",
        width: 100,
        render: (_, row) => {
          if (row.kind === "bridge") return <span className="om-muted">-</span>;
          const cs = row.group.cardinalities;
          if (cs.length === 0) return <span className="om-muted">-</span>;
          if (cs.length === 1) return cs[0];
          return (
            <Tooltip title={cs.join("、")}>
              <span>多种</span>
            </Tooltip>
          );
        },
      },
      {
        title: "置信度",
        key: "confidence",
        width: 110,
        align: "right",
        render: (_, row) =>
          row.kind === "group"
            ? renderConfidence(row.group.confidence_min, row.group.confidence_max)
            : (row.obj.role_confidence?.toFixed(2) ?? <span className="om-muted">-</span>),
      },
      {
        title: "复核状态",
        key: "review",
        width: 120,
        render: (_, row) => {
          if (row.kind === "bridge") {
            const pending = Boolean(row.obj.needs_review);
            return pending ? (
              <Tag color="orange">待复核</Tag>
            ) : (
              <StatusBadge status={row.obj.status} />
            );
          }
          const st = row.group.statuses;
          if (st.length === 1) return <StatusBadge status={st[0]} />;
          if (st.length === 0) return <span className="om-muted">-</span>;
          return (
            <Tooltip title={st.map((s) => STATUS_LABEL[s] ?? s).join("、")}>
              <Tag>混合</Tag>
            </Tooltip>
          );
        },
      },
    ],
    [detailPath, objectDetailPath],
  );

  if (error) {
    return <Alert type="error" message={error} showIcon />;
  }

  return (
    <Spin spinning={loading}>
      <Table<UnifiedRow>
        rowKey="key"
        columns={columns}
        dataSource={rows}
        size="middle"
        pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: true } : false}
        locale={{
          emptyText: <Empty description="暂无关系" image={Empty.PRESENTED_IMAGE_SIMPLE} />,
        }}
      />
    </Spin>
  );
}
