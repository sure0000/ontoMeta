import { KeyOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { SectionCard } from "./SectionCard";
import type { Principal, PrincipalCreated, PrincipalRole } from "../types";

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
  const [form] = Form.useForm<{ name: string; role: PrincipalRole }>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api.listPrincipals());
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

  const columns: ColumnsType<Principal> = [
    { title: "名称", dataIndex: "name", key: "name" },
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
        <Switch
          size="small"
          checked={active}
          onChange={(v) => patch(row.id, { active: v })}
        />
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
          <Popconfirm
            title="轮换后旧 Token 立即失效"
            onConfirm={() => rotate(row.id)}
          >
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
          type="info"
          showIcon
          title="四层角色：reader < editor < reviewer < publisher"
          description="ONTOMETA_ADMIN_TOKEN 仍为 superuser（等价 publisher）；未创建任何主体时行为与启用 RBAC 前一致。"
        />
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
        />
      </Space>

      <Modal
        open={createOpen}
        title="新建主体"
        onOk={create}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
      >
        <Form form={form} layout="vertical" initialValues={{ role: "reader" }}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
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
        <Paragraph copyable={{ text: issued?.token }}>
          <Text code>{issued?.token}</Text>
        </Paragraph>
        {issued && (
          <Text type="secondary">
            主体「{issued.name}」·{" "}
            <Tag color={ROLE_COLOR[issued.role]}>{issued.role}</Tag>
          </Text>
        )}
      </Modal>
    </SectionCard>
  );
}
