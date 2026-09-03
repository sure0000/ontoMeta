import { ArrowRightOutlined, NodeIndexOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Input, Popconfirm, Segmented, Tabs, Tag, Tooltip, message } from "antd";
import { useMemo, useState } from "react";
import { LineageCanvas } from "../components/lineage/LineageCanvas";
import type { CanvasEdge, CanvasNode } from "../components/lineage/LineageCanvas";
import { ScanReport } from "../components/lineage/ScanReport";
import { DOMAIN_FACTS, SCAN_GROUPS, TABLES } from "../components/lineage/prototypeData";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";

/**
 * 血缘补录（原型）。
 *
 * 页面只讲一个数：**这个域里有多少张表是孤岛**（上下游皆空 → 本体生成时被判孤岛、
 * 降级 data_table、所在业务环节断裂）。表为什么没血缘（外购、新导入、临时落地）
 * 是来源场景，不进界面——补录的动作只有两种：
 *
 * - **扫代码包**：丢一个没有格式约定的 SQL 包进来，递归扫 .sql，自动提血缘；
 * - **画布补录**：把已知的表摆上画布，像连 ER 图一样手工连。
 *
 * 两条路径在「代码包没覆盖的孤岛表」处交接：扫完仍是孤岛的，一键送进画布。
 *
 * ⚠ 纯前端原型：数据来自 `components/lineage/prototypeData`，
 * 「上报到 DataHub」不会真的调 `add_lineage_edge`。
 */

type Mode = "scan" | "canvas";
type RailFilter = "all" | "isolated";

const CANVAS_COL_W = 308;
const CANVAS_ROW_H = 240;

export function LineageSupplementPage() {
  const [mode, setMode] = useState<Mode>("scan");
  const [railFilter, setRailFilter] = useState<RailFilter>("isolated");
  const [keyword, setKeyword] = useState("");

  const [scanning, setScanning] = useState(false);
  const [scanned, setScanned] = useState(false);
  const [selectedGroups, setSelectedGroups] = useState<string[]>(SCAN_GROUPS.map((g) => g.target));

  const [nodes, setNodes] = useState<CanvasNode[]>([
    { table: "tabSales Order", x: 16, y: 16 },
    { table: "tabCustomer", x: 16, y: 232 },
    { table: "imp_channel_order_2026", x: 324, y: 110 },
  ]);
  const [edges, setEdges] = useState<CanvasEdge[]>([
    {
      id: "tabSales Order->imp_channel_order_2026",
      from: "tabSales Order",
      to: "imp_channel_order_2026",
      keys: [
        { id: "tabSales Order.name->imp_channel_order_2026.so_no", src: "name", dst: "so_no" },
      ],
    },
  ]);

  /** 已上报：两条路径分别记，孤岛数与覆盖率从这里累加。 */
  const [applied, setApplied] = useState<Record<Mode, { edges: number; resolved: number }>>({
    scan: { edges: 0, resolved: 0 },
    canvas: { edges: 0, resolved: 0 },
  });

  const isolatedSet = useMemo(
    () => new Set(TABLES.filter((t) => t.isolated).map((t) => t.name)),
    [],
  );
  const isIsolated = (table: string) => isolatedSet.has(table);

  /** 代码包覆盖到的表——左栏据此标「代码包已覆盖」。 */
  const coveredByScan = useMemo(() => {
    const set = new Set<string>();
    SCAN_GROUPS.forEach((group) => {
      set.add(group.target);
      group.edges.forEach((edge) => set.add(edge.src));
    });
    return set;
  }, []);

  /* ---------- 本次待写入 ---------- */

  const scanPending = useMemo(() => {
    // 没扫之前没有任何待写入的边：预勾选的分组不能提前把写入条和孤岛预估点亮。
    if (!scanned) return { edges: 0, blocked: 0, skipped: 0, resolved: 0 };
    const groups = SCAN_GROUPS.filter((g) => selectedGroups.includes(g.target));
    const all = groups.flatMap((g) => g.edges);
    return {
      edges: all.filter((e) => e.state === "ok").length,
      blocked: all.filter((e) => e.state === "blocked").length,
      skipped: all.filter((e) => e.state === "skipped").length,
      resolved: groups.filter((g) => g.isolated).length,
    };
  }, [selectedGroups, scanned]);

  const canvasPending = useMemo(() => {
    const writable = edges.filter((e) => e.keys.length > 0);
    const resolved = new Set(writable.map((e) => e.to).filter((t) => isolatedSet.has(t)));
    return {
      edges: writable.length,
      blocked: edges.length - writable.length,
      skipped: 0,
      resolved: resolved.size,
    };
  }, [edges, isolatedSet]);

  const frozen = applied[mode].edges > 0;
  const pending = mode === "scan" ? scanPending : canvasPending;
  const appliedEdges = applied.scan.edges + applied.canvas.edges;
  const appliedResolved = applied.scan.resolved + applied.canvas.resolved;

  const isolatedNow = DOMAIN_FACTS.isolated - appliedResolved;
  const isolatedNext = isolatedNow - (frozen ? 0 : pending.resolved);
  const coveragePct = ((DOMAIN_FACTS.withLineage + appliedEdges) / DOMAIN_FACTS.total) * 100;

  /* ---------- 动作 ---------- */

  const runScan = () => {
    setScanning(true);
    window.setTimeout(() => {
      setScanning(false);
      setScanned(true);
      message.success(`扫描完成：${SCAN_GROUPS.length} 个落点有血缘可补`);
    }, 900);
  };

  const resetScan = () => {
    setScanned(false);
    setSelectedGroups(SCAN_GROUPS.map((g) => g.target));
  };

  const addToCanvas = (table: string) => {
    setMode("canvas");
    setNodes((prev) => {
      if (prev.some((n) => n.table === table)) return prev;
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
  };

  const apply = () => {
    setApplied((prev) => ({
      ...prev,
      [mode]: { edges: pending.edges, resolved: pending.resolved },
    }));
    message.success(`已上报 ${pending.edges} 条血缘边 · ${pending.resolved} 张表脱离孤岛 · 失败 0`);
  };

  /* ---------- 左栏 ---------- */

  const railRows = TABLES.filter(
    (t) =>
      (railFilter === "all" || t.isolated) &&
      t.name.toLowerCase().includes(keyword.trim().toLowerCase()),
  );

  const onCanvas = new Set(nodes.map((n) => n.table));

  return (
    <PageContainer>
      <PageHeader
        icon={<NodeIndexOutlined />}
        title="血缘补录"
        description="推不出血缘的表会被判成孤岛：降级 data_table、所在业务环节断裂、关系推断跟着丢。这一页把血缘补回 DataHub。"
        extra={
          <Tag color="processing" variant="filled">
            原型 · 示例数据
          </Tag>
        }
        meta={
          <div className="lin-facts">
            <div className="lin-fact lin-fact--key">
              <span className="lin-fact-label">孤岛表</span>
              <span className="lin-fact-value">
                <b>{isolatedNow}</b>
                {isolatedNext !== isolatedNow && (
                  <>
                    <ArrowRightOutlined className="lin-fact-arrow" />
                    <b className="lin-fact-next">{isolatedNext}</b>
                    <em>预计</em>
                  </>
                )}
              </span>
              <span className="lin-fact-foot">
                上下游皆空 · 占域内 {((isolatedNow / DOMAIN_FACTS.total) * 100).toFixed(1)}%
              </span>
            </div>

            <div className="lin-fact">
              <span className="lin-fact-label">域内表</span>
              <span className="lin-fact-value">
                <b>{DOMAIN_FACTS.total}</b>
              </span>
              <span className="lin-fact-foot">
                {DOMAIN_FACTS.domain} · {DOMAIN_FACTS.platform} · {DOMAIN_FACTS.database}
              </span>
            </div>

            <div className="lin-fact">
              <span className="lin-fact-label">血缘覆盖率</span>
              <span className="lin-fact-value">
                <b>{coveragePct.toFixed(1)}%</b>
              </span>
              <div className="lin-meter">
                <i
                  className="lin-meter-have"
                  style={{ width: `${(DOMAIN_FACTS.withLineage / DOMAIN_FACTS.total) * 100}%` }}
                />
                <i
                  className="lin-meter-gain"
                  style={{ width: `${(appliedEdges / DOMAIN_FACTS.total) * 100}%` }}
                />
              </div>
            </div>

            <div className="lin-fact">
              <span className="lin-fact-label">本轮已上报</span>
              <span className="lin-fact-value">
                <b>{appliedEdges}</b>
                <em>条边</em>
              </span>
              <span className="lin-fact-foot">
                GraphQL updateLineage · 幂等，重复上报不重复建边
              </span>
            </div>
          </div>
        }
      />

      <div className="lin-panes">
        {/* ── 左栏：表清单。扫描模式当对照，画布模式当取表口 ── */}
        <aside className="section-card lin-rail">
          <div className="section-card-head">
            <span className="section-card-head-title">表清单</span>
            <span className="section-card-count">{railRows.length}</span>
          </div>
          <div className="lin-rail-controls">
            <Segmented
              block
              size="small"
              value={railFilter}
              onChange={(value) => setRailFilter(value as RailFilter)}
              options={[
                { label: `仅孤岛 ${DOMAIN_FACTS.isolated}`, value: "isolated" },
                { label: `全部 ${DOMAIN_FACTS.total}`, value: "all" },
              ]}
            />
            <Input.Search
              size="small"
              allowClear
              placeholder="搜表名"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
          </div>

          <ul className="lin-rail-list">
            {railRows.map((row) => (
              <li key={row.name} className="lin-rail-row">
                <span className="lin-rail-main">
                  <span className="lin-rail-name">
                    {row.isolated && <i className="lin-iso-dot" title="孤岛表" />}
                    {row.name}
                  </span>
                  <span className="lin-rail-meta">
                    {row.isolated ? (
                      <>上下游 0 / 0</>
                    ) : (
                      <>
                        上游 {row.upstream} · 下游 {row.downstream}
                      </>
                    )}
                    {scanned && coveredByScan.has(row.name) && (
                      <Tag color="success" variant="filled">
                        代码包已覆盖
                      </Tag>
                    )}
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
            {railRows.length === 0 && <li className="lin-muted lin-rail-empty">没有匹配的表</li>}
          </ul>
        </aside>

        {/* ── 右栏：两种操作 ── */}
        <div className="section-card lin-work">
          <Tabs
            activeKey={mode}
            onChange={(key) => setMode(key as Mode)}
            className="lin-tabs"
            items={[
              {
                key: "scan",
                label: "扫描 SQL 代码包",
                children: (
                  <div className="lin-pane-body">
                    <ScanReport
                      scanned={scanned}
                      scanning={scanning}
                      onScan={runScan}
                      onReset={resetScan}
                      selected={selectedGroups}
                      onSelectedChange={setSelectedGroups}
                      frozen={applied.scan.edges > 0}
                      onSendToCanvas={addToCanvas}
                    />
                  </div>
                ),
              },
              {
                key: "canvas",
                label: "画布补录",
                children: (
                  <LineageCanvas
                    nodes={nodes}
                    edges={edges}
                    setNodes={setNodes}
                    setEdges={setEdges}
                    isolated={isIsolated}
                    frozen={applied.canvas.edges > 0}
                  />
                ),
              },
            ]}
          />
        </div>
      </div>

      {/* ── 写入条：preview / apply 分离 ── */}
      <div className="lin-writebar">
        <div className="lin-writebar-counts">
          <span>将写入 DataHub</span>
          <b>{frozen ? applied[mode].edges : pending.edges}</b>
          <span>条边</span>
          <Tag color={mode === "canvas" ? "blue" : "default"} variant="filled">
            {mode === "canvas" ? "画布手工连" : "代码包扫描"}
          </Tag>
          {pending.resolved > 0 && (
            <Tag color="success" variant="filled">
              {pending.resolved} 张表脱离孤岛
            </Tag>
          )}
          {pending.blocked > 0 && (
            <Tag color="warning" variant="filled">
              {mode === "canvas" ? `待补关联键 ${pending.blocked}` : `待映射 ${pending.blocked}`}
            </Tag>
          )}
          {pending.skipped > 0 && <Tag variant="filled">跳过 {pending.skipped}</Tag>}
        </div>

        <div className="lin-writebar-acts">
          {frozen ? (
            <>
              <span className="lin-done">
                已上报 {applied[mode].edges} 条 · 失败 0 · 幂等，重复上报不会重复建边
              </span>
              <Button size="small">去 DataHub 查看血缘图</Button>
            </>
          ) : (
            <Popconfirm
              placement="topRight"
              title={`确认向 DataHub 写入 ${pending.edges} 条血缘边？`}
              description={
                pending.resolved > 0
                  ? `写入后 ${pending.resolved} 张表将脱离孤岛，需重跑本体起草才生效。`
                  : "写入后可在 DataHub 血缘图中查看；重复上报幂等。"
              }
              okText="确认上报"
              cancelText="再看看"
              onConfirm={apply}
              disabled={pending.edges === 0}
            >
              <Button type="primary" disabled={pending.edges === 0}>
                上报到 DataHub
              </Button>
            </Popconfirm>
          )}
        </div>
      </div>
    </PageContainer>
  );
}
