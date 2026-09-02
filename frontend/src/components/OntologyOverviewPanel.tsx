import {
  ApartmentOutlined,
  ArrowRightOutlined,
  AppstoreOutlined,
  ClusterOutlined,
  DatabaseOutlined,
  GlobalOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PartitionOutlined,
  QuestionCircleOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { Alert, Button, Empty, Segmented, Spin, Switch, Tooltip } from "antd";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type {
  ClusterDetail,
  GraphCluster,
  GraphEdge,
  GraphNode,
  OntologyGroupedGraph,
  OntologyGraph,
  SegmentDetail,
  SegmentKind,
} from "../types";
import { useApi } from "../hooks/useApi";
import { OntologyGraphView } from "./graph";
import { ISOLATED_CLUSTER_ID } from "./graph/g6/buildG6Data";

interface Props {
  ontologyId: string;
  publishedOnly?: boolean;
  objectDetailPath?: (objectId: string) => string;
  segmentPath?: (segmentId: string) => string;
  /** 固定高度；省略则量出面板到视口底部的真实可用高度 */
  height?: number;
}

/** 地图底部留白：够避开滚动条，又不至于浪费一整行卡片。 */
const VIEWPORT_GUTTER = 20;
/** 可用高度下限：再矮就不如不画图了。 */
const MIN_STAGE_HEIGHT = 420;

/**
 * 量出「面板顶边到视口底边」的真实可用高度。
 *
 * 之前用 `innerHeight - 330` 这种魔数猜，工作区页（有页头 + 提示条 + 视图切换）和
 * 本体浏览页的头部高度差了近百像素，一边浪费一边溢出。直接量元素自己的 top 就没这问题。
 */
function useAvailableHeight(ref: React.RefObject<HTMLElement | null>, fixed?: number): number {
  const [height, setHeight] = useState(fixed ?? 560);

  const measure = useCallback(() => {
    if (fixed) return;
    const el = ref.current;
    if (!el) return;
    const next = Math.max(
      MIN_STAGE_HEIGHT,
      window.innerHeight - el.getBoundingClientRect().top - VIEWPORT_GUTTER,
    );
    // 只在真的变了才 setState：本元素的 top 不依赖自身高度，但上方内容
    // （提示条出现/消失）会改它，抖动阈值挡住亚像素回环。
    setHeight((cur) => (Math.abs(cur - next) > 4 ? next : cur));
  }, [ref, fixed]);

  // 每次提交后都量一次（**不带依赖数组**）：加载态是另一棵子树（`return <Spin/>`），
  // 挂载那一次 ref 还是空的，measure 直接返回；只在 mount 量一次的话，高度就永远停在
  // 默认 560——画布比可用空间矮一大截，底部空一片白，得手动缩一下窗口才会好。
  // 不会自激：本元素的 top 由上方内容决定，与自身高度无关（页头已置 flex-shrink:0），
  // 且 setHeight 只在差值 >4px 时才落。
  useLayoutEffect(() => {
    measure();
  });

  useLayoutEffect(() => {
    if (fixed) return;
    window.addEventListener("resize", measure);
    // 上方内容（生成进度条、冲突提示）撑开或收起时同样要重量一次。
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => {
      window.removeEventListener("resize", measure);
      observer.disconnect();
    };
  }, [measure, fixed]);

  return fixed ?? height;
}

/** 右栏读法：模块内的关系图 / 关系句子清单。 */
type PaneMode = "graph" | "list";

/** 目录里唯一的伪条目：全域概览。板块划分是全覆盖分区，不再有「未接入」这一桶。 */
const OVERVIEW_KEY = "__overview__";

/**
 * 兜底板块的展示口径。它们不是业务子域，所以在目录里固定排在业务模块之后，
 * 并且各自写清「为什么在这」——这一栏的价值就是让人知道下一步该动哪里。
 */
const FALLBACK_KINDS: SegmentKind[] = ["shared", "pending", "technical", "system"];

const KIND_META: Record<
  Exclude<SegmentKind, "business">,
  { icon: React.ReactNode; why: string }
> = {
  shared: {
    icon: <ClusterOutlined />,
    why: "被多个模块共同引用的枢纽对象，刻意不并入任何单个模块",
  },
  pending: {
    icon: <QuestionCircleOutlined />,
    why: "判为业务对象/桥表，但在关系图上连不成簇——补齐关系推断后会自动归入模块",
  },
  technical: {
    icon: <SettingOutlined />,
    why: "框架管道表，不参与业务聚类——复核改判角色后会自动归入模块",
  },
  system: {
    icon: <DatabaseOutlined />,
    why: "来自数据库自带 schema，不是业务数据——根治办法是收窄摄取范围",
  },
};

const EMPTY_GRAPH: OntologyGraph = { nodes: [], edges: [] };

/**
 * 单个模块的关系图规模上限。超过就退回关系清单——句子在稠密块里比图好读，
 * 图硬画只会得到毛线球。阈值取自实测：ERP 最大模块 58 成员 / 116 条内部关系仍可读。
 */
const GRAPH_NODE_LIMIT = 120;
const GRAPH_EDGE_LIMIT = 280;

/** 落库板块的 id 是 uuid；无板块时 grouped-graph 回退算法给的是 `cluster-N`。 */
function isLegacyClusterId(id: string): boolean {
  return id.startsWith("cluster-");
}

/**
 * 业务地图。
 *
 * 默认落在**单个业务模块内部的关系图**上，而不是全域宏观图：上千个对象铺在一屏里
 * 只剩色块，读不出任何一条「谁关联谁」。全域宏观图退为目录里可选的一项。
 *
 * 版面上只留两层：左边目录、右边舞台。舞台的标题/计数/控件全部并进图自己的工具条那一行，
 * 不再额外叠一层卡片头——省下的每一行都还给画布。
 */
export function OntologyOverviewPanel({
  ontologyId,
  publishedOnly = false,
  objectDetailPath = (id) => `/ontology/${id}`,
  segmentPath = (id) => `/segments/${id}`,
  height: fixedHeight,
}: Props) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const height = useAvailableHeight(rootRef, fixedHeight);
  const [dirCollapsed, setDirCollapsed] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [paneMode, setPaneMode] = useState<PaneMode>("graph");
  const [withNeighbors, setWithNeighbors] = useState(false);
  const [detail, setDetail] = useState<SegmentDetail | ClusterDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const {
    data: groupedGraph,
    loading,
    error,
  } = useApi<OntologyGroupedGraph>(
    () => api.getOntologyGroupedGraph(ontologyId, publishedOnly),
    [ontologyId, publishedOnly],
  );

  // 业务模块的排序键 = 模块内关系条数。目的直接：能读出关系的模块排在最前面，
  // 而不是按成员数把「一堆没有关系的表」顶到第一屏。
  const businessClusters = useMemo(
    () =>
      (groupedGraph?.clusters ?? [])
        .filter((c) => c.kind === "business")
        .sort(
          (a, b) =>
            b.internal_relation_count - a.internal_relation_count || b.node_count - a.node_count,
        ),
    [groupedGraph],
  );

  // 兜底板块按固定顺序排在业务模块之后：它们不是业务子域，不该跟业务模块抢第一屏，
  // 但也不能藏起来——「为什么这么多东西没进业务模块」正是要回答的问题。
  const fallbackClusters = useMemo(() => {
    const byKind = new Map((groupedGraph?.clusters ?? []).map((c) => [c.kind, c] as const));
    return FALLBACK_KINDS.map((k) => byKind.get(k)).filter((c): c is GraphCluster => !!c);
  }, [groupedGraph]);

  const clusters = useMemo(
    () => [...businessClusters, ...fallbackClusters],
    [businessClusters, fallbackClusters],
  );

  // 默认选中关系最丰富的**业务**模块——「打开就有东西可读」。
  useEffect(() => {
    if (!groupedGraph) return;
    setSelectedId((current) => current ?? businessClusters[0]?.id ?? clusters[0]?.id ?? OVERVIEW_KEY);
  }, [groupedGraph, businessClusters, clusters]);

  const isModule = selectedId !== null && selectedId !== OVERVIEW_KEY;

  useEffect(() => {
    if (!isModule || !selectedId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    const load = isLegacyClusterId(selectedId)
      ? api.getOntologyCluster(ontologyId, selectedId, publishedOnly)
      : api.getSegment(selectedId, publishedOnly);
    load
      .then((next) => {
        if (!cancelled) setDetail(next);
      })
      .catch((err) => {
        if (!cancelled) setDetailError(err instanceof Error ? err.message : "加载模块详情失败");
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, isModule, ontologyId, publishedOnly]);

  const segmentDetail = detail && "members" in detail ? (detail as SegmentDetail) : null;
  const clusterDetail = detail && !("members" in detail) ? (detail as ClusterDetail) : null;

  // 模块关系图的数据：成员 + 模块内的边；开了「含跨模块邻居」再补上外部节点与跨模块边。
  const moduleGraph: OntologyGraph = useMemo(() => {
    if (clusterDetail) {
      return { nodes: clusterDetail.nodes, edges: clusterDetail.edges };
    }
    if (!segmentDetail) return EMPTY_GRAPH;
    const nodes: GraphNode[] = segmentDetail.members.map((m) => ({
      id: m.id,
      label: m.name,
      display_name: m.display_name,
      status: m.status,
      table_role: m.table_role,
      needs_review: m.needs_review,
    }));
    const edges: GraphEdge[] = [...(segmentDetail.edges ?? [])];
    if (withNeighbors) {
      for (const neighbor of segmentDetail.neighbors ?? []) {
        nodes.push({
          id: neighbor.id,
          label: neighbor.label,
          display_name: neighbor.display_name,
          status: neighbor.status,
          external: true,
          linkCount: neighbor.link_count,
          externalGroup: neighbor.segment_name ?? (neighbor.is_hub ? "枢纽对象" : null),
        });
      }
      edges.push(...(segmentDetail.cross_edges ?? []));
    }
    return { nodes, edges };
  }, [segmentDetail, clusterDetail, withNeighbors]);

  const neighborCount = segmentDetail?.neighbors?.length ?? 0;
  const crossRelationCount = segmentDetail?.cross_relation_count ?? 0;

  // 超过规模上限就不画图了——毛线球比没有图更糟。直接落到关系清单并说明原因。
  const graphTooDense =
    moduleGraph.nodes.length > GRAPH_NODE_LIMIT || moduleGraph.edges.length > GRAPH_EDGE_LIMIT;
  const effectivePane: PaneMode = graphTooDense ? "list" : paneMode;

  // 关系清单：后端句子已经把「主语 谓语 宾语 · 基数 · 外键证据」拼成人话，
  // 直接用；回退聚类没有句子，就地按边拼一份。
  const relationSentences = useMemo(() => {
    if (segmentDetail) return segmentDetail.relation_sentences ?? [];
    if (!clusterDetail) return [];
    const names = new Map(clusterDetail.nodes.map((n) => [n.id, n.display_name] as const));
    return clusterDetail.edges.map(
      (e) =>
        `${names.get(e.source) ?? e.source} ${e.label || "关联"} ${names.get(e.target) ?? e.target}` +
        (e.cardinality ? ` · ${e.cardinality}` : ""),
    );
  }, [segmentDetail, clusterDetail]);

  if (loading && !groupedGraph) return <Spin spinning />;
  if (error) return <Alert type="error" message="业务地图加载失败" description={error} showIcon />;
  if (!groupedGraph || (clusters.length === 0 && groupedGraph.hub_nodes.length === 0)) {
    return <Empty description="暂无可展示的业务结构" />;
  }

  const maxRelations = Math.max(1, ...businessClusters.map((c) => c.internal_relation_count));
  const selectedCluster: GraphCluster | undefined = clusters.find((c) => c.id === selectedId);

  const renderDirectoryRow = (
    key: string,
    title: string,
    meta: React.ReactNode,
    barRatio: number | null,
    icon?: React.ReactNode,
  ) => (
    <button
      key={key}
      type="button"
      className={`business-map-dir-row${selectedId === key ? " is-active" : ""}`}
      onClick={() => setSelectedId(key)}
    >
      <span className="business-map-dir-title">
        {icon}
        <span className="business-map-dir-name">{title}</span>
      </span>
      <span className="business-map-dir-meta">{meta}</span>
      {barRatio !== null && (
        <span className="business-map-dir-bar">
          <span style={{ width: `${Math.round(barRatio * 100)}%` }} />
        </span>
      )}
    </button>
  );

  const selectedKind = selectedCluster?.kind ?? "business";

  /** 目录里的一行板块。业务模块给关系量条（哪块能读出业务一眼可见）；
   *  兜底板块给种类图标、不给量条——它的成员数不代表业务体量，画成条会误导。 */
  const renderClusterRow = (cluster: GraphCluster) =>
    renderDirectoryRow(
      cluster.id,
      cluster.name,
      <>
        <span>{cluster.node_count} 对象</span>
        <span className="business-map-dir-sep">·</span>
        <span
          className={
            cluster.internal_relation_count > 0
              ? "business-map-dir-strong"
              : "business-map-dir-weak"
          }
        >
          {cluster.internal_relation_count} 关系
        </span>
      </>,
      cluster.kind === "business" ? cluster.internal_relation_count / maxRelations : null,
      cluster.kind === "business" ? undefined : KIND_META[cluster.kind].icon,
    );

  const stageTitle =
    selectedId === OVERVIEW_KEY ? "全域概览" : (selectedCluster?.name ?? "模块关系图");

  const stageMeta = (): string => {
    if (selectedId === OVERVIEW_KEY) {
      return `${businessClusters.length} 业务模块 · ${groupedGraph.hub_nodes.length} 枢纽 · 只画骨架，要读关系请在左侧选模块`;
    }
    const parts = [
      `${moduleGraph.nodes.length} 对象`,
      `${moduleGraph.edges.length} 关系`,
      `${crossRelationCount} 跨模块`,
    ];
    // 兜底板块最该说清的是「为什么在这」，而不是交互提示。
    if (selectedKind !== "business") {
      parts.push(KIND_META[selectedKind].why);
    } else if (effectivePane === "graph") {
      parts.push("悬浮对象只看它的关系");
    }
    return parts.join(" · ");
  };

  // 舞台标题行：标题 + 计数 + 控件全部一行。图的这一行由 OntologyGraphView 的工具条承载
  // （右侧还有它自己的适配/全屏按钮），非图面板则由 .business-map-head 自己撑起同样一行。
  const stageHead = (
    <div className="business-map-head">
      <span className="business-map-head-title">
        {selectedId === OVERVIEW_KEY ? (
          <GlobalOutlined />
        ) : selectedKind === "business" ? (
          <ApartmentOutlined />
        ) : (
          KIND_META[selectedKind].icon
        )}
        {stageTitle}
      </span>
      <span className="business-map-head-meta">{stageMeta()}</span>
      {isModule && (
        <span className="business-map-head-controls">
          {neighborCount > 0 && (
            <Tooltip
              title={`该模块与外部对象共 ${crossRelationCount} 条关系；打开后补上连接最多的 ${neighborCount} 个外部对象`}
            >
              <span className="business-map-switch">
                <Switch size="small" checked={withNeighbors} onChange={setWithNeighbors} />
                含跨模块邻居
              </span>
            </Tooltip>
          )}
          <Segmented
            size="small"
            value={effectivePane}
            disabled={graphTooDense}
            onChange={(value) => setPaneMode(value as PaneMode)}
            options={[
              { label: "关系图", value: "graph", icon: <PartitionOutlined /> },
              { label: "关系清单", value: "list", icon: <UnorderedListOutlined /> },
            ]}
          />
          {!isLegacyClusterId(selectedId!) && (
            <Link to={segmentPath(selectedId!)}>
              模块档案 <ArrowRightOutlined />
            </Link>
          )}
        </span>
      )}
    </div>
  );

  /** 非图面板（清单/未接入/空态）：自带同高的标题行，与图的工具条对齐。 */
  const staticPane = (body: React.ReactNode) => (
    <>
      <div className="business-map-pane-head">{stageHead}</div>
      <div className="business-map-pane-body">{body}</div>
    </>
  );

  const renderStage = () => {
    if (selectedId === OVERVIEW_KEY) {
      return (
        <OntologyGraphView
          graph={EMPTY_GRAPH}
          groupedGraph={groupedGraph}
          graphMode="overview"
          onClusterDrillIn={(id) => {
            if (id !== ISOLATED_CLUSTER_ID) setSelectedId(id);
          }}
          objectDetailPath={objectDetailPath}
          hint={stageHead}
          embedded
        />
      );
    }
    if (detailLoading) {
      return staticPane(
        <div style={{ padding: 48, textAlign: "center" }}>
          <Spin />
        </div>,
      );
    }
    if (detailError) return staticPane(<Alert type="error" message={detailError} showIcon />);
    if (moduleGraph.edges.length === 0) {
      return staticPane(
        <Empty
          style={{ padding: 48 }}
          description={
            crossRelationCount > 0
              ? `该模块内部没有关系，${crossRelationCount} 条关系全部连向模块之外——打开「含跨模块邻居」查看`
              : "该模块内部暂无关系可展示"
          }
        />,
      );
    }
    if (effectivePane === "graph") {
      return (
        <OntologyGraphView
          graph={moduleGraph}
          objectDetailPath={objectDetailPath}
          hint={stageHead}
          embedded
        />
      );
    }
    return staticPane(
      <>
        {graphTooDense && (
          <Alert
            type="info"
            showIcon
            message="该模块过于稠密，关系图会糊成一团，已改为逐条列出"
            style={{ marginBottom: 12 }}
          />
        )}
        {relationSentences.length === 0 ? (
          <Empty description="暂无模块内关系" />
        ) : (
          relationSentences.map((sentence, index) => (
            <div key={`${index}-${sentence}`} className="business-map-sentence">
              {sentence}
            </div>
          ))
        )}
      </>,
    );
  };

  return (
    <div className="ontology-overview-panel business-map" ref={rootRef}>
      {/* 高度走 CSS 变量而不是内联 height：窄屏下两栏改上下堆叠，
          必须让媒体查询把「整体固定高」换成「目录限高 + 舞台占满」。 */}
      <div
        className={`business-map-body${dirCollapsed ? " is-collapsed" : ""}`}
        style={{ ["--bm-h" as string]: `${height}px` }}
      >
        {dirCollapsed ? (
          <Tooltip title="展开业务模块目录" placement="right">
            <button
              type="button"
              className="business-map-rail"
              onClick={() => setDirCollapsed(false)}
            >
              <MenuUnfoldOutlined />
              <span className="business-map-rail-label">业务模块</span>
            </button>
          </Tooltip>
        ) : (
          <section className="business-map-col">
            <div className="business-map-pane-head business-map-pane-head--dir">
              <span className="business-map-head-title">
                <AppstoreOutlined />
                业务模块
              </span>
              <Tooltip title="收起目录，把宽度让给关系图">
                <Button
                  size="small"
                  type="text"
                  icon={<MenuFoldOutlined />}
                  onClick={() => setDirCollapsed(true)}
                />
              </Tooltip>
            </div>
            <div className="business-map-dir">
              {businessClusters.map((cluster) => renderClusterRow(cluster))}
              {fallbackClusters.length > 0 && (
                <>
                  {/* 兜底板块与业务模块之间必须有明确的分界：它们不是业务子域，
                      同一列排下来但读法完全不同（这里的数字是「还没归位的量」）。 */}
                  <div className="business-map-dir-section">未进业务模块</div>
                  {fallbackClusters.map((cluster) => renderClusterRow(cluster))}
                </>
              )}
              <div className="business-map-dir-divider" />
              {renderDirectoryRow(
                OVERVIEW_KEY,
                "全域概览",
                <span>
                  {businessClusters.length} 业务模块 · {groupedGraph.hub_nodes.length} 枢纽
                </span>,
                null,
                <GlobalOutlined />,
              )}
            </div>
          </section>
        )}

        <section className="business-map-col business-map-stage">{renderStage()}</section>
      </div>
    </div>
  );
}
