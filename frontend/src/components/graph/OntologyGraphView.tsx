import {
  CompressOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  LayoutOutlined,
  SearchOutlined,
  UpOutlined,
  DownOutlined,
} from "@ant-design/icons";
import { Graph, type GraphOptions, type IElementEvent, type NodeData } from "@antv/g6";
import { Button, Input, Segmented, Space, Tooltip as AntTooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
  /** 固定像素高度。**省略则填满 flex 父容器**（父容器需为限定高度的 flex column）。 */
  height?: number;
  centerNodeId?: string;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  onEdgeClick?: (edge: OntologyGraph["edges"][number]) => void;
  /** 双击节点展开邻域 */
  onExpandNode?: (objectId: string) => void;
  expanding?: boolean;
  /** 工具条左侧内容。传节点即可把标题/计数/控件并进这一行，避免再叠一层卡片头。 */
  hint?: ReactNode;
  /** 嵌入 SectionCard / Tabs 时使用，去除外层重复边框 */
  embedded?: boolean;
  /** 域层级概览图数据；未提供时不展示 详情/概览 切换 */
  groupedGraph?: OntologyGroupedGraph | null;
  groupedGraphLoading?: boolean;
  /** 双击概览版块下钻：打开该聚类的邻接矩阵 */
  onClusterDrillIn?: (clusterId: string) => void;
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

// 详情图切换布局的节点数阈值。层级布局（dagre）在小邻域图上最好读——左→右就是引用方向；
// 但业务模块的图是「一堆表指向少数几个被引用对象」的星形，dagre 会把二十几个节点塞进同一
// 个 rank 摞成一根两千多像素高的柱子，fitView 一缩就全糊了。超过这个规模改用力导向，
// 它会把图铺成接近画布长宽比的一团，节点尺寸才留得住。
const DETAIL_FORCE_NODE_THRESHOLD = 12;

// 详情图初次适配后的最低缩放。0.8 下 13px 的对象名还剩约 10px，是能读的下限；
// 再往下就只剩色块了。塞不进一屏的部分靠拖拽/缩小——「看清关系」优先于「一屏看全」。
const DETAIL_MIN_READABLE_ZOOM = 0.8;

// 力导向的确定性随机源：同一份数据每次打开必须落在同一个位置，否则「上次那张图」找不回来。
function seededRandom(seed = 0x2f6e2b1): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x100000000;
  };
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
  height,
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
  onClusterDrillIn,
}: OntologyGraphViewProps) {
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<Graph | null>(null);
  // 「适配到可读缩放」由建图 effect 装配（它才知道当前是概览还是详情），其余时机复用它。
  // 直接调 fitView 会把详情图缩回读不出字的比例——这正是之前地图看不清的原因之一。
  const readableFitRef = useRef<(() => Promise<void>) | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  // 搜索状态
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<string[]>([]);
  const [currentResultIndex, setCurrentResultIndex] = useState(0);
  // 清空搜索时要知道「刚才命中的是哪几个」才能给它们留痕。用 ref 读最新值，
  // 免得把 searchResults 塞进那两个回调的依赖里、每次搜索都重建回调。
  const searchResultsRef = useRef<string[]>([]);
  searchResultsRef.current = searchResults;

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
    onClusterDrillIn,
  });
  latest.current = {
    navigate,
    objectDetailPath,
    relationDetailPath,
    onEdgeClick,
    onExpandNode,
    expanding,
    isOverview,
    onClusterDrillIn,
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
      void readableFitRef.current?.();
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
    // 模块级的图（节点多）走紧凑卡片 + 力导向；小邻域图保留大卡片 + 层级布局。
    const compactDetail = !isOverview && graph.nodes.length > DETAIL_FORCE_NODE_THRESHOLD;
    const data = isOverview
      ? buildOverviewData(groupedGraph!, openClustersRef.current)
      : buildDetailData(graph, centerNodeId, compactDetail);

    const options: GraphOptions = {
      container,
      // 关键：全局关闭动画。G6 v5.1 的视口动画（fitView/zoomTo 等）的 promise 偶发不 resolve，
      // 一旦挂起会锁死整个视口系统——表现为画布空白、滚轮无法缩放、后续 fitView/zoomTo 全部失效。
      // 关掉动画后这些视口操作同步生效、promise 立即 resolve，缩放恢复正常。
      animation: false,
      // 详情图不用内置 autoFit：它会在渲染后再自适应一次，把下面「顶回可读缩放」的
      // 结果覆盖掉，图又缩回读不出字。详情图的初次适配由 render().then 里自己完成。
      autoFit: isOverview ? "view" : undefined,
      padding: 32,
      zoomRange: isOverview ? [0.01, 2] : [0.25, 2],
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
    } else if (compactDetail) {
      // 力导向：连在一起的对象自然靠拢、被多方引用的对象自然居中，长度接近的边比
      // dagre 的长距离跨 rank 连线好追。y 向心力比 x 强，把结果压成宽幅，贴合画布长宽比。
      // 力的尺度跟紧凑卡片走（碰撞半径略大于卡片对角），卡片小了图就密，缩放才留得住。
      options.layout = {
        type: "d3-force",
        randomSource: seededRandom(),
        link: { distance: 170, strength: 0.45 },
        manyBody: { strength: -820, distanceMax: 1100 },
        collide: { radius: 78, strength: 1 },
        x: { strength: 0.03 },
        y: { strength: 0.11 },
        alphaDecay: 0.028,
      };
    } else {
      // 小邻域图：层级布局按关系方向分层，左→右直接读作「谁引用谁」。
      options.layout = { type: "antv-dagre", rankdir: "LR", nodesep: 28, ranksep: 96 };
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
    // 适配画布，但详情图不允许缩到读不出字：fitView 之后把缩放顶回可读下限，
    // 塞不下的部分靠拖拽——「看清关系」优先于「一屏看全」。
    const readableFit = async () => {
      if (disposed) return;
      // 布局的收敛是异步的，画布可能在两次 await 之间被销毁（切模块 / 离开页面）；
      // 对已销毁的实例调视口 API 会抛，这里逐步守 disposed 并兜底 catch。
      try {
        await g.fitView();
        if (disposed || isOverview) return;
        if (g.getZoom() < DETAIL_MIN_READABLE_ZOOM) {
          await g.zoomTo(DETAIL_MIN_READABLE_ZOOM, false);
        }
      } catch {
        // 画布已销毁，无需适配
      }
    };
    readableFitRef.current = readableFit;
    cleanups.push(() => {
      if (readableFitRef.current === readableFit) readableFitRef.current = null;
    });

    // 力导向是异步收敛的：render() 的 promise 先于布局落定 resolve，那一刻的包围盒还很小，
    // fitView 得到的缩放没有意义。等 afterlayout 再适配一次，这才是用户看到的第一屏。
    if (!isOverview) g.on("afterlayout", () => void readableFit());

    void g.render().then(async () => {
      if (disposed) return;
      // 补一次尺寸同步：容器在 G6 初始化期间还会再长高一次（面板量出「到视口底边」的
      // 真实可用高度是渲染后的一步）。而 G6 的 resize() 在画布就绪前是**静默空操作**，
      // ResizeObserver 的那一次因此丢掉，且不会重试——画布就此卡在初始高度，底部空出
      // 一大片白（容器 656 / 画布 509）。渲染完成后重放一次即可，尺寸没变则内部直接返回。
      g.resize();
      const camera = pendingCameraRef.current;
      pendingCameraRef.current = null;
      if (isOverview && camera) {
        // 语义缩放/展开触发的重建：还原重建前的相机，避免视图跳动。
        await restoreCamera(g, camera);
        return;
      }
      await readableFit();
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

      // 邻域聚焦：悬浮一个对象 → 只留它和它的直接关系，其余压暗。
      // 30+ 节点的板块图里，这是「这个对象连着谁」唯一读得出来的方式；
      // 状态一次性批量下发（单次 setElementState → 单次重绘），避免连环重绘卡顿。
      let focusedId: string | null = null;
      // 刚看过的那一组节点。鼠标一移开就全清的话，刚读出来的东西也跟着没了——想再确认
      // 一眼只能重新找到那个节点再悬浮一次。留个痕，视线可以离开画布去看别处，回头还
      // 认得住。只留**最后一组**，不累积，否则翻十个节点整张图就黄了。
      //
      // **连线不留痕**：边一亮就是一张黄网铺在图上，盖过底下真正要读的结构；
      // 节点标住就够找回来了。所以移开时边一律复位。
      let recentNodes = new Set<string>();
      const clearFocus = () => {
        if (focusedId === null) return; // 没在聚焦就别白白重绘
        focusedId = null;
        const states: Record<string, string[]> = {};
        g.getNodeData().forEach((n) => {
          const nid = String(n.id);
          states[nid] = recentNodes.has(nid) ? ["recent"] : [];
        });
        g.getEdgeData().forEach((e) => {
          if (e.id != null) states[String(e.id)] = [];
        });
        void g.setElementState(states, false);
      };
      g.on<IElementEvent>("node:pointerenter", (evt) => {
        const focusId = String(evt.target.id);
        focusedId = focusId;
        const neighbors = new Set<string>([focusId]);
        const activeEdges = new Set<string>();
        g.getEdgeData().forEach((e) => {
          const [src, tgt] = [String(e.source), String(e.target)];
          if (src !== focusId && tgt !== focusId) return;
          neighbors.add(src);
          neighbors.add(tgt);
          if (e.id != null) activeEdges.add(String(e.id));
        });
        const states: Record<string, string[]> = {};
        g.getNodeData().forEach((n) => {
          const nid = String(n.id);
          // 指着的那个单独一档：邻居也用 active 的话，「我点的是哪个」就读不出来了。
          states[nid] =
            nid === focusId ? ["selected"] : neighbors.has(nid) ? ["active"] : ["dimmed"];
        });
        g.getEdgeData().forEach((e) => {
          if (e.id == null) return;
          states[String(e.id)] = activeEdges.has(String(e.id)) ? ["active"] : ["dimmed"];
        });
        // 这一组就是「刚看过的」——移开时由 clearFocus 把这些节点留成痕迹。
        recentNodes = neighbors;
        void g.setElementState(states, false);
      });
      g.on<IElementEvent>("node:pointerleave", clearFocus);
      // 关键补漏：指针从一个节点上**直接滑出画布**时，G6 不发 node:pointerleave，
      // 聚焦态就永久卡在压暗上。用容器自己的原生 pointerleave 收尾——它只在指针
      // 真的离开容器时触发，语义明确。
      //
      // 别用 G6 的 canvas:pointerenter/canvas:pointerleave 代替：canvas 事件会从
      // 节点冒上来，实测顺序是 node:pointerenter → canvas:pointerenter，
      // 拿它清理会把刚设好的聚焦立刻擦掉。
      container.addEventListener("pointerleave", clearFocus);
      cleanups.push(() => container.removeEventListener("pointerleave", clearFocus));

      // 悬停高亮可点击的关系边（描边加粗 + 标签链接色），配合 cursor:pointer 让边的
      // 可点击性可见。仅当边确实可跳转时才亮，避免误导。
      // 处于邻域聚焦时不接管边的状态：否则扫过一条边会在压暗层上戳个洞。
      const edgeNavigable = () => {
        const { onEdgeClick, relationDetailPath } = latest.current;
        return Boolean(onEdgeClick || relationDetailPath);
      };
      g.on<IElementEvent>("edge:pointerenter", (evt) => {
        if (!edgeNavigable() || focusedId !== null) return;
        void g.setElementState(String(evt.target.id), ["hover"], false);
      });
      g.on<IElementEvent>("edge:pointerleave", (evt) => {
        if (focusedId !== null) return;
        void g.setElementState(String(evt.target.id), [], false);
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

      // 双击版块下钻矩阵（跳过孤立虚拟簇 "__isolated__"，后端无对应聚类）。
      g.on<IElementEvent>("combo:dblclick", (evt) => {
        const id = String(evt.target.id);
        if (id.startsWith("__")) return;
        latest.current.onClusterDrillIn?.(id);
      });

      // 概览 hover 联动：悬浮版块/枢纽时高亮相关元素、压暗其余，让单个业务块从密图中凸显。
      // 关键性能点：状态一次性批量下发（单次 setElementState → 单次重绘），
      // 避免逐元素调用导致的连环重绘卡顿。
      // 与详情图同一条约束：指针从某个版块/枢纽上直接滑出画布时不会发对应元素的
      // pointerleave —— 不接住这条，整张概览会永久停在压暗态。
      // hasFocus 守卫避免无谓重绘。
      let hasFocus = false;
      const applyStates = (states: Record<string, string[]>) => {
        hasFocus = true;
        void g.setElementState(states, false);
      };
      const clearOverviewStates = () => {
        if (!hasFocus) return;
        hasFocus = false;
        const states: Record<string, string[]> = {};
        g.getComboData().forEach((c) => (states[String(c.id)] = []));
        g.getNodeData().forEach((n) => (states[String(n.id)] = []));
        g.getEdgeData().forEach((e) => {
          if (e.id != null) states[String(e.id)] = [];
        });
        void g.setElementState(states, false);
      };
      container.addEventListener("pointerleave", clearOverviewStates);
      cleanups.push(() => container.removeEventListener("pointerleave", clearOverviewStates));

      // 以某个宏观节点(版块/枢纽)为中心，算出所有元素的高亮/压暗批量状态。
      const buildFocusStates = (
        focusId: string,
        isMember: (n: NodeData) => boolean,
      ): Record<string, string[]> => {
        const edges = g.getEdgeData();
        const neighbors = new Set<string>();
        edges.forEach((e) => {
          if (String(e.source) === focusId) neighbors.add(String(e.target));
          if (String(e.target) === focusId) neighbors.add(String(e.source));
        });
        const states: Record<string, string[]> = {};
        g.getComboData().forEach((c) => {
          const cid = String(c.id);
          states[cid] = cid === focusId || neighbors.has(cid) ? [] : ["dimmed"];
        });
        g.getNodeData().forEach((n) => {
          const nid = String(n.id);
          // 指着的那个（枢纽 hover 时）单独一档：跟邻居同样式的话就看不出焦点在哪。
          // 版块 hover 时 focusId 是 combo id，这里不会命中，行为不变。
          states[nid] = nid === focusId
            ? ["selected"]
            : isMember(n) || neighbors.has(nid)
              ? ["active"]
              : ["dimmed"];
        });
        edges.forEach((e) => {
          if (e.id == null) return;
          const related = String(e.source) === focusId || String(e.target) === focusId;
          states[String(e.id)] = related ? ["active"] : ["dimmed"];
        });
        return states;
      };

      g.on<IElementEvent>("combo:pointerenter", (evt) => {
        const hoveredId = String(evt.target.id);
        applyStates(buildFocusStates(hoveredId, (n) => String(n.combo) === hoveredId));
      });
      g.on<IElementEvent>("combo:pointerleave", clearOverviewStates);

      // 枢纽 hover 联动：高亮其邻接的版块/枢纽与边（成员节点悬浮不触发，避免抖动）。
      g.on<IElementEvent>("node:pointerenter", (evt) => {
        const id = String(evt.target.id);
        const nd = g.getNodeData(id);
        if ((nd?.data as { kind?: string } | undefined)?.kind !== "hub") return;
        applyStates(buildFocusStates(id, () => false));
      });
      g.on<IElementEvent>("node:pointerleave", clearOverviewStates);
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

  // 搜索只按节点名匹配，边不参与——但必须把边一并复位：上一次悬浮聚焦在边上留下的
  // 琥珀色痕迹不清掉，就会跟搜索结果混在同一张图里，看起来像几条无主的黄线。
  const resetEdgeStates = useCallback((states: Record<string, string[]>) => {
    graphRef.current?.getEdgeData().forEach((e) => {
      if (e.id != null) states[String(e.id)] = [];
    });
  }, []);

  // 搜索功能
  const handleSearch = useCallback(
    (query: string) => {
      setSearchQuery(query);
      if (!query.trim() || !graphRef.current) {
        // 清空搜索：刚命中的那几个留成痕迹，其余整个复位。
        // 复位要用 []（回落到基础样式），不能用 ["default"]——default 只写了 opacity，
        // 选中态改过的填充色会原样留在卡片上，看起来像是残留的脏状态。
        const keep = new Set(searchResultsRef.current);
        setSearchResults([]);
        setCurrentResultIndex(0);
        const states: Record<string, string[]> = {};
        graphRef.current?.getNodeData().forEach((n) => {
          const nid = String(n.id);
          states[nid] = keep.has(nid) ? ["recent"] : [];
        });
        // 边也要一起复位：只写节点的话，上一次悬浮留下的琥珀色连线会挂在图上不走，
        // 看起来像是几条无主的黄线。
        resetEdgeStates(states);
        graphRef.current?.setElementState(states);
        return;
      }

      // 搜索匹配的节点
      const lowerQuery = query.toLowerCase();
      const allNodes = graphRef.current.getNodeData();
      const matches = allNodes
        .filter((node: NodeData) => {
          const label = String(node.data?.label || "").toLowerCase();
          return label.includes(lowerQuery);
        })
        .map((node: NodeData) => String(node.id));

      setSearchResults(matches);
      setCurrentResultIndex(matches.length > 0 ? 0 : -1);

      // 更新节点状态
      const states: Record<string, string[]> = {};
      if (matches.length > 0) {
        allNodes.forEach((node: NodeData) => {
          const id = String(node.id);
          // 当前这一条是"选中"，其余命中是"还在结果里"，两者不能长一样——
          // 否则按上一条/下一条时看不出光标挪到哪去了。
          states[id] = id === matches[0]
            ? ["selected"]
            : matches.includes(id)
              ? ["active"]
              : ["dimmed"];
        });
        resetEdgeStates(states);
        graphRef.current.setElementState(states);
        // 聚焦到第一个结果
        graphRef.current.focusElement(matches[0], { duration: 300 });
      } else {
        // 没有结果时压暗所有节点
        allNodes.forEach((n: NodeData) => (states[String(n.id)] = ["dimmed"]));
        resetEdgeStates(states);
        graphRef.current.setElementState(states);
      }
    },
    [resetEdgeStates],
  );

  const handleSearchNavigation = useCallback(
    (direction: "prev" | "next") => {
      if (searchResults.length === 0 || !graphRef.current) return;

      const newIndex =
        direction === "next"
          ? (currentResultIndex + 1) % searchResults.length
          : (currentResultIndex - 1 + searchResults.length) % searchResults.length;

      setCurrentResultIndex(newIndex);

      // 更新节点状态
      const allNodes = graphRef.current.getNodeData();
      const states: Record<string, string[]> = {};
      allNodes.forEach((node: NodeData) => {
        const id = String(node.id);
        states[id] =
          id === searchResults[newIndex]
            ? ["selected"]
            : searchResults.includes(id)
              ? ["active"]
              : ["dimmed"];
      });
      resetEdgeStates(states);
      graphRef.current.setElementState(states);

      // 聚焦到当前结果
      graphRef.current.focusElement(searchResults[newIndex], { duration: 300 });
    },
    [searchResults, currentResultIndex, resetEdgeStates],
  );

  const handleClearSearch = useCallback(() => {
    setSearchQuery("");
    // 刚命中的那几个留成痕迹，其余复位（同 handleSearch 的清空分支）。
    const keep = new Set(searchResultsRef.current);
    setSearchResults([]);
    setCurrentResultIndex(0);
    const states: Record<string, string[]> = {};
    graphRef.current?.getNodeData().forEach((n) => {
      const nid = String(n.id);
      states[nid] = keep.has(nid) ? ["recent"] : [];
    });
    resetEdgeStates(states);
    graphRef.current?.setElementState(states);
  }, [resetEdgeStates]);

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
    return "悬浮聚类高亮其关系 · 点击聚类展开内部节点 · 双击聚类看矩阵 · 拖拽重排";
  };

  // 悬浮聚焦是稠密图唯一好用的读法，提示语里必须写出来，否则没人会去试。
  const defaultHint = isOverview
    ? overviewHint()
    : onExpandNode
      ? "悬浮对象只看它的关系 · 双击展开邻域 · Shift+单击查看详情"
      : edgeClickEnabled
        ? "悬浮对象只看它的关系 · 点击对象查看详情 · 点击关系边跳转编辑"
        : "悬浮对象只看它的关系 · 点击对象查看详情 · 拖拽重排";

  const layoutButtons = (
    <Space size={4}>
      <AntTooltip title="重新排布">
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
        { label: "概览", value: "overview" },
        { label: "详情", value: "detail" },
      ]}
    />
  ) : null;

  return (
    <div
      className={`ontology-graph-view${embedded ? " ontology-graph-view--embedded" : ""}${
        isFullscreen ? " ontology-graph-view--fullscreen" : ""
      }${height == null ? " ontology-graph-view--fill" : ""}`}
      style={isFullscreen || height == null ? undefined : { height }}
    >
      <div className="ontology-graph-toolbar">
        {/* 左侧：搜索框 */}
        <Space size={8}>
          <Input
            size="small"
            placeholder="搜索节点..."
            prefix={<SearchOutlined />}
            suffix={
              searchResults.length > 0 ? (
                <Space size={4}>
                  <span style={{ fontSize: 12, color: "var(--om-text-tertiary)" }}>
                    {currentResultIndex + 1}/{searchResults.length}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    icon={<UpOutlined />}
                    onClick={() => handleSearchNavigation("prev")}
                    style={{ padding: "0 4px", minWidth: 20 }}
                  />
                  <Button
                    size="small"
                    type="text"
                    icon={<DownOutlined />}
                    onClick={() => handleSearchNavigation("next")}
                    style={{ padding: "0 4px", minWidth: 20 }}
                  />
                </Space>
              ) : undefined
            }
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            onPressEnter={() => handleSearchNavigation("next")}
            allowClear
            onClear={handleClearSearch}
            style={{ width: 240 }}
          />
          <span className="ontology-graph-hint">
            {expanding ? "正在展开邻域…" : (hint ?? defaultHint)}
          </span>
        </Space>

        {/* 右侧：视图控制 */}
        <Space size={8}>
          {modeSwitcher}
          {!isOverview && layoutButtons}
          {isOverview && (
            <AntTooltip title="适应画布">
              <Button
                size="small"
                type="text"
                icon={<CompressOutlined />}
                onClick={handleFitView}
              />
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
