import type { ComboData, EdgeData, GraphData, NodeData } from "@antv/g6";
import type { GraphCluster, GraphPoint, OntologyGraph, OntologyGroupedGraph } from "../../../types";
import type { OntologyComboDatum } from "./ontologyCombo";
import type { OntologyNodeDatum } from "./ontologyNode";
import { NODE_HEIGHT, NODE_WIDTH } from "./theme";

// 未被聚类算法归入任何业务子域的孤立节点，统一折叠进一个虚拟"聚类"里展示，
// 与其他聚类一致地默认收起，避免几十上百个散点直接铺满画布。
export const ISOLATED_CLUSTER_ID = "__isolated__";
export const ISOLATED_CLUSTER_MAX_NODES = 50;

/** 把 isolated_nodes 折叠为一个虚拟聚类，让"默认收起、点击展开"对它们同样生效。 */
export function foldIsolatedIntoCluster(groupedGraph: OntologyGroupedGraph): OntologyGroupedGraph {
  if (groupedGraph.isolated_nodes.length === 0) return groupedGraph;
  const isolatedCluster: GraphCluster = {
    id: ISOLATED_CLUSTER_ID,
    name: "未分类节点",
    nodes: groupedGraph.isolated_nodes.slice(0, ISOLATED_CLUSTER_MAX_NODES),
    node_count: groupedGraph.isolated_nodes.length,
    truncated: groupedGraph.isolated_nodes.length > ISOLATED_CLUSTER_MAX_NODES,
  };
  return {
    ...groupedGraph,
    clusters: [...groupedGraph.clusters, isolatedCluster],
    isolated_nodes: [],
  };
}

function edgeLabel(edge: OntologyGraph["edges"][number]): string {
  return edge.cardinality ? `${edge.label} (${edge.cardinality})` : edge.label;
}

/** 详情模式：单个对象邻域图。同一对节点间若存在方向相反的两条关系，合并为一根双箭头线展示。 */
export function buildDetailData(graph: OntologyGraph, centerNodeId?: string): GraphData {
  const nodes: NodeData[] = graph.nodes.map((node) => ({
    id: node.id,
    data: {
      label: node.display_name,
      status: node.status,
      isCenter: node.id === centerNodeId,
    } satisfies OntologyNodeDatum,
  }));

  const pairKey = (edge: OntologyGraph["edges"][number]) => [edge.source, edge.target].sort().join("::");
  const groups = new Map<string, OntologyGraph["edges"]>();
  graph.edges.forEach((edge) => {
    const key = pairKey(edge);
    const group = groups.get(key);
    if (group) group.push(edge);
    else groups.set(key, [edge]);
  });

  const consumed = new Set<string>();
  const edges: EdgeData[] = [];
  graph.edges.forEach((edge) => {
    if (consumed.has(edge.id)) return;
    consumed.add(edge.id);

    const reverse = groups
      .get(pairKey(edge))
      ?.find((other) => other.id !== edge.id && other.source === edge.target && other.target === edge.source);
    if (reverse) consumed.add(reverse.id);

    const label = reverse
      ? edgeLabel(edge) === edgeLabel(reverse)
        ? edgeLabel(edge)
        : `${edgeLabel(edge)} / ${edgeLabel(reverse)}`
      : edgeLabel(edge);

    edges.push({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: "line",
      data: { graphEdge: edge, bidirectional: Boolean(reverse) },
      style: {
        labelText: label,
        startArrow: Boolean(reverse),
      },
    });
  });

  return { nodes, edges };
}

// 后端布局坐标近邻间距约 1 个单位，乘以此像素间距铺开成"地图"。
// 折叠态 combo 约 200px 宽，取略大于此的间距，让相邻版块默认不挤在一起。
export const OVERVIEW_SPACING = 340;

function scaled(point: GraphPoint | null | undefined): { x: number; y: number } | null {
  if (!point) return null;
  return { x: point.x * OVERVIEW_SPACING, y: point.y * OVERVIEW_SPACING };
}

/**
 * 每个（折叠后）聚类的稳定中心坐标（像素）。孤立虚拟聚类没有后端坐标，
 * 摆到所有真实版块包围盒的正下方。概览初始渲染与语义缩放增量展开共用同一套中心，
 * 保证收起/展开时版块始终落在同一位置。
 */
export function computeClusterCenters(
  groupedGraph: OntologyGroupedGraph,
): Map<string, { x: number; y: number }> {
  const realPoints = [
    ...groupedGraph.clusters.map((c) => scaled(c.layout)),
    ...groupedGraph.hub_nodes.map((h) => scaled(h.layout)),
  ].filter((p): p is { x: number; y: number } => p !== null);
  const maxY = realPoints.length ? Math.max(...realPoints.map((p) => p.y)) : 0;
  const isolatedCenter = { x: 0, y: maxY + OVERVIEW_SPACING };

  const centers = new Map<string, { x: number; y: number }>();
  for (const cluster of foldIsolatedIntoCluster(groupedGraph).clusters) {
    centers.set(
      cluster.id,
      scaled(cluster.layout) ?? (cluster.id === ISOLATED_CLUSTER_ID ? isolatedCenter : { x: 0, y: 0 }),
    );
  }
  return centers;
}

// 语义缩放展开时单个版块最多平铺的成员卡片数（与后端 _LOD_MEMBER_CAP 对应）。
// 版块成员已按度数降序，展开时优先展示最核心的对象；后端也按此上限预留了去重叠空间。
export const OVERVIEW_MEMBER_CAP = 24;

/** 展开的聚类：把成员节点在其中心周围排成网格，combo 自动包裹。 */
export function buildClusterMemberNodes(
  cluster: GraphCluster,
  center: { x: number; y: number },
): NodeData[] {
  const shown = cluster.nodes.slice(0, OVERVIEW_MEMBER_CAP);
  const count = shown.length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(count)));
  const rows = Math.ceil(count / cols);
  const cellW = NODE_WIDTH + 28;
  const cellH = NODE_HEIGHT + 40;
  const originX = center.x - ((cols - 1) * cellW) / 2;
  const originY = center.y - ((rows - 1) * cellH) / 2;
  return shown.map((member, i) => ({
    id: member.id,
    combo: cluster.id,
    data: {
      label: member.display_name,
      status: member.status,
      isCenter: false,
      kind: "member",
    } satisfies OntologyNodeDatum,
    style: {
      x: originX + (i % cols) * cellW,
      y: originY + Math.floor(i / cols) * cellH,
    },
  }));
}

/**
 * 概览模式的数据：域层级"地图"。
 * - `openClusterIds` 里的版块渲染为展开态（平铺成员网格）；其余折叠成色块；
 * - 枢纽作为深色主干常驻，孤立节点折入外围的虚拟聚类。
 *
 * 展开态直接固化进"初始数据"，配合上层"改动即整体重建画布"——这是 G6 v5.1 下唯一稳定的路径：
 * 增量 addNodeData/removeNodeData + draw() 会把渲染管线卡死（draw 的 promise 永不 resolve，
 * 并连带锁死后续所有缩放/相机操作）。布局完全由这里给出的坐标决定，G6 不跑力导向。
 */
export function buildOverviewData(
  groupedGraph: OntologyGroupedGraph,
  openClusterIds: ReadonlySet<string> = new Set(),
): GraphData {
  const centers = computeClusterCenters(groupedGraph);
  const folded = foldIsolatedIntoCluster(groupedGraph);
  const centerOf = (id: string) => centers.get(id) ?? { x: 0, y: 0 };
  const isOpen = (cluster: GraphCluster) => openClusterIds.has(cluster.id) && cluster.nodes.length > 0;

  const combos: ComboData[] = folded.clusters.map((cluster, index) => {
    const center = centerOf(cluster.id);
    return {
      id: cluster.id,
      data: {
        name: cluster.name,
        nodeCount: cluster.node_count,
        truncated: cluster.truncated,
        colorIndex: index,
      } satisfies OntologyComboDatum,
      // 展开态由成员节点撑开定位；折叠态没有子节点，直接把 combo 钉在稳定坐标上。
      style: isOpen(cluster) ? { collapsed: false } : { collapsed: true, x: center.x, y: center.y },
    };
  });

  // 展开版块的成员节点，按各自中心排成网格。
  const memberNodes: NodeData[] = folded.clusters
    .filter(isOpen)
    .flatMap((cluster) => buildClusterMemberNodes(cluster, centerOf(cluster.id)));

  // 枢纽主干节点：始终展示，钉在稳定坐标上。
  const hubNodes: NodeData[] = groupedGraph.hub_nodes.map((hub) => {
    const center = scaled(hub.layout) ?? { x: 0, y: 0 };
    return {
      id: hub.id,
      data: {
        label: hub.display_name,
        status: hub.status,
        kind: "hub",
        degree: hub.degree,
      } satisfies OntologyNodeDatum,
      style: { x: center.x, y: center.y },
    };
  });

  // 宏观边：默认压暗、不画箭头/文字，强弱交给 hover 高亮；端点既可能是 combo 也可能是枢纽节点。
  const edges: EdgeData[] = folded.edges.map((edge) => ({
    id: edge.id,
    source: edge.source_cluster_id,
    target: edge.target_cluster_id,
    type: "line",
    data: { groupedEdge: edge, weight: edge.weight },
    style: {
      lineWidth: Math.min(1 + edge.weight * 0.08, 3.5),
      opacity: Math.min(0.2 + edge.weight * 0.04, 0.75),
    },
  }));

  return { nodes: [...memberNodes, ...hubNodes], edges, combos };
}
