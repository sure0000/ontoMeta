import {
  ArrowLeftOutlined,
  AuditOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  MoreOutlined,
  PartitionOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  RollbackOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  Popover,
  Progress,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
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
  PageResult,
  RelationType,
  ReviewGroup,
  ReviewModeStats,
  ReviewQueue,
  SegmentReviewProgress,
  SegmentSummary,
} from "../types";
import {
  getRoleMeta,
  LOW_CONFIDENCE,
  parseRoleReason,
  reviewFlags,
  riskRank,
  ROLE_OPTIONS,
  summarizeFlags,
} from "../utils/role";
import type { ReviewFlag } from "../utils/role";
import { getRelationStructureLabel, parseJoinKey, relationReviewFlags } from "../utils/relation";
import { VerbRefinementDrawer } from "../components/review/VerbRefinementDrawer";
import { ObjectArchiveDrawer } from "../components/review/ObjectArchiveDrawer";
import { FlagChips, MachineMark, WhyReview } from "../components/review/ReviewSignals";

/** 队列成员：对象与关系共用选择/判定逻辑，那部分只认 id 与 needs_review。 */
type QueueMember = ObjectTypeSummary | RelationType;

/** 审核范围：两个顶层 tab（对象 / 关系），关系页内再分外键与关系表。 */
type ReviewScope = "object" | "relation" | "bridge";

/**
 * 看待判的还是回看已判的。
 *
 * 判完不等于看不见：判错的要能被找回来重判，判完的板块要能原样打开复查。
 * 服务端在「待判 + 已判」的完整人口上分组，两个视图因此是同一批组、同一套 key，
 * 位置感不会因为切换而丢。
 */
type ReviewStatus = "pending" | "reviewed";

/** 对象页收的角色：关系表挪到关系页去判，不在这里出现。 */
const OBJECT_SCOPE_ROLES = ["business_object", "data_table", "technical"];

/**
 * 一次判定动作。四个字段互相独立，组合出全部动作：
 * 只确认（review=false）、改判（role）、归类（segmentId）、退回重判（review=true）。
 * 改判不带 review——后端把「人工改了角色」本身视为复核通过。
 */
type Verdict = { role?: string; segmentId?: string; review?: boolean; label: string };

const VERDICTS: Record<string, Verdict> = {
  "1": { role: "business_object", label: "改判业务对象" },
  "2": { role: "data_table", label: "改判数据表" },
  "3": { role: "bridge", label: "改判关系表" },
  "4": { role: "technical", label: "改判技术表" },
};

/** 撤销用的原值快照：批量改判前记下来，撤销即反向写回。 */
type UndoEntry = {
  ids: string[];
  // 板块也要记：「归类」改的是板块，撤销必须把它挪回原处，光还原复核态没用。
  before: Record<string, { role: string; review: boolean; segment: string | null }>;
  kind: ReviewScope;
};

/** 「系统表」板块的 kind（后端 services/segment_kinds）。 */
const SYSTEM_SEGMENT_KIND = "system";
const STRAND_HINT = "业务对象/关系表压在系统表里，说明归错了地方——挑个业务板块移过去";

/**
 * 待判计数的红色上标。
 *
 * 「还剩多少要判」是这一屏唯一持续变化的数字，混在灰色的 12/40 里读不出来。
 * 提成上标 + 红色之后，扫一眼侧栏就知道哪几块还没判完；判完自动消失（不占位）。
 */
function PendingSup({ count }: { count?: number | null }) {
  if (!count) return null;
  return (
    <sup className="review-sup" title={`${count} 个待判`}>
      {count > 999 ? "999+" : count}
    </sup>
  );
}

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

/** 图连通性：优先读 connected，回落到 isolated 的反面；两者皆无＝这一项没测过。 */
function connectivity(obj: ObjectTypeSummary): boolean | null {
  const s = obj.role_signals?.signals;
  if (!s) return null;
  if (s.connected !== undefined && s.connected !== null) return Boolean(s.connected);
  if (s.isolated !== undefined && s.isolated !== null) return !s.isolated;
  return null;
}

/**
 * 把握：低于阈值就染色。
 *
 * 机器自己没底恰恰是这一条被推给人看的理由，它不该是一个小号灰数字。
 */
function ConfCell({ value }: { value?: number | null }) {
  if (typeof value !== "number") return <span className="review-num">—</span>;
  const shaky = value < LOW_CONFIDENCE;
  return (
    <span className={`review-num${shaky ? " review-num--flag" : ""}`}>
      {Math.round(value * 100)}%
    </span>
  );
}

/** 连通是常态，说了等于没说；孤立才是要看的那一条，所以只给孤立上色。 */
function ConnCell({ connected }: { connected: boolean | null }) {
  if (connected === null) return <span className="review-num">—</span>;
  if (connected) return <span className="review-cell-ok">连通</span>;
  return (
    <Tooltip title="图上孤立：既无外键关联也无血缘，业务对象很少是这样">
      <span className="review-cell-bad">孤立</span>
    </Tooltip>
  );
}

/**
 * 判定依据进列。
 *
 * 右栏撤掉之后这一列得自带深度：行内一句话说完「机器为什么心虚 + 这张表是干什么的」，
 * 悬停展开的才是原来右栏那一整块（逐条信号、结构判据、邻居）。两级之间不重复也不丢。
 */
function ObjectBasisCell({ obj, flags }: { obj: ObjectTypeSummary; flags: ReviewFlag[] }) {
  const reading = parseRoleReason(obj.role_reason).llmReading;
  // 图连通性自己是一列了，chip 里不必再说一遍——省下的宽度归 LLM 读表那句话。
  const chips = flags.filter((f) => f.key !== "connected");
  return (
    <Popover
      trigger="hover"
      mouseEnterDelay={0.3}
      placement="left"
      title={obj.display_name}
      content={
        <div className="review-basis-pop">
          <DecisionEvidencePanel obj={obj} compact />
          <div className="review-evidence-row">
            <span>行数</span>
            <span>{obj.row_count == null ? "—" : formatCount(obj.row_count)}</span>
          </div>
          {obj.top_neighbors && obj.top_neighbors.length > 0 && (
            <div className="review-neighbors">
              <span className="review-neighbors-label">邻居</span>
              {obj.top_neighbors.slice(0, 6).map((n) => (
                <Tooltip key={n.id} title={n.relation_name}>
                  <span className="review-neighbor">
                    {n.direction === "inbound" ? "←" : "→"} {n.display_name || n.name}
                  </span>
                </Tooltip>
              ))}
            </div>
          )}
        </div>
      }
    >
      <div className="review-basis">
        <FlagChips flags={chips} max={2} />
        {reading && <span className="review-basis-reading">{reading}</span>}
      </div>
    </Popover>
  );
}

/** 关系的判定依据：结构/基数/连接键这类窄事实进悬停，行内只留旗标与证据首句。 */
function RelationBasisCell({
  relation,
  flags,
  domainId,
}: {
  relation: RelationType;
  flags: ReviewFlag[];
  domainId?: string;
}) {
  const note = relation.source_evidence || relation.description;
  return (
    <Popover
      trigger="hover"
      mouseEnterDelay={0.3}
      placement="left"
      title={relation.display_name}
      content={
        <div className="review-basis-pop">
          <WhyReview flags={flags} />
          <div className="review-evidence-row">
            <span>结构</span>
            <span>{getRelationStructureLabel(relation.structure_type)}</span>
          </div>
          <div className="review-evidence-row">
            <span>基数</span>
            <span>{relation.cardinality || "—"}</span>
          </div>
          <div className="review-evidence-row">
            <span>连接键</span>
            <span>{parseJoinKey(relation.source_evidence || relation.description) || "—"}</span>
          </div>
          <div className="review-evidence-note">{note || "暂无证据说明"}</div>
          {domainId && (
            <div className="review-evidence-foot">
              <Link
                to={`/workspace/${domainId}/relations/${relation.id}`}
                target="_blank"
                rel="noreferrer"
              >
                打开关系详情 →
              </Link>
            </div>
          )}
        </div>
      }
    >
      <div className="review-basis">
        <FlagChips flags={flags} max={2} />
        {note && <span className="review-basis-reading">{note}</span>}
      </div>
    </Popover>
  );
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
  // 三种审核范围，两个顶层 tab：
  //   object  → 对象页：业务对象/数据表/技术表
  //   relation→ 关系页 · 外键关系（RelationType）
  //   bridge  → 关系页 · 关系表（table_role=bridge 的**对象**）
  //
  // 关系表是「表」不是「边」，数据上是 ObjectType；但复核者面对它时问的是关系的问题
  // （这张表是不是只是把 A 和 B 连起来），所以归到关系页。
  const [kind, setKind] = useUrlState<ReviewScope>("kind", "object", [
    "object",
    "relation",
    "bridge",
  ]);
  // 待判 / 已判。判完的板块要能回去看判了什么、把判错的退回重判——判定可逆是敢快判的前提。
  const [status, setStatus] = useUrlState<ReviewStatus>("status", "pending", [
    "pending",
    "reviewed",
  ]);
  const [verbDrawerOpen, setVerbDrawerOpen] = useState(false);
  // 看细节不离开队列：跳出去再回来，位置/选择集/判到哪一组全得重建。
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [segmentEditor, setSegmentEditor] = useState<"create" | "edit" | null>(null);
  const [editingSegment, setEditingSegment] = useState<SegmentSummary | null>(null);
  const [segmentSaving, setSegmentSaving] = useState(false);
  const [segmentForm] = Form.useForm<{
    name?: string;
    display_name: string;
    description?: string;
  }>();

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
            // 关系表在数据上是对象，走对象队列，只是把角色收窄到 bridge。
            kind: kind === "relation" ? "relation" : "object",
            status,
            roleIn:
              kind === "bridge" ? ["bridge"] : kind === "object" ? OBJECT_SCOPE_ROLES : undefined,
            segmentId: segmentFilter || undefined,
            cursor: cursor || undefined,
            limit: 12,
          })
        : Promise.resolve(null),
    [ontologyId, kind, status, segmentFilter, cursor],
  );

  // 移动的目的地：**所有**板块，系统表也在内。分错的要能移出来，也要能被移回去
  // ——只给业务板块的话，误判成业务对象、被人移进销售的技术表就再也退不回系统表。
  // 业务板块排在前面（成员多的靠前），系统表垫底。
  const segments = useApi<PageResult<SegmentSummary> | null>(
    () => (ontologyId ? api.listSegments({ ontologyId, limit: 200 }) : Promise.resolve(null)),
    [ontologyId],
  );
  const moveTargets = useMemo(
    () =>
      (segments.data?.items ?? [])
        .slice()
        .sort(
          (a, b) =>
            (a.kind === "system" ? 1 : 0) - (b.kind === "system" ? 1 : 0) ||
            b.member_count - a.member_count,
        )
        .map((seg) => ({
          value: seg.id,
          label: `${seg.display_name}（${seg.member_count}）`,
        })),
    [segments.data],
  );
  const [moveTarget, setMoveTarget] = useState<string | undefined>();

  const groups = useMemo(() => queue.data?.groups ?? [], [queue.data]);
  const activeGroup: ReviewGroup | null = groups[0] ?? null;
  // isRelation 只指「外键」这条队列：关系表虽然挂在关系页，但它是对象，
  // 列、判定接口、改判按钮全走对象那一套——尤其是改判，误判的关系表要能调回业务对象。
  const isRelation = kind === "relation";
  const isBridgeScope = kind === "bridge";
  /** 侧栏与顶栏的计数口径跟着范围走，三者各算各的。 */
  const relationScope = kind !== "object";
  const isReviewedView = status === "reviewed";
  // 这一组归错了地方：业务对象/关系表压在系统表里。不再拿门禁挡确认（那只会留下一个
  // 被禁掉的按钮），而是把「移动到板块」摆到最显眼处，并在组头说清缺的是什么。
  // 判定由服务端给（同一条规则在后端 segment_placement 写一次），前端不重算。
  const isStranded = Boolean(activeGroup?.stranded_in_system);
  // 组成员：对象走 members，关系走 relation_members。下面的选择/判定逻辑只认 id，
  // 两条队列因此共用同一套代码，不各写一遍。
  const members = useMemo(
    () => (activeGroup ? (isRelation ? activeGroup.relation_members : activeGroup.members) : []),
    [activeGroup, isRelation],
  );

  // 每个成员「机器为什么把它推给我看」：对象读分类证据，关系读动词/证据/置信度。
  const memberFlags = useMemo(() => {
    const map = new Map<string, ReviewFlag[]>();
    for (const member of members) {
      map.set(
        member.id,
        isRelation
          ? relationReviewFlags(member as RelationType)
          : reviewFlags(member as ObjectTypeSummary),
      );
    }
    return map;
  }, [members, isRelation]);

  // 共性归组头、例外归行内。29 张 InnoDB 系统表全是同一种分歧，逐行标红等于没标；
  // 11 张销售对象里只有 1 张是机器自我推翻——那一张才需要跳出来。
  const flagSummary = useMemo(
    () => summarizeFlags(members.map((m) => ({ id: m.id, flags: memberFlags.get(m.id) ?? [] }))),
    [members, memberFlags],
  );
  const exceptionIds = flagSummary.exceptionIds;
  const exceptionSet = useMemo(() => new Set(exceptionIds), [exceptionIds]);

  // 例外排前面：本组唯一一条「机器改判」不该躺在第 11 行等人翻到。
  // 只影响本组内部的呈现顺序，与队列游标无关（组的排序键仍由服务端确定）。
  const orderedMembers = useMemo(() => {
    const rank = (m: QueueMember) => riskRank(memberFlags.get(m.id) ?? [], flagSummary.commonKeys);
    return members
      .map((member, index) => ({ member, index }))
      .sort((a, b) => rank(b.member) - rank(a.member) || a.index - b.index)
      .map((entry) => entry.member);
  }, [members, memberFlags, flagSummary]);

  // 当前组换了就把焦点挪回表首——判过一组之后，选中行不该停在上一组的位置。
  useEffect(() => {
    setActiveMemberId(orderedMembers[0]?.id ?? null);
  }, [activeGroup?.key, orderedMembers]);

  // 默认非全选：进入一个新组时把全部成员排除（=空选），审核人逐条勾选要判的。
  // 已有记录的组不重复初始化，避免覆盖用户已做的选择。
  useEffect(() => {
    if (!activeGroup) return;
    const groupKey = activeGroup.key;
    const allIds = orderedMembers.map((m) => m.id);
    setExcluded((prev) => {
      if (prev[groupKey] !== undefined || allIds.length === 0) return prev;
      return { ...prev, [groupKey]: allIds };
    });
  }, [activeGroup, orderedMembers]);

  // useMemo：这个数组进了下面两个 useMemo 的依赖，每次渲染新建会让它们永远失效。
  const excludedIds = useMemo(
    () => (activeGroup ? (excluded[activeGroup.key] ?? []) : []),
    [activeGroup, excluded],
  );
  const selectedIds = useMemo(
    () => members.map((m) => m.id).filter((id) => !excludedIds.includes(id)),
    [members, excludedIds],
  );

  const toggleMember = useCallback((groupKey: string, id: string) => {
    setExcluded((prev) => {
      const current = prev[groupKey] ?? [];
      return {
        ...prev,
        [groupKey]: current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
      };
    });
  }, []);

  // 全选状态：非全选时表头复选框为半选，空选时为空，全选时为勾。
  const allSelected = selectedIds.length > 0 && selectedIds.length === members.length;
  const someSelected = selectedIds.length > 0 && selectedIds.length < members.length;

  const toggleSelectAll = useCallback(() => {
    if (!activeGroup) return;
    const groupKey = activeGroup.key;
    setExcluded((prev) => {
      // 当前已全选 → 清空（空选）；否则 → 全选（排除集清空）。
      const isAll = members.length > 0 && selectedIds.length === members.length;
      return { ...prev, [groupKey]: isAll ? members.map((m) => m.id) : [] };
    });
  }, [activeGroup, members, selectedIds.length]);

  /**
   * 一键把「例外」剔出选择集：组内多数一次确认，剩下那几条单独看。
   * 再按一次恢复全选——判错方向时不用逐个点回来。
   */
  const toggleExceptions = useCallback(() => {
    if (!activeGroup || exceptionIds.length === 0) return;
    const groupKey = activeGroup.key;
    setExcluded((prev) => {
      const current = prev[groupKey] ?? [];
      const alreadyExcluded = exceptionIds.every((id) => current.includes(id));
      return { ...prev, [groupKey]: alreadyExcluded ? [] : [...exceptionIds] };
    });
  }, [activeGroup, exceptionIds]);

  const refresh = useCallback(async () => {
    await Promise.all([queue.reload(), stats.reload(), segments.reload()]);
  }, [queue, stats, segments]);

  const openCreateSegment = useCallback(() => {
    setEditingSegment(null);
    segmentForm.resetFields();
    setSegmentEditor("create");
  }, [segmentForm]);

  const openEditSegment = useCallback(
    (segment: SegmentSummary) => {
      setEditingSegment(segment);
      segmentForm.setFieldsValue({
        name: segment.name,
        display_name: segment.display_name,
        description: segment.description || "",
      });
      setSegmentEditor("edit");
    },
    [segmentForm],
  );

  const saveSegment = useCallback(async () => {
    if (!ontologyId) return;
    try {
      const values = await segmentForm.validateFields();
      setSegmentSaving(true);
      if (segmentEditor === "create") {
        const created = await api.createSegment({ ontology_id: ontologyId, ...values });
        setMoveTarget(created.id);
        message.success(`已新建业务板块「${created.display_name}」`);
      } else if (editingSegment) {
        await api.updateSegment(editingSegment.id, values);
        message.success("业务板块已重命名");
      }
      setSegmentEditor(null);
      setEditingSegment(null);
      await refresh();
    } catch (err) {
      if (err instanceof Error) {
        message.error(err.message);
      }
    } finally {
      setSegmentSaving(false);
    }
  }, [ontologyId, segmentForm, segmentEditor, editingSegment, refresh]);

  const removeSegment = useCallback(
    async (segment: SegmentSummary) => {
      try {
        setSegmentSaving(true);
        const result = await api.deleteSegment(segment.id);
        if (segmentFilter === segment.id) {
          setSegmentFilter("");
          setCursor("");
        }
        if (moveTarget === segment.id) setMoveTarget(undefined);
        message.success(
          result.reassigned > 0
            ? `已删除「${segment.display_name}」，${result.reassigned} 个成员已重新分配`
            : `已删除「${segment.display_name}」`,
        );
        await refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "删除业务板块失败");
      } finally {
        setSegmentSaving(false);
      }
    },
    [segmentFilter, moveTarget, refresh, setSegmentFilter, setCursor],
  );

  const applyVerdict = useCallback(
    async (verdict: Verdict) => {
      if (!activeGroup || selectedIds.length === 0 || applying) return;
      if (isRelation && (verdict.role || verdict.segmentId)) return; // 关系没有角色/板块可改
      const before: UndoEntry["before"] = {};
      for (const member of members) {
        if (!selectedIds.includes(member.id)) continue;
        before[member.id] = {
          role: ("table_role" in member ? member.table_role : "") || "business_object",
          review: Boolean(member.needs_review),
          segment: "segment_id" in member ? (member.segment_id ?? null) : null,
        };
      }
      setApplying(true);
      setError(null);
      try {
        const result = isRelation
          ? await api.batchUpdateRelationTypes({
              ids: selectedIds,
              needs_review: verdict.review ?? false,
            })
          : await api.batchUpdateObjectTypes({
              ids: selectedIds,
              // 三件事可以同时发：改判角色、归类到板块、置复核态。
              // 改判不带 needs_review——后端把「人工改了角色」本身视为复核通过。
              ...(verdict.role ? { table_role: verdict.role } : {}),
              ...(verdict.segmentId ? { segment_id: verdict.segmentId } : {}),
              ...(verdict.review === undefined ? {} : { needs_review: verdict.review }),
            });
        setUndoStack((prev) => [{ ids: selectedIds, before, kind }, ...prev].slice(0, 10));
        // 判完后该组记录清除，刷新后若仍是同组会被上面的 effect 重新初始化为空选。
        setExcluded((prev) => {
          if (!activeGroup) return prev;
          const next = { ...prev };
          delete next[activeGroup.key];
          return next;
        });
        // 报服务端实际改了几条：已经是目标状态的不计数，别让人以为多判了。
        // 判成业务对象却归不进任何业务模块的那批还留在系统表里——不说清楚，
        // 看起来就像整组归好位了。
        const stranded = "stranded_in_system" in result ? (result.stranded_in_system ?? 0) : 0;
        const suffix = stranded > 0 ? `，其中 ${stranded} 个还在系统表里待移出` : "";
        message.success(`${verdict.label} ${result.updated} 个${suffix} · ⌘Z 可撤销`);
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
        // 原值可能不止一种（同组里有的本是数据表），按 (角色, 复核态, 板块) 分桶反向写回。
        // 板块必须一起还原：撤销一次「归类」而不把对象挪回去，等于只撤了一半。
        const buckets = new Map<string, string[]>();
        for (const id of entry.ids) {
          const snap = entry.before[id];
          if (!snap) continue;
          const key = `${snap.role}|${snap.review}|${snap.segment ?? ""}`;
          buckets.set(key, [...(buckets.get(key) ?? []), id]);
        }
        for (const [key, ids] of buckets) {
          const [role, review, segment] = key.split("|");
          await api.batchUpdateObjectTypes({
            ids,
            table_role: role,
            // 空串＝移出板块（后端约定）。先挪回原处，复核态才可能被接受。
            segment_id: segment,
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

  /**
   * 主动作（`A`）：待判视图里是「确认」，已判视图里是「退回复核」。
   *
   * 一个键位对应「这一屏最该做的那件事」，两个视图各自只有一件。归错地方的那一组
   * 也照常能确认——判定不该被板块归属挡住，缺的归属由下面那行「移动到」补，
   * 没补的会在回执里如实报出来。
   */
  const runPrimary = useCallback(() => {
    if (selectedIds.length === 0) {
      message.warning("请先勾选要判的成员");
      return;
    }
    if (isReviewedView) {
      Modal.confirm({
        title: `退回复核 ${selectedIds.length} 个？`,
        content: "退回的成员将重新进入待复核队列。",
        okText: "退回",
        cancelText: "取消",
        onOk: () => applyVerdict({ label: "退回复核", review: true }),
      });
      return;
    }
    Modal.confirm({
      title: `确认这 ${selectedIds.length} 个？`,
      content: "确认后将标记为已复核，⌘Z 可撤销。",
      okText: "确认",
      cancelText: "取消",
      onOk: () => applyVerdict({ label: "确认", review: false }),
    });
  }, [applyVerdict, isReviewedView, selectedIds.length]);

  /** 移动并确认：挪板块与判复核是同一次请求，后端因此看得到挪过之后的归属。 */
  const moveGroup = useCallback(() => {
    if (selectedIds.length === 0) {
      message.warning("请先勾选要移动的成员");
      return;
    }
    if (!moveTarget) {
      message.warning("先选一个板块");
      return;
    }
    const name = moveTargets.find((opt) => opt.value === moveTarget)?.label ?? "所选板块";
    Modal.confirm({
      title: `把 ${selectedIds.length} 个移入「${name}」？`,
      content: "移动并计为已复核，⌘Z 可撤销。",
      okText: "移动",
      cancelText: "取消",
      onOk: () => applyVerdict({ segmentId: moveTarget, review: false, label: `移入 ${name}` }),
    });
  }, [applyVerdict, moveTarget, moveTargets, selectedIds.length]);

  // ---- 键盘：审核是重复动作，鼠标点选是最慢的输入方式 ----
  // overlayOpen：抽屉盖着时按 A 会把底下那组直接确认掉——人以为在抽屉里操作，
  // 实际上判掉了一批关系。键位只在队列本身有焦点时才生效。
  const handlersRef = useRef({
    applyVerdict,
    runPrimary,
    undo,
    skipGroup,
    toggleExceptions,
    activeGroup,
    overlayOpen: false,
  });
  handlersRef.current = {
    applyVerdict,
    runPrimary,
    undo,
    skipGroup,
    toggleExceptions,
    activeGroup,
    overlayOpen: verbDrawerOpen || archiveOpen,
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)
      ) {
        return;
      }
      const {
        applyVerdict: apply,
        runPrimary: primary,
        undo: doUndo,
        skipGroup: skip,
        toggleExceptions: toggleEx,
        overlayOpen,
      } = handlersRef.current;
      if (overlayOpen) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        void doUndo();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key.toLowerCase() === "a") {
        e.preventDefault();
        primary();
        return;
      }
      const verdict = VERDICTS[e.key.toLowerCase()];
      if (verdict) {
        e.preventDefault();
        void apply(verdict);
        return;
      }
      if (e.key.toLowerCase() === "s") {
        e.preventDefault();
        skip();
        return;
      }
      if (e.key.toLowerCase() === "x") {
        e.preventDefault();
        toggleEx();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /** 点对象名＝开完整档案：看细节不离开队列，位置、选择集、判到哪一组都还在。 */
  const openArchive = useCallback((id: string) => {
    setActiveMemberId(id);
    setArchiveOpen(true);
  }, []);

  /**
   * 改判收进下拉，键位仍在原处。
   *
   * 键位是**位置固定**的（1..4 直连 VERDICTS），所以先带上原下标再过滤：关系表页里
   * 「改判关系表」是空操作，去掉，但 1/2/4 不能跟着挪。留下的正是「误判的关系表调回
   * 业务对象」这条路。
   */
  const recastItems = useMemo(
    () =>
      ROLE_OPTIONS.map((option, index) => ({ option, index }))
        .filter(({ option }) => !(isBridgeScope && option.value === "bridge"))
        .map(({ option, index }) => ({
          key: option.value,
          label: (
            <span>
              <kbd className="review-key">{index + 1}</kbd>
              {option.label}
            </span>
          ),
        })),
    [isBridgeScope],
  );

  const onRecast = useCallback(
    ({ key }: { key: string }) => {
      if (selectedIds.length === 0) {
        message.warning("请先勾选要改判的成员");
        return;
      }
      const option = ROLE_OPTIONS.find((o) => o.value === key);
      if (!option) return;
      Modal.confirm({
        title: `把 ${selectedIds.length} 个改判为「${option.label}」？`,
        content: "改判角色同时计为已复核，⌘Z 可撤销。",
        okText: "改判",
        cancelText: "取消",
        onOk: () => applyVerdict({ role: option.value, label: `改判${option.label}` }),
      });
    },
    [applyVerdict, selectedIds.length],
  );

  const relationColumns: ColumnsType<RelationType> = useMemo(
    () => [
      {
        title: (
          <Checkbox
            checked={allSelected}
            indeterminate={someSelected}
            onChange={toggleSelectAll}
            onClick={(e) => e.stopPropagation()}
            aria-label="全选"
          />
        ),
        key: "select",
        width: 40,
        render: (_, row) => (
          <Checkbox
            checked={!excludedIds.includes(row.id)}
            onChange={() => activeGroup && toggleMember(activeGroup.key, row.id)}
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      {
        // 外键的最小可读单元是「源对象 —动词→ 目标对象」，不是一个动词——
        // 只列动词等于什么都没说。
        title: "关系",
        key: "triple",
        width: "26%",
        render: (_, row) => (
          <span className="review-obj-name">
            {row.source_object_name || "?"}
            <span className="review-triple-verb">— {row.display_name} →</span>
            {row.target_object_name || "?"}
          </span>
        ),
      },
      {
        title: <MachineMark bare label="判定类型" />,
        key: "structure",
        width: 104,
        render: (_, row) => (
          <span className="review-verdict-role review-verdict-role--bridge">
            {getRelationStructureLabel(row.structure_type)}
          </span>
        ),
      },
      {
        title: "把握",
        key: "confidence",
        width: 76,
        align: "right",
        sorter: (a, b) => (a.source_confidence ?? -1) - (b.source_confidence ?? -1),
        render: (_, row) => <ConfCell value={row.source_confidence} />,
      },
      {
        title: "判定依据",
        key: "basis",
        render: (_, row) => (
          <RelationBasisCell
            relation={row}
            flags={memberFlags.get(row.id) ?? []}
            domainId={domainId}
          />
        ),
      },
      {
        // 机器凭哪一列认定这条关系：不给这个，动词对不对就只能猜。
        title: "连接键",
        key: "join_key",
        width: 132,
        ellipsis: true,
        render: (_, row) => {
          const key = parseJoinKey(row.source_evidence || row.description);
          return key ? <span className="review-obj-sub">{key}</span> : <NumCell value={null} />;
        },
      },
    ],
    [excludedIds, activeGroup, toggleMember, memberFlags, domainId, allSelected, someSelected, toggleSelectAll],
  );

  /**
   * 判定结果、把握、依据全部进列——判据不在列表旁边，而**就是**列表。
   *
   * 原来它们分散在三处：组头一张机器判定卡（只说组内最低值）、右栏一整块判据（只说
   * 选中那一行）、中间一列光溜溜的数字。要比较两行的判定强弱，得逐行点过去看右栏。
   * 摊进列之后，一屏之内可以直接扫、直接排序，例外自己会跳出来。
   */
  const columns: ColumnsType<ObjectTypeSummary> = useMemo(
    () => [
      {
        title: (
          <Checkbox
            checked={allSelected}
            indeterminate={someSelected}
            onChange={toggleSelectAll}
            onClick={(e) => e.stopPropagation()}
            aria-label="全选"
          />
        ),
        key: "select",
        width: 40,
        render: (_, row) => (
          <Checkbox
            checked={!excludedIds.includes(row.id)}
            onChange={() => activeGroup && toggleMember(activeGroup.key, row.id)}
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      {
        title: "对象名",
        key: "name",
        width: "22%",
        render: (_, row) => (
          // 固定布局下长表名会被省略，title 让悬停仍能读到全名。
          <button
            type="button"
            className="review-obj-link"
            title={`${row.display_name}\n${row.name}\n点击查看完整档案`}
            onClick={(event) => {
              event.stopPropagation();
              openArchive(row.id);
            }}
          >
            <span className="review-obj-name">{row.display_name}</span>
            <span className="review-obj-sub">{row.name}</span>
          </button>
        ),
      },
      // 判定类型 / 把握 / 判定依据：这三列是机器说的，表头的芯片印记标明来源——
      // 一枚绿色的「业务对象」和人工敲定后的结果长得一模一样，不标就会被读成已定。
      {
        title: <MachineMark bare label="判定类型" />,
        key: "role",
        width: 104,
        render: (_, row) => {
          const meta = getRoleMeta(row.table_role);
          return (
            <span className={`review-verdict-role review-verdict-role--${meta.cls}`}>
              {meta.label}
            </span>
          );
        },
      },
      {
        title: "把握",
        key: "confidence",
        width: 76,
        align: "right",
        sorter: (a, b) => (a.role_confidence ?? -1) - (b.role_confidence ?? -1),
        render: (_, row) => <ConfCell value={row.role_confidence} />,
      },
      {
        title: "判定依据",
        key: "basis",
        render: (_, row) => <ObjectBasisCell obj={row} flags={memberFlags.get(row.id) ?? []} />,
      },
      // 两条最能一眼定性的结构信号留在列里（孤立的表、几乎没有描述性字段的表，
      // 基本不是业务对象）；主键/入度/属性/行数这些在「判定依据」里展开。
      {
        title: "图连通性",
        key: "connected",
        width: 92,
        sorter: (a, b) => Number(connectivity(a) ?? -1) - Number(connectivity(b) ?? -1),
        render: (_, row) => <ConnCell connected={connectivity(row)} />,
      },
      {
        title: "描述性字段占比",
        key: "descriptive_ratio",
        width: 122,
        align: "right",
        sorter: (a, b) =>
          (signal(a, "descriptive_ratio") ?? -1) - (signal(b, "descriptive_ratio") ?? -1),
        render: (_, row) => {
          const v = signal(row, "descriptive_ratio");
          return (
            <NumCell
              value={v == null ? null : `${Math.round(v * 100)}%`}
              flag={v != null && v < 0.4}
            />
          );
        },
      },
    ],
    [excludedIds, activeGroup, toggleMember, memberFlags, openArchive, allSelected, someSelected, toggleSelectAll],
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
  // 进度按当前范围的口径算，三者不混：对象页排除关系表，关系表只数 bridge。
  const roleSum = (m?: Record<string, number>, roles: string[] = OBJECT_SCOPE_ROLES) =>
    roles.reduce((sum, role) => sum + (m?.[role] ?? 0), 0);
  const objectScopeTotal = roleSum(stats.data?.total_by_role);
  const objectScopePending = roleSum(stats.data?.pending_by_role);
  const bridgeTotal = stats.data?.total_by_role?.bridge ?? 0;
  const bridgePending = stats.data?.pending_by_role?.bridge ?? 0;
  const relationPending = stats.data?.relation_needs_review_count ?? 0;
  const relationTotal = stats.data?.total_relations ?? 0;

  /** 一个板块在当前范围下的「已判/总数」。三种范围各读各的字段，不互相顶替。 */
  const segmentScope = (seg: SegmentReviewProgress) => {
    if (isRelation) return { total: seg.relation_total, pending: seg.relation_needs_review };
    if (isBridgeScope) {
      return {
        total: seg.role_total?.bridge ?? 0,
        pending: seg.role_pending?.bridge ?? 0,
      };
    }
    return {
      total: roleSum(seg.role_total),
      pending: roleSum(seg.role_pending),
    };
  };
  const unsegmentedScope = isRelation
    ? {
        total: stats.data?.unsegmented_relation_total ?? 0,
        pending: stats.data?.unsegmented_relation_pending ?? 0,
      }
    : { total: stats.data?.unsegmented_total ?? 0, pending: stats.data?.unsegmented_pending ?? 0 };

  // 「已确认、却还是业务对象压在系统表里」的那批：它们不在待判队列里，也进不了
  // 业务地图——不给一个入口，谁也不会再想起它们。
  const strandedCount = stats.data?.stranded_reviewed ?? 0;
  const systemSegmentId =
    stats.data?.segment_progress?.find((seg) => seg.kind === SYSTEM_SEGMENT_KIND)?.segment_id ??
    null;
  const total = isBridgeScope ? bridgeTotal : isRelation ? relationTotal : objectScopeTotal;
  const reviewed =
    total - (isBridgeScope ? bridgePending : isRelation ? relationPending : objectScopePending);
  const percent = total > 0 ? Math.round((reviewed / total) * 100) : 100;
  const activeMember = members.find((m) => m.id === activeMemberId) ?? members[0] ?? null;
  const activeObject = !isRelation ? (activeMember as ObjectTypeSummary | null) : null;
  // 「本组已判 N 个」由服务端在完整人口上分组后给出——前端拿 size 减 members 是算不出的，
  // 判过的成员根本不在队列载荷里。
  const confirmedInGroup = activeGroup?.reviewed_in_group ?? 0;

  return (
    <PageContainer full>
      <div className="review-workbench">
        <div className="review-topbar">
          <Space size={12} className="review-topbar-leading">
            <Link to={`/workspace/${domainId}`}>
              <Button icon={<ArrowLeftOutlined />} aria-label="返回工作区" />
            </Link>
            <span className="review-topbar-title">
              <AuditOutlined /> 审核 · {domain.data.name}
            </span>
            <Segmented
              // 顶层只有两个 tab；关系页里的「外键 / 关系表」由下面的次级切换给。
              value={relationScope ? "relation" : "object"}
              onChange={(value) => {
                setKind(value === "relation" ? "relation" : "object");
                setSegmentFilter("");
                setCursor("");
              }}
              options={[
                {
                  label: (
                    <span>
                      对象
                      <PendingSup count={objectScopePending} />
                    </span>
                  ),
                  value: "object",
                },
                {
                  label: (
                    <span>
                      关系
                      <PendingSup count={relationPending + bridgePending} />
                    </span>
                  ),
                  value: "relation",
                },
              ]}
            />
            {relationScope && (
              <Segmented
                size="small"
                value={kind}
                onChange={(value) => {
                  setKind(value as ReviewScope);
                  setSegmentFilter("");
                  setCursor("");
                }}
                // 已经在「关系」tab 里了，标签不必再复述「关系」二字——顶栏是零和的，
                // 多这两个字就会把整行挤到折行，白白吃掉 50px 的判据表高度。
                options={[
                  {
                    label: (
                      <span>
                        外键
                        <PendingSup count={relationPending} />
                      </span>
                    ),
                    value: "relation",
                  },
                  {
                    label: (
                      <span>
                        关系表
                        <PendingSup count={bridgePending} />
                      </span>
                    ),
                    value: "bridge",
                  },
                ]}
              />
            )}
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
              {isRelation ? "条" : isBridgeScope ? "张" : "个"} · 已判 {reviewed} / {total}
            </span>
          </div>
          <Space className="review-topbar-actions">
            {strandedCount > 0 && systemSegmentId && (
              <Tooltip title="这些对象的角色确认过，却还是业务对象压在「系统表」里：不属于任何业务模块，也就不会出现在业务地图上。点开逐组移出去。">
                <button
                  type="button"
                  className="review-strand-chip"
                  onClick={() => {
                    setKind("object");
                    setSegmentFilter(systemSegmentId);
                    setStatus("reviewed");
                    setCursor("");
                  }}
                >
                  系统表里的业务对象 <b>{strandedCount}</b>
                </button>
              </Tooltip>
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
            <div className="review-pane-label review-pane-label--with-action">
              <span>{relationScope ? "按板块筛选" : "队列"}</span>
              {ontologyId && (
                <Tooltip title="新建业务板块">
                  <Button
                    type="text"
                    size="small"
                    icon={<PlusOutlined />}
                    disabled={segmentSaving}
                    onClick={openCreateSegment}
                  >
                    新建板块
                  </Button>
                </Tooltip>
              )}
            </div>
            {/* 判完不等于看不见：同一批组、同一套 key，换的只是成员的那一半。
                判错了能退回重判，判完的板块能原样打开复查。 */}
            <Segmented
              block
              size="small"
              className="review-status-switch"
              value={status}
              onChange={(value) => {
                setStatus(value as ReviewStatus);
                setCursor("");
              }}
              options={[
                {
                  label: (
                    <span>
                      待判
                      <PendingSup count={queue.data?.pending_total ?? 0} />
                    </span>
                  ),
                  value: "pending",
                },
                { label: `已判 ${queue.data?.reviewed_total ?? 0}`, value: "reviewed" },
              ]}
            />
            <button
              type="button"
              className={`review-seg ${segmentFilter === "" ? "review-seg--on" : ""}`}
              onClick={() => {
                setSegmentFilter("");
                setCursor("");
              }}
            >
              <span className="review-seg-name">全部板块</span>
              <PendingSup count={total - reviewed} />
              <span className="review-seg-num">
                {reviewed}/{total}
              </span>
            </button>
            {(stats.data?.segment_progress ?? []).map((seg) => {
              const scoped = segmentScope(seg);
              const segment = segments.data?.items.find((item) => item.id === seg.segment_id);
              return (
                <div
                  className={`review-seg-entry ${
                    segmentFilter === seg.segment_id ? "review-seg-entry--on" : ""
                  } ${scoped.total > 0 && scoped.pending === 0 ? "review-seg-entry--done" : ""}`}
                  key={seg.segment_id}
                >
                  <button
                    type="button"
                    className={`review-seg ${
                      segmentFilter === seg.segment_id ? "review-seg--on" : ""
                    } ${scoped.total > 0 && scoped.pending === 0 ? "review-seg--done" : ""}`}
                    onClick={() => {
                      setSegmentFilter(seg.segment_id);
                      setCursor("");
                      // 判完的板块点进去不该是一片空白：直接给它已判的那一半。
                      if (scoped.total > 0 && scoped.pending === 0) setStatus("reviewed");
                    }}
                  >
                    <span className="review-seg-name" title={seg.segment_name}>
                      {scoped.total > 0 && scoped.pending === 0 ? "✓ " : ""}
                      {seg.segment_name}
                    </span>
                    <PendingSup count={scoped.pending} />
                    {/* 计数跟着当前范围走：对象页排除关系表，外键数边，关系表数
                        bridge 对象。此前关系页只显示板块名不给数字，是因为拿对象进度
                        顶替会读成假数字——现在后端按各自口径给，数字就可以给全。 */}
                    <span className="review-seg-num">
                      {scoped.total - scoped.pending}/{scoped.total}
                    </span>
                    <span className="review-seg-bar">
                      <i
                        style={{
                          width: `${
                            scoped.total > 0
                              ? Math.round(((scoped.total - scoped.pending) / scoped.total) * 100)
                              : 100
                          }%`,
                        }}
                      />
                    </span>
                  </button>
                  {segment && segment.kind !== "system" && (
                    <Dropdown
                      trigger={["click"]}
                      placement="bottomRight"
                      disabled={segmentSaving}
                      menu={{
                        items: [
                          {
                            key: "edit",
                            icon: <EditOutlined />,
                            label: "重命名板块",
                            onClick: () => openEditSegment(segment),
                          },
                          { type: "divider" as const },
                          {
                            key: "delete",
                            danger: true,
                            icon: <DeleteOutlined />,
                            label: "删除板块",
                            onClick: () => {
                              Modal.confirm({
                                title: `删除「${segment.display_name}」？`,
                                content:
                                  segment.member_count > 0
                                    ? `${segment.member_count} 个成员将重新分配到其他板块或系统表。`
                                    : "删除后不可在审核台继续使用该板块。",
                                okText: "删除",
                                cancelText: "取消",
                                okButtonProps: { danger: true, loading: segmentSaving },
                                onOk: () => removeSegment(segment),
                              });
                            },
                          },
                        ],
                      }}
                    >
                      <Button
                        type="text"
                        size="small"
                        className="review-seg-more"
                        icon={<MoreOutlined />}
                        aria-label={`板块操作 ${segment.display_name}`}
                        disabled={segmentSaving}
                        onClick={(event) => event.stopPropagation()}
                      />
                    </Dropdown>
                  )}
                </div>
              );
            })}
            {unsegmentedScope.total > 0 && <div className="review-seg-divider" />}
            {unsegmentedScope.total > 0 && (
              <button
                type="button"
                className={`review-seg ${segmentFilter === "-" ? "review-seg--on" : ""}`}
                onClick={() => {
                  setSegmentFilter("-");
                  setCursor("");
                }}
              >
                <span className="review-seg-name">未接入板块</span>
                <PendingSup count={unsegmentedScope.pending} />
                <span className="review-seg-num">
                  {unsegmentedScope.total - unsegmentedScope.pending}/{unsegmentedScope.total}
                </span>
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
                      : isReviewedView
                        ? "这个范围还没有判过的记录"
                        : pending === 0
                          ? `这个范围已经判完了${segmentFilter ? "，换一个板块继续" : ""}`
                          : "当前游标之后没有待判的组"
                  }
                />
                {cursor && !queue.loading && (
                  <Button onClick={() => setCursor("")}>回到队列开头</Button>
                )}
                {/* 判完的板块不该是一条死路：判了什么就在隔壁那半边。 */}
                {!queue.loading && !isReviewedView && (queue.data?.reviewed_total ?? 0) > 0 && (
                  <Button
                    onClick={() => {
                      setStatus("reviewed");
                      setCursor("");
                    }}
                  >
                    查看已判的 {queue.data?.reviewed_total} 个
                  </Button>
                )}
                {!queue.loading && isReviewedView && (
                  <Button
                    onClick={() => {
                      setStatus("pending");
                      setCursor("");
                    }}
                  >
                    回到待判队列
                  </Button>
                )}
              </div>
            ) : (
              <>
                {/* 组头收成一行：判成什么、多大把握、凭什么，现在全在下面的列里，
                    这里只留「这是哪一组、在队列的什么位置」。原来的机器判定卡说的是
                    组内**最低**把握，摊进列之后每行各说各的，那张卡就没有存在理由了。 */}
                <div className="review-group-head">
                  <div className="review-group-title">
                    {activeGroup.segment_name} · <em>{familyLabel(activeGroup.name_family)}</em> ·{" "}
                    {activeGroup.size} {isRelation ? "条" : "张"}
                    {isReviewedView && (
                      <Tag color="green" style={{ marginInlineStart: 8 }}>
                        回看已判 {members.length}
                      </Tag>
                    )}
                    {activeGroup.truncated && (
                      <Tooltip title="本组过大，先判这一批，剩下的下次进来继续">
                        <Tag style={{ marginInlineStart: 8 }}>仅显示前 {members.length} 个</Tag>
                      </Tooltip>
                    )}
                    {/* 这一组的角色判对了、位置没放对。整段解释收进悬停：常驻一整块
                        黄底说明读一次就够了，之后每屏都在吃判据表的高度。 */}
                    {isStranded && (
                      <Tooltip title="这批表判成了业务对象/关系表，却压在系统表里：机器按关系邻居和命名族都推不出它们属于哪个业务模块。用下面那行「移动到板块」移到对应的业务板块，它们才会出现在业务地图上；确实不是业务数据的，改判为数据表/技术表即可留在系统表。">
                        <Tag
                          color="warning"
                          icon={<PartitionOutlined />}
                          style={{ marginInlineStart: 8 }}
                        >
                          压在系统表
                        </Tag>
                      </Tooltip>
                    )}
                  </div>
                  <div className="review-group-aside">
                    {exceptionIds.length > 0 && (
                      <Tooltip title="这些成员带有本组其他成员没有的分歧/证据问题，已排在表首">
                        <span className="review-group-exception">{exceptionIds.length} 个例外</span>
                      </Tooltip>
                    )}
                    {confirmedInGroup > 0 && (
                      <span className="review-group-pos">本组已判 {confirmedInGroup}</span>
                    )}
                    <span className="review-group-pos">
                      {(queue.data?.group_offset ?? 0) + 1} / {queue.data?.group_total ?? 0} 组
                    </span>
                    {/* 用法说明读一次就够了，放判定条上是每屏都在占宽度——那一行的宽度
                        要留给真正在推进队列的动作。 */}
                    <Popover
                      placement="bottomRight"
                      content={
                        <div style={{ maxWidth: 320, lineHeight: 1.8 }}>
                          组内默认全选，反选掉例外后再判；判完自动进入下一组，误判按 ⌘Z 撤销。
                          「判定类型 / 把握 / 判定依据」是机器给的结论——悬停判定依据看完整
                          判据，点对象名开完整档案。主动作按 <kbd className="review-key">A</kbd>
                          ，跳过 <kbd className="review-key">S</kbd>，排除例外{" "}
                          <kbd className="review-key">X</kbd>，改判直接按{" "}
                          <kbd className="review-key">1</kbd>
                          <kbd className="review-key">2</kbd>
                          <kbd className="review-key">3</kbd>
                          <kbd className="review-key">4</kbd>。
                        </div>
                      }
                    >
                      <Button type="text" size="small" icon={<QuestionCircleOutlined />} />
                    </Popover>
                  </div>
                </div>

                <div className="review-group-body">
                  <Spin spinning={queue.loading}>
                    <Table
                      className="om-table review-group-table"
                      rowKey="id"
                      size="small"
                      columns={(isRelation ? relationColumns : columns) as ColumnsType<QueueMember>}
                      dataSource={orderedMembers as QueueMember[]}
                      pagination={false}
                      // 固定布局：对象名（尤其是 events_statements_summary_by_program
                      // 这类长表名）会把 auto 布局的表撑到 709px，中栏只有 574px，
                      // 于是「行数」和「机器存疑」被横向滚动推出视口——判据列看不见，
                      // 这个工作台就白做了。宽度由列定义决定，对象名溢出省略。
                      tableLayout="fixed"
                      rowClassName={(row) =>
                        [
                          row.id === activeMemberId ? "review-row--active" : "",
                          exceptionSet.has(row.id) ? "review-row--exception" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")
                      }
                      onRow={(row) => ({ onClick: () => setActiveMemberId(row.id) })}
                    />
                  </Spin>
                </div>

                {/* 判定条压成一行。
                    原来是三行：走流程的一行、常驻的「移动到」一行、四个改判按钮一行，
                    加起来吃掉近 150px——那是七八行判据表的高度，而这三行里真正每屏都
                    要用的只有「确认」。改判收进下拉（键位 1..4 不变），移动缩成一个
                    下拉加一个按钮，走流程的动作靠右。 */}
                <div className={`review-actions${isStranded ? " review-actions--urgent" : ""}`}>
                  <Tooltip title={!isReviewedView && isStranded ? STRAND_HINT : undefined}>
                    {/* 已判视图里主动作换成「退回复核」：回看的目的就是把判错的捞回来。
                        键位不变（A 永远是这一屏最该做的那件事）。 */}
                    <Button
                      type="primary"
                      danger={isReviewedView}
                      icon={isReviewedView ? <RollbackOutlined /> : undefined}
                      loading={applying}
                      disabled={applying}
                      onClick={runPrimary}
                    >
                      <kbd className="review-key">A</kbd>
                      {isReviewedView
                        ? `退回复核 ${selectedIds.length} 个`
                        : `确认这 ${selectedIds.length} 个`}
                    </Button>
                  </Tooltip>
                  {/* 紧挨确认，因为它是**同一批**的另一种处置：动词说不出业务语义时，
                      先把词改准再算复核，而不是原样确认下去。范围＝上面勾中的这些。 */}
                  {isRelation && (
                    <Tooltip title="给上面勾中的这批关系换上更精确的动词，逐条可改，采纳即计为已复核">
                      <Button
                        icon={<BulbOutlined />}
                        disabled={applying}
                        onClick={() => {
                          if (selectedIds.length === 0) {
                            message.warning("请先勾选要改动词的关系");
                            return;
                          }
                          setVerbDrawerOpen(true);
                        }}
                      >
                        动词建议
                      </Button>
                    </Tooltip>
                  )}
                  {!isRelation && (
                    <Dropdown
                      trigger={["click"]}
                      disabled={applying}
                      menu={{ items: recastItems, onClick: onRecast }}
                    >
                      <Button>
                        改判为 <DownOutlined />
                      </Button>
                    </Dropdown>
                  )}
                  {/* 移动到板块是常驻动作：分错板块的对象哪一组里都可能有，得随时能移走。
                      归错地方的那一组把它提成主按钮。 */}
                  {!isRelation && (
                    <span className="review-move">
                      <Select
                        showSearch
                        allowClear
                        optionFilterProp="label"
                        placeholder={isStranded ? "移到业务板块" : "移动到板块"}
                        style={{ width: 160 }}
                        value={moveTarget}
                        onChange={setMoveTarget}
                        options={moveTargets}
                        notFoundContent="本体里还没有其他板块"
                      />
                      {/* 图标按钮：动作名已经写在它左边的下拉占位符里（「移动到板块」），
                          再写一遍「移动」只是重复，而这一行的宽度是零和的。 */}
                      <Tooltip
                        title={`把勾中的 ${selectedIds.length} 个移到所选板块，并计为已复核`}
                      >
                        <Button
                          type={isStranded ? "primary" : "default"}
                          icon={<PartitionOutlined />}
                          aria-label="移动并确认"
                          disabled={applying || !moveTarget}
                          onClick={moveGroup}
                        />
                      </Tooltip>
                    </span>
                  )}
                  <span className="review-spacer" />
                  {/* 排除例外 / 跳过 / 说明小一号：它们不是每屏都要用的动作，
                      让位给确认、改判、移动这三个真正在推进队列的。 */}
                  {exceptionIds.length > 0 && (
                    <Tooltip title="把带有非共性分歧的成员剔出选择集，先把没争议的一次判掉；再按一次恢复全选">
                      <Button size="small" disabled={applying} onClick={toggleExceptions}>
                        <kbd className="review-key">X</kbd>
                        排除例外 {exceptionIds.length}
                      </Button>
                    </Tooltip>
                  )}
                  <Button size="small" disabled={applying} onClick={skipGroup}>
                    <kbd className="review-key">S</kbd>
                    跳过
                  </Button>
                </div>
              </>
            )}
          </section>
        </div>

        <ObjectArchiveDrawer
          objectId={activeObject?.id ?? null}
          open={archiveOpen}
          onClose={() => setArchiveOpen(false)}
          domainId={domainId}
        />

        {ontologyId && isRelation && (
          <VerbRefinementDrawer
            ontologyId={ontologyId}
            // 范围就是判定条上那批：抽屉里改的和刚才屏幕上看的必须是同一组关系。
            relationIds={selectedIds}
            open={verbDrawerOpen}
            onClose={() => setVerbDrawerOpen(false)}
            onApplied={refresh}
          />
        )}

        <Modal
          title={segmentEditor === "create" ? "新建业务板块" : "重命名业务板块"}
          open={segmentEditor !== null}
          onOk={() => void saveSegment()}
          onCancel={() => {
            if (!segmentSaving) {
              setSegmentEditor(null);
              setEditingSegment(null);
            }
          }}
          confirmLoading={segmentSaving}
          destroyOnClose
        >
          <Form form={segmentForm} layout="vertical">
            <Form.Item
              name="display_name"
              label="板块名称"
              rules={[{ required: true, whitespace: true, message: "请输入板块名称" }]}
            >
              <Input placeholder="例如：销售管理" maxLength={255} autoFocus />
            </Form.Item>
            <Form.Item
              name="name"
              label="技术标识名"
              extra={
                segmentEditor === "create" ? "可留空，系统会根据板块名称自动生成。" : undefined
              }
              rules={[{ whitespace: true, message: "技术标识名不能只包含空格" }]}
            >
              <Input placeholder="可选，例如 sales_management" maxLength={255} />
            </Form.Item>
            <Form.Item name="description" label="描述">
              <Input.TextArea rows={3} maxLength={1000} />
            </Form.Item>
          </Form>
        </Modal>
      </div>
    </PageContainer>
  );
}
