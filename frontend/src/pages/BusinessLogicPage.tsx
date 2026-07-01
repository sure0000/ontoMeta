import { FunctionOutlined } from "@ant-design/icons";
import { Alert, Select, Space, Spin, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import type { BusinessLogic, DomainContext } from "../types";

const { Text } = Typography;

export function BusinessLogicPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [logics, setLogics] = useState<BusinessLogic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listDomains().then(setDomains).catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (domains.length === 0) {
      setLoading(false);
      return;
    }

    const targetDomainId = domainId ?? domains[0]?.id;
    if (!targetDomainId) {
      setLoading(false);
      return;
    }

    if (!domainId && targetDomainId) {
      setSearchParams({ domain: targetDomainId }, { replace: true });
      return;
    }

    setLoading(true);
    api
      .listBusinessLogics({ domainId: targetDomainId, publishedOnly: true })
      .then(setLogics)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId, domains, setSearchParams]);

  const columns: ColumnsType<BusinessLogic> = [
    {
      title: "逻辑名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <Link to={`/business-logic/${record.id}`} className="id-link">
          <span>{record.display_name}</span>
          <span className="id-link-sub">{record.name}</span>
        </Link>
      ),
    },
    {
      title: "类型",
      dataIndex: "logic_type",
      key: "logic_type",
      width: 130,
    },
    {
      title: "数据域",
      dataIndex: "domain_name",
      key: "domain_name",
      width: 160,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "来源",
      dataIndex: "source_type",
      key: "source_type",
      width: 130,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "绑定对象",
      dataIndex: "bound_object_count",
      key: "bound_object_count",
      width: 100,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
    },
    {
      title: "绑定字段",
      dataIndex: "bound_property_count",
      key: "bound_property_count",
      width: 100,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
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
  ];

  if (loading && domains.length === 0) return <PageSkeleton type="list" />;

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title="业务逻辑"
        description="已发布本体中的指标、标签与规则语义（只读），按数据域切换浏览。"
        extra={
          domains.length > 0 ? (
            <Space>
              <Text type="secondary" style={{ fontSize: 13 }}>
                数据域
              </Text>
              <Select
                style={{ minWidth: 220 }}
                value={domainId ?? domains[0]?.id}
                onChange={(value) => setSearchParams({ domain: value })}
                options={domains.map((d) => ({ label: d.name, value: d.id }))}
              />
            </Space>
          ) : undefined
        }
      />

      {error && (
        <Alert
          type="error"
          message="加载失败"
          description={error}
          showIcon
          closable
          onClose={() => setError(null)}
        />
      )}

      <Spin spinning={loading}>
        {logics.length === 0 ? (
          <EmptyState
            title="暂无已发布的业务逻辑"
            description="前往工作区完成本体建模与发布后，相关业务逻辑将在此展示。"
            action={
              <Link to="/workspace">
                <span className="om-link">前往工作区 →</span>
              </Link>
            }
          />
        ) : (
          <div className="section-card">
            <div className="section-card-head">
              <div className="section-card-head-title">
                <span>业务逻辑列表</span>
                <span className="section-card-count section-card-count--primary">
                  {logics.length}
                </span>
              </div>
            </div>
            <div className="section-card-body section-card-body--flush">
              <Table
                className="om-table"
                rowKey="id"
                size="middle"
                columns={columns}
                dataSource={logics}
                pagination={false}
              />
            </div>
          </div>
        )}
      </Spin>
    </PageContainer>
  );
}
