import { useEffect, useState } from "react";
import {
  Button,
  Col,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Row,
  Space,
  Switch,
  message,
} from "antd";
import { CheckCircleOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { DataSource, DorisWarehouseConfig } from "../types";

type DorisWarehouseFormValues = {
  name: string;
  enabled: boolean;
  host: string;
  query_port: number;
  default_catalog: string;
  default_database?: string;
  username?: string;
  password?: string;
  fenodes: string;
  benodes: string;
  connect_timeout_seconds: number;
  query_timeout_seconds: number;
  ssl_enabled: boolean;
};

function buildDorisDsn(values: DorisWarehouseFormValues): string {
  const username = values.username?.trim();
  const password = values.password;
  const auth = username
    ? `${encodeURIComponent(username)}${password ? `:${encodeURIComponent(password)}` : ""}@`
    : "";
  const database = values.default_database?.trim();
  return `mysql+pymysql://${auth}${values.host.trim()}:${values.query_port}${
    database ? `/${database}` : ""
  }`;
}

function optionalValue(value?: string): string | undefined {
  return value?.trim() || undefined;
}

/** 默认 Doris 数仓控制器。列表状态与抽屉表单共用这一份数据，避免出现两套 UI。 */
// eslint-disable-next-line react-refresh/only-export-components
export function useDorisWarehouseController() {
  const [form] = Form.useForm<DorisWarehouseFormValues>();
  const [source, setSource] = useState<DataSource | null>(null);
  const [config, setConfig] = useState<DorisWarehouseConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [sources, dorisConfig] = await Promise.all([
        api.listDataSources(),
        api.getDorisWarehouseConfig(),
      ]);
      const currentSource =
        sources.find((item) => item.id === dorisConfig?.warehouse_datasource_id) ??
        sources.find((item) => item.kind === "doris" && item.is_default_warehouse) ??
        sources.find((item) => item.kind === "doris") ??
        null;

      setSource(currentSource);
      setConfig(dorisConfig);
      form.resetFields();
      form.setFieldsValue({
        name: currentSource?.name ?? "默认 Doris 数仓",
        enabled: dorisConfig?.enabled ?? currentSource?.enabled ?? true,
        host: dorisConfig?.query_host ?? currentSource?.host ?? "",
        query_port: dorisConfig?.query_port ?? currentSource?.port ?? 9030,
        default_catalog: dorisConfig?.default_catalog ?? "internal",
        default_database: dorisConfig?.default_database ?? currentSource?.database ?? "",
        username: currentSource?.username ?? "",
        password: "",
        fenodes: dorisConfig?.fenodes?.join("\n") ?? "",
        benodes: dorisConfig?.benodes?.join("\n") ?? "",
        connect_timeout_seconds: dorisConfig?.connect_timeout_seconds ?? 10,
        query_timeout_seconds: dorisConfig?.query_timeout_seconds ?? 15,
        ssl_enabled: dorisConfig?.ssl_enabled ?? false,
      });
    } catch (err) {
      message.error(err instanceof Error ? err.message : "Doris 配置加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // Form 实例稳定，仅在面板首次挂载时加载。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSave = async () => {
    let values: DorisWarehouseFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    const fenodes = values.fenodes
      .split(/[\n,]/)
      .map((value) => value.trim())
      .filter(Boolean);
    const benodes = (values.benodes ?? "")
      .split(/[\n,]/)
      .map((value) => value.trim())
      .filter(Boolean);
    const dsn = buildDorisDsn(values);

    setSaving(true);
    try {
      const warehouseSource = source
        ? await api.updateDataSource(source.id, {
            name: values.name.trim(),
            kind: "doris",
            purpose: "warehouse",
            is_default_warehouse: true,
            enabled: values.enabled,
            dsn_secret_ref: dsn,
          })
        : await api.createDataSource({
            name: values.name.trim(),
            kind: "doris",
            purpose: "warehouse",
            is_default_warehouse: true,
            enabled: values.enabled,
            dsn_secret_ref: dsn,
          });

      await api.saveDorisWarehouseConfig({
        warehouse_datasource_id: warehouseSource.id,
        enabled: values.enabled,
        query_host: values.host.trim(),
        query_port: values.query_port,
        default_catalog: values.default_catalog.trim(),
        default_database: optionalValue(values.default_database),
        connect_timeout_seconds: values.connect_timeout_seconds,
        query_timeout_seconds: values.query_timeout_seconds,
        ssl_enabled: values.ssl_enabled,
        fenodes,
        benodes,
        reader_dsn_secret_ref: dsn,
      });

      message.success("Doris 数仓配置已保存");
      setDrawerOpen(false);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "Doris 配置保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!source) return;
    setTesting(true);
    try {
      const tested = await api.testDataSource(source.id);
      message.success(tested.status === "ok" ? "Doris 连接测试通过" : `连接状态：${tested.status}`);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "Doris 连接测试失败");
    } finally {
      setTesting(false);
    }
  };

  return {
    form,
    source,
    config,
    loading,
    saving,
    testing,
    drawerOpen,
    setDrawerOpen,
    load,
    handleSave,
    handleTest,
  };
}

export type DorisWarehouseController = ReturnType<typeof useDorisWarehouseController>;

/** 与依赖组件编辑一致，Doris 配置也只在右侧抽屉中展示。 */
export function DorisWarehouseDrawer({ controller }: { controller: DorisWarehouseController }) {
  const { form, source, config, saving, drawerOpen, setDrawerOpen, handleSave } = controller;

  return (
    <Drawer
      title={config ? "编辑 Doris 数仓" : "配置 Doris 数仓"}
      open={drawerOpen}
      onClose={() => setDrawerOpen(false)}
      width={520}
      forceRender
      extra={
        <Space>
          <Button onClick={() => setDrawerOpen(false)}>取消</Button>
          <Button
            type="primary"
            icon={<CheckCircleOutlined />}
            loading={saving}
            onClick={() => void handleSave()}
          >
            保存
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical">
        <Divider titlePlacement="left" plain style={{ marginTop: 0 }}>
          基本信息
        </Divider>
        <Row gutter={16} align="middle">
          <Col span={17}>
            <Form.Item
              name="name"
              label="配置名称"
              tooltip="用于在基础设施列表中识别这套 Doris 数仓配置，不影响 Doris 内部对象名称。"
              rules={[{ required: true, whitespace: true, message: "请输入配置名称" }]}
            >
              <Input placeholder="默认 Doris 数仓" />
            </Form.Item>
          </Col>
          <Col span={7}>
            <Form.Item
              name="enabled"
              label="启用"
              tooltip="关闭后系统不会把这套 Doris 作为可用数仓执行查询、物化或同步任务。"
              valuePropName="checked"
            >
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
          </Col>
        </Row>

        <Divider titlePlacement="left" plain>
          SQL 查询连接
        </Divider>
        <Row gutter={16}>
          <Col span={16}>
            <Form.Item
              name="host"
              label="FE SQL 主机"
              tooltip="Doris FE 提供 MySQL 协议服务的主机名或 IP，供 SQL 查询和建表使用；不要包含协议头或端口。"
              rules={[{ required: true, whitespace: true, message: "请输入 FE SQL 主机" }]}
            >
              <Input placeholder="doris-fe.example.com" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item
              name="query_port"
              label="SQL 端口"
              tooltip="Doris FE 的 MySQL 协议端口，默认是 9030。"
              rules={[{ required: true, message: "请输入 SQL 端口" }]}
            >
              <InputNumber min={1} max={65535} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="default_catalog"
              label="默认 Catalog"
              tooltip="未在 SQL 中显式指定 Catalog 时使用的 Doris Catalog；Doris 内部表通常填写 internal。"
              rules={[{ required: true, whitespace: true, message: "请输入默认 Catalog" }]}
            >
              <Input placeholder="internal" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="default_database"
              label="默认数据库"
              tooltip="系统默认创建和查询表的 Doris Database；执行任务时仍可由具体物理投影指定数据库。"
            >
              <Input placeholder="如 ontometa_dw" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="username"
              label="查询账号"
              tooltip="连接 Doris SQL 服务使用的账号。建议使用仅具备所需查询权限的独立账号。"
            >
              <Input placeholder="如 ontometa_reader" autoComplete="off" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="password"
              label="查询密码"
              tooltip={
                source?.password_set
                  ? "该 Doris 查询账号的密码已设置；不修改时请留空，系统会保留原密码。"
                  : "该 Doris 查询账号的密码。密码只写入后端受管配置，保存后不会回显。"
              }
            >
              <Input.Password
                placeholder={source?.password_set ? "已设置，留空保持不变" : "数据库密码"}
                autoComplete="new-password"
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item
          name="ssl_enabled"
          label="SSL 安全连接"
          tooltip="Doris SQL 端点已启用 TLS/SSL 时打开；服务端未配置 SSL 时请保持关闭。"
          valuePropName="checked"
        >
          <Switch checkedChildren="启用" unCheckedChildren="关闭" />
        </Form.Item>

        <Divider titlePlacement="left" plain>
          FE HTTP 节点
        </Divider>
        <Form.Item
          name="fenodes"
          label="节点地址"
          tooltip="Doris FE 的 HTTP 服务地址，供数据写入和节点发现使用；它不同于上面的 SQL 端口。"
          rules={[{ required: true, whitespace: true, message: "请填写至少一个 FE HTTP 节点" }]}
          extra="每行填写一个节点，也支持使用逗号分隔；通常使用 8030 端口。"
        >
          <Input.TextArea
            rows={3}
            placeholder={"fe-1:8030\nfe-2:8030"}
            autoSize={{ minRows: 2, maxRows: 5 }}
          />
        </Form.Item>

        <Form.Item
          name="benodes"
          label="BE HTTP 地址"
          tooltip="Doris BE 的 HTTP 地址。留空时由 FE 告诉 Flink BE 在哪；容器化/单机 Doris 的 BE 常向 FE 登记成 127.0.0.1，集群外的 Flink 照着连会失败，此时在这里填可路由的地址。"
          extra="选填。每行一个，通常是 8040 端口；不确定就留空。"
        >
          <Input.TextArea
            rows={2}
            placeholder={"be-1:8040\nbe-2:8040"}
            autoSize={{ minRows: 1, maxRows: 4 }}
          />
        </Form.Item>

        <Divider titlePlacement="left" plain>
          超时参数
        </Divider>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="connect_timeout_seconds"
              label="连接超时（秒）"
              tooltip="建立 Doris 网络连接最多等待的时间。网络不稳定时可适当增大。"
              rules={[{ required: true, message: "请输入连接超时" }]}
            >
              <InputNumber min={1} max={300} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="query_timeout_seconds"
              label="查询超时（秒）"
              tooltip="单次 Doris SQL 查询允许执行的最长时间，超时后系统会中止并报错。"
              rules={[{ required: true, message: "请输入查询超时" }]}
            >
              <InputNumber min={1} max={3600} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
      </Form>
    </Drawer>
  );
}
