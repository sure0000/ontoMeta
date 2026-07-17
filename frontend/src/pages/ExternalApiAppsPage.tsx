import {
  ApiOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
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
import { useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import type { ExternalApp, ExternalAppCreated } from "../types";
import { formatDateTime } from "../utils/format";

const { Text, Paragraph } = Typography;

type AppFormValues = {
  name: string;
  description?: string;
  scopes: string[];
  rate_limit_per_minute?: number | null;
};

const SCOPE_LABELS: Record<string, string> = {
  "domains:read": "数据域只读",
  "objects:read": "业务对象只读",
  "relations:read": "业务关系只读",
  "logics:read": "业务逻辑只读",
};

function KeyRevealModal({
  open,
  title,
  app,
  onClose,
}: {
  open: boolean;
  title: string;
  app: ExternalAppCreated | null;
  onClose: () => void;
}) {
  if (!app) return null;
  return (
    <Modal
      open={open}
      title={title}
      onCancel={onClose}
      footer={[
        <Button key="close" type="primary" onClick={onClose}>
          我已保存
        </Button>,
      ]}
      destroyOnClose
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="请立即复制并妥善保管 API Key，关闭后将无法再次完整查看（可重新生成）。"
      />
      <div style={{ marginBottom: 12 }}>
        <Text type="secondary">应用标识 (App Key)</Text>
        <Paragraph
          copyable={{ text: app.app_key }}
          style={{
            marginBottom: 0,
            marginTop: 4,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          }}
        >
          {app.app_key}
        </Paragraph>
      </div>
      <div>
        <Text type="secondary">API Key</Text>
        <Paragraph
          copyable={{ text: app.api_key }}
          style={{
            marginBottom: 0,
            marginTop: 4,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            wordBreak: "break-all",
          }}
        >
          {app.api_key}
        </Paragraph>
      </div>
    </Modal>
  );
}

export function ExternalApiAppsPage() {
  const {
    data: appsData,
    loading,
    error,
    reload: load,
  } = useApi(() => api.listExternalApps(), []);
  const apps = appsData ?? [];

  const { data: scopesBundle } = useApi(() => api.listExternalScopes(), []);
  const availableScopes = scopesBundle?.scopes ?? Object.keys(SCOPE_LABELS);

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ExternalApp | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [revealed, setRevealed] = useState<ExternalAppCreated | null>(null);
  const [revealTitle, setRevealTitle] = useState("API Key 已生成");
  const [form] = Form.useForm<AppFormValues>();

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ scopes: [...availableScopes] });
    setModalOpen(true);
  };

  const openEdit = (row: ExternalApp) => {
    setEditing(row);
    form.setFieldsValue({
      name: row.name,
      description: row.description ?? undefined,
      scopes: row.scopes?.length ? row.scopes : [...availableScopes],
      rate_limit_per_minute: row.rate_limit_per_minute ?? undefined,
    });
    setModalOpen(true);
  };

  const handleSubmit = async () => {
    const values = await form.validateFields();
    setSubmitting(true);
    try {
      if (editing) {
        await api.updateExternalApp(editing.id, {
          name: values.name,
          description: values.description,
          scopes: values.scopes,
          rate_limit_per_minute: values.rate_limit_per_minute ?? null,
        });
        message.success("应用已更新");
        setModalOpen(false);
        await load();
      } else {
        const created = await api.createExternalApp({
          name: values.name,
          description: values.description,
          scopes: values.scopes,
          rate_limit_per_minute: values.rate_limit_per_minute ?? null,
        });
        message.success("应用已创建");
        setModalOpen(false);
        setRevealTitle("应用创建成功 · API Key");
        setRevealed(created);
        await load();
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleStatus = async (row: ExternalApp, checked: boolean) => {
    try {
      await api.updateExternalApp(row.id, {
        status: checked ? "active" : "disabled",
      });
      message.success(checked ? "已启用" : "已禁用");
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRegenerate = async (row: ExternalApp) => {
    try {
      const created = await api.regenerateExternalAppKey(row.id);
      setRevealTitle("密钥已重新生成");
      setRevealed(created);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDelete = async (row: ExternalApp) => {
    try {
      await api.deleteExternalApp(row.id);
      message.success("已删除");
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const columns: ColumnsType<ExternalApp> = [
    {
      title: "应用名称",
      dataIndex: "name",
      key: "name",
      width: 160,
      ellipsis: true,
      render: (name: string) => (
        <Text strong style={{ fontSize: 14 }}>
          {name}
        </Text>
      ),
    },
    {
      title: "描述",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (desc?: string | null) =>
        desc ? (
          <Text type="secondary">{desc}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: "权限 Scope",
      dataIndex: "scopes",
      key: "scopes",
      width: 220,
      render: (scopes: string[] | undefined) => (
        <Space size={[4, 4]} wrap>
          {(scopes || []).map((s) => (
            <Tag key={s} style={{ marginInlineEnd: 0 }}>
              {s}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string, row) => (
        <Space size={8}>
          <Tag color={status === "active" ? "success" : "default"}>
            {status === "active" ? "启用" : "禁用"}
          </Tag>
          <Switch
            size="small"
            checked={status === "active"}
            onChange={(checked) => void handleToggleStatus(row, checked)}
          />
        </Space>
      ),
    },
    {
      title: "密钥提示",
      dataIndex: "api_key_hint",
      key: "api_key_hint",
      width: 140,
      render: (hint?: string | null) => (
        <Text
          type="secondary"
          style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 12 }}
        >
          {hint || "—"}
        </Text>
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 140,
      render: (v: string) => (
        <Text type="secondary">{formatDateTime(v) ?? "—"}</Text>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 280,
      fixed: "right",
      render: (_, row) => (
        <Space size={0} style={{ whiteSpace: "nowrap" }}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(row)}
          >
            编辑
          </Button>
          <Popconfirm
            title="重新生成密钥？"
            description="旧密钥将立即失效。明文仅显示一次，请立即保存。"
            onConfirm={() => void handleRegenerate(row)}
          >
            <Button type="link" size="small" icon={<ReloadOutlined />}>
              重置密钥
            </Button>
          </Popconfirm>
          <Popconfirm
            title="确认删除该应用？"
            description="删除后对应密钥将无法再调用 MCP 接口。"
            onConfirm={() => void handleDelete(row)}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (loading && apps.length === 0) {
    return (
      <PageContainer>
        <PageHeader
          title="应用创建"
          description="为 Agent 创建接入应用并生成 API Key"
          icon={<KeyOutlined />}
        />
        <PageSkeleton type="list" />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title="应用创建"
        description="为 Agent / MCP Client 创建接入应用，生成调用 MCP Tools 所需的 API Key"
        icon={<KeyOutlined />}
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建应用
          </Button>
        }
      />

      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      )}

      <SectionCard
        title="已接入应用"
        count={apps.length}
        icon={<ApiOutlined />}
        bodyFlush
      >
        {apps.length === 0 ? (
          <EmptyState
            title="暂无外部应用"
            description="创建应用后，Agent 可使用 API Key 通过 MCP 调用业务对象、关系与逻辑 Tools"
            action={
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                新建应用
              </Button>
            }
          />
        ) : (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={apps}
            pagination={false}
            size="middle"
            scroll={{ x: 980 }}
          />
        )}
      </SectionCard>

      <Modal
        open={modalOpen}
        title={editing ? "编辑应用" : "新建应用"}
        onCancel={() => setModalOpen(false)}
        onOk={() => void handleSubmit()}
        confirmLoading={submitting}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="name"
            label="应用名称"
            rules={[{ required: true, message: "请输入应用名称" }]}
          >
            <Input placeholder="例如：数据分析平台" maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea
              placeholder="用途说明（可选）"
              rows={3}
              maxLength={500}
            />
          </Form.Item>
          <Form.Item
            name="scopes"
            label="权限 Scope"
            rules={[{ required: true, message: "至少选择一个 scope" }]}
            extra="无对应 scope 的 Key 访问相关接口将返回 403"
          >
            <Select
              mode="multiple"
              placeholder="选择可访问的资源范围"
              options={availableScopes.map((s) => ({
                value: s,
                label: `${SCOPE_LABELS[s] || s} (${s})`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="rate_limit_per_minute"
            label="每分钟限流（可选）"
            extra="留空则使用服务端默认 EXTERNAL_API_RATE_LIMIT_PER_MINUTE"
          >
            <InputNumber min={1} max={10000} style={{ width: "100%" }} placeholder="例如 60" />
          </Form.Item>
        </Form>
      </Modal>

      <KeyRevealModal
        open={!!revealed}
        title={revealTitle}
        app={revealed}
        onClose={() => setRevealed(null)}
      />
    </PageContainer>
  );
}
