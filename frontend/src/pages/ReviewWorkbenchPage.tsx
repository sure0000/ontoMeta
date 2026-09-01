import {
  ArrowLeftOutlined,
  AuditOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Progress,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { DecisionEvidencePanel } from "../components/review/DecisionEvidence";
import { PageContainer } from "../components/PageContainer";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import { useUrlState } from "../hooks/useUrlState";
import type {
  DomainContextDetail,
  ObjectTypeSummary,
  RelationType,
  ReviewGroup,
  ReviewModeStats,
  ReviewQueue,
} from "../types";
import { getRoleMeta, ROLE_OPTIONS } from "../utils/role";
import { getRelationStructureLabel } from "../utils/relation";
import { VerbRefinementDrawer } from "../components/review/VerbRefinementDrawer";
import { ObjectArchiveDrawer } from "../components/review/ObjectArchiveDrawer";

const { Text } = Typography;

/** 队列成员：对象与关系共用选择/判定逻辑，那部分只认 id 与 needs_review。 */
type QueueMember = ObjectTypeSummary | RelationType;

/** 一次判定动作。`role` 为空表示只确认（保持机器判定的角色）。 */
type Verdict = { role?: string; label: string };

const VERDICTS: Record<string, Verdict> = {
  a: { label: "确认" },
  "1": { role: "business_object", label: "改判业务对象" },
  "2": { role: "data_table", label: "改判数据表" },
  "3": { role: "bridge", label: "改判关系表" },
  "4": { role: "technical", label: "改判技术表" },
};

/** 撤销用的原值快照：批量改判前记下来，撤销即反向写回。 */
type UndoEntry = {
  ids: string[];
  before: Record<string, { role: string; review: boolean }>;
  kind: "object" | "relation";
};

const SCORE_BAND_COLOR: Record<string, string> = {
  strong: "green",
  near: "gold",
  weak: "orange",
  unknown: "default",
};

function formatCount(value?: number | null) {
  if (value == null) return "-";
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(value);
}

/** 后端把「各叫各名、不成族」的表并进零散桶（族名 "*"），这里给它一个人话标签。 */
function familyLabel(family: string) {
  if (!family || family === "*") return "零散表";
  return family;
}

/** 从判定证据里取一个信号的观测值（缺失给 null，不编造 0）。 */
function signal(obj: ObjectTypeSummary, key: string): number | null {
  const raw = obj.role_signals?.signals?.[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * 数值单元：等宽对齐 + 异常值染色。
 *
 * 扫一列比读一行快得多，但前提是位数对齐、且「不像业务对象」的值自己会跳出来
 * （0 主键、0 入度、百万行的日志表）。否则复核者得逐行读或先排序才发现例外。
 */
function NumCell({ value, flag }: { value: string | number | null; flag?: boolean }) {
  if (value === null || value === undefined) return <span className="review-num">—</span>;
  return <span className={`review-num${flag ? " review-num--flag" : ""}`}>{value}</span>;
}

/** 组内某个信号的取值跨度：全组同值就不必逐行看，有跨度才去找例外。 */
function spread(members: ObjectTypeSummary[], read: (m: ObjectTypeSummary) => number | null) {
  const values = members.map(read).filter((v): v is number => v != null);
  if (values.length === 0) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? formatCount(min) : `${formatCount(min)}–${formatCount(max)}`;
}

/**
 * 审核工作台：一屏判一组。
 *
 * 与工作区列表的区别不在样式，在工作单元——这里的一次操作处理**一组同类对象**
 * （同板块 + 同命名族 + 同判定强度），判据就是表格的列，例外靠反选。
 * 队列顺序由服务端确定性给出，判掉一批不会让后面的组错位（见 services/review_queue）。
 */
export function ReviewWorkbenchPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const navigate = useNavigate();
  // 看哪个板块、停在哪一组：都进 URL，刷新与返回都回到原位。
  const [segmentFilter, setSegmentFilter] = useUrlState<string>("segment", "");
  const [cursor, setCursor] = useUrlState<string>("cursor", "");
  // 对象与关系是同一套交互的两条队列：分组、排序、成组裁决完全一致，
  // 只是成员类型不同。切换不换页面，判完对象顺手判关系。
  const [kind, setKind] = useUrlState<"object" | "relation">("kind", "object", [
    "object",
    "relation",
  ]);
  const [verbDrawerOpen, setVerbDrawerOpen] = useState(false);
  // 看细节不离开队列：跳出去再回来，位置/选择集/判到哪一组全得重建。
  const [archiveOpen, setArchiveOpen] = useState(false);

  const [applying, setApplying] = useState(false);
  const [undoStack, setUndoStack] = useState<UndoEntry[]>([]);
  const [excluded, setExcluded] = useState<Record<string, string[]>>({});
  const [activeMemberId, setActiveMemberId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const domain = useApi<DomainContextDetail>(
    () => (domainId ? api.getDomain(domainId) : Promise.reject(new Error("缺少数据域 ID"))),
    [domainId],
  );
  const ontologyId = domain.data?.working_ontology_id ?? null;

  const stats = useApi<ReviewModeStats | null>(
    () => (ontologyId ? api.getReviewStats(ontologyId) : Promise.resolve(null)),
    [ontologyId],
  );

  const queue = useApi<ReviewQueue | null>(
    () =>
      ontologyId
        ? api.getReviewQueue(ontologyId, {
            kind,
            segmentId: segmentFilter || undefined,
            cursor: cursor || undefined,
            limit: 12,
          })
        : Promise.resolve(null),
    [ontologyId, kind, segmentFilter, cursor],
  );

  const groups = useMemo(() => queue.data?.groups ?? [], [queue.data]);
  const activeGroup: ReviewGroup | null = groups[0] ?? null;
  const isRelation = kind === "relation";
  // 组成员：对象走 members，关系走 relation_members。下面的选择/判定逻辑只认 id，
  // 两条队列因此共用同一套代码，不各写一遍。
  const members = useMemo(
    () =>
      activeGroup ? (isRelation ? activeGroup.relation_members : activeGroup.members) : [],
    [activeGroup, isRelation],
  );

  // 当前组换了就把焦点挪到第一名成员——右栏判据永远指着某一行。
  useEffect(() => {
    setActiveMemberId(members[0]?.id ?? null);
  }, [activeGroup?.key, members]);

  // useMemo：这个数组进了下面两个 useMemo 的依赖，每次渲染新建会让它们永远失效。
  const excludedIds = useMemo(
    () => (activeGroup ? (excluded[activeGroup.key] ?? []) : []),
    [activeGroup, excluded],
  );
  const selectedIds = useMemo(
    () => members.map((m) => m.id).filter((id) => !excludedIds.includes(id)),
    [members, excludedIds],
  );

  const toggleMember = useCallback(
    (groupKey: string, id: string) => {
      setExcluded((prev) => {
        const current = prev[groupKey] ?? [];
        return {
          ...prev,
          [groupKey]: current.includes(id)
            ? current.filter((x) => x !== id)
            : [...current, id],
        };
      });
    },
    [],
  );

  const refresh = useCallback(async () => {
    await Promise.all([queue.reload(), stats.reload()]);
  }, [queue, stats]);

  const applyVerdict = useCallback(
    async (verdict: Verdict) => {
      if (!activeGroup || selectedIds.length === 0 || applying) return;
      if (isRelation && verdict.role) return; // 关系没有角色可改判
      const before: UndoEntry["before"] = {};
      for (const member of members) {
        if (!selectedIds.includes(member.id)) continue;
        before[member.id] = {
          role: ("table_role" in member ? member.table_role : "") || "business_object",
          review: Boolean(member.needs_review),
        };
      }
      setApplying(true);
      setError(null);
      try {
        const result = isRelation
          ? await api.batchUpdateRelationTypes({ ids: selectedIds, needs_review: false })
          : await api.batchUpdateObjectTypes({
              ids: selectedIds,
              // 只确认：不动角色，仅清掉待复核。改判：后端会把改判视为复核通过。
              ...(verdict.role ? { table_role: verdict.role } : { needs_review: false }),
            });
        setUndoStack((prev) => [{ ids: selectedIds, before, kind }, ...prev].slice(0, 10));
        setExcluded((prev) => ({ ...prev, [activeGroup.key]: [] }));
        // 报服务端实际改了几条：已经是目标状态的不计数，别让人以为多判了。
        message.success(`${verdict.label} ${result.updated} 个 · ⌘Z 可撤销`);
        await refresh();
      } catch (err) {
        const msg = err instanceof Error ? err.message : "判定失败";
        setError(msg);
        message.error(msg);
      } finally {
        setApplying(false);
      }
    },
    [activeGroup, members, selectedIds, applying, refresh, isRelation, kind],
  );

  const undo = useCallback(async () => {
    const entry = undoStack[0];
    if (!entry || applying) return;
    setApplying(true);
    try {
      if (entry.kind === "relation") {
        await api.batchUpdateRelationTypes({ ids: entry.ids, needs_review: true });
      } else {
        // 原值可能不止一种（同组里有的本是数据表），按 (角色, 复核态) 分桶反向写回。
        const buckets = new Map<string, string[]>();
        for (const id of entry.ids) {
          const snap = entry.before[id];
          if (!snap) continue;
          const key = `${snap.role}|${snap.review}`;
          buckets.set(key, [...(buckets.get(key) ?? []), id]);
        }
        for (const [key, ids] of buckets) {
          const [role, review] = key.split("|");
          await api.batchUpdateObjectTypes({
            ids,
            table_role: role,
            needs_review: review === "true",
          });
        }
      }
      setUndoStack((prev) => prev.slice(1));
      message.success(`已撤销 ${entry.ids.length} 个判定`);
      await refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "撤销失败");
    } finally {
      setApplying(false);
    }
  }, [undoStack, applying, refresh]);

  const skipGroup = useCallback(() => {
    if (!queue.data) return;
    // 跳过 = 把游标推到下一组，保持待复核不变。
    const next = groups[1]?.key ?? queue.data.next_cursor ?? "";
    setCursor(next);
  }, [groups, queue.data, setCursor]);

  // ---- 键盘：审核是重复动作，鼠标点选是最慢的输入方式 ----
  const handlersRef = useRef({ applyVerdict, undo, skipGroup, activeGroup });
  handlersRef.current = { applyVerdict, undo, skipGroup, activeGroup };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      const { applyVerdict: apply, undo: doUndo, skipGroup: skip } = handlersRef.current;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        void doUndo();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const verdict = VERDICTS[e.key.toLowerCase()];
      if (verdict) {
        e.preventDefault();
        void apply(verdict);
        return;
      }
      if (e.key.toLowerCase() === "s") {
        e.preventDefault();
        skip();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const relationColumns: ColumnsType<RelationType> = useMemo(
    () => [
      {
        title: "",
        key: "select",
        width: 44,
        render: (_, row) => (
          <Checkbox
            checked={!excludedIds.includes(row.id)}
            onChange={() => activeGroup && toggleMember(activeGroup.key, row.id)}
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      {
        // 关系的最小可读单元是三元组，不是一个动词——只列动词等于什么都没说。
        title: "关系三元组",
        key: "triple",
        render: (_, row) => (
          <span className="review-obj-name">
            {row.source_object_name || "?"}
            <span className="review-triple-verb">— {row.display_name} →</span>
            {row.target_object_name || "?"}
          </span>
        ),
      },
      {
        title: "结构",
        key: "structure",
        width: 110,
        render: (_, row) => getRelationStructureLabel(row.structure_type),
      },
      { title: "基数", dataIndex: "cardinality", key: "cardinality", width: 90 },
      {
        title: "置信度",
        key: "confidence",
        width: 88,
        align: "right",
        sorter: (a, b) => (a.source_confidence ?? -1) - (b.source_confidence ?? -1),
        render: (_, row) => (
          <NumCell
            value={row.source_confidence?.toFixed(2) ?? null}
            flag={(row.source_confidence ?? 1) < 0.6}
          />
        ),
      },
    ],
    [excludedIds, activeGroup, toggleMember],
  );

  const columns: ColumnsType<ObjectTypeSummary> = useMemo(
    () => [
      {
        title: "",
        key: "select",
        width: 44,
        render: (_, row) => (
          <Checkbox
            checked={!excludedIds.includes(row.id)}
            onChange={() => activeGroup && toggleMember(activeGroup.key, row.id)}
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      {
        title: "对象",
        key: "name",
        render: (_, row) => (
          <div>
            <span className="review-obj-name">{row.display_name}</span>
            <span className="review-obj-sub">{row.name}</span>
          </div>
        ),
      },
      // 下面五列就是判据本身：可排序，异常值染色后不排序也能一眼看见
      // （0 主键 / 0 入度 / 90 万行 一眼是日志表）。
      {
        title: "主键",
        key: "pk",
        width: 62,
        align: "right",
        sorter: (a, b) => (signal(a, "pk_columns") ?? -1) - (signal(b, "pk_columns") ?? -1),
        render: (_, row) => {
          const v = signal(row, "pk_columns");
          return <NumCell value={v} flag={v === 0 || (v ?? 0) > 1} />;
        },
      },
      {
        title: "入度",
        key: "fk_in",
        width: 62,
        align: "right",
        sorter: (a, b) => (signal(a, "fk_in_degree") ?? -1) - (signal(b, "fk_in_degree") ?? -1),
        render: (_, row) => {
          const v = signal(row, "fk_in_degree");
          return <NumCell value={v} flag={v === 0} />;
        },
      },
      {
        title: "属性",
        dataIndex: "property_count",
        key: "property_count",
        width: 62,
        align: "right",
        sorter: (a, b) => a.property_count - b.property_count,
        render: (_, row) => <NumCell value={row.property_count} flag={row.property_count <= 2} />,
      },
      {
        title: "行数",
        key: "row_count",
        width: 76,
        align: "right",
        sorter: (a, b) => (a.row_count ?? -1) - (b.row_count ?? -1),
        render: (_, row) => (
          <NumCell
            value={row.row_count == null ? null : formatCount(row.row_count)}
            flag={(row.row_count ?? 0) >= 100000}
          />
        ),
      },
      {
        title: "邻居",
        key: "neighbors",
        width: 130,
        ellipsis: true,
        render: (_, row) =>
          row.top_neighbors && row.top_neighbors.length > 0 ? (
            <span className="review-obj-sub">
              {row.top_neighbors
                .slice(0, 2)
                .map((n) => `${n.direction === "inbound" ? "←" : "→"} ${n.display_name || n.name}`)
                .join(" · ")}
            </span>
          ) : (
            <NumCell value={null} />
          ),
      },
    ],
    [excludedIds, activeGroup, toggleMember],
  );

  if (domain.loading && !domain.data) return <PageSkeleton type="detail" />;

  if (!domain.data || !ontologyId) {
    return (
      <PageContainer>
        <Alert
          type="error"
          showIcon
          message={domain.error || "该数据域尚无工作本体"}
          description="先在工作区生成本体草稿，再来审核。"
        />
      </PageContainer>
    );
  }

  const pending = queue.data?.pending_total ?? 0;
  // 进度按当前队列的口径算：对象看对象、关系看关系，两者不混。
  const reviewed = stats.data
    ? isRelation
      ? stats.data.reviewed_relation_count
      : stats.data.reviewed_count
    : 0;
  const total = stats.data
    ? isRelation
      ? stats.data.total_relations
      : stats.data.total_objects
    : 0;
  const percent = total > 0 ? Math.round((reviewed / total) * 100) : 100;
  const activeMember = members.find((m) => m.id === activeMemberId) ?? members[0] ?? null;
  const activeObject = !isRelation ? (activeMember as ObjectTypeSummary | null) : null;
  const activeRelation = isRelation ? (activeMember as RelationType | null) : null;
  // 「本组已判 N 个」由服务端在完整人口上分组后给出——前端拿 size 减 members 是算不出的，
  // 判过的成员根本不在队列载荷里。
  const confirmedInGroup = activeGroup?.reviewed_in_group ?? 0;
  // 组内信号跨度：全组同值就不必逐行读，有跨度才去找例外。
  const groupSpread = isRelation
    ? []
    : (
        [
          ["主键", (m: ObjectTypeSummary) => signal(m, "pk_columns")],
          ["入度", (m: ObjectTypeSummary) => signal(m, "fk_in_degree")],
          ["属性", (m: ObjectTypeSummary) => m.property_count],
          ["行数", (m: ObjectTypeSummary) => m.row_count ?? null],
        ] as const
      )
        .map(([label, read]) => [label, spread(members as ObjectTypeSummary[], read)] as const)
        .filter(([, value]) => value !== null);

  return (
    <PageContainer full>
      <div className="review-workbench">
        <div className="review-topbar">
          <Space size={12}>
            <Link to={`/workspace/${domainId}`}>
              <Button icon={<ArrowLeftOutlined />} aria-label="返回工作区" />
            </Link>
            <span className="review-topbar-title">
              <AuditOutlined /> 审核 · {domain.data.name}
            </span>
            <Segmented
              value={kind}
              onChange={(value) => {
                setKind(value as "object" | "relation");
                setCursor("");
              }}
              options={[
                { label: `对象 ${stats.data?.needs_review_count ?? 0}`, value: "object" },
                {
                  label: `关系 ${stats.data?.relation_needs_review_count ?? 0}`,
                  value: "relation",
                },
              ]}
            />
          </Space>
          <div className="review-topbar-progress">
            <Progress
              percent={percent}
              size="small"
              showInfo={false}
              strokeColor="var(--om-success)"
              trailColor="var(--om-bg-soft)"
            />
            <span className="review-topbar-count">
              还剩 <b>{pending}</b>
              {isRelation ? "条" : "个"} · 已判 {reviewed} / {total}
            </span>
          </div>
          <Space>
            {isRelation && (
              <Button icon={<BulbOutlined />} onClick={() => setVerbDrawerOpen(true)}>
                动词建议
              </Button>
            )}
            <Tooltip title="撤销上一次判定（⌘Z）">
              <Button
                icon={<UndoOutlined />}
                disabled={undoStack.length === 0 || applying}
                onClick={() => void undo()}
              >
                撤销
              </Button>
            </Tooltip>
            <Button
              type="primary"
              icon={<CheckCircleOutlined />}
              onClick={() => navigate(`/workspace/${domainId}`)}
            >
              回工作区发布
            </Button>
          </Space>
        </div>

        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            closable
            onClose={() => setError(null)}
            style={{ marginBottom: 12 }}
          />
        )}

        <div className="review-panes">
          <aside className="review-pane review-pane--queue">
            <div className="review-pane-label">{isRelation ? "按板块筛选" : "队列"}</div>
            <button
              type="button"
              className={`review-seg ${segmentFilter === "" ? "review-seg--on" : ""}`}
              onClick={() => {
                setSegmentFilter("");
                setCursor("");
              }}
            >
              <span>全部板块</span>
              <span className="review-seg-num">{pending}</span>
            </button>
            {(stats.data?.segment_progress ?? []).map((seg) => (
              <button
                type="button"
                key={seg.segment_id}
                className={`review-seg ${segmentFilter === seg.segment_id ? "review-seg--on" : ""} ${
                  !isRelation && seg.needs_review_count === 0 ? "review-seg--done" : ""
                }`}
                onClick={() => {
                  setSegmentFilter(seg.segment_id);
                  setCursor("");
                }}
              >
                <span className="review-seg-name" title={seg.segment_name}>
                  {!isRelation && seg.needs_review_count === 0 ? "✓ " : ""}
                  {seg.segment_name}
                </span>
                {/* 板块进度是对象口径的。关系队列按源端对象的板块筛选，但拿对象的
                    进度当关系进度会读成假数字——那里只留板块名。 */}
                {!isRelation && (
                  <>
                    <span className="review-seg-num">
                      {seg.reviewed_count}/{seg.total_count}
                    </span>
                    <span className="review-seg-bar">
                      <i style={{ width: `${Math.round(seg.progress_ratio * 100)}%` }} />
                    </span>
                  </>
                )}
              </button>
            ))}
            {(stats.data?.unsegmented_total ?? 0) > 0 && <div className="review-seg-divider" />}
            {(stats.data?.unsegmented_total ?? 0) > 0 && (
              <button
                type="button"
                className={`review-seg ${segmentFilter === "-" ? "review-seg--on" : ""}`}
                onClick={() => {
                  setSegmentFilter("-");
                  setCursor("");
                }}
              >
                <span className="review-seg-name">未接入板块</span>
                {!isRelation && (
                  <span className="review-seg-num">{stats.data?.unsegmented_pending ?? 0}</span>
                )}
              </button>
            )}
          </aside>

          <section className="review-pane review-pane--group">
            {!activeGroup ? (
              <div className="review-empty">
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    queue.loading
                      ? "加载中…"
                      : pending === 0
                        ? `这个范围已经判完了${segmentFilter ? "，换一个板块继续" : ""}`
                        : "当前游标之后没有待判的组"
                  }
                />
                {cursor && !queue.loading && (
                  <Button onClick={() => setCursor("")}>回到队列开头</Button>
                )}
              </div>
            ) : (
              <>
                <div className="review-group-head">
                  <div style={{ minWidth: 0 }}>
                    <div className="review-group-title">
                      {activeGroup.segment_name} · <em>{familyLabel(activeGroup.name_family)}</em> ·{" "}
                      {activeGroup.size} {isRelation ? "条" : "张"}
                    </div>
                    <Space size={6} style={{ marginTop: 6 }} wrap>
                      <Tag color={isRelation ? "blue" : getRoleMeta(activeGroup.table_role).color}>
                        {isRelation
                          ? `结构：${getRelationStructureLabel(activeGroup.table_role)}`
                          : `机器判定：${getRoleMeta(activeGroup.table_role).label}`}
                      </Tag>
                      <Tag color={SCORE_BAND_COLOR[activeGroup.score_band]}>
                        {activeGroup.score_band_label}
                      </Tag>
                      {activeGroup.truncated && (
                        <Tooltip title="本组过大，先判这一批，剩下的下次进来继续">
                          <Tag>仅显示前 {members.length} 个</Tag>
                        </Tooltip>
                      )}
                    </Space>
                    {groupSpread.length > 0 && (
                      <div className="review-group-spread">
                        {groupSpread.map(([label, value]) => (
                          <span key={label}>
                            {label} <b>{value}</b>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <span className="review-group-pos">
                    {(queue.data?.group_offset ?? 0) + 1} / {queue.data?.group_total ?? 0} 组
                  </span>
                </div>

                <div className="review-group-body">
                  <Spin spinning={queue.loading}>
                    <Table
                      className="om-table review-group-table"
                      rowKey="id"
                      size="small"
                      columns={
                        (isRelation ? relationColumns : columns) as ColumnsType<QueueMember>
                      }
                      dataSource={members as QueueMember[]}
                      pagination={false}
                      // 极窄容器下宁可横向滚动，也不让对象名被压成竖排单字。
                      scroll={{ x: 520 }}
                      rowClassName={(row) =>
                        [
                          row.id === activeMemberId ? "review-row--active" : "",
                          excludedIds.includes(row.id) ? "review-row--excluded" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")
                      }
                      onRow={(row) => ({ onClick: () => setActiveMemberId(row.id) })}
                    />
                  </Spin>
                </div>

                <div className="review-actions">
                  <Button
                    type="primary"
                    size="large"
                    loading={applying}
                    disabled={selectedIds.length === 0}
                    onClick={() => void applyVerdict(VERDICTS.a)}
                  >
                    <kbd className="review-key">A</kbd>
                    确认这 {selectedIds.length} 个
                  </Button>
                  {!isRelation && (
                    <>
                      <span className="review-actions-sep" />
                      <span className="review-actions-label">改判为</span>
                      {ROLE_OPTIONS.map((option, index) => (
                        <Button
                          key={option.value}
                          disabled={applying || selectedIds.length === 0}
                          onClick={() =>
                            void applyVerdict({ role: option.value, label: `改判${option.label}` })
                          }
                        >
                          <kbd className="review-key">{index + 1}</kbd>
                          {option.label}
                        </Button>
                      ))}
                    </>
                  )}
                  <span className="review-spacer" />
                  <Button disabled={applying} onClick={skipGroup}>
                    <kbd className="review-key">S</kbd>
                    跳过本组
                  </Button>
                </div>
                <div className="review-hint">
                  组内默认全选，反选掉例外后再判；判完自动进入下一组，误判按 ⌘Z 撤销。
                </div>
              </>
            )}
          </section>

          <aside className="review-pane review-pane--evidence">
            <div className="review-pane-label">判定依据</div>
            {activeMember && (
              <div className="review-evidence-title">{activeMember.display_name}</div>
            )}
            {activeObject ? (
              <div className="review-evidence-body">
                <DecisionEvidencePanel obj={activeObject} compact />
                {confirmedInGroup > 0 && (
                  <div className="review-evidence-streak">
                    本组已判 <b>{confirmedInGroup}</b> 个，还剩 {activeGroup?.size ?? 0} 个
                  </div>
                )}
                <div className="review-evidence-foot">
                  <Button type="link" style={{ padding: 0 }} onClick={() => setArchiveOpen(true)}>
                    打开完整档案 →
                  </Button>
                </div>
              </div>
            ) : activeRelation ? (
              <div className="review-evidence-body">
                <div style={{ marginBottom: 12 }}>
                  {activeRelation.source_object_name}
                  <span className="review-triple-verb">— {activeRelation.display_name} →</span>
                  {activeRelation.target_object_name}
                </div>
                <div className="review-evidence-row">
                  <span>结构</span>
                  <span>{getRelationStructureLabel(activeRelation.structure_type)}</span>
                </div>
                <div className="review-evidence-row">
                  <span>基数</span>
                  <span>{activeRelation.cardinality || "—"}</span>
                </div>
                <div className="review-evidence-row">
                  <span>置信度</span>
                  <span>{activeRelation.source_confidence?.toFixed(2) ?? "—"}</span>
                </div>
                <div className="review-evidence-note">
                  {activeRelation.source_evidence || activeRelation.description || "暂无证据说明"}
                </div>
                <div className="review-evidence-foot">
                  <Link
                    to={`/workspace/${domainId}/relations/${activeRelation.id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开关系详情 →
                  </Link>
                </div>
              </div>
            ) : (
              <Text type="secondary">选中左侧任一行查看判定依据。</Text>
            )}
          </aside>
        </div>

        <ObjectArchiveDrawer
          objectId={activeObject?.id ?? null}
          open={archiveOpen}
          onClose={() => setArchiveOpen(false)}
          domainId={domainId}
        />

        {ontologyId && (
          <VerbRefinementDrawer
            ontologyId={ontologyId}
            open={verbDrawerOpen}
            onClose={() => setVerbDrawerOpen(false)}
            onApplied={refresh}
          />
        )}
      </div>
    </PageContainer>
  );
}
