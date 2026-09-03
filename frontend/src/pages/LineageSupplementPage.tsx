import {
  ArrowRightOutlined,
  LeftOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Button, Input, Popconfirm, Segmented, Tag, Tooltip, message } from "antd";
import { useMemo, useState } from "react";
import { LineageCanvas } from "../components/lineage/LineageCanvas";
import type { CanvasEdge, CanvasNode } from "../components/lineage/LineageCanvas";
import { PackageRail } from "../components/lineage/PackageRail";
import { ScanReport } from "../components/lineage/ScanReport";
import { DOMAIN_FACTS, groupsOf, PACKAGES, TABLES } from "../components/lineage/prototypeData";
import type { SqlPackage } from "../components/lineage/prototypeData";
import { PageContainer } from "../components/PageContainer";

/**
 * 血缘补录工作台（原型）。
 *
 * 页面只讲一个数：**这个域里有多少张表是孤岛**（上下游皆空 → 本体生成时被判孤岛、
 * 降级 data_table、所在业务环节断裂）。表为什么没血缘（外购、新导入、临时落地）
 * 是来源场景，不进界面——补录的动作只有两种：
 *
 * - **扫代码包**：丢一个没有格式约定的 SQL 包进来，递归扫 .sql，自动提血缘；
 *   包会留在历史里，什么时候投的、上报没上报都查得到。
 * - **画布补录**：把已知的表摆上画布，像连 ER 图一样手工连。
 *
 * 两条路径在「所有包都没覆盖的孤岛表」处交接：这些表一键送进画布。
 *
 * 版式是**工作台**：顶栏一条事实带 + 可收起的左栏 + 吃满剩余高度的工作面 +
 * 常驻写入条。页面自己不滚动，滚动只发生在工作面内部。
 *
 * ⚠ 纯前端原型：数据来自 `components/lineage/prototypeData`，
 * 「上报到 DataHub」不会真的调 `add_lineage_edge`。
 */

type Mode = "scan" | "canvas";
type RailFilter = "all" | "isolated";

const CANVAS_COL_W = 320;
const CANVAS_ROW_H = 250;

/** 本地时间戳：用 toISOString 会串成 UTC，新投的包看起来比旧包还早。 */
function localStamp() {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

export function LineageSupplementPage() {
  const [mode, setMode] = useState<Mode>("scan");
  const [railOpen, setRailOpen] = useState(true);
  const [railFilter, setRailFilter] = useState<RailFilter>("isolated");
  const [keyword, setKeyword] = useState("");

  const [packages, setPackages] = useState<SqlPackage[]>(PACKAGES);
  const [pkgId, setPkgId] = useState<string | null>(PACKAGES[0].id);
  const [uploading, setUploading] = useState(false);
  const [scanningId, setScanningId] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(PACKAGES.map((p) => [p.id, p.targets])),
  );

  const [nodes, setNodes] = useState<CanvasNode[]>([
    { table: "tabSales Order", x: 16, y: 16 },
    { table: "tabCustomer", x: 16, y: 236 },
    { table: "imp_channel_order_2026", x: 340, y: 110 },
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

  /** 本次会话里上报过的：历史包的 applied 已经算进 DOMAIN_FACTS，不重复计。 */
  const [appliedScan, setAppliedScan] = useState<
    Record<string, { edges: number; resolved: number }>
  >({});
  const [appliedCanvas, setAppliedCanvas] = useState<{ edges: number; resolved: number } | null>(
    null,
  );

  const isolatedSet = useMemo(
    () => new Set(TABLES.filter((t) => t.isolated).map((t) => t.name)),
    [],
  );
  const isIsolated = (table: string) => isolatedSet.has(table);

  const pkg = packages.find((p) => p.id === pkgId) ?? null;
  const selected = pkg ? (selection[pkg.id] ?? pkg.targets) : [];
  const pkgFrozen = Boolean(pkg && (pkg.applied || appliedScan[pkg.id]));

  /* ---------- 本次待写入 ---------- */

  const scanPending = useMemo(() => {
    if (!pkg || uploading || pkgFrozen) return { edges: 0, blocked: 0, skipped: 0, resolved: 0 };
    const groups = groupsOf(pkg).filter((g) => selected.includes(g.target));
    const all = groups.flatMap((g) => g.edges);
    return {
      edges: all.filter((e) => e.state === "ok").length,
      blocked: all.filter((e) => e.state === "blocked").length,
      skipped: all.filter((e) => e.state === "skipped").length,
      resolved: groups.filter((g) => g.isolated).length,
    };
  }, [pkg, uploading, pkgFrozen, selected]);

  const canvasPending = useMemo(() => {
    if (appliedCanvas) return { edges: 0, blocked: 0, skipped: 0, resolved: 0 };
    const writable = edges.filter((e) => e.keys.length > 0);
    const resolved = new Set(writable.map((e) => e.to).filter((t) => isolatedSet.has(t)));
    return {
      edges: writable.length,
      blocked: edges.length - writable.length,
      skipped: 0,
      resolved: resolved.size,
    };
  }, [edges, isolatedSet, appliedCanvas]);

  const pending = mode === "scan" ? scanPending : canvasPending;
  const frozen = mode === "scan" ? pkgFrozen : Boolean(appliedCanvas);

  const sessionApplied = useMemo(() => {
    const scan = Object.values(appliedScan);
    return {
      edges: scan.reduce((sum, a) => sum + a.edges, 0) + (appliedCanvas ? appliedCanvas.edges : 0),
      resolved:
        scan.reduce((sum, a) => sum + a.resolved, 0) + (appliedCanvas ? appliedCanvas.resolved : 0),
    };
  }, [appliedScan, appliedCanvas]);

  const isolatedNow = DOMAIN_FACTS.isolated - sessionApplied.resolved;
  const isolatedNext = isolatedNow - pending.resolved;
  const coveragePct =
    ((DOMAIN_FACTS.withLineage + sessionApplied.edges) / DOMAIN_FACTS.total) * 100;

  /** 写入条上的数字：上报过就显示实际写入量，否则是本次将写入量。 */
  const writtenCount = frozen
    ? mode === "scan"
      ? pkg
        ? (appliedScan[pkg.id]?.edges ?? pkg.applied?.edges ?? 0)
        : 0
      : (appliedCanvas?.edges ?? 0)
    : pending.edges;

  const appliedNote = pkg
    ? appliedScan[pkg.id]
      ? `本次已上报 ${appliedScan[pkg.id].edges} 条`
      : pkg.applied
        ? `${pkg.applied.at} 已上报 ${pkg.applied.edges} 条`
        : undefined
    : undefined;

  /* ---------- 动作 ---------- */

  const startUpload = () => {
    setMode("scan");
    setUploading(true);
  };

  /** 原型：不真读文件，扫的是内置示例包——但会**新建一条历史记录**，重投看得见。 */
  const runScan = () => {
    const source = PACKAGES[0];
    const id = `pkg-${Date.now()}`;
    setScanningId(id);
    window.setTimeout(() => {
      const fresh: SqlPackage = {
        ...source,
        id,
        uploadedAt: localStamp(),
        applied: undefined,
      };
      setPackages((prev) => [fresh, ...prev]);
      setSelection((prev) => ({ ...prev, [id]: fresh.targets }));
      setScanningId(null);
      setUploading(false);
      setPkgId(id);
      message.success(`扫描完成：${fresh.targets.length} 个落点有血缘可补`);
    }, 900);
  };

  const rescan = (id: string) => {
    setScanningId(id);
    window.setTimeout(() => {
      setScanningId(null);
      setPkgId(id);
      setUploading(false);
      message.success("重新扫描完成，结果无变化");
    }, 900);
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
    if (mode === "scan" && pkg) {
      setAppliedScan((prev) => ({
        ...prev,
        [pkg.id]: { edges: pending.edges, resolved: pending.resolved },
      }));
    } else {
      setAppliedCanvas({ edges: pending.edges, resolved: pending.resolved });
    }
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
    <PageContainer full>
      <div className="lin-workbench">
        {/* ── 顶栏：标题 + 事实带 + 模式切换，压成一条 ── */}
        <header className="lin-topbar">
          <span className="lin-topbar-title">
            <NodeIndexOutlined />
            血缘补录
            <Tag color="processing" variant="filled">
              原型
            </Tag>
          </span>

          <div className="lin-stats">
            <div className="lin-stat lin-stat--iso">
              <span className="lin-stat-label">孤岛表</span>
              <span className="lin-stat-value">
                <b>{isolatedNow}</b>
                {isolatedNext !== isolatedNow && (
                  <>
                    <ArrowRightOutlined className="lin-stat-arrow" />
                    <b className="lin-stat-next">{isolatedNext}</b>
                    <em>预计</em>
                  </>
                )}
              </span>
            </div>

            <div className="lin-stat">
              <span className="lin-stat-label">血缘覆盖率</span>
              <span className="lin-stat-value">
                <b>{coveragePct.toFixed(1)}%</b>
              </span>
              <div className="lin-meter">
                <i
                  className="lin-meter-have"
                  style={{ width: `${(DOMAIN_FACTS.withLineage / DOMAIN_FACTS.total) * 100}%` }}
                />
                <i
                  className="lin-meter-gain"
                  style={{ width: `${(sessionApplied.edges / DOMAIN_FACTS.total) * 100}%` }}
                />
              </div>
            </div>

            <div className="lin-stat">
              <span className="lin-stat-label">本轮已上报</span>
              <span className="lin-stat-value">
                <b>{sessionApplied.edges}</b>
                <em>条边</em>
              </span>
            </div>

            <div className="lin-stat lin-stat--src">
              <span className="lin-stat-label">目标域</span>
              <span className="lin-stat-src">
                {DOMAIN_FACTS.domain} · {DOMAIN_FACTS.platform} · {DOMAIN_FACTS.database} ·{" "}
                {DOMAIN_FACTS.fabric}
              </span>
            </div>
          </div>

          <Segmented
            value={mode}
            onChange={(value) => setMode(value as Mode)}
            options={[
              { label: "代码包扫描", value: "scan" },
              { label: "画布补录", value: "canvas" },
            ]}
          />
        </header>

        {/* ── 主体：左栏（可收起）+ 工作面 ── */}
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
                  appliedInSession={appliedScan}
                  scanningId={scanningId}
                  onSelect={(id) => {
                    setPkgId(id);
                    setUploading(false);
                  }}
                  onUpload={startUpload}
                  onRescan={rescan}
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
                    {railRows.length === 0 && (
                      <li className="lin-muted lin-rail-empty">没有匹配的表</li>
                    )}
                  </ul>
                </>
              ))}
          </aside>

          <main className={`lin-main lin-main--${mode}`}>
            {mode === "scan" ? (
              <ScanReport
                pkg={pkg}
                uploading={uploading}
                scanning={scanningId !== null}
                onScan={runScan}
                selected={selected}
                onSelectedChange={(keys) =>
                  pkg && setSelection((prev) => ({ ...prev, [pkg.id]: keys }))
                }
                frozen={pkgFrozen}
                appliedNote={appliedNote}
                onSendToCanvas={addToCanvas}
              />
            ) : (
              <LineageCanvas
                nodes={nodes}
                edges={edges}
                setNodes={setNodes}
                setEdges={setEdges}
                isolated={isIsolated}
                frozen={Boolean(appliedCanvas)}
              />
            )}
          </main>
        </div>

        {/* ── 写入条：preview / apply 分离，常驻不滚动 ── */}
        <footer className="lin-footer">
          <div className="lin-footer-counts">
            <span>将写入 DataHub</span>
            <b>{writtenCount}</b>
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

          <div className="lin-footer-acts">
            {frozen ? (
              <>
                <span className="lin-done">
                  {mode === "scan" ? appliedNote : "画布已上报"} · 失败 0 ·
                  幂等，重复上报不会重复建边
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
        </footer>
      </div>
    </PageContainer>
  );
}
