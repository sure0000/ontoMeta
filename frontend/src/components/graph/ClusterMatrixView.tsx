import { Empty, Space, Tag, Typography } from "antd";
import { useMemo } from "react";
import type { ClusterDetail, GraphEdge } from "../../types";

const { Text } = Typography;

// 单个矩阵最多渲染的成员数（成员已按度数降序，超出部分为低度数长尾）。
// 上限内单元格数 ≤ 120² ≈ 1.4 万，SVG 可从容渲染；真实最大簇约 114，正好落在上限内。
const MATRIX_MAX = 120;

// 关系结构类型 → 颜色 / 中文标签。颜色取自品牌色系，保持与全站一致。
const TYPE_COLOR: Record<string, string> = {
  foreign_key: "#2563eb", // 外键：主蓝
  derivation: "#7c3aed", // 血缘/加工：紫
  bridge_table: "#0d9488", // 桥接：青
  fact_table: "#d97706", // 事实：琥珀
  other: "#94a3b8",
};
const TYPE_LABEL: Record<string, string> = {
  foreign_key: "外键",
  derivation: "血缘/加工",
  bridge_table: "桥接",
  fact_table: "事实",
  other: "其他",
};
const colorOf = (t?: string | null) => TYPE_COLOR[t ?? "other"] ?? TYPE_COLOR.other;
const labelOf = (t?: string | null) => TYPE_LABEL[t ?? "other"] ?? t ?? "其他";
const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}…` : s);

export interface ClusterMatrixViewProps {
  detail: ClusterDetail;
  /** 点击有关系的单元格 */
  onRelationClick?: (edge: GraphEdge) => void;
}

/**
 * 稠密簇下钻：成员×成员邻接矩阵（行=关系源，列=关系目标）。
 * 有关系的格子按结构类型着色，避免节点-连线图在稠密簇里重叠成毛线球。
 * 纯内联 SVG（无第三方图表依赖），仿 DataAppRenderer 的手绘模式。
 */
export function ClusterMatrixView({ detail, onRelationClick }: ClusterMatrixViewProps) {
  const nodes = useMemo(() => detail.nodes.slice(0, MATRIX_MAX), [detail.nodes]);
  const truncated = detail.nodes.length > MATRIX_MAX;

  const { cells, typesPresent } = useMemo(() => {
    const index = new Map(nodes.map((n, i) => [n.id, i] as const));
    const map = new Map<string, GraphEdge[]>();
    const types = new Set<string>();
    for (const edge of detail.edges) {
      const i = index.get(edge.source);
      const j = index.get(edge.target);
      if (i == null || j == null) continue; // 端点被上限截断
      const key = `${i}-${j}`;
      const bucket = map.get(key);
      if (bucket) bucket.push(edge);
      else map.set(key, [edge]);
      types.add(edge.structure_type ?? "other");
    }
    return { cells: map, typesPresent: [...types] };
  }, [nodes, detail.edges]);

  if (!nodes.length) {
    return <Empty description="该聚类暂无成员" />;
  }

  const N = nodes.length;
  const cell = Math.max(9, Math.min(18, Math.floor(640 / N)));
  const labelCol = 150;
  const labelRow = 130;
  const gridW = N * cell;
  const gridH = N * cell;
  const width = labelCol + gridW + 16;
  const height = labelRow + gridH + 16;
  const clickable = Boolean(onRelationClick);

  const gridLines = [];
  for (let k = 0; k <= N; k++) {
    gridLines.push(
      <line
        key={`h${k}`}
        x1={labelCol}
        y1={labelRow + k * cell}
        x2={labelCol + gridW}
        y2={labelRow + k * cell}
        stroke="var(--om-border)"
        strokeWidth={0.5}
      />,
      <line
        key={`v${k}`}
        x1={labelCol + k * cell}
        y1={labelRow}
        x2={labelCol + k * cell}
        y2={labelRow + gridH}
        stroke="var(--om-border)"
        strokeWidth={0.5}
      />,
    );
  }

  return (
    <div>
      <Space size={16} style={{ marginBottom: 8, flexWrap: "wrap" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          行 = 关系源 · 列 = 关系目标 · 共 {detail.node_count} 个成员、{detail.edges.length} 条簇内关系
        </Text>
        {typesPresent.map((t) => (
          <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span
              style={{
                width: 11,
                height: 11,
                borderRadius: 2,
                background: colorOf(t),
                display: "inline-block",
              }}
            />
            <Text style={{ fontSize: 12 }}>{labelOf(t)}</Text>
          </span>
        ))}
      </Space>
      {truncated && (
        <div style={{ marginBottom: 8 }}>
          <Tag color="warning">
            成员较多，仅展示度数最高的前 {MATRIX_MAX} 个（共 {detail.node_count} 个）
          </Tag>
        </div>
      )}
      <div style={{ overflow: "auto", maxHeight: "70vh", border: "1px solid var(--om-border)" }}>
        <svg width={width} height={height} style={{ display: "block" }}>
          {/* 网格背景 */}
          <rect
            x={labelCol}
            y={labelRow}
            width={gridW}
            height={gridH}
            fill="var(--om-surface)"
          />
          {gridLines}
          {/* 对角线（自身），淡色占位，帮助读者定位 */}
          {nodes.map((_, i) => (
            <rect
              key={`diag${i}`}
              x={labelCol + i * cell}
              y={labelRow + i * cell}
              width={cell}
              height={cell}
              fill="var(--om-surface-muted)"
            />
          ))}
          {/* 有关系的单元格 */}
          {[...cells.entries()].map(([key, edges]) => {
            const [i, j] = key.split("-").map(Number);
            const e = edges[0];
            return (
              <rect
                key={key}
                x={labelCol + j * cell + 0.5}
                y={labelRow + i * cell + 0.5}
                width={cell - 1}
                height={cell - 1}
                rx={2}
                fill={colorOf(e.structure_type)}
                style={{ cursor: clickable ? "pointer" : "default" }}
                onClick={() => onRelationClick?.(e)}
              >
                <title>
                  {`${nodes[i].display_name} → ${nodes[j].display_name}\n${labelOf(
                    e.structure_type,
                  )} · ${e.label}${edges.length > 1 ? ` (共 ${edges.length} 条)` : ""}`}
                </title>
              </rect>
            );
          })}
          {/* 行标签（源） */}
          {nodes.map((n, i) => (
            <text
              key={`r${n.id}`}
              x={labelCol - 6}
              y={labelRow + i * cell + cell / 2}
              textAnchor="end"
              dominantBaseline="middle"
              fontSize={10}
              fill="var(--om-text-secondary)"
            >
              {trunc(n.display_name, 18)}
            </text>
          ))}
          {/* 列标签（目标），旋转避让 */}
          {nodes.map((n, j) => {
            const cx = labelCol + j * cell + cell / 2;
            const cy = labelRow - 6;
            return (
              <text
                key={`c${n.id}`}
                x={cx}
                y={cy}
                textAnchor="start"
                fontSize={10}
                fill="var(--om-text-tertiary)"
                transform={`rotate(-55 ${cx} ${cy})`}
              >
                {trunc(n.display_name, 16)}
              </text>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
