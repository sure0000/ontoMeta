import {
  AimOutlined,
  ApartmentOutlined,
  CloseOutlined,
  DeleteOutlined,
  MinusOutlined,
  PlusOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import { Button, Tag, Tooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, PointerEvent as ReactPointerEvent, SetStateAction } from "react";
import type { LineageColumn } from "../../types";
import { assignLayers, curve } from "./graphLayout";
import { LineageJoinKey, LineageTableName } from "./LineageTableName";

/**
 * 路径 B：把表摆到画布上，像连 ER 图一样连血缘。
 *
 * 这是个重操作，三条设计取舍：
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

export interface CanvasEdge {
  id: string;
  from: string;
  to: string;
  keys: CanvasKey[];
}

const NODE_W = 228;
const HEADER_H = 32;
const ROW_H = 22;
const BODY_PAD = 5;
const GRID = 8;
const MIN_K = 0.5;
const MAX_K = 1.4;

type Drag =
  | { kind: "node"; table: string; offX: number; offY: number }
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

interface CanvasTableNodeProps {
  node: CanvasNode;
  columns: LineageColumn[];
  dropTarget: boolean;
  dropColumn: string | null;
  isolated: boolean;
  frozen: boolean;
  onStartDrag: (event: ReactPointerEvent, node: CanvasNode) => void;
  onToggleCollapse: (table: string) => void;
  onStartLink: (event: ReactPointerEvent, table: string, col: string | null) => void;
  onRemoveNode: (table: string) => void;
}

const CanvasTableNode = memo(function CanvasTableNode({
  node,
  columns,
  dropTarget,
  dropColumn,
  isolated,
  frozen,
  onStartDrag,
  onToggleCollapse,
  onStartLink,
  onRemoveNode,
}: CanvasTableNodeProps) {
  return (
    <div
      className={`lin-tnode${dropTarget ? " lin-tnode--drop" : ""}`}
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
        <span className="lin-tnode-count">{columns.length}</span>
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
  frozen: boolean;
}

export function LineageCanvas({
  nodes,
  edges,
  setNodes,
  setEdges,
  isolated,
  columnsOf,
  frozen,
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
  const pendingNodePosition = useRef<PendingNodePosition | null>(null);
  const nodePositionFrame = useRef<number | null>(null);

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
      setDrag({ kind: "node", table: node.table, offX: p.x - node.x, offY: p.y - node.y });
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
    if (drag?.kind === "node") flushNodePosition();
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
      return prev.map((e) => (e === existing ? { ...e, keys: [...e.keys, key] } : e));
    });
  };

  /* ---------- 编辑 ---------- */

  const removeNode = useCallback(
    (table: string) => {
      setNodes((prev) => prev.filter((n) => n.table !== table));
      setEdges((prev) => prev.filter((e) => e.from !== table && e.to !== table));
    },
    [setEdges, setNodes],
  );

  const toggleCollapse = useCallback(
    (table: string) =>
      setNodes((prev) =>
        prev.map((n) => (n.table === table ? { ...n, collapsed: !n.collapsed } : n)),
      ),
    [setNodes],
  );

  const reverseEdge = (id: string) =>
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
            }
          : e,
      ),
    );

  const removeEdge = (id: string) => {
    setEdges((prev) => prev.filter((e) => e.id !== id));
    setSelected(null);
  };

  const removeKey = (edgeId: string, keyId: string) =>
    setEdges((prev) =>
      prev.map((e) => (e.id === edgeId ? { ...e, keys: e.keys.filter((k) => k.id !== keyId) } : e)),
    );

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

  const autoLayout = () => {
    const layer = assignLayers(
      nodes.map((n) => n.table),
      edges,
    );
    const byLayer = new Map<number, CanvasNode[]>();
    nodes.forEach((n) => {
      const l = layer.get(n.table) ?? 0;
      byLayer.set(l, [...(byLayer.get(l) ?? []), n]);
    });
    const placed: CanvasNode[] = [];
    [...byLayer.keys()]
      .sort((a, b) => a - b)
      .forEach((l, columnIndex) => {
        let y = 16;
        (byLayer.get(l) ?? []).forEach((n) => {
          placed.push({ ...n, x: 16 + columnIndex * (NODE_W + 96), y });
          y += nodeHeight(n) + 24;
        });
      });
    setNodes(placed);
    setView({ x: 24, y: 16, k: 1 });
  };

  const zoom = (delta: number) =>
    setView((v) => ({
      ...v,
      k: Math.min(MAX_K, Math.max(MIN_K, Number((v.k + delta).toFixed(2)))),
    }));

  const fit = () => {
    if (nodes.length === 0 || !ref.current) return;
    const maxX = Math.max(...nodes.map((n) => n.x + NODE_W));
    const maxY = Math.max(...nodes.map((n) => n.y + nodeHeight(n)));
    const minX = Math.min(...nodes.map((n) => n.x));
    const minY = Math.min(...nodes.map((n) => n.y));
    const box = ref.current.getBoundingClientRect();
    const k = Math.min(
      MAX_K,
      Math.max(
        MIN_K,
        Math.min((box.width - 48) / (maxX - minX), (box.height - 48) / (maxY - minY)),
      ),
    );
    setView({ k, x: 24 - minX * k, y: 24 - minY * k });
  };

  /* ---------- 渲染 ---------- */

  const links = edges.flatMap((edge) => {
    const a = nodeMap.get(edge.from);
    const b = nodeMap.get(edge.to);
    if (!a || !b) return [];
    const rows = edge.keys.length > 0 ? edge.keys : [null];
    return rows.map((key, index) => ({
      id: `${edge.id}#${index}`,
      edgeId: edge.id,
      keyless: key === null,
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

  return (
    <div className="lin-canvas-wrap">
      <div className="lin-canvas-toolbar">
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
            </defs>

            {links.map((link) => {
              const on = link.edgeId === selected;
              const stroke = on
                ? "var(--om-primary)"
                : link.keyless
                  ? "var(--om-warning)"
                  : "var(--om-machine)";
              return (
                <g key={link.id}>
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
                    strokeDasharray={link.keyless ? "5 4" : undefined}
                    markerEnd={`url(#${on ? "lin-tip-on" : link.keyless ? "lin-tip-warn" : "lin-tip"})`}
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

          {nodes.map((node) => (
            <CanvasTableNode
              key={node.table}
              node={node}
              columns={columnsByTable.get(node.table) ?? []}
              dropTarget={hover?.table === node.table}
              dropColumn={hover?.table === node.table ? hover.col : null}
              isolated={isolated(node.table)}
              frozen={frozen}
              onStartDrag={startNodeDrag}
              onToggleCollapse={toggleCollapse}
              onStartLink={startLink}
              onRemoveNode={removeNode}
            />
          ))}
        </div>

        {nodes.length === 0 && (
          <div className="lin-canvas-empty">
            <p>画布是空的</p>
            <span>从左边的表清单点 ＋ 把表放上来，再从字段拖到另一张表的字段上。</span>
          </div>
        )}

        <div className="lin-canvas-hint">
          拖字段圆点 → 另一张表的字段＝一条血缘 + 一对关联键 · 拖表头移动 · 双击表头折叠 ·
          拖空白平移 · Delete 删除选中的边
        </div>

        {selectedEdge && (
          <div className="lin-inspector">
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

            {/* 表名可以很长，按钮里塞不下方向说明——方向在上面那两行已经写清楚了 */}
            <Tooltip title={`改成 ${selectedEdge.to} → ${selectedEdge.from}`}>
              <Button
                size="small"
                icon={<SwapOutlined />}
                disabled={frozen}
                onClick={() => reverseEdge(selectedEdge.id)}
                block
              >
                反向
              </Button>
            </Tooltip>

            <div className="lin-inspector-label">
              关联键
              {selectedEdge.keys.length === 0 && (
                <Tag color="warning" variant="filled">
                  缺键 · 上报后喂不了关系推断
                </Tag>
              )}
            </div>
            <ul className="lin-inspector-keys">
              {selectedEdge.keys.map((key) => (
                <li key={key.id}>
                  <LineageJoinKey
                    value={`${selectedEdge.from}.${key.src} = ${selectedEdge.to}.${key.dst}`}
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
              {selectedEdge.keys.length === 0 && (
                <li className="lin-muted">在两张表之间再拖一次字段即可补上。</li>
              )}
            </ul>

            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={frozen}
              onClick={() => removeEdge(selectedEdge.id)}
              block
            >
              删除这条血缘
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
