import {
  ApiOutlined,
  BookOutlined,
  RightOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { Alert, Input, Tag } from "antd";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import type { ExternalApiCatalogItem } from "../types";

export function ExternalApiCatalogPage() {
  const navigate = useNavigate();
  const { data, loading, error, reload } = useApi(
    () => api.listExternalApiCatalog(),
    [],
  );
  const [keyword, setKeyword] = useState("");

  const filtered = useMemo(() => {
    const list = data ?? [];
    const q = keyword.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (item) =>
        item.name.toLowerCase().includes(q) ||
        item.tool_name.toLowerCase().includes(q) ||
        item.category.toLowerCase().includes(q) ||
        item.description.toLowerCase().includes(q),
    );
  }, [data, keyword]);

  if (loading && !data) {
    return (
      <PageContainer>
        <PageHeader
          title="MCP接口"
          description="面向 Agent 的 MCP Tool，可发现、可调用"
          icon={<BookOutlined />}
        />
        <PageSkeleton type="list" />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title="MCP接口"
        description="以 MCP Tool 形式开放已发布的业务对象、业务关系与业务逻辑，供 Agent 调用"
        icon={<BookOutlined />}
        extra={
          <Input
            allowClear
            placeholder="搜索工具名称 / tool name / 分类"
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            style={{ width: 300 }}
          />
        }
      />

      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 16 }}
          action={
            <a onClick={() => void reload()} style={{ marginLeft: 8 }}>
              重试
            </a>
          }
        />
      )}

      <SectionCard
        title="可用 MCP Tools"
        count={filtered.length}
        icon={<ApiOutlined />}
      >
        {filtered.length === 0 ? (
          <EmptyState
            title="未找到工具"
            description={keyword ? "尝试调整搜索关键词" : "暂无 MCP 工具"}
          />
        ) : (
          <div className="api-catalog-list">
            {filtered.map((row: ExternalApiCatalogItem) => (
              <button
                key={row.id}
                type="button"
                className="api-catalog-item"
                onClick={() => navigate(`/external-api/endpoints/${row.id}`)}
              >
                <div className="api-catalog-item-main">
                  <div className="api-catalog-item-title-row">
                    <Tag color="cyan" className="api-method-tag">
                      tool
                    </Tag>
                    <span className="api-catalog-item-name">{row.name}</span>
                    <Tag>{row.category}</Tag>
                  </div>
                  <code className="api-path-inline">{row.tool_name}</code>
                  <p className="api-catalog-item-desc">{row.description}</p>
                </div>
                <RightOutlined className="api-catalog-item-arrow" />
              </button>
            ))}
          </div>
        )}
      </SectionCard>
    </PageContainer>
  );
}
