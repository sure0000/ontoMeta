import { ApiOutlined, CopyOutlined, EyeOutlined, KeyOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { SectionCard } from "./SectionCard";
import type { McpAuditEntry, Principal, PrincipalCreated, PrincipalMcpAccess, PrincipalRole } from "../types";

const { Paragraph, Text } = Typography;

const ROLE_OPTIONS: { value: PrincipalRole; label: string; hint: string }[] = [
  { value: "reader", label: "读者 reader", hint: "只读" },
  { value: "editor", label: "编辑 editor", hint: "改本体、跑草稿生成" },
  { value: "reviewer", label: "复核 reviewer", hint: "二次确认、冲突裁决" },
  { value: "publisher", label: "发布 publisher", hint: "发布、删除、执行 SQL、改设置" },
];

const ROLE_COLOR: Record<PrincipalRole, string> = {
  reader: "default",
  editor: "blue",
  reviewer: "gold",
  publisher: "red",
};

/** 角色与令牌管理。Token 明文仅在创建/轮换时返回一次。 */
export function PrincipalsPanel() {
  const [rows, setRows] = useState<Principal[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [issued, setIssued] = useState<PrincipalCreated | null>(null);
  const [accessById, setAccessById] = useState<Record<string, PrincipalMcpAccess>>({});
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const [form] = Form.useForm<{ name: string; role: PrincipalRole }>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const principals = await api.listPrincipals();
      setRows(principals);
      const access = await Promise.allSettled(
        principals.map((principal) => api.getPrincipalMcpAccess(principal.id)),
      );
      setAccessById(
        Object.fromEntries(
          access
            .filter((item): item is PromiseFulfilledResult<PrincipalMcpAccess> => item.status === "fulfilled")
            .map((item) => [item.value.principal_id, item.value]),
        ),
      );
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    const values = await form.validateFields();
    try {
      setIssued(await api.createPrincipal(values));
      setCreateOpen(false);
      form.resetFields();
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    }
  };

  const patch = async (id: string, body: Parameters<typeof api.updatePrincipal>[1]) => {
    try {
      await api.updatePrincipal(id, body);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新失败");
    }
  };

  const rotate = async (id: string) => {
    try {
      setIssued(await api.rotatePrincipalToken(id));
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "轮换失败");
    }
  };

  const remove = async (id: string) => {
    try {
      await api.deletePrincipal(id);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const openDetails = async (id: string) => {
    if (!accessById[id]) {
      try {
        const access = await api.getPrincipalMcpAccess(id);
        setAccessById((current) => ({ ...current, [id]: access }));
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载详情失败");
        return;
      }
    }
    setDetailsId(id);
  };

  const columns: ColumnsType<Principal> = [
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "MCP 工具",
      key: "mcp_tools",
      width: 120,
      render: (_, row) => {
        const access = accessById[row.id];
        return access ? <Tag color="blue">可调 {access.allowed_count} / {access.tool_count}</Tag> : "—";
      },
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      render: (role: PrincipalRole, row) => (
        <Select
          size="small"
          style={{ width: 150 }}
          value={role}
          options={ROLE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          onChange={(v) => patch(row.id, { role: v })}
        />
      ),
    },
    {
      title: "Token 前缀",
      dataIndex: "token_prefix",
      key: "token_prefix",
      render: (v: string) => <Text code>{v}…</Text>,
    },
    {
      title: "启用",
      dataIndex: "active",
      key: "active",
      render: (active: boolean, row) => (
        <Switch size="small" checked={active} onChange={(v) => patch(row.id, { active: v })} />
      ),
    },
    {
      title: "最近使用",
      dataIndex: "last_used_at",
      key: "last_used_at",
      render: (v: string | null) =>
        v ? new Date(v).toLocaleString() : <span className="om-muted">未使用</span>,
    },
    {
      title: "操作",
      key: "actions",
      render: (_, row) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={() => void openDetails(row.id)}>
            详情
          </Button>
          <Popconfirm title="轮换后旧 Token 立即失效" onConfirm={() => rotate(row.id)}>
            <Button size="small" icon={<ReloadOutlined />}>
              轮换
            </Button>
          </Popconfirm>
          <Popconfirm title="确认删除该主体？" onConfirm={() => remove(row.id)}>
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const issuedAccess = issued ? accessById[issued.id] : undefined;
  const issuedHttpConfig = issuedAccess && issued
    ? JSON.stringify({
        ...issuedAccess.http_config,
        url: typeof window === "undefined" ? issuedAccess.http_config.url : `${window.location.origin}/mcp/`,
        headers: { Authorization: `Bearer ${issued.token}` },
      }, null, 2)
    : "";

  return (
    <SectionCard
      title="角色与令牌"
      icon={<KeyOutlined />}
      count={rows.length}
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建主体
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert
          type="warning"
          showIcon
          title="四层角色：reader < editor < reviewer < publisher"
          description="外部 Agent 默认使用 editor：可起草和校验，但不能确认或执行。确需自动执行时再授予 publisher；不要把 Admin Token 交给 Agent。"
        />
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 960 }}
        />
      </Space>

      <Drawer
        title={detailsId ? `主体详情：${rows.find((row) => row.id === detailsId)?.name ?? ""}` : "主体详情"}
        open={Boolean(detailsId)}
        onClose={() => setDetailsId(null)}
        width={760}
        destroyOnHidden
      >
        {(() => {
          const principal = rows.find((row) => row.id === detailsId);
          const access = detailsId ? accessById[detailsId] : undefined;
          if (!principal || !access) return <Text type="secondary">MCP 权限信息暂不可用</Text>;
          return (
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label="名称">{principal.name}</Descriptions.Item>
                <Descriptions.Item label="角色"><Tag color={ROLE_COLOR[principal.role]}>{principal.role}</Tag></Descriptions.Item>
                <Descriptions.Item label="Token 前缀"><Text code>{principal.token_prefix}…</Text></Descriptions.Item>
                <Descriptions.Item label="状态">{principal.active ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>}</Descriptions.Item>
                <Descriptions.Item label="最近使用" span={2}>{principal.last_used_at ? new Date(principal.last_used_at).toLocaleString() : "未使用"}</Descriptions.Item>
              </Descriptions>
              <Tabs
                items={[
                  {
                    key: "tools",
                    label: `工具权限 ${access.allowed_count}/${access.tool_count}`,
                    children: (
                      <Table
                        rowKey="name"
                        size="small"
                        pagination={{ pageSize: 12, size: "small" }}
                        dataSource={access.tools}
                        scroll={{ x: 520 }}
                        columns={[
                          { title: "工具", dataIndex: "name", render: (value: string) => <Text code>{value}</Text> },
                          { title: "最低角色", dataIndex: "required_role", width: 110 },
                          { title: "状态", dataIndex: "allowed", width: 90, render: (value: boolean) => value ? <Tag color="green">可调用</Tag> : <Tag>不可调用</Tag> },
                        ]}
                      />
                    ),
                  },
                  {
                    key: "calls",
                    label: `最近调用 ${access.total_calls}`,
                    children: (
                      <Table<McpAuditEntry>
                        rowKey="id"
                        size="small"
                        pagination={false}
                        dataSource={access.recent_calls}
                        scroll={{ x: 540 }}
                        columns={[
                          { title: "时间", dataIndex: "created_at", width: 180, render: (value: string | null) => value ? new Date(value).toLocaleString() : "—" },
                          { title: "工具", dataIndex: "tool_name", render: (value: string) => <Text code>{value}</Text> },
                          { title: "结果", width: 90, render: (_value: unknown, item: McpAuditEntry) => item.rate_limited ? <Tag color="orange">限流</Tag> : item.denied ? <Tag color="red">被拒</Tag> : item.success ? <Tag color="green">成功</Tag> : <Tag color="volcano">失败</Tag> },
                        ]}
                      />
                    ),
                  },
                  {
                    key: "config",
                    label: "接入配置",
                    children: (
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Text strong>远程 HTTP</Text>
                        <Paragraph copyable={{ text: JSON.stringify(access.http_config, null, 2) }}>
                          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(access.http_config, null, 2)}</pre>
                        </Paragraph>
                        <Text type="secondary">详情中只显示 Token 前缀占位符；完整令牌仅在创建或轮换后显示一次。</Text>
                      </Space>
                    ),
                  },
                ]}
              />
            </Space>
          );
        })()}
      </Drawer>

      <Modal
        open={createOpen}
        title="新建主体"
        onOk={create}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
      >
        <Form form={form} layout="vertical" initialValues={{ role: "editor" }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="如：数据组-张三 / CI 流水线" />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              options={ROLE_OPTIONS.map((o) => ({
                value: o.value,
                label: `${o.label} — ${o.hint}`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={Boolean(issued)}
        title="Token 已生成"
        onOk={() => setIssued(null)}
        onCancel={() => setIssued(null)}
        okText="我已保存"
        cancelButtonProps={{ style: { display: "none" } }}
      >
        <Alert
          type="warning"
          showIcon
          title="明文仅此一次显示，关闭后无法再次获取"
          style={{ marginBottom: 12 }}
        />
        <Space.Compact block>
          <Input.TextArea
            value={issued?.token}
            readOnly
            autoSize={{ minRows: 2, maxRows: 4 }}
            style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", wordBreak: "break-all" }}
          />
          <Button
            icon={<CopyOutlined />}
            onClick={() => {
              if (issued?.token) void navigator.clipboard?.writeText(issued.token);
              message.success("Token 已复制");
            }}
          >
            复制
          </Button>
        </Space.Compact>
        {issued && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Text type="secondary">
              主体「{issued.name}」· <Tag color={ROLE_COLOR[issued.role]}>{issued.role}</Tag>
            </Text>
            {issuedHttpConfig && <>
              <Text strong><ApiOutlined /> HTTP 客户端配置</Text>
              <Paragraph copyable={{ text: issuedHttpConfig }}>
                <pre style={{ whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto", fontSize: 12 }}>{issuedHttpConfig}</pre>
              </Paragraph>
            </>}
          </Space>
        )}
      </Modal>
    </SectionCard>
  );
}
