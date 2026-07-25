import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Card, Empty, Spin } from "antd";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "../components/DataAppRenderer";
import { DashboardGrid, type DashboardTile } from "../components/DashboardGrid";
import type { DataAppDetail, DataAppPreviewResult } from "../types";

/** 无外壳的可嵌入（iframe）已发布数据应用页。路径：/embed/apps/:appId */
export function DataAppEmbedPage() {
  const { appId } = useParams<{ appId: string }>();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});

  const { data: app, loading } = useApi<DataAppDetail>(
    async () => api.getDataApp(appId!),
    [appId],
  );

  useEffect(() => {
    if (!app || !appId) return;
    let cancelled = false;
    (async () => {
      const out: Record<string, DataAppPreviewResult> = {};
      for (const ds of app.datasets) {
        try {
          out[ds.id] = await api.previewDataAppDataset(appId, ds.id);
        } catch {
          /* ignore */
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
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin />
      </div>
    );
  }
  if (!app) return <Empty description="数据应用不存在" />;

  const isScreen = app.app_type === "screen";
  const isDashboard = app.app_type === "dashboard";
  const tiles = (app.spec?.tiles as DashboardTile[]) ?? [];
  const previewByIndex: Record<number, DataAppPreviewResult> = {};
  app.datasets.forEach((d, i) => {
    if (previews[d.id]) previewByIndex[i] = previews[d.id];
  });
  const widgets =
    (app.spec?.widgets as { type?: string; datasetIndex?: number; title?: string }[]) ??
    [];

  const renderData = (datasetId: string, type?: string) => {
    const p = previews[datasetId];
    if (!p) return <Spin size="small" />;
    const props = { columns: p.columns, rows: p.rows };
    if (type === "bar") return <BarChartRender {...props} />;
    if (type === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <div style={{ padding: 12, minHeight: "100vh", background: "#f5f7fa" }}>
      {isDashboard ? (
        <DashboardGrid
          tiles={tiles}
          grid={app.spec?.grid as { cols?: number; rowHeight?: number; gap?: number }}
          datasets={app.datasets.map((d) => ({ id: d.id, name: d.name }))}
          previews={previewByIndex}
        />
      ) : isScreen ? (
        <div
          style={{
            background: (app.spec?.canvas as { bg?: string })?.bg || "#0b1a2e",
            padding: 16,
            borderRadius: 12,
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
            gap: 12,
          }}
        >
          {widgets.map((w, i) => {
            const ds = app.datasets[w.datasetIndex ?? 0];
            return (
              <Card key={i} size="small" title={w.title || `组件 ${i + 1}`}>
                {ds ? renderData(ds.id, w.type) : <Empty />}
              </Card>
            );
          })}
        </div>
      ) : (
        app.datasets.map((ds) => (
          <Card key={ds.id} title={ds.name} style={{ marginBottom: 12 }}>
            {renderData(ds.id)}
          </Card>
        ))
      )}
    </div>
  );
}
