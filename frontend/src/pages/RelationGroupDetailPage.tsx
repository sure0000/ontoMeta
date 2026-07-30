import { BranchesOutlined } from "@ant-design/icons";
import { Alert, Button, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import type { RelationType } from "../types";
import { getRelationStructureLabel, inferRelationStructureType } from "../utils/relation";

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];

/**
 * 关系详情页：一个关系名（display_name）下的全部 (源对象 → 目标对象) 三元组。
 * 由关系去重列表点入；数据用 GET /api/relation-types?display_name=... 服务端分页拉取。
 * scope（ontology / 是否仅已发布）通过 query 参数 oid / pub 传入，避免再次派生本体。
 */
export function RelationGroupDetailPage() {
  const { displayName: rawName, domainId } = useParams<{
    displayName: string;
    domainId?: string;
  }>();
  const [searchParams] = useSearchParams();
  const displayName = rawName ? decodeURIComponent(rawName) : "";
  const ontologyId = searchParams.get("oid") || undefined;
  const publishedOnly = searchParams.get("pub") === "1";
  const inWorkspace = Boolean(domainId);

  const [rows, setRows] = useState<RelationType[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const objectDetailPath = useCallback(
    (objectId: string) =>
      inWorkspace ? `/workspace/${domainId}/objects/${objectId}` : `/ontology/${objectId}`,
    [inWorkspace, domainId],
  );
  const relationEditPath = useCallback(
    (relationId: string) =>
      inWorkspace
        ? `/workspace/${domainId}/relations/${relationId}`
        : `/ontology/relations/${relationId}`,
    [inWorkspace, domainId],
  );
  const backPath = inWorkspace ? `/workspace/${domainId}` : "/ontology";

  useEffect(() => {
    if (!ontologyId || !displayName) {
      setError("缺少本体或关系名参数");
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listRelationTypes({
        ontologyId,
        displayName,
        publishedOnly,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      })
      .then((res) => {
        if (cancelled) return;
        setRows(res.items);
        setTotal(res.total);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "加载关系三元组失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [ontologyId, displayName, publishedOnly, page, pageSize]);

  const columns: ColumnsType<RelationType> = useMemo(
    () => [
      {
        title: "源对象",
        key: "source",
        render: (_, r) =>
          r.source_object_name ? (
            <Link to={objectDetailPath(r.source_object_type_id)}>{r.source_object_name}</Link>
          ) : (
            <span className="om-muted">-</span>
          ),
      },
      {
        title: "关系",
        key: "relation",
        align: "center",
        width: 120,
        render: (_, r) => (
          <Space size={4}>
            <span className="om-muted">→</span>
            <Tag color="blue">{r.display_name}</Tag>
            <span className="om-muted">→</span>
          </Space>
        ),
      },
      {
        title: "目标对象",
        key: "target",
        render: (_, r) =>
          r.target_object_name ? (
            <Link to={objectDetailPath(r.target_object_type_id)}>{r.target_object_name}</Link>
          ) : (
            <span className="om-muted">-</span>
          ),
      },
      {
        title: "关系类型",
        key: "structure_type",
        width: 110,
        render: (_, r) =>
          getRelationStructureLabel(
            r.structure_type || inferRelationStructureType(r.description, r.source_evidence),
          ),
      },
      {
        title: "基数",
        dataIndex: "cardinality",
        key: "cardinality",
        width: 90,
        render: (v?: string) => v || <span className="om-muted">-</span>,
      },
      {
        title: "置信度",
        dataIndex: "source_confidence",
        key: "source_confidence",
        width: 90,
        align: "right",
        render: (v?: number) => v?.toFixed(2) ?? <span className="om-muted">-</span>,
      },
      {
        title: "复核状态",
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (status: string) => <StatusBadge status={status} />,
      },
      {
        title: "操作",
        key: "actions",
        width: 80,
        render: (_, r) => <Link to={relationEditPath(r.id)}>详情</Link>,
      },
    ],
    [objectDetailPath, relationEditPath],
  );

  if (loading && rows.length === 0) {
    return (
      <PageContainer>
        <PageSkeleton />
      </PageContainer>
    );
  }

  return (
    <PageContainer full>
      <PageHeader
        icon={<BranchesOutlined />}
        title={displayName || "关系详情"}
        description={`该关系名下共 ${total} 条 (源对象 → 目标对象) 三元组`}
        extra={
          <Link to={backPath}>
            <Button>返回</Button>
          </Link>
        }
      />

      {error && (
        <Alert type="error" message={error} showIcon closable onClose={() => setError(null)} />
      )}

      <SectionCard title="关系三元组" count={total} countPrimary icon={<BranchesOutlined />} bodyFlush>
        <Table<RelationType>
          className="om-table"
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: "max-content" }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
          }}
        />
      </SectionCard>
    </PageContainer>
  );
}
