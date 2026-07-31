import type { EdgeData, EdgeOptions } from "@antv/g6";
import { EDGE_COLORS, GROUPED_EDGE_COLOR } from "./theme";

/** 详情模式：关系边，箭头 + 居中文字标签，双向关系用双箭头表示。 */
export const detailEdgeOptions: EdgeOptions = {
  type: "line",
  style: {
    stroke: EDGE_COLORS.stroke,
    lineWidth: 1.5,
    cursor: "pointer", // 可点击的鼠标提示，与节点/版块一致，暗示边可点进关系详情
    endArrow: true,
    endArrowType: "triangle",
    endArrowSize: 8,
    startArrowType: "triangle",
    startArrowSize: 8,
    labelFontSize: 11,
    labelFontWeight: 500,
    labelFill: EDGE_COLORS.label,
    labelCursor: "pointer",
    labelBackground: true,
    labelBackgroundFill: EDGE_COLORS.labelBg,
    labelBackgroundOpacity: 0.95,
    labelBackgroundRadius: 5,
    labelPadding: [2, 6],
  },
  state: {
    // 悬停：加粗描边 + 标签链接色/加粗，读作「这是可点击的链接」。
    hover: {
      stroke: EDGE_COLORS.hoverStroke,
      lineWidth: 2.5,
      halo: true,
      labelFill: EDGE_COLORS.hoverStroke,
      labelFontWeight: 700,
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
