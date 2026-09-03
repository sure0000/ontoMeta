import { AimOutlined, MinusOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Tooltip } from "antd";
import { useCallback, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { assignLayers, curve } from "./graphLayout";
import type { LineagePackageGroup } from "../../types";

/**
 * 代码包扫出来的血缘，画成图。
 *
 * 与画布补录同一套画法（左上游右下游、同样的贝塞尔、同样的分层），但**只读**：
 * SQL 里写的东西不该在图上改，要改去改 SQL。差别都在读图上：
 *
 * 1. **节点只显示表名**，不铺字段。一个包动辄二十几张表，把字段全铺开谁也看不清；
 *    关联键挪到「点中某张表时，只显示它那几条边的键」。
 * 2. **点表聚焦**：不相干的节点和线压暗，一张表的上下游一眼看完。二十几个节点的
 *    图，不给聚焦就只能靠眼睛描线。
 * 3. **跟着勾选走**：表格里取消勾选的落点在图上变淡——图和写入条说的是同一件事。
 */

const NODE_W = 196;
const NODE_H = 30;
const GAP_Y = 12;
const GAP_X = 108;
const PAD = 20;
const MIN_K = 0.4;
const MAX_K = 1.4;

interface Props {
  groups: LineagePackageGroup[];
  /** 表格里勾选的落点，未勾选的在图上压暗。 */
  selected: string[];
  /** 当前是孤岛的表名。图上给它们标红点。 */
  isolated: Set<string>;
}

interface PlacedNode {
  table: string;
  x: number;
  y: number;
  kind: "target" | "source";
  isolated: boolean;
  dimmed: boolean;
}

function short(table: string) {
  const name = table.includes(".") ? table.slice(table.indexOf(".") + 1) : table;
  return name.length > 24 ? `${name.slice(0, 23)}…` : name;
}

export function ScanGraph({ groups, selected, isolated }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [pan, setPan] = useState<{ sx: number; sy: number; ox: number; oy: number } | null>(null);
  const [focus, setFocus] = useState<string | null>(null);

  const model = useMemo(() => {
    const edges = groups.flatMap((group) =>
      group.edges.map((edge) => ({
        id: edge.id,
        from: edge.source_table,
        to: edge.target_table,
        key: edge.join_key ?? "",
        state: edge.state,
        dimmed: !selected.includes(group.target),
      })),
    );

    const targets = new Set(groups.map((g) => g.target));
    const names = Array.from(new Set(edges.flatMap((e) => [e.from, e.to])));
    const layer = assignLayers(names, edges);

    // 同层内的次序：落点按表格顺序，上游按「它第一个下游的位置」排，减少交叉
    const targetOrder = new Map(groups.map((g, i) => [g.target, i]));
    const columns = new Map<number, string[]>();
    names.forEach((name) => {
      const l = layer.get(name) ?? 0;
      columns.set(l, [...(columns.get(l) ?? []), name]);
    });

    const rank = (name: string) => {
      if (targetOrder.has(name)) return targetOrder.get(name) ?? 0;
      const firstTarget = edges.find((e) => e.from === name)?.to;
      return firstTarget ? (targetOrder.get(firstTarget) ?? 99) : 99;
    };

    const placed = new Map<string, PlacedNode>();
    const orderedLayers = [...columns.keys()].sort((a, b) => a - b);
    let height = 0;
    orderedLayers.forEach((l) => {
      const column = (columns.get(l) ?? []).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
      height = Math.max(height, column.length * NODE_H + (column.length - 1) * GAP_Y);
    });

    orderedLayers.forEach((l, columnIndex) => {
      const column = (columns.get(l) ?? []).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
      const columnHeight = column.length * NODE_H + (column.length - 1) * GAP_Y;
      const y0 = PAD + (height - columnHeight) / 2;
      column.forEach((name, i) => {
        placed.set(name, {
          table: name,
          x: PAD + columnIndex * (NODE_W + GAP_X),
          y: y0 + i * (NODE_H + GAP_Y),
          kind: targets.has(name) ? "target" : "source",
          isolated: isolated.has(name),
          dimmed: targets.has(name)
            ? !selected.includes(name)
            : !edges.some((e) => e.from === name && !e.dimmed),
        });
      });
    });

    const wires = edges.flatMap((edge) => {
      const a = placed.get(edge.from);
      const b = placed.get(edge.to);
      if (!a || !b) return [];
      const x1 = a.x + NODE_W;
      const y1 = a.y + NODE_H / 2;
      const x2 = b.x - 7;
      const y2 = b.y + NODE_H / 2;
      return [
        {
          ...edge,
          d: curve(x1, y1, x2, y2),
        },
      ];
    });

    return {
      nodes: [...placed.values()],
      wires,
      width: PAD * 2 + orderedLayers.length * NODE_W + (orderedLayers.length - 1) * GAP_X,
      height: height + PAD * 2,
    };
  }, [groups, selected, isolated]);

  /**
   * 聚焦某张表：不相干的压暗，它的上下游与关联键进右侧面板。
   *
   * 键不画在线上——五条边汇到同一张表时，五个标签叠在一起；缩放一小更没法读。
   * 面板与画布补录的检查器同一位置、同一读法。
   */
  const related = useMemo(() => {
    if (!focus) return null;
    const tables = new Set<string>([focus]);
    const wires = new Set<string>();
    const upstream: { table: string; key: string; state: string; reason?: string }[] = [];
    const downstream: { table: string; key: string; state: string; reason?: string }[] = [];
    model.wires.forEach((wire) => {
      if (wire.to === focus) {
        wires.add(wire.id);
        tables.add(wire.from);
        upstream.push({ table: wire.from, key: wire.key, state: wire.state });
      } else if (wire.from === focus) {
        wires.add(wire.id);
        tables.add(wire.to);
        downstream.push({ table: wire.to, key: wire.key, state: wire.state });
      }
    });
    return { tables, wires, upstream, downstream };
  }, [focus, model.wires]);

  const fit = useCallback(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const k = Math.min(
      MAX_K,
      Math.max(MIN_K, Math.min(box.width / model.width, box.height / model.height)),
    );
    setView({ k, x: (box.width - model.width * k) / 2, y: (box.height - model.height * k) / 2 });
  }, [model.width, model.height]);

  const startPan = (event: ReactPointerEvent) => {
    setFocus(null);
    setPan({ sx: event.clientX, sy: event.clientY, ox: view.x, oy: view.y });
    try {
      ref.current?.setPointerCapture(event.pointerId);
    } catch {
      /* 合成事件没有活动指针，忽略 */
    }
  };

  const onMove = (event: ReactPointerEvent) => {
    if (!pan) return;
    setView((v) => ({
      ...v,
      x: pan.ox + (event.clientX - pan.sx),
      y: pan.oy + (event.clientY - pan.sy),
    }));
  };

  const zoom = (delta: number) =>
    setView((v) => ({
      ...v,
      k: Math.min(MAX_K, Math.max(MIN_K, Number((v.k + delta).toFixed(2)))),
    }));

  return (
    <div className="lin-graph-wrap">
      <div className="lin-graph-legend">
        <span>
          <i className="lin-swatch lin-swatch--target" />
          落点
        </span>
        <span>
          <i className="lin-swatch lin-swatch--source" />
          上游表
        </span>
        <span>
          <i className="lin-iso-dot" />
          孤岛
        </span>
        <span>
          <i className="lin-swatch lin-swatch--blocked" />
          待映射
        </span>
        <span className="lin-graph-legend-hint">点表看它的上下游与关联键 · 拖空白平移</span>
      </div>

      <div className="lin-canvas-toolbar lin-graph-toolbar">
        <Tooltip title="缩放到全图">
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
        className="lin-graph-canvas"
        onPointerDown={startPan}
        onPointerMove={onMove}
        onPointerUp={() => setPan(null)}
        onPointerCancel={() => setPan(null)}
      >
        <div
          className="lin-world lin-world--flat"
          style={{
            width: model.width,
            height: model.height,
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
          }}
        >
          <svg className="lin-wires" width={model.width} height={model.height}>
            <defs>
              <marker
                id="scan-tip"
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
                id="scan-tip-warn"
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

            {model.wires.map((wire) => {
              const off = related ? !related.wires.has(wire.id) : false;
              const faded = wire.dimmed || wire.state === "skipped" || off;
              const warn = wire.state === "blocked";
              return (
                <g key={wire.id} className={faded ? "lin-wire--faded" : undefined}>
                  <path
                    d={wire.d}
                    fill="none"
                    stroke={warn ? "var(--om-warning)" : "var(--om-machine)"}
                    strokeWidth={related && !off ? 2 : 1.3}
                    strokeDasharray={wire.state === "ok" ? undefined : "5 4"}
                    markerEnd={`url(#${warn ? "scan-tip-warn" : "scan-tip"})`}
                  />
                </g>
              );
            })}
          </svg>

          {model.nodes.map((node) => {
            const off = related ? !related.tables.has(node.table) : false;
            return (
              <button
                type="button"
                key={node.table}
                className={`lin-gnode lin-gnode--${node.kind}${
                  node.dimmed || off ? " lin-gnode--faded" : ""
                }${focus === node.table ? " lin-gnode--focus" : ""}`}
                style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H }}
                title={node.table}
                onPointerDown={(event) => event.stopPropagation()}
                onClick={() => setFocus((prev) => (prev === node.table ? null : node.table))}
              >
                {node.isolated && <i className="lin-iso-dot" />}
                <span className="lin-gnode-name">{short(node.table)}</span>
              </button>
            );
          })}
        </div>

        {focus && related && (
          <aside className="lin-inspector lin-inspector--graph">
            <div className="lin-inspector-head">
              <span className="lin-focus-name" title={focus}>
                {isolated.has(focus) && <i className="lin-iso-dot" />}
                {short(focus)}
              </span>
              <button
                type="button"
                className="lin-tnode-x"
                aria-label="取消聚焦"
                onClick={() => setFocus(null)}
              >
                ×
              </button>
            </div>

            <div className="lin-inspector-label">上游 {related.upstream.length}</div>
            <ul className="lin-focus-list">
              {related.upstream.map((item) => (
                <li key={`u-${item.table}-${item.key}`}>
                  <span className="lin-node">{short(item.table)}</span>
                  {item.key ? (
                    <span className="lin-key">{item.key}</span>
                  ) : (
                    <span className="lin-muted">无 JOIN 条件 · 仅表级</span>
                  )}
                </li>
              ))}
              {related.upstream.length === 0 && <li className="lin-muted">没有上游</li>}
            </ul>

            <div className="lin-inspector-label">下游 {related.downstream.length}</div>
            <ul className="lin-focus-list">
              {related.downstream.map((item) => (
                <li key={`d-${item.table}-${item.key}`}>
                  <span className="lin-node lin-node--target">{short(item.table)}</span>
                  {item.key ? (
                    <span className="lin-key">{item.key}</span>
                  ) : (
                    <span className="lin-muted">无 JOIN 条件 · 仅表级</span>
                  )}
                </li>
              ))}
              {related.downstream.length === 0 && <li className="lin-muted">没有下游</li>}
            </ul>
          </aside>
        )}
      </div>
    </div>
  );
}
