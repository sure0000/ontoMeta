import {
  CompressOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  LayoutOutlined,
} from "@ant-design/icons";
import { Graph, type GraphOptions, type IElementEvent } from "@antv/g6";
import { Button, Segmented, Space, Tooltip as AntTooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { OntologyGraph, OntologyGroupedGraph } from "../../types";
import {
  buildDetailData,
  buildOverviewData,
  computeClusterCenters,
  foldIsolatedIntoCluster,
} from "./g6/buildG6Data";
import { detailEdgeOptions, overviewEdgeOptions } from "./g6/ontologyEdge";
import { ontologyComboOptions } from "./g6/ontologyCombo";
import { ontologyNodeOptions } from "./g6/ontologyNode";

export type GraphMode = "detail" | "overview";

export interface OntologyGraphViewProps {
  graph: OntologyGraph;
  height?: number;
  centerNodeId?: string;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  onEdgeClick?: (edge: OntologyGraph["edges"][number]) => void;
  /** 双击节点展开邻域 */
  onExpandNode?: (objectId: string) => void;
  expanding?: boolean;
  hint?: string;
  /** 嵌入 SectionCard / Tabs 时使用，去除外层重复边框 */
  embedded?: boolean;
  /** 域层级概览图数据；未提供时不展示 详情/概览 切换 */
  groupedGraph?: OntologyGroupedGraph | null;
  groupedGraphLoading?: boolean;
  graphMode?: GraphMode;
  onGraphModeChange?: (mode: GraphMode) => void;
}

function resolveRelationId(edge: OntologyGraph["edges"][number]): string {
  return edge.relationId || edge.relation_id || edge.id.replace(/^in-/, "");
}

// 概览重建前后的相机保持：捕获当前缩放 + 视口中心对应的世界坐标，重建后还原到同一处，
// 避免展开/收起版块时视图跳动。坐标换算全部走已验证可用的 getCanvasByViewport。
function captureCamera(g: Graph): { zoom: number; center: [number, number] } | null {
  try {
    const [w, h] = g.getSize();
    const c = g.getCanvasByViewport([w / 2, h / 2]);
    if (!c) return null;
    return { zoom: g.getZoom(), center: [c[0], c[1]] };
  } catch {
    return null;
  }
}

async function restoreCamera(g: Graph, cam: { zoom: number; center: [number, number] }) {
  try {
    await g.zoomTo(cam.zoom, false);
    const [w, h] = g.getSize();
    const cur = g.getCanvasByViewport([w / 2, h / 2]);
    if (!cur) return;
    // 当前视口中心的世界点是 cur，想让它变成 cam.center：内容需平移 (cur - target) * zoom 像素。
    const dx = (cur[0] - cam.center[0]) * cam.zoom;
    const dy = (cur[1] - cam.center[1]) * cam.zoom;
    if (dx || dy) await g.translateBy([dx, dy], false);
  } catch {
    void g.fitView();
  }
}

// 语义缩放（LoD）阈值：缩放低于此值只看版块色块（远景地图）；高于此值自动展开视口内的版块成员节点。
const LOD_OPEN_ZOOM = 0.42;
// 同时展开的版块数上限：增量渲染虽稳，但节点过多仍会拖慢；放大后视口本就只覆盖少数版块，取其中最大的若干个。
const LOD_MAX_OPEN_CLUSTERS = 12;
// 缩放/平移后延迟重算 LoD，避免连续滚轮/拖拽期间频繁增删节点。
const LOD_DEBOUNCE_MS = 200;

// 使用 React.memo 包裹，避免父组件渲染但 props 引用稳定时，整个 G6 画布被无谓地重新创建。
function OntologyGraphViewInner({
  graph,
  height = 520,
  centerNodeId,
  objectDetailPath,
  relationDetailPath,
  onEdgeClick,
  onExpandNode,
  expanding = false,
  hint,
  embedded = false,
  groupedGraph,
  groupedGraphLoading = false,
  graphMode = "detail",
  onGraphModeChange,
}: OntologyGraphViewProps) {
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const isOverview = graphMode === "overview";

  // 语义缩放（zoom→LoD）：当前已展开成员节点的聚类 id 集合。缩小到阈值以下自动全部收起，
  // 放大后自动展开视口内的版块。groupedGraph 换新数据时重置。
  const openClustersRef = useRef<Set<string>>(new Set());
  // 概览重建计数器：G6 v5.1 下增量改动会卡死管线，所以展开/收起版块一律整体重建画布。
  // 碰一下这个计数器即触发下面的销毁重建 effect。
  const [overviewRebuildTick, setOverviewRebuildTick] = useState(0);
  // 重建前捕获的相机（缩放 + 中心世界坐标）；重建后据此还原，避免视图跳回 fitView。
  // 为 null 时表示是初次构建（或换数据），走 fitView。
  const pendingCameraRef = useRef<{ zoom: number; center: [number, number] } | null>(null);
  useEffect(() => {
    openClustersRef.current = new Set();
    pendingCameraRef.current = null;
  }, [groupedGraph]);

  // 供事件监听器读取的最新聚类中心坐标 / 按 id 索引的聚类，避免重新绑定监听器。
  const lodData = useMemo(() => {
    if (!groupedGraph) {
      return {
        centers: new Map<string, { x: number; y: number }>(),
        clustersById: new Map<string, OntologyGroupedGraph["clusters"][number]>(),
      };
    }
    const centers = computeClusterCenters(groupedGraph);
    const clustersById = new Map(
      foldIsolatedIntoCluster(groupedGraph).clusters.map((c) => [c.id, c] as const),
    );
    return { centers, clustersById };
  }, [groupedGraph]);
  const lodRef = useRef(lodData);
  lodRef.current = lodData;

  // 用 ref 暴露最新的回调/状态给 G6 事件监听器，避免每次 props 变化都要重新绑定监听器。
  const latest = useRef({
    navigate,
    objectDetailPath,
    relationDetailPath,
    onEdgeClick,
    onExpandNode,
    expanding,
    isOverview,
  });
  latest.current = {
    navigate,
    objectDetailPath,
    relationDetailPath,
    onEdgeClick,
    onExpandNode,
    expanding,
    isOverview,
  };

  useEffect(() => {
    if (!isFullscreen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsFullscreen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isFullscreen]);

  // 全屏切换会改变容器尺寸，等浏览器完成布局后通知画布重新计算尺寸并适配。
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      graphRef.current?.resize();
      graphRef.current?.fitView();
    });
    return () => cancelAnimationFrame(id);
  }, [isFullscreen]);

  // 核心：graph/groupedGraph/graphMode/centerNodeId 变化时销毁并重建 G6 画布。
  // 详情/概览两种模式的 node/combo/behavior 配置差异较大，切换模式直接重建比试图在
  // 同一实例上硬切换"有无 combo"更省心。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (isOverview && !groupedGraph) return;

    // effect 清理时要执行的额外收尾（LoD 的原生事件监听、定时器等）。
    const cleanups: Array<() => void> = [];

    // 概览数据：openClustersRef 里的版块展开成成员网格，其余折叠成色块（展开态固化进初始数据，
    // 整体重建 —— G6 v5.1 下唯一稳定的路径，见 buildOverviewData 注释）。
    const data = isOverview
      ? buildOverviewData(groupedGraph!, openClustersRef.current)
      : buildDetailData(graph, centerNodeId);

    const options: GraphOptions = {
      container,
      // 关键：全局关闭动画。G6 v5.1 的视口动画（fitView/zoomTo 等）的 promise 偶发不 resolve，
      // 一旦挂起会锁死整个视口系统——表现为画布空白、滚轮无法缩放、后续 fitView/zoomTo 全部失效。
      // 关掉动画后这些视口操作同步生效、promise 立即 resolve，缩放恢复正常。
      animation: false,
      autoFit: "view",
      padding: 32,
      zoomRange: isOverview ? [0.01, 2] : [0.5, 2],
      data,
      node: ontologyNodeOptions,
      edge: isOverview ? overviewEdgeOptions : detailEdgeOptions,
      // 不用 G6 内置 zoom-canvas：它在本项目 G6 v5.1 下对滚轮无响应（用户反馈"无法缩放"）。
      // 改由下面自己监听 wheel → zoomTo，行为可控且稳定。拖拽平移仍用内置 drag-canvas。
      behaviors: ["drag-canvas", "drag-element"],
    };
    if (isOverview) {
      // 概览：版块/枢纽坐标已由后端稳定布局算好并写入数据，这里禁用 G6 自动布局，
      // 让画布直接采用这些坐标——既保证"同一数据每次打开位置不变"的地图感，
      // 也彻底绕开 combo-combined 在动态展开时的卡死风险。
      options.combo = ontologyComboOptions;
    } else {
      options.layout = { type: "antv-dagre", rankdir: "LR", nodesep: 24, ranksep: 56 };
    }

    const g = new Graph(options);
    graphRef.current = g;

    let disposed = false;

    // 自实现滚轮缩放（替代失效的内置 zoom-canvas）：以光标位置为中心缩放，钳制在 zoomRange 内。
    // animation:false 已保证 zoomTo 同步生效、promise 立即 resolve，这里可放心连续调用。
    const [minZoom, maxZoom] = options.zoomRange ?? [0.01, 2];
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (disposed) return;
      const cur = g.getZoom();
      const next = Math.max(minZoom, Math.min(maxZoom, cur * Math.pow(1.0015, -event.deltaY)));
      if (next === cur) return;
      const rect = container.getBoundingClientRect();
      void g.zoomTo(next, false, [event.clientX - rect.left, event.clientY - rect.top]);
    };
    container.addEventListener("wheel", onWheel, { passive: false });
    cleanups.push(() => container.removeEventListener("wheel", onWheel));
    void g.render().then(async () => {
      if (disposed) return;
      const camera = pendingCameraRef.current;
      pendingCameraRef.current = null;
      if (isOverview && camera) {
        // 语义缩放/展开触发的重建：还原重建前的相机，避免视图跳动。
        await restoreCamera(g, camera);
        return;
      }
      await g.fitView();
      // 概览用预设坐标、无布局：折叠态空壳 combo 的包围盒偶尔在首帧还没算好，
      // 首次 fitView 会框不住内容导致画布看似空白。下一帧再 fit 一次兜底。
      if (isOverview) {
        requestAnimationFrame(() => {
          if (!disposed) void graphRef.current?.fitView();
        });
      }
    });

    g.on<IElementEvent>("node:click", (evt) => {
      const { objectDetailPath, onExpandNode, isOverview, navigate } = latest.current;
      if (!objectDetailPath) return;
      const id = String(evt.target.id);
      if (isOverview) {
        navigate(objectDetailPath(id));
        return;
      }
      const shiftKey = Boolean((evt as unknown as { shiftKey?: boolean }).shiftKey);
      if (onExpandNode && !shiftKey) return;
      navigate(objectDetailPath(id));
    });

    g.on<IElementEvent>("node:dblclick", (evt) => {
      const { isOverview, onExpandNode, expanding } = latest.current;
      if (isOverview) return;
      if (onExpandNode && !expanding) onExpandNode(String(evt.target.id));
    });

    if (!isOverview) {
      g.on<IElementEvent>("edge:click", (evt) => {
        const { onEdgeClick, relationDetailPath, navigate } = latest.current;
        const edgeId = evt.target.id;
        const edgeData = g.getEdgeData(String(edgeId));
        const graphEdge = edgeData?.data?.graphEdge as OntologyGraph["edges"][number] | undefined;
        if (!graphEdge) return;
        if (onEdgeClick) {
          onEdgeClick(graphEdge);
          return;
        }
        if (relationDetailPath) navigate(relationDetailPath(resolveRelationId(graphEdge)));
      });
    }

    if (isOverview) {
      // 展开/收起版块 = 整体重建画布（G6 v5.1 下唯一稳定的路径）。重建前捕获相机、
      // 重建后还原，避免视图跳回 fitView。只有"期望展开集合"真正变化时才重建。
      const requestRebuildWithOpen = (nextOpen: Set<string>) => {
        const cur = openClustersRef.current;
        if (nextOpen.size === cur.size && [...nextOpen].every((id) => cur.has(id))) return;
        pendingCameraRef.current = captureCamera(g);
        openClustersRef.current = nextOpen;
        setOverviewRebuildTick((v) => v + 1);
      };

      // 按当前缩放 + 视口计算应展开的版块：远景（zoom 低）全收起；放大后展开视口内的版块，
      // 上限 LOD_MAX_OPEN_CLUSTERS（取节点数最多的几个），避免密集区域一次展开过多。
      const computeDesiredOpen = (): Set<string> => {
        if (g.getZoom() < LOD_OPEN_ZOOM) return new Set();
        const [w, h] = g.getSize();
        const tl = g.getCanvasByViewport([0, 0]);
        const br = g.getCanvasByViewport([w, h]);
        if (!tl || !br) return new Set();
        const [minX, maxX] = [Math.min(tl[0], br[0]), Math.max(tl[0], br[0])];
        const [minY, maxY] = [Math.min(tl[1], br[1]), Math.max(tl[1], br[1])];
        const { centers, clustersById } = lodRef.current;
        const inView: Array<{ id: string; count: number }> = [];
        for (const [id, c] of centers) {
          if (c.x >= minX && c.x <= maxX && c.y >= minY && c.y <= maxY) {
            inView.push({ id, count: clustersById.get(id)?.node_count ?? 0 });
          }
        }
        inView.sort((a, b) => b.count - a.count);
        return new Set(inView.slice(0, LOD_MAX_OPEN_CLUSTERS).map((x) => x.id));
      };

      let lodTimer: ReturnType<typeof setTimeout> | undefined;
      const scheduleLoD = () => {
        clearTimeout(lodTimer);
        lodTimer = setTimeout(() => {
          if (!disposed) requestRebuildWithOpen(computeDesiredOpen());
        }, LOD_DEBOUNCE_MS);
      };
      // 缩放/平移后重算 LoD。用 G6 自身的 aftertransform 事件，而不是在 container 上加原生
      // wheel 监听——后者会干扰 zoom-canvas 行为，导致滚轮无法缩放。
      g.on("aftertransform", scheduleLoD);
      cleanups.push(() => clearTimeout(lodTimer));

      // 点击版块 = 手动展开/收起该版块（不受缩放阈值限制），方便精确钻取某个域。
      g.on<IElementEvent>("combo:click", (evt) => {
        const id = String(evt.target.id);
        const next = new Set(openClustersRef.current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        requestRebuildWithOpen(next);
      });

      g.on<IElementEvent>("combo:pointerenter", (evt) => {
        const hoveredId = evt.target.id;
        const combos = g.getComboData();
        const edges = g.getEdgeData();
        combos.forEach((c) => {
          if (c.id !== hoveredId) void g.setElementState(c.id, "dimmed");
        });
        edges.forEach((e) => {
          const related = e.source === hoveredId || e.target === hoveredId;
          if (e.id != null) void g.setElementState(e.id, related ? "active" : "dimmed");
        });
      });
      g.on<IElementEvent>("combo:pointerleave", () => {
        const combos = g.getComboData();
        const edges = g.getEdgeData();
        combos.forEach((c) => void g.setElementState(c.id, []));
        edges.forEach((e) => {
          if (e.id != null) void g.setElementState(e.id, []);
        });
      });
    }

    return () => {
      disposed = true;
      cleanups.forEach((fn) => fn());
      g.destroy();
      graphRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, groupedGraph, graphMode, centerNodeId, overviewRebuildTick]);

  // 容器尺寸变化(窗口缩放、侧栏折叠、Tab 切换)时通知画布重新计算尺寸，canvas 不会像 flex 布局的 DOM 那样自动响应。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => {
      graphRef.current?.resize();
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const handleResetLayout = useCallback(() => {
    void graphRef.current?.layout();
  }, []);

  const handleFitView = useCallback(() => {
    void graphRef.current?.fitView();
  }, []);

  const handleToggleFullscreen = useCallback(() => {
    setIsFullscreen((v) => !v);
  }, []);

  const edgeClickEnabled = !isOverview && Boolean(onEdgeClick || relationDetailPath);

  const overviewHint = (): string => {
    if (groupedGraphLoading) return "正在生成域概览…";
    if (!groupedGraph) return "暂无概览数据";
    if (groupedGraph.clusters.length === 0) {
      return "对象之间暂无可聚合的关系，无法生成概览";
    }
    if (groupedGraph.clusters.length === 1 && groupedGraph.isolated_nodes.length === 0) {
      return "所有对象聚为了一类，概览意义有限，可切换详情模式查看细节";
    }
    return "悬浮聚类高亮其关系 · 点击聚类展开内部节点 · 拖拽重排";
  };

  const defaultHint = isOverview
    ? overviewHint()
    : onExpandNode
      ? "双击展开邻域 · Shift+单击查看详情 · 拖拽重排"
      : edgeClickEnabled
        ? "拖拽节点重排 · 点击节点查看详情 · 点击关系边跳转编辑"
        : "拖拽节点重排 · 点击节点查看详情";

  const layoutButtons = (
    <Space size={4}>
      <AntTooltip title="重新排布(层级布局)">
        <Button size="small" type="text" icon={<LayoutOutlined />} onClick={handleResetLayout} />
      </AntTooltip>
      <AntTooltip title="适应画布">
        <Button size="small" type="text" icon={<CompressOutlined />} onClick={handleFitView} />
      </AntTooltip>
    </Space>
  );

  const modeSwitcher = onGraphModeChange ? (
    <Segmented
      size="small"
      value={graphMode}
      onChange={(value) => onGraphModeChange(value as GraphMode)}
      options={[
        { label: "详情", value: "detail" },
        { label: "概览", value: "overview" },
      ]}
    />
  ) : null;

  return (
    <div
      className={`ontology-graph-view${embedded ? " ontology-graph-view--embedded" : ""}${
        isFullscreen ? " ontology-graph-view--fullscreen" : ""
      }`}
      style={isFullscreen ? undefined : { height }}
    >
      <div className="ontology-graph-toolbar">
        <span className="ontology-graph-hint">
          {expanding ? "正在展开邻域…" : (hint ?? defaultHint)}
        </span>
        <Space size={8}>
          {modeSwitcher}
          {!isOverview && layoutButtons}
          {isOverview && (
            <AntTooltip title="适应画布">
              <Button size="small" type="text" icon={<CompressOutlined />} onClick={handleFitView} />
            </AntTooltip>
          )}
          <span className="toolbar-divider" />
          <AntTooltip title={isFullscreen ? "退出全屏" : "全屏展示"}>
            <Button
              size="small"
              type="text"
              icon={isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              onClick={handleToggleFullscreen}
            />
          </AntTooltip>
        </Space>
      </div>
      <div ref={containerRef} className="ontology-graph-canvas" />
    </div>
  );
}

export const OntologyGraphView = memo(OntologyGraphViewInner);
