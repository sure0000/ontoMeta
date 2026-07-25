import { useEffect, useState } from "react";
import {
  Button,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { DataSource } from "../types";

const KIND_OPTIONS = ["sqlite", "duckdb", "postgres", "mysql", "cube", "mock"].map((v) => ({
  label: v,
  value: v,
}));

const STATUS_COLOR: Record<string, string> = {
  ok: "success",
  error: "error",
  untested: "default",
};

export function DataSourcesModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [sources, setSources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);
  const [form] = Form.useForm();

  const load = () => {
    setLoading(true);
    api
      .listDataSources()
      .then(setSources)
      .catch(() => setSources([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (open) load();
  }, [open]);

  const handleAdd = async () => {
    const values = await form.validateFields();
    let mapping: Record<string, unknown> | undefined;
    if (values.mapping?.trim()) {
      try {
        mapping = JSON.parse(values.mapping);
      } catch {
        message.error("映射必须是合法 JSON");
        return;
      }
    }
    try {
      await api.createDataSource({
        name: values.name,
        kind: values.kind,
        dsn_secret_ref: values.dsn_secret_ref,
        mapping,
      });
      message.success("已添加数据源");
      form.resetFields();
      setAdding(false);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "添加失败");
    }
  };

  const handleTest = async (id: string) => {
    try {
      const ds = await api.testDataSource(id);
      message.success(`连接状态：${ds.status}`);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "测试失败");
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteDataSource(id);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <Modal
      title="数据源管理"
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
    >
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<PlusOutlined />} onClick={() => setAdding((v) => !v)}>
          新增数据源
        </Button>
      </Space>

      {adding && (
        <Form form={form} layout="vertical" style={{ marginBottom: 16 }}>
          <Space align="start" wrap>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="如 生产库-只读" style={{ width: 160 }} />
            </Form.Item>
            <Form.Item name="kind" label="类型" initialValue="sqlite">
              <Select options={KIND_OPTIONS} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item
              name="dsn_secret_ref"
              label="连接串（SQLAlchemy URL）"
              tooltip="如 sqlite:////abs/path.db、postgresql+psycopg://user:pwd@host/db（建议只读账号）"
            >
              <Input placeholder="sqlite:////data/app.db" style={{ width: 300 }} />
            </Form.Item>
          </Space>
          <Form.Item
            name="mapping"
            label="物理映射（可选 JSON）"
            tooltip='本体名→物理名，如 {"tables":{"orders":"t_orders"},"columns":{"amount":"amt"}}'
          >
            <Input.TextArea rows={2} placeholder='{"tables":{},"columns":{}}' />
          </Form.Item>
          <Button type="primary" onClick={handleAdd}>
            保存
          </Button>
        </Form>
      )}

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={sources}
        pagination={false}
        columns={[
          { title: "名称", dataIndex: "name" },
          { title: "类型", dataIndex: "kind", render: (k: string) => <Tag>{k}</Tag> },
          {
            title: "状态",
            dataIndex: "status",
            render: (s: string) => <Tag color={STATUS_COLOR[s] ?? "default"}>{s}</Tag>,
          },
          {
            title: "操作",
            key: "actions",
            render: (_: unknown, row: DataSource) => (
              <Space>
                <Button size="small" onClick={() => handleTest(row.id)}>
                  测试
                </Button>
                <Popconfirm title="删除该数据源？" onConfirm={() => handleDelete(row.id)}>
                  <Button size="small" danger type="text">
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
    </Modal>
  );
}
