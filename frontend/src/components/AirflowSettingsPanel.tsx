/** Airflow 编排配置面板（M10）。
 *
 * 物化改由调度器执行后，「怎么连 Airflow、DAG 往哪投递」需要一处可管理的配置。
 * 目标库与源库的凭据**不在这里**——那些是 Airflow 侧的 Connection，
 * ontoMeta 产出的 DAG 里只出现 conn_id。
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Form, Input, Space, Switch, Tag, message } from "antd";
import { ApiOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { AirflowSettings } from "../types";

interface FormValues {
  endpoint: string;
  username?: string;
  password?: string;
  api_version: string;
  dags_dir: string;
  jobs_dir: string;
  warehouse_conn_id: string;
  seatunnel_image: string;
  enabled: boolean;
}

export function AirflowSettingsPanel() {
  const [form] = Form.useForm<FormValues>();
  const [settings, setSettings] = useState<AirflowSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const load = useCallback(() => {
    api
      .getAirflowSettings()
      .then((s) => {
        setSettings(s);
        form.setFieldsValue({
          endpoint: s.endpoint,
          username: s.username ?? undefined,
          password: undefined, // 密文不回显，留空 = 保持不变
          api_version: s.api_version,
          dags_dir: s.dags_dir,
          jobs_dir: s.jobs_dir,
          warehouse_conn_id: s.warehouse_conn_id,
          seatunnel_image: s.seatunnel_image,
          enabled: s.enabled,
        });
      })
      .catch((err: unknown) =>
        message.error(err instanceof Error ? err.message : "读取 Airflow 配置失败"),
      );
  }, [form]);

  useEffect(load, [load]);

  const save = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      const updated = await api.updateAirflowSettings({
        ...values,
        username: values.username?.trim() || null,
        password: values.password?.trim() || undefined,
      });
      setSettings(updated);
      message.success("Airflow 配置已保存");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    try {
      await api.testAirflowConnection();
      message.success("Airflow 连接正常");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "连接失败");
    } finally {
      setTesting(false);
    }
  };

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <span>
            物化执行方式：
            {settings?.available ? (
              <Tag color="success" style={{ marginLeft: 6 }}>
                调度执行（Airflow）
              </Tag>
            ) : (
              <Tag color="warning" style={{ marginLeft: 6 }}>
                直连落库（开发模式）
              </Tag>
            )}
          </span>
        }
        description={
          settings?.available
            ? "物化将产出建表 DDL + 搬运作业 + DAG 交给 Airflow 执行，血缘由 DataHub 插件自动上报。"
            : "未启用或投递目录未配全时，物化回落到 ontoMeta 直连目标库执行——仅适合本地验证，跨源搬运走不通。"
        }
      />
      <Form form={form} layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item
          label="启用调度执行"
          name="enabled"
          valuePropName="checked"
          extra="关闭即回到直连落库（开发模式）"
        >
          <Switch />
        </Form.Item>
        <Form.Item
          label="Airflow 地址"
          name="endpoint"
          rules={[{ required: true, message: "请输入 Airflow 地址" }]}
          extra="例如 http://localhost:8081"
        >
          <Input prefix={<ApiOutlined />} placeholder="http://localhost:8081" />
        </Form.Item>
        <Space align="start" wrap>
          <Form.Item label="账号" name="username">
            <Input placeholder="admin" style={{ width: 200 }} autoComplete="off" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            extra={
              settings?.password_set
                ? `已配置：${settings.password_hint ?? "****"}，留空则保持不变`
                : undefined
            }
          >
            <Input.Password
              placeholder="Airflow 密码"
              style={{ width: 220 }}
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item
            label="REST 版本"
            name="api_version"
            extra="Airflow 2.x=v1，3.x=v2"
          >
            <Input placeholder="v1" style={{ width: 100 }} />
          </Form.Item>
        </Space>
        <Form.Item
          label="DAG 投递目录"
          name="dags_dir"
          extra="ontoMeta 生成的 DAG 与边车 JSON 落盘处，需被 Airflow 加载（本地为挂载卷，生产可为 git-sync 工作区）"
        >
          <Input placeholder="/path/to/docker/orchestration/dags" />
        </Form.Item>
        <Form.Item
          label="作业配置目录"
          name="jobs_dir"
          extra="SeaTunnel 作业配置落盘处，需被搬运任务容器挂载"
        >
          <Input placeholder="/path/to/docker/orchestration/seatunnel/jobs" />
        </Form.Item>
        <Space align="start" wrap>
          <Form.Item
            label="目标库 Connection id"
            name="warehouse_conn_id"
            rules={[{ required: true, message: "请输入 conn_id" }]}
            extra="DAG 里建表任务用它连目标数仓；凭据存在 Airflow 侧"
          >
            <Input placeholder="warehouse_default" style={{ width: 240 }} />
          </Form.Item>
          <Form.Item
            label="SeaTunnel 镜像"
            name="seatunnel_image"
            rules={[{ required: true, message: "请输入镜像" }]}
          >
            <Input placeholder="apache/seatunnel:2.3.11" style={{ width: 280 }} />
          </Form.Item>
        </Space>
        <Form.Item>
          <Space>
            <Button type="primary" onClick={() => void save()} loading={saving}>
              保存
            </Button>
            <Button
              icon={<ThunderboltOutlined />}
              onClick={() => void test()}
              loading={testing}
            >
              测试连接
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </>
  );
}
