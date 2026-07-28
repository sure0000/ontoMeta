import {
  CloudServerOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Descriptions,
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
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { api, clearAdminToken, getAdminToken, setAdminToken } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { useApi } from "../hooks/useApi";
import type {
  DatahubSettings,
  CubeSettings,
  DraftGenerationSettings,
  LlmModelOption,
  LlmServiceConfig,
  LlmConnectionTestResult,
} from "../types";

const { Text } = Typography;

type LlmFormValues = {
  name: string;
  provider: string;
  api_base_url: string;
  api_key?: string;
  model: string;
  is_default: boolean;
  enabled: boolean;
  use_mock: boolean;
};

type DatahubFormValues = {
  gms_url: string;
  frontend_url: string;
  token?: string;
  use_mock: boolean;
};

type DraftGenerationFormValues = {
  object_chunk_concurrency: number;
  relation_chunk_concurrency: number;
};

type CubeFormValues = {
  api_url: string;
  api_secret?: string;
  use_mock: boolean;
  preagg_refresh: string;
  tenant_dimension?: string;
  timeout_seconds: number;
};

type AdminTokenFormValues = {
  token: string;
};

type SettingsBundle = {
  llmServices: LlmServiceConfig[];
  llmModels: LlmModelOption[];
  datahubSettings: DatahubSettings;
  draftGenerationSettings: DraftGenerationSettings;
  cubeSettings: CubeSettings;
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
        llmServices: [],
        llmModels: [],
        datahubSettings: null as unknown as DatahubSettings,
        draftGenerationSettings: null as unknown as DraftGenerationSettings,
        cubeSettings: null as unknown as CubeSettings,
      };
    }
    const [services, models, datahub, draftGeneration, cube] = await Promise.all([
      api.listLlmServices(),
      api.listLlmModels(),
      api.getDatahubSettings(),
      api.getDraftGenerationSettings(),
      api.getCubeSettings(),
    ]);
    return {
      llmServices: services,
      llmModels: models,
      datahubSettings: datahub,
      draftGenerationSettings: draftGeneration,
      cubeSettings: cube,
    };
  }, []);

  const isAuthError = Boolean(error && (error.includes("鉴权") || error.includes("Token") || error.includes("token") || error.includes("401") || error.includes("503")));

  const llmServices = bundle?.llmServices ?? [];
  const llmModels = bundle?.llmModels ?? [];
  const datahubSettings = bundle?.datahubSettings ?? null;
  const draftGenerationSettings = bundle?.draftGenerationSettings ?? null;
  const cubeSettings = bundle?.cubeSettings ?? null;

  const [llmModalOpen, setLlmModalOpen] = useState(false);
  const [llmModalMode, setLlmModalMode] = useState<"create" | "edit">("create");
  const [editingLlmId, setEditingLlmId] = useState<string | null>(null);
  const [llmSubmitting, setLlmSubmitting] = useState(false);
  const [viewingLlm, setViewingLlm] = useState<LlmServiceConfig | null>(null);
  const [llmTesting, setLlmTesting] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState<LlmConnectionTestResult | null>(null);

  const [llmForm] = Form.useForm<LlmFormValues>();
  // 监听提供商，自建 OpenAI 兼容端点需切换为自由填写模型名 + 通用占位符
  const llmProvider = Form.useWatch("provider", llmForm);
  const isOpenAICompatible = llmProvider === "openai-compatible";
  const [datahubForm] = Form.useForm<DatahubFormValues>();
  const [draftGenerationForm] = Form.useForm<DraftGenerationFormValues>();
  const [cubeForm] = Form.useForm<CubeFormValues>();
  const [adminTokenForm] = Form.useForm<AdminTokenFormValues>();
  const [datahubSaving, setDatahubSaving] = useState(false);
  const [draftGenerationSaving, setDraftGenerationSaving] = useState(false);
  const [cubeSaving, setCubeSaving] = useState(false);
  const [adminTokenSaved, setAdminTokenSaved] = useState(() => Boolean(getAdminToken()));

  useEffect(() => {
    if (!datahubSettings) return;
    datahubForm.setFieldsValue({
      gms_url: datahubSettings.gms_url,
      frontend_url: datahubSettings.frontend_url,
      use_mock: datahubSettings.use_mock,
    });
    adminTokenForm.setFieldsValue({ token: getAdminToken() });
    setAdminTokenSaved(Boolean(getAdminToken()));
  }, [datahubSettings, datahubForm, adminTokenForm]);

  useEffect(() => {
    if (!draftGenerationSettings) return;
    draftGenerationForm.setFieldsValue({
      object_chunk_concurrency: draftGenerationSettings.object_chunk_concurrency,
      relation_chunk_concurrency: draftGenerationSettings.relation_chunk_concurrency,
    });
  }, [draftGenerationSettings, draftGenerationForm]);

  useEffect(() => {
    if (!cubeSettings) return;
    cubeForm.setFieldsValue({
      api_url: cubeSettings.api_url,
      use_mock: cubeSettings.use_mock,
      preagg_refresh: cubeSettings.preagg_refresh,
      tenant_dimension: cubeSettings.tenant_dimension ?? "",
      timeout_seconds: cubeSettings.timeout_seconds,
    });
  }, [cubeSettings, cubeForm]);

  const handleCubeSave = async () => {
    try {
      const values = await cubeForm.validateFields();
      setCubeSaving(true);
      const updated = await api.updateCubeSettings({
        api_url: values.api_url,
        api_secret: values.api_secret?.trim() ? values.api_secret.trim() : undefined,
        use_mock: values.use_mock,
        preagg_refresh: values.preagg_refresh,
        tenant_dimension: values.tenant_dimension?.trim() || null,
        timeout_seconds: values.timeout_seconds,
      });
      setBundle((prev) => (prev ? { ...prev, cubeSettings: updated } : prev));
      message.success("Cube 配置已保存");
      cubeForm.setFieldValue("api_secret", "");
    } catch (err) {
      if (err instanceof Error) message.error(err.message);
    } finally {
      setCubeSaving(false);
    }
  };

  const openCreateLlm = () => {
    setLlmModalMode("create");
    setEditingLlmId(null);
    setLlmTestResult(null);
    llmForm.setFieldsValue({
      name: "",
      provider: "deepseek",
      api_base_url: "https://api.deepseek.com",
      api_key: "",
      model: llmModels.find((m) => !m.deprecated)?.id ?? "deepseek-v4-flash",
      is_default: llmServices.length === 0,
      enabled: true,
      use_mock: false,
    });
    setLlmModalOpen(true);
  };

  const openEditLlm = async (record: LlmServiceConfig) => {
    setLlmModalMode("edit");
    setEditingLlmId(record.id);
    setLlmTestResult(null);
    try {
      const detail = await api.getLlmService(record.id);
      llmForm.setFieldsValue({
        name: detail.name,
        provider: detail.provider,
        api_base_url: detail.api_base_url,
        api_key: "",
        model: detail.model,
        is_default: detail.is_default,
        enabled: detail.enabled,
        use_mock: detail.use_mock,
      });
      setLlmModalOpen(true);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载配置失败");
    }
  };

  const openViewLlm = async (record: LlmServiceConfig) => {
    try {
      const detail = await api.getLlmService(record.id);
      setViewingLlm(detail);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载配置失败");
    }
  };

  const handleTestLlm = async () => {
    // 只校验拨测必需字段，避免未填名称等无关项阻断测试
    const values = llmForm.getFieldsValue();
    if (!values.api_base_url || !values.model) {
      message.warning("请先填写 API 地址与模型");
      return;
    }
    setLlmTesting(true);
    setLlmTestResult(null);
    try {
      const result = await api.testLlmConnection({
        api_base_url: values.api_base_url,
        model: values.model,
        provider: values.provider,
        api_key: values.api_key?.trim() ? values.api_key.trim() : undefined,
        service_id: llmModalMode === "edit" && editingLlmId ? editingLlmId : undefined,
      });
      setLlmTestResult(result);
    } catch (err) {
      setLlmTestResult({
        ok: false,
        message: err instanceof Error ? err.message : "测试请求失败",
      });
    } finally {
      setLlmTesting(false);
    }
  };

  const handleLlmSubmit = async () => {
    try {
      const values = await llmForm.validateFields();
      setLlmSubmitting(true);
      const payload = {
        ...values,
        api_key: values.api_key?.trim() ? values.api_key.trim() : undefined,
      };
      if (llmModalMode === "create") {
        await api.createLlmService(payload);
        message.success("LLM 服务配置已创建");
      } else if (editingLlmId) {
        await api.updateLlmService(editingLlmId, payload);
        message.success("LLM 服务配置已更新");
      }
      setLlmModalOpen(false);
      await loadAll();
    } catch (err) {
      if (err instanceof Error && err.message) {
        message.error(err.message);
      }
    } finally {
      setLlmSubmitting(false);
    }
  };

  const handleDeleteLlm = async (id: string) => {
    try {
      await api.deleteLlmService(id);
      message.success("已删除");
      await loadAll();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const handleDatahubSave = () => {
    Modal.confirm({
      title: "确认保存 DataHub 配置",
      content: "将更新 DataHub 连接地址与 Token 配置。",
      okText: "确认保存",
      cancelText: "取消",
      onOk: async () => {
        try {
          const values = await datahubForm.validateFields();
          setDatahubSaving(true);
          const updated = await api.updateDatahubSettings({
            gms_url: values.gms_url.trim(),
            frontend_url: values.frontend_url.trim(),
            token: values.token?.trim() ? values.token.trim() : undefined,
            use_mock: values.use_mock,
          });
          setBundle((prev) =>
            prev ? { ...prev, datahubSettings: updated } : prev,
          );
          message.success("DataHub 配置已保存");
          datahubForm.setFieldValue("token", "");
        } catch (err) {
          message.error(err instanceof Error ? err.message : "保存失败");
        } finally {
          setDatahubSaving(false);
        }
      },
    });
  };

  const handleDraftGenerationSave = async () => {
    try {
      const values = await draftGenerationForm.validateFields();
      setDraftGenerationSaving(true);
      const updated = await api.updateDraftGenerationSettings({
        object_chunk_concurrency: values.object_chunk_concurrency,
        relation_chunk_concurrency: values.relation_chunk_concurrency,
      });
      setBundle((prev) =>
        prev ? { ...prev, draftGenerationSettings: updated } : prev,
      );
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

  const llmColumns: ColumnsType<LlmServiceConfig> = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      render: (name, record) => (
        <Space size={8}>
          <span>{name}</span>
          {record.is_default && <Tag color="blue">默认</Tag>}
        </Space>
      ),
    },
    {
      title: "提供商",
      dataIndex: "provider",
      key: "provider",
      width: 110,
      render: (v) => v.toUpperCase(),
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      width: 180,
    },
    {
      title: "API 地址",
      dataIndex: "api_base_url",
      key: "api_base_url",
      ellipsis: true,
    },
    {
      title: "状态",
      key: "status",
      width: 160,
      render: (_, record) => (
        <Space size={6} wrap>
          <Tag color={record.enabled ? "success" : "default"}>
            {record.enabled ? "启用" : "停用"}
          </Tag>
          {record.use_mock && <Tag color="gold">Mock</Tag>}
          {record.api_key_set ? (
            <Tag>Key 已配置</Tag>
          ) : (
            <Tag color="warning">未配置 Key</Tag>
          )}
        </Space>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 120,
      fixed: "right",
      render: (_, record) => (
        <Space size={8}>
          <Tooltip title="查看">
            <Button type="text" size="small" icon={<EyeOutlined />} onClick={() => openViewLlm(record)} />
          </Tooltip>
          <Tooltip title="编辑">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEditLlm(record)} />
          </Tooltip>
          <Popconfirm
            title="确认删除该 LLM 配置？"
            onConfirm={() => handleDeleteLlm(record.id)}
            okText="删除"
            cancelText="取消"
          >
            <Tooltip title="删除">
              <Button type="text" size="small" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (loading) return <PageSkeleton type="detail" />;

  return (
    <PageContainer>
      <PageHeader
        icon={<SettingOutlined />}
        title="系统设置"
        description="管理 LLM 服务与 DataHub 连接配置，用于本体草稿生成与元数据读取。"
      />

      {error && (
        <Alert
          type={isAuthError ? "warning" : "error"}
          message={isAuthError ? "需要配置管理鉴权" : "加载失败"}
          description={
            isAuthError
              ? "请切换到「管理鉴权」标签页，输入与 backend/.env 中 ONTOMETA_ADMIN_TOKEN 一致的 Token，然后点击保存。"
              : error
          }
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <Tabs
        className="om-tabs"
        defaultActiveKey={isAuthError || !hasToken ? "security" : "llm"}
          items={[
            {
              key: "llm",
              label: (
                <span>
                  <RobotOutlined style={{ marginRight: 6 }} />
                  LLM 服务
                </span>
              ),
              children: (
              <SectionCard
                title="LLM 服务配置"
                icon={<RobotOutlined />}
                extra={
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreateLlm}>
                    新增配置
                  </Button>
                }
                bodyFlush
              >
                <div className="om-table-hint">
                  配置 DeepSeek 等模型服务，默认配置将用于本体草稿生成
                </div>
                <Table
                  className="om-table"
                  rowKey="id"
                  columns={llmColumns}
                  dataSource={llmServices}
                  pagination={false}
                  scroll={{ x: 800 }}
                  locale={{ emptyText: "暂无 LLM 配置，请点击「新增配置」" }}
                />
              </SectionCard>
            ),
          },
          {
            key: "datahub",
            label: (
              <span>
                <CloudServerOutlined style={{ marginRight: 6 }} />
                DataHub
              </span>
            ),
            children: (
              <SectionCard
                title="DataHub 连接配置"
                icon={<CloudServerOutlined />}
                extra={
                  datahubSettings ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      最近更新：{new Date(datahubSettings.updated_at).toLocaleString()}
                    </Text>
                  ) : null
                }
              >
                <Text type="secondary" style={{ display: "block", marginBottom: 16, fontSize: 13 }}>
                  配置 GMS 与前端地址，用于读取数据域、表元数据与生成跳转链接
                </Text>
                <Form
                  form={datahubForm}
                  layout="vertical"
                  style={{ maxWidth: 640 }}
                >
                  <Form.Item
                    label="GMS 地址"
                    name="gms_url"
                    rules={[{ required: true, message: "请输入 GMS 地址" }]}
                    extra="DataHub GraphQL API 地址，例如 http://localhost:8080"
                  >
                    <Input prefix={<CloudServerOutlined />} placeholder="http://localhost:8080" />
                  </Form.Item>
                  <Form.Item
                    label="前端地址"
                    name="frontend_url"
                    rules={[{ required: true, message: "请输入前端地址" }]}
                    extra="用于生成 DataHub 页面跳转链接，例如 http://localhost:9002"
                  >
                    <Input placeholder="http://localhost:9002" />
                  </Form.Item>
                  <Form.Item
                    label="访问 Token"
                    name="token"
                    extra={
                      datahubSettings?.token_set
                        ? `已配置：${datahubSettings.token_hint ?? "****"}，留空则保持不变`
                        : "可选，访问受保护的 DataHub 实例时需要"
                    }
                  >
                    <Input.Password placeholder="Bearer Token（可选）" />
                  </Form.Item>
                  <Form.Item
                    label="使用 Mock 数据"
                    name="use_mock"
                    valuePropName="checked"
                    extra="开启后不连接真实 DataHub，使用内置示例数据"
                  >
                    <Switch />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" onClick={handleDatahubSave} loading={datahubSaving}>
                      保存 DataHub 配置
                    </Button>
                  </Form.Item>
                </Form>
              </SectionCard>
            ),
          },
          {
            key: "draft-generation",
            label: (
              <span>
                <ThunderboltOutlined style={{ marginRight: 6 }} />
                草稿生成并发
              </span>
            ),
            children: (
              <SectionCard
                title="草稿生成分块并发度"
                icon={<ThunderboltOutlined />}
                extra={
                  draftGenerationSettings ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      最近更新：{new Date(draftGenerationSettings.updated_at).toLocaleString()}
                    </Text>
                  ) : null
                }
              >
                <Text type="secondary" style={{ display: "block", marginBottom: 16, fontSize: 13 }}>
                  数据域表数较多时，草稿生成会把业务对象命名与业务关系命名分别拆成多个批次并发调用
                  LLM。这里的并发度决定同一时刻最多有多少个批次在同时请求；调大可缩短大域的生成耗时，
                  但也会提高对 LLM 服务的瞬时并发压力，请结合服务端承载能力设置。修改后立即生效，无需
                  重启服务。
                </Text>
                <Form
                  form={draftGenerationForm}
                  layout="vertical"
                  style={{ maxWidth: 480 }}
                >
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
            ),
          },
          {
            key: "cube",
            label: (
              <span>
                <DatabaseOutlined style={{ marginRight: 6 }} />
                Cube 语义层
              </span>
            ),
            children: (
              <SectionCard
                title="Cube 外挂配置"
                icon={<DatabaseOutlined />}
                extra={
                  cubeSettings ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      最近更新：{new Date(cubeSettings.updated_at).toLocaleString()}
                    </Text>
                  ) : null
                }
              >
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="所有 Cube 配置均在此管理，无需环境变量/配置文件，保存后立即生效"
                  description="Cube 作为外挂语义层承担执行/缓存/预聚合定时刷新/行级权限。关闭 Mock 并填写密钥后即走真实 Cube。"
                />
                <Form form={cubeForm} layout="vertical" style={{ maxWidth: 640 }}>
                  <Form.Item
                    label="使用 Mock（不连真实 Cube）"
                    name="use_mock"
                    valuePropName="checked"
                    extra="开启后返回确定性示例数据，本地零依赖可跑；未填密钥时也自动走 Mock"
                  >
                    <Switch />
                  </Form.Item>
                  <Form.Item
                    label="Cube API 地址"
                    name="api_url"
                    rules={[{ required: true, message: "请输入 Cube API 地址" }]}
                    extra="例如 http://cube:4000"
                  >
                    <Input prefix={<DatabaseOutlined />} placeholder="http://cube:4000" />
                  </Form.Item>
                  <Form.Item
                    label="API 密钥（CUBEJS_API_SECRET）"
                    name="api_secret"
                    extra={
                      cubeSettings?.secret_set
                        ? `已配置：${cubeSettings.secret_hint ?? "****"}，留空则保持不变`
                        : "与 Cube 服务的 CUBEJS_API_SECRET 一致，用于签发访问令牌"
                    }
                  >
                    <Input.Password placeholder="与 Cube 保持一致" />
                  </Form.Item>
                  <Form.Item
                    label="预聚合定时刷新间隔"
                    name="preagg_refresh"
                    rules={[{ required: true, message: "请输入刷新间隔" }]}
                    extra="交给 Cube Refresh Worker，例如 1 hour / 30 minute"
                  >
                    <Input placeholder="1 hour" />
                  </Form.Item>
                  <Form.Item
                    label="行级权限：租户隔离列名（可选）"
                    name="tenant_dimension"
                    extra="各对象若含此属性，生成的 cube.js 会自动按 securityContext.tenant 强制过滤；留空不启用"
                  >
                    <Input placeholder="tenant_id" />
                  </Form.Item>
                  <Form.Item
                    label="查询超时（秒）"
                    name="timeout_seconds"
                    rules={[{ required: true, message: "请输入超时" }]}
                  >
                    <InputNumber min={1} max={600} style={{ width: "100%" }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" onClick={() => void handleCubeSave()} loading={cubeSaving}>
                      保存 Cube 配置
                    </Button>
                  </Form.Item>
                </Form>
              </SectionCard>
            ),
          },
          {
            key: "security",
            label: (
              <span>
                <SafetyCertificateOutlined style={{ marginRight: 6 }} />
                管理鉴权
              </span>
            ),
            children: (
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
            ),
          },
        ]}
      />

      <Modal
        title={llmModalMode === "create" ? "新增 LLM 服务配置" : "编辑 LLM 服务配置"}
        open={llmModalOpen}
        onCancel={() => setLlmModalOpen(false)}
        onOk={handleLlmSubmit}
        confirmLoading={llmSubmitting}
        okText="保存"
        cancelText="取消"
        destroyOnClose
        width={560}
      >
        <Form form={llmForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            label="配置名称"
            name="name"
            rules={[{ required: true, message: "请输入配置名称" }]}
          >
            <Input placeholder="例如：DeepSeek 生产环境" />
          </Form.Item>
          <Form.Item label="提供商" name="provider" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "deepseek", label: "DeepSeek" },
                { value: "openai-compatible", label: "OpenAI 兼容（自建）" },
              ]}
              disabled={llmModalMode === "edit"}
              onChange={(value) => {
                // 切换提供商时套用该类型默认地址/模型，避免残留上一个提供商的取值
                if (value === "openai-compatible") {
                  llmForm.setFieldsValue({ api_base_url: "http://localhost:8000/v1", model: "" });
                } else if (value === "deepseek") {
                  llmForm.setFieldsValue({
                    api_base_url: "https://api.deepseek.com",
                    model: llmModels.find((m) => !m.deprecated)?.id ?? "deepseek-v4-flash",
                  });
                }
              }}
            />
          </Form.Item>
          <Form.Item
            label="API 地址"
            name="api_base_url"
            rules={[{ required: true, message: "请输入 API 地址" }]}
          >
            <Input placeholder={isOpenAICompatible ? "http://localhost:8000/v1" : "https://api.deepseek.com"} />
          </Form.Item>
          <Form.Item
            label="API Key"
            name="api_key"
            extra={
              llmModalMode === "edit" && editingLlmId
                ? `留空则保持现有 Key（${
                    llmServices.find((s) => s.id === editingLlmId)?.api_key_hint ?? "未配置"
                  }）`
                : isOpenAICompatible
                  ? "自建服务的鉴权 Key；若服务无需鉴权可留空"
                  : "DeepSeek 开放平台申请的 API Key"
            }
          >
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Form.Item
            label="模型"
            name="model"
            rules={[
              { required: true, message: isOpenAICompatible ? "请输入模型名称" : "请选择模型" },
            ]}
          >
            {isOpenAICompatible ? (
              <Input placeholder="模型名称，如 qwen2.5-72b-instruct、gpt-4o-mini" />
            ) : (
              <Select
                options={[
                {
                  label: "最新模型（推荐）",
                  options: llmModels
                    .filter((model) => !model.deprecated)
                    .map((model) => ({
                      value: model.id,
                      label: model.label,
                      desc: model.description,
                    })),
                },
                {
                  label: "兼容模型（即将弃用）",
                  options: llmModels
                    .filter((model) => model.deprecated)
                    .map((model) => ({
                      value: model.id,
                      label: model.label,
                      desc: model.description,
                    })),
                },
              ]}
              optionRender={(option) => {
                const desc = (option.data as { desc?: string }).desc;
                if (!desc) return <span>{option.data.label}</span>;
                return (
                  <div>
                    <div>{option.data.label}</div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {desc}
                    </Text>
                  </div>
                );
              }}
              />
            )}
          </Form.Item>
          <Form.Item label="设为默认" name="is_default" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item label="启用" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            label="使用 Mock 生成"
            name="use_mock"
            valuePropName="checked"
            extra="开启后跳过真实 LLM 调用，使用规则生成草稿"
          >
            <Switch />
          </Form.Item>
          <Form.Item label="连接测试" extra="向该地址/模型发一次最小请求，验证连通性（不会保存配置）">
            <Space direction="vertical" style={{ width: "100%" }} size={8}>
              <Button
                icon={<ThunderboltOutlined />}
                loading={llmTesting}
                onClick={handleTestLlm}
              >
                测试连接
              </Button>
              {llmTestResult && (
                <Alert
                  type={llmTestResult.ok ? "success" : "error"}
                  showIcon
                  message={llmTestResult.ok ? "连接成功" : "连接失败"}
                  description={
                    llmTestResult.ok
                      ? `模型 ${llmTestResult.model ?? ""}${
                          llmTestResult.latency_ms != null
                            ? ` · 耗时 ${llmTestResult.latency_ms}ms`
                            : ""
                        }`
                      : llmTestResult.message
                  }
                />
              )}
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title="LLM 服务配置详情"
        open={!!viewingLlm}
        onClose={() => setViewingLlm(null)}
        width={480}
      >
        {viewingLlm && (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="名称">{viewingLlm.name}</Descriptions.Item>
            <Descriptions.Item label="提供商">{viewingLlm.provider}</Descriptions.Item>
            <Descriptions.Item label="API 地址">{viewingLlm.api_base_url}</Descriptions.Item>
            <Descriptions.Item label="模型">{viewingLlm.model}</Descriptions.Item>
            <Descriptions.Item label="API Key">
              {viewingLlm.api_key_set ? viewingLlm.api_key_hint : "未配置"}
            </Descriptions.Item>
            <Descriptions.Item label="默认配置">
              {viewingLlm.is_default ? "是" : "否"}
            </Descriptions.Item>
            <Descriptions.Item label="启用状态">
              {viewingLlm.enabled ? "启用" : "停用"}
            </Descriptions.Item>
            <Descriptions.Item label="Mock 模式">
              {viewingLlm.use_mock ? "是" : "否"}
            </Descriptions.Item>
            <Descriptions.Item label="创建时间">
              {new Date(viewingLlm.created_at).toLocaleString()}
            </Descriptions.Item>
            <Descriptions.Item label="更新时间">
              {new Date(viewingLlm.updated_at).toLocaleString()}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Drawer>
    </PageContainer>
  );
}
