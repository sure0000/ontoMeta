import { Segmented } from "antd";
import { memo, type ReactNode } from "react";
import type { OntologyGraph, OntologyGroupedGraph } from "../../types";
import { OntologyDetailGraph } from "./OntologyDetailGraph";
import { OntologyOverviewGraph } from "./OntologyOverviewGraph";
import type { GraphMode } from "./OntologyGraphView";

export interface OntologyGraphSwitcherProps {
  /** 详情模式数据 */
  graph: OntologyGraph;
  /** 概览模式数据 */
  groupedGraph?: OntologyGroupedGraph | null;
  groupedGraphLoading?: boolean;
  /** 固定像素高度。**省略则填满 flex 父容器**（父容器需为限定高度的 flex column）。 */
  height?: number;
  centerNodeId?: string;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  onEdgeClick?: (edge: OntologyGraph["edges"][number]) => void;
  /** 双击节点展开邻域 */
  onExpandNode?: (objectId: string) => void;
  expanding?: boolean;
  /** 双击概览版块下钻：打开该聚类的邻接矩阵 */
  onClusterDrillIn?: (clusterId: string) => void;
  /** 工具条左侧内容。传节点即可把标题/计数/控件并进这一行，避免再叠一层卡片头。 */
  hint?: ReactNode;
  /** 嵌入 SectionCard / Tabs 时使用，去除外层重复边框 */
  embedded?: boolean;
  graphMode?: GraphMode;
  onGraphModeChange?: (mode: GraphMode) => void;
}

/**
 * 混合图组件包装器：概览模式使用 Cosmograph（高性能 WebGL），详情模式使用 G6（交互丰富）。
 *
 * 这个组件是 OntologyGraphView 的替代品，采用"分而治之"策略：
 * - 概览：Cosmograph 处理大规模图（100+ 节点），WebGL 渲染，性能极致
 * - 详情：G6 处理小邻域图（10-50 节点），保留丰富的交互能力
 */
function OntologyGraphSwitcherInner({
  graph,
  groupedGraph,
  groupedGraphLoading: _groupedGraphLoading = false,
  height,
  centerNodeId,
  objectDetailPath,
  relationDetailPath,
  onEdgeClick,
  onExpandNode,
  expanding = false,
  onClusterDrillIn,
  hint,
  embedded = false,
  graphMode = "detail",
  onGraphModeChange,
}: OntologyGraphSwitcherProps) {
  const isOverview = graphMode === "overview";

  const modeSwitcher = onGraphModeChange ? (
    <Segmented
      size="small"
      value={graphMode}
      onChange={(value) => onGraphModeChange(value as GraphMode)}
      options={[
        { label: "概览", value: "overview" },
        { label: "详情", value: "detail" },
      ]}
    />
  ) : null;

  return (
    <div style={{ position: "relative", height: height ?? "100%" }}>
      {modeSwitcher && (
        <div
          style={{
            position: "absolute",
            top: 8,
            right: 120,
            zIndex: 10,
          }}
        >
          {modeSwitcher}
        </div>
      )}
      {isOverview && groupedGraph ? (
        <OntologyOverviewGraph
          graph={groupedGraph}
          height={height}
          objectDetailPath={objectDetailPath}
          onClusterDrillIn={onClusterDrillIn}
          embedded={embedded}
        />
      ) : (
        <OntologyDetailGraph
          graph={graph}
          height={height}
          centerNodeId={centerNodeId}
          objectDetailPath={objectDetailPath}
          relationDetailPath={relationDetailPath}
          onEdgeClick={onEdgeClick}
          onExpandNode={onExpandNode}
          expanding={expanding}
          hint={hint}
          embedded={embedded}
        />
      )}
    </div>
  );
}

export const OntologyGraphSwitcher = memo(OntologyGraphSwitcherInner);
