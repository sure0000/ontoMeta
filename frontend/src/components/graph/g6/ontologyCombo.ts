import type { ComboData, ComboOptions } from "@antv/g6";
import { comboColors } from "./theme";

export interface OntologyComboDatum {
  name: string;
  nodeCount: number;
  truncated: boolean;
  /** 聚类在颜色板中的序号,用于区分不同分块 */
  colorIndex: number;
}

function datum(data: ComboData): OntologyComboDatum {
  return (data.data ?? {}) as unknown as OntologyComboDatum;
}

/** 概览模式的聚类分组：虚线容器 + 名称/数量徽标，复刻原 OntologyGroupNode.tsx 视觉。整个组合可点击折叠/展开。 */
export const ontologyComboOptions: ComboOptions = {
  type: "rect",
  style: (data) => {
    const { name, nodeCount, truncated, colorIndex } = datum(data);
    const colors = comboColors(colorIndex);
    const collapsed = Boolean(data.style?.collapsed);
    const countText = truncated ? `${nodeCount}+` : `${nodeCount}`;
    return {
      fill: collapsed ? colors.bgCollapsed : colors.bg,
      stroke: colors.border,
      lineWidth: 2,
      lineDash: [4, 3],
      radius: 12,
      cursor: "pointer",
      padding: [36, 16, 16, 16],
      collapsedSize: [200, 44],
      labelText: `${collapsed ? "▸" : "▾"}  ${name}`,
      labelPlacement: "top",
      labelOffsetY: collapsed ? 0 : 8,
      labelFontSize: 13,
      labelFontWeight: 700,
      labelFill: colors.name,
      labelWordWrap: true,
      labelWordWrapWidth: 160,
      labelMaxLines: 1,
      labelTextOverflow: "ellipsis",
      badge: true,
      badges: [
        {
          text: countText,
          placement: "top-right",
          offsetY: collapsed ? 0 : 8,
          fill: colors.countText,
          fontSize: 11,
          fontWeight: 600,
          padding: [1, 8],
          backgroundFill: colors.countBg,
          backgroundRadius: 999,
        },
      ],
    };
  },
  state: {
    hover: (data) => ({ stroke: comboColors(datum(data).colorIndex).borderHover }),
    dimmed: {
      opacity: 0.3,
    },
  },
};
