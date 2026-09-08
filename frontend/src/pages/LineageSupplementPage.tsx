import {
  ArrowRightOutlined,
  CloudUploadOutlined,
  LeftOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  RightOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Tag,
  Tooltip,
  message,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { LineageCanvas } from "../components/lineage/LineageCanvas";
import type { CanvasEdge, CanvasNode } from "../components/lineage/LineageCanvas";
import { reviewGroups } from "../components/lineage/graphLayout";
import { GroupRail, LONERS_GROUP } from "../components/lineage/GroupRail";
import { LineageTableName, readableDatabaseList } from "../components/lineage/LineageTableName";
import { PackageRail } from "../components/lineage/PackageRail";
import { ScanReport } from "../components/lineage/ScanReport";
import { TableMappingModal } from "../components/lineage/TableMappingModal";
import { PageContainer } from "../components/PageContainer";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { useUrlState } from "../hooks/useUrlState";
import type {
  CanvasSuggestion,
  DomainContext,
  LineageColumn,
  LineageOverview,
  LineagePackageDetail,
  LineagePackageRow,
  LineageTableMapping,
  LineageTableRow,
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
 * - **画布补录**：把已知的表摆上画布连线。摆好后点「智能补录」，机器按共用键先把线
 *   连出来（琥珀虚线），人在图上删掉不对的那几根、需要时反向或补键，剩下的一起上报。
 *   **审的单位是线，不是候选**——这一屏的人本来就在看图。
 *
 * 两条路径在「所有包都没覆盖的孤岛表」处交接：这些表一键送进画布。
 *
 * 版式是工作台：顶栏一条事实带 + 可收起的左栏 + 吃满剩余高度的工作面 + 常驻写入条。
 * 页面自己不滚动，滚动只发生在工作面内部。
 */

type Mode = "scan" | "canvas";
type RailFilter = "all" | "isolated";
/** 左栏在画布模式下的两副面孔：往画布上放表，或按连通分量逐组审。 */
type RailView = "tables" | "groups";

const CANVAS_COL_W = 320;
const CANVAS_ROW_H = 250;
/** 批量放上来的节点默认折叠，行距按折叠后的高度给（表头 32px + 呼吸）。 */
const CANVAS_ROW_H_COLLAPSED = 76;
/** 超过这个数就折叠着放：30 张表 × 每张几十个字段一次全展开，画布直接不能看。 */
const COLLAPSE_ABOVE = 8;
const RAIL_PAGE_SIZE = 50;
/** 同时向 DataHub 取字段的并发数。批量放 50 张表不能变成 50 个并发请求。 */
const COLUMN_FETCH_CONCURRENCY = 6;
/** 字段结果攒多久落一次 state。攒太久看着像没反应，不攒就是一表一次重渲染。 */
const COLUMN_FLUSH_MS = 150;
const DIALECTS = ["mysql", "postgres", "hive", "doris", "starrocks"];

export function LineageSupplementPage() {
  const [domainId, setDomainId] = useUrlState<string>("domain", "");
  const [mode, setMode] = useState<Mode>("scan");
  const [railOpen, setRailOpen] = useState(true);
  const [railFilter, setRailFilter] = useState<RailFilter>("isolated");
  const [keyword, setKeyword] = useState("");
  const [railPage, setRailPage] = useState(1);
  const [railView, setRailView] = useState<RailView>("tables");
  /** 正在审的连通分量 id；null = 画布上全部显示。 */
  const [activeGroup, setActiveGroup] = useState<string | null>(null);
  /** 表清单里勾中的表。**跨页保留**——不然「全选 138 张」翻一页就没了。 */
  const [checked, setChecked] = useState<Set<string>>(() => new Set());
  const [dialect, setDialect] = useState("mysql");

  const [packages, setPackages] = useState<LineagePackageRow[]>([]);
  const [pkgId, setPkgId] = useState<string | null>(null);
  const [detail, setDetail] = useState<LineagePackageDetail | null>(null);
  const [scanningId, setScanningId] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, string[]>>({});
  const [uncovered, setUncovered] = useState<string[]>([]);
  const [applying, setApplying] = useState(false);

  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<CanvasEdge[]>([]);
  const [columns, setColumns] = useState<Record<string, LineageColumn[]>>({});
  /** 取字段失败的表。没有它，节点会永远停在「正在取字段…」——DataHub 忙起来
      单次 get_dataset_by_urn 实测能到一分钟以上并超时。 */
  const [columnFailures, setColumnFailures] = useState<Set<string>>(() => new Set());
  const [optimisticResolvedTables, setOptimisticResolvedTables] = useState<Set<string>>(
    () => new Set<string>(),
  );
  // 人工表名映射：blocked 边此前只能重扫，而重扫用同一套解析、结果一样。
  const [mapTarget, setMapTarget] = useState<string | null>(null);
  const [mappings, setMappings] = useState<LineageTableMapping[]>([]);
  const [savingMapping, setSavingMapping] = useState(false);

  // 智能补录：作用域是画布上（或点选中的）那几张表，同步返回，直接把线摆上去。
  const [suggesting, setSuggesting] = useState(false);

  const railListRef = useRef<HTMLUListElement>(null);
  const inventoryRefreshDomainRef = useRef<string | null>(null);
  /** 已取过或正在取字段的表。``columns`` 是 state，批量循环里读到的是同一份旧快照，
      靠它去重才不会对同一张表连发几次请求。 */
  const columnsInFlightRef = useRef<Set<string>>(new Set());

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
    setOptimisticResolvedTables(new Set());
    setChecked(new Set());
    setRailPage(1);
    setRailView("tables");
    setActiveGroup(null);
    setColumnFailures(new Set());
    columnsInFlightRef.current = new Set();
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
  const columnStateOf = useCallback(
    (table: string): "loading" | "loaded" | "failed" =>
      columns[table] ? "loaded" : columnFailures.has(table) ? "failed" : "loading",
    [columnFailures, columns],
  );

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
    if (!detail) return { edges: 0, blocked: 0, skipped: 0, resolved: 0 };
    const groups = detail.groups.filter((group) => selected.includes(group.target));
    const all = groups.flatMap((group) => group.edges).filter((edge) => !edge.applied);
    return {
      edges: all.filter((edge) => edge.state === "ok").length,
      blocked: all.filter((edge) => edge.state === "blocked").length,
      skipped: all.filter((edge) => edge.state === "skipped").length,
      resolved: groups.filter((group) => group.isolated).length,
    };
  }, [detail, selected]);

  // 画布上留着的线**就是待上报的线**：上报成功的那些已经在 releaseReportedEdges 里撤掉了。
  const canvasPending = useMemo(() => {
    const writable = edges.filter((edge) => edge.keys.length > 0);
    const resolved = new Set(
      writable.map((edge) => edge.to).filter((table) => isolatedNames.has(table)),
    );
    return {
      edges: writable.length,
      blocked: edges.length - writable.length,
      skipped: 0,
      resolved: resolved.size,
    };
  }, [edges, isolatedNames]);

  const pending = mode === "scan" ? scanPending : canvasPending;
  const frozen = mode === "scan" ? pending.edges === 0 && (detail?.applied_edges ?? 0) > 0 : false;
  const isolatedNext = isolatedTotal - pending.resolved;

  /* ---------- 动作 ---------- */

  /** 取若干表的字段。**限并发 + 去重**：批量放 50 张表时，一张一个请求地并发轰
      DataHub 会把它打趴（每个请求是一次 get_dataset_by_urn）。失败的从在途集合里
      摘掉，下次还能重试。 */
  const loadColumnsFor = useCallback(
    async (tables: string[]) => {
      if (!domainId) return;
      // 家底里已经没有的表（清单刷新后消失了）标成失败，不是默默跳过——跳过的话
      // 节点会永远停在「正在取字段…」。
      const unknown = tables.filter((table) => !tableByName.has(table));
      if (unknown.length > 0) {
        setColumnFailures((prev) => {
          const next = new Set(prev);
          for (const table of unknown) next.add(table);
          return next;
        });
      }
      const queue = tables.filter((table) => {
        if (columnsInFlightRef.current.has(table)) return false;
        if (!tableByName.has(table)) return false;
        columnsInFlightRef.current.add(table);
        return true;
      });
      if (queue.length === 0) return;
      setColumnFailures((prev) => {
        if (!queue.some((table) => prev.has(table))) return prev;
        const next = new Set(prev);
        for (const table of queue) next.delete(table);
        return next;
      });

      // 结果攒起来批量落 state。一张表一次 setColumns 的话，放 138 张表就是 138 次
      // 重渲染，而画布上此时正挂着 138 个节点——界面会肉眼可见地卡住。
      let buffer: Record<string, LineageColumn[]> = {};
      let timer: number | null = null;
      const flush = () => {
        if (timer !== null) {
          window.clearTimeout(timer);
          timer = null;
        }
        if (Object.keys(buffer).length === 0) return;
        const batch = buffer;
        buffer = {};
        setColumns((prev) => ({ ...prev, ...batch }));
      };
      const scheduleFlush = () => {
        if (timer === null) timer = window.setTimeout(flush, COLUMN_FLUSH_MS);
      };

      let cursor = 0;
      let failed = 0;
      const worker = async () => {
        while (cursor < queue.length) {
          const table = queue[cursor++];
          const row = tableByName.get(table);
          if (!row) continue;
          try {
            buffer[table] = await api.lineageColumns(domainId, row.urn);
            scheduleFlush();
          } catch {
            // 从在途集合里摘掉，节点上的「重试」才能真的再发一次。
            columnsInFlightRef.current.delete(table);
            setColumnFailures((prev) => new Set(prev).add(table));
            failed += 1;
          }
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(COLUMN_FETCH_CONCURRENCY, queue.length) }, worker),
      );
      flush();
      // 逐条弹错会刷屏；批量只报一次总数，节点上会留一个「点此重试」。
      if (failed > 0) message.warning(`${failed} 张表的字段没取到，展开表头点「重试」再试`);
    },
    [domainId, tableByName],
  );

  /** 把若干表放到画布。
   *
   * 位置从**现有内容下方**另起一片网格，不跟已有节点抢格子——已有节点可能已经被人
   * 拖过位置，按序号硬算会盖上去。数量多时折叠着放：几十张表全展开字段，画布不能看。
   */
  const addManyToCanvas = useCallback(
    (tables: string[]) => {
      setMode("canvas");
      setNodes((prev) => {
        const present = new Set(prev.map((node) => node.table));
        const fresh = tables.filter((table) => !present.has(table));
        if (fresh.length === 0) return prev;

        const total = prev.length + fresh.length;
        const collapsed = total > COLLAPSE_ABOVE;
        const cols = collapsed
          ? Math.min(6, Math.max(3, Math.ceil(Math.sqrt(total))))
          : 3;
        const rowH = collapsed ? CANVAS_ROW_H_COLLAPSED : CANVAS_ROW_H;
        const baseY = 16 + Math.ceil(prev.length / 3) * CANVAS_ROW_H;

        // 折叠着放就先不取字段：一张表一次 DataHub 往返（实测数秒），138 张要几分钟，
        // 而折叠状态下这些字段一个都不显示。展开哪张再取哪张（见 LineageCanvas）。
        if (!collapsed) void loadColumnsFor(fresh);

        return [
          ...prev,
          ...fresh.map((table, index) => ({
            table,
            x: 16 + (index % cols) * CANVAS_COL_W,
            y: baseY + Math.floor(index / cols) * rowH,
            collapsed,
          })),
        ];
      });
    },
    [loadColumnsFor],
  );

  const addToCanvas = useCallback(
    (table: string) => addManyToCanvas([table]),
    [addManyToCanvas],
  );

  const addToCanvasColumns = useCallback(
    (table: string) => void loadColumnsFor([table]),
    [loadColumnsFor],
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

  // --- 智能补录 -----------------------------------------------------------

  /** 一批建议 → 画布上的边。
   *
   * 两条规矩：
   *
   * 1. **人连过的那对表原样留着**——机器不覆盖人的结论，与本仓既有的三级字段权威
   *    同一口径。人连的边哪怕方向反了，也是人的判断。
   * 2. **同一对表的多条建议并成一条边的多对键**，不是丢掉后来的那几条：案件编号和
   *    人员编号可以同时把两张表连起来，只画一根线但键要都在。这也是画布本来的口径
   *    （「同一对表再拖一次＝追加关联键」）。
   */
  const mergeSuggestions = useCallback((items: CanvasSuggestion[]) => {
    setEdges((prev) => {
      const next = [...prev];
      const find = (from: string, to: string) =>
        next.findIndex((edge) => edge.from === from && edge.to === to);

      for (const item of items) {
        const { source_table: from, target_table: to } = item;
        if (from === to) continue;
        const key = {
          id: `${from}.${item.source_column}->${to}.${item.target_column}`,
          src: item.source_column,
          dst: item.target_column,
        };

        const same = find(from, to);
        const reversed = find(to, from);
        const at = same >= 0 ? same : reversed;
        if (at >= 0) {
          const edge = next[at];
          // 人连的、或方向和建议相反的，都不动——前者是人的结论，后者动了就等于
          // 替人改方向。已经有的机器边则追加这一对键。
          if (!edge.machine || same < 0) continue;
          if (edge.keys.some((k) => k.id === key.id)) continue;
          next[at] = {
            ...edge,
            keys: [...edge.keys, key],
            machine: {
              ...edge.machine,
              extraKeys: [
                ...(edge.machine.extraKeys ?? []),
                item.key_name ?? item.value_shape,
              ],
            },
          };
          continue;
        }

        next.push({
          id: `${from}->${to}`,
          from,
          to,
          keys: [key],
          machine: {
            confidence: item.confidence,
            keyName: item.key_name,
            entityName: item.entity_name,
            valueShape: item.value_shape,
            cardinality: item.cardinality,
            reason: item.reason,
            source: item.origin,
          },
        });
      }
      return next;
    });
  }, []);

  const runSuggest = useCallback(
    async (scope: string[]) => {
      if (!domainId || scope.length < 2) return;
      setSuggesting(true);
      try {
        const report = await api.suggestCanvasEdges(domainId, scope);
        mergeSuggestions(report.suggestions);

        if (report.suggestions.length === 0) {
          message.info(
            report.families > 0
              ? `这 ${report.scanned_tables.length} 张表聚出 ${report.families} 个键族，但没有一个判成可连的键`
              : "这些表之间没找到共用的键——手工连或者再多选几张表",
          );
        } else {
          message.success(
            `预连了 ${report.suggestions.length} 条线，删掉不对的再上报`,
          );
        }
        // 对不上号的表要如实说，否则人只会看到「怎么没连出线」。
        if (report.skipped_tables.length > 0) {
          message.warning(
            `${report.skipped_tables.length} 张表在 DataHub 元数据里对不上号，没参与分析：` +
              report.skipped_tables.slice(0, 3).join("、") +
              (report.skipped_tables.length > 3 ? " …" : ""),
          );
        }
        if (report.truncated) {
          message.warning("建议过多，只画了把握最高的一批；先审完这批再补下一轮");
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : "预连线失败");
      } finally {
        setSuggesting(false);
      }
    },
    [domainId, mergeSuggestions],
  );

  const clearMachineEdges = useCallback(() => {
    setEdges((prev) => prev.filter((edge) => !edge.machine));
  }, []);

  const machineEdgeCount = useMemo(
    () => edges.filter((edge) => edge.machine).length,
    [edges],
  );


  const runScan = async (file: File) => {
    if (!domainId) return;
    setScanningId("uploading");
    try {
      const created = await api.uploadLineagePackage(domainId, file, dialect);
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
    try {
      setDetail(await api.getLineagePackage(id, inventoryReady));
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  /** 上报成功之后的收尾：把已经写进 DataHub 的那些线从画布上撤掉。
   *
   * 不撤会连着坏两件事：**已上报的线一直留在画布和分组队列里**（审完的东西永远不消失，
   * 看不出还剩什么没审），而且**这些表一直算「已在画布上」，表清单里就永远勾不动了**
   * ——上报完等于把工作台锁死，只能刷新页面。
   *
   * 节点按需回收：只收那些「参与了这次上报、且身上已经一根线都不剩」的。人自己加上来
   * 还没连的表留着——他可能正打算手工连。
   */
  const releaseReportedEdges = useCallback((reported: CanvasEdge[]) => {
    const reportedIds = new Set(reported.map((edge) => edge.id));
    const touched = new Set(reported.flatMap((edge) => [edge.from, edge.to]));
    setEdges((prev) => {
      const kept = prev.filter((edge) => !reportedIds.has(edge.id));
      const stillLinked = new Set(kept.flatMap((edge) => [edge.from, edge.to]));
      setNodes((nodes) =>
        nodes.filter((node) => !touched.has(node.table) || stillLinked.has(node.table)),
      );
      return kept;
    });
    setOptimisticResolvedTables((prev) => {
      const next = new Set(prev);
      for (const table of touched) if (isolatedNames.has(table)) next.add(table);
      return next;
    });
    setActiveGroup(null);
  }, [isolatedNames]);

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
        const pendingCanvasEdges = edges.filter((edge) => edge.keys.length > 0);
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
          releaseReportedEdges(pendingCanvasEdges);
        } else if (receipt.failed > 0) {
          // 部分失败时**一条都不撤**：回执只按 URN 报失败，对不回具体哪根线。
          // 重复上报本来就是幂等的，留着整批重来比猜着删安全。
          message.warning(`${receipt.failed} 条没写进去，画布原样留着，可直接重报`);
        }
        await refreshPackages(pkgId ?? undefined);
        // 上报改掉的正是孤岛数与血缘覆盖率，家底必须重拉——扫描那条路径一直这么做，
        // 画布这条以前漏了，于是上报完顶栏和左栏还停在旧数字上。
        await Promise.all([overview.reload(), tables.reload()]);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setApplying(false);
    }
  };

  /* ---------- 左栏 ---------- */

  const railRows = useMemo(
    () =>
      tableRows.filter(
        (row) =>
          (railFilter === "all" || row.isolated) &&
          row.name.toLowerCase().includes(keyword.trim().toLowerCase()),
      ),
    [keyword, railFilter, tableRows],
  );
  const railPageCount = Math.max(1, Math.ceil(railRows.length / RAIL_PAGE_SIZE));
  // 行数变了（换筛选/搜关键字）可能把当前页顶没了，钳回最后一页而不是渲染空白。
  const railPageSafe = Math.min(railPage, railPageCount);
  const pageRows = useMemo(
    () => railRows.slice((railPageSafe - 1) * RAIL_PAGE_SIZE, railPageSafe * RAIL_PAGE_SIZE),
    [railPageSafe, railRows],
  );
  const onCanvas = useMemo(() => new Set(nodes.map((node) => node.table)), [nodes]);

  /* ---------- 连通分量：表一多就按组审 ---------- */

  const graph = useMemo(
    () =>
      reviewGroups(
        nodes.map((node) => node.table),
        // 边的审核标签＝机器判出来的键名；人手工连的没有键名，归「手工连的」。
        edges.map((edge) => ({
          id: edge.id,
          from: edge.from,
          to: edge.to,
          label: edge.machine?.keyName ?? null,
        })),
      ),
    [edges, nodes],
  );

  /** 组没了（线被删光、表被移走）就退回全部显示，不要停在一个空组上。 */
  useEffect(() => {
    if (activeGroup === LONERS_GROUP) {
      if (graph.loners.length === 0) setActiveGroup(null);
      return;
    }
    if (activeGroup && !graph.groups.some((item) => item.id === activeGroup)) {
      setActiveGroup(null);
    }
  }, [activeGroup, graph]);

  /** 正在审的那一组：**表和线都要过滤**。只过滤表的话，这一组的表之间由别的键
      连出来的线也会画出来，人分不清哪几根是这一组要审的。 */
  const canvasFocus = useMemo(() => {
    if (!activeGroup) return null;
    if (activeGroup === LONERS_GROUP) {
      return { tables: new Set(graph.loners), edgeIds: new Set<string>() };
    }
    const hit = graph.groups.find((item) => item.id === activeGroup);
    return hit
      ? { tables: new Set(hit.tables), edgeIds: new Set(hit.edgeIds) }
      : null;
  }, [activeGroup, graph]);

  const groupIndex = graph.groups.findIndex((item) => item.id === activeGroup);

  /** 上一组 / 下一组：分组审核的主动作是「审完这组看下一组」，不该逼人回列表点。
      走到头就停，不绕回第一组——绕回去人会以为还没审完。 */
  const stepGroup = useCallback(
    (delta: number) => {
      const list = graph.groups.map((item) => item.id);
      if (graph.loners.length > 0) list.push(LONERS_GROUP);
      if (list.length === 0) return;
      const at = activeGroup ? list.indexOf(activeGroup) : -1;
      const next = Math.min(list.length - 1, Math.max(0, at + delta));
      setActiveGroup(list[next]);
    },
    [activeGroup, graph],
  );

  /** 勾选与「加入画布」都只认还没在画布上的表——已经在上面的勾了也没有动作。 */
  const selectableNames = useMemo(
    () => railRows.filter((row) => !onCanvas.has(row.name)).map((row) => row.name),
    [onCanvas, railRows],
  );
  const checkedCount = checked.size;
  const allSelected = selectableNames.length > 0 && checkedCount >= selectableNames.length;

  const toggleChecked = useCallback((table: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(table)) next.delete(table);
      else next.add(table);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setChecked((prev) => (prev.size >= selectableNames.length ? new Set() : new Set(selectableNames)));
  }, [selectableNames]);

  const addCheckedToCanvas = useCallback(() => {
    const wanted = [...checked].filter((table) => !onCanvas.has(table));
    if (wanted.length === 0) return;
    addManyToCanvas(wanted);
    setChecked(new Set());
    message.success(
      wanted.length > COLLAPSE_ABOVE
        ? `已放上 ${wanted.length} 张表（折叠显示，双击表头展开）`
        : `已放上 ${wanted.length} 张表`,
    );
  }, [addManyToCanvas, checked, onCanvas]);

  // 换域/换筛选/改关键字都回到第一页；勾选按域清（换域时那些表名已经没意义了）。
  useEffect(() => {
    setRailPage(1);
    railListRef.current?.scrollTo({ top: 0 });
  }, [domainId, keyword, railFilter]);

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
                {mode === "canvas" && machineEdgeCount > 0 && (
                  <Tooltip title="机器预连、还没经人删改的线。不删就是通过——上报时一并写入。">
                    <Tag color="orange" variant="filled">
                      机器预连 {machineEdgeCount}
                    </Tag>
                  </Tooltip>
                )}
              </div>

              <div className="lin-submit-acts">
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
              {railOpen &&
                (mode === "scan" ? (
                  <>
                    <span className="lin-rail-title">代码包</span>
                    <span className="section-card-count">{packages.length}</span>
                  </>
                ) : (
                  /* 左栏两副面孔：往画布上放表，或按连通分量逐组审。分组审核走左栏
                     而不是画布上的浮层——它是个队列（还剩几组没审），而且这样一点
                     画布空间都不占。 */
                  <Segmented
                    size="small"
                    className="lin-rail-view"
                    value={railView}
                    onChange={(value) => setRailView(value as RailView)}
                    options={[
                      { label: `表清单 ${railRows.length}`, value: "tables" },
                      { label: `分组审核 ${graph.groups.length}`, value: "groups" },
                    ]}
                  />
                ))}
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

            {railOpen && mode === "canvas" && railView === "groups" ? (
              <GroupRail
                components={graph.groups}
                loners={graph.loners}
                active={activeGroup}
                onPick={setActiveGroup}
                onStep={stepGroup}
                index={groupIndex}
              />
            ) : null}

            {railOpen &&
              !(mode === "canvas" && railView === "groups") &&
              (mode === "scan" ? (
                <PackageRail
                  packages={packages}
                  currentId={pkgId}
                  scanningId={scanningId}
                  uploading={scanningId === "uploading"}
                  onSelect={(id) => void selectPackage(id)}
                  onPick={(file) => void runScan(file)}
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

                  {/* 全选的口径是**当前筛选出的全部**，不是本页——所以数字要写在按钮上，
                      点之前就知道会勾中多少张。已经在画布上的表不参与。 */}
                  <div className="lin-rail-select">
                    <Checkbox
                      checked={allSelected}
                      indeterminate={checkedCount > 0 && !allSelected}
                      disabled={selectableNames.length === 0}
                      onChange={toggleAll}
                    >
                      全选 {selectableNames.length}
                    </Checkbox>
                    {checkedCount > 0 && (
                      <>
                        <span className="lin-rail-select-count">已选 {checkedCount}</span>
                        <Button size="small" type="link" onClick={() => setChecked(new Set())}>
                          清除
                        </Button>
                      </>
                    )}
                  </div>

                  <ul ref={railListRef} className="lin-rail-list">
                    {pageRows.map((row) => {
                      const already = onCanvas.has(row.name);
                      return (
                        <li
                          key={row.urn}
                          className={`lin-rail-row${already ? " lin-rail-row--on" : ""}`}
                        >
                          <Checkbox
                            checked={checked.has(row.name)}
                            disabled={already}
                            onChange={() => toggleChecked(row.name)}
                          />
                          <span className="lin-rail-main">
                            <span className="lin-rail-name" title={row.name}>
                              {row.isolated && <i className="lin-iso-dot" title="孤岛表" />}
                              <LineageTableName name={row.name} />
                            </span>
                            <span className="lin-rail-meta">
                              上游 {row.upstream} · 下游 {row.downstream}
                            </span>
                          </span>
                          <Tooltip title={already ? "已在画布上" : "放到画布"}>
                            <Button
                              size="small"
                              type="text"
                              icon={<PlusOutlined />}
                              disabled={already}
                              onClick={() => addToCanvas(row.name)}
                            />
                          </Tooltip>
                        </li>
                      );
                    })}
                    {railRows.length === 0 && (
                      <li className="lin-muted lin-rail-empty">
                        {tables.loading ? "加载中…" : "没有匹配的表"}
                      </li>
                    )}
                  </ul>

                  {railRows.length > RAIL_PAGE_SIZE && (
                    <div className="lin-rail-pager">
                      <Pagination
                        simple
                        size="small"
                        current={railPageSafe}
                        pageSize={RAIL_PAGE_SIZE}
                        total={railRows.length}
                        onChange={(page) => {
                          setRailPage(page);
                          railListRef.current?.scrollTo({ top: 0 });
                        }}
                      />
                    </div>
                  )}

                  {checkedCount > 0 && (
                    <div className="lin-rail-batch">
                      <Button
                        size="small"
                        type="primary"
                        block
                        icon={<PlusOutlined />}
                        onClick={addCheckedToCanvas}
                      >
                        把选中的 {checkedCount} 张放到画布
                      </Button>
                    </div>
                  )}
                </>
              ))}
          </aside>

          <main className={`lin-main lin-main--${mode}`}>
            {mode === "scan" ? (
              <ScanReport
                pkg={detail}
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
                edges={edges}
                setNodes={setNodes}
                setEdges={setEdges}
                isolated={isIsolated}
                columnsOf={columnsOf}
                columnStateOf={columnStateOf}
                onNeedColumns={addToCanvasColumns}
                frozen={applying}
                focus={canvasFocus}
                onSuggest={(scope) => void runSuggest(scope)}
                suggesting={suggesting}
                onClearMachine={clearMachineEdges}
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
    </PageContainer>
  );
}
