import {
  Button,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Space,
  Statistic,
  Row,
  Select,
  Switch,
  Table,
  Tag,
  Tabs,
  Typography,
  message,
} from "antd";
import {
  ApiOutlined,
  AuditOutlined,
  BarChartOutlined,
  CopyOutlined,
  DownloadOutlined,
  ReloadOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { SectionCard } from "./SectionCard";
import { MarkdownLite } from "../pages/chat-bi/ChatBiReferences";
import type {
  McpAuditEntry,
  McpServiceInfo,
  McpStats,
  McpSettings,
  McpToolInfo,
  Principal,
} from "../types";

const { Text, Paragraph } = Typography;

function InstallExample({ content }: { content: string }) {
  return (
    <Paragraph copyable={{ text: content }} style={{ marginBottom: 0 }}>
      <pre
        style={{
          background: "var(--om-bg-soft)",
          border: "1px solid var(--om-border)",
          padding: 12,
          borderRadius: 6,
          fontSize: 12,
          overflowX: "auto",
          margin: 0,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {content}
      </pre>
    </Paragraph>
  );
}

type McpServiceSettings = Pick<
  McpSettings,
  | "mcp_rate_limit_per_minute"
  | "mcp_execute_sql_rate_limit_per_minute"
  | "mcp_require_execution_approval"
  | "mcp_allow_stdio_interactive_approval"
  | "mcp_console_base_url"
>;

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

/**
 * MCP 服务配置：连接与状态、运行期配置和远程安装示例。
 *
 * 工具目录和审计监控由同文件中的独立面板承载，避免服务页一次加载全部内容。
 */
export function McpServicePanel() {
  const [loading, setLoading] = useState(true);
  const [settingsForm] = Form.useForm<McpServiceSettings>();
  const [savingSettings, setSavingSettings] = useState(false);

  const loadInfo = useCallback(async () => {
    setLoading(true);
    try {
      const settings = await api.getMcpSettings();
      settingsForm.setFieldsValue(settings);
    } catch (e) {
      message.error(`加载 MCP 服务配置失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [settingsForm]);

  useEffect(() => {
    void loadInfo();
  }, [loadInfo]);

  const [downloading, setDownloading] = useState(false);
  const downloadSkills = async () => {
    setDownloading(true);
    try {
      await api.downloadMcpSkills();
    } catch (e) {
      message.error(`下载 Skill 安装包失败：${(e as Error).message}`);
    } finally {
      setDownloading(false);
    }
  };

  const [endpoint, setEndpoint] = useState("");
  useEffect(() => {
    if (typeof window !== "undefined") {
      setEndpoint(`${window.location.origin}/mcp/`);
    }
  }, []);

  const installExamples = useMemo(() => {
    const auth = { Authorization: "Bearer <YOUR_PRINCIPAL_TOKEN>" };
    return {
      claudeDesktop: JSON.stringify(
        { mcpServers: { ontometa: { type: "http", url: endpoint, headers: auth } } },
        null,
        2,
      ),
      cursor: JSON.stringify(
        { mcpServers: { ontometa: { url: endpoint, headers: auth } } },
        null,
        2,
      ),
      claudeCode: `claude mcp add ontometa --transport http ${endpoint} --header "Authorization: Bearer <YOUR_PRINCIPAL_TOKEN>"`,
      windsurf: JSON.stringify(
        { mcpServers: { ontometa: { serverUrl: endpoint, headers: auth } } },
        null,
        2,
      ),
      cline: JSON.stringify(
        { mcpServers: { ontometa: { url: endpoint, headers: auth, disabled: false } } },
        null,
        2,
      ),
      vscode: JSON.stringify(
        { servers: { ontometa: { type: "http", url: endpoint, headers: auth } } },
        null,
        2,
      ),
      // dsh (DeepSeek Harness)：写在 ~/.dsh/profiles/<profile>/cordis.patch.yml。
      // 这个坑不写出来一定会踩：新增插件必须用 `- insert:` 包起来——裸 {id,name,config}
      // 会被当成"覆盖已有 id"，启动时报 entry not found。
      dsh: `# ~/.dsh/profiles/<profile>/cordis.patch.yml
# 新增插件必须用 "- insert:" 包裹；裸 {id,name,config} 会被当成覆盖已有条目而报 not found
- insert:
    - id: mcp-ontometa
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: ontometa
        transport: http
        url: "${endpoint}"
        headers:
          Authorization: "Bearer <YOUR_PRINCIPAL_TOKEN>"`,
    };
  }, [endpoint]);

  const saveServiceSettings = async (values: McpServiceSettings) => {
    setSavingSettings(true);
    try {
      const next = await api.updateMcpSettings(values);
      settingsForm.setFieldsValue(next);
      await loadInfo();
      message.success("配置已保存，立即生效");
    } catch (e) {
      message.error(`保存 MCP 配置失败：${(e as Error).message}`);
    } finally {
      setSavingSettings(false);
    }
  };

  return (
    <>
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
        <Descriptions
          bordered
          size="small"
          column={{ xs: 1, sm: 2 }}
          styles={{ label: { width: 120 } }}
        >
          <Descriptions.Item label="服务类型">
            <Tag color="green">HTTP</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="鉴权">
            <Tag color="green">必须携带令牌</Tag>
          </Descriptions.Item>
        </Descriptions>

        <div style={{ marginTop: 16 }}>
          <Text strong>运行期配置</Text>
          <Paragraph type="secondary" style={{ margin: "4px 0 12px", fontSize: 12 }}>
            修改后立即生效。
          </Paragraph>
          <Form
            form={settingsForm}
            layout="vertical"
            onFinish={(values) => void saveServiceSettings(values)}
          >
            <Space wrap align="end">
              <Form.Item
                name="mcp_rate_limit_per_minute"
                label="每工具限流/分"
                style={{ width: 140, marginBottom: 0 }}
              >
                <InputNumber min={0} max={100000} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item
                name="mcp_execute_sql_rate_limit_per_minute"
                label="execute_sql/分"
                style={{ width: 140, marginBottom: 0 }}
              >
                <InputNumber min={0} max={100000} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item
                name="mcp_require_execution_approval"
                label="代执行需逐条授权"
                valuePropName="checked"
                style={{ marginBottom: 0 }}
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="mcp_allow_stdio_interactive_approval"
                label="本机宿主交互确认"
                valuePropName="checked"
                style={{ marginBottom: 0 }}
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="mcp_console_base_url"
                label="控制台地址"
                style={{ width: 260, marginBottom: 0 }}
              >
                {/* Agent 发交互表单链接时拼在前面。后端听什么地址与用户从哪访问它是两回事，
                    所以只取这里配的值，不从请求里推导。 */}
                <Input placeholder="如 http://localhost:5180" allowClear />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={savingSettings}>
                保存
              </Button>
            </Space>
          </Form>
          <Paragraph type="secondary" style={{ margin: "10px 0 0", fontSize: 12 }}>
            开启时，Agent 通过 MCP 确认或执行任务，除角色外还需要有人在任务详情里逐条放行。
            角色是长期许可，「这一条现在可以自动跑」是另一个决定。关掉则退回「一个 publisher
            令牌即可推到远端真跑」。启用「本机宿主交互确认」后，只有本机 stdio 的真实
            Principal 可在宿主确认 UI 中批准，并以任务 digest 绑定本次确认；远程 HTTP 不适用。
            「控制台地址」用于 Agent 用 <Text code>open_task_form</Text>{" "}
            发网页表单链接时拼出可点的完整地址；留空只会给相对路径。
          </Paragraph>
        </div>

        <div style={{ marginTop: 16 }}>
          <Text strong>远程 MCP 地址</Text>
          {/* 这个地址是按**当前页面的来源**拼的，等于默认了前后端同源。
              前端单独部署（或经网关改写路径）时它指不到后端，而复制走的人不会知道。 */}
          <Paragraph type="secondary" style={{ margin: "4px 0 8px", fontSize: 12 }}>
            按当前页面地址推导（假定前端与后端同源）。前端单独部署或走网关时，
            请把下面的地址换成后端对外可达的地址再交给 Agent。
          </Paragraph>
          <Space.Compact block style={{ maxWidth: 560 }}>
            <Input value={endpoint} readOnly addonBefore="URL" />
            <Button
              icon={<CopyOutlined />}
              aria-label="复制 MCP 地址"
              onClick={() => {
                if (endpoint) void navigator.clipboard?.writeText(endpoint);
                message.success("MCP 地址已复制");
              }}
            >
              复制
            </Button>
          </Space.Compact>
        </div>

        <div style={{ marginTop: 16 }}>
          <Text strong>主流 Agent 安装示例</Text>
          <Tabs
            size="small"
            style={{ marginTop: 8 }}
            items={[
              {
                key: "claude-desktop",
                label: "Claude Desktop",
                children: <InstallExample content={installExamples.claudeDesktop} />,
              },
              {
                key: "claude-code",
                label: "Claude Code",
                children: <InstallExample content={installExamples.claudeCode} />,
              },
              {
                key: "cursor",
                label: "Cursor",
                children: <InstallExample content={installExamples.cursor} />,
              },
              {
                key: "windsurf",
                label: "Windsurf",
                children: <InstallExample content={installExamples.windsurf} />,
              },
              {
                key: "cline",
                label: "Cline",
                children: <InstallExample content={installExamples.cline} />,
              },
              {
                key: "vscode",
                label: "VS Code",
                children: <InstallExample content={installExamples.vscode} />,
              },
              {
                key: "dsh",
                label: "dsh",
                children: <InstallExample content={installExamples.dsh} />,
              },
            ]}
          />
        </div>

        <div style={{ marginTop: 16 }}>
          <Text strong>操作指引（Skill）</Text>
          <Paragraph type="secondary" style={{ margin: "4px 0 8px", fontSize: 12 }}>
            工具描述只说明单个工具做什么；跨工具的调用顺序、闸门和输出契约在指引里。
            <b>多数 MCP 客户端只桥接工具、不消费 prompts</b>，所以按上面接进来的 Agent
            默认拿不到指引——它可以随时调 <Text code>get_playbook</Text> 工具取回，
            也可以把指引装进客户端自己的 Skill 目录：
          </Paragraph>
          <Space wrap>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              onClick={() => void downloadSkills()}
              loading={downloading}
            >
              下载 Skill 安装包
            </Button>
            <Text type="secondary" style={{ fontSize: 12 }}>
              ZIP 内按 <Text code>&lt;skill-name&gt;/SKILL.md</Text> 组织，解压到客户端的 Skill
              目录即可（dsh 是 <Text code>skill-filesystem.customSkillDirs</Text>）。Agent 与
              ontoMeta 后端在同一台机器时，用「Agent 接入 → 技能 → 部署 Skill」填目录直接安装，
              免去下载解压。
            </Text>
          </Space>
        </div>
      </SectionCard>
    </>
  );
}

export function McpToolsPanel() {
  const [info, setInfo] = useState<McpServiceInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setInfo(await api.getMcpInfo());
    } catch (e) {
      message.error(`加载 MCP 工具目录失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <SectionCard
      // 首屏加载期写「（0）」会被读成"真的一个工具都没有"。还没拿到就不给数字。
      title={loading && !info ? "MCP 工具" : `MCP 工具（${info?.tool_count ?? 0}）`}
      icon={<ToolOutlined />}
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => void load()}
          loading={loading}
        >
          刷新
        </Button>
      }
    >
      <ToolCatalog tools={info?.tools ?? []} loading={loading} />
    </SectionCard>
  );
}

export function McpMonitoringPanel() {
  return (
    <>
      <SectionCard title="运行概览" icon={<BarChartOutlined />}>
        <StatsView />
      </SectionCard>
      <SectionCard title="调用明细" icon={<AuditOutlined />}>
        <AuditTable />
      </SectionCard>
    </>
  );
}

/** Backward-compatible composition for callers that still need all MCP panels. */
export function McpPanel() {
  return (
    <>
      <McpServicePanel />
      <McpToolsPanel />
      <McpMonitoringPanel />
    </>
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
      scroll={{ x: 640 }}
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
          render: (v: string) => <ToolDescription text={v} />,
        },
      ]}
    />
  );
}

/**
 * 工具说明是**写给模型看的 Markdown**：带 `**加粗**`、反引号和换行。
 * 这页原样把它当纯文本倒进单元格——星号裸奔、换行被压平，
 * get_ops_record 那条 850 字 13 个 bullet 挤成一坨灰字。而这页的用途恰恰是"看职责"。
 *
 * 所以：首行（到第一个换行为止）常驻，剩下的折叠；展开后用与对话区同一个
 * MarkdownLite 渲染，不再自己写第二套。
 */
function ToolDescription({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const content = (text || "").trim();
  const firstBreak = content.indexOf("\n");
  const summary = firstBreak === -1 ? content : content.slice(0, firstBreak);
  const hasMore = firstBreak !== -1;

  return (
    <div style={{ fontSize: 12 }}>
      <div className="chatbi-md-compact">
        <MarkdownLite content={open ? content : summary} />
      </div>
      {hasMore && (
        <Button
          type="link"
          size="small"
          style={{ padding: 0, height: "auto" }}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "收起" : "展开完整说明"}
        </Button>
      )}
    </div>
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
  const [tools, setTools] = useState<McpToolInfo[]>([]);
  const [principals, setPrincipals] = useState<Principal[]>([]);
  const [toolName, setToolName] = useState("");
  const [result, setResult] = useState<"success" | "failed" | "denied" | "rate_limited" | "">("");
  const [principalRole, setPrincipalRole] = useState("");
  const [principalId, setPrincipalId] = useState("");
  const [windowMinutes, setWindowMinutes] = useState<number | undefined>(1440);
  const [page, setPage] = useState(1);
  const pageSize = 25;

  useEffect(() => {
    void Promise.allSettled([api.getMcpInfo(), api.listPrincipals()]).then(
      ([info, principalList]) => {
        if (info.status === "fulfilled") setTools(info.value.tools);
        if (principalList.status === "fulfilled") setPrincipals(principalList.value);
      },
    );
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await api.getMcpAudit({
        toolName: toolName || undefined,
        result: result || undefined,
        principalRole: (principalRole || undefined) as
          "reader" | "editor" | "reviewer" | "publisher" | "anonymous" | undefined,
        principalId: principalId || undefined,
        windowMinutes,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setRows(response.logs);
      setTotal(response.total);
    } catch (e) {
      message.error(`加载审计失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, principalId, principalRole, result, toolName, windowMinutes]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="small">
      <Space wrap>
        <Select
          value={windowMinutes ?? "all"}
          onChange={(value) => {
            setWindowMinutes(value === "all" ? undefined : Number(value));
            setPage(1);
          }}
          options={[
            { value: 60, label: "最近 1 小时" },
            { value: 1440, label: "最近 24 小时" },
            { value: 10080, label: "最近 7 天" },
            { value: "all", label: "全部" },
          ]}
          style={{ width: 150 }}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="按工具筛选"
          value={toolName || undefined}
          onChange={(value) => {
            setToolName(value ?? "");
            setPage(1);
          }}
          options={tools.map((tool) => ({ value: tool.name, label: tool.name }))}
          style={{ width: 190 }}
        />
        <Select
          allowClear
          placeholder="按结果筛选"
          value={result || undefined}
          onChange={(value) => {
            setResult(value ?? "");
            setPage(1);
          }}
          options={[
            { value: "success", label: "成功" },
            { value: "failed", label: "业务失败" },
            { value: "denied", label: "被拒" },
            { value: "rate_limited", label: "被限流" },
          ]}
          style={{ width: 150 }}
        />
        <Select
          allowClear
          placeholder="按角色筛选"
          value={principalRole || undefined}
          onChange={(value) => {
            setPrincipalRole(value ?? "");
            setPage(1);
          }}
          options={[
            { value: "reader", label: "reader" },
            { value: "editor", label: "editor" },
            { value: "reviewer", label: "reviewer" },
            { value: "publisher", label: "publisher" },
            { value: "anonymous", label: "匿名" },
          ]}
          style={{ width: 150 }}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="按主体筛选"
          value={principalId || undefined}
          onChange={(value) => {
            setPrincipalId(value ?? "");
            setPage(1);
          }}
          options={principals.map((principal) => ({
            value: principal.id,
            label: `${principal.name} · ${principal.token_prefix}`,
          }))}
          style={{ width: 240 }}
        />
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {total} 条
        </Text>
      </Space>
      <Table<McpAuditEntry>
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{
          current: page,
          pageSize,
          total,
          size: "small",
          showSizeChanger: false,
          onChange: (nextPage) => setPage(nextPage),
        }}
        scroll={{ x: 980 }}
        expandable={{
          expandedRowRender: (row) => (
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="客户端">{row.client_type}</Descriptions.Item>
              <Descriptions.Item label="主体 ID">{row.principal_id ?? "匿名"}</Descriptions.Item>
              <Descriptions.Item label="参数（已脱敏）">
                <pre
                  style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 220, overflow: "auto" }}
                >
                  {JSON.stringify(row.arguments, null, 2)}
                </pre>
              </Descriptions.Item>
              {row.error && (
                <Descriptions.Item label="错误">
                  <Text type="danger">{row.error}</Text>
                </Descriptions.Item>
              )}
            </Descriptions>
          ),
        }}
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
          {
            title: "主体",
            dataIndex: "principal_id",
            width: 150,
            render: (v: string | null) =>
              v ? <Text code copyable={{ text: v }}>{`${v.slice(0, 12)}…`}</Text> : "匿名",
          },
          {
            title: "客户端",
            dataIndex: "client_type",
            width: 110,
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
  const [windowMinutes, setWindowMinutes] = useState<number | undefined>(1440);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStats(await api.getMcpStats(windowMinutes));
    } catch (e) {
      message.error(`加载统计失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [windowMinutes]);

  useEffect(() => {
    void load();
  }, [load]);

  const t = stats?.totals;
  const successRate = t && t.calls ? `${((t.succeeded / t.calls) * 100).toFixed(1)}%` : "—";
  const windowLabel =
    windowMinutes === undefined
      ? "全部"
      : windowMinutes === 15
        ? "最近 15 分钟"
        : windowMinutes === 60
          ? "最近 1 小时"
          : windowMinutes === 1440
            ? "最近 24 小时"
            : "最近 7 天";
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Space wrap>
        <Select
          value={windowMinutes ?? "all"}
          onChange={(value) => setWindowMinutes(value === "all" ? undefined : Number(value))}
          options={[
            { value: 15, label: "最近 15 分钟" },
            { value: 60, label: "最近 1 小时" },
            { value: 1440, label: "最近 24 小时" },
            { value: 10080, label: "最近 7 天" },
            { value: "all", label: "全部" },
          ]}
          style={{ width: 150 }}
        />
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary">
          当前窗口：{windowLabel}
          {stats?.last_call_at
            ? ` · 最近调用 ${new Date(stats.last_call_at).toLocaleString()}`
            : ""}
        </Text>
      </Space>
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={8} lg={4}>
          <Statistic title="总调用" value={t?.calls ?? 0} loading={loading} />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="成功率"
            value={successRate}
            loading={loading}
            valueStyle={{ color: "#389e0d" }}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="业务失败"
            value={t?.business_failed ?? 0}
            loading={loading}
            valueStyle={{ color: (t?.business_failed ?? 0) > 0 ? "#d46b08" : undefined }}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="业务错误率"
            value={t?.error_rate == null ? "—" : `${t.error_rate.toFixed(1)}%`}
            loading={loading}
            valueStyle={{ color: (t?.error_rate ?? 0) > 0 ? "#d46b08" : undefined }}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="被拒"
            value={t?.denied ?? 0}
            loading={loading}
            valueStyle={{ color: (t?.denied ?? 0) > 0 ? "#cf1322" : undefined }}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="被限流"
            value={t?.rate_limited ?? 0}
            loading={loading}
            valueStyle={{ color: (t?.rate_limited ?? 0) > 0 ? "#d46b08" : undefined }}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="P95 延迟"
            value={t?.p95_duration_ms == null ? "—" : `${t.p95_duration_ms}ms`}
            loading={loading}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic
            title="平均延迟"
            value={t?.average_duration_ms == null ? "—" : `${t.average_duration_ms}ms`}
            loading={loading}
          />
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Statistic title="活跃主体" value={stats?.unique_principals ?? 0} loading={loading} />
        </Col>
      </Row>
      <Row gutter={[16, 20]}>
        <Col xs={24} xl={15}>
          <Table
            title={() => "工具健康度"}
            rowKey="tool_name"
            size="small"
            loading={loading}
            dataSource={stats?.by_tool ?? []}
            pagination={false}
            scroll={{ x: 620 }}
            columns={[
              {
                title: "工具",
                dataIndex: "tool_name",
                render: (v: string) => <Text code>{v}</Text>,
              },
              { title: "调用", dataIndex: "calls", width: 70, sorter: (a, b) => a.calls - b.calls },
              {
                title: "成功率",
                width: 90,
                render: (_: unknown, row: McpStats["by_tool"][number]) =>
                  row.calls ? `${((row.succeeded / row.calls) * 100).toFixed(0)}%` : "—",
              },
              {
                title: "异常",
                width: 80,
                render: (_: unknown, row: McpStats["by_tool"][number]) =>
                  row.failed + row.denied + row.rate_limited,
              },
              {
                title: "平均延迟",
                width: 100,
                render: (_: unknown, row: McpStats["by_tool"][number]) =>
                  row.avg_duration_ms == null ? "—" : `${row.avg_duration_ms}ms`,
              },
            ]}
          />
        </Col>
        <Col xs={24} xl={9}>
          <Table
            title={() => "角色分布"}
            rowKey="role"
            size="small"
            loading={loading}
            dataSource={stats?.by_role ?? []}
            pagination={false}
            scroll={{ x: 340 }}
            columns={[
              {
                title: "角色",
                dataIndex: "role",
                render: (v: string) =>
                  v === "(anonymous)" ? <Tag>匿名</Tag> : <RoleTag role={v} />,
              },
              { title: "调用", dataIndex: "calls", width: 70 },
              { title: "成功", dataIndex: "succeeded", width: 70 },
              { title: "被拒", dataIndex: "denied", width: 70 },
            ]}
          />
        </Col>
        <Col xs={24} xl={12}>
          <Table
            title={() => "调用趋势"}
            rowKey="bucket"
            size="small"
            loading={loading}
            dataSource={(stats?.timeline ?? []).slice(-12)}
            pagination={false}
            scroll={{ x: 500 }}
            columns={[
              {
                title: "时间",
                dataIndex: "bucket",
                width: 180,
                render: (v: string) => new Date(v).toLocaleString(),
              },
              { title: "调用", dataIndex: "calls", width: 70 },
              { title: "成功", dataIndex: "succeeded", width: 70 },
              { title: "业务失败", dataIndex: "failed", width: 90 },
              {
                title: "拒绝/限流",
                render: (_: unknown, row: McpStats["timeline"][number]) =>
                  row.denied + row.rate_limited,
              },
            ]}
          />
        </Col>
        <Col xs={24} xl={12}>
          <Table
            title={() => "高频业务错误"}
            rowKey={(row) => `${row.tool_name}-${row.error}`}
            size="small"
            loading={loading}
            dataSource={stats?.error_groups ?? []}
            pagination={false}
            locale={{ emptyText: "当前窗口没有业务错误" }}
            scroll={{ x: 520 }}
            columns={[
              {
                title: "工具",
                dataIndex: "tool_name",
                width: 150,
                render: (v: string) => <Text code>{v}</Text>,
              },
              { title: "次数", dataIndex: "count", width: 70 },
              {
                title: "错误",
                dataIndex: "error",
                ellipsis: true,
                render: (v: string) => <Text type="danger">{v}</Text>,
              },
            ]}
          />
        </Col>
      </Row>
    </Space>
  );
}
