import { useCallback, useRef, useState } from "react";
import { Card, Empty } from "antd";
import { BarChartRender, DataTableRender, KpiRender } from "./DataAppRenderer";
import type { DataAppPreviewResult } from "../types";

export interface ScreenWidget {
  id: string;
  type: string; // bar / kpi / table
  title?: string;
  datasetIndex?: number;
  rect: { x: number; y: number; w: number; h: number };
}

export interface ScreenCanvasProps {
  canvas: { width: number; height: number; bg?: string };
  widgets: ScreenWidget[];
  previews: Record<number, DataAppPreviewResult>;
  selectedId: string | null;
  editable?: boolean;
  onSelect?: (id: string | null) => void;
  onChange?: (widgets: ScreenWidget[]) => void;
  onCommit?: () => void;
  onDrill?: (widget: ScreenWidget, column: string, value: string) => void;
}

const DISPLAY_W = 960; // 画布展示宽度（等比缩放）

export function ScreenCanvas({
  canvas,
  widgets,
  previews,
  selectedId,
  editable = false,
  onSelect,
  onChange,
  onCommit,
  onDrill,
}: ScreenCanvasProps) {
  const scale = DISPLAY_W / canvas.width;
  const displayH = canvas.height * scale;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragState = useRef<{
    id: string;
    mode: "move" | "resize";
    startX: number;
    startY: number;
    orig: { x: number; y: number; w: number; h: number };
  } | null>(null);
  const [, force] = useState(0);

  const onPointerDown = useCallback(
    (e: React.PointerEvent, w: ScreenWidget, mode: "move" | "resize") => {
      if (!editable) return;
      e.stopPropagation();
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
      dragState.current = {
        id: w.id,
        mode,
        startX: e.clientX,
        startY: e.clientY,
        orig: { ...w.rect },
      };
      onSelect?.(w.id);
    },
    [editable, onSelect],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const st = dragState.current;
      if (!st || !onChange) return;
      const dx = (e.clientX - st.startX) / scale;
      const dy = (e.clientY - st.startY) / scale;
      const next = widgets.map((w) => {
        if (w.id !== st.id) return w;
        if (st.mode === "move") {
          return {
            ...w,
            rect: {
              ...w.rect,
              x: Math.max(0, Math.round(st.orig.x + dx)),
              y: Math.max(0, Math.round(st.orig.y + dy)),
            },
          };
        }
        return {
          ...w,
          rect: {
            ...w.rect,
            w: Math.max(120, Math.round(st.orig.w + dx)),
            h: Math.max(80, Math.round(st.orig.h + dy)),
          },
        };
      });
      onChange(next);
      force((n) => n + 1);
    },
    [onChange, scale, widgets],
  );

  const onPointerUp = useCallback(() => {
    if (dragState.current && onCommit) onCommit();
    dragState.current = null;
  }, [onCommit]);

  const renderWidget = (w: ScreenWidget) => {
    const preview = previews[w.datasetIndex ?? 0];
    if (!preview) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未预览" />;
    }
    const props = { columns: preview.columns, rows: preview.rows };
    if (w.type === "bar")
      return (
        <BarChartRender
          {...props}
          onBarClick={onDrill ? (col, val) => onDrill(w, col, val) : undefined}
        />
      );
    if (w.type === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <div
      ref={containerRef}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onClick={() => onSelect?.(null)}
      style={{
        position: "relative",
        width: DISPLAY_W,
        height: displayH,
        background: canvas.bg || "#0b1a2e",
        borderRadius: 12,
        overflow: "hidden",
        margin: "0 auto",
        boxShadow: "0 4px 24px rgba(0,0,0,0.2)",
      }}
    >
      {widgets.length === 0 && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#94a3b8",
          }}
        >
          画布为空，点击「添加组件」
        </div>
      )}
      {widgets.map((w) => {
        const selected = w.id === selectedId;
        return (
          <div
            key={w.id}
            onPointerDown={(e) => onPointerDown(e, w, "move")}
            onClick={(e) => {
              e.stopPropagation();
              onSelect?.(w.id);
            }}
            style={{
              position: "absolute",
              left: w.rect.x * scale,
              top: w.rect.y * scale,
              width: w.rect.w * scale,
              height: w.rect.h * scale,
              cursor: editable ? "move" : "default",
              outline: selected ? "2px solid #3b82f6" : "1px solid rgba(148,163,184,0.3)",
              borderRadius: 8,
              background: "#ffffff",
              overflow: "hidden",
            }}
          >
            <Card
              size="small"
              title={w.title || w.type}
              styles={{ body: { padding: 8, height: "calc(100% - 38px)", overflow: "auto" } }}
              style={{ height: "100%", border: "none" }}
            >
              {renderWidget(w)}
            </Card>
            {editable && (
              <div
                onPointerDown={(e) => onPointerDown(e, w, "resize")}
                style={{
                  position: "absolute",
                  right: 0,
                  bottom: 0,
                  width: 14,
                  height: 14,
                  cursor: "nwse-resize",
                  background: "#3b82f6",
                  borderTopLeftRadius: 6,
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

let widgetSeq = 0;
export function newWidget(type: string, datasetIndex = 0): ScreenWidget {
  widgetSeq += 1;
  return {
    id: `w${Date.now()}_${widgetSeq}`,
    type,
    title: type === "bar" ? "柱状图" : type === "kpi" ? "指标卡" : "表格",
    datasetIndex,
    rect: { x: 60 + widgetSeq * 20, y: 60 + widgetSeq * 20, w: 640, h: 360 },
  };
}

/** 看板 canvas 面板（Panel）与大屏组件（ScreenWidget）互转，便于 canvas 布局复用本组件。 */
export interface CanvasPanelLike {
  id: string;
  widgetType?: string;
  type?: string;
  title?: string;
  datasetIndex?: number;
  panel_id?: string;
  widget_id?: string;
  rect?: { x: number; y: number; w: number; h: number };
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

export function panelToScreenWidget(p: CanvasPanelLike): ScreenWidget {
  return {
    id: p.id,
    type: p.widgetType || p.type || "table",
    title: p.title,
    datasetIndex: p.datasetIndex ?? 0,
    rect: p.rect ?? { x: 60, y: 60, w: 640, h: 360 },
  };
}

export function applyWidgetToPanel(w: ScreenWidget, base?: CanvasPanelLike): CanvasPanelLike {
  return {
    ...(base ?? {}),
    id: w.id,
    widgetType: w.type,
    title: w.title,
    datasetIndex: w.datasetIndex,
    rect: w.rect,
  };
}

/** 导出行数据为 CSV 并触发下载 */
export function exportCsv(
  filename: string,
  columns: { key: string; title: string }[],
  rows: Record<string, unknown>[],
) {
  const esc = (v: unknown) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = columns.map((c) => esc(c.title)).join(",");
  const body = rows.map((r) => columns.map((c) => esc(r[c.key])).join(",")).join("\n");
  const csv = `\ufeff${header}\n${body}`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${filename}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
