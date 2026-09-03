/**
 * 血缘图的共用几何：画布（可编辑）与扫描结果图（只读）共用同一套连线曲线和分层
 * 算法，两边画出来的图才是同一种图——同样的走线、同样的左上游右下游。
 */

export interface SimpleEdge {
  from: string;
  to: string;
}

/**
 * 上游在左、下游在右的贝塞尔。目标在左侧（回环、反向边）时给固定的大偏移，
 * 线才绕得开、不糊在节点上。
 */
export function curve(x1: number, y1: number, x2: number, y2: number) {
  const dx = x2 >= x1 ? Math.min(Math.max((x2 - x1) / 2, 30), 120) : 90;
  return `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/**
 * 最长路径分层：没有上游的在第 0 层，其余取「所有上游层号 + 1」的最大值。
 *
 * 迭代次数按节点数封顶——ERP 的血缘图**有环**（见 erp-lineage-is-cyclic-tangle），
 * 环上不收敛，硬跑会死循环；封顶后环里的节点各自停在某一层，图仍然画得出来。
 */
export function assignLayers(nodes: string[], edges: SimpleEdge[]) {
  const layer = new Map<string, number>();
  nodes.forEach((n) => layer.set(n, 0));
  for (let i = 0; i < nodes.length; i += 1) {
    let moved = false;
    edges.forEach((edge) => {
      const from = layer.get(edge.from) ?? 0;
      const to = layer.get(edge.to) ?? 0;
      if (to < from + 1) {
        layer.set(edge.to, from + 1);
        moved = true;
      }
    });
    if (!moved) break;
  }
  return layer;
}
