/** sync-runner 的连接配置（凭据代填）。
 *
 * **这里是输入框，不是凭据库**：填的值经 ontoMeta 直接转给 runner，落在 runner 自己的
 * 存储里，ontoMeta 不落库、不缓存、读不回明文。凭据只有一个归属地——runner 的 /probe
 * 才因此有意义，ontoMeta 产出的 DAG 里也才只有别名。
 *
 * 列表里 source=env 的别名由部署时的环境变量钉死，**不接受从这里改**：环境变量优先级
 * 更高，静默覆盖会「保存成功但不生效」，比报错更难查。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { DeleteOutlined, KeyOutlined, PlusOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { SyncRunnerSecret } from "../types";

interface EditorValues {
  alias: string;
  url?: string;
  user?: string;
  password?: string;
  metastore_uri?: string;
}

export function SyncRunnerSecretsPanel() {
  const [items, setItems] = useState<SyncRunnerSecret[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<SyncRunnerSecret | null>(null);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<EditorValues>();

  const load = useCallback(() => {
    setLoading(true);
    api
      .listSyncRunnerSecrets()
      .then((rows) => {
        setItems(rows);
        setError(null);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "读取连接配置失败"),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const openEditor = (row: SyncRunnerSecret | null) => {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue({
      alias: row?.alias ?? "",
      // 非机密项回填现值便于改；机密项一律留空，留空 = 保持不变
      url: row?.values.url,
      metastore_uri: row?.values.metastore_uri,
    });
    setOpen(true);
  };

  const submit = async () => {
    let values: EditorValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const { alias, ...rest } = values;
    const payload: Record<string, string> = {};
    for (const [key, value] of Object.entries(rest)) {
      if (value !== undefined && value !== "") payload[key] = value;
    }
    try {
      await api.putSyncRunnerSecret(alias.trim(), payload);
      message.success("已写入 runner");
      setOpen(false);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "写入失败");
    }
  };

  const remove = async (alias: string) => {
    try {
      await api.deleteSyncRunnerSecret(alias);
      message.success("已删除");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="连接配置存在 sync-runner 一侧，ontoMeta 不保存"
        description="这里填的值会直接转给 runner 落进它自己的存储：ontoMeta 不落库、不缓存，也读不回明文。这样凭据只有一个归属地，物化产出的 DAG 里始终只有别名。写入需要 runner 设了 token（在上方 Airflow 配置里填）。"
      />
      {error && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={error} />
      )}
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<PlusOutlined />} onClick={() => openEditor(null)}>
          新增连接
        </Button>
        <Button onClick={load} loading={loading}>
          刷新
        </Button>
      </Space>
      <Table<SyncRunnerSecret>
        rowKey="alias"
        size="small"
        loading={loading}
        dataSource={items}
        locale={{ emptyText: <Empty description="runner 侧还没有配置任何连接" /> }}
        pagination={false}
        columns={[
          { title: "别名", dataIndex: "alias", width: 200 },
          {
            title: "来源",
            dataIndex: "source",
            width: 120,
            render: (source: string) =>
              source === "env" ? (
                <Tag color="default">环境变量（只读）</Tag>
              ) : (
                <Tag color="blue">设置页写入</Tag>
              ),
          },
          {
            title: "已配置的项",
            dataIndex: "values",
            render: (values: Record<string, string>) => (
              <Space size={[4, 4]} wrap>
                {Object.entries(values).map(([key, value]) => (
                  <Tag key={key} icon={value === "<已设置>" ? <KeyOutlined /> : undefined}>
                    {key}
                    {value === "<已设置>" ? "" : `=${value}`}
                  </Tag>
                ))}
              </Space>
            ),
          },
          {
            title: "",
            width: 120,
            render: (_: unknown, row: SyncRunnerSecret) =>
              row.source === "env" ? (
                <span style={{ color: "#999" }}>部署固定</span>
              ) : (
                <Space>
                  <Button type="link" size="small" onClick={() => openEditor(row)}>
                    编辑
                  </Button>
                  <Popconfirm title={`删除 ${row.alias}？`} onConfirm={() => void remove(row.alias)}>
                    <Button type="link" size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
          },
        ]}
      />
      <Modal
        open={open}
        title={editing ? `编辑连接 ${editing.alias}` : "新增连接"}
        onCancel={() => setOpen(false)}
        onOk={() => void submit()}
        okText="写入 runner"
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            label="别名"
            name="alias"
            rules={[{ required: true, message: "请输入别名" }]}
            extra="要与作业里用的别名一致：源库默认 erp_readonly，目标仓是 ontometa_ds_<数据源名>"
          >
            <Input placeholder="erp_readonly" disabled={!!editing} />
          </Form.Item>
          <Form.Item
            label="连接串"
            name="url"
            extra="SQLAlchemy 形式，如 mysql+pymysql://user:pw@host:3306/db。账号密码可以写在串里，也可以用下面两项分开填"
          >
            <Input placeholder="mysql+pymysql://user:pw@host:3306/db" />
          </Form.Item>
          <Space align="start" wrap>
            <Form.Item label="账号" name="user">
              <Input style={{ width: 200 }} autoComplete="off" />
            </Form.Item>
            <Form.Item
              label="密码"
              name="password"
              extra={editing ? "留空 = 保持不变" : undefined}
            >
              <Input.Password style={{ width: 220 }} autoComplete="new-password" />
            </Form.Item>
          </Space>
          <Form.Item
            label="Hive metastore 地址"
            name="metastore_uri"
            extra="仅 Hive 目标需要。主机名不能带下划线，也不能因容器 DNS 搜索域被拼出下划线，否则 Hive 的 URI 解析会直接报错"
          >
            <Input placeholder="thrift://host.docker.internal:9083" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
