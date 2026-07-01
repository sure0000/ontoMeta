import dagre from "@dagrejs/dagre";
import type { OntologyGraph } from "../../types";

export type LayoutMode = "dagre" | "center" | "circular";

export interface LayoutPosition {
  id: string;
  x: number;
  y: number;
}

const NODE_WIDTH = 168;
const NODE_HEIGHT = 72;
const PADDING = 80;

export function getNodeSize() {
  return { width: NODE_WIDTH, height: NODE_HEIGHT };
}

export function layoutCircular(nodes: OntologyGraph["nodes"], width = 900, height = 520): LayoutPosition[] {
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) / 2 - PADDING - NODE_HEIGHT / 2;
  const count = nodes.length;

  if (count === 0) return [];
  if (count === 1) return [{ id: nodes[0].id, x: cx - NODE_WIDTH / 2, y: cy - NODE_HEIGHT / 2 }];

  return nodes.map((node, index) => {
    const angle = (2 * Math.PI * index) / count - Math.PI / 2;
    return {
      id: node.id,
      x: cx + radius * Math.cos(angle) - NODE_WIDTH / 2,
      y: cy + radius * Math.sin(angle) - NODE_HEIGHT / 2,
    };
  });
}

export function layoutCenterStar(
  nodes: OntologyGraph["nodes"],
  centerNodeId: string,
  width = 900,
  height = 520,
): LayoutPosition[] {
  const cx = width / 2;
  const cy = height / 2;
  const center = nodes.find((n) => n.id === centerNodeId);
  const neighbors = nodes.filter((n) => n.id !== centerNodeId);

  if (!center) return layoutCircular(nodes, width, height);
  if (neighbors.length === 0) {
    return [{ id: center.id, x: cx - NODE_WIDTH / 2, y: cy - NODE_HEIGHT / 2 }];
  }

  const radius = Math.min(width, height) / 2 - PADDING - NODE_HEIGHT / 2;
  const positions: LayoutPosition[] = [
    { id: center.id, x: cx - NODE_WIDTH / 2, y: cy - NODE_HEIGHT / 2 },
  ];

  neighbors.forEach((node, index) => {
    const angle = (2 * Math.PI * index) / neighbors.length - Math.PI / 2;
    positions.push({
      id: node.id,
      x: cx + radius * Math.cos(angle) - NODE_WIDTH / 2,
      y: cy + radius * Math.sin(angle) - NODE_HEIGHT / 2,
    });
  });

  return positions;
}

export function layoutDagre(
  graph: OntologyGraph,
  direction: "TB" | "LR" = "LR",
): LayoutPosition[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: direction, nodesep: 60, ranksep: 90, marginx: 40, marginy: 40 });

  graph.nodes.forEach((node) => {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });
  graph.edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  dagre.layout(g);

  return graph.nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      id: node.id,
      x: pos.x - NODE_WIDTH / 2,
      y: pos.y - NODE_HEIGHT / 2,
    };
  });
}

export function applyLayout(
  graph: OntologyGraph,
  mode: LayoutMode,
  centerNodeId?: string,
): LayoutPosition[] {
  if (mode === "center" && centerNodeId) {
    return layoutCenterStar(graph.nodes, centerNodeId);
  }
  if (mode === "circular") {
    return layoutCircular(graph.nodes);
  }
  return layoutDagre(graph);
}
