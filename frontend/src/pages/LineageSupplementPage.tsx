import {
  ArrowRightOutlined,
  BulbOutlined,
  CloudUploadOutlined,
  LeftOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Alert, Badge, Button, Input, Popconfirm, Segmented, Select, Tag, Tooltip, message } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { LineageCanvas } from "../components/lineage/LineageCanvas";
import type { CanvasEdge, CanvasNode } from "../components/lineage/LineageCanvas";
import { LineageTableName, readableDatabaseList } from "../components/lineage/LineageTableName";
import { PackageRail } from "../components/lineage/PackageRail";
import { RelationSuggestionDrawer } from "../components/lineage/RelationSuggestionDrawer";
import { ScanReport } from "../components/lineage/ScanReport";
import { TableMappingModal } from "../components/lineage/TableMappingModal";
import { PageContainer } from "../components/PageContainer";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { useUrlState } from "../hooks/useUrlState";
import type {
  DomainContext,
  LineageColumn,
  LineageOverview,
  LineagePackageDetail,
  LineagePackageRow,
  LineageTableMapping,
  LineageTableRow,
  RelationCandidate,
  RelationInferenceTask,
} from "../types";

/**
 * 血缘补录工作台。
 *
 * 页面只讲一个数：**这个域里有多少张表是孤岛**（上下游皆空 → 本体生成时被判孤岛、
 * 降级 data_table、所在业务环节断裂）。表为什么没血缘（外购、新导入、临时落地）
 * 是来源场景，不进界面——补录的动作只有两种：
 *
 * - **扫代码包**：丢一个没有格式约定的 SQL 包进来，递归扫 .sql，自动提血缘；
 *   包留在历史里，什么时候投的、上报没上报都查得到。
 * - **画布补录**：把已知的表摆上画布，像连 ER 图一样手工连。
 *
 * 两条路径在「所有包都没覆盖的孤岛表」处交接：这些表一键送进画布。
 *
 * 版式是工作台：顶栏一条事实带 + 可收起的左栏 + 吃满剩余高度的工作面 + 常驻写入条。
 * 页面自己不滚动，滚动只发生在工作面内部。
 */

type Mode = "scan" | "canvas";
type RailFilter = "all" | "isolated";

const CANVAS_COL_W = 320;
const CANVAS_ROW_H = 250;
const RAIL_ROW_H = 64;
const RAIL_OVERSCAN = 6;
const DIALECTS = ["mysql", "postgres", "hive", "doris", "starrocks"];

function canvasEdgeSignature(edge: CanvasEdge) {
  return `${edge.id}|${edge.keys
    .map((key) => key.id)
    .sort()
    .join("|")}`;
}

export function LineageSupplementPage() {
  const navigate = useNavigate();
  const [domainId, setDomainId] = useUrlState<string>("domain", "");
  const [mode, setMode] = useState<Mode>("scan");
  const [railOpen, setRailOpen] = useState(true);
  const [railFilter, setRailFilter] = useState<RailFilter>("isolated");
  const [keyword, setKeyword] = useState("");
  const [railScrollTop, setRailScrollTop] = useState(0);
  const [railViewportHeight, setRailViewportHeight] = useState(0);
  const [dialect, setDialect] = useState("mysql");

  const [packages, setPackages] = useState<LineagePackageRow[]>([]);
  const [pkgId, setPkgId] = useState<string | null>(null);
  const [detail, setDetail] = useState<LineagePackageDetail | null>(null);
  const [uploading, setUploading] = useState(false);
  const [scanningId, setScanningId] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, string[]>>({});
  const [uncovered, setUncovered] = useState<string[]>([]);
  const [applying, setApplying] = useState(false);

  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<CanvasEdge[]>([]);
  const [columns, setColumns] = useState<Record<string, LineageColumn[]>>({});
  const [appliedCanvasEdges, setAppliedCanvasEdges] = useState<Set<string>>(
    () => new Set<string>(),
  );
  const [optimisticResolvedTables, setOptimisticResolvedTables] = useState<Set<string>>(
    () => new Set<string>(),
  );
  // 人工表名映射：blocked 边此前只能重扫，而重扫用同一套解析、结果一样。
  const [mapTarget, setMapTarget] = useState<string | null>(null);
  const [mappings, setMappings] = useState<LineageTableMapping[]>([]);
  const [savingMapping, setSavingMapping] = useState(false);

  // 智能关系补充：推断异步跑（LLM 判定实测数百秒），这里只管起任务 + 轮询 + 表态。
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [inferTask, setInferTask] = useState<RelationInferenceTask | null>(null);
  const [candidates, setCandidates] = useState<RelationCandidate[]>([]);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [applyingRelations, setApplyingRelations] = useState(false);

  const railListRef = useRef<HTMLUListElement>(null);
  const inventoryRefreshDomainRef = useRef<string | null>(null);

  // Domain selection only needs the local workspace cache.  The regular
  // /api/domains call also synchronizes DataHub and can take several seconds.
  const domains = useApi<DomainContext[]>((signal) => api.listDomains(signal, false), []);

  // 没选域时落到**对象最多的那个**，不是列表第一个：域列表里排在最前的往往是
  // 调试域（datahub_domain_id 是假的），进来就是一屏 DataHub 报错。
  useEffect(() => {
    if (domainId || !domains.data || domains.data.length === 0) return;
    const best = [...domains.data].sort(
      (a, b) => (b.object_type_count ?? 0) - (a.object_type_count ?? 0),
    )[0];
    setDomainId(best.id);
  }, [domainId, domains.data, setDomainId]);

  useEffect(() => {
    inventoryRefreshDomainRef.current = null;
    setNodes([]);
    setEdges([]);
    setColumns({});
    setAppliedCanvasEdges(new Set());
    setOptimisticResolvedTables(new Set());
    setInferTask(null);
    setCandidates([]);
  }, [domainId]);

  const overview = useApi<LineageOverview | null>(
    async (signal) => (domainId ? api.lineageOverview(domainId, false, signal) : null),
    [domainId],
  );
  const needsTableInventory = mode === "canvas" || mapTarget !== null;
  const tables = useApi<LineageTableRow[]>(
    async (signal) => {
      // The scan view works entirely from the local package detail. Fetch the
      // potentially large table inventory only when the canvas or mapping modal
      // needs it.
      if (!domainId || !needsTableInventory) return [];
      return api.lineageTables(domainId, { limit: 2000 }, signal);
    },
    [domainId, needsTableInventory],
  );

  const inventoryLoading = Boolean(
    domainId &&
      (overview.loading ||
        (needsTableInventory && tables.loading) ||
        overview.data?.domain_id !== domainId),
  );
  const inventoryReady = Boolean(
    domainId &&
      !overview.loading &&
      overview.data?.domain_id === domainId &&
      (!needsTableInventory || (!tables.loading && tables.data)),
  );
  const selectedDomainKnown = Boolean(
    domainId && domains.data?.some((domain) => domain.id === domainId),
  );

  const refreshPackages = useCallback(
    async (select?: string, withInventory = true) => {
      if (!domainId) return;
      const rows = await api.listLineagePackages(domainId, "scan", withInventory);
      setPackages(rows);
      const next = select ?? (rows.length > 0 ? rows[0].id : null);
      setPkgId(next);
      const [nextDetail, nextUncovered] = await Promise.all([
        next ? api.getLineagePackage(next, withInventory) : Promise.resolve(null),
        withInventory ? api.lineageUncoveredIsolated(domainId) : Promise.resolve([]),
      ]);
      setDetail(nextDetail);
      setUncovered(nextUncovered);
    },
    [domainId],
  );

  useEffect(() => {
    if (!domainId) return;
    // Local package history is available immediately; inventory-dependent
    // badges are refreshed by the effect below once DataHub finishes.
    void refreshPackages(undefined, false).catch((err: Error) => message.error(err.message));
  }, [domainId, refreshPackages]);

  useEffect(() => {
    if (!inventoryReady || !domainId || inventoryRefreshDomainRef.current === domainId) return;
    inventoryRefreshDomainRef.current = domainId;
    void refreshPackages(pkgId ?? undefined, true).catch((err: Error) =>
      message.error(err.message),
    );
  }, [domainId, inventoryReady, pkgId, refreshPackages]);

  const sourceTableRows = useMemo(() => tables.data ?? [], [tables.data]);
  const optimisticResolvedCount = useMemo(() => {
    if (optimisticResolvedTables.size === 0) return 0;
    const isolatedFromApi = new Set(
      sourceTableRows.filter((row) => row.isolated).map((row) => row.name),
    );
    return [...optimisticResolvedTables].filter((name) => isolatedFromApi.has(name)).length;
  }, [optimisticResolvedTables, sourceTableRows]);
  const tableRows = useMemo(
    () =>
      sourceTableRows.map((row) =>
        optimisticResolvedTables.has(row.name)
          ? { ...row, isolated: false, upstream: Math.max(1, row.upstream) }
          : row,
      ),
    [optimisticResolvedTables, sourceTableRows],
  );
  const tableByName = useMemo(() => new Map(tableRows.map((row) => [row.name, row])), [tableRows]);
  const isolatedNames = useMemo(
    () => new Set(tableRows.filter((row) => row.isolated).map((row) => row.name)),
    [tableRows],
  );
  const isIsolated = useCallback((table: string) => isolatedNames.has(table), [isolatedNames]);
  const columnsOf = useCallback((table: string) => columns[table] ?? [], [columns]);

  const isolatedTotal = Math.max(0, (overview.data?.isolated ?? 0) - optimisticResolvedCount);
  const noLineageTotal = Math.max(
    0,
    (overview.data?.no_lineage ?? overview.data?.isolated ?? 0) - optimisticResolvedCount,
  );
  const noAnyRelationTotal = Math.max(
    0,
    (overview.data?.no_any_relation ?? overview.data?.isolated ?? 0) - optimisticResolvedCount,
  );
  const total = overview.data?.total ?? 0;
  const withLineage = Math.min(total, (overview.data?.with_lineage ?? 0) + optimisticResolvedCount);
  const coveragePct = total > 0 ? (withLineage / total) * 100 : 0;

  /* ---------- 待写入 ---------- */

  const selected = useMemo(
    () => (detail ? (selection[detail.id] ?? detail.groups.map((group) => group.target)) : []),
    [detail, selection],
  );

  const scanPending = useMemo(() => {
    if (!detail || uploading) return { edges: 0, blocked: 0, skipped: 0, resolved: 0 };
    const groups = detail.groups.filter((group) => selected.includes(group.target));
    const all = groups.flatMap((group) => group.edges).filter((edge) => !edge.applied);
    return {
      edges: all.filter((edge) => edge.state === "ok").length,
      blocked: all.filter((edge) => edge.state === "blocked").length,
      skipped: all.filter((edge) => edge.state === "skipped").length,
      resolved: groups.filter((group) => group.isolated).length,
    };
  }, [detail, selected, uploading]);

  const canvasPending = useMemo(() => {
    const pendingEdges = edges.filter((edge) => !appliedCanvasEdges.has(canvasEdgeSignature(edge)));
    const writable = pendingEdges.filter((edge) => edge.keys.length > 0);
    const resolved = new Set(
      writable.map((edge) => edge.to).filter((table) => isolatedNames.has(table)),
    );
    return {
      edges: writable.length,
      blocked: pendingEdges.length - writable.length,
      skipped: 0,
      resolved: resolved.size,
    };
  }, [appliedCanvasEdges, edges, isolatedNames]);

  const pending = mode === "scan" ? scanPending : canvasPending;
  const frozen = mode === "scan" ? pending.edges === 0 && (detail?.applied_edges ?? 0) > 0 : false;
  const isolatedNext = isolatedTotal - pending.resolved;

  /* ---------- 动作 ---------- */

  const loadColumns = useCallback(
    async (table: string) => {
      const row = tableByName.get(table);
      if (!domainId || !row || columns[table]) return;
      try {
        const cols = await api.lineageColumns(domainId, row.urn);
        setColumns((prev) => ({ ...prev, [table]: cols }));
      } catch (err) {
        message.error(err instanceof Error ? err.message : String(err));
      }
    },
    [columns, domainId, tableByName],
  );

  const addToCanvas = useCallback(
    (table: string) => {
      setMode("canvas");
      void loadColumns(table);
      setNodes((prev) => {
        if (prev.some((node) => node.table === table)) return prev;
        const index = prev.length;
        return [
          ...prev,
          {
            table,
            x: 16 + (index % 3) * CANVAS_COL_W,
            y: 16 + Math.floor(index / 3) * CANVAS_ROW_H,
          },
        ];
      });
    },
    [loadColumns],
  );

  // --- 人工表名映射 -------------------------------------------------------

  useEffect(() => {
    if (!domainId) return;
    let cancelled = false;
    void api
      .listLineageTableMappings(domainId)
      .then((rows) => !cancelled && setMappings(rows))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [domainId]);

  const saveMapping = async (targetUrn: string) => {
    if (!domainId || !mapTarget) return;
    setSavingMapping(true);
    try {
      const receipt = await api.saveLineageTableMapping(domainId, mapTarget, targetUrn);
      setMappings((prev) => [
        ...prev.filter((item) => item.id !== receipt.mapping.id),
        receipt.mapping,
      ]);
      setMapTarget(null);
      message.success(
        receipt.repaired > 0
          ? `已记下映射，当场修复 ${receipt.repaired} 条对不上的边`
          : "已记下映射（当前没有可修复的边）",
      );
      // 边的状态变了，重拉当前包
      if (pkgId) setDetail(await api.getLineagePackage(pkgId));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存映射失败");
    } finally {
      setSavingMapping(false);
    }
  };

  // --- 智能关系补充 -------------------------------------------------------

  const inferRunning =
    inferTask?.status === "queued" || inferTask?.status === "running";

  const reloadCandidates = useCallback(async () => {
    if (!domainId) return;
    try {
      setCandidates(await api.listRelationCandidates(domainId, { withPairs: true }));
    } catch {
      /* 候选读不出来不该打断页面；抽屉里会显示空态 */
    }
  }, [domainId]);

  // 进页面先看有没有在跑的推断——上一次可能是别的标签页/刷新前起的。
  useEffect(() => {
    if (!domainId) return;
    let cancelled = false;
    void (async () => {
      try {
        const task = await api.getLatestRelationInferenceTask(domainId);
        if (!cancelled) setInferTask(task);
      } catch {
        /* 没有就没有 */
      }
      if (!cancelled) await reloadCandidates();
    })();
    return () => {
      cancelled = true;
    };
  }, [domainId, reloadCandidates]);

  // 轮询：推断是分钟级的，2 秒一次足够，跑完即停。
  useEffect(() => {
    if (!inferTask || !inferRunning) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getRelationInferenceTask(inferTask.id);
        setInferTask(next);
        if (next.status === "succeeded") {
          await reloadCandidates();
          message.success(`推断完成：${next.summary?.families ?? 0} 个键族`);
        } else if (next.status === "failed") {
          message.error(next.error_summary ?? "推断失败");
        }
      } catch {
        /* 轮询失败下一轮再来 */
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [inferTask, inferRunning, reloadCandidates]);

  const runInference = async () => {
    if (!domainId) return;
    try {
      setInferTask(await api.startRelationInference(domainId));
      setSuggestOpen(true);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法开始推断");
    }
  };

  const decideCandidate = async (
    candidate: RelationCandidate,
    state: "confirmed" | "rejected",
  ) => {
    setDeciding(candidate.id);
    try {
      const updated = await api.decideRelationCandidate(candidate.id, state);
      setCandidates((prev) =>
        prev.map((item) => (item.id === updated.id ? updated : item)),
      );
      message.success(
        state === "confirmed"
          ? `已确认「${updated.key_name ?? updated.value_shape}」，${updated.pair_count} 条关系将参与本体生成`
          : "已否决",
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "表态失败");
    } finally {
      setDeciding(null);
    }
  };

  const createMasterDataTask = useCallback(() => {
    if (!domainId) return;
    const returnTo = `/lineage-supplement?domain=${encodeURIComponent(domainId)}`;
    navigate(`/tasks/create?kind=materialize&returnTo=${encodeURIComponent(returnTo)}`);
  }, [domainId, navigate]);

  const confirmedCandidateCount = useMemo(
    () => candidates.filter((item) => item.state === "confirmed").length,
    [candidates],
  );

  const applyRelations = async () => {
    if (!domainId || confirmedCandidateCount === 0) return;
    setApplyingRelations(true);
    try {
      const receipt = await api.applyRelationCandidates(domainId);
      await reloadCandidates();
      message.success(`已写回 ${receipt.applied} 条外键约束，${receipt.candidates_applied} 个键族完成`);
      if (receipt.failed > 0) {
        message.warning(`${receipt.failed} 条约束写回失败，候选仍保留为已确认，可重试`);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : "外键写回失败");
    } finally {
      setApplyingRelations(false);
    }
  };

  /** 已确认的族 → 画布上的建议连线（虚线，与人工实线分开）。
   *
   * **表名要过一次归一**：画布节点名来自 ``lineage_inventory``（``库.表``，如
   * ``jwsp.aj_bl_zl``），候选成员名来自 ``fetch_domain_bundle``（裸表名 ``aj_bl_zl``）——
   * 两个 DataHub 查询给的形态不一样。按裸名匹配，且**只在画布上裸名唯一时才认**，
   * 与后端 ``lineage_inventory.resolve`` 同一条口径：对不上就丢，不猜。
   */
  const suggestedEdges = useMemo<CanvasEdge[]>(() => {
    const bare = (name: string) => name.split(".").pop() ?? name;
    const byBare = new Map<string, string[]>();
    for (const node of nodes) {
      const key = bare(node.table);
      byBare.set(key, [...(byBare.get(key) ?? []), node.table]);
    }
    const resolve = (name: string) => {
      const hits = byBare.get(bare(name)) ?? [];
      return hits.length === 1 ? hits[0] : null;
    };

    const seen = new Set<string>();
    const result: CanvasEdge[] = [];
    for (const candidate of candidates) {
      if (candidate.state !== "confirmed") continue;
      for (const pair of candidate.pairs) {
        const from = resolve(pair.source_table);
        const to = resolve(pair.target_table);
        if (!from || !to || from === to) continue;
        const id = `sug:${from}.${pair.source_column}->${to}.${pair.target_column}`;
        if (seen.has(id)) continue;
        seen.add(id);
        result.push({
          id,
          from,
          to,
          keys: [
            { id: `${id}:k`, src: pair.source_column, dst: pair.target_column },
          ],
          suggested: true,
        });
      }
    }
    return result;
  }, [candidates, nodes]);

  /** 画布要画的边 = 人工连的（可编辑）+ 建议的（只读虚线）。
      建议边**不进 edges 状态**，所以画布里的增删改只会作用在人工边上。 */
  const canvasEdges = useMemo(
    () => [...edges, ...suggestedEdges],
    [edges, suggestedEdges],
  );

  const runScan = async (file: File) => {
    if (!domainId) return;
    setScanningId("uploading");
    try {
      const created = await api.uploadLineagePackage(domainId, file, dialect);
      setUploading(false);
      await refreshPackages(created.id);
      message.success(`扫描完成：${created.targets} 个落点、${created.edges_ok} 条边可上报`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setScanningId(null);
    }
  };

  const rescan = async (id: string) => {
    setScanningId(id);
    try {
      const updated = await api.rescanLineagePackage(id, dialect);
      await refreshPackages(updated.id);
      message.success(`重新扫描完成：${updated.edges_ok} 条边可上报`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setScanningId(null);
    }
  };

  const removePackage = async (id: string) => {
    try {
      await api.deleteLineagePackage(id);
      await refreshPackages();
      message.success("已删除该记录（DataHub 里的边不受影响）");
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const selectPackage = async (id: string) => {
    setPkgId(id);
    setUploading(false);
    try {
      setDetail(await api.getLineagePackage(id, inventoryReady));
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const apply = async () => {
    if (!domainId) return;
    setApplying(true);
    try {
      if (mode === "scan" && detail) {
        const receipt = await api.applyLineagePackage(detail.id, selected);
        message.success(
          `已上报 ${receipt.applied} 条 · 失败 ${receipt.failed} 条 · ${receipt.resolved} 张表脱离孤岛`,
        );
        if (receipt.failures.length > 0) {
          message.warning(`有 ${receipt.failures.length} 条边写入失败，详见回执`);
        }
        await refreshPackages(detail.id);
        await Promise.all([overview.reload(), tables.reload()]);
      } else {
        const pendingCanvasEdges = edges.filter(
          (edge) => edge.keys.length > 0 && !appliedCanvasEdges.has(canvasEdgeSignature(edge)),
        );
        const payload = pendingCanvasEdges.map((edge) => ({
          source_table: edge.from,
          target_table: edge.to,
          join_keys: edge.keys.map((key) => `${edge.from}.${key.src} = ${edge.to}.${key.dst}`),
        }));
        const attemptedKeyCount = pendingCanvasEdges.reduce(
          (count, edge) => count + edge.keys.length,
          0,
        );
        const receipt = await api.applyManualLineage(domainId, payload);
        message.success(
          `已上报 ${receipt.applied} 条 · 失败 ${receipt.failed} 条 · ${receipt.resolved} 张表脱离孤岛`,
        );
        if (
          receipt.failed === 0 &&
          receipt.applied === attemptedKeyCount &&
          attemptedKeyCount > 0
        ) {
          setAppliedCanvasEdges(
            (prev) => new Set([...prev, ...pendingCanvasEdges.map(canvasEdgeSignature)]),
          );
          const resolvedTables = new Set(
            pendingCanvasEdges
              .flatMap((edge) => [edge.from, edge.to])
              .filter((table) => isolatedNames.has(table)),
          );
          if (resolvedTables.size > 0) {
            setOptimisticResolvedTables((prev) => new Set([...prev, ...resolvedTables]));
          }
        }
        await refreshPackages(pkgId ?? undefined);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setApplying(false);
    }
  };

  /* ---------- 左栏 ---------- */

  const railRows = tableRows.filter(
    (row) =>
      (railFilter === "all" || row.isolated) &&
      row.name.toLowerCase().includes(keyword.trim().toLowerCase()),
  );
  const railWindow = useMemo(() => {
    const viewportHeight = railViewportHeight || 480;
    const start = Math.min(
      railRows.length,
      Math.max(0, Math.floor(railScrollTop / RAIL_ROW_H) - RAIL_OVERSCAN),
    );
    const end = Math.min(
      railRows.length,
      Math.ceil((railScrollTop + viewportHeight) / RAIL_ROW_H) + RAIL_OVERSCAN,
    );
    return { start, end, rows: railRows.slice(start, end) };
  }, [railRows, railScrollTop, railViewportHeight]);
  const railRowCount = railRows.length;
  const onCanvas = new Set(nodes.map((node) => node.table));

  useEffect(() => {
    if (mode !== "canvas" || !railOpen) return;
    const element = railListRef.current;
    if (!element) return;
    const updateHeight = () => setRailViewportHeight(element.clientHeight);
    updateHeight();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateHeight);
    observer.observe(element);
    return () => observer.disconnect();
  }, [mode, railOpen]);

  useEffect(() => {
    if (mode !== "canvas") return;
    setRailScrollTop(0);
    railListRef.current?.scrollTo({ top: 0 });
  }, [domainId, keyword, mode, railFilter, railRowCount]);

  // A shared domain list can take a round trip to DataHub.  When a domain is
  // already present in the URL, render the workbench immediately and let the
  // selector/statistics fill in asynchronously; this also exposes local
  // package history without waiting for the remote inventory.
  if (!domainId && domains.loading && !domains.data) {
    return <PageSkeleton type="detail" full />;
  }

  return (
    <PageContainer full>
      <div className="lin-workbench">
        <header className="lin-topbar">
          <span className="lin-topbar-title">
            <NodeIndexOutlined />
            血缘补录
          </span>

          <Select
            size="small"
            style={{ width: 168 }}
            value={selectedDomainKnown ? domainId : undefined}
            placeholder={domains.loading ? "正在加载数据域…" : "选择数据域"}
            onChange={(value) => setDomainId(value)}
            options={(domains.data ?? []).map((domain) => ({
              value: domain.id,
              label: domain.name,
            }))}
          />

          <div className="lin-stats">
            <div className="lin-stat lin-stat--iso">
              <span className="lin-stat-label">无血缘表</span>
              <span className="lin-stat-value">
                <b>{inventoryLoading ? "…" : noLineageTotal}</b>
                {!inventoryLoading && pending.resolved > 0 && (
                  <>
                    <ArrowRightOutlined className="lin-stat-arrow" />
                    <b className="lin-stat-next">{isolatedNext}</b>
                    <em>预计</em>
                  </>
                )}
              </span>
              <span className="lin-stat-note">
                无任何关系 {inventoryLoading ? "…" : noAnyRelationTotal}
              </span>
            </div>

            <div className="lin-stat">
              <span className="lin-stat-label">血缘覆盖率</span>
              <span className="lin-stat-value">
                <b>{inventoryLoading ? "—" : `${coveragePct.toFixed(1)}%`}</b>
              </span>
              <div className={`lin-meter${inventoryLoading ? " lin-meter--loading" : ""}`}>
                <i className="lin-meter-have" style={{ width: `${coveragePct}%` }} />
              </div>
            </div>

            <div className="lin-stat">
              <span className="lin-stat-label">域内表</span>
              <span className="lin-stat-value">
                <b>{inventoryLoading ? "…" : total}</b>
              </span>
            </div>

            <div className="lin-stat lin-stat--src">
              <span className="lin-stat-label">目标域</span>
              <span
                className="lin-stat-src"
                title={overview.data?.databases.join(" / ") || undefined}
              >
                {overview.data
                  ? inventoryLoading
                    ? `${overview.data.domain_name} · 正在同步 DataHub 血缘…`
                    : `${overview.data.domain_name} · ${overview.data.platform ?? "—"} · ${readableDatabaseList(
                        overview.data.databases,
                      )}`
                  : "—"}
              </span>
            </div>
          </div>

          <div className="lin-topbar-tools">
            <Segmented
              size="small"
              value={mode}
              onChange={(value) => setMode(value as Mode)}
              options={[
                { label: "代码包扫描", value: "scan" },
                { label: "画布补录", value: "canvas" },
              ]}
            />

            <div className="lin-submit-tools">
              <div className="lin-submit-summary" title={`将写入 DataHub ${pending.edges} 条边`}>
                <span className="lin-submit-count">
                  <b>{pending.edges}</b>
                  <span>条待上报</span>
                </span>
                {pending.resolved > 0 && (
                  <Tag color="success" variant="filled">
                    {pending.resolved} 张脱离孤岛
                  </Tag>
                )}
                {pending.blocked > 0 && (
                  <Tag color="warning" variant="filled">
                    {mode === "canvas" ? `待补键 ${pending.blocked}` : `待映射 ${pending.blocked}`}
                  </Tag>
                )}
                {pending.skipped > 0 && <Tag variant="filled">跳过 {pending.skipped}</Tag>}
              </div>

              <div className="lin-submit-acts">
                {mode === "canvas" && (
                  <Tooltip title="按值形状聚出键族，再由 LLM 判定哪些是真实体键。确认后的族会作为关联证据参与本体生成。">
                    <Badge count={confirmedCandidateCount} size="small" color="green">
                      <Button
                        size="small"
                        icon={<BulbOutlined />}
                        loading={inferRunning}
                        onClick={() => setSuggestOpen(true)}
                      >
                        智能补充关系
                      </Button>
                    </Badge>
                  </Tooltip>
                )}
                {mode === "scan" && (
                  <Select
                    size="small"
                    value={dialect}
                    className="lin-submit-dialect"
                    onChange={setDialect}
                    options={DIALECTS.map((item) => ({ value: item, label: `方言 ${item}` }))}
                  />
                )}
                {frozen && (
                  <Tooltip title={`已上报 ${detail?.applied_edges ?? 0} 条，重复上报不会重复建边`}>
                    <span className="lin-done">已上报</span>
                  </Tooltip>
                )}
                <Popconfirm
                  placement="bottomRight"
                  title={`确认向 DataHub 写入 ${pending.edges} 条血缘边？`}
                  description={
                    pending.resolved > 0
                      ? `写入后 ${pending.resolved} 张表将脱离孤岛，需重跑本体起草才生效。`
                      : "写入后可在 DataHub 血缘图中查看；重复上报幂等。"
                  }
                  okText="确认上报"
                  cancelText="再看看"
                  onConfirm={() => void apply()}
                  disabled={pending.edges === 0}
                >
                  <Button
                    size="small"
                    type="primary"
                    icon={<CloudUploadOutlined />}
                    loading={applying}
                    disabled={pending.edges === 0}
                    aria-label="上报到 DataHub"
                  >
                    上报
                  </Button>
                </Popconfirm>
              </div>
            </div>
          </div>
        </header>

        {overview.error && (
          <Alert type="error" showIcon title={`读取域血缘失败：${overview.error}`} />
        )}

        <div className={`lin-body${railOpen ? "" : " lin-body--rail-closed"}`}>
          <aside className="lin-rail">
            <div className="lin-rail-head">
              {railOpen && (
                <>
                  <span className="lin-rail-title">{mode === "scan" ? "代码包" : "表清单"}</span>
                  <span className="section-card-count">
                    {mode === "scan" ? packages.length : railRows.length}
                  </span>
                </>
              )}
              <Tooltip title={railOpen ? "收起" : "展开"} placement="right">
                <button
                  type="button"
                  className="lin-rail-toggle"
                  onClick={() => setRailOpen((open) => !open)}
                  aria-label={railOpen ? "收起左栏" : "展开左栏"}
                >
                  {railOpen ? <LeftOutlined /> : <RightOutlined />}
                </button>
              </Tooltip>
            </div>

            {railOpen &&
              (mode === "scan" ? (
                <PackageRail
                  packages={packages}
                  currentId={uploading ? null : pkgId}
                  scanningId={scanningId}
                  uploading={uploading}
                  onSelect={(id) => void selectPackage(id)}
                  onUpload={() => {
                    setMode("scan");
                    setUploading(true);
                  }}
                  onRescan={(id) => void rescan(id)}
                  onDelete={(id) => void removePackage(id)}
                />
              ) : (
                <>
                  <div className="lin-rail-controls">
                    <Segmented
                      block
                      size="small"
                      value={railFilter}
                      onChange={(value) => setRailFilter(value as RailFilter)}
                      options={[
                        {
                          label: `仅孤岛 ${inventoryLoading ? "…" : isolatedTotal}`,
                          value: "isolated",
                        },
                        { label: `全部 ${inventoryLoading ? "…" : total}`, value: "all" },
                      ]}
                    />
                    <Input.Search
                      size="small"
                      allowClear
                      placeholder="搜表名"
                      value={keyword}
                      onChange={(event) => setKeyword(event.target.value)}
                    />
                  </div>

                  <ul
                    ref={railListRef}
                    className="lin-rail-list"
                    onScroll={(event) => setRailScrollTop(event.currentTarget.scrollTop)}
                  >
                    {railWindow.start > 0 && (
                      <li
                        className="lin-rail-virtual-spacer"
                        aria-hidden="true"
                        style={{ height: railWindow.start * RAIL_ROW_H }}
                      />
                    )}
                    {railWindow.rows.map((row) => (
                      <li key={row.urn} className="lin-rail-row" style={{ height: RAIL_ROW_H }}>
                        <span className="lin-rail-main">
                          <span className="lin-rail-name" title={row.name}>
                            {row.isolated && <i className="lin-iso-dot" title="孤岛表" />}
                            <LineageTableName name={row.name} />
                          </span>
                          <span className="lin-rail-meta">
                            上游 {row.upstream} · 下游 {row.downstream}
                          </span>
                        </span>
                        <Tooltip title={onCanvas.has(row.name) ? "已在画布上" : "放到画布"}>
                          <Button
                            size="small"
                            type="text"
                            icon={<PlusOutlined />}
                            disabled={onCanvas.has(row.name)}
                            onClick={() => addToCanvas(row.name)}
                          />
                        </Tooltip>
                      </li>
                    ))}
                    {railWindow.end < railRows.length && (
                      <li
                        className="lin-rail-virtual-spacer"
                        aria-hidden="true"
                        style={{ height: (railRows.length - railWindow.end) * RAIL_ROW_H }}
                      />
                    )}
                    {railRows.length === 0 && (
                      <li className="lin-muted lin-rail-empty">
                        {tables.loading ? "加载中…" : "没有匹配的表"}
                      </li>
                    )}
                  </ul>
                </>
              ))}
          </aside>

          <main className={`lin-main lin-main--${mode}`}>
            {mode === "scan" ? (
              <ScanReport
                pkg={uploading ? null : detail}
                uploading={uploading}
                scanning={scanningId !== null}
                onScan={(file) => void runScan(file)}
                selected={selected}
                onSelectedChange={(keys) =>
                  detail && setSelection((prev) => ({ ...prev, [detail.id]: keys }))
                }
                frozen={frozen}
                isolated={isolatedNames}
                isolatedTotal={isolatedTotal}
                uncovered={uncovered}
                inventoryLoading={inventoryLoading}
                onSendToCanvas={addToCanvas}
                onMapTable={setMapTarget}
              />
            ) : (
              <LineageCanvas
                nodes={nodes}
                edges={canvasEdges}
                setNodes={setNodes}
                setEdges={setEdges}
                isolated={isIsolated}
                columnsOf={columnsOf}
                frozen={applying}
              />
            )}
          </main>
        </div>
      </div>

      <TableMappingModal
        open={Boolean(mapTarget)}
        sqlTable={mapTarget ?? ""}
        tables={tables.data ?? []}
        loading={needsTableInventory && tables.loading}
        mappings={mappings}
        saving={savingMapping}
        onCancel={() => setMapTarget(null)}
        onSave={saveMapping}
      />

      <RelationSuggestionDrawer
        open={suggestOpen}
        onClose={() => setSuggestOpen(false)}
        task={inferTask}
        candidates={candidates}
        deciding={deciding}
        onDecide={decideCandidate}
        onCreateTask={createMasterDataTask}
        onApply={applyRelations}
        applying={applyingRelations}
        onRun={runInference}
        running={inferRunning}
      />
    </PageContainer>
  );
}
