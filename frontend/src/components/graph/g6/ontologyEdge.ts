import type { EdgeData, EdgeOptions } from "@antv/g6";
import { EDGE_COLORS, GROUPED_EDGE_COLOR } from "./theme";

/** 详情模式：关系边，箭头 + 居中文字标签，双向关系用双箭头表示。 */
export const detailEdgeOptions: EdgeOptions = {
  type: "line",
  style: {
    stroke: EDGE_COLORS.stroke,
    lineWidth: 1.5,
    endArrow: true,
    endArrowType: "triangle",
    endArrowSize: 8,
    startArrowType: "triangle",
    startArrowSize: 8,
    labelFontSize: 11,
    labelFontWeight: 500,
    labelFill: EDGE_COLORS.label,
    labelBackground: true,
    labelBackgroundFill: EDGE_COLORS.labelBg,
    labelBackgroundOpacity: 0.95,
    labelBackgroundRadius: 5,
    labelPadding: [2, 6],
  },
  state: {
    hover: {
      stroke: EDGE_COLORS.hoverStroke,
      lineWidth: 2,
    },
  },
};

/** 概览模式：聚类间的聚合边，默认压暗、hover 高亮显示关系数。 */
export const overviewEdgeOptions: EdgeOptions = {
  type: "line",
  style: {
    stroke: GROUPED_EDGE_COLOR,
    endArrow: false,
    label: false,
  },
  state: {
    active: (data: EdgeData) => {
      const weight = (data.data?.weight as number | undefined) ?? 1;
      return {
        stroke: "#4338ca",
        lineWidth: Math.max(2, Math.min(1 + weight * 0.08, 3.5)),
        opacity: 1,
        endArrow: true,
        endArrowType: "triangle",
        endArrowSize: 8,
        label: true,
        labelText: `${weight} 条关系`,
        labelFontSize: 11,
        labelFontWeight: 600,
        labelFill: "#4338ca",
        labelBackground: true,
        labelBackgroundFill: EDGE_COLORS.labelBg,
        labelBackgroundOpacity: 0.95,
        labelBackgroundRadius: 5,
        labelPadding: [2, 6],
        zIndex: 10,
      };
    },
    dimmed: {
      opacity: 0.05,
    },
  },
};
