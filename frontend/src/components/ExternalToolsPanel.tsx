import { DeleteOutlined, PlusOutlined, ReloadOutlined, ToolOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
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
import { ApiError, api } from "../api";
import { SectionCard } from "./SectionCard";
import type { ChatBiExternalTool } from "../types";

const { Text } = Typography;

/**
 * P4：配置驱动的外部工具管理（Data Agent 免改代码扩能力）。
 * 运维在此注册 HTTP 工具，**启用**即注入 Data Agent 工具集；机密（auth_header）写入后不回显。
 * 整个命名空间的写操作走全局管理鉴权（与治理智能体一致）。
 */
export function ExternalToolsPanel() {
  const [rows, setRows] = useState<ChatBiExternalTool[]>([]);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm();

  const handleError = useCallback((err: unknown, fallback: string) => {
    if (err instanceof ApiError && err.status === 403) {
      setForbidden(true);
      return;
    }
    message.error(err instanceof Error ? err.message : fallback);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api.listExternalTools());
      setForbidden(false);
    } catch (err) {
      handleError(err, "加载失败");
    } finally {
      setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    const v = await form.validateFields();
    let parameters: Record<string, unknown> | undefined;
    if (v.parameters?.trim()) {
      try {
        parameters = JSON.parse(v.parameters) as Record<string, unknown>;
      } catch {
        message.error("入参 JSON-Schema 不是合法 JSON");
        return;
      }
    }
    try {
      await api.registerExternalTool({
        name: v.name.trim(),
        description: v.description.trim(),
        url: v.url.trim(),
        method: v.method,
        parameters,
        auth_header: v.auth_header?.trim() || undefined,
        domain_id: v.domain_id?.trim() || null,
        result_max_chars: v.result_max_chars || undefined,
      });
      message.success("已注册");
      setCreateOpen(false);
      form.resetFields();
      await load();
    } catch (err) {
      handleError(err, "注册失败");
    }
  };

  const toggle = async (row: ChatBiExternalTool, enabled: boolean) => {
    try {
      await api.toggleExternalTool(row.id, enabled);
      await load();
    } catch (err) {
      handleError(err, "操作失败");
    }
  };

  const remove = async (row: ChatBiExternalTool) => {
    try {
      await api.deleteExternalTool(row.id);
      message.success("已删除");
      await load();
    } catch (err) {
      handleError(err, "删除失败");
    }
  };

  const columns: ColumnsType<ChatBiExternalTool> = [
    {
      title: "工具",
      key: "name",
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text code>{r.name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.display_name || r.description}
          </Text>
        </Space>
      ),
    },
    {
      title: "端点",
      key: "url",
      render: (_, r) => (
        <Space size={4}>
          <Tag>{r.method}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>{r.url}</Text>
          {r.has_auth && <Tag color="gold">带鉴权</Tag>}
        </Space>
      ),
    },
    {
      title: "作用域",
      key: "domain",
      render: (_, r) => (r.domain_id ? <Tag>域 {r.domain_id.slice(0, 8)}</Tag> : <Tag color="blue">全局</Tag>),
    },
    {
      title: "启用",
      key: "enabled",
      render: (_, r) => (
        <Switch checked={r.enabled} onChange={(v) => void toggle(r, v)} disabled={forbidden} />
      ),
    },
    {
      title: "操作",
      key: "actions",
      render: (_, r) => (
        <Popconfirm title="删除该外部工具？" onConfirm={() => void remove(r)} disabled={forbidden}>
          <Button size="small" danger icon={<DeleteOutlined />} disabled={forbidden} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <SectionCard
      title="外部工具（Data Agent 扩展）"
      icon={<ToolOutlined />}
      count={rows.length}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} />
          <Button type="primary" icon={<PlusOutlined />} disabled={forbidden} onClick={() => setCreateOpen(true)}>
            注册工具
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {forbidden ? (
          <Alert
            type="error"
            showIcon
            message="需要管理鉴权"
            description="外部工具的写操作需管理 Token（X-Admin-Token 或 Bearer）。"
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="注册即扩展：启用的外部工具会被 Data Agent 按名调用"
            description="仅 http(s)；结果封顶；机密（鉴权头）写入后不回显。每个数据域最多注入 8 个。"
          />
        )}
        <Table rowKey="id" size="small" loading={loading} columns={columns} dataSource={rows} pagination={false} />
      </Space>

      {createOpen && (
        <ExternalToolForm form={form} onOk={create} onCancel={() => setCreateOpen(false)} />
      )}
    </SectionCard>
  );
}

function ExternalToolForm({
  form,
  onOk,
  onCancel,
}: {
  form: ReturnType<typeof Form.useForm>[0];
  onOk: () => void;
  onCancel: () => void;
}) {
  return (
    <div style={{ marginTop: 12, padding: 16, border: "1px solid rgba(0,0,0,0.08)", borderRadius: 8 }}>
      <Form form={form} layout="vertical" initialValues={{ method: "POST", result_max_chars: 4000 }}>
        <Form.Item
          name="name"
          label="工具名（snake_case，不得与原生工具同名）"
          rules={[{ required: true }, { pattern: /^[a-z][a-z0-9_]{1,63}$/, message: "小写字母开头的 snake_case" }]}
        >
          <Input placeholder="如 dq_check / ticket_create" />
        </Form.Item>
        <Form.Item name="description" label="描述（模型据此判断何时调用）" rules={[{ required: true }]}>
          <Input.TextArea rows={2} placeholder="如：对给定表运行数据质量检查，返回评分与问题清单" />
        </Form.Item>
        <Space>
          <Form.Item name="method" label="方法">
            <Select style={{ width: 100 }} options={[{ value: "POST" }, { value: "GET" }]} />
          </Form.Item>
          <Form.Item name="url" label="端点 URL（http/https）" rules={[{ required: true }]} style={{ flex: 1, minWidth: 360 }}>
            <Input placeholder="https://internal.example.com/tools/dq-check" />
          </Form.Item>
        </Space>
        <Form.Item name="parameters" label="入参 JSON-Schema（可选）">
          <Input.TextArea rows={3} placeholder='{"type":"object","properties":{"table":{"type":"string"}}}' />
        </Form.Item>
        <Space>
          <Form.Item name="auth_header" label="鉴权头（机密，可选）">
            <Input.Password placeholder="Bearer xxx" autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="domain_id" label="数据域 ID（空=全局）">
            <Input placeholder="domain_id（可选）" />
          </Form.Item>
          <Form.Item name="result_max_chars" label="结果封顶字符">
            <InputNumber min={200} max={20000} />
          </Form.Item>
        </Space>
        <Space>
          <Button type="primary" onClick={onOk}>注册</Button>
          <Button onClick={onCancel}>取消</Button>
        </Space>
      </Form>
    </div>
  );
}
