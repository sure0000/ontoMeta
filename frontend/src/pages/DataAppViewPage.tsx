import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Card, Empty, Space, Spin, Tag, Typography, Button, message } from "antd";
import { ArrowLeftOutlined, LinkOutlined } from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "../components/DataAppRenderer";
import {
  ParamBar,
  buildRuntimeFilters,
  type DrillFilter,
} from "../components/ParamBar";
import type {
  DataAppDetail,
  DataAppPreviewResult,
  RuntimeFilter,
  ScreenParam,
} from "../types";

const { Text } = Typography;

export function DataAppViewPage() {
  const { appId } = useParams<{ appId: string }>();
  const navigate = useNavigate();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [drills, setDrills] = useState<DrillFilter[]>([]);

  const { data: app, loading } = useApi<DataAppDetail>(
    async () => api.getDataApp(appId!),
    [appId],
  );

  const params = (app?.spec?.params as ScreenParam[]) ?? [];

  const loadData = async (filters: RuntimeFilter[]) => {
    if (!app || !appId) return;
    const out: Record<string, DataAppPreviewResult> = {};
    for (const ds of app.datasets) {
      try {
        out[ds.id] = await api.previewDataAppDataset(appId, ds.id, 50, filters);
      } catch {
        /* ignore individual dataset failure */
      }
    }
    setPreviews(out);
  };

  // 已发布只读页：自动拉取各数据集数据
  useEffect(() => {
    if (!app || !appId) return;
    let cancelled = false;
    (async () => {
      const out: Record<string, DataAppPreviewResult> = {};
      for (const ds of app.datasets) {
        try {
          out[ds.id] = await api.previewDataAppDataset(appId, ds.id);
        } catch {
          /* ignore individual dataset failure */
        }
      }
      if (!cancelled) setPreviews(out);
    })();
    return () => {
      cancelled = true;
    };
  }, [app, appId]);

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

  const renderData = (datasetId: string, widgetType?: string) => {
    const p = previews[datasetId];
    if (!p) return <Spin size="small" />;
    const props = { columns: p.columns, rows: p.rows };
    if (widgetType === "bar")
      return (
        <BarChartRender
          {...props}
          onBarClick={(col, val) => {
            const next = [...drills.filter((d) => d.column !== col), { column: col, value: val }];
            setDrills(next);
            void api
              .previewDataAppDataset(appId!, datasetId, 50, buildRuntimeFilters(params, paramValues, next))
              .then((res) => setPreviews((prev) => ({ ...prev, [datasetId]: res })));
          }}
        />
      );
    if (widgetType === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <PageContainer>
      <PageHeader
        icon={
          <ArrowLeftOutlined
            onClick={() => navigate("/data-apps")}
            style={{ cursor: "pointer" }}
          />
        }
        title={
          <Space>
            {app.name}
            {app.status === "published" ? (
              <Tag color="success">已发布 v{app.published_version}</Tag>
            ) : (
              <Tag>草稿</Tag>
            )}
          </Space>
        }
        description={app.description}
        extra={
          app.status === "published" ? (
            <Button
              icon={<LinkOutlined />}
              onClick={() => {
                const url = `${window.location.origin}/embed/apps/${app.id}`;
                void navigator.clipboard?.writeText(url);
                message.success("嵌入链接已复制（/embed/apps/…，可用于 iframe）");
              }}
            >
              复制嵌入链接
            </Button>
          ) : undefined
        }
      />

      {app.status !== "published" && (
        <Text type="warning">当前应用尚未发布，此处展示的是草稿内容。</Text>
      )}

      <ParamBar
        params={params}
        values={paramValues}
        drills={drills}
        onChange={setParamValues}
        onClearDrill={(i) => {
          const next = drills.filter((_, xi) => xi !== i);
          setDrills(next);
          void loadData(buildRuntimeFilters(params, paramValues, next));
        }}
        onApply={() => loadData(buildRuntimeFilters(params, paramValues, drills))}
      />

      {isScreen ? (
        <div
          style={{
            background: (app.spec?.canvas as { bg?: string })?.bg || "#0b1a2e",
            padding: 24,
            borderRadius: 12,
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))",
            gap: 16,
          }}
        >
          {widgets.length === 0 ? (
            <Empty description="暂无组件" />
          ) : (
            widgets.map((w, i) => {
              const ds = app.datasets[w.datasetIndex ?? 0];
              return (
                <Card key={i} size="small" title={w.title || `组件 ${i + 1}`}>
                  {ds ? renderData(ds.id, w.type) : <Empty description="无数据集" />}
                </Card>
              );
            })
          )}
        </div>
      ) : (
        <Space direction="vertical" style={{ width: "100%" }} size="large">
          {app.datasets.map((ds) => (
            <Card key={ds.id} title={ds.name}>
              {renderData(ds.id)}
            </Card>
          ))}
        </Space>
      )}
    </PageContainer>
  );
}
