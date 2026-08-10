import { useMemo, type ComponentType } from "react";
// react-grid-layout v2 默认入口不再导出 WidthProvider；使用 legacy 子路径保留 v1 扁平 props API。
import { WidthProvider, Responsive } from "react-grid-layout/legacy";
import { Card, Empty, Select } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { BarChartRender, DataTableRender, KpiRender } from "./DataAppRenderer";
import {
  resolveDashboardTheme,
  dashboardThemeVars,
  type DashboardThemeSpec,
} from "./dashboardThemes";
import type { DataAppPreviewResult } from "../types";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

const ResponsiveGrid = (
  WidthProvider as unknown as (c: unknown) => ComponentType<Record<string, unknown>>
)(Responsive as unknown);

type RGLItem = {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
  minW?: number;
  minH?: number;
};

export interface DashboardTile {
  id: string;
  widgetType: string; // table / bar / kpi
  title?: string;
  datasetIndex?: number;
  panel_id?: string; // 引用可复用面板（Panel）资产（优先于 datasetIndex）
  widget_id?: string; // 旧字段，兼容读取
  rect?: { x: number; y: number; w: number; h: number }; // canvas 布局下的像素坐标
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 读取看板面板列表，兼容旧字段 spec.tiles。 */
export function getSpecPanels(spec: Record<string, unknown> | null | undefined): DashboardTile[] {
  if (!spec) return [];
  return ((spec.panels as DashboardTile[]) ??
    (spec.tiles as DashboardTile[]) ??
    []) as DashboardTile[];
}

/** 面板引用的可复用图表 ID，兼容旧字段 widget_id。 */
export function getPanelRefId(tile: { panel_id?: string; widget_id?: string }): string | undefined {
  return tile.panel_id ?? tile.widget_id;
}

export interface DashboardGridProps {
  tiles: DashboardTile[];
  grid?: { cols?: number; rowHeight?: number; gap?: number };
  theme?: DashboardThemeSpec;
  datasets: { id: string; name: string }[];
  previews: Record<number, DataAppPreviewResult>;
  widgetPreviews?: Record<string, DataAppPreviewResult>;
  editable?: boolean;
  onLayoutChange?: (tiles: DashboardTile[]) => void;
  onPersist?: (tiles: DashboardTile[]) => void;
  onTilePatch?: (id: string, patch: Partial<DashboardTile>) => void;
  onRemoveTile?: (id: string) => void;
  onDrill?: (tile: DashboardTile, column: string, value: string) => void;
}

const WIDGET_OPTIONS = [
  { label: "表格", value: "table" },
  { label: "柱状图", value: "bar" },
  { label: "指标卡", value: "kpi" },
];

export function DashboardGrid({
  tiles,
  grid,
  theme,
  datasets,
  previews,
  widgetPreviews,
  editable = false,
  onLayoutChange,
  onPersist,
  onTilePatch,
  onRemoveTile,
  onDrill,
}: DashboardGridProps) {
  const cols = grid?.cols ?? 12;
  const rowHeight = grid?.rowHeight ?? 40;
  const margin = grid?.gap ?? 12;

  const layout = useMemo<RGLItem[]>(
    () =>
      tiles.map((t) => ({
        i: t.id,
        x: t.x,
        y: t.y,
        w: t.w,
        h: t.h,
        minW: 2,
        minH: 4,
      })),
    [tiles],
  );

  const applyLayout = (next: RGLItem[]): DashboardTile[] => {
    const byId = new Map(next.map((l) => [l.i, l]));
    return tiles.map((t) => {
      const l = byId.get(t.id);
      return l ? { ...t, x: l.x, y: l.y, w: l.w, h: l.h } : t;
    });
  };

  const handleLayoutChange = (next: RGLItem[]) => {
    onLayoutChange?.(applyLayout(next));
  };

  // 拖拽/缩放松手即持久化布局（无需手动“保存布局”）
  const handleDragResizeStop = (next: RGLItem[]) => {
    onPersist?.(applyLayout(next));
  };

  const renderBody = (t: DashboardTile) => {
    const refId = getPanelRefId(t);
    const p = refId ? widgetPreviews?.[refId] : previews[t.datasetIndex ?? 0];
    if (!p) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未预览数据" />;
    }
    const props = { columns: p.columns, rows: p.rows };
    if (t.widgetType === "bar")
      return (
        <BarChartRender
          {...props}
          onBarClick={onDrill ? (col, val) => onDrill(t, col, val) : undefined}
        />
      );
    if (t.widgetType === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  if (tiles.length === 0) {
    return (
      <div style={{ padding: 40 }}>
        <Empty description={editable ? "看板为空，点击「添加面板」" : "看板暂无面板"} />
      </div>
    );
  }

  const rt = resolveDashboardTheme(theme);

  return (
    <div
      className={rt.dark ? "dashboard-canvas dashboard-canvas--dark" : "dashboard-canvas"}
      style={{ ...dashboardThemeVars(rt), padding: 12, borderRadius: 12 }}
    >
      <ResponsiveGrid
        className="dashboard-grid"
        layouts={{ lg: layout, md: layout, sm: layout, xs: layout }}
        breakpoints={{ lg: 1200, md: 900, sm: 640, xs: 0 }}
        cols={{ lg: cols, md: cols, sm: Math.max(2, Math.round(cols / 2)), xs: 1 }}
        rowHeight={rowHeight}
        margin={[margin, margin]}
        isDraggable={editable}
        isResizable={editable}
        draggableHandle=".dashboard-tile-drag"
        onLayoutChange={handleLayoutChange as (l: unknown) => void}
        onDragStop={((l: RGLItem[]) => handleDragResizeStop(l)) as (l: unknown) => void}
        onResizeStop={((l: RGLItem[]) => handleDragResizeStop(l)) as (l: unknown) => void}
      >
        {tiles.map((t) => (
          <div key={t.id}>
            <Card
              size="small"
              title={
                <span
                  className={editable ? "dashboard-tile-drag" : undefined}
                  style={{ cursor: editable ? "move" : "default" }}
                >
                  {t.title || t.widgetType}
                </span>
              }
              styles={{ body: { height: "calc(100% - 40px)", overflow: "auto", padding: 8 } }}
              style={{ height: "100%" }}
              extra={
                editable ? (
                  <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <Select
                      size="small"
                      style={{ width: 84 }}
                      value={t.widgetType}
                      options={WIDGET_OPTIONS}
                      onChange={(v) => onTilePatch?.(t.id, { widgetType: v })}
                    />
                    {getPanelRefId(t) ? (
                      <span style={{ fontSize: 11, color: "var(--om-success)", fontWeight: 500 }}>
                        面板库
                      </span>
                    ) : (
                      <Select
                        size="small"
                        style={{ width: 110 }}
                        value={t.datasetIndex ?? 0}
                        options={datasets.map((d, i) => ({ label: d.name, value: i }))}
                        onChange={(v) => onTilePatch?.(t.id, { datasetIndex: v })}
                      />
                    )}
                    <DeleteOutlined
                      style={{ color: "var(--om-error)", cursor: "pointer" }}
                      onClick={() => onRemoveTile?.(t.id)}
                    />
                  </span>
                ) : null
              }
            >
              {renderBody(t)}
            </Card>
          </div>
        ))}
      </ResponsiveGrid>
    </div>
  );
}

let tileSeq = 0;
export function newTile(widgetType: string, datasetIndex = 0, y = Infinity): DashboardTile {
  tileSeq += 1;
  return {
    id: `t${Date.now()}_${tileSeq}`,
    widgetType,
    title: widgetType === "bar" ? "柱状图" : widgetType === "kpi" ? "指标卡" : "表格",
    datasetIndex,
    x: 0,
    y: y === Infinity ? 9999 : y,
    w: 6,
    h: 8,
  };
}
