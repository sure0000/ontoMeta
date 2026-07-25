import { useMemo, type ComponentType } from "react";
import GridLayout from "react-grid-layout";
import { Card, Empty, Select } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "./DataAppRenderer";
import type { DataAppPreviewResult } from "../types";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

// react-grid-layout 为 CJS（export =），WidthProvider 挂在默认导出对象上。
const ResponsiveGrid = (
  GridLayout as unknown as { WidthProvider: (c: unknown) => ComponentType<Record<string, unknown>> }
).WidthProvider(GridLayout);

type RGLItem = { i: string; x: number; y: number; w: number; h: number; minW?: number; minH?: number };

export interface DashboardTile {
  id: string;
  widgetType: string; // table / bar / kpi
  title?: string;
  datasetIndex?: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface DashboardGridProps {
  tiles: DashboardTile[];
  grid?: { cols?: number; rowHeight?: number; gap?: number };
  datasets: { id: string; name: string }[];
  previews: Record<number, DataAppPreviewResult>;
  editable?: boolean;
  onLayoutChange?: (tiles: DashboardTile[]) => void;
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
  datasets,
  previews,
  editable = false,
  onLayoutChange,
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

  const handleLayoutChange = (next: RGLItem[]) => {
    if (!onLayoutChange) return;
    const byId = new Map(next.map((l) => [l.i, l]));
    onLayoutChange(
      tiles.map((t) => {
        const l = byId.get(t.id);
        return l ? { ...t, x: l.x, y: l.y, w: l.w, h: l.h } : t;
      }),
    );
  };

  const renderBody = (t: DashboardTile) => {
    const p = previews[t.datasetIndex ?? 0];
    if (!p) {
      return (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未预览数据" />
      );
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
        <Empty description={editable ? "看板为空，点击「添加图表」" : "看板暂无图表"} />
      </div>
    );
  }

  return (
    <ResponsiveGrid
      className="dashboard-grid"
      layout={layout}
      cols={cols}
      rowHeight={rowHeight}
      margin={[margin, margin]}
      isDraggable={editable}
      isResizable={editable}
      draggableHandle=".dashboard-tile-drag"
      onLayoutChange={handleLayoutChange as (l: unknown) => void}
    >
      {tiles.map((t) => (
        <div key={t.id}>
          <Card
            size="small"
            title={
              <span className={editable ? "dashboard-tile-drag" : undefined} style={{ cursor: editable ? "move" : "default" }}>
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
                  <Select
                    size="small"
                    style={{ width: 110 }}
                    value={t.datasetIndex ?? 0}
                    options={datasets.map((d, i) => ({ label: d.name, value: i }))}
                    onChange={(v) => onTilePatch?.(t.id, { datasetIndex: v })}
                  />
                  <DeleteOutlined
                    style={{ color: "#ef4444", cursor: "pointer" }}
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
