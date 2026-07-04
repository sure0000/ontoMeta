import { FolderOpenOutlined } from "@ant-design/icons";
import { Alert, Col, Row } from "antd";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import { useApi } from "../hooks/useApi";
import { formatDateTime } from "../utils/format";
import type { DomainContext } from "../types";

export function WorkspacePage() {
  const { data: domains, loading, error } = useApi<DomainContext[]>(
    () => api.listDomains(),
    [],
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
      ) : !domains || domains.length === 0 ? (
        <EmptyState
          title="暂无数据域"
          description="尚未从 DataHub 同步任何数据域，请联系管理员配置数据域后开始建模。"
        />
      ) : (
        <Row gutter={[16, 16]}>
          {domains.map((domain) => (
            <Col key={domain.id} xs={24} sm={12} lg={8} xl={6}>
              <Link to={`/workspace/${domain.id}`} className="om-card-link">
                <div className="entity-card">
                  <div className="entity-card-head">
                    <div style={{ minWidth: 0 }}>
                      <div className="entity-card-title">{domain.name}</div>
                      <div className="entity-card-subtitle">
                        {domain.owner || "未指定负责人"}
                        </div>
                      </div>
                      <StatusBadge status={domain.status} />
                    </div>
                    <div className="entity-card-desc">
                      {domain.description || "暂无描述"}
                    </div>
                    <div className="entity-card-foot">
                      {domain.latest_published_at ? (
                        <span className="entity-card-foot-item">
                          已发布 <span className="entity-card-foot-time">{formatDateTime(domain.latest_published_at)}</span>
                        </span>
                      ) : domain.latest_draft_at ? (
                        <span className="entity-card-foot-item">
                          草稿生成 <span className="entity-card-foot-time">{formatDateTime(domain.latest_draft_at)}</span>
                        </span>
                      ) : (
                        <span className="entity-card-foot-item">
                          <span className="entity-card-foot-time">未开始</span>
                        </span>
                      )}
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
      )}
    </PageContainer>
  );
}
