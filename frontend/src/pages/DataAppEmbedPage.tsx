import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Card, Empty, Spin } from "antd";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { BarChartRender, DataTableRender, KpiRender } from "../components/DataAppRenderer";
import { DashboardGrid, getSpecPanels, getPanelRefId } from "../components/DashboardGrid";
import { ScreenCanvas, panelToScreenWidget } from "../components/ScreenCanvas";
import type { DataAppDetail, DataAppPreviewResult } from "../types";

/** 无外壳的可嵌入（iframe）已发布数据应用页。路径：/embed/apps/:appId */
export function DataAppEmbedPage() {
  const { appId } = useParams<{ appId: string }>();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [widgetPreviews, setWidgetPreviews] = useState<Record<string, DataAppPreviewResult>>({});

  const { data: app, loading } = useApi<DataAppDetail>(async () => api.getDataApp(appId!), [appId]);

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
      const wout: Record<string, DataAppPreviewResult> = {};
      const wtiles = getSpecPanels(app.spec)
        .map((t) => getPanelRefId(t))
        .filter((wid): wid is string => Boolean(wid));
      for (const wid of wtiles) {
        try {
          wout[wid] = await api.previewWidget(wid);
        } catch {
          /* ignore */
        }
      }
      if (!cancelled) {
        setPreviews(out);
        setWidgetPreviews(wout);
      }
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
  const isCanvas = (app.spec?.layout as string) === "canvas" || isScreen;
  const tiles = getSpecPanels(app.spec);
  const previewByIndex: Record<number, DataAppPreviewResult> = {};
  app.datasets.forEach((d, i) => {
    if (previews[d.id]) previewByIndex[i] = previews[d.id];
  });

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
      {isCanvas ? (
        <div style={{ overflow: "auto" }}>
          <ScreenCanvas
            canvas={
              (app.spec?.canvas as { width: number; height: number; bg?: string }) ?? {
                width: 1920,
                height: 1080,
                bg: "#0b1a2e",
              }
            }
            widgets={tiles.map(panelToScreenWidget)}
            previews={previewByIndex}
            selectedId={null}
          />
        </div>
      ) : isDashboard || tiles.length > 0 ? (
        <DashboardGrid
          tiles={tiles}
          grid={app.spec?.grid as { cols?: number; rowHeight?: number; gap?: number }}
          theme={app.spec?.theme as { bg?: string; accent?: string; preset?: string }}
          datasets={app.datasets.map((d) => ({ id: d.id, name: d.name }))}
          previews={previewByIndex}
          widgetPreviews={widgetPreviews}
        />
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
