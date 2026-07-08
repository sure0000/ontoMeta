import {
  ApartmentOutlined,
  CompressOutlined,
  LayoutOutlined,
  ShareAltOutlined,
} from "@ant-design/icons";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Button, Space, Tooltip as AntTooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { OntologyGraph } from "../../types";
import { OntologyGraphNode, type OntologyNodeData } from "./OntologyGraphNode";
import { applyLayout, type LayoutMode } from "./layout";

const nodeTypes = { ontology: OntologyGraphNode };

export interface OntologyGraphViewProps {
  graph: OntologyGraph;
  height?: number;
  centerNodeId?: string;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  onEdgeClick?: (edge: OntologyGraph["edges"][number]) => void;
  hint?: string;
  defaultLayout?: LayoutMode;
  /** 嵌入 SectionCard / Tabs 时使用，去除外层重复边框 */
  embedded?: boolean;
}

function resolveRelationId(edge: OntologyGraph["edges"][number]): string {
  return edge.relationId || edge.relation_id || edge.id.replace(/^in-/, "");
}

function buildFlowElements(
  graph: OntologyGraph,
  layoutMode: LayoutMode,
  centerNodeId?: string,
): { nodes: Node[]; edges: Edge[] } {
  const positions = applyLayout(graph, layoutMode, centerNodeId);
  const positionMap = Object.fromEntries(positions.map((p) => [p.id, p]));

  const nodes: Node[] = graph.nodes.map((node) => ({
    id: node.id,
    type: "ontology",
    position: positionMap[node.id] ?? { x: 0, y: 0 },
    data: {
      label: node.display_name,
      status: node.status,
      isCenter: node.id === centerNodeId,
    } satisfies OntologyNodeData,
    draggable: true,
  }));

  const edges: Edge[] = graph.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: edge.cardinality ? `${edge.label} (${edge.cardinality})` : edge.label,
    markerEnd: { type: MarkerType.ArrowClosed, color: "#94a3b8" },
    style: { stroke: "#94a3b8", strokeWidth: 1.5 },
    labelStyle: { fill: "#475569", fontSize: 11, fontWeight: 500 },
    labelBgStyle: { fill: "#ffffff", fillOpacity: 0.95 },
    labelBgPadding: [4, 6] as [number, number],
    labelBgBorderRadius: 5,
    data: { graphEdge: edge },
    animated: false,
  }));

  return { nodes, edges };
}

// 使用 React.memo 包裹，避免父组件渲染但 props 引用稳定时，
// 整个 ReactFlow 子树被无谓地重新渲染。
function OntologyGraphViewInner({
  graph,
  height = 520,
  centerNodeId,
  objectDetailPath,
  relationDetailPath,
  onEdgeClick,
  hint,
  defaultLayout,
  embedded = false,
}: OntologyGraphViewProps) {
  const navigate = useNavigate();
  const flowRef = useRef<ReactFlowInstance | null>(null);
  const initialLayout = defaultLayout ?? (centerNodeId ? "center" : "dagre");
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(initialLayout);
  const [layoutVersion, setLayoutVersion] = useState(0);

  const { nodes, edges } = useMemo(
    () => buildFlowElements(graph, layoutMode, centerNodeId),
    [graph, layoutMode, centerNodeId, layoutVersion],
  );

  const [flowNodes, setFlowNodes] = useState<Node[]>(nodes);
  const [flowEdges, setFlowEdges] = useState<Edge[]>(edges);

  useEffect(() => {
    setFlowNodes(nodes);
    setFlowEdges(edges);
    requestAnimationFrame(() => flowRef.current?.fitView({ padding: 0.15, duration: 300 }));
  }, [nodes, edges]);

  const handleLayout = useCallback(
    (mode: LayoutMode) => {
      setLayoutMode(mode);
      setLayoutVersion((v) => v + 1);
    },
    [],
  );

  const handleFitView = useCallback(() => {
    flowRef.current?.fitView({ padding: 0.15, duration: 300 });
  }, []);

  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      if (objectDetailPath) {
        navigate(objectDetailPath(node.id));
      }
    },
    [navigate, objectDetailPath],
  );

  const handleEdgeClick = useCallback(
    (_: React.MouseEvent, edge: Edge) => {
      const graphEdge = edge.data?.graphEdge as OntologyGraph["edges"][number] | undefined;
      if (!graphEdge) return;
      if (onEdgeClick) {
        onEdgeClick(graphEdge);
        return;
      }
      if (relationDetailPath) {
        navigate(relationDetailPath(resolveRelationId(graphEdge)));
      }
    },
    [navigate, onEdgeClick, relationDetailPath],
  );

  const edgeClickEnabled = Boolean(onEdgeClick || relationDetailPath);
  const defaultHint = edgeClickEnabled
    ? "拖拽节点重排 · 点击节点查看详情 · 点击关系边跳转编辑"
    : "拖拽节点重排 · 点击节点查看详情";

  const layoutButtons = (
    <Space size={4}>
      <AntTooltip title="层级布局">
        <Button
          size="small"
          type={layoutMode === "dagre" ? "primary" : "text"}
          icon={<LayoutOutlined />}
          onClick={() => handleLayout("dagre")}
        />
      </AntTooltip>
      {centerNodeId && (
        <AntTooltip title="中心辐射布局">
          <Button
            size="small"
            type={layoutMode === "center" ? "primary" : "text"}
            icon={<ShareAltOutlined />}
            onClick={() => handleLayout("center")}
          />
        </AntTooltip>
      )}
      <AntTooltip title="环形布局">
        <Button
          size="small"
          type={layoutMode === "circular" ? "primary" : "text"}
          icon={<ApartmentOutlined />}
          onClick={() => handleLayout("circular")}
        />
      </AntTooltip>
      <span className="toolbar-divider" />
      <AntTooltip title="适应画布">
        <Button size="small" type="text" icon={<CompressOutlined />} onClick={handleFitView} />
      </AntTooltip>
    </Space>
  );

  return (
    <div
      className={`ontology-graph-view${embedded ? " ontology-graph-view--embedded" : ""}`}
      style={{ height }}
    >
      <div className="ontology-graph-toolbar">
        <span className="ontology-graph-hint">{hint ?? defaultHint}</span>
        {layoutButtons}
      </div>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodesChange={(changes: NodeChange[]) => {
          setFlowNodes((current) => applyNodeChanges(changes, current));
        }}
        onEdgesChange={() => undefined}
        onInit={(instance) => {
          flowRef.current = instance;
          instance.fitView({ padding: 0.15 });
        }}
        onNodeClick={handleNodeClick}
        onEdgeClick={edgeClickEnabled ? handleEdgeClick : undefined}
        nodesConnectable={false}
        nodesDraggable
        elementsSelectable
        panOnScroll
        zoomOnScroll
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} size={1} color="#e2e8f0" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export const OntologyGraphView = memo(OntologyGraphViewInner);
