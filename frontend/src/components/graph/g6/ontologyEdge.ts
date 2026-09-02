import type { EdgeData, EdgeOptions } from "@antv/g6";
import { EDGE_COLORS, GROUPED_EDGE_COLOR } from "./theme";

/** 详情模式：关系边，箭头 + 居中文字标签，双向关系用双箭头表示。 */
export const detailEdgeOptions: EdgeOptions = {
  type: "line",
  // 边多到标签会互相压住时默认藏起动词（阈值见 buildG6Data.EDGE_LABEL_LIMIT），
  // 悬浮聚焦时再逐条亮出来；边不多的邻域图照旧常显，动词正是要读的东西。
  style: (data: EdgeData) => {
    // 稠密图里 100+ 条实线会盖过对象名，先退成淡线只负责「有结构」这层信息，
    // 具体是哪条关系交给悬浮聚焦——名字读得出来，才谈得上读关系。
    const dense = Boolean(data.data?.hideLabel);
    return {
      stroke: EDGE_COLORS.stroke,
      lineWidth: dense ? 1 : 1.5,
      opacity: dense ? 0.4 : 1,
      cursor: "pointer", // 可点击的鼠标提示，与节点/版块一致，暗示边可点进关系详情
      endArrow: true,
      endArrowType: "triangle",
      endArrowSize: 8,
      startArrowType: "triangle",
      startArrowSize: 8,
      label: !dense,
      labelFontSize: 11,
      labelFontWeight: 500,
      labelFill: EDGE_COLORS.label,
      labelCursor: "pointer",
      labelBackground: true,
      labelBackgroundFill: EDGE_COLORS.labelBg,
      labelBackgroundOpacity: 0.95,
      labelBackgroundRadius: 5,
      labelPadding: [2, 6],
    };
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
    // 邻域聚焦：悬浮某个对象时，它的关系线亮起（并亮出动词标签）、其余压到近乎透明。
    // 这是稠密板块图唯一真正管用的读法——「这个对象连着谁、什么关系」一眼可答。
    active: {
      stroke: EDGE_COLORS.hoverStroke,
      lineWidth: 2.5,
      opacity: 1,
      label: true,
      labelFill: EDGE_COLORS.hoverStroke,
      labelFontWeight: 700,
      zIndex: 10,
    },
    dimmed: {
      opacity: 0.08,
      labelOpacity: 0,
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
