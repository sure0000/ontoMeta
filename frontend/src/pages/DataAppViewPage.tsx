import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Empty, Spin, Tooltip, message } from "antd";
import {
  ArrowLeftOutlined,
  LinkOutlined,
  FullscreenOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
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
import { DashboardGrid, getSpecPanels, getPanelRefId } from "../components/DashboardGrid";
import { ScreenCanvas, panelToScreenWidget } from "../components/ScreenCanvas";
import type {
  DataAppDetail,
  DataAppPreviewResult,
  RuntimeFilter,
  ScreenParam,
} from "../types";

/** 顶部实时时钟 */
function LiveClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const time = now.toLocaleTimeString("zh-CN", { hour12: false });
  const date = now.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
  });
  return (
    <div className="bigscreen-clock">
      <span className="bigscreen-clock-time">{time}</span>
      <span className="bigscreen-clock-date">{date}</span>
    </div>
  );
}

/**
 * 已发布数据应用的独立「大屏」展示页（路径：/apps/:appId）。
 * 脱离主框架，整页铺满视口，深色科技风，看上去像一块真正的数据大屏。
 */
export function DataAppViewPage() {
  const { appId } = useParams<{ appId: string }>();
  const navigate = useNavigate();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [widgetPreviews, setWidgetPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [drills, setDrills] = useState<DrillFilter[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const { data: app, loading } = useApi<DataAppDetail>(
    async () => api.getDataApp(appId!),
    [appId],
  );

  const params = (app?.spec?.params as ScreenParam[]) ?? [];

  const loadData = async (filters: RuntimeFilter[]) => {
    if (!app || !appId) return;
    setRefreshing(true);
    try {
      const out: Record<string, DataAppPreviewResult> = {};
      for (const ds of app.datasets) {
        try {
          out[ds.id] = await api.previewDataAppDataset(appId, ds.id, 50, filters);
        } catch {
          /* ignore individual dataset failure */
        }
      }
      const wout: Record<string, DataAppPreviewResult> = {};
      const wtiles = getSpecPanels(app.spec)
        .map((t) => getPanelRefId(t))
        .filter((wid): wid is string => Boolean(wid));
      for (const wid of wtiles) {
        try {
          wout[wid] = await api.previewWidget(wid, 50, filters);
        } catch {
          /* ignore */
        }
      }
      setPreviews(out);
      setWidgetPreviews(wout);
    } finally {
      setRefreshing(false);
    }
  };

  // 已发布只读页：自动拉取各数据集/图表数据
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
      const wout: Record<string, DataAppPreviewResult> = {};
      const wtiles = getSpecPanels(app.spec)
        .map((t) => getPanelRefId(t))
        .filter((wid): wid is string => Boolean(wid));
      for (const wid of wtiles) {
        try {
          wout[wid] = await api.previewWidget(wid);
        } catch {
          /* ignore individual widget failure */
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

  const previewByIndex = useMemo(() => {
    const byIndex: Record<number, DataAppPreviewResult> = {};
    app?.datasets.forEach((d, i) => {
      if (previews[d.id]) byIndex[i] = previews[d.id];
    });
    return byIndex;
  }, [app, previews]);

  if (loading) {
    return (
      <div className="bigscreen bigscreen-center">
        <Spin size="large" />
      </div>
    );
  }
  if (!app) {
    return (
      <div className="bigscreen bigscreen-center">
        <Empty description={<span style={{ color: "#7f9bc2" }}>数据应用不存在</span>} />
      </div>
    );
  }

  const isScreen = app.app_type === "screen";
  const isDashboard = app.app_type === "dashboard";
  const isCanvas = (app.spec?.layout as string) === "canvas" || isScreen;
  const tiles = getSpecPanels(app.spec);

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

  const copyEmbedLink = () => {
    const url = `${window.location.origin}/embed/apps/${app.id}`;
    void navigator.clipboard?.writeText(url);
    message.success("嵌入链接已复制（/embed/apps/…，可用于 iframe）");
  };

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen?.();
    } else {
      void document.documentElement.requestFullscreen?.();
    }
  };

  const typeLabel = isCanvas ? "数据看板·大屏" : "数据看板";

  return (
    <div className="bigscreen">
      {/* ---------- 顶部标题栏 ---------- */}
      <header className="bigscreen-header">
        <div className="bigscreen-header-left">
          <button
            className="bigscreen-btn"
            onClick={() => navigate("/data-apps")}
            title="返回数据应用列表"
          >
            <ArrowLeftOutlined />
            返回
          </button>
          <span
            className={`bigscreen-tag${app.status === "published" ? "" : " bigscreen-tag--draft"}`}
          >
            <span className="bigscreen-tag-dot" />
            {app.status === "published" ? `已发布 v${app.published_version}` : "草稿"}
          </span>
        </div>

        <div className="bigscreen-title-wrap">
          <h1 className="bigscreen-title">{app.name}</h1>
          <div className="bigscreen-subtitle">
            {app.description || `${typeLabel} · 实时数据展示`}
          </div>
        </div>

        <div className="bigscreen-header-right">
          <LiveClock />
          <Tooltip title="刷新数据">
            <button
              className="bigscreen-btn bigscreen-btn--icon"
              onClick={() => void loadData(buildRuntimeFilters(params, paramValues, drills))}
              style={{ width: 34 }}
            >
              <ReloadOutlined spin={refreshing} />
            </button>
          </Tooltip>
          {app.status === "published" && (
            <Tooltip title="复制嵌入链接（iframe）">
              <button
                className="bigscreen-btn bigscreen-btn--icon"
                onClick={copyEmbedLink}
                style={{ width: 34 }}
              >
                <LinkOutlined />
              </button>
            </Tooltip>
          )}
          <Tooltip title="全屏展示">
            <button
              className="bigscreen-btn bigscreen-btn--icon"
              onClick={toggleFullscreen}
              style={{ width: 34 }}
            >
              <FullscreenOutlined />
            </button>
          </Tooltip>
        </div>
      </header>

      {/* ---------- 内容区 ---------- */}
      <main className="bigscreen-body">
        {app.status !== "published" && (
          <div className="bigscreen-draft-note">当前应用尚未发布，此处展示的是草稿内容。</div>
        )}

        {(params.length > 0 || drills.length > 0) && (
          <div className="bigscreen-parambar">
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
          </div>
        )}

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
              onDrill={(_w, column, value) => {
                const next = [
                  ...drills.filter((d) => d.column !== column),
                  { column, value },
                ];
                setDrills(next);
                void loadData(buildRuntimeFilters(params, paramValues, next));
              }}
            />
          </div>
        ) : isDashboard || tiles.length > 0 ? (
          <DashboardGrid
            tiles={tiles}
            grid={app.spec?.grid as { cols?: number; rowHeight?: number; gap?: number }}
            theme={{
              ...(app.spec?.theme as { bg?: string; accent?: string; preset?: string }),
              preset: "dark",
              bg: "transparent",
            }}
            datasets={app.datasets.map((d) => ({ id: d.id, name: d.name }))}
            previews={previewByIndex}
            widgetPreviews={widgetPreviews}
            onDrill={(_tile, column, value) => {
              const next = [
                ...drills.filter((d) => d.column !== column),
                { column, value },
              ];
              setDrills(next);
              // 交叉过滤广播：刷新全部面板
              void loadData(buildRuntimeFilters(params, paramValues, next));
            }}
          />
        ) : (
          <div className="bigscreen-grid">
            {app.datasets.map((ds) => (
              <section className="bigscreen-panel" key={ds.id}>
                <div className="bigscreen-panel-head">{ds.name}</div>
                <div className="bigscreen-panel-body">{renderData(ds.id)}</div>
              </section>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
