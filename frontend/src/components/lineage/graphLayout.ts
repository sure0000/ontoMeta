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

/** 带审核标签的边。``label`` 缺省表示「人手工连的」。 */
export interface LabelledEdge extends SimpleEdge {
  id: string;
  label?: string | null;
}

export interface ReviewGroup {
  id: string;
  /** 这一组是靠什么连起来的：键名（「人员编号」）或「手工连的」。 */
  label: string;
  /** 同一个标签裂成多个连通分量时的序号，从 1 开始；只有一个时为 0（不显示）。 */
  ordinal: number;
  tables: string[];
  edgeIds: string[];
}

/**
 * 把画布切成**可审核的组**：先按边的标签（键）分，再在每个标签内部求连通分量。
 *
 * **为什么不是直接求整张图的连通分量**：实测 jwsp 138 张表连出 200 条线，整张图是
 * **一个** 93 表的连通分量——每个键各自是一颗星，星与星共用枢纽表，于是全粘成一坨，
 * 「按连通图审核」什么也没分开。按键先分再求分量，同一批线立刻变成 9 组
 * （行政区划代码 73 表、人员编号 34 表、案件编号 16 表…），每组一个语义、
 * 一个要回答的问题：「这些表真的共用这个键吗」。
 *
 * 没有键的图（纯手工连的）只有一个标签，于是**退化成普通的连通分量**——
 * 这正是「按连通图审核」本来的意思。
 *
 * 一张表可以同时出现在几组里（它既在行政区划码那颗星上，也在人员编号那颗星上），
 * 这是对的：审的是「这一组线」，不是「这张表归谁」。
 */
export function reviewGroups(nodes: string[], edges: LabelledEdge[]) {
  const known = new Set(nodes);
  const byLabel = new Map<string, LabelledEdge[]>();
  for (const edge of edges) {
    if (!known.has(edge.from) || !known.has(edge.to)) continue;
    const label = edge.label || "手工连的";
    byLabel.set(label, [...(byLabel.get(label) ?? []), edge]);
  }

  const groups: ReviewGroup[] = [];
  const touched = new Set<string>();

  for (const [label, group] of byLabel) {
    const parent = new Map<string, string>();
    const find = (x: string): string => {
      parent.set(x, parent.get(x) ?? x);
      let root = x;
      while (parent.get(root) !== root) root = parent.get(root) as string;
      let cursor = x;
      while (parent.get(cursor) !== root) {
        const next = parent.get(cursor) as string;
        parent.set(cursor, root);
        cursor = next;
      }
      return root;
    };
    for (const edge of group) {
      const a = find(edge.from);
      const b = find(edge.to);
      if (a !== b) parent.set(a, b);
      touched.add(edge.from);
      touched.add(edge.to);
    }

    const buckets = new Map<string, { tables: Set<string>; edgeIds: string[] }>();
    for (const edge of group) {
      const root = find(edge.from);
      const bucket = buckets.get(root) ?? { tables: new Set<string>(), edgeIds: [] };
      bucket.tables.add(edge.from);
      bucket.tables.add(edge.to);
      bucket.edgeIds.push(edge.id);
      buckets.set(root, bucket);
    }

    const parts = [...buckets.entries()].sort(
      (a, b) => b[1].tables.size - a[1].tables.size,
    );
    parts.forEach(([root, bucket], index) => {
      groups.push({
        id: `${label}::${root}`,
        label,
        ordinal: parts.length > 1 ? index + 1 : 0,
        tables: [...bucket.tables].sort(),
        edgeIds: bucket.edgeIds,
      });
    });
  }

  // 线多的组排前面：审核从最大的那一坨开始最划算。
  groups.sort(
    (a, b) => b.edgeIds.length - a.edgeIds.length || a.label.localeCompare(b.label),
  );

  return { groups, loners: nodes.filter((n) => !touched.has(n)).sort() };
}
