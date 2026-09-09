/**
 * 基础设施组件面板：固定几样服务的连接登记 + 拨测。
 *
 * ontoMeta 不部署任何依赖，一律**连接已经跑着的服务**——所以这里没有新增、没有删除、
 * 没有部署方式：组件是固定的（Doris 数仓 / LLM / DataHub / Airflow），后端 ``ensure_components``
 * 保证每样都有一行，面板只负责填连接、拨测、启停。
 *
 * Doris 那行是**投影**：它的连接配在数据源里（DorisWarehousePanel），这里只是让四样
 * 基础设施在同一张表里看得全，编辑走它自己的抽屉。
 *
 * ERPNext 等业务源库不在此纳管（走「数据源」标签页）。
 */
import { useCallback, useEffect, useState } from "react";
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
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { EditOutlined, ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { ApiError, api } from "../api";
import type { DependencyComponent, DependencySchema } from "../types";
import { DorisWarehouseDrawer, useDorisWarehouseController } from "./DorisWarehousePanel";

const { Text } = Typography;

// Airflow 编排专有参数（存 settings.extra）。连接字段在 connection，这里只剩编排旋钮。
const AIRFLOW_EXTRA_FIELDS = [
  "dags_dir",
  "ssh_host",
  "ssh_port",
  "ssh_user",
  "max_tasks_per_dag",
  "max_active_tasks_per_dag",
  "dag_parse_timeout",
  "staging_swap",
  // Flink 执行引擎参数（搬运/计算经 Airflow BashOperator 提交 flink run）
  "flink_sql_runner_jar",
  "flink_sql_runner_class",
  "flink_bin",
  "flink_deploy_target",
  "flink_parallelism",
  "flink_yarn_queue",
  "flink_checkpoint_dir",
  "flink_rest_endpoint",
] as const;

// 连接字段的中文名：schema 回的是后端字段名，直接当 label 会让表单变成一串英文。
const FIELD_LABEL: Record<string, string> = {
  endpoint: "服务地址",
  username: "用户名",
  password: "密码",
  base_url: "服务地址（后端访问）",
  public_base_url: "对外地址（浏览器访问）",
};

// 由组件专有分节自己渲染的连接分组：airflow 的 SSH 密码要和 SSH 主机/端口/目录放在
// 一起才读得懂，故不在通用「连接信息」段再渲染一遍——那正是「SSH 密码填两遍」的由来。
const CONN_GROUPS_RENDERED_ELSEWHERE: Record<string, string[]> = {
  airflow: ["ssh"],
};

const STATUS_COLOR: Record<string, string> = {
  connected: "success",
  failed: "error",
  unknown: "default",
};
const STATUS_LABEL: Record<string, string> = {
  connected: "已连接",
  failed: "连接失败",
  unknown: "未拨测",
};

// Doris 那行不是 dependency_components 里的行，用一个固定 id 占位（它不参与后端 CRUD）。
const DORIS_ROW_ID = "__doris_warehouse__";

export function DependencyPanel() {
  const [schema, setSchema] = useState<DependencySchema | null>(null);
  const [rows, setRows] = useState<DependencyComponent[]>([]);
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<DependencyComponent | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const doris = useDorisWarehouseController();

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

  const openEdit = (row: DependencyComponent) => {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue({ name: row.name, enabled: row.enabled });
    (schema?.connection_schemas[row.key] ?? []).forEach((f) => {
      form.setFieldValue(`conn_${f.name}`, row.connection[f.name] ?? "");
    });
    // Airflow 编排参数回填（存 settings.extra）
    const extra = (row.settings?.extra ?? {}) as Record<string, unknown>;
    AIRFLOW_EXTRA_FIELDS.forEach((f) => {
      if (f in extra) form.setFieldValue(`extra_${f}`, extra[f]);
    });
    setDrawerOpen(true);
  };

  const handleSave = async (opts?: { keepOpen?: boolean }): Promise<boolean> => {
    if (!editing) return false;
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return false;
    }
    const conn: Record<string, unknown> = {};
    (schema?.connection_schemas[editing.key] ?? []).forEach((f) => {
      conn[f.name] = values[`conn_${f.name}`] ?? null;
    });
    const body: Parameters<typeof api.updateDependency>[1] = {
      name: values.name as string | undefined,
      enabled: values.enabled as boolean,
      connection: conn,
    };
    // Airflow 编排参数 → settings.extra
    if (editing.key === "airflow") {
      const extra: Record<string, unknown> = {};
      AIRFLOW_EXTRA_FIELDS.forEach((f) => {
        const v = values[`extra_${f}`];
        if (v !== undefined && v !== null && v !== "") extra[f] = v;
      });
      body.settings = { extra };
    }
    setSaving(true);
    try {
      await api.updateDependency(editing.id, body);
      message.success("已保存");
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
      // 机密现已明文回显，可与预填值直接比对（之前是掩码比不了，只能当非空即改过）。
      if (norm(v[`conn_${f.name}`]) !== norm(editing.connection[f.name])) {
        out.push(FIELD_LABEL[f.name] ?? f.name);
      }
    });
    const extra = (editing.settings?.extra ?? {}) as Record<string, unknown>;
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

  // 某一条连接的「只测这条」按钮，放在该连接的配置分节里。拨测读的是**已保存**的配置。
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
    ) : null;

  const dorisRow: DependencyComponent = {
    id: DORIS_ROW_ID,
    key: "doris",
    name: doris.source?.name ?? "默认 Doris 数仓",
    settings: {},
    connection_status:
      doris.config && doris.source?.status === "ok"
        ? "connected"
        : doris.config && doris.source?.status === "error"
          ? "failed"
          : "unknown",
    connection_error: doris.config
      ? `${doris.config.query_host ?? "未填写主机"}:${doris.config.query_port} · ${
          doris.config.fenodes.length
        } 个 FE HTTP 节点`
      : "尚未配置 Doris FE SQL/HTTP 端点和连接凭据",
    connection: {},
    enabled: doris.config?.enabled ?? false,
    created_at: doris.config?.created_at ?? "",
    updated_at: doris.config?.updated_at ?? "",
  };
  const infrastructureRows = [dorisRow, ...rows];

  const columns: ColumnsType<DependencyComponent> = [
    {
      title: "组件",
      dataIndex: "key",
      width: 200,
      render: (k: string) => {
        const meta = schema?.components.find((c) => c.key === k);
        return <Tag>{k === "doris" ? "Apache Doris（统一数仓）" : (meta?.label ?? k)}</Tag>;
      },
    },
    {
      title: "名称",
      dataIndex: "name",
      width: 160,
    },
    {
      title: "状态",
      dataIndex: "connection_status",
      width: 220,
      render: (s: string, r) => {
        // 多条连接的组件（airflow = 调度 API + DAG 投递）逐条显示最近一次拨测结果：
        // 总状态只说"失败"，说不出是哪条断了。
        const groups = schema?.connection_groups?.[r.key] ?? [];
        const ledger = (r.settings?._probe ?? {}) as Record<
          string,
          { ok: boolean; message: string }
        >;
        return (
          <Space direction="vertical" size={2} align="start">
            <Tooltip title={r.connection_error}>
              <Tag color={STATUS_COLOR[s] ?? "default"}>{STATUS_LABEL[s] ?? s}</Tag>
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
      title: "启用",
      dataIndex: "enabled",
      width: 90,
      render: (v: boolean) => (v ? <Tag color="blue">已启用</Tag> : <Tag>已停用</Tag>),
    },
    {
      title: "操作",
      width: 180,
      render: (_: unknown, r: DependencyComponent) =>
        r.key === "doris" ? (
          <Space size="small">
            <Button size="small" icon={<EditOutlined />} onClick={() => doris.setDrawerOpen(true)}>
              编辑连接
            </Button>
            <Button
              size="small"
              disabled={!doris.source}
              loading={doris.testing}
              onClick={() => void doris.handleTest()}
            >
              拨测
            </Button>
          </Space>
        ) : (
          <Space size="small">
            <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
              编辑连接
            </Button>
            <Button size="small" onClick={() => void handleProbe(r.id)} loading={probing[r.id]}>
              拨测
            </Button>
          </Space>
        ),
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
        <Text type="secondary">共 {infrastructureRows.length} 个组件</Text>
        <Button icon={<ReloadOutlined />} onClick={() => void Promise.all([load(), doris.load()])}>
          刷新
        </Button>
      </Space>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={infrastructureRows}
        loading={loading || doris.loading}
        pagination={false}
        className="om-table"
        size="middle"
        scroll={{ x: "max-content" }}
      />
      <DorisWarehouseDrawer controller={doris} />
      <Drawer
        title={editing ? `${editing.name} · 连接配置` : "连接配置"}
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
          <Form.Item label="展示名" name="name">
            <Input placeholder="留空则用组件默认名" />
          </Form.Item>
          <Form.Item
            label="启用"
            name="enabled"
            valuePropName="checked"
            extra="停用后上层功能不再使用该服务"
          >
            <Switch />
          </Form.Item>
          {editing
            ? (() => {
                const key = editing.key;
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
                          extra={
                            f.secret && editing.connection[f.name]
                              ? "已回显，清空将保持原值不变"
                              : undefined
                          }
                        >
                          {f.secret ? (
                            <Input.Password placeholder="清空将保持原值不变" />
                          ) : (
                            <Input />
                          )}
                        </Form.Item>
                      ))
                    )}
                  </>
                );
              })()
            : null}
          {/* Airflow 编排专有参数（存 settings.extra） */}
          {editing?.key === "airflow" ? (
            <>
              <Divider>编排参数</Divider>
              <Collapse
                defaultActiveKey={["delivery", "shape"]}
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
                          <Form.Item label="SSH 主机" name="extra_ssh_host" extra="Airflow 所在主机">
                            <Input placeholder="airflow-host" style={{ width: 200 }} />
                          </Form.Item>
                          <Form.Item label="SSH 端口" name="extra_ssh_port" extra="默认 22">
                            <InputNumber min={1} max={65535} style={{ width: 110 }} />
                          </Form.Item>
                          <Form.Item label="SSH 用户名" name="extra_ssh_user" extra="留空用 ssh 默认">
                            <Input placeholder="deploy" style={{ width: 160 }} />
                          </Form.Item>
                        </Space>
                        <Form.Item
                          label="SSH 密码"
                          name="conn_ssh_password"
                          extra="留空 = 用 ontoMeta 主机的默认 SSH 身份/agent（要指定私钥就写进该机 ~/.ssh/config）；填了则用密码认证，需装 sshpass"
                        >
                          <Input.Password placeholder="清空将保持原值不变" />
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
                            <Input
                              placeholder="flink（在 PATH 上）或绝对路径"
                              style={{ width: 260 }}
                            />
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
                          <Form.Item label="SqlRunner main class" name="extra_flink_sql_runner_class">
                            <Input placeholder="com.ontometa.flink.SqlRunner" style={{ width: 280 }} />
                          </Form.Item>
                        </Space>
                        <Form.Item
                          label="Checkpoint 目录"
                          name="extra_flink_checkpoint_dir"
                          extra="CDC 流式作业持久化读位点用；incremental 是有界水位 batch，不依赖 checkpoint"
                        >
                          <Input placeholder="file:///var/flink/checkpoints 或 hdfs://…" />
                        </Form.Item>
                        <Form.Item
                          label="Flink REST Endpoint"
                          name="extra_flink_rest_endpoint"
                          extra="CDC 健康检查使用，如 http://flink-jobmanager:8081；配置落数据库"
                        >
                          <Input placeholder="http://flink-jobmanager:8081" />
                        </Form.Item>
                      </>
                    ),
                  },
                ]}
              />
            </>
          ) : null}
        </Form>
      </Drawer>
    </>
  );
}
