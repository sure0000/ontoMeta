/**
 * 依赖组件统一部署管理面板（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。
 *
 * 除 ontoMeta 自身前后端外，所有依赖组件在此统一管理：选一种部署方式
 * （已有服务 / Docker / Kubernetes / 物理机），部署成功自动回写连接信息，
 * 或选「已有」手填连接。ERPNext 等外部源库不在此纳管（走数据源管理）。
 *
 * Phase 0：本面板独立运作，不接既有 LLM/DataHub/Airflow/Cube 读取侧。
 * Phase 1 起既有配置迁移进本表、读取侧改为投影。
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  ApiOutlined,
  CloudServerOutlined,
  ContainerOutlined,
  DeleteOutlined,
  DesktopOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { ApiError, api } from "../api";
import type { DependencyComponent, DependencySchema } from "../types";

const { Text } = Typography;

const MODE_LABEL: Record<string, string> = {
  external: "已有服务",
  docker: "Docker",
  k8s: "Kubernetes",
  bare_metal: "物理机",
};

const MODE_ICON: Record<string, React.ReactNode> = {
  external: <CloudServerOutlined />,
  docker: <ContainerOutlined />,
  k8s: <ApiOutlined />,
  bare_metal: <DesktopOutlined />,
};

const STATUS_COLOR: Record<string, string> = {
  connected: "success",
  deployed: "success",
  deploying: "processing",
  failed: "error",
  not_deployed: "default",
};
const STATUS_LABEL: Record<string, string> = {
  connected: "已连接",
  deployed: "已部署",
  deploying: "部署中",
  failed: "失败",
  not_deployed: "未部署",
};

export function DependencyPanel() {
  const [schema, setSchema] = useState<DependencySchema | null>(null);
  const [rows, setRows] = useState<DependencyComponent[]>([]);
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<DependencyComponent | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const [deploying, setDeploying] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sch, list] = await Promise.all([
        api.getDependencySchema(),
        api.listDependencies(),
      ]);
      setSchema(sch);
      setRows(list);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ deploy_mode: "external", enabled: true });
    setDrawerOpen(true);
  };

  const openEdit = (row: DependencyComponent) => {
    setEditing(row);
    form.setFieldsValue({
      key: row.key,
      name: row.name,
      deploy_mode: row.deploy_mode,
      enabled: row.enabled,
    });
    (schema?.connection_schemas[row.key] ?? []).forEach((f) => {
      form.setFieldValue(`conn_${f.name}`, row.connection[f.name] ?? "");
    });
    // 部署参数回填
    Object.entries(row.deploy_spec ?? {}).forEach(([k, v]) => {
      form.setFieldValue(`spec_${k}`, v as unknown);
    });
    setDrawerOpen(true);
  };

  const handleSave = async () => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const key = values.key as string;
    const mode = values.deploy_mode as string;
    const conn: Record<string, unknown> = {};
    (schema?.connection_schemas[key] ?? []).forEach((f) => {
      conn[f.name] = values[`conn_${f.name}`] ?? null;
    });
    // 收集部署参数（spec_*）
    const deploySpec: Record<string, unknown> = {};
    const specFields =
      mode === "bare_metal"
        ? schema?.bare_metal_params[key] ?? []
        : mode === "docker"
          ? schema?.docker_params[key] ?? []
          : mode === "k8s"
            ? schema?.deploy_spec_schemas["k8s"] ?? []
            : [];
    specFields.forEach((f) => {
      const v = values[`spec_${f.name}`];
      if (v !== undefined && v !== null && v !== "") deploySpec[f.name] = v;
    });
    const body = {
      key,
      name: values.name as string | undefined,
      deploy_mode: mode,
      enabled: values.enabled as boolean,
      connection: mode === "external" ? conn : {},
      deploy_spec: deploySpec,
    };
    setSaving(true);
    try {
      if (editing) {
        await api.updateDependency(editing.id, body);
        message.success("已保存");
      } else {
        await api.createDependency(body);
        message.success("已新增");
      }
      setDrawerOpen(false);
      await load();
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleProbe = async (id: string) => {
    setProbing((p) => ({ ...p, [id]: true }));
    try {
      const r = await api.probeDependency(id);
      if (r.ok) message.success(`拨测通过${r.latency_ms ? ` · ${r.latency_ms}ms` : ""}`);
      else message.error(r.message);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "拨测失败");
    } finally {
      setProbing((p) => ({ ...p, [id]: false }));
    }
  };

  const handleDeploy = async (id: string) => {
    setDeploying((p) => ({ ...p, [id]: true }));
    try {
      const r = await api.deployDependency(id);
      if (r.ok) message.success(r.message ?? "部署成功");
      else message.warning(r.message ?? "部署未完成");
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "部署失败");
    } finally {
      setDeploying((p) => ({ ...p, [id]: false }));
    }
  };

  const handleTeardown = async (id: string) => {
    try {
      await api.teardownDependency(id);
      message.success("已卸载");
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "卸载失败");
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteDependency(id);
      message.success("已删除");
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const columns: ColumnsType<DependencyComponent> = [
    {
      title: "组件",
      dataIndex: "key",
      width: 160,
      render: (k: string) => {
        const meta = schema?.components.find((c) => c.key === k);
        return <Tag>{meta?.label ?? k}</Tag>;
      },
    },
    {
      title: "名称",
      dataIndex: "name",
      width: 140,
      render: (n: string, r) => (
        <Space size={4}>
          {n}
          {r.is_default ? <Tag color="blue">默认</Tag> : null}
          {r.key === "warehouse" && r.deploy_spec?._datasource_id ? (
            <Tooltip title="已自动创建数据源，可作为物化目标">
              <Tag color="green" style={{ fontSize: 11 }}>
                ✓ 数据源已链
              </Tag>
            </Tooltip>
          ) : null}
        </Space>
      ),
    },
    {
      title: "部署方式",
      dataIndex: "deploy_mode",
      width: 120,
      render: (m: string) => (
        <Space size={4}>
          {MODE_ICON[m]} {MODE_LABEL[m] ?? m}
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "deploy_status",
      width: 100,
      render: (s: string, r) => (
        <Tooltip title={r.deploy_error}>
          <Tag color={STATUS_COLOR[s] ?? "default"}>{STATUS_LABEL[s] ?? s}</Tag>
        </Tooltip>
      ),
    },
    {
      title: "操作",
      width: 220,
      render: (_: unknown, r: DependencyComponent) => (
        <Space size="small">
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Button
            size="small"
            onClick={() => void handleProbe(r.id)}
            loading={probing[r.id]}
          >
            拨测
          </Button>
          {r.deploy_mode !== "external" ? (
            <>
              <Button
                size="small"
                type="primary"
                loading={deploying[r.id]}
                onClick={() => void handleDeploy(r.id)}
              >
                部署
              </Button>
              <Popconfirm title="卸载该组件？" onConfirm={() => void handleTeardown(r.id)}>
                <Button size="small">卸载</Button>
              </Popconfirm>
            </>
          ) : null}
          <Popconfirm title="删除该组件？" onConfirm={() => void handleDelete(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const addable =
    schema?.components
      .filter((c) => c.key !== "airflow" && c.key !== "sync_runner")
      .filter((c) => c.multi || !rows.some((r) => r.key === c.key)) ?? [];

  return (
    <>
      <Alert
        style={{ marginBottom: 16 }}
        type="info"
        showIcon
        message="依赖组件统一管理"
        description="每个组件选一种部署方式：选「部署」由 ontoMeta 拉起并自动回收连接信息，选「已有」手填连接。ERPNext 等外部源库走「数据源管理」，不在此纳管。"
      />
      <Space style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
        <Text type="secondary">共 {rows.length} 个组件</Text>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
            disabled={addable.length === 0}
          >
            新增
          </Button>
        </Space>
      </Space>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        pagination={false}
        locale={{ emptyText: "暂无组件，点击「新增」" }}
      />
      <Drawer
        title={editing ? "编辑依赖组件" : "新增依赖组件"}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={520}
        extra={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saving} onClick={() => void handleSave()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item label="组件类型" name="key" rules={[{ required: true }]}>
            <Select
              disabled={!!editing}
              placeholder="选择组件"
              options={(schema?.components ?? []).map((c) => ({
                value: c.key,
                label: c.label,
              }))}
            />
          </Form.Item>
          <Form.Item label="展示名" name="name">
            <Input placeholder="留空则用组件默认名" />
          </Form.Item>
          <Form.Item label="部署方式" name="deploy_mode" rules={[{ required: true }]}>
            <Select
              options={(schema?.deploy_modes ?? []).map((m) => ({
                value: m,
                label: MODE_LABEL[m] ?? m,
              }))}
            />
          </Form.Item>
          <Form.Item label="启用" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item shouldUpdate noStyle>
            {({ getFieldValue }) => {
              const key = getFieldValue("key") as string | undefined;
              const mode = getFieldValue("deploy_mode") as string;
              if (!key) return <Text type="secondary">请先选择组件类型</Text>;
              // external：手填连接信息；其余模式：填部署参数，部署后自动回收连接。
              if (mode === "external") {
                const fields = schema?.connection_schemas[key] ?? [];
                return (
                  <>
                    <Divider>连接信息</Divider>
                    {fields.length === 0 ? (
                      <Text type="secondary">该组件暂无连接字段</Text>
                    ) : (
                      fields.map((f) => (
                        <Form.Item
                          key={f.name}
                          label={f.name}
                          name={`conn_${f.name}`}
                          rules={f.required ? [{ required: true, message: "必填" }] : []}
                        >
                          {f.secret ? (
                            <Input.Password placeholder={editing ? "留空=保持不变" : ""} />
                          ) : (
                            <Input />
                          )}
                        </Form.Item>
                      ))
                    )}
                  </>
                );
              }
              // bare_metal / docker / k8s：部署参数
              const specFields =
                mode === "bare_metal"
                  ? schema?.bare_metal_params[key] ?? []
                  : mode === "docker"
                    ? schema?.docker_params[key] ?? []
                    : schema?.deploy_spec_schemas["k8s"] ?? [];
              return (
                <>
                  <Divider>部署参数</Divider>
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={
                      mode === "bare_metal"
                        ? "登记一台已装好的物理机服务：填 host + 端口 + 凭据，点「保存」后在列表点「部署」自动拼连接并拨测。"
                        : mode === "docker"
                          ? "填 compose 文件与服务端口，点「保存」后在列表点「部署」起容器并自动回收连接。"
                          : "填 K8s manifest（含同名 Service），点「保存」后在列表点「部署」apply 并回收端点。"
                    }
                  />
                  {specFields.map((f) => (
                    <Form.Item
                      key={f.name}
                      label={f.name}
                      name={`spec_${f.name}`}
                      rules={f.required ? [{ required: true, message: "必填" }] : []}
                    >
                      {f.secret ? <Input.Password /> : f.type === "int" ? <InputNumber style={{ width: "100%" }} /> : <Input />}
                    </Form.Item>
                  ))}
                  {editing && Object.keys(editing.connection).length > 0 && (
                    <>
                      <Divider>已回收的连接（只读）</Divider>
                      <Text type="secondary" style={{ fontSize: 13 }}>
                        {JSON.stringify(editing.connection)}
                      </Text>
                    </>
                  )}
                </>
              );
            }}
          </Form.Item>
        </Form>
      </Drawer>
    </>
  );
}
