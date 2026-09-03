import {
  CompressOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  LayoutOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { Graph, type GraphOptions, type IElementEvent } from "@antv/g6";
import { Button, Input, Space, Tooltip as AntTooltip } from "antd";
import { memo, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import type { OntologyGraph } from "../../types";
import { buildDetailData } from "./g6/buildG6Data";
import { detailEdgeOptions } from "./g6/ontologyEdge";
import { ontologyNodeOptions } from "./g6/ontologyNode";

export interface OntologyDetailGraphProps {
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
}

function resolveRelationId(edge: OntologyGraph["edges"][number]): string {
  return edge.relationId || edge.relation_id || edge.id.replace(/^in-/, "");
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

// 使用 React.memo 包裹，避免父组件渲染但 props 引用稳定时，整个 G6 画布被无谓地重新创建。
function OntologyDetailGraphInner({
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
}: OntologyDetailGraphProps) {
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<Graph | null>(null);
  // 「适配到可读缩放」由建图 effect 装配（它才知道当前是概览还是详情），其余时机复用它。
  // 直接调 fitView 会把详情图缩回读不出字的比例——这正是之前地图看不清的原因之一。
  const readableFitRef = useRef<(() => Promise<void>) | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [searchValue, setSearchValue] = useState("");
  const [searchResultIndex, setSearchResultIndex] = useState(0);
  const [searchResults, setSearchResults] = useState<string[]>([]);

  // 用 ref 暴露最新的回调/状态给 G6 事件监听器，避免每次 props 变化都要重新绑定监听器。
  const latest = useRef({
    navigate,
    objectDetailPath,
    relationDetailPath,
    onEdgeClick,
    onExpandNode,
    expanding,
  });
  latest.current = {
    navigate,
    objectDetailPath,
    relationDetailPath,
    onEdgeClick,
    onExpandNode,
    expanding,
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

  // 核心：graph/centerNodeId 变化时销毁并重建 G6 画布。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // effect 清理时要执行的额外收尾（原生事件监听、定时器等）。
    const cleanups: Array<() => void> = [];

    // 模块级的图（节点多）走紧凑卡片 + 力导向；小邻域图保留大卡片 + 层级布局。
    const compactDetail = graph.nodes.length > DETAIL_FORCE_NODE_THRESHOLD;
    const data = buildDetailData(graph, centerNodeId, compactDetail);

    const options: GraphOptions = {
      container,
      // 关键：全局关闭动画。G6 v5.1 的视口动画（fitView/zoomTo 等）的 promise 偶发不 resolve，
      // 一旦挂起会锁死整个视口系统——表现为画布空白、滚轮无法缩放、后续 fitView/zoomTo 全部失效。
      // 关掉动画后这些视口操作同步生效、promise 立即 resolve，缩放恢复正常。
      animation: false,
      // 详情图不用内置 autoFit：它会在渲染后再自适应一次，把下面「顶回可读缩放」的
      // 结果覆盖掉，图又缩回读不出字。详情图的初次适配由 render().then 里自己完成。
      autoFit: undefined,
      padding: 32,
      zoomRange: [0.25, 2],
      data,
      node: ontologyNodeOptions,
      edge: detailEdgeOptions,
      // 不用 G6 内置 zoom-canvas：它在本项目 G6 v5.1 下对滚轮无响应（用户反馈"无法缩放"）。
      // 改由下面自己监听 wheel → zoomTo，行为可控且稳定。拖拽平移仍用内置 drag-canvas。
      behaviors: ["drag-canvas", "drag-element"],
    };

    if (compactDetail) {
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
        if (disposed) return;
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
    g.on("afterlayout", () => void readableFit());

    void g.render().then(async () => {
      if (disposed) return;
      // 补一次尺寸同步：容器在 G6 初始化期间还会再长高一次（面板量出「到视口底边」的
      // 真实可用高度是渲染后的一步）。而 G6 的 resize() 在画布就绪前是**静默空操作**，
      // ResizeObserver 的那一次因此丢掉，且不会重试——画布就此卡在初始高度，底部空出
      // 一大片白（容器 656 / 画布 509）。渲染完成后重放一次即可，尺寸没变则内部直接返回。
      g.resize();
      await readableFit();
    });

    g.on<IElementEvent>("node:click", (evt) => {
      const { objectDetailPath, onExpandNode, navigate } = latest.current;
      if (!objectDetailPath) return;
      const id = String(evt.target.id);
      const shiftKey = Boolean((evt as unknown as { shiftKey?: boolean }).shiftKey);
      if (onExpandNode && !shiftKey) return;
      navigate(objectDetailPath(id));
    });

    g.on<IElementEvent>("node:dblclick", (evt) => {
      const { onExpandNode, expanding } = latest.current;
      if (onExpandNode && !expanding) onExpandNode(String(evt.target.id));
    });

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
    const clearFocus = () => {
      if (focusedId === null) return; // 没在聚焦就别白白重绘
      focusedId = null;
      const states: Record<string, string[]> = {};
      // 重置为 default 状态而不是空数组，确保恢复到完全正常的显示
      g.getNodeData().forEach((n) => (states[String(n.id)] = ["default"]));
      g.getEdgeData().forEach((e) => {
        if (e.id != null) states[String(e.id)] = ["default"];
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
        states[nid] = neighbors.has(nid) ? ["active"] : ["dimmed"];
      });
      g.getEdgeData().forEach((e) => {
        if (e.id == null) return;
        states[String(e.id)] = activeEdges.has(String(e.id)) ? ["active"] : ["dimmed"];
      });
      void g.setElementState(states, false);
    });
    g.on<IElementEvent>("node:pointerleave", clearFocus);
    // 关键补漏：指针从一个节点上**直接滑出画布**时，G6 不发 node:pointerleave，
    // 聚焦态就永久卡在压暗上。用容器自己的原生 pointerleave 收尾——它只在指针
    // 真的离开容器时触发，语义明确。
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

    return () => {
      disposed = true;
      cleanups.forEach((fn) => fn());
      g.destroy();
      graphRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, centerNodeId]);

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

  // 搜索节点：模糊匹配节点标签
  const handleSearch = useCallback((value: string) => {
    const g = graphRef.current;
    if (!g) return;

    setSearchValue(value);

    if (!value.trim()) {
      // 清空搜索：移除所有高亮状态
      setSearchResults([]);
      setSearchResultIndex(0);
      const states: Record<string, string[]> = {};
      g.getNodeData().forEach((n) => (states[String(n.id)] = ["default"]));
      void g.setElementState(states, false);
      return;
    }

    // 搜索匹配的节点
    const keyword = value.toLowerCase();
    const matches: string[] = [];
    g.getNodeData().forEach((node) => {
      const label = String(node.data?.label || "").toLowerCase();
      if (label.includes(keyword)) {
        matches.push(String(node.id));
      }
    });

    setSearchResults(matches);
    setSearchResultIndex(0);

    if (matches.length > 0) {
      // 高亮第一个匹配的节点
      highlightSearchResult(g, matches, 0);
    } else {
      // 无匹配：恢复默认状态
      const states: Record<string, string[]> = {};
      g.getNodeData().forEach((n) => (states[String(n.id)] = ["default"]));
      void g.setElementState(states, false);
    }
  }, []);

  // 高亮指定的搜索结果
  const highlightSearchResult = useCallback((g: Graph, matches: string[], index: number) => {
    const targetId = matches[index];
    if (!targetId) return;

    // 设置状态：当前结果高亮，其他匹配项普通高亮，非匹配项压暗
    const states: Record<string, string[]> = {};
    g.getNodeData().forEach((n) => {
      const nid = String(n.id);
      if (nid === targetId) {
        states[nid] = ["active"]; // 当前结果：蓝色高亮
      } else if (matches.includes(nid)) {
        states[nid] = ["hover"]; // 其他匹配：灰色高亮
      } else {
        states[nid] = ["dimmed"]; // 非匹配：压暗
      }
    });
    void g.setElementState(states, false);

    // 将节点移动到视口中心
    const nodeData = g.getNodeData(targetId);
    if (nodeData) {
      void g.focusElement(targetId, { duration: 300 });
    }
  }, []);

  // 切换到下一个/上一个搜索结果
  const handleNextResult = useCallback(() => {
    const g = graphRef.current;
    if (!g || searchResults.length === 0) return;

    const nextIndex = (searchResultIndex + 1) % searchResults.length;
    setSearchResultIndex(nextIndex);
    highlightSearchResult(g, searchResults, nextIndex);
  }, [searchResults, searchResultIndex, highlightSearchResult]);

  const handlePrevResult = useCallback(() => {
    const g = graphRef.current;
    if (!g || searchResults.length === 0) return;

    const prevIndex = (searchResultIndex - 1 + searchResults.length) % searchResults.length;
    setSearchResultIndex(prevIndex);
    highlightSearchResult(g, searchResults, prevIndex);
  }, [searchResults, searchResultIndex, highlightSearchResult]);

  const edgeClickEnabled = Boolean(onEdgeClick || relationDetailPath);

  // 悬浮聚焦是稠密图唯一好用的读法，提示语里必须写出来，否则没人会去试。
  const defaultHint = onExpandNode
    ? "悬浮对象只看它的关系 · 双击展开邻域 · Shift+单击查看详情"
    : edgeClickEnabled
      ? "悬浮对象只看它的关系 · 点击对象查看详情 · 点击关系边跳转编辑"
      : "悬浮对象只看它的关系 · 点击对象查看详情 · 拖拽重排";

  const searchBox = (
    <Input
      size="small"
      placeholder="搜索节点..."
      prefix={<SearchOutlined />}
      value={searchValue}
      onChange={(e) => handleSearch(e.target.value)}
      onPressEnter={handleNextResult}
      suffix={
        searchResults.length > 0 ? (
          <span style={{ fontSize: 12, color: "#64748b", userSelect: "none" }}>
            {searchResultIndex + 1}/{searchResults.length}
            <Button
              size="small"
              type="text"
              style={{ marginLeft: 4, padding: "0 4px", minWidth: 20 }}
              onClick={handlePrevResult}
            >
              ↑
            </Button>
            <Button
              size="small"
              type="text"
              style={{ padding: "0 4px", minWidth: 20 }}
              onClick={handleNextResult}
            >
              ↓
            </Button>
          </span>
        ) : null
      }
      style={{ width: 240 }}
      allowClear
    />
  );

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

  return (
    <div
      className={`ontology-graph-view${embedded ? " ontology-graph-view--embedded" : ""}${
        isFullscreen ? " ontology-graph-view--fullscreen" : ""
      }${height == null ? " ontology-graph-view--fill" : ""}`}
      style={isFullscreen || height == null ? undefined : { height }}
    >
      <div className="ontology-graph-toolbar">
        <span className="ontology-graph-hint">
          {expanding ? "正在展开邻域…" : (hint ?? defaultHint)}
        </span>
        <Space size={8}>
          {searchBox}
          <span className="toolbar-divider" />
          {layoutButtons}
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

export const OntologyDetailGraph = memo(OntologyDetailGraphInner);
