import { useEffect, useState } from "react";
import {
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
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

// 各数据源类型的连接表单形态与 DSN 组装方式。
// host 类：拼 SQLAlchemy URL（账号/密码分列填写、URL 编码）；file 类：填路径；
// cube：dsn 存 Cube API 地址；mock：内置样例无需连接。
type ConnField = "path" | "host" | "port" | "database" | "user" | "password" | "url";

interface ConnFormValues {
  name: string;
  kind: string;
  path?: string;
  host?: string;
  port?: number;
  database?: string;
  user?: string;
  password?: string;
  url?: string;
  raw_dsn?: string;
  mapping?: string;
}

const KIND_PROFILES: Record<
  string,
  {
    label: string;
    fields: ConnField[];
    scheme?: string;
    defaultPort?: number;
    build?: (v: ConnFormValues) => string | undefined;
  }
> = {
  postgres: {
    label: "PostgreSQL",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "postgresql+psycopg",
    defaultPort: 5432,
  },
  mysql: {
    label: "MySQL",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "mysql+pymysql",
    defaultPort: 3306,
  },
  hive: {
    label: "Hive（数仓）",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "hive",
    defaultPort: 10000,
  },
  doris: {
    label: "Doris（数仓）",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "mysql+pymysql", // Doris 走 MySQL 线协议
    defaultPort: 9030,
  },
  starrocks: {
    label: "StarRocks（数仓）",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "mysql+pymysql", // StarRocks 走 MySQL 线协议
    defaultPort: 9030,
  },
  clickhouse: {
    label: "ClickHouse（数仓）",
    fields: ["host", "port", "database", "user", "password"],
    scheme: "clickhouse+native",
    defaultPort: 9000,
  },
  sqlite: {
    label: "SQLite（文件）",
    fields: ["path"],
    build: (v) => (v.path?.trim() ? `sqlite:///${v.path.trim()}` : undefined),
  },
  duckdb: {
    label: "DuckDB（文件）",
    fields: ["path"],
    build: (v) => (v.path?.trim() ? `duckdb:///${v.path.trim()}` : undefined),
  },
  cube: {
    label: "Cube 语义层",
    fields: ["url"],
    build: (v) => v.url?.trim() || undefined,
  },
  mock: { label: "Mock（内置样例）", fields: [] },
};

const KIND_OPTIONS = Object.entries(KIND_PROFILES).map(([value, p]) => ({
  value,
  label: p.label,
}));

/** 按类型把结构化字段拼成 dsn_secret_ref；无连接信息时返回 undefined。 */
function buildConnDsn(kind: string, v: ConnFormValues): string | undefined {
  const p = KIND_PROFILES[kind];
  if (!p) return undefined;
  if (p.build) return p.build(v);
  if (!p.scheme || !v.host?.trim()) return undefined; // host 类无主机即视为未填
  const auth = v.user
    ? `${encodeURIComponent(v.user)}${v.password ? `:${encodeURIComponent(v.password)}` : ""}@`
    : "";
  const port = v.port ?? p.defaultPort;
  const hostPort = port ? `${v.host.trim()}:${port}` : v.host.trim();
  const db = v.database?.trim() ? `/${v.database.trim()}` : "";
  return `${p.scheme}://${auth}${hostPort}${db}`;
}

// 连接测试状态的统一呈现（列表、物化选源等处复用，避免各写各的文案）。
const STATUS_TAG: Record<string, { color: string; text: string }> = {
  ok: { color: "success", text: "已连通" },
  error: { color: "error", text: "连接失败" },
  untested: { color: "default", text: "未测试" },
};

/** 数据源连接状态标签。未知状态原样显示，不假装成已知态。 */
export function DataSourceStatusTag({ status }: { status: string }) {
  const s = STATUS_TAG[status];
  return (
    <Tag color={s?.color ?? "default"} style={{ marginInlineEnd: 0 }}>
      {s?.text ?? status}
    </Tag>
  );
}

export function DataSourcesModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Modal
      title="数据源管理"
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
      destroyOnClose
    >
      <DataSourcesPanel />
    </Modal>
  );
}

/** 数据源管理面板：登记 / 测试 / 删除可写连接。供物化落库与数据应用取数复用；
 *  可内嵌于设置页 Tab，或包在 {@link DataSourcesModal} 里从数据应用编辑器打开。
 *  新增 / 编辑通过弹框填写，连接按类型给结构化表单（账号密码分列）。 */
export function DataSourcesPanel() {
  const [sources, setSources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  // null=新增；非空=正在编辑该数据源 id（密码不回显，留空即保持原密码）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingPwSet, setEditingPwSet] = useState(false); // 编辑的源是否已配密码
  const [rawMode, setRawMode] = useState(false); // 高级：直接填连接串
  const [initialVals, setInitialVals] = useState<Partial<ConnFormValues>>({
    kind: "postgres",
  });
  const [form] = Form.useForm<ConnFormValues>();
  const kind = Form.useWatch("kind", form) ?? initialVals.kind ?? "postgres";
  const profile = KIND_PROFILES[kind] ?? KIND_PROFILES.postgres;
  const required = !editingId && !rawMode; // 新增且非高级模式时，连接字段必填

  const load = () => {
    setLoading(true);
    api
      .listDataSources()
      .then(setSources)
      .catch(() => setSources([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const openAdd = () => {
    setEditingId(null);
    setEditingPwSet(false);
    setRawMode(false);
    setInitialVals({ kind: "postgres" });
    setFormOpen(true);
  };

  const openEdit = (row: DataSource) => {
    setEditingId(row.id);
    setEditingPwSet(Boolean(row.password_set));
    setRawMode(false);
    // 回显非机密连接字段：host 类给主机/端口/库/账号，文件类给路径，cube 给地址。
    // 密码不回显，留空＝保持原密码（后端在 update 时沿用旧密码）。
    setInitialVals({
      name: row.name,
      kind: row.kind,
      host: row.host ?? undefined,
      port: row.port ?? undefined,
      database: row.database ?? undefined,
      user: row.username ?? undefined,
      path: row.path ?? undefined,
      url: row.url ?? undefined,
      mapping: row.mapping ? JSON.stringify(row.mapping) : "",
    });
    setFormOpen(true);
  };

  const closeForm = () => {
    setFormOpen(false);
    setEditingId(null);
    setEditingPwSet(false);
    setRawMode(false);
  };

  const handleSubmit = async () => {
    let values: ConnFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    let mapping: Record<string, unknown> | undefined;
    if (values.mapping?.trim()) {
      try {
        mapping = JSON.parse(values.mapping);
      } catch {
        message.error("映射必须是合法 JSON");
        return;
      }
    }
    const dsn = rawMode
      ? values.raw_dsn?.trim() || undefined
      : buildConnDsn(values.kind, values);
    if (!editingId && values.kind !== "mock" && !dsn) {
      message.error("请填写连接信息");
      return;
    }
    setSaving(true);
    try {
      if (editingId) {
        // 编辑：连接/映射留空即不改（后端 PATCH 为 exclude_unset）。
        const body: {
          name: string;
          kind: string;
          dsn_secret_ref?: string;
          mapping?: Record<string, unknown>;
        } = { name: values.name, kind: values.kind };
        if (dsn) body.dsn_secret_ref = dsn;
        if (mapping) body.mapping = mapping;
        await api.updateDataSource(editingId, body);
        message.success("已保存修改");
      } else {
        await api.createDataSource({
          name: values.name,
          kind: values.kind,
          dsn_secret_ref: dsn,
          mapping,
        });
        message.success("已添加数据源");
      }
      closeForm();
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
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

  // 按当前类型渲染结构化连接字段。
  const connFields = (
    <Space align="start" wrap>
      {profile.fields.includes("path") && (
        <Form.Item
          name="path"
          label="文件路径"
          rules={required ? [{ required: true, message: "请填写文件路径" }] : undefined}
          extra="绝对路径，如 /data/app.db"
        >
          <Input placeholder="/data/app.db" style={{ width: 320 }} />
        </Form.Item>
      )}
      {profile.fields.includes("url") && (
        <Form.Item
          name="url"
          label="Cube API 地址"
          rules={required ? [{ required: true, message: "请填写 Cube API 地址" }] : undefined}
          extra="如 https://cube.host/cubejs-api/v1"
        >
          <Input placeholder="https://cube.host/cubejs-api/v1" style={{ width: 320 }} />
        </Form.Item>
      )}
      {profile.fields.includes("host") && (
        <Form.Item
          name="host"
          label="主机"
          rules={required ? [{ required: true, message: "请填写主机" }] : undefined}
        >
          <Input placeholder="10.0.0.1 或 host" style={{ width: 160 }} />
        </Form.Item>
      )}
      {profile.fields.includes("port") && (
        <Form.Item name="port" label="端口" extra={`默认 ${profile.defaultPort ?? "—"}`}>
          <InputNumber
            style={{ width: 100 }}
            min={1}
            max={65535}
            placeholder={String(profile.defaultPort ?? "")}
          />
        </Form.Item>
      )}
      {profile.fields.includes("database") && (
        <Form.Item name="database" label="库名 / Schema">
          <Input placeholder="可选" style={{ width: 140 }} />
        </Form.Item>
      )}
      {profile.fields.includes("user") && (
        <Form.Item name="user" label="账号">
          <Input placeholder="数据库账号" style={{ width: 140 }} autoComplete="off" />
        </Form.Item>
      )}
      {profile.fields.includes("password") && (
        <Form.Item
          name="password"
          label="密码"
          extra={
            editingId && editingPwSet
              ? "已配置密码，留空则保持原密码不变"
              : undefined
          }
        >
          <Input.Password
            placeholder={editingId && editingPwSet ? "留空＝不修改密码" : "数据库密码"}
            style={{ width: 160 }}
            autoComplete="new-password"
          />
        </Form.Item>
      )}
      {profile.fields.length === 0 && !rawMode && (
        <span className="muted">Mock 数据源使用内置样例，无需填写连接。</span>
      )}
    </Space>
  );

  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<PlusOutlined />} type="primary" onClick={openAdd}>
          新增数据源
        </Button>
      </Space>

      <Modal
        title={editingId ? "编辑数据源" : "新增数据源"}
        open={formOpen}
        onOk={handleSubmit}
        onCancel={closeForm}
        okText={editingId ? "保存修改" : "保存"}
        confirmLoading={saving}
        destroyOnClose
        width={640}
      >
        <Form form={form} layout="vertical" initialValues={initialVals}>
          <Space align="start" wrap>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="如 生产库-只读" style={{ width: 220 }} />
            </Form.Item>
            <Form.Item name="kind" label="类型">
              <Select options={KIND_OPTIONS} style={{ width: 180 }} />
            </Form.Item>
          </Space>

          {editingId && (
            <div className="muted" style={{ marginBottom: 8 }}>
              连接字段已回显（密码除外）；密码留空＝保持原密码不变，改动其它字段不会清空密码。
            </div>
          )}

          {rawMode ? (
            <Form.Item
              name="raw_dsn"
              label="连接串（SQLAlchemy URL）"
              rules={required ? [{ required: true, message: "请填写连接串" }] : undefined}
              tooltip="如 postgresql+psycopg://user:pwd@host:5432/db；账号密码含特殊字符请自行编码"
            >
              <Input placeholder="postgresql+psycopg://user:pwd@host:5432/db" />
            </Form.Item>
          ) : (
            connFields
          )}

          {kind !== "mock" && (
            <Checkbox
              checked={rawMode}
              onChange={(e) => setRawMode(e.target.checked)}
              style={{ marginBottom: 12 }}
            >
              高级：直接填写连接串
            </Checkbox>
          )}

          <Form.Item
            name="mapping"
            label="物理映射（可选 JSON）"
            tooltip='本体名→物理名，如 {"tables":{"orders":"t_orders"},"columns":{"amount":"amt"}}'
            extra={editingId ? "留空＝保持原映射不变" : undefined}
          >
            <Input.TextArea rows={2} placeholder='{"tables":{},"columns":{}}' />
          </Form.Item>
        </Form>
      </Modal>

      <Table
        className="om-table"
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
            render: (s: string) => <DataSourceStatusTag status={s} />,
          },
          {
            title: "操作",
            key: "actions",
            render: (_: unknown, row: DataSource) => (
              <Space>
                <Button size="small" onClick={() => openEdit(row)}>
                  编辑
                </Button>
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
    </>
  );
}
