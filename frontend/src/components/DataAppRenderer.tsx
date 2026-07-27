import { Table, Empty, Typography } from "antd";
import type { DataAppColumn } from "../types";

const { Text } = Typography;

export interface RenderRows {
  columns: DataAppColumn[];
  rows: Record<string, unknown>[];
}

/** 数据表格渲染 */
export function DataTableRender({ columns, rows }: RenderRows) {
  if (!columns.length) {
    return <Empty description="暂无数据集列，请先配置数据集" />;
  }
  const antColumns = columns.map((c) => ({
    title: c.title,
    dataIndex: c.key,
    key: c.key,
    render: (v: unknown) => String(v ?? ""),
  }));
  const dataSource = rows.map((r, i) => ({ key: i, ...r }));
  return (
    <Table
      size="small"
      columns={antColumns}
      dataSource={dataSource}
      pagination={{ pageSize: 10, hideOnSinglePage: true }}
    />
  );
}

/** 轻量 SVG 柱状图（无第三方图表依赖），取首个维度列为 x、首个度量列为 y */
export function BarChartRender({
  columns,
  rows,
  onBarClick,
}: RenderRows & { onBarClick?: (column: string, value: string) => void }) {
  if (!columns.length || !rows.length) {
    return <Empty description="暂无数据" />;
  }
  // 约定：第一列为维度(x)，最后一列为度量(y)
  const xKey = columns[0].key;
  const yKey = columns[columns.length - 1].key;
  const points = rows.slice(0, 12).map((r) => ({
    label: String(r[xKey] ?? ""),
    value: Number(r[yKey] ?? 0) || 0,
  }));
  const max = Math.max(...points.map((p) => p.value), 1);
  const barW = 40;
  const gap = 24;
  const chartH = 260;
  const width = points.length * (barW + gap) + gap;
  const clickable = Boolean(onBarClick);

  return (
    <div style={{ overflowX: "auto" }}>
      <svg width={Math.max(width, 320)} height={chartH + 40}>
        {points.map((p, i) => {
          const h = Math.round((p.value / max) * chartH);
          const x = gap + i * (barW + gap);
          const y = chartH - h + 10;
          return (
            <g
              key={i}
              style={{ cursor: clickable ? "pointer" : "default" }}
              onClick={() => onBarClick?.(xKey, p.label)}
            >
              <rect x={x} y={y} width={barW} height={h} rx={4} fill="var(--om-primary)">
                {clickable && <title>点击下钻：{p.label}</title>}
              </rect>
              <text
                x={x + barW / 2}
                y={y - 6}
                textAnchor="middle"
                fontSize={11}
                fill="var(--om-text-secondary)"
              >
                {p.value}
              </text>
              <text
                x={x + barW / 2}
                y={chartH + 28}
                textAnchor="middle"
                fontSize={11}
                fill="var(--om-text-tertiary)"
              >
                {p.label.length > 8 ? `${p.label.slice(0, 8)}…` : p.label}
              </text>
            </g>
          );
        })}
      </svg>
      <div style={{ marginTop: 4 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          x：{columns[0].title} ・ y：{columns[columns.length - 1].title}
        </Text>
      </div>
    </div>
  );
}

/** KPI 单值 */
export function KpiRender({ columns, rows }: RenderRows) {
  const key = columns.length ? columns[columns.length - 1].key : null;
  const title = columns.length ? columns[columns.length - 1].title : "指标";
  const value =
    key && rows.length
      ? rows.reduce((acc, r) => acc + (Number(r[key] ?? 0) || 0), 0)
      : 0;
  return (
    <div style={{ textAlign: "center", padding: "24px 0" }}>
      <div style={{ fontSize: 40, fontWeight: 700, color: "var(--om-primary)" }}>
        {value.toLocaleString()}
      </div>
      <Text type="secondary">{title}</Text>
    </div>
  );
}
