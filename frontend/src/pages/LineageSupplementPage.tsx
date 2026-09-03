import {
  ArrowRightOutlined,
  LeftOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Alert, Button, Input, Popconfirm, Segmented, Select, Tag, Tooltip, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { LineageCanvas } from "../components/lineage/LineageCanvas";
import type { CanvasEdge, CanvasNode } from "../components/lineage/LineageCanvas";
import { PackageRail } from "../components/lineage/PackageRail";
import { ScanReport } from "../components/lineage/ScanReport";
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
const DIALECTS = ["mysql", "postgres", "hive", "doris", "starrocks"];

export function LineageSupplementPage() {
  const [domainId, setDomainId] = useUrlState<string>("domain", "");
  const [mode, setMode] = useState<Mode>("scan");
  const [railOpen, setRailOpen] = useState(true);
  const [railFilter, setRailFilter] = useState<RailFilter>("isolated");
  const [keyword, setKeyword] = useState("");
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

  const domains = useApi<DomainContext[]>(() => api.listDomains(), []);

  // 没选域时落到**对象最多的那个**，不是列表第一个：域列表里排在最前的往往是
  // 调试域（datahub_domain_id 是假的），进来就是一屏 DataHub 报错。
  useEffect(() => {
    if (domainId || !domains.data || domains.data.length === 0) return;
    const best = [...domains.data].sort(
      (a, b) => (b.object_type_count ?? 0) - (a.object_type_count ?? 0),
    )[0];
    setDomainId(best.id);
  }, [domainId, domains.data, setDomainId]);

  const overview = useApi<LineageOverview | null>(
    async () => (domainId ? api.lineageOverview(domainId) : null),
    [domainId],
  );
  const tables = useApi<LineageTableRow[]>(
    async () => (domainId ? api.lineageTables(domainId, { limit: 2000 }) : []),
    [domainId],
  );

  const refreshPackages = useCallback(
    async (select?: string) => {
      if (!domainId) return;
      const rows = await api.listLineagePackages(domainId);
      setPackages(rows);
      const next = select ?? (rows.length > 0 ? rows[0].id : null);
      setPkgId(next);
      setDetail(next ? await api.getLineagePackage(next) : null);
      setUncovered(await api.lineageUncoveredIsolated(domainId));
    },
    [domainId],
  );

  useEffect(() => {
    if (!domainId) return;
    void refreshPackages().catch((err: Error) => message.error(err.message));
  }, [domainId, refreshPackages]);

  const tableRows = useMemo(() => tables.data ?? [], [tables.data]);
  const tableByName = useMemo(() => new Map(tableRows.map((row) => [row.name, row])), [tableRows]);
  const isolatedNames = useMemo(
    () => new Set(tableRows.filter((row) => row.isolated).map((row) => row.name)),
    [tableRows],
  );

  const isolatedTotal = overview.data?.isolated ?? 0;
  const total = overview.data?.total ?? 0;
  const withLineage = overview.data?.with_lineage ?? 0;
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
      setDetail(await api.getLineagePackage(id));
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
      } else {
        const payload = edges
          .filter((edge) => edge.keys.length > 0)
          .map((edge) => ({
            source_table: edge.from,
            target_table: edge.to,
            join_keys: edge.keys.map((key) => `${edge.from}.${key.src} = ${edge.to}.${key.dst}`),
          }));
        const receipt = await api.applyManualLineage(domainId, payload);
        message.success(
          `已上报 ${receipt.applied} 条 · 失败 ${receipt.failed} 条 · ${receipt.resolved} 张表脱离孤岛`,
        );
        setEdges([]);
        await refreshPackages(pkgId ?? undefined);
      }
      await Promise.all([overview.reload(), tables.reload()]);
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
  const onCanvas = new Set(nodes.map((node) => node.table));

  if (domains.loading && !domains.data) return <PageSkeleton type="detail" full />;

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
            value={domainId || undefined}
            placeholder="选择数据域"
            onChange={(value) => setDomainId(value)}
            options={(domains.data ?? []).map((domain) => ({
              value: domain.id,
              label: domain.name,
            }))}
          />

          <div className="lin-stats">
            <div className="lin-stat lin-stat--iso">
              <span className="lin-stat-label">孤岛表</span>
              <span className="lin-stat-value">
                <b>{isolatedTotal}</b>
                {pending.resolved > 0 && (
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
                <i className="lin-meter-have" style={{ width: `${coveragePct}%` }} />
              </div>
            </div>

            <div className="lin-stat">
              <span className="lin-stat-label">域内表</span>
              <span className="lin-stat-value">
                <b>{total}</b>
              </span>
            </div>

            <div className="lin-stat lin-stat--src">
              <span className="lin-stat-label">目标域</span>
              <span className="lin-stat-src">
                {overview.data
                  ? `${overview.data.domain_name} · ${overview.data.platform ?? "—"} · ${
                      overview.data.databases.join(" / ") || "—"
                    }`
                  : "—"}
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
                        { label: `仅孤岛 ${isolatedTotal}`, value: "isolated" },
                        { label: `全部 ${total}`, value: "all" },
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

                  <ul className="lin-rail-list">
                    {railRows.slice(0, 300).map((row) => (
                      <li key={row.urn} className="lin-rail-row">
                        <span className="lin-rail-main">
                          <span className="lin-rail-name" title={row.name}>
                            {row.isolated && <i className="lin-iso-dot" title="孤岛表" />}
                            {row.name}
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
                onSendToCanvas={addToCanvas}
              />
            ) : (
              <LineageCanvas
                nodes={nodes}
                edges={edges}
                setNodes={setNodes}
                setEdges={setEdges}
                isolated={(table) => isolatedNames.has(table)}
                columnsOf={(table) => columns[table] ?? []}
                frozen={applying}
              />
            )}
          </main>
        </div>

        <footer className="lin-footer">
          <div className="lin-footer-counts">
            <span>将写入 DataHub</span>
            <b>{pending.edges}</b>
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
            {mode === "scan" && (
              <Select
                size="small"
                value={dialect}
                style={{ width: 116 }}
                onChange={setDialect}
                options={DIALECTS.map((item) => ({ value: item, label: `方言 ${item}` }))}
              />
            )}
            {frozen && (
              <span className="lin-done">
                已上报 {detail?.applied_edges} 条 · 幂等，重复上报不会重复建边
              </span>
            )}
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
              onConfirm={() => void apply()}
              disabled={pending.edges === 0}
            >
              <Button type="primary" loading={applying} disabled={pending.edges === 0}>
                上报到 DataHub
              </Button>
            </Popconfirm>
          </div>
        </footer>
      </div>
    </PageContainer>
  );
}
