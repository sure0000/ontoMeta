import {
  ApiOutlined,
  ArrowLeftOutlined,
  BookOutlined,
  CopyOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import type {
  ExternalApiFieldDoc,
  McpToolCallResult,
} from "../types";

const { Text } = Typography;

type SchemaParam = {
  name: string;
  type: string;
  required: boolean;
  description: string;
  default?: string;
};

function schemaParams(schema: Record<string, unknown> | undefined): SchemaParam[] {
  if (!schema) return [];
  const properties =
    (schema.properties as Record<
      string,
      { type?: string; description?: string; default?: unknown }
    >) || {};
  const required = new Set((schema.required as string[]) || []);
  return Object.entries(properties).map(([name, def]) => ({
    name,
    type: def.type || "string",
    required: required.has(name),
    description: def.description || "",
    default: def.default != null ? String(def.default) : undefined,
  }));
}

function copyText(text: string) {
  navigator.clipboard.writeText(text).then(
    () => message.success("已复制"),
    () => message.error("复制失败"),
  );
}

function CodeBlock({
  children,
  onCopy,
}: {
  children: string;
  onCopy?: () => void;
}) {
  return (
    <div className="api-code-block">
      {onCopy && (
        <Button
          type="text"
          size="small"
          className="api-code-block-copy"
          icon={<CopyOutlined />}
          onClick={onCopy}
        >
          复制
        </Button>
      )}
      <pre>{children}</pre>
    </div>
  );
}

export function ExternalApiDetailPage() {
  const { apiId } = useParams<{ apiId: string }>();
  const navigate = useNavigate();

  const {
    data: detailBundle,
    loading,
    error,
  } = useApi(
    async () => {
      if (!apiId) throw new Error("缺少 API ID");
      const [catalogItem, appList] = await Promise.all([
        api.getExternalApiCatalogItem(apiId),
        api.listExternalApps(),
      ]);
      return {
        item: catalogItem,
        apps: appList.filter((a) => a.status === "active"),
      };
    },
    [apiId],
  );

  const item = detailBundle?.item ?? null;
  const apps = detailBundle?.apps ?? [];

  const [selectedAppId, setSelectedAppId] = useState<string | undefined>();
  const [apiKey, setApiKey] = useState("");
  const [argValues, setArgValues] = useState<Record<string, string>>({});
  const [trying, setTrying] = useState(false);
  const [tryResult, setTryResult] = useState<{
    status: number;
    data: unknown;
    elapsedMs: number;
  } | null>(null);
  const [tryError, setTryError] = useState<string | null>(null);

  // 目录项变化时重置试用参数默认值
  useEffect(() => {
    if (!item) return;
    const defaults: Record<string, string> = {};
    for (const p of schemaParams(item.input_schema)) {
      defaults[p.name] = p.default ?? "";
    }
    setArgValues(defaults);
    setTryResult(null);
    setTryError(null);
  }, [item?.id]);

  const params = useMemo(
    () => schemaParams(item?.input_schema),
    [item],
  );

  const callArguments = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [key, value] of Object.entries(argValues)) {
      if (value.trim() !== "") out[key] = value.trim();
    }
    return out;
  }, [argValues]);

  const mcpCallExample = useMemo(() => {
    if (!item) return "";
    const body = {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: item.tool_name,
        arguments: callArguments,
      },
    };
    return `curl -X POST '${item.mcp_endpoint}' \\\n  -H 'X-API-Key: YOUR_API_KEY' \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(body)}'`;
  }, [item, callArguments]);

  const mcpListExample = useMemo(() => {
    if (!item) return "";
    const body = {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/list",
      params: {},
    };
    return `curl -X POST '${item.mcp_endpoint}' \\\n  -H 'X-API-Key: YOUR_API_KEY' \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(body)}'`;
  }, [item]);

  const handleSelectApp = (appId: string) => {
    setSelectedAppId(appId);
    setApiKey("");
    message.info("密钥已哈希存储，请粘贴创建/重置时保存的 API Key");
  };

  const handleTry = async () => {
    if (!item) return;
    if (!apiKey.trim()) {
      message.warning("请先选择应用或填写 API Key");
      return;
    }
    for (const p of params) {
      if (p.required && !argValues[p.name]?.trim()) {
        message.warning(`请填写必填参数：${p.name}`);
        return;
      }
    }

    setTrying(true);
    setTryError(null);
    setTryResult(null);
    const started = performance.now();
    try {
      const result = await api.callMcpTool(
        item.tool_name,
        callArguments,
        apiKey.trim(),
      );
      setTryResult({
        status: result.status,
        data: result.data,
        elapsedMs: Math.round(performance.now() - started),
      });
      const payload = result.data as McpToolCallResult | { detail?: string };
      if (result.status >= 400) {
        const detail =
          typeof payload === "object" &&
          payload &&
          "detail" in payload &&
          payload.detail
            ? String(payload.detail)
            : `HTTP ${result.status}`;
        setTryError(detail);
      } else if (
        typeof payload === "object" &&
        payload &&
        "isError" in payload &&
        payload.isError
      ) {
        const text =
          Array.isArray(payload.content) &&
          payload.content[0] &&
          typeof payload.content[0].text === "string"
            ? payload.content[0].text
            : "工具调用失败";
        setTryError(text);
      }
    } catch (err) {
      setTryError(err instanceof Error ? err.message : String(err));
    } finally {
      setTrying(false);
    }
  };

  const paramColumns: ColumnsType<SchemaParam> = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      width: 180,
      render: (name: string) => <code className="api-field-name">{name}</code>,
    },
    {
      title: "类型",
      dataIndex: "type",
      key: "type",
      width: 100,
    },
    {
      title: "必填",
      dataIndex: "required",
      key: "required",
      width: 80,
      render: (v: boolean) =>
        v ? <Tag color="red">是</Tag> : <Tag>否</Tag>,
    },
    {
      title: "说明",
      dataIndex: "description",
      key: "description",
      render: (desc: string) => <span className="api-table-desc">{desc}</span>,
    },
  ];

  const fieldColumns: ColumnsType<ExternalApiFieldDoc> = [
    {
      title: "字段",
      dataIndex: "name",
      key: "name",
      width: 220,
      render: (name: string) => <code className="api-field-name">{name}</code>,
    },
    {
      title: "类型",
      dataIndex: "type",
      key: "type",
      width: 110,
    },
    {
      title: "说明",
      dataIndex: "description",
      key: "description",
      render: (desc: string) => <span className="api-table-desc">{desc}</span>,
    },
  ];

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="MCP接口" icon={<BookOutlined />} />
        <PageSkeleton type="detail" />
      </PageContainer>
    );
  }

  if (error || !item) {
    return (
      <PageContainer>
        <PageHeader title="MCP接口" icon={<BookOutlined />} />
        <Alert
          type="error"
          showIcon
          message={error || "MCP 工具不存在"}
          action={
            <Button
              size="small"
              onClick={() => navigate("/external-api/endpoints")}
            >
              返回列表
            </Button>
          }
        />
      </PageContainer>
    );
  }

  const docsTab = (
    <div className="api-detail-stack">
      <SectionCard title="输入参数 (inputSchema)" count={params.length}>
        {params.length === 0 ? (
          <Text type="secondary">此工具无需输入参数</Text>
        ) : (
          <Table
            rowKey="name"
            columns={paramColumns}
            dataSource={params}
            pagination={false}
            size="middle"
          />
        )}
      </SectionCard>

      <SectionCard title="inputSchema JSON">
        <CodeBlock
          onCopy={() =>
            copyText(JSON.stringify(item.input_schema, null, 2))
          }
        >
          {JSON.stringify(item.input_schema, null, 2)}
        </CodeBlock>
      </SectionCard>

      <SectionCard title="输出字段" count={item.output_fields.length}>
        <Table
          rowKey="name"
          columns={fieldColumns}
          dataSource={item.output_fields}
          pagination={false}
          size="middle"
        />
      </SectionCard>

      <SectionCard title="返回示例">
        <CodeBlock
          onCopy={() =>
            copyText(JSON.stringify(item.example_result, null, 2))
          }
        >
          {JSON.stringify(item.example_result, null, 2)}
        </CodeBlock>
      </SectionCard>

      <SectionCard title="Agent 调用示例 (tools/call)">
        <CodeBlock onCopy={() => copyText(mcpCallExample)}>
          {mcpCallExample}
        </CodeBlock>
      </SectionCard>

      <SectionCard title="发现工具 (tools/list)">
        <CodeBlock onCopy={() => copyText(mcpListExample)}>
          {mcpListExample}
        </CodeBlock>
      </SectionCard>
    </div>
  );

  const tryTab = (
    <SectionCard title="在线试用" icon={<PlayCircleOutlined />}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 20 }}
        message={
          <span>
            将通过 MCP <code className="api-path-inline">tools/call</code> 调用{" "}
            <code className="api-path-inline">{item.tool_name}</code>
            ，请选择应用并粘贴创建/重置时保存的 API Key。
          </span>
        }
      />
      <Form layout="vertical" style={{ maxWidth: 760 }}>
        <Form.Item label="选择应用">
          <Select
            allowClear
            placeholder={
              apps.length === 0
                ? "暂无启用中的应用，请先创建"
                : "选择已创建的应用"
            }
            value={selectedAppId}
            onChange={(v) => {
              if (!v) {
                setSelectedAppId(undefined);
                return;
              }
              handleSelectApp(v);
            }}
            options={apps.map((a) => ({
              value: a.id,
              label: `${a.name} (${a.api_key_hint || a.app_key})`,
            }))}
            notFoundContent={<Link to="/external-api/apps">前往创建应用</Link>}
          />
        </Form.Item>
        <Form.Item
          label="API Key"
          required
          extra="密钥仅创建/重置时显示一次，请粘贴保存的明文；请求头为 X-API-Key"
        >
          <Input.Password
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="om_sk_..."
          />
        </Form.Item>

        {params.length > 0 && (
          <Form.Item label="工具参数 (arguments)">
            <Space direction="vertical" style={{ width: "100%" }} size={12}>
              {params.map((p) => (
                <Input
                  key={p.name}
                  addonBefore={
                    p.required ? (
                      <span>
                        {p.name}
                        <span style={{ color: "#ef4444", marginLeft: 2 }}>*</span>
                      </span>
                    ) : (
                      p.name
                    )
                  }
                  placeholder={p.description || p.name}
                  value={argValues[p.name] ?? ""}
                  onChange={(e) =>
                    setArgValues((prev) => ({
                      ...prev,
                      [p.name]: e.target.value,
                    }))
                  }
                />
              ))}
            </Space>
          </Form.Item>
        )}

        <Button
          type="primary"
          size="large"
          icon={<PlayCircleOutlined />}
          loading={trying}
          onClick={() => void handleTry()}
        >
          调用 Tool
        </Button>
      </Form>

      {(tryResult || tryError) && (
        <div className="api-try-result">
          {tryResult && (
            <Space style={{ marginBottom: 12 }}>
              <Tag color={tryResult.status < 400 && !tryError ? "success" : "error"}>
                HTTP {tryResult.status}
              </Tag>
              <Text type="secondary">{tryResult.elapsedMs} ms</Text>
            </Space>
          )}
          {tryError && (
            <Alert
              type="error"
              showIcon
              message={tryError}
              style={{ marginBottom: 12 }}
            />
          )}
          {tryResult && (
            <CodeBlock
              onCopy={() =>
                copyText(
                  typeof tryResult.data === "string"
                    ? tryResult.data
                    : JSON.stringify(tryResult.data, null, 2),
                )
              }
            >
              {typeof tryResult.data === "string"
                ? tryResult.data
                : JSON.stringify(tryResult.data, null, 2)}
            </CodeBlock>
          )}
        </div>
      )}
    </SectionCard>
  );

  return (
    <PageContainer>
      <PageHeader
        title={item.name}
        description={item.description}
        icon={<BookOutlined />}
        extra={
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate("/external-api/endpoints")}
          >
            返回列表
          </Button>
        }
      />

      <div className="api-endpoint-bar">
        <Tag color="cyan" className="api-method-tag">
          tool
        </Tag>
        <code className="api-endpoint-path">{item.tool_name}</code>
        <Tag>{item.category}</Tag>
        <Tag color="blue">{item.required_scope}</Tag>
        {item.rest_path && (
          <code className="api-endpoint-path" style={{ flex: "0 1 auto", opacity: 0.75 }}>
            {item.rest_method} {item.rest_path}
          </code>
        )}
        <code className="api-endpoint-path" style={{ flex: "0 1 auto", opacity: 0.75 }}>
          {item.mcp_endpoint}
        </code>
        <span className="api-endpoint-auth">
          <ApiOutlined />{" "}
          {item.auth_required ? "需 X-API-Key" : "无需鉴权"}
        </span>
      </div>

      <Tabs
        className="api-detail-tabs"
        defaultActiveKey="docs"
        items={[
          { key: "docs", label: "Tool 文档", children: docsTab },
          { key: "try", label: "在线试用", children: tryTab },
        ]}
      />
    </PageContainer>
  );
}
