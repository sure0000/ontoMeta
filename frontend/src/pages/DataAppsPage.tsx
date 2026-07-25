import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import {
  AppstoreOutlined,
  PlusOutlined,
  TableOutlined,
  FundProjectionScreenOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import type { DataAppSummary, DomainContext } from "../types";

const TYPE_LABEL: Record<string, string> = {
  data_table: "数据表格",
  screen: "可视化大屏",
};

const STATUS_COLOR: Record<string, string> = {
  draft: "default",
  in_review: "processing",
  published: "success",
  archived: "warning",
};

export function DataAppsPage() {
  const navigate = useNavigate();
  const [domainFilter, setDomainFilter] = useState<string | undefined>();
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm();

  const { data: domains } = useApi<DomainContext[]>(
    async () => api.listDomains(),
    [],
  );
  const { data: apps, loading, reload } = useApi<DataAppSummary[]>(
    async () => api.listDataApps(domainFilter),
    [domainFilter],
  );

  const domainOptions = useMemo(
    () => (domains ?? []).map((d) => ({ label: d.name, value: d.id })),
    [domains],
  );

  const handleCreate = async () => {
    const values = await form.validateFields();
    try {
      const app = await api.createDataApp({
        domain_id: values.domain_id,
        app_type: values.app_type,
        name: values.name,
      });
      message.success("已创建数据应用草稿");
      setCreating(false);
      form.resetFields();
      navigate(`/data-apps/${app.id}/edit`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteDataApp(id);
      message.success("已删除");
      reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const columns = [
    {
      title: "名称",
      dataIndex: "name",
      render: (name: string, row: DataAppSummary) => (
        <a onClick={() => navigate(`/data-apps/${row.id}/edit`)}>{name}</a>
      ),
    },
    {
      title: "类型",
      dataIndex: "app_type",
      render: (t: string) => (
        <Tag icon={t === "screen" ? <FundProjectionScreenOutlined /> : <TableOutlined />}>
          {TYPE_LABEL[t] ?? t}
        </Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (s: string) => <Tag color={STATUS_COLOR[s] ?? "default"}>{s}</Tag>,
    },
    {
      title: "来源",
      dataIndex: "source",
      render: (s: string) =>
        s === "chat_generated" ? <Tag color="blue">问数生成</Tag> : <Tag>手工</Tag>,
    },
    {
      title: "版本",
      dataIndex: "current_version",
      render: (v: number, row: DataAppSummary) =>
        row.published_version ? `已发布 v${row.published_version}` : `草稿 v${v}`,
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, row: DataAppSummary) => (
        <Space>
          <Button size="small" onClick={() => navigate(`/data-apps/${row.id}/edit`)}>
            编辑
          </Button>
          {row.status === "published" && (
            <Button size="small" type="link" onClick={() => navigate(`/apps/${row.id}`)}>
              查看
            </Button>
          )}
          <Popconfirm title="确认删除该数据应用？" onConfirm={() => handleDelete(row.id)}>
            <Button size="small" danger type="text">
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={<AppstoreOutlined />}
        title="数据应用"
        description="基于已发布本体创建数据表格页面与可视化大屏，支持预览与发布。"
        extra={
          <Space>
            <Select
              allowClear
              placeholder="按数据域筛选"
              style={{ width: 200 }}
              options={domainOptions}
              value={domainFilter}
              onChange={setDomainFilter}
            />
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setCreating(true)}
            >
              新建应用
            </Button>
          </Space>
        }
      />
      <Card>
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={apps ?? []}
          locale={{ emptyText: <Empty description="暂无数据应用，点击右上角新建" /> }}
        />
      </Card>

      <Modal
        title="新建数据应用"
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={handleCreate}
        okText="创建并编辑"
      >
        <Form form={form} layout="vertical" initialValues={{ app_type: "data_table" }}>
          <Form.Item
            name="domain_id"
            label="数据域"
            rules={[{ required: true, message: "请选择数据域" }]}
          >
            <Select placeholder="选择数据域" options={domainOptions} />
          </Form.Item>
          <Form.Item name="app_type" label="应用类型">
            <Segmented
              options={[
                { label: "数据表格", value: "data_table", icon: <TableOutlined /> },
                {
                  label: "可视化大屏",
                  value: "screen",
                  icon: <FundProjectionScreenOutlined />,
                },
              ]}
            />
          </Form.Item>
          <Form.Item name="name" label="名称">
            <Input placeholder="可选，默认按类型命名" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
