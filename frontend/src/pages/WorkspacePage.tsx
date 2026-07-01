import {
  ApartmentOutlined,
  FolderOpenOutlined,
  CheckCircleOutlined,
  EditOutlined,
  AlertOutlined,
} from "@ant-design/icons";
import { Alert, Col, Row } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import type { DomainContext } from "../types";

export function WorkspacePage() {
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDomains()
      .then(setDomains)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <PageSkeleton type="cards" />;

  const totalDraft = domains.reduce((s, d) => s + (d.draft_count ?? 0), 0);
  const totalPublished = domains.reduce((s, d) => s + (d.published_count ?? 0), 0);
  const inReview = domains.filter((d) => d.status === "in_review").length;

  return (
    <PageContainer>
      <PageHeader
        icon={<FolderOpenOutlined />}
        title="工作区"
        description="按 DataHub 数据域组织本体建模任务，发起草稿生成、编辑与发布。"
      />

      {error ? (
        <Alert type="error" message="加载失败" description={error} showIcon />
      ) : domains.length === 0 ? (
        <EmptyState
          title="暂无数据域"
          description="尚未从 DataHub 同步任何数据域，请联系管理员配置数据域后开始建模。"
        />
      ) : (
        <>
          <div className="stat-row">
            <StatCard
              tone="primary"
              icon={<ApartmentOutlined />}
              label="数据域"
              value={domains.length}
              hint="已接入建模"
            />
            <StatCard
              tone="warning"
              icon={<EditOutlined />}
              label="草稿数量"
              value={totalDraft}
              hint="待完善与发布"
            />
            <StatCard
              tone="success"
              icon={<CheckCircleOutlined />}
              label="已发布本体"
              value={totalPublished}
              hint="对外可见"
            />
            <StatCard
              tone="neutral"
              icon={<AlertOutlined />}
              label="需要关注"
              value={inReview}
              hint="待审状态"
            />
          </div>

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
                      <span className="entity-card-foot-item">
                        草稿 <strong>{domain.draft_count}</strong>
                      </span>
                      <span className="entity-card-foot-item">
                        已发布 <strong>{domain.published_count}</strong>
                      </span>
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
        </>
      )}
    </PageContainer>
  );
}
