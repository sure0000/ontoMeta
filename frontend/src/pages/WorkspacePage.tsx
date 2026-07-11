import { FolderOpenOutlined } from "@ant-design/icons";
import { Alert } from "antd";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import { getOntologyDomainStatusVisual } from "../utils/statusVisual";
import type { DomainContext } from "../types";

const WORKSPACE_STATUS_ORDER: Record<string, number> = {
  draft: 0,
  in_review: 0,
  active: 1,
  published: 2,
  archived: 3,
};

function sortWorkspaceDomains(domains: DomainContext[]): DomainContext[] {
  return [...domains].sort((a, b) => {
    const orderA = WORKSPACE_STATUS_ORDER[a.status] ?? 1;
    const orderB = WORKSPACE_STATUS_ORDER[b.status] ?? 1;
    if (orderA !== orderB) return orderA - orderB;
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}

export function WorkspacePage() {
  const { data: domains, loading, error } = useApi<DomainContext[]>(
    () => api.listDomains(),
    [],
  );

  const sortedDomains = useMemo(
    () => (domains ? sortWorkspaceDomains(domains) : []),
    [domains],
  );

  if (loading) return <PageSkeleton type="cards" />;

  return (
    <PageContainer>
      <PageHeader
        icon={<FolderOpenOutlined />}
        title="工作区"
        description="按 DataHub 数据域组织本体建模任务，发起草稿生成、编辑与发布。"
      />

      {error ? (
        <Alert type="error" message="加载失败" description={error} showIcon />
      ) : sortedDomains.length === 0 ? (
        <EmptyState
          title="暂无数据域"
          description="尚未从 DataHub 同步任何数据域，请联系管理员配置数据域后开始建模。"
        />
      ) : (
        <div className="workspace-domain-grid">
          {sortedDomains.map((domain) => {
            const statusVisual = getOntologyDomainStatusVisual(domain.status);
            return (
              <Link key={domain.id} to={`/workspace/${domain.id}`} className="om-card-link">
                <div className="entity-card">
                  <div className="entity-card-head">
                    <div className="entity-card-head-main">
                      <div className={`entity-card-icon entity-card-icon--${statusVisual.tone}`}>
                        {statusVisual.icon}
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div className="entity-card-title">{domain.name}</div>
                        <div className="entity-card-subtitle">
                          {domain.owner || "未指定负责人"}
                        </div>
                      </div>
                    </div>
                    <StatusBadge status={domain.status} />
                  </div>
                  <div className="entity-card-desc">
                    {domain.description || "暂无描述"}
                  </div>
                  <div className="entity-card-foot">
                    <div className="entity-card-foot-stats">
                      <span className="entity-card-foot-item">
                        <strong>{domain.object_type_count}</strong> 业务对象
                      </span>
                      <span className="entity-card-foot-item">
                        <strong>{domain.relation_type_count}</strong> 关系
                      </span>
                    </div>
                    <div className="entity-card-foot-meta">
                      {domain.latest_published_at ? (
                        <>
                          已发布{" "}
                          <span className="entity-card-foot-time">
                            {formatDateTime(domain.latest_published_at)}
                          </span>
                        </>
                      ) : domain.latest_draft_at ? (
                        <>
                          草稿生成{" "}
                          <span className="entity-card-foot-time">
                            {formatDateTime(domain.latest_draft_at)}
                          </span>
                        </>
                      ) : (
                        <span className="entity-card-foot-time">未开始</span>
                      )}
                    </div>
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </PageContainer>
  );
}
