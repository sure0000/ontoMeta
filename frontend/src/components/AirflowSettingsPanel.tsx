/** Airflow 编排连接配置面板（M10）。
 *
 * 只管「怎么连 Airflow」（地址 / 鉴权 / REST 版本）。搬运工具与同步策略由物化弹窗逐次选；
 * 目标仓/源库的凭据都是 Airflow 侧的 Connection，ontoMeta 产出的 DAG 里只出现 conn_id；
 * DAG/作业投递目录属部署基础设施，由后端环境变量给默认。
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
            物化执行：
            {settings?.available ? (
              <Tag color="success" style={{ marginLeft: 6 }}>
                可用（Airflow 编排）
              </Tag>
            ) : (
              <Tag color="warning" style={{ marginLeft: 6 }}>
                不可用（未启用 / 未配 endpoint）
              </Tag>
            )}
          </span>
        }
        description={
          settings?.available
            ? "物化将产出建表 DDL + 搬运作业 + DAG 交给 Airflow 执行，血缘由 DataHub 插件自动上报。搬运工具与同步策略在物化弹窗逐次选。"
            : "物化一律交 Airflow 编排执行（已去除直连落库模式）：未启用或未配 endpoint 时，物化将报错无法执行。"
        }
      />
      <Form form={form} layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item
          label="启用调度执行"
          name="enabled"
          valuePropName="checked"
          extra="物化必须经 Airflow 执行；关闭后物化将不可用"
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
