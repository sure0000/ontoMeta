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
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
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

// Airflow 编排专有参数（存 deploy_spec.extra）。从 AirflowSettingsPanel 合并而来，
// 连接字段已归本面板的 connection，sync-runner 连接归 sync_runner 组件，这里只剩编排旋钮。
const AIRFLOW_EXTRA_FIELDS = [
  "dag_delivery_method", "dags_dir", "jobs_dir",
  "git_remote", "git_branch", "git_auto_init", "git_author", "git_email",
  "sync_channel", "docker_network", "drivers_dir", "sync_tool_images", "sync_tool",
  "max_tasks_per_dag", "max_active_tasks_per_dag", "dag_parse_timeout",
  "preflight_sentinel_timeout", "staging_swap",
] as const;

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
  const [logRow, setLogRow] = useState<DependencyComponent | null>(null);

  // 组件类型/部署方式切换时，上一组 conn_*/spec_* 字段靠 antd preserve 留在 store 里，
  // 同名字段（如 conn_endpoint、conn_token 在 sync_runner/airflow/datahub 都存在）会把
  // 上次填的值带过来——「新增时表单残留上次内容」即此。切换即清空动态字段。
  const dynamicFieldNames = useMemo(() => {
    if (!schema) return [];
    const names: string[] = [];
    Object.values(schema.connection_schemas).forEach((fs) =>
      fs.forEach((f) => names.push(`conn_${f.name}`)),
    );
    [schema.bare_metal_params, schema.docker_params].forEach((m) =>
      Object.values(m ?? {}).forEach((fs) =>
        fs.forEach((f) => names.push(`spec_${f.name}`)),
      ),
    );
    (schema.deploy_spec_schemas?.k8s ?? []).forEach((f) =>
      names.push(`spec_${f.name}`),
    );
    return names;
  }, [schema]);
  const clearDynamicFields = useCallback(() => {
    if (dynamicFieldNames.length) form.resetFields(dynamicFieldNames);
  }, [form, dynamicFieldNames]);

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
    form.resetFields();
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
      if (k === "extra") return; // extra 单独回填到 extra_* 字段
      form.setFieldValue(`spec_${k}`, v as unknown);
    });
    // Airflow 编排参数回填（存 deploy_spec.extra）
    const extra = (row.deploy_spec?.extra ?? {}) as Record<string, unknown>;
    AIRFLOW_EXTRA_FIELDS.forEach((f) => {
      if (f in extra) form.setFieldValue(`extra_${f}`, extra[f]);
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
        ? schema?.bare_metal_params?.[key] ?? []
        : mode === "docker"
          ? schema?.docker_params?.[key] ?? []
          : mode === "k8s"
            ? schema?.deploy_spec_schemas?.["k8s"] ?? []
            : [];
    specFields.forEach((f) => {
      const v = values[`spec_${f.name}`];
      if (v !== undefined && v !== null && v !== "") deploySpec[f.name] = v;
    });
    // Airflow 编排参数 → deploy_spec.extra
    if (key === "airflow") {
      const extra: Record<string, unknown> = {};
      AIRFLOW_EXTRA_FIELDS.forEach((f) => {
        const v = values[`extra_${f}`];
        if (v !== undefined && v !== null && v !== "") extra[f] = v;
      });
      if (Object.keys(extra).length) deploySpec.extra = extra;
    }
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
      // external 同步返回最终态；docker/k8s/bare_metal（SSH 安装可能数分钟）先返回
      // deploying，需轮询 getDependency 直到状态落定。
      if (r.status !== "deploying") {
        if (r.ok) message.success(r.message ?? "部署成功");
        else message.warning(r.message ?? "部署未完成");
        await load();
        return;
      }
      message.info(r.message ?? "部署已在后台开始，正在等待结果…");
      // 轮询：每 3s 拉一次，最多 ~10 分钟（SSH 装重组件耗时）。
      const deadline = Date.now() + 10 * 60 * 1000;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((res) => setTimeout(res, 3000));
        await load(); // 顺带刷新整表，让状态列实时更新
        let row: DependencyComponent;
        try {
          row = await api.getDependency(id);
        } catch {
          continue; // 单次拉取失败不终止轮询
        }
        if (row.deploy_status !== "deploying") {
          if (row.deploy_status === "connected" || row.deploy_status === "deployed") {
            message.success("SSH 远程安装完成并连通");
          } else {
            message.error(row.deploy_error || "部署失败，请查看状态详情");
          }
          break;
        }
        if (Date.now() > deadline) {
          message.warning("部署仍在进行，已停止等待。可稍后刷新查看最终状态");
          break;
        }
      }
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
      width: 280,
      render: (_: unknown, r: DependencyComponent) => (
        <Space size="small">
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          {r.deploy_log ? (
            <Button size="small" onClick={() => setLogRow(r)}>
              日志
            </Button>
          ) : null}
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
        forceRender
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
              onChange={(k: string) => {
                // 切组件类型：清掉上一组 conn_*/spec_*，避免同名字段残留上次填的值。
                clearDynamicFields();
                // 切组件后若当前部署方式不在该组件白名单内，回落到 external，
                // 避免留下一个后端会拒绝的非法组合（datahub/warehouse/llm 仅 external）。
                const allowed =
                  schema?.component_deploy_modes?.[k] ?? schema?.deploy_modes ?? [];
                const cur = form.getFieldValue("deploy_mode") as string | undefined;
                if (!cur || !allowed.includes(cur)) {
                  form.setFieldValue("deploy_mode", allowed[0] ?? "external");
                }
              }}
              options={(schema?.components ?? []).map((c) => ({
                value: c.key,
                label: c.label,
              }))}
            />
          </Form.Item>
          <Form.Item label="展示名" name="name">
            <Input placeholder="留空则用组件默认名" />
          </Form.Item>
          <Form.Item noStyle shouldUpdate>
            {({ getFieldValue }) => {
              const key = getFieldValue("key") as string | undefined;
              // 未列出的组件默认全支持；列出的（datahub/warehouse/llm）只回 external。
              const allowed = key
                ? schema?.component_deploy_modes?.[key] ?? schema?.deploy_modes ?? []
                : schema?.deploy_modes ?? [];
              const onlyExternal = allowed.length === 1 && allowed[0] === "external";
              return (
                <Form.Item
                  label="部署方式"
                  name="deploy_mode"
                  rules={[{ required: true }]}
                  extra={
                    onlyExternal
                      ? "该组件仅支持登记已有服务（external）：其裸机安装随发行版/集群差异极大，请在别处装好后在此填连接。"
                      : undefined
                  }
                >
                  <Select
                    options={allowed.map((m) => ({
                      value: m,
                      label: MODE_LABEL[m] ?? m,
                    }))}
                    onChange={() => clearDynamicFields()}
                  />
                </Form.Item>
              );
            }}
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
                  ? schema?.bare_metal_params?.[key] ?? []
                  : mode === "docker"
                    ? schema?.docker_params?.[key] ?? []
                    : schema?.deploy_spec_schemas?.["k8s"] ?? [];
              // bare_metal 下按认证方式隐藏无关的机密字段：
              //   password 认证 → 隐藏私钥/口令；key 认证 → 隐藏密码。
              const authMethod =
                (getFieldValue("spec_auth_method") as string | undefined) ?? "password";
              const hiddenForAuth = (name: string): boolean => {
                if (mode !== "bare_metal") return false;
                if (authMethod === "key") return name === "ssh_password";
                return name === "ssh_private_key" || name === "ssh_key_passphrase";
              };
              return (
                <>
                  <Divider>部署参数</Divider>
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={
                      mode === "bare_metal"
                        ? "SSH 远程安装：填目标机 IP + SSH 账号 + 密码/私钥，点「保存」后在列表点「部署」，由 ontoMeta 远程安装、启动服务并自动回收连接。安装可能持续数分钟，状态会自动刷新。"
                        : mode === "docker"
                          ? "填 compose 文件与服务端口，点「保存」后在列表点「部署」起容器并自动回收连接。"
                          : "填 K8s manifest（含同名 Service），点「保存」后在列表点「部署」apply 并回收端点。"
                    }
                  />
                  {specFields.map((f) =>
                    hiddenForAuth(f.name) ? null : (
                      <Form.Item
                        key={f.name}
                        label={f.name}
                        name={`spec_${f.name}`}
                        rules={
                          f.required && !hiddenForAuth(f.name)
                            ? [{ required: true, message: "必填" }]
                            : []
                        }
                      >
                        {f.name === "auth_method" ? (
                          <Select
                            options={[
                              { value: "password", label: "密码（password）" },
                              { value: "key", label: "私钥（key）" },
                            ]}
                          />
                        ) : f.type === "text" ? (
                          <Input.TextArea
                            rows={5}
                            placeholder={
                              editing && f.secret
                                ? "留空=保持不变"
                                : "粘贴 PEM 私钥（-----BEGIN ... KEY-----）"
                            }
                          />
                        ) : f.secret ? (
                          <Input.Password placeholder={editing ? "留空=保持不变" : ""} />
                        ) : f.type === "int" ? (
                          <InputNumber
                            style={{ width: "100%" }}
                            placeholder={
                              key === "sync_runner" && f.name === "port"
                                ? "留空自动选空闲端口"
                                : undefined
                            }
                          />
                        ) : (
                          <Input />
                        )}
                      </Form.Item>
                    ),
                  )}
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
          {/* Airflow 编排专有参数（存 deploy_spec.extra）。从 AirflowSettingsPanel 合并而来。 */}
          <Form.Item shouldUpdate noStyle>
            {({ getFieldValue }) =>
              getFieldValue("key") === "airflow" ? (
                <>
                  <Divider>编排参数</Divider>
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message="DAG 投递 / 执行通道 / DAG 形状与时序"
                    description="连接（endpoint/账密）与 sync-runner 连接已在上方与本面板 sync_runner 组件管理；这里只剩编排旋钮。sync-runner 地址/令牌在 sync_runner 组件的连接里。"
                  />
                  <Collapse
                    defaultActiveKey={["delivery", "channel", "shape"]}
                    items={[
                      {
                        key: "delivery",
                        label: "DAG 投递",
                        children: (
                          <>
                            <Form.Item label="投递方式" name="extra_dag_delivery_method">
                              <Radio.Group
                                optionType="button"
                                options={[
                                  { label: "local（写共享目录）", value: "local" },
                                  { label: "git（commit+push）", value: "git" },
                                ]}
                              />
                            </Form.Item>
                            <Form.Item
                              label="DAG 目录"
                              name="extra_dags_dir"
                              extra="local：必须是 Airflow 真正挂进容器的那个目录；git：本地 git 工作副本里的 dags 目录"
                            >
                              <Input placeholder="/opt/airflow/dags 在宿主机上的路径" />
                            </Form.Item>
                            <Form.Item
                              label="作业配置目录"
                              name="extra_jobs_dir"
                              extra="docker 通道的搬运作业配置落在这里并挂进搬运容器；runner 通道不用"
                            >
                              <Input placeholder="…/seatunnel/jobs" />
                            </Form.Item>
                            <Form.Item
                              noStyle
                              shouldUpdate={(prev, cur) =>
                                prev.extra_dag_delivery_method !== cur.extra_dag_delivery_method
                              }
                            >
                              {({ getFieldValue: gv }) =>
                                gv("extra_dag_delivery_method") === "git" ? (
                                  <>
                                    <Alert
                                      type="info"
                                      showIcon
                                      style={{ marginBottom: 12 }}
                                      message="git-sync 前置条件"
                                      description="DAG 目录须在一个已配好 remote 的 git 工作副本内，且本服务进程有推送凭据。Airflow 侧用 git-sync sidecar 拉取同一仓库。产物进 git 天然可 diff / review / 回滚。"
                                    />
                                    <Space align="start" wrap>
                                      <Form.Item label="remote 名称" name="extra_git_remote" extra="默认 origin">
                                        <Input placeholder="origin" style={{ width: 160 }} />
                                      </Form.Item>
                                      <Form.Item label="推送分支" name="extra_git_branch" extra="默认 main">
                                        <Input placeholder="main" style={{ width: 160 }} />
                                      </Form.Item>
                                    </Space>
                                    <Form.Item
                                      label="目录不是 git 仓库时自动 init"
                                      name="extra_git_auto_init"
                                      valuePropName="checked"
                                      extra="关闭时若 DAG 目录不是 git 仓库则报错"
                                    >
                                      <Switch />
                                    </Form.Item>
                                    <Space align="start" wrap>
                                      <Form.Item label="commit 作者名" name="extra_git_author" extra="留空则用 git 全局配置">
                                        <Input placeholder="ontoMeta" style={{ width: 200 }} />
                                      </Form.Item>
                                      <Form.Item label="commit 邮箱" name="extra_git_email" extra="留空则用 git 全局配置">
                                        <Input placeholder="ontometa@example.com" style={{ width: 240 }} />
                                      </Form.Item>
                                    </Space>
                                  </>
                                ) : null
                              }
                            </Form.Item>
                          </>
                        ),
                      },
                      {
                        key: "channel",
                        label: "执行通道",
                        children: (
                          <>
                            <Form.Item
                              label="通道"
                              name="extra_sync_channel"
                              extra="runner：Airflow 任务向常驻 sync-runner 发 HTTP（推荐）；docker：经 docker.sock 起搬运容器"
                            >
                              <Radio.Group
                                optionType="button"
                                options={[
                                  { label: "runner（常驻服务）", value: "runner" },
                                  { label: "docker（兄弟容器）", value: "docker" },
                                ]}
                              />
                            </Form.Item>
                            <Text type="secondary" style={{ display: "block", fontSize: 13, marginBottom: 12 }}>
                              sync-runner 的地址/令牌在 sync_runner 组件的连接里配。
                            </Text>
                            <Form.Item
                              label="搬运容器网络"
                              name="extra_docker_network"
                              extra="仅 docker 通道：搬运容器要能解析源库/数仓的容器名"
                            >
                              <Input placeholder="bridge" style={{ width: 260 }} />
                            </Form.Item>
                            <Form.Item
                              label="JDBC 驱动目录"
                              name="extra_drivers_dir"
                              extra="仅 docker 通道：驱动因授权不随镜像分发，逐个 jar 挂进搬运容器"
                            >
                              <Input placeholder="…/seatunnel/drivers" />
                            </Form.Item>
                            <Form.Item
                              label="搬运工具镜像覆盖"
                              name="extra_sync_tool_images"
                              extra="仅 docker 通道：工具名=镜像，逗号分隔"
                            >
                              <Input placeholder="datax=registry.internal/datax:3.0" />
                            </Form.Item>
                            <Form.Item
                              label="搬运工具"
                              name="extra_sync_tool"
                              extra="留空 = 自动；指定后物化一律用它"
                            >
                              <Select
                                style={{ width: 260 }}
                                options={[
                                  { value: "", label: "自动（推荐）" },
                                  { value: "seatunnel", label: "seatunnel" },
                                  { value: "datax", label: "datax（需先配镜像）" },
                                  { value: "flink", label: "flink" },
                                ]}
                              />
                            </Form.Item>
                          </>
                        ),
                      },
                      {
                        key: "shape",
                        label: "DAG 形状与时序",
                        children: (
                          <>
                            <Space align="start" wrap>
                              <Form.Item label="单 DAG 最大任务数" name="extra_max_tasks_per_dag" extra="超出按此拆成多个 DAG">
                                <InputNumber min={1} max={1000} style={{ width: 160 }} />
                              </Form.Item>
                              <Form.Item label="单 DAG 并发上限" name="extra_max_active_tasks_per_dag" extra="层内不再一次性全放开">
                                <InputNumber min={1} max={256} style={{ width: 160 }} />
                              </Form.Item>
                            </Space>
                            <Space align="start" wrap>
                              <Form.Item label="等 DAG 解析超时（秒）" name="extra_dag_parse_timeout" extra="要大于 Airflow 的 dag_dir_list_interval（默认 300s）">
                                <InputNumber min={0} max={3600} style={{ width: 200 }} />
                              </Form.Item>
                              <Form.Item label="自检探针超时（秒）" name="extra_preflight_sentinel_timeout" extra="提交前自检写一个 sentinel DAG，等它被解析到">
                                <InputNumber min={0} max={600} style={{ width: 200 }} />
                              </Form.Item>
                            </Space>
                            <Form.Item
                              label="全量装载走 staging + 原子切换"
                              name="extra_staging_swap"
                              valuePropName="checked"
                              extra="先搬进 staging 表、成功后再切换；关掉则直接写正式表"
                            >
                              <Switch />
                            </Form.Item>
                          </>
                        ),
                      },
                    ]}
                  />
                </>
              ) : null
            }
          </Form.Item>
        </Form>
      </Drawer>
      <Modal
        open={logRow !== null}
        title={logRow ? `${logRow.name} 部署日志` : ""}
        footer={null}
        width={720}
        onCancel={() => setLogRow(null)}
      >
        <pre
          style={{
            maxHeight: 480,
            overflow: "auto",
            background: "#0f1419",
            color: "#d9e2ec",
            padding: 12,
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
          }}
        >
          {logRow?.deploy_log}
        </pre>
      </Modal>
    </>
  );
}
