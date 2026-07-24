import type { NodeData, NodeOptions } from "@antv/g6";
import {
  HUB_COLORS,
  HUB_HEIGHT,
  HUB_WIDTH,
  NODE_COLORS,
  NODE_HEIGHT,
  NODE_WIDTH,
  statusColors,
  statusLabel,
} from "./theme";

export interface OntologyNodeDatum {
  label: string;
  status: string;
  isCenter?: boolean;
  /** hub = 概览图的主干枢纽节点（公共维度表），member = 聚类内的普通对象节点 */
  kind?: "hub" | "member";
  /** hub 节点的度数，作为徽标展示其连接广度 */
  degree?: number;
}

function datum(data: NodeData): OntologyNodeDatum {
  return (data.data ?? {}) as unknown as OntologyNodeDatum;
}

/** 详情模式的对象节点：卡片(标题 + 状态徽标)，复刻原 OntologyGraphNode.tsx 视觉。 */
export const ontologyNodeOptions: NodeOptions = {
  type: "rect",
  style: (data) => {
    const { label, status, isCenter, kind, degree } = datum(data);
    if (kind === "hub") {
      return {
        size: [HUB_WIDTH, HUB_HEIGHT],
        radius: 12,
        fill: HUB_COLORS.bg,
        stroke: HUB_COLORS.border,
        lineWidth: 1.5,
        shadowColor: "rgba(15, 23, 42, 0.28)",
        shadowBlur: 12,
        cursor: "pointer",
        labelText: label,
        labelPlacement: "center",
        labelOffsetY: -9,
        labelFontSize: 13,
        labelFontWeight: 700,
        labelFill: HUB_COLORS.title,
        labelWordWrap: true,
        labelWordWrapWidth: HUB_WIDTH - 24,
        labelMaxLines: 1,
        labelTextOverflow: "ellipsis",
        badge: true,
        badges: [
          {
            text: `枢纽 · ${degree ?? 0}`,
            placement: "bottom",
            offsetY: -16,
            fill: HUB_COLORS.badgeText,
            fontSize: 11,
            fontWeight: 600,
            padding: [1, 8],
            backgroundFill: HUB_COLORS.badgeBg,
            backgroundRadius: 999,
          },
        ],
        port: false,
      };
    }
    const colors = statusColors(status);
    return {
      size: [NODE_WIDTH, NODE_HEIGHT],
      radius: 10,
      fill: isCenter ? NODE_COLORS.centerBgFrom : NODE_COLORS.bg,
      stroke: isCenter ? NODE_COLORS.centerBorder : NODE_COLORS.border,
      lineWidth: isCenter ? 1.5 : 1,
      shadowColor: "rgba(15, 23, 42, 0.12)",
      shadowBlur: isCenter ? 14 : 6,
      cursor: "pointer",
      labelText: label,
      labelPlacement: "center",
      labelOffsetY: -11,
      labelFontSize: 13,
      labelFontWeight: 600,
      labelFill: isCenter ? NODE_COLORS.centerTitle : NODE_COLORS.title,
      labelWordWrap: true,
      labelWordWrapWidth: NODE_WIDTH - 24,
      labelMaxLines: 1,
      labelTextOverflow: "ellipsis",
      badge: true,
      badges: [
        {
          text: statusLabel(status),
          placement: "bottom",
          offsetY: -15,
          fill: colors.text,
          fontSize: 11,
          fontWeight: 500,
          padding: [1, 8],
          backgroundFill: colors.bg,
          backgroundStroke: colors.border,
          backgroundLineWidth: 1,
          backgroundRadius: 999,
        },
      ],
      port: false,
    };
  },
  state: {
    hover: {
      stroke: NODE_COLORS.hoverBorder,
      shadowBlur: 16,
      shadowColor: "rgba(37, 99, 235, 0.28)",
    },
  },
};
