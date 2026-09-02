import type { NodeData, NodeOptions } from "@antv/g6";
import {
  EXTERNAL_COLORS,
  HUB_COLORS,
  NODE_COMPACT_HEIGHT,
  NODE_COMPACT_WIDTH,
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
  /** 副标题：物理表名等次要标识，填充卡片、提升信息密度 */
  subLabel?: string;
  /** 板块视图：本节点在板块之外，虚线弱化，让「板块内 vs 板块外」一眼分得开 */
  external?: boolean;
  /** 外部节点与当前板块的连接条数（徽标） */
  linkCount?: number;
  /** 外部节点所属板块名（徽标），答「连到哪块业务去了」 */
  externalGroup?: string | null;
  /** 紧凑卡片：只画名字，用于一屏塞下整个模块的关系图 */
  compact?: boolean;
}

function datum(data: NodeData): OntologyNodeDatum {
  return (data.data ?? {}) as unknown as OntologyNodeDatum;
}

/** 详情模式的对象节点：卡片(标题 + 状态徽标)，复刻原 OntologyGraphNode.tsx 视觉。 */
export const ontologyNodeOptions: NodeOptions = {
  type: "rect",
  style: (data) => {
    const {
      label,
      status,
      isCenter,
      kind,
      degree,
      subLabel,
      external,
      linkCount,
      externalGroup,
      compact,
    } = datum(data);
    if (compact) {
      // 紧凑卡片：名字撑满，状态退成描边色。外部邻居用虚线 + 灰调，仍然一眼分得开。
      const colors = statusColors(status);
      return {
        size: [NODE_COMPACT_WIDTH, NODE_COMPACT_HEIGHT],
        radius: 6,
        fill: external ? EXTERNAL_COLORS.bg : isCenter ? NODE_COLORS.centerBgFrom : NODE_COLORS.bg,
        stroke: external
          ? EXTERNAL_COLORS.border
          : isCenter
            ? NODE_COLORS.centerBorder
            : colors.border,
        lineWidth: isCenter ? 1.6 : 1,
        lineDash: external ? [4, 3] : undefined,
        cursor: "pointer",
        labelText: external && linkCount ? `${label} ×${linkCount}` : label,
        labelPlacement: "center",
        labelFontSize: 13,
        labelFontWeight: isCenter ? 700 : 500,
        labelFill: external
          ? EXTERNAL_COLORS.title
          : isCenter
            ? NODE_COLORS.centerTitle
            : NODE_COLORS.title,
        labelWordWrap: true,
        labelWordWrapWidth: NODE_COMPACT_WIDTH - 12,
        labelMaxLines: 1,
        labelTextOverflow: "ellipsis",
        port: false,
      };
    }
    if (external) {
      // 板块外的邻居：虚线描边 + 灰底，读作「这不是本块的成员，只是本块连出去的地方」。
      // 徽标写「N 条 · 所属板块」，把跨板块关系的量和去向压进同一张卡。
      return {
        size: [NODE_WIDTH, NODE_HEIGHT],
        radius: 10,
        fill: EXTERNAL_COLORS.bg,
        stroke: EXTERNAL_COLORS.border,
        lineWidth: 1,
        lineDash: [4, 3],
        cursor: "pointer",
        labelText: label,
        labelPlacement: "center",
        labelOffsetY: -13,
        labelFontSize: 13,
        labelFontWeight: 600,
        labelFill: EXTERNAL_COLORS.title,
        labelWordWrap: true,
        labelWordWrapWidth: NODE_WIDTH - 24,
        labelMaxLines: 2,
        labelTextOverflow: "ellipsis",
        badge: true,
        badges: [
          {
            text: externalGroup
              ? `${linkCount ?? 0} 条 · ${externalGroup}`
              : `${linkCount ?? 0} 条 · 板块外`,
            placement: "bottom",
            offsetY: -15,
            fill: EXTERNAL_COLORS.badgeText,
            fontSize: 11,
            fontWeight: 500,
            padding: [1, 8],
            backgroundFill: EXTERNAL_COLORS.badgeBg,
            backgroundRadius: 999,
          },
        ],
        port: false,
      };
    }
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
    // 标题 + 物理表名副标题（两者不同才拼），让卡片承载更多信息、减少留白。
    const memberLabel = subLabel && subLabel !== label ? `${label}\n${subLabel}` : label;
    return {
      size: [NODE_WIDTH, NODE_HEIGHT],
      radius: 10,
      fill: isCenter ? NODE_COLORS.centerBgFrom : NODE_COLORS.bg,
      stroke: isCenter ? NODE_COLORS.centerBorder : NODE_COLORS.border,
      lineWidth: isCenter ? 1.5 : 1,
      shadowColor: "rgba(15, 23, 42, 0.12)",
      shadowBlur: isCenter ? 14 : 6,
      cursor: "pointer",
      labelText: memberLabel,
      labelPlacement: "center",
      labelOffsetY: -13,
      labelFontSize: 13,
      labelFontWeight: 600,
      labelFill: isCenter ? NODE_COLORS.centerTitle : NODE_COLORS.title,
      labelWordWrap: true,
      labelWordWrapWidth: NODE_WIDTH - 24,
      labelMaxLines: 2,
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
    // 概览 hover 联动：高亮当前簇/枢纽的相关节点，压暗其余，避免一屏噪声。
    active: {
      stroke: NODE_COLORS.hoverBorder,
      lineWidth: 2,
      shadowBlur: 18,
      shadowColor: "rgba(37, 99, 235, 0.35)",
    },
    dimmed: {
      opacity: 0.2,
      labelOpacity: 0.35,
    },
  },
};
