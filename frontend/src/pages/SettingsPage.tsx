import {
  ApiOutlined,
  DatabaseOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Space,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { useEffect, useState } from "react";
import { api, clearAdminToken, getAdminToken, setAdminToken } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { DependencyPanel } from "../components/DependencyPanel";
import { DataSourcesPanel } from "../components/DataSourcesModal";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import type { DraftGenerationSettings } from "../types";

const { Text } = Typography;

type DraftGenerationFormValues = {
  object_chunk_concurrency: number;
  relation_chunk_concurrency: number;
};

type AdminTokenFormValues = {
  token: string;
};

type SettingsBundle = {
  draftGenerationSettings: DraftGenerationSettings;
};

export function SettingsPage() {
  const hasToken = Boolean(getAdminToken());

  const {
    data: bundle,
    loading,
    error,
    reload: loadAll,
    setData: setBundle,
  } = useApi<SettingsBundle>(async () => {
    if (!getAdminToken()) {
      // 尚未配置管理 Token，跳过请求，避免无意义的 401 报错
      return {
        draftGenerationSettings: null as unknown as DraftGenerationSettings,
      };
    }
    const [draftGeneration] = await Promise.all([api.getDraftGenerationSettings()]);
    return { draftGenerationSettings: draftGeneration };
  }, []);

  const isAuthError = Boolean(
    error &&
    (error.includes("鉴权") ||
      error.includes("Token") ||
      error.includes("token") ||
      error.includes("401") ||
      error.includes("503")),
  );

  const draftGenerationSettings = bundle?.draftGenerationSettings ?? null;

  const [draftGenerationForm] = Form.useForm<DraftGenerationFormValues>();
  const [adminTokenForm] = Form.useForm<AdminTokenFormValues>();
  const [draftGenerationSaving, setDraftGenerationSaving] = useState(false);
  const [adminTokenSaved, setAdminTokenSaved] = useState(() => Boolean(getAdminToken()));

  // 管理 Token 存在本机 localStorage，与服务端 bundle 无关：单独初始化，
  // 否则鉴权失败（bundle 加载不出来）时 Token 输入框会不回显。
  useEffect(() => {
    adminTokenForm.setFieldsValue({ token: getAdminToken() });
    setAdminTokenSaved(Boolean(getAdminToken()));
  }, [adminTokenForm]);

  useEffect(() => {
    if (!draftGenerationSettings) return;
    draftGenerationForm.setFieldsValue({
      object_chunk_concurrency: draftGenerationSettings.object_chunk_concurrency,
      relation_chunk_concurrency: draftGenerationSettings.relation_chunk_concurrency,
    });
  }, [draftGenerationSettings, draftGenerationForm]);

  const handleDraftGenerationSave = async () => {
    try {
      const values = await draftGenerationForm.validateFields();
      setDraftGenerationSaving(true);
      const updated = await api.updateDraftGenerationSettings({
        object_chunk_concurrency: values.object_chunk_concurrency,
        relation_chunk_concurrency: values.relation_chunk_concurrency,
      });
      setBundle((prev) => (prev ? { ...prev, draftGenerationSettings: updated } : prev));
      message.success("草稿生成并发配置已保存，下次生成即生效");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setDraftGenerationSaving(false);
    }
  };

  const handleAdminTokenSave = async () => {
    const values = await adminTokenForm.validateFields();
    const token = values.token.trim();
    if (!token) {
      message.warning("请输入与 backend/.env 中 ONTOMETA_ADMIN_TOKEN 一致的 Token");
      return;
    }
    setAdminToken(token);
    setAdminTokenSaved(true);
    message.success("管理 Token 已保存到本机，后续请求将自动携带");
    await loadAll();
  };

  const handleAdminTokenClear = () => {
    clearAdminToken();
    adminTokenForm.setFieldsValue({ token: "" });
    setAdminTokenSaved(false);
    message.info("已清除本机管理 Token");
  };

  if (loading) return <PageSkeleton type="detail" />;

  return (
    <PageContainer>
      <PageHeader
        icon={<SettingOutlined />}
        title="系统设置"
        description="管理依赖组件、Doris 统一数仓、业务数据源、生成配置与鉴权。LLM / DataHub / Airflow 统一在「基础设施」中管理。"
      />

      {error && (
        <Alert
          type={isAuthError ? "warning" : "error"}
          message={isAuthError ? "需要配置管理鉴权" : "加载失败"}
          description={
            isAuthError
              ? "请切换到「安全与鉴权」标签页，输入与 backend/.env 中 ONTOMETA_ADMIN_TOKEN 一致的 Token，然后点击保存。"
              : error
          }
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <Tabs
        className="om-tabs om-tabs--vertical"
        tabPosition="left"
        defaultActiveKey={isAuthError || !hasToken ? "security" : "infra"}
        items={[
          {
            key: "infra",
            label: (
              <span>
                <ApiOutlined style={{ marginRight: 6 }} />
                基础设施
              </span>
            ),
            children: (
              <div className="om-tab-stack">
                <SectionCard title="基础设施组件管理" icon={<ApiOutlined />} bodyFlush>
                  <DependencyPanel />
                </SectionCard>
              </div>
            ),
          },
          {
            key: "generation",
            label: (
              <span>
                <ThunderboltOutlined style={{ marginRight: 6 }} />
                生成配置
              </span>
            ),
            children: (
              <div className="om-tab-stack">
                <SectionCard
                  title="草稿生成分块并发度"
                  icon={<ThunderboltOutlined />}
                  extra={
                    draftGenerationSettings ? (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        最近更新：
                        {new Date(draftGenerationSettings.updated_at).toLocaleString()}
                      </Text>
                    ) : null
                  }
                >
                  <Text
                    type="secondary"
                    style={{ display: "block", marginBottom: 16, fontSize: 13 }}
                  >
                    数据域表数较多时，草稿生成会把业务对象命名与业务关系命名分别拆成多个批次并发调用
                    LLM。这里的并发度决定同一时刻最多有多少个批次在同时请求；调大可缩短大域的生成耗时，
                    但也会提高对 LLM
                    服务的瞬时并发压力，请结合服务端承载能力设置。修改后立即生效，无需 重启服务。LLM
                    服务本身的连接配置在「基础设施」中管理。
                  </Text>
                  <Form form={draftGenerationForm} layout="vertical" style={{ maxWidth: 480 }}>
                    <Form.Item
                      label="业务对象命名并发度"
                      name="object_chunk_concurrency"
                      rules={[{ required: true, message: "请输入并发度" }]}
                      extra="每批最多 10 张表，此处设置同时执行的批次数上限（1~32）"
                    >
                      <InputNumber min={1} max={32} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item
                      label="业务关系命名并发度"
                      name="relation_chunk_concurrency"
                      rules={[{ required: true, message: "请输入并发度" }]}
                      extra="每批最多 40 条关系，此处设置同时执行的批次数上限（1~32）"
                    >
                      <InputNumber min={1} max={32} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item>
                      <Button
                        type="primary"
                        onClick={handleDraftGenerationSave}
                        loading={draftGenerationSaving}
                      >
                        保存并发配置
                      </Button>
                    </Form.Item>
                  </Form>
                </SectionCard>
              </div>
            ),
          },
          {
            key: "data-sources",
            forceRender: true,
            label: (
              <span>
                <DatabaseOutlined style={{ marginRight: 6 }} />
                数据源
              </span>
            ),
            children: (
              <SectionCard title="业务数据源管理" icon={<DatabaseOutlined />}>
                <Text type="secondary" style={{ display: "block", marginBottom: 16, fontSize: 13 }}>
                  这里登记 MySQL、PostgreSQL 等业务源连接，用于源数据接入。默认数仓的 SQL 端点、FE
                  HTTP 节点和凭据请在「基础设施」中统一配置。
                </Text>
                <DataSourcesPanel includeDoris={false} />
              </SectionCard>
            ),
          },
          {
            key: "security",
            forceRender: true,
            label: (
              <span>
                <SafetyCertificateOutlined style={{ marginRight: 6 }} />
                安全与鉴权
              </span>
            ),
            children: (
              <div className="om-tab-stack">
                <SectionCard
                  title="管理 Token"
                  icon={<SafetyCertificateOutlined />}
                  extra={
                    adminTokenSaved ? (
                      <Tag color="success">本机已配置</Tag>
                    ) : (
                      <Tag color="warning">未配置</Tag>
                    )
                  }
                >
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message="管理 API 需携带与后端 ONTOMETA_ADMIN_TOKEN 一致的 Token"
                    description="Token 仅保存在本机浏览器 localStorage，用于开发便利；生产环境建议由反向代理注入。"
                  />
                  <Form form={adminTokenForm} layout="vertical" style={{ maxWidth: 640 }}>
                    <Form.Item
                      label="Admin Token"
                      name="token"
                      rules={[{ required: true, message: "请输入管理 Token" }]}
                      extra="对应 backend/.env 中的 ONTOMETA_ADMIN_TOKEN"
                    >
                      <Input.Password placeholder="与后端配置保持一致" />
                    </Form.Item>
                    <Form.Item>
                      <Space>
                        <Button type="primary" onClick={() => void handleAdminTokenSave()}>
                          保存到本机
                        </Button>
                        <Button onClick={handleAdminTokenClear}>清除</Button>
                      </Space>
                    </Form.Item>
                  </Form>
                </SectionCard>
                <Alert
                  type="info"
                  showIcon
                  message="Agent 接入已移至独立菜单"
                  description="MCP 服务、Skill 和外部 Agent 令牌请前往左侧「Agent 接入」管理。"
                  style={{ marginTop: 16 }}
                />
              </div>
            ),
          },
        ]}
      />
    </PageContainer>
  );
}
