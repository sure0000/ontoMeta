import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  message,
  Modal,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  EyeOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "../components/DataAppRenderer";
import type { DataAppDetail, DataAppPreviewResult } from "../types";

const { Text, Paragraph } = Typography;

export function DataAppEditorPage() {
  const { appId } = useParams<{ appId: string }>();
  const navigate = useNavigate();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishComment, setPublishComment] = useState("");
  const [showPublish, setShowPublish] = useState(false);

  const { data: app, loading, reload, setData } = useApi<DataAppDetail>(
    async () => api.getDataApp(appId!),
    [appId],
  );

  const runPreview = async (datasetId: string) => {
    if (!appId) return;
    setPreviewing(datasetId);
    try {
      const res = await api.previewDataAppDataset(appId, datasetId);
      setPreviews((prev) => ({ ...prev, [datasetId]: res }));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预览失败");
    } finally {
      setPreviewing(null);
    }
  };

  const handleRename = async (name: string) => {
    if (!appId || !name.trim()) return;
    try {
      const updated = await api.updateDataApp(appId, { name: name.trim() });
      setData(updated);
      message.success("已保存名称");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    }
  };

  const handlePublish = async () => {
    if (!appId) return;
    setPublishing(true);
    try {
      const updated = await api.publishDataApp(appId, publishComment || undefined);
      setData(updated);
      setShowPublish(false);
      setPublishComment("");
      message.success(`已发布 v${updated.published_version}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "发布失败");
    } finally {
      setPublishing(false);
    }
  };

  if (loading) {
    return (
      <PageContainer>
        <Spin />
      </PageContainer>
    );
  }
  if (!app) {
    return (
      <PageContainer>
        <Empty description="数据应用不存在" />
      </PageContainer>
    );
  }

  const isScreen = app.app_type === "screen";
  const widgets =
    (app.spec?.widgets as { type?: string; datasetIndex?: number; title?: string }[]) ??
    [];

  const renderPreview = (datasetId: string, widgetType?: string) => {
    const p = previews[datasetId];
    if (!p) {
      return (
        <Text type="secondary">点击「预览」拉取示例数据（当前为 Mock 数据源）</Text>
      );
    }
    const props = { columns: p.columns, rows: p.rows };
    if (widgetType === "bar") return <BarChartRender {...props} />;
    if (widgetType === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <PageContainer>
      <PageHeader
        icon={<ArrowLeftOutlined onClick={() => navigate("/data-apps")} style={{ cursor: "pointer" }} />}
        title={
          <Space>
            <Input
              defaultValue={app.name}
              variant="borderless"
              style={{ fontSize: 20, fontWeight: 600, width: 320 }}
              onBlur={(e) => handleRename(e.target.value)}
            />
            <Tag color={app.status === "published" ? "success" : "default"}>
              {app.status === "published" ? `已发布 v${app.published_version}` : `草稿 v${app.current_version}`}
            </Tag>
            <Tag>{isScreen ? "可视化大屏" : "数据表格"}</Tag>
          </Space>
        }
        description={app.description}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => reload()}>
              刷新
            </Button>
            {app.status === "published" && (
              <Button icon={<EyeOutlined />} onClick={() => navigate(`/apps/${app.id}`)}>
                查看已发布
              </Button>
            )}
            <Button
              type="primary"
              icon={<CloudUploadOutlined />}
              onClick={() => setShowPublish(true)}
            >
              发布
            </Button>
          </Space>
        }
      />

      {app.datasets.length === 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="该应用暂无数据集"
          description="可在「智能问数」中提问后点击「生成表格/大屏」自动带入数据集，或后续在此手工配置口径绑定。"
        />
      )}

      {isScreen ? (
        <Card title="大屏画布（预览）">
          {widgets.length === 0 ? (
            <Empty description="暂无组件" />
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))",
                gap: 16,
              }}
            >
              {widgets.map((w, i) => {
                const ds = app.datasets[w.datasetIndex ?? 0];
                return (
                  <Card
                    key={i}
                    size="small"
                    title={w.title || `组件 ${i + 1}`}
                    extra={
                      ds && (
                        <Button
                          size="small"
                          loading={previewing === ds.id}
                          onClick={() => runPreview(ds.id)}
                        >
                          预览
                        </Button>
                      )
                    }
                  >
                    {ds ? renderPreview(ds.id, w.type) : <Empty description="无数据集" />}
                  </Card>
                );
              })}
            </div>
          )}
        </Card>
      ) : (
        <Tabs
          items={app.datasets.map((ds) => ({
            key: ds.id,
            label: ds.name,
            children: (
              <Card
                extra={
                  <Button
                    icon={<EyeOutlined />}
                    loading={previewing === ds.id}
                    onClick={() => runPreview(ds.id)}
                  >
                    预览
                  </Button>
                }
              >
                <Paragraph>
                  <Text type="secondary">编译 SQL（基于本体语义，需映射物理表后执行）：</Text>
                </Paragraph>
                <pre
                  style={{
                    background: "#0f172a",
                    color: "#e2e8f0",
                    padding: 12,
                    borderRadius: 8,
                    overflowX: "auto",
                    fontSize: 12,
                  }}
                >
                  {ds.compiled_sql || "（未能编译，请检查数据集绑定是否落地到本体）"}
                </pre>
                {previews[ds.id]?.warnings?.length ? (
                  <Alert
                    type="warning"
                    style={{ marginBottom: 12 }}
                    message={previews[ds.id].warnings.join("；")}
                  />
                ) : null}
                <div style={{ marginTop: 12 }}>{renderPreview(ds.id)}</div>
              </Card>
            ),
          }))}
        />
      )}

      <Modal
        title="发布数据应用"
        open={showPublish}
        confirmLoading={publishing}
        onOk={handlePublish}
        onCancel={() => setShowPublish(false)}
        okText="确认发布"
      >
        <Paragraph type="secondary">
          发布将冻结当前配置与数据集绑定为一个只读版本快照，可在版本记录中回看。
        </Paragraph>
        <Input.TextArea
          rows={3}
          placeholder="版本备注（可选）"
          value={publishComment}
          onChange={(e) => setPublishComment(e.target.value)}
        />
      </Modal>
    </PageContainer>
  );
}
