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
// 连接字段已归本面板的 connection，这里只剩编排旋钮。
const AIRFLOW_EXTRA_FIELDS = [
  "dags_dir",
  "ssh_host",
  "ssh_port",
  "ssh_user",
  "max_tasks_per_dag",
  "max_active_tasks_per_dag",
  "dag_parse_timeout",
  "preflight_sentinel_timeout",
  "staging_swap",
  // Flink 执行引擎参数（搬运/计算经 Airflow BashOperator 提交 flink run）
  "flink_sql_runner_jar",
  "flink_sql_runner_class",
  "flink_bin",
  "flink_deploy_target",
  "flink_parallelism",
  "flink_yarn_queue",
  "flink_checkpoint_dir",
] as const;

// 连接字段的中文名：schema 回的是后端字段名，直接当 label 会让表单变成一串英文。
const FIELD_LABEL: Record<string, string> = {
  endpoint: "服务地址",
  username: "用户名",
  password: "密码",
};

// 由组件专有分节自己渲染的连接分组：airflow 的 SSH 密码要和 SSH 主机/端口/目录放在
// 一起才读得懂，故不在通用「连接信息」段再渲染一遍——那正是「SSH 密码填两遍」的由来。
const CONN_GROUPS_RENDERED_ELSEWHERE: Record<string, string[]> = {
  airflow: ["ssh"],
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
  const [logRow, setLogRow] = useState<DependencyComponent | null>(null);

  // 组件类型/部署方式切换时，上一组 conn_*/spec_* 字段靠 antd preserve 留在 store 里，
  // 同名字段（如 conn_endpoint、conn_token 在 airflow/datahub 都存在）会把
  // 上次填的值带过来——「新增时表单残留上次内容」即此。切换即清空动态字段。
  const dynamicFieldNames = useMemo(() => {
    if (!schema) return [];
    const names: string[] = [];
    Object.values(schema.connection_schemas).forEach((fs) =>
      fs.forEach((f) => names.push(`conn_${f.name}`)),
    );
    [schema.bare_metal_params, schema.docker_params].forEach((m) =>
      Object.values(m ?? {}).forEach((fs) => fs.forEach((f) => names.push(`spec_${f.name}`))),
    );
    (schema.deploy_spec_schemas?.k8s ?? []).forEach((f) => names.push(`spec_${f.name}`));
    return names;
  }, [schema]);
  const clearDynamicFields = useCallback(() => {
    if (dynamicFieldNames.length) form.resetFields(dynamicFieldNames);
  }, [form, dynamicFieldNames]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sch, list] = await Promise.all([api.getDependencySchema(), api.listDependencies()]);
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

  const handleSave = async (opts?: { keepOpen?: boolean }): Promise<boolean> => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return false;
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
        ? (schema?.bare_metal_params?.[key] ?? [])
        : mode === "docker"
          ? (schema?.docker_params?.[key] ?? [])
          : mode === "k8s"
            ? (schema?.deploy_spec_schemas?.["k8s"] ?? [])
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
      if (!opts?.keepOpen) setDrawerOpen(false);
      await load();
      return true;
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : "保存失败");
      return false;
    } finally {
      setSaving(false);
    }
  };

  // 表单里改了、但还没保存的字段。拨测走的是**库里已保存**的配置（探针在后端跑，
  // 读的是组件行），不提示的话就会出现「我明明改了目录，报错里还是旧路径」——
  // 那不是路径写死了，是拨测压根没看见你刚填的值。
  const norm = (v: unknown) => (v === undefined || v === null ? "" : String(v));
  const unsavedFields = (): string[] => {
    if (!editing) return [];
    const v = form.getFieldsValue() as Record<string, unknown>;
    const out: string[] = [];
    (schema?.connection_schemas[editing.key] ?? []).forEach((f) => {
      const cur = v[`conn_${f.name}`];
      // 机密回显的是掩码，比不了原值：输入框里有内容就当改过
      if (f.secret) {
        if (cur) out.push(FIELD_LABEL[f.name] ?? f.name);
        return;
      }
      if (norm(cur) !== norm(editing.connection[f.name])) {
        out.push(FIELD_LABEL[f.name] ?? f.name);
      }
    });
    const extra = (editing.deploy_spec?.extra ?? {}) as Record<string, unknown>;
    AIRFLOW_EXTRA_FIELDS.forEach((f) => {
      if (norm(v[`extra_${f}`]) !== norm(extra[f])) out.push(f);
    });
    return out;
  };

  // target 只测组件的其中一条连接（如 airflow 的 api / ssh），省略则全测。
  // 一个组件的几条连接互不相干，得能分开测——否则 SSH 没配好会把「调度 API 其实是
  // 通的」也一起盖成红叉，用户根本看不出该修哪条。
  const handleProbe = async (id: string, target?: string) => {
    const busy = target ? `${id}:${target}` : id;
    setProbing((p) => ({ ...p, [busy]: true }));
    try {
      const r = await api.probeDependency(id, target);
      if (r.ok) message.success(`拨测通过${r.latency_ms ? ` · ${r.latency_ms}ms` : ""}`);
      else message.error(r.message);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "拨测失败");
    } finally {
      setProbing((p) => ({ ...p, [busy]: false }));
    }
  };

  // 某一条连接的「只测这条」按钮，放在该连接的配置分节里。拨测读的是**已保存**的
  // 配置，故新增未保存时不给按钮，免得测的和刚填的不是一回事。
  const probeSaved = (id: string, target: string) => {
    const dirty = unsavedFields();
    if (!dirty.length) {
      void handleProbe(id, target);
      return;
    }
    Modal.confirm({
      title: "表单有未保存的改动",
      content: `拨测读的是已保存的配置，不是你刚填的内容（${dirty.join("、")}）。先保存再测吗？`,
      okText: "保存并测试",
      cancelText: "取消",
      onOk: async () => {
        if (await handleSave({ keepOpen: true })) await handleProbe(id, target);
      },
    });
  };

  const probeButton = (target: string, label: string) =>
    editing ? (
      <Button
        size="small"
        style={{ marginBottom: 12 }}
        loading={probing[`${editing.id}:${target}`]}
        onClick={() => probeSaved(editing.id, target)}
      >
        测试「{label}」
      </Button>
    ) : (
      <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
        保存后可单独测试这条连接
      </Text>
    );

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
      width: 190,
      render: (s: string, r) => {
        // external 模式的 not_deployed 显示"未拨测"而非"未部署"
        const label = s === "not_deployed" && r.deploy_mode === "external"
          ? "未拨测"
          : (STATUS_LABEL[s] ?? s);
        // 多条连接的组件（airflow = 调度 API + DAG 投递）逐条显示最近一次拨测结果：
        // 总状态只说"失败"，说不出是哪条断了。
        const groups = schema?.connection_groups?.[r.key] ?? [];
        const ledger = (r.deploy_spec?._probe ?? {}) as Record<
          string,
          { ok: boolean; message: string }
        >;
        return (
          <Space direction="vertical" size={2} align="start">
            <Tooltip title={r.deploy_error}>
              <Tag color={STATUS_COLOR[s] ?? "default"}>{label}</Tag>
            </Tooltip>
            {groups.length > 1
              ? groups.map((g) => {
                  const part = ledger[g.id];
                  return (
                    <Tooltip key={g.id} title={part?.message ?? "尚未拨测"}>
                      <Tag color={part ? (part.ok ? "success" : "error") : "default"}>
                        {g.label} {part ? (part.ok ? "✓" : "✗") : "—"}
                      </Tag>
                    </Tooltip>
                  );
                })
              : null}
          </Space>
        );
      },
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
          <Button size="small" onClick={() => void handleProbe(r.id)} loading={probing[r.id]}>
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
      .filter((c) => c.key !== "airflow")
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
        className="om-table"
        size="middle"
        scroll={{ x: 'max-content' }}
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
                // 避免留下一个后端会拒绝的非法组合（datahub/llm 仅 external）。
                const allowed = schema?.component_deploy_modes?.[k] ?? schema?.deploy_modes ?? [];
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
              // 未列出的组件默认全支持；列出的（datahub/llm）只回 external。
              const allowed = key
                ? (schema?.component_deploy_modes?.[key] ?? schema?.deploy_modes ?? [])
                : (schema?.deploy_modes ?? []);
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
                const groups = schema?.connection_groups?.[key] ?? [];
                const movedOut = CONN_GROUPS_RENDERED_ELSEWHERE[key] ?? [];
                // 已由专有分节渲染的字段在这里跳过，否则同一个字段填两遍。
                const skip = new Set(
                  movedOut.flatMap((gid) => groups.find((g) => g.id === gid)?.fields ?? []),
                );
                const fields = (schema?.connection_schemas[key] ?? []).filter(
                  (f) => !skip.has(f.name),
                );
                // 多连接组件：这一段就是其中一条，标题用它的名字并给「只测这条」按钮。
                const here = groups.filter((g) => !movedOut.includes(g.id));
                const solo = groups.length > 1 && here.length === 1 ? here[0] : null;
                return (
                  <>
                    <Divider>{solo ? `${solo.label} 连接` : "连接信息"}</Divider>
                    {solo ? probeButton(solo.id, solo.label) : null}
                    {fields.length === 0 ? (
                      <Text type="secondary">该组件暂无连接字段</Text>
                    ) : (
                      fields.map((f) => (
                        <Form.Item
                          key={f.name}
                          label={FIELD_LABEL[f.name] ?? f.name}
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
                  ? (schema?.bare_metal_params?.[key] ?? [])
                  : mode === "docker"
                    ? (schema?.docker_params?.[key] ?? [])
                    : (schema?.deploy_spec_schemas?.["k8s"] ?? []);
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
                  <Collapse
                    defaultActiveKey={["delivery", "channel", "shape"]}
                    items={[
                      {
                        key: "delivery",
                        label: "DAG 投递（SSH）",
                        children: (
                          <>
                            {probeButton("ssh", "DAG 投递")}
                            <Form.Item
                              label="DAG 目录"
                              name="extra_dags_dir"
                              extra="Airflow 主机上、它已在扫描的 DAG 目录（容器部署填宿主机上的挂载源）"
                            >
                              <Input placeholder="~/airflow/dags" />
                            </Form.Item>
                            <Space align="start" wrap>
                              <Form.Item
                                label="SSH 主机"
                                name="extra_ssh_host"
                                extra="Airflow 所在主机"
                              >
                                <Input placeholder="airflow-host" style={{ width: 200 }} />
                              </Form.Item>
                              <Form.Item label="SSH 端口" name="extra_ssh_port" extra="默认 22">
                                <InputNumber min={1} max={65535} style={{ width: 110 }} />
                              </Form.Item>
                              <Form.Item
                                label="SSH 用户名"
                                name="extra_ssh_user"
                                extra="留空用 ssh 默认"
                              >
                                <Input placeholder="deploy" style={{ width: 160 }} />
                              </Form.Item>
                            </Space>
                            <Form.Item
                              label="SSH 密码"
                              name="conn_ssh_password"
                              extra="留空 = 用 ontoMeta 主机的默认 SSH 身份/agent（要指定私钥就写进该机 ~/.ssh/config）；填了则用密码认证，需装 sshpass"
                            >
                              <Input.Password placeholder={editing ? "留空=保持不变" : ""} />
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
                              <Form.Item
                                label="单 DAG 最大任务数"
                                name="extra_max_tasks_per_dag"
                                extra="超出按此拆成多个 DAG"
                              >
                                <InputNumber min={1} max={1000} style={{ width: 160 }} />
                              </Form.Item>
                              <Form.Item
                                label="单 DAG 并发上限"
                                name="extra_max_active_tasks_per_dag"
                                extra="层内不再一次性全放开"
                              >
                                <InputNumber min={1} max={256} style={{ width: 160 }} />
                              </Form.Item>
                            </Space>
                            <Space align="start" wrap>
                              <Form.Item
                                label="等 DAG 解析超时（秒）"
                                name="extra_dag_parse_timeout"
                                extra="要大于 Airflow 的 dag_dir_list_interval（默认 300s）"
                              >
                                <InputNumber min={0} max={3600} style={{ width: 200 }} />
                              </Form.Item>
                              <Form.Item
                                label="自检探针超时（秒）"
                                name="extra_preflight_sentinel_timeout"
                                extra="提交前自检写一个 sentinel DAG，等它被解析到"
                              >
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
                            <Divider plain>Flink 执行引擎（默认值）</Divider>
                            <Alert
                              type="info"
                              showIcon
                              style={{ marginBottom: 16 }}
                              message="这里配的是默认值"
                              description="并行度 / YARN 队列 / 提交目标 / Checkpoint 目录可在每个任务的「高级：Flink 执行参数」里单独覆盖，任务留空才用这里的值。SqlRunner JAR、main class、flink 命令路径是部署事实，只在这里配。"
                            />
                            <Form.Item
                              label="Flink SqlRunner JAR"
                              name="extra_flink_sql_runner_jar"
                              extra="通用 SqlRunner JAR 路径；留空则搬运/计算只产出 SQL、不执行"
                            >
                              <Input placeholder="/opt/flink/sql-runner.jar" />
                            </Form.Item>
                            <Space align="start" wrap>
                              <Form.Item label="flink 命令路径" name="extra_flink_bin">
                                <Input placeholder="flink（在 PATH 上）或绝对路径" style={{ width: 260 }} />
                              </Form.Item>
                              <Form.Item label="提交目标" name="extra_flink_deploy_target">
                                <Select
                                  style={{ width: 180 }}
                                  options={[
                                    { value: "yarn-per-job", label: "yarn-per-job" },
                                    { value: "yarn-session", label: "yarn-session" },
                                    { value: "remote", label: "remote" },
                                    { value: "local", label: "local" },
                                  ]}
                                  allowClear
                                />
                              </Form.Item>
                              <Form.Item label="并行度" name="extra_flink_parallelism">
                                <InputNumber min={1} max={512} style={{ width: 120 }} />
                              </Form.Item>
                            </Space>
                            <Space align="start" wrap>
                              <Form.Item label="YARN 队列" name="extra_flink_yarn_queue">
                                <Input placeholder="default" style={{ width: 200 }} />
                              </Form.Item>
                              <Form.Item
                                label="SqlRunner main class"
                                name="extra_flink_sql_runner_class"
                              >
                                <Input placeholder="com.ontometa.flink.SqlRunner" style={{ width: 280 }} />
                              </Form.Item>
                            </Space>
                            <Form.Item
                              label="Checkpoint 目录"
                              name="extra_flink_checkpoint_dir"
                              extra="增量/CDC 流式作业持久化读位点用；file://… 本地 或 hdfs://… 集群。全量搬运不需要"
                            >
                              <Input placeholder="file:///var/flink/checkpoints 或 hdfs://…" />
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
