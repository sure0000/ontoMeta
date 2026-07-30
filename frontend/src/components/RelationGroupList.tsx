import { Alert, Empty, Spin, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { RelationGroup } from "../types";
import { getRelationStructureLabel } from "../utils/relation";
import { StatusBadge } from "./StatusBadge";

export interface RelationScope {
  ontologyId?: string;
  domainId?: string;
  publishedOnly?: boolean;
}

interface Props {
  scope: RelationScope;
  /** 受控搜索词，透传给后端分组端点（按 name/display_name/description 过滤）。 */
  query?: string;
  /** 由父页面注入的详情路径构造器，已内置 scope（ontology/published）。 */
  detailPath: (displayName: string) => string;
}

const STATUS_LABEL: Record<string, string> = {
  suggested: "待复核",
  edited: "已编辑",
  published: "已发布",
  deprecated: "已弃用",
};

function renderConfidence(min?: number | null, max?: number | null) {
  if (min == null && max == null) return <span className="om-muted">-</span>;
  if (min != null && max != null && min !== max) {
    return `${min.toFixed(2)}–${max.toFixed(2)}`;
  }
  const v = (min ?? max) as number;
  return v.toFixed(2);
}

/**
 * 关系去重列表：按 display_name 折叠展示（一行代表一种关系语义），
 * 仅展示 名称/描述/类型/基数/置信度/复核状态六列；具体 (源→目标) 三元组
 * 在关系详情页展开。数据来自 GET /api/relation-groups。
 */
export function RelationGroupList({ scope, query, detailPath }: Props) {
  const [groups, setGroups] = useState<RelationGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { ontologyId, domainId, publishedOnly } = scope;

  useEffect(() => {
    if (!ontologyId && !domainId) {
      setGroups([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listRelationGroups({ ontologyId, domainId, publishedOnly, q: query || undefined })
      .then((data) => {
        if (!cancelled) setGroups(data);
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
  }, [ontologyId, domainId, publishedOnly, query]);

  const columns: ColumnsType<RelationGroup> = useMemo(
    () => [
      {
        title: "关系名称",
        dataIndex: "display_name",
        key: "display_name",
        render: (_, g) => (
          <Link to={detailPath(g.display_name)} className="id-link">
            <span>
              {g.display_name || <span className="om-muted">（未命名）</span>}
              <Tag style={{ marginInlineStart: 8 }}>×{g.count}</Tag>
            </span>
          </Link>
        ),
      },
      {
        title: "关系描述",
        dataIndex: "description",
        key: "description",
        ellipsis: true,
        render: (v?: string | null) => v || <span className="om-muted">-</span>,
      },
      {
        title: "关系类型",
        key: "structure_types",
        width: 130,
        render: (_, g) => {
          const labels = g.structure_types.map(getRelationStructureLabel);
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
        key: "cardinalities",
        width: 100,
        render: (_, g) => {
          if (g.cardinalities.length === 0) return <span className="om-muted">-</span>;
          if (g.cardinalities.length === 1) return g.cardinalities[0];
          return (
            <Tooltip title={g.cardinalities.join("、")}>
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
        render: (_, g) => renderConfidence(g.confidence_min, g.confidence_max),
      },
      {
        title: "复核状态",
        key: "statuses",
        width: 120,
        render: (_, g) => {
          if (g.statuses.length === 1) return <StatusBadge status={g.statuses[0]} />;
          if (g.statuses.length === 0) return <span className="om-muted">-</span>;
          return (
            <Tooltip title={g.statuses.map((s) => STATUS_LABEL[s] ?? s).join("、")}>
              <Tag>混合</Tag>
            </Tooltip>
          );
        },
      },
    ],
    [detailPath],
  );

  if (error) {
    return <Alert type="error" message={error} showIcon />;
  }

  return (
    <Spin spinning={loading}>
      <Table<RelationGroup>
        rowKey="display_name"
        columns={columns}
        dataSource={groups}
        size="middle"
        pagination={groups.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
        locale={{
          emptyText: <Empty description="暂无关系" image={Empty.PRESENTED_IMAGE_SIMPLE} />,
        }}
      />
    </Spin>
  );
}
