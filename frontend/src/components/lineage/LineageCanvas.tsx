import {
  AimOutlined,
  ApartmentOutlined,
  CloseOutlined,
  DeleteOutlined,
  MinusOutlined,
  PlusOutlined,
  SwapOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Button, Popconfirm, Select, Tag, Tooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, PointerEvent as ReactPointerEvent, SetStateAction } from "react";
import type { LineageColumn } from "../../types";
import { assignLayers, reviewGroups, curve } from "./graphLayout";
import { LineageTableName } from "./LineageTableName";

/**
 * 路径 B：把表摆到画布上，像连 ER 图一样连血缘。
 *
 * 这是个重操作，四条设计取舍：
 *
 * 1. **一次手势产出一条边 + 一对关联键**——从字段圆点拖到另一张表的字段上。
 *    血缘边光有方向对关系推断没用，键必须和边同时产生，否则一定会有人只连线不填键。
 * 2. **同一对表再拖一次＝追加关联键**，不是新建第二条边。复合键在 ERP 里是常态
 *    （单号 + 行号），拆成两条边会让上报出现重复。
 * 3. **空间**：节点默认只有 228px 宽、字段行 22px，表头可双击折叠成一条；画布自己
 *    吃掉视口剩余高度，缩放与自动分层布局是常驻按钮而不是藏在菜单里——表一多，
 *    手工摆位就是这一屏最贵的操作。
 */

export interface CanvasNode {
  table: string;
  x: number;
  y: number;
  collapsed?: boolean;
}

export interface CanvasKey {
  id: string;
  src: string;
  dst: string;
}

/** 一根线是机器预连出来的时候，它凭什么这么连。人点开线看的就是这个。 */
export interface MachineOrigin {
  confidence: number;
  keyName: string | null;
  entityName: string | null;
  valueShape: string;
  cardinality: string;
  reason: string | null;
  /** key_family（本次现推）/ confirmed_family（此前人工确认过的族） */
  source: string;
  /** 同一对表被别的键也连上时，那些键的名字。判据仍以把握最高的那个键为准。 */
  extraKeys?: string[];
}

export interface CanvasEdge {
  id: string;
  from: string;
  to: string;
  keys: CanvasKey[];
  /** 智能补录预连出来的线：画琥珀色虚线，**照样可编辑**——人的动作是删错的那几根。 */
  machine?: MachineOrigin;
}

const NODE_W = 228;
const HEADER_H = 32;
const ROW_H = 22;
const BODY_PAD = 5;
const GRID = 8;
const MIN_K = 0.5;
const MAX_K = 1.4;
/** 连通分量之间的横带间距。要明显大于带内的 24px 行距，边界才看得出来。 */
const BAND_GAP = 96;

const CARDINALITY_LABEL: Record<string, string> = {
  one_to_one: "1:1",
  one_to_many: "1:N",
  many_to_one: "N:1",
  many_to_many: "N:M",
};

/** 反向一条边，基数字面量跟着翻——不翻就会出现「N:1」画在 1:N 的方向上。 */
function flipCardinality(value: string): string {
  if (value === "one_to_many") return "many_to_one";
  if (value === "many_to_one") return "one_to_many";
  return value;
}

type Drag =
  | {
      kind: "node";
      table: string;
      offX: number;
      offY: number;
      /** 按下时的屏幕坐标 + 修饰键：抬起时几乎没动＝这是一次点选，不是拖动。 */
      downX: number;
      downY: number;
      additive: boolean;
    }
  | { kind: "pan"; startX: number; startY: number; origX: number; origY: number }
  | { kind: "link"; from: { table: string; col: string | null }; x: number; y: number };

interface PendingNodePosition {
  table: string;
  x: number;
  y: number;
}

/** 指针捕获失败（指针已抬起、合成事件）不该把整个拖拽手势带崩。 */
function capture(el: HTMLElement | null, pointerId: number) {
  try {
    el?.setPointerCapture(pointerId);
  } catch {
    /* 没有活动指针时忽略 */
  }
}

/** 关联键里的一列。字段没取回来时**不给空下拉**——那会让人以为这张表没有字段；
    给一个只含当前值的选项，并提示去展开表头把字段取回来。 */
function ColumnPicker({
  value,
  columns,
  disabled,
  onChange,
}: {
  value: string;
  columns: LineageColumn[];
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  const loaded = columns.length > 0;
  const options = loaded
    ? columns.map((col) => ({
        value: col.name,
        label: col.name,
        title: `${col.name} · ${col.data_type ?? "—"}`,
      }))
    : [{ value, label: value, title: value }];
  return (
    <Select
      size="small"
      showSearch
      className="lin-key-select"
      value={value}
      disabled={disabled}
      options={options}
      title={loaded ? value : `${value}（字段还没取回来：双击表头展开这张表）`}
      popupMatchSelectWidth={false}
      onChange={onChange}
    />
  );
}

interface CanvasTableNodeProps {
  node: CanvasNode;
  columns: LineageColumn[];
  /** 字段取到哪一步了。折叠的节点不取字段，``columns`` 空既可能是"还没取"也可能是
      "这表真没字段"，靠它区分——不然计数会一律显示 0，看着像表是空的。
      ``failed`` 必须单列：DataHub 忙起来会超时，没有这个状态节点会永远停在
      "正在取字段…"，人不知道该等还是该重试。 */
  columnState: "loading" | "loaded" | "failed";
  dropTarget: boolean;
  dropColumn: string | null;
  isolated: boolean;
  picked: boolean;
  /** 与选中的表直接相连（自己没被选中）。 */
  related: boolean;
  /** 跟选中的表没关系——压暗，让人一眼看出哪些是相关的。 */
  dim: boolean;
  frozen: boolean;
  onStartDrag: (event: ReactPointerEvent, node: CanvasNode) => void;
  onToggleCollapse: (table: string) => void;
  onStartLink: (event: ReactPointerEvent, table: string, col: string | null) => void;
  onRemoveNode: (table: string) => void;
  onRetryColumns: (table: string) => void;
}

const CanvasTableNode = memo(function CanvasTableNode({
  node,
  columns,
  columnState,
  dropTarget,
  dropColumn,
  isolated,
  picked,
  related,
  dim,
  frozen,
  onStartDrag,
  onToggleCollapse,
  onStartLink,
  onRemoveNode,
  onRetryColumns,
}: CanvasTableNodeProps) {
  return (
    <div
      className={`lin-tnode${dropTarget ? " lin-tnode--drop" : ""}${
        picked ? " lin-tnode--picked" : ""
      }${related ? " lin-tnode--related" : ""}${dim ? " lin-tnode--dim" : ""}`}
      style={{
        left: 0,
        top: 0,
        width: NODE_W,
        transform: `translate3d(${node.x}px, ${node.y}px, 0)`,
      }}
      data-table={node.table}
    >
      <div
        className="lin-tnode-head"
        onPointerDown={(event) => onStartDrag(event, node)}
        onDoubleClick={() => onToggleCollapse(node.table)}
        data-table={node.table}
      >
        {isolated && <i className="lin-iso-dot" title="孤岛表" />}
        <LineageTableName className="lin-tnode-name" name={node.table} />
        <span className="lin-tnode-count" title={columnState === "failed" ? "字段没取到" : undefined}>
          {columnState === "loaded" ? columns.length : columnState === "failed" ? "!" : "…"}
        </span>
        {!frozen && (
          <button
            type="button"
            className="lin-tnode-x"
            aria-label="从画布移除"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={() => onRemoveNode(node.table)}
          >
            <CloseOutlined />
          </button>
        )}
      </div>

      {!node.collapsed && (
        <div className="lin-tnode-body">
          {columnState === "loading" && <div className="lin-col lin-muted">正在取字段…</div>}
          {columnState === "failed" && (
            <button
              type="button"
              className="lin-col lin-col-retry"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => onRetryColumns(node.table)}
            >
              字段没取到，点此重试
            </button>
          )}
          {columns.map((col) => (
            <div
              key={col.name}
              className={`lin-col${dropColumn === col.name ? " lin-col--drop" : ""}`}
              data-table={node.table}
              data-col={col.name}
            >
              <span className="lin-col-name">
                {col.is_primary_key && <em className="lin-pk">PK</em>}
                {col.name}
              </span>
              <span className="lin-col-type">{col.data_type}</span>
              <span
                className="lin-port"
                onPointerDown={(event) => onStartLink(event, node.table, col.name)}
                title="从这里拖到下游表的字段"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
});

interface Props {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  setNodes: Dispatch<SetStateAction<CanvasNode[]>>;
  setEdges: Dispatch<SetStateAction<CanvasEdge[]>>;
  isolated: (table: string) => boolean;
  /** 表的字段。画布拖字段连线要用，字段来自 DataHub 的 schema。 */
  columnsOf: (table: string) => LineageColumn[];
  /** 这张表的字段取到哪一步了。批量放上来的表是折叠的、不预取字段。 */
  columnStateOf: (table: string) => "loading" | "loaded" | "failed";
  /** 展开一张还没取过字段的表时按需去取——一次取几十上百张表的字段要几分钟，
      而折叠着根本不显示字段。取失败后点节点里的重试也走这里。 */
  onNeedColumns: (table: string) => void;
  frozen: boolean;
  /** 正在审的那一组：只画这些表和这些线。null = 全部显示。
      **是过滤不是压暗**：分组审核的意义就是这一屏只剩这一组，压暗留不出空间。
      线也要过滤——只滤表的话，这些表之间由别的键连出来的线也会冒出来。 */
  focus: { tables: Set<string>; edgeIds: Set<string> } | null;
  /** 智能补录：在这些表之间预连线。选中了就只算选中的，没选就算画布上全部。 */
  onSuggest: (tables: string[]) => void;
  suggesting: boolean;
  /** 清掉全部还带机器标记的线——预连错得离谱时的一键撤销。 */
  onClearMachine: () => void;
}

export function LineageCanvas({
  nodes,
  edges,
  setNodes,
  setEdges,
  isolated,
  columnsOf,
  columnStateOf,
  onNeedColumns,
  frozen,
  focus: groupFocus,
  onSuggest,
  suggesting,
  onClearMachine,
}: Props) {
  const nodeHeight = (node: CanvasNode) =>
    node.collapsed ? HEADER_H : HEADER_H + BODY_PAD * 2 + columnsOf(node.table).length * ROW_H;

  /** 字段行的中心 y；折叠或找不到字段时落到表头中心。 */
  const portY = (node: CanvasNode, col: string | null) => {
    if (node.collapsed || !col) return node.y + HEADER_H / 2;
    const index = columnsOf(node.table).findIndex((c) => c.name === col);
    if (index < 0) return node.y + HEADER_H / 2;
    return node.y + HEADER_H + BODY_PAD + index * ROW_H + ROW_H / 2;
  };

  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ x: 24, y: 16, k: 1 });
  const [drag, setDrag] = useState<Drag | null>(null);
  const [hover, setHover] = useState<{ table: string; col: string | null } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  /** 点选中的表。智能补录的作用域就是它——空集＝画布上全部。 */
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const pendingNodePosition = useRef<PendingNodePosition | null>(null);
  const nodePositionFrame = useRef<number | null>(null);

  const shownNodes = useMemo(
    () => (groupFocus ? nodes.filter((n) => groupFocus.tables.has(n.table)) : nodes),
    [nodes, groupFocus],
  );
  const shownEdges = useMemo(
    () => (groupFocus ? edges.filter((e) => groupFocus.edgeIds.has(e.id)) : edges),
    [edges, groupFocus],
  );

  const nodeMap = useMemo(() => new Map(nodes.map((n) => [n.table, n])), [nodes]);
  const columnsByTable = useMemo(
    () => new Map(nodes.map((node) => [node.table, columnsOf(node.table)])),
    [columnsOf, nodes],
  );
  const selectedEdge = edges.find((e) => e.id === selected) ?? null;

  const toWorld = useCallback(
    (clientX: number, clientY: number) => {
      const rect = ref.current?.getBoundingClientRect();
      if (!rect) return { x: 0, y: 0 };
      return {
        x: (clientX - rect.left - view.x) / view.k,
        y: (clientY - rect.top - view.y) / view.k,
      };
    },
    [view],
  );

  /* ---------- 指针 ---------- */

  const startNodeDrag = useCallback(
    (event: ReactPointerEvent, node: CanvasNode) => {
      if (frozen) return;
      event.stopPropagation();
      const p = toWorld(event.clientX, event.clientY);
      setDrag({
        kind: "node",
        table: node.table,
        offX: p.x - node.x,
        offY: p.y - node.y,
        downX: event.clientX,
        downY: event.clientY,
        additive: event.shiftKey || event.metaKey || event.ctrlKey,
      });
      capture(ref.current, event.pointerId);
    },
    [frozen, toWorld],
  );

  const startLink = useCallback(
    (event: ReactPointerEvent, table: string, col: string | null) => {
      if (frozen) return;
      event.stopPropagation();
      const p = toWorld(event.clientX, event.clientY);
      setDrag({ kind: "link", from: { table, col }, x: p.x, y: p.y });
      capture(ref.current, event.pointerId);
    },
    [frozen, toWorld],
  );

  const startPan = (event: ReactPointerEvent) => {
    setSelected(null);
    setPicked(new Set());
    setDrag({
      kind: "pan",
      startX: event.clientX,
      startY: event.clientY,
      origX: view.x,
      origY: view.y,
    });
    capture(ref.current, event.pointerId);
  };

  const flushNodePosition = useCallback(() => {
    if (nodePositionFrame.current !== null) {
      cancelAnimationFrame(nodePositionFrame.current);
      nodePositionFrame.current = null;
    }
    const next = pendingNodePosition.current;
    pendingNodePosition.current = null;
    if (!next) return;
    setNodes((prev) => {
      const current = prev.find((node) => node.table === next.table);
      if (!current || (current.x === next.x && current.y === next.y)) return prev;
      return prev.map((node) =>
        node.table === next.table ? { ...node, x: next.x, y: next.y } : node,
      );
    });
  }, [setNodes]);

  const scheduleNodePosition = useCallback(
    (next: PendingNodePosition) => {
      pendingNodePosition.current = next;
      if (nodePositionFrame.current !== null) return;
      nodePositionFrame.current = requestAnimationFrame(flushNodePosition);
    },
    [flushNodePosition],
  );

  useEffect(
    () => () => {
      if (nodePositionFrame.current !== null) cancelAnimationFrame(nodePositionFrame.current);
      nodePositionFrame.current = null;
      pendingNodePosition.current = null;
    },
    [],
  );

  const onPointerMove = (event: ReactPointerEvent) => {
    if (!drag) return;
    if (drag.kind === "pan") {
      setView((v) => ({
        ...v,
        x: drag.origX + (event.clientX - drag.startX),
        y: drag.origY + (event.clientY - drag.startY),
      }));
      return;
    }
    const p = toWorld(event.clientX, event.clientY);
    if (drag.kind === "node") {
      const x = Math.round((p.x - drag.offX) / GRID) * GRID;
      const y = Math.round((p.y - drag.offY) / GRID) * GRID;
      scheduleNodePosition({ table: drag.table, x, y });
      return;
    }
    setDrag({ ...drag, x: p.x, y: p.y });
    const el = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
    const target = el?.closest("[data-table]") as HTMLElement | null;
    if (target && target.dataset.table && target.dataset.table !== drag.from.table) {
      setHover({ table: target.dataset.table, col: target.dataset.col ?? null });
    } else {
      setHover(null);
    }
  };

  const onPointerUp = (event: ReactPointerEvent) => {
    if (drag?.kind === "node") {
      flushNodePosition();
      // 按下到抬起几乎没移动＝这是一次点选。用位移判，而不是另接 onClick——
      // 拖动结束时浏览器同样会派发 click，两者只能靠位移分开。
      const moved =
        Math.abs(event.clientX - drag.downX) + Math.abs(event.clientY - drag.downY);
      if (moved < 5) {
        const table = drag.table;
        setPicked((prev) => {
          const next = new Set(drag.additive ? prev : []);
          if (prev.has(table) && (drag.additive || prev.size === 1)) next.delete(table);
          else next.add(table);
          return next;
        });
        setSelected(null);
      }
    }
    if (drag?.kind === "link") {
      const el = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
      const target = el?.closest("[data-table]") as HTMLElement | null;
      const table = target?.dataset.table;
      if (table && table !== drag.from.table) {
        connect(drag.from.table, drag.from.col, table, target?.dataset.col ?? null);
      }
    }
    setDrag(null);
    setHover(null);
  };

  /** 同一对表之间已有边就并入键，不新建——复合键是一条边的多对字段。 */
  const connect = (from: string, fromCol: string | null, to: string, toCol: string | null) => {
    const edgeId = `${from}->${to}`;
    if (fromCol && toCol) setSelected(edgeId);
    setEdges((prev) => {
      const existing = prev.find((e) => e.from === from && e.to === to);
      if (!fromCol || !toCol) {
        return existing ? prev : [...prev, { id: edgeId, from, to, keys: [] }];
      }
      const key: CanvasKey = { id: `${from}.${fromCol}->${to}.${toCol}`, src: fromCol, dst: toCol };
      if (!existing) {
        const edge = { id: edgeId, from, to, keys: [key] };
        return [...prev, edge];
      }
      if (existing.keys.some((k) => k.id === key.id)) return prev;
      // 人往机器预连的线上补了一对键 → 这根线已经过人手，不再算机器的。
      return prev.map((e) =>
        e === existing ? { ...e, keys: [...e.keys, key], machine: undefined } : e,
      );
    });
  };

  /* ---------- 编辑 ---------- */

  const removeNode = useCallback(
    (table: string) => {
      setNodes((prev) => prev.filter((n) => n.table !== table));
      setEdges((prev) => prev.filter((e) => e.from !== table && e.to !== table));
      setPicked((prev) => {
        if (!prev.has(table)) return prev;
        const next = new Set(prev);
        next.delete(table);
        return next;
      });
    },
    [setEdges, setNodes],
  );

  const toggleCollapse = useCallback(
    (table: string) => {
      // 展开的那一刻才去取字段（折叠时不显示字段，取了也是白取）。
      if (columnStateOf(table) !== "loaded") onNeedColumns(table);
      setNodes((prev) =>
        prev.map((n) => (n.table === table ? { ...n, collapsed: !n.collapsed } : n)),
      );
    },
    [columnStateOf, onNeedColumns, setNodes],
  );

  const reverseEdge = (id: string) => {
    // 边的 id 就是 `上游->下游`，反向后它变了；选中态跟着改，否则一点「反向」
    // 检查面板就消失了——看着像点了没反应，而反向恰恰是审这批预连线的主要动作。
    const edge = edges.find((e) => e.id === id);
    if (edge) setSelected(`${edge.to}->${edge.from}`);
    setEdges((prev) =>
      prev.map((e) =>
        e.id === id
          ? {
              ...e,
              id: `${e.to}->${e.from}`,
              from: e.to,
              to: e.from,
              keys: e.keys.map((k) => ({
                id: `${e.to}.${k.dst}->${e.from}.${k.src}`,
                src: k.dst,
                dst: k.src,
              })),
              // 判据（凭哪个键连的）反向后依然成立，留着；只有基数要跟着翻。
              machine: e.machine
                ? { ...e.machine, cardinality: flipCardinality(e.machine.cardinality) }
                : undefined,
            }
          : e,
      ),
    );
  };

  const removeEdge = (id: string) => {
    setEdges((prev) => prev.filter((e) => e.id !== id));
    setSelected(null);
  };

  const removeKey = (edgeId: string, keyId: string) =>
    setEdges((prev) =>
      prev.map((e) => (e.id === edgeId ? { ...e, keys: e.keys.filter((k) => k.id !== keyId) } : e)),
    );

  /** 改一对关联键的某一端。
   *
   * 键的 id 是由两端字段名拼出来的，改了字段就得重算——上报去重、"这对键已经存在"
   * 的判断都认它。改到和已有的另一对键重合时**并成一对**，不留两条一模一样的。
   * 机器预连的边一经人手修改就不再算机器的（与拖字段补键同一条口径）。
   */
  const setKeyColumn = (
    edgeId: string,
    keyId: string,
    side: "src" | "dst",
    column: string,
  ) =>
    setEdges((prev) =>
      prev.map((edge) => {
        if (edge.id !== edgeId) return edge;
        const target = edge.keys.find((k) => k.id === keyId);
        if (!target || target[side] === column) return edge;
        const next = { ...target, [side]: column };
        next.id = `${edge.from}.${next.src}->${edge.to}.${next.dst}`;
        const keys = edge.keys
          .map((k) => (k.id === keyId ? next : k))
          .filter((k, index, all) => all.findIndex((o) => o.id === k.id) === index);
        return { ...edge, keys, machine: undefined };
      }),
    );

  /** 再加一对字段：默认取两边的第一个字段，人再改。**不给空键**——空键上报时会被
      当成"缺键"整条丢掉，人却以为加上了。 */
  const addKey = (edge: CanvasEdge) => {
    const src = (columnsByTable.get(edge.from) ?? [])[0]?.name;
    const dst = (columnsByTable.get(edge.to) ?? [])[0]?.name;
    if (!src || !dst) {
      onNeedColumns(edge.from);
      onNeedColumns(edge.to);
      return;
    }
    const key: CanvasKey = { id: `${edge.from}.${src}->${edge.to}.${dst}`, src, dst };
    setEdges((prev) =>
      prev.map((e) =>
        e.id === edge.id && !e.keys.some((k) => k.id === key.id)
          ? { ...e, keys: [...e.keys, key], machine: undefined }
          : e,
      ),
    );
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
      if ((event.key === "Delete" || event.key === "Backspace") && selected && !frozen) {
        const active = document.activeElement?.tagName;
        if (active === "INPUT" || active === "TEXTAREA") return;
        removeEdge(selected);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, frozen]);

  /* ---------- 布局 / 视图 ---------- */

  /** 整理布局：**每个连通分量占一条自己的横带**，带内再按上下游分层。
   *
   * 以前是整张图一起分层：所有"没有上游"的表挤进第 0 列，互不相干的几组表混成一片，
   * 分量之间看不出边界。而这一屏的审核单位正是「一组连得上的表」——分量必须在版面上
   * 就是分开的，切到某一组时才能直接框住它。孤立表最后单独排一片密网格。
   */
  const autoLayout = () => {
    // 分组审核时只排当前这一组——「整理布局」的意思是「把我正在看的这堆理顺」，
    // 顺手把看不见的几十张表挪位置只会让人切回全部时发现图全变了。
    const scopeNodes = shownNodes;
    const scopeEdges = shownEdges;
    const { groups, loners } = reviewGroups(
      scopeNodes.map((n) => n.table),
      scopeEdges.map((e) => ({ id: e.id, from: e.from, to: e.to, label: e.machine?.keyName ?? null })),
    );
    const byTable = new Map(scopeNodes.map((n) => [n.table, n]));
    const placed: CanvasNode[] = [];
    let bandY = 16;

    for (const component of groups) {
      const members = new Set(component.tables);
      const layer = assignLayers(
        component.tables,
        scopeEdges.filter((e) => members.has(e.from) && members.has(e.to)),
      );
      const byLayer = new Map<number, CanvasNode[]>();
      for (const table of component.tables) {
        const node = byTable.get(table);
        if (!node) continue;
        const l = layer.get(table) ?? 0;
        byLayer.set(l, [...(byLayer.get(l) ?? []), node]);
      }
      let bandBottom = bandY;
      [...byLayer.keys()]
        .sort((a, b) => a - b)
        .forEach((l, columnIndex) => {
          let y = bandY;
          (byLayer.get(l) ?? []).forEach((node) => {
            placed.push({ ...node, x: 16 + columnIndex * (NODE_W + 96), y });
            y += nodeHeight(node) + 24;
          });
          bandBottom = Math.max(bandBottom, y);
        });
      bandY = bandBottom + BAND_GAP;
    }

    // 一条线都没有的表排成密网格放在最后，只占一小片。
    loners.forEach((table, index) => {
      const node = byTable.get(table);
      if (!node) return;
      placed.push({
        ...node,
        collapsed: true,
        x: 16 + (index % 6) * (NODE_W + 24),
        y: bandY + Math.floor(index / 6) * 48,
      });
    });

    // **只覆盖排过的那些**，不能整个替换：分组审核时 scope 只有这一组，
    // 直接 setNodes(placed) 会把画布上其余上百张表一起删掉。
    const moved = new Map(placed.map((node) => [node.table, node]));
    setNodes((prev) => prev.map((node) => moved.get(node.table) ?? node));
    // 按**刚排好的**坐标框住，不是排之前那批。分组审核时一组可能很宽（73 张表的星形），
    // 缩放硬回 1.0 会把刚理好的图顶出视口。
    setView(viewFor(placed) ?? { x: 24, y: 16, k: 1 });
  };

  const zoom = (delta: number) =>
    setView((v) => ({
      ...v,
      k: Math.min(MAX_K, Math.max(MIN_K, Number((v.k + delta).toFixed(2)))),
    }));

  /** 框住给定的这些节点。**收一个显式的节点数组**而不是读 state：整理布局要按
      刚排好的坐标框，而那批坐标这一帧还没进 state。 */
  const viewFor = (list: CanvasNode[]) => {
    if (list.length === 0 || !ref.current) return null;
    const maxX = Math.max(...list.map((n) => n.x + NODE_W));
    const maxY = Math.max(...list.map((n) => n.y + nodeHeight(n)));
    const minX = Math.min(...list.map((n) => n.x));
    const minY = Math.min(...list.map((n) => n.y));
    const box = ref.current.getBoundingClientRect();
    const k = Math.min(
      MAX_K,
      Math.max(
        MIN_K,
        Math.min((box.width - 48) / (maxX - minX), (box.height - 48) / (maxY - minY)),
      ),
    );
    return { k, x: 24 - minX * k, y: 24 - minY * k };
  };

  const fit = useCallback(() => {
    const next = viewFor(shownNodes);
    if (next) setView(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownNodes]);

  /** 切到另一组时自动框住它——分组审核的动作是「下一组」，每切一次还要手动缩放
      就等于没分组。只在**换组**时触发，不跟着拖节点重新框。 */
  const fitKey = groupFocus ? [...groupFocus.tables].sort().join("|") : "";
  const lastFitKey = useRef<string | null>(null);
  useEffect(() => {
    if (lastFitKey.current === fitKey) return;
    lastFitKey.current = fitKey;
    if (fitKey) fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  /* ---------- 渲染 ---------- */

  /** 选中节点（或选中一条边）时的「相关」集合：选中的那些表 + 与它们直接相连的表。
   *
   * 空集 = 没有选中任何东西，此时**谁都不压暗**——一屏上百个节点，默认全暗只会
   * 让人以为界面坏了。压暗只在人主动圈定范围之后发生。
   */
  const focus = useMemo(() => {
    const seeds = new Set(picked);
    const active = shownEdges.find((e) => e.id === selected);
    if (active) {
      seeds.add(active.from);
      seeds.add(active.to);
    }
    if (seeds.size === 0) return null;
    const neighbours = new Set(seeds);
    for (const edge of shownEdges) {
      if (seeds.has(edge.from)) neighbours.add(edge.to);
      if (seeds.has(edge.to)) neighbours.add(edge.from);
    }
    return { seeds, neighbours };
  }, [shownEdges, picked, selected]);

  const links = shownEdges.flatMap((edge) => {
    const a = nodeMap.get(edge.from);
    const b = nodeMap.get(edge.to);
    if (!a || !b) return [];
    const rows = edge.keys.length > 0 ? edge.keys : [null];
    return rows.map((key, index) => ({
      id: `${edge.id}#${index}`,
      edgeId: edge.id,
      keyless: key === null,
      machine: Boolean(edge.machine),
      // 与选中的表直接相连 = 相关；其余的压暗，但不隐藏——藏起来人会以为线没了。
      dim: Boolean(focus) && !focus?.seeds.has(edge.from) && !focus?.seeds.has(edge.to),
      d: curve(a.x + NODE_W, portY(a, key ? key.src : null), b.x, portY(b, key ? key.dst : null)),
    }));
  });

  const ghost =
    drag?.kind === "link"
      ? (() => {
          const a = nodeMap.get(drag.from.table);
          if (!a) return null;
          return curve(a.x + NODE_W, portY(a, drag.from.col), drag.x, drag.y);
        })()
      : null;

  // 智能补录的作用域：点选了就只算点选的，一张没选就算画布上全部。
  const scope = picked.size > 0 ? [...picked] : nodes.map((node) => node.table);
  const machineCount = edges.filter((edge) => edge.machine).length;

  return (
    <div className="lin-canvas-wrap">
      <div className="lin-canvas-toolbar">
        <Tooltip
          title={
            nodes.length < 2
              ? "画布上至少要有两张表"
              : picked.size === 1
                ? "只选中一张表连不出线：Shift 点表头再选一张，或点空白取消选择、对画布上全部表补录"
                : picked.size > 1
                  ? `在点选的这 ${picked.size} 张表之间按共用键预连线`
                  : `在画布上全部 ${nodes.length} 张表之间按共用键预连线（点表头可先圈定范围）`
          }
        >
          <Button
            size="small"
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={suggesting}
            disabled={frozen || scope.length < 2}
            onClick={() => onSuggest(scope)}
          >
            智能补录
            {picked.size > 0 && <span className="lin-scope-badge">{picked.size}</span>}
          </Button>
        </Tooltip>

        {/* 工具条浮在画布上方、会盖住节点表头，所以这里只放最短的形态：
            一根虚线 + 条数。「删掉不对的再上报」这句话在底部提示条和上报摘要里都有。 */}
        {machineCount > 0 && (
          <>
            <Tooltip title={`${machineCount} 条机器预连、还没经人删改的线。不删就是通过。`}>
              <span className="lin-machine-note">
                <i className="lin-machine-dot" />
                <b>{machineCount}</b>
              </span>
            </Tooltip>
            <Popconfirm
              title={`清掉这 ${machineCount} 条机器预连的线？`}
              description="你自己连的线不受影响。"
              okText="清掉"
              cancelText="不了"
              onConfirm={onClearMachine}
            >
              <Tooltip title="清掉全部机器预连的线">
                <Button size="small" type="text" disabled={frozen}>
                  清空
                </Button>
              </Tooltip>
            </Popconfirm>
          </>
        )}

        <span className="lin-toolbar-gap" />

        <Tooltip title="按上下游自动分层排布">
          <Button size="small" icon={<ApartmentOutlined />} onClick={autoLayout} disabled={frozen}>
            整理布局
          </Button>
        </Tooltip>
        <Tooltip title="缩放到全部节点">
          <Button size="small" icon={<AimOutlined />} onClick={fit} />
        </Tooltip>
        <div className="lin-zoom">
          <Button size="small" icon={<MinusOutlined />} onClick={() => zoom(-0.1)} />
          <span className="lin-zoom-num">{Math.round(view.k * 100)}%</span>
          <Button size="small" icon={<PlusOutlined />} onClick={() => zoom(0.1)} />
        </div>
      </div>

      <div
        ref={ref}
        className={`lin-canvas${drag?.kind === "link" ? " lin-canvas--linking" : ""}`}
        onPointerDown={startPan}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div
          className="lin-world"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
        >
          <svg className="lin-wires" width={6000} height={4000}>
            <defs>
              <marker
                id="lin-tip"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" fill="var(--om-machine)" />
              </marker>
              <marker
                id="lin-tip-on"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" fill="var(--om-primary)" />
              </marker>
              <marker
                id="lin-tip-warn"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" fill="var(--om-warning)" />
              </marker>
              <marker
                id="lin-tip-machine"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" fill="var(--om-machine-edge)" />
              </marker>
            </defs>

            {links.map((link) => {
              const on = link.edgeId === selected;
              // 机器预连的线走琥珀虚线：它可编辑、也会被上报，但**没经人看过**，
              // 视觉上必须和人自己连的实线一眼分得开——这一屏的工作就是把它们看一遍。
              const stroke = on
                ? "var(--om-primary)"
                : link.machine
                  ? "var(--om-machine-edge)"
                  : link.keyless
                    ? "var(--om-warning)"
                    : "var(--om-machine)";
              return (
                <g key={link.id} className={link.dim ? "lin-wire--dim" : undefined}>
                  <path
                    className="lin-wire-hit"
                    d={link.d}
                    onPointerDown={(event) => {
                      event.stopPropagation();
                      setSelected(link.edgeId);
                    }}
                  />
                  <path
                    d={link.d}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={on ? 2.2 : 1.5}
                    strokeDasharray={
                      link.machine ? "6 4" : link.keyless ? "5 4" : undefined
                    }
                    markerEnd={`url(#${
                      on
                        ? "lin-tip-on"
                        : link.machine
                          ? "lin-tip-machine"
                          : link.keyless
                            ? "lin-tip-warn"
                            : "lin-tip"
                    })`}
                  />
                </g>
              );
            })}

            {ghost && (
              <path
                d={ghost}
                fill="none"
                stroke="var(--om-primary)"
                strokeWidth={1.8}
                strokeDasharray="4 4"
              />
            )}
          </svg>

          {shownNodes.map((node) => (
            <CanvasTableNode
              key={node.table}
              node={node}
              columns={columnsByTable.get(node.table) ?? []}
              columnState={columnStateOf(node.table)}
              dropTarget={hover?.table === node.table}
              dropColumn={hover?.table === node.table ? hover.col : null}
              isolated={isolated(node.table)}
              picked={focus ? focus.seeds.has(node.table) : picked.has(node.table)}
              related={Boolean(
                focus && !focus.seeds.has(node.table) && focus.neighbours.has(node.table),
              )}
              dim={Boolean(focus && !focus.neighbours.has(node.table))}
              frozen={frozen}
              onStartDrag={startNodeDrag}
              onToggleCollapse={toggleCollapse}
              onStartLink={startLink}
              onRemoveNode={removeNode}
              onRetryColumns={onNeedColumns}
            />
          ))}
        </div>

        {shownNodes.length === 0 && (
          <div className="lin-canvas-empty">
            <p>画布是空的</p>
            <span>
              从左边的表清单点 ＋ 把表放上来，再点「智能补录」让机器先连一遍，你只管删错的。
            </span>
          </div>
        )}

        <div className="lin-canvas-hint">
          点表头选表（Shift 多选）→ 智能补录先把线连上 · 拖字段圆点 → 另一张表的字段＝
          手工连一条 · 拖表头移动 · 双击表头折叠 · 拖空白平移 · Delete 删除选中的边
        </div>

        {selectedEdge && (
          <div
            className="lin-inspector"
            /* 面板画在画布内部（它要跟着画布定位），指针事件会一路冒到画布的
               ``startPan``，那一下会清掉选中、把面板整个卸掉——于是面板里的按钮和
               下拉一个都按不动，「边无法编辑」就是这么来的。

               **必须在这里拦，不能在 startPan 里按 DOM 祖先判断**：antd 的下拉是
               portal 到 body 的，DOM 上根本不在面板里；但 React 合成事件走的是
               **组件树**，照样冒到画布。在这里 stopPropagation 才两种都挡得住。 */
            onPointerDown={(event) => event.stopPropagation()}
          >
            <div className="lin-inspector-head">
              <span>这条血缘</span>
              <button
                type="button"
                className="lin-tnode-x"
                aria-label="收起"
                onClick={() => setSelected(null)}
              >
                <CloseOutlined />
              </button>
            </div>

            <div className="lin-inspector-flow">
              <LineageTableName className="lin-node" name={selectedEdge.from} />
              <span className="lin-inspector-arrow">上游 → 下游</span>
              <LineageTableName className="lin-node lin-node--target" name={selectedEdge.to} />
            </div>

            {/* 面板的主角是**哪两个字段在关联**：表名在上面写过一次了，这里就只剩两列，
                而且两列都能直接改。字段挑错是最常见的情况（尤其机器预连的），
                以前只能删掉整对键重新拖一次。 */}
            <div className="lin-inspector-label">
              关联的字段
              {selectedEdge.keys.length === 0 && (
                <Tag color="warning" variant="filled">
                  缺键 · 上报后喂不了关系推断
                </Tag>
              )}
            </div>

            <ul className="lin-inspector-keys">
              {selectedEdge.keys.map((key) => (
                <li key={key.id}>
                  <ColumnPicker
                    value={key.src}
                    columns={columnsByTable.get(selectedEdge.from) ?? []}
                    disabled={frozen}
                    onChange={(next) => setKeyColumn(selectedEdge.id, key.id, "src", next)}
                  />
                  <span className="lin-inspector-eq">=</span>
                  <ColumnPicker
                    value={key.dst}
                    columns={columnsByTable.get(selectedEdge.to) ?? []}
                    disabled={frozen}
                    onChange={(next) => setKeyColumn(selectedEdge.id, key.id, "dst", next)}
                  />
                  {!frozen && (
                    <button
                      type="button"
                      className="lin-tnode-x"
                      aria-label="删除这对关联键"
                      onClick={() => removeKey(selectedEdge.id, key.id)}
                    >
                      <CloseOutlined />
                    </button>
                  )}
                </li>
              ))}
            </ul>

            {!frozen && (
              <Button
                size="small"
                type="dashed"
                icon={<PlusOutlined />}
                block
                onClick={() => addKey(selectedEdge)}
              >
                再加一对字段
              </Button>
            )}

            <div className="lin-inspector-acts">
              <Tooltip title={`改成 ${selectedEdge.to} → ${selectedEdge.from}`}>
                <Button
                  size="small"
                  icon={<SwapOutlined />}
                  disabled={frozen}
                  onClick={() => reverseEdge(selectedEdge.id)}
                >
                  反向
                </Button>
              </Tooltip>
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={frozen}
                onClick={() => removeEdge(selectedEdge.id)}
              >
                删除这条线
              </Button>
            </div>

            {/* 机器凭什么这么连：压成一行。判据是**改完之后才想核对**的东西，
                不该占掉面板顶上一整块——那正好挤掉了人真正要看的两列字段。
                完整判据在悬停里。 */}
            {selectedEdge.machine && (
              <Tooltip
                placement="left"
                title={
                  <div className="lin-why-pop">
                    <div>
                      值域 <code>{selectedEdge.machine.valueShape}</code> ·{" "}
                      {CARDINALITY_LABEL[selectedEdge.machine.cardinality] ??
                        selectedEdge.machine.cardinality}
                      {selectedEdge.machine.source === "confirmed_family" &&
                        " · 此前已确认的键族"}
                    </div>
                    {selectedEdge.machine.extraKeys?.length ? (
                      <div>
                        另有「{selectedEdge.machine.extraKeys.join("」「")}」也连着这两张表
                      </div>
                    ) : null}
                    {selectedEdge.machine.reason && <div>{selectedEdge.machine.reason}</div>}
                  </div>
                }
              >
                <div className="lin-inspector-why">
                  <i className="lin-machine-dot" />
                  机器预连
                  {selectedEdge.machine.keyName && <b>· {selectedEdge.machine.keyName}</b>}
                  <span className="lin-inspector-conf">
                    {Math.round(selectedEdge.machine.confidence * 100)}%
                  </span>
                </div>
              </Tooltip>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
