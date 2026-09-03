import {
  Alert,
  Button,
  Descriptions,
  Input,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  ApiOutlined,
  AuditOutlined,
  BarChartOutlined,
  ReloadOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { SectionCard } from "./SectionCard";
import type {
  McpAuditEntry,
  McpServiceInfo,
  McpStats,
  McpToolInfo,
} from "../types";

const { Text, Paragraph } = Typography;

const ROLE_COLOR: Record<string, string> = {
  reader: "default",
  editor: "blue",
  reviewer: "gold",
  publisher: "red",
};

function RoleTag({ role }: { role: string | null }) {
  if (!role) return <Tag>无身份</Tag>;
  return <Tag color={ROLE_COLOR[role] ?? "default"}>{role}</Tag>;
}

/** MCP 服务管理：功能清单、远程连接、服务状态、审计、统计。 */
export function McpPanel() {
  const [info, setInfo] = useState<McpServiceInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const loadInfo = useCallback(async () => {
    setLoading(true);
    try {
      setInfo(await api.getMcpInfo());
    } catch (e) {
      message.error(`加载 MCP 服务信息失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadInfo();
  }, [loadInfo]);

  const httpEnabled = info?.transports.http.enabled ?? false;
  // 连接地址按当前页面地址推测；前后端不同源（如开发时后端在 :8000）时用户可改。
  const [endpoint, setEndpoint] = useState("");
  useEffect(() => {
    if (typeof window !== "undefined") {
      setEndpoint(`${window.location.origin}/mcp/`);
    }
  }, []);

  return (
    <SectionCard
      title="MCP 服务"
      icon={<ApiOutlined />}
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => void loadInfo()}
          loading={loading}
        >
          刷新
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="MCP 把本项目的本体/查询/任务能力暴露给通用 agent（Claude Desktop、Cursor、Claude Code）"
        description="通用 agent 是 MCP 的『客户端』——配置在它们那边，不在本页。本页用于查看 MCP 提供哪些能力、如何远程连接、以及调用审计与统计。"
      />
      <Tabs
        items={[
          {
            key: "connect",
            label: (
              <span>
                <ApiOutlined /> 连接与状态
              </span>
            ),
            children: (
              <ConnectAndStatus
                info={info}
                endpoint={endpoint}
                setEndpoint={setEndpoint}
                httpEnabled={httpEnabled}
              />
            ),
          },
          {
            key: "tools",
            label: (
              <span>
                <ToolOutlined /> 功能清单
                {info ? `（${info.tool_count}）` : ""}
              </span>
            ),
            children: <ToolCatalog tools={info?.tools ?? []} loading={loading} />,
          },
          {
            key: "audit",
            label: (
              <span>
                <AuditOutlined /> 审计日志
              </span>
            ),
            children: <AuditTable />,
          },
          {
            key: "stats",
            label: (
              <span>
                <BarChartOutlined /> 使用统计
              </span>
            ),
            children: <StatsView />,
          },
        ]}
      />
    </SectionCard>
  );
}

function ConnectAndStatus({
  info,
  endpoint,
  setEndpoint,
  httpEnabled,
}: {
  info: McpServiceInfo | null;
  endpoint: string;
  setEndpoint: (v: string) => void;
  httpEnabled: boolean;
}) {
  const desktopConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            ontometa: {
              type: "http",
              url: endpoint,
              headers: { Authorization: "Bearer <你的令牌>" },
            },
          },
        },
        null,
        2,
      ),
    [endpoint],
  );

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Descriptions bordered size="small" column={2} title="服务状态">
        <Descriptions.Item label="本地 stdio">
          <Tag color="green">始终可用</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            客户端以子进程拉起（同机）
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="远程 HTTP">
          {httpEnabled ? (
            <Tag color="green">已启用</Tag>
          ) : (
            <Tag color="default">未启用</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="远程匿名访问">
          {info?.transports.http.allow_anonymous ? (
            <Tag color="orange">允许</Tag>
          ) : (
            <Tag color="green">需令牌</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="匿名默认角色">
          <RoleTag role={info?.default_role ?? null} />
        </Descriptions.Item>
        <Descriptions.Item label="限流（默认）">
          {info?.rate_limit.enabled
            ? `${info.rate_limit.default_per_minute} 次/分`
            : "关闭"}
        </Descriptions.Item>
        <Descriptions.Item label="限流（execute_sql）">
          {info?.rate_limit.enabled
            ? `${info.rate_limit.execute_sql_per_minute} 次/分`
            : "关闭"}
        </Descriptions.Item>
        <Descriptions.Item label="审计表">
          {info?.audit.reachable ? (
            <Tag color="green">可达</Tag>
          ) : (
            <Tooltip title={info?.audit.error ?? ""}>
              <Tag color="red">不可达</Tag>
            </Tooltip>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="工具数">{info?.tool_count ?? "—"}</Descriptions.Item>
      </Descriptions>

      {!httpEnabled && (
        <Alert
          type="warning"
          showIcon
          message="远程 HTTP 传输未启用，异地 agent 无法连接"
          description="agent 与本项目不在同一台机器时需要它：在 backend/.env 设 MCP_HTTP_ENABLED=true（可选 MCP_HTTP_ALLOW_ANONYMOUS，默认要令牌）后重启后端。未启用时仅支持本机 stdio。"
        />
      )}

      <div>
        <Text strong>远程连接地址</Text>
        <Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
          按当前页面地址推测。若前后端不同源（如开发时后端在 :8000，或经反向代理），请替换为后端实际地址。路径末尾的斜杠不要去掉。
        </Paragraph>
        <Input
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
          addonBefore="URL"
          style={{ maxWidth: 560 }}
        />
      </div>

      <Alert
        type="info"
        showIcon
        message="令牌 = 身份与权限"
        description={
          <span>
            远程连接用 <Text code>Authorization: Bearer &lt;令牌&gt;</Text> 认证。令牌到「安全与鉴权 → 角色与令牌」新建一个<strong>最小权限</strong>主体（建议 reader；要让 agent 出提案用 editor、代跑 SQL 用 publisher），别把 Admin Token 交给 agent。
          </span>
        }
      />

      <div>
        <Text strong>Claude Desktop / Cursor 配置示例</Text>
        <Paragraph
          copyable={{ text: desktopConfig }}
          style={{ marginTop: 8 }}
        >
          <pre
            style={{
              background: "var(--om-bg-soft)",
              border: "1px solid var(--om-border)",
              padding: 12,
              borderRadius: 6,
              fontSize: 12,
              overflowX: "auto",
            }}
          >
            {desktopConfig}
          </pre>
        </Paragraph>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Claude Code：<Text code>{`claude mcp add ontometa -t http ${endpoint} -H "Authorization: Bearer <你的令牌>"`}</Text>
        </Text>
      </div>
    </Space>
  );
}

function ToolCatalog({ tools, loading }: { tools: McpToolInfo[]; loading: boolean }) {
  return (
    <Table<McpToolInfo>
      rowKey="name"
      size="small"
      loading={loading}
      dataSource={tools}
      pagination={false}
      columns={[
        {
          title: "工具",
          dataIndex: "name",
          width: 180,
          render: (v: string) => <Text code>{v}</Text>,
        },
        {
          title: "最低角色",
          dataIndex: "required_role",
          width: 110,
          render: (v: string) => <RoleTag role={v} />,
          filters: [
            { text: "reader", value: "reader" },
            { text: "editor", value: "editor" },
            { text: "reviewer", value: "reviewer" },
            { text: "publisher", value: "publisher" },
          ],
          onFilter: (val, r) => r.required_role === val,
        },
        {
          title: "说明",
          dataIndex: "description",
          render: (v: string) => (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {v}
            </Text>
          ),
        },
      ]}
    />
  );
}

function resultTag(row: McpAuditEntry) {
  if (row.denied) return <Tag color="red">被拒</Tag>;
  if (row.rate_limited) return <Tag color="orange">限流</Tag>;
  if (row.success) return <Tag color="green">成功</Tag>;
  return <Tag color="volcano">失败</Tag>;
}

function AuditTable() {
  const [rows, setRows] = useState<McpAuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [deniedOnly, setDeniedOnly] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const page = await api.getMcpAudit({ deniedOnly, limit: 100 });
      setRows(page.logs);
      setTotal(page.total);
    } catch (e) {
      message.error(`加载审计失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [deniedOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="small">
      <Space>
        <Button
          size="small"
          type={deniedOnly ? "primary" : "default"}
          onClick={() => setDeniedOnly((v) => !v)}
        >
          {deniedOnly ? "只看被拒 ✓" : "只看被拒"}
        </Button>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {total} 条（显示最近 100）
        </Text>
      </Space>
      <Table<McpAuditEntry>
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 20, size: "small" }}
        scroll={{ x: 720 }}
        columns={[
          {
            title: "时间",
            dataIndex: "created_at",
            width: 170,
            render: (v: string | null) => (v ? new Date(v).toLocaleString() : "—"),
          },
          {
            title: "工具",
            dataIndex: "tool_name",
            width: 160,
            render: (v: string) => <Text code>{v}</Text>,
          },
          {
            title: "身份",
            dataIndex: "principal_role",
            width: 100,
            render: (v: string | null) => <RoleTag role={v} />,
          },
          { title: "结果", key: "result", width: 80, render: (_: unknown, r) => resultTag(r) },
          {
            title: "耗时",
            dataIndex: "duration_ms",
            width: 80,
            render: (v: number | null) => (v == null ? "—" : `${v}ms`),
          },
          {
            title: "错误",
            dataIndex: "error",
            render: (v: string | null) =>
              v ? (
                <Text type="danger" style={{ fontSize: 12 }}>
                  {v}
                </Text>
              ) : (
                "—"
              ),
          },
        ]}
      />
    </Space>
  );
}

function StatsView() {
  const [stats, setStats] = useState<McpStats | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStats(await api.getMcpStats());
    } catch (e) {
      message.error(`加载统计失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const t = stats?.totals;
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Space wrap size="large">
        <Statistic title="总调用" value={t?.calls ?? 0} loading={loading} />
        <Statistic title="成功" value={t?.succeeded ?? 0} loading={loading} />
        <Statistic
          title="业务失败"
          value={t?.business_failed ?? 0}
          loading={loading}
        />
        <Statistic
          title="被拒"
          value={t?.denied ?? 0}
          loading={loading}
          valueStyle={{ color: (t?.denied ?? 0) > 0 ? "#cf1322" : undefined }}
        />
        <Statistic
          title="被限流"
          value={t?.rate_limited ?? 0}
          loading={loading}
          valueStyle={{ color: (t?.rate_limited ?? 0) > 0 ? "#d46b08" : undefined }}
        />
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
      </Space>
      <Space align="start" wrap size="large" style={{ width: "100%" }}>
        <Table
          title={() => "按工具"}
          rowKey="tool_name"
          size="small"
          loading={loading}
          dataSource={stats?.by_tool ?? []}
          pagination={false}
          style={{ minWidth: 320 }}
          columns={[
            {
              title: "工具",
              dataIndex: "tool_name",
              render: (v: string) => <Text code>{v}</Text>,
            },
            { title: "调用", dataIndex: "calls", width: 80 },
            { title: "被拒", dataIndex: "denied", width: 80 },
          ]}
        />
        <Table
          title={() => "按角色"}
          rowKey="role"
          size="small"
          loading={loading}
          dataSource={stats?.by_role ?? []}
          pagination={false}
          style={{ minWidth: 220 }}
          columns={[
            {
              title: "角色",
              dataIndex: "role",
              render: (v: string) =>
                v === "(anonymous)" ? <Tag>匿名</Tag> : <RoleTag role={v} />,
            },
            { title: "调用", dataIndex: "calls", width: 80 },
          ]}
        />
      </Space>
    </Space>
  );
}
