import { AppstoreOutlined, SearchOutlined } from "@ant-design/icons";
import { Alert, Input, Pagination, Spin } from "antd";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import type { SegmentSummary, DomainContextDetail } from "../types";

const PAGE_SIZE = 20;

export function SegmentsPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const [searchQuery, setSearchQuery] = useState("");
  const [currentPage, setCurrentPage] = useState(1);

  // 先获取 domain 以得到 ontologyId
  const { data: domain } = useApi<DomainContextDetail>(
    () => (domainId ? api.getDomain(domainId) : Promise.reject(new Error("缺少 domainId"))),
    [domainId],
  );

  const ontologyId = domain?.working_ontology_id;

  const {
    data: result,
    loading,
    error,
  } = useApi(
    () =>
      ontologyId
        ? api.listSegments({
            ontologyId,
            publishedOnly: false,
            q: searchQuery || undefined,
            limit: PAGE_SIZE,
            offset: (currentPage - 1) * PAGE_SIZE,
          })
        : Promise.resolve({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 }),
    [ontologyId, searchQuery, currentPage],
  );

  const segments = result?.items || [];
  const total = result?.total || 0;

  if (loading && !result) return <PageSkeleton type="cards" />;

  return (
    <PageContainer>
      <PageHeader
        icon={<AppstoreOutlined />}
        title="业务板块"
        description="按关系紧密度自动聚类的业务子域，提供业务地图视图的骨架。"
      />

      <div style={{ marginBottom: 24 }}>
        <Input
          placeholder="搜索板块名称或描述..."
          prefix={<SearchOutlined />}
          value={searchQuery}
          onChange={(e) => {
            setSearchQuery(e.target.value);
            setCurrentPage(1);
          }}
          style={{ maxWidth: 400 }}
          allowClear
        />
      </div>

      {error ? (
        <Alert type="error" message="加载失败" description={error} showIcon />
      ) : segments.length === 0 ? (
        <EmptyState
          title={searchQuery ? "未找到匹配的板块" : "暂无板块"}
          description={
            searchQuery
              ? "尝试使用不同的关键词搜索"
              : "尚未生成业务板块，请先运行草稿生成。"
          }
        />
      ) : (
        <Spin spinning={loading}>
          <div className="workspace-domain-grid">
            {segments.map((segment: SegmentSummary) => (
              <Link
                key={segment.id}
                to={`/segments/${segment.id}`}
                className="om-card-link"
              >
                <div className="entity-card">
                  <div className="entity-card-head">
                    <div className="entity-card-head-main">
                      <div className="entity-card-icon entity-card-icon--primary">
                        <AppstoreOutlined />
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div className="entity-card-title">{segment.display_name}</div>
                        <div className="entity-card-subtitle">{segment.name}</div>
                      </div>
                    </div>
                    {segment.needs_review && (
                      <span
                        style={{
                          padding: "2px 8px",
                          borderRadius: 4,
                          background: "var(--om-warning-bg)",
                          color: "var(--om-warning)",
                          fontSize: 12,
                        }}
                      >
                        待复核
                      </span>
                    )}
                  </div>
                  <div className="entity-card-desc">
                    {segment.description || "暂无描述"}
                  </div>
                  <div className="entity-card-foot">
                    <div className="entity-card-foot-stats">
                      <span className="entity-card-foot-item">
                        <strong>{segment.member_count}</strong> 成员对象
                      </span>
                    </div>
                    <div className="entity-card-foot-meta">
                      更新于{" "}
                      <span className="entity-card-foot-time">
                        {formatDateTime(segment.updated_at)}
                      </span>
                    </div>
                  </div>
                </div>
              </Link>
            ))}
          </div>

          {total > PAGE_SIZE && (
            <div style={{ marginTop: 24, textAlign: "center" }}>
              <Pagination
                current={currentPage}
                pageSize={PAGE_SIZE}
                total={total}
                onChange={setCurrentPage}
                showSizeChanger={false}
                showTotal={(t) => `共 ${t} 个板块`}
              />
            </div>
          )}
        </Spin>
      )}
    </PageContainer>
  );
}
