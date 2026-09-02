import {
  ArrowLeftOutlined,
  AuditOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  PartitionOutlined,
  QuestionCircleOutlined,
  RollbackOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Popover,
  Progress,
  Segmented,
  Select,
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
  PageResult,
  RelationType,
  ReviewGroup,
  ReviewModeStats,
  ReviewQueue,
  SegmentReviewProgress,
  SegmentSummary,
} from "../types";
import { reviewFlags, riskRank, roleVerdict, ROLE_OPTIONS, summarizeFlags } from "../utils/role";
import type { ReviewFlag } from "../utils/role";
import { getRelationStructureLabel, parseJoinKey, relationReviewFlags } from "../utils/relation";
import { VerbRefinementDrawer } from "../components/review/VerbRefinementDrawer";
import { ObjectArchiveDrawer } from "../components/review/ObjectArchiveDrawer";
import {
  FlagChips,
  MachineMark,
  MachineVerdict,
  VerdictHeadline,
  WhyReview,
} from "../components/review/ReviewSignals";

const { Text } = Typography;

/** 队列成员：对象与关系共用选择/判定逻辑，那部分只认 id 与 needs_review。 */
type QueueMember = ObjectTypeSummary | RelationType;

/** 审核范围：两个顶层 tab（对象 / 关系），关系页内再分三元组与关系表。 */
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

/** 「待归类业务对象」板块的 kind（后端 services/segment_kinds）。 */
const PENDING_SEGMENT_KIND = "pending";
const CLASSIFY_HINT = "「待归类业务对象」要先归入一个业务板块才算判完";

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
  // 三种审核范围，两个顶层 tab：
  //   object  → 对象页：业务对象/数据表/技术表
  //   relation→ 关系页 · 关系三元组（RelationType）
  //   bridge  → 关系页 · 关系表（table_role=bridge 的**对象**）
  //
  // 关系表是「表」不是「边」，数据上是 ObjectType；但复核者面对它时问的是关系的问题
  // （这张表是不是只是把 A 和 B 连起来），所以归到关系页。它此前混在对象页的
  // 「待归类业务对象」里，和真正的业务对象抢同一屏，两种判断标准打架。
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

  // 归类的目的地：只给业务板块与公共主数据。技术表/系统表那两个兜底板块不是「归类」，
  // 想去那边应该改判角色（改判会把对象自动落到对应的兜底板块）。
  const segments = useApi<PageResult<SegmentSummary> | null>(
    () => (ontologyId ? api.listSegments({ ontologyId, limit: 200 }) : Promise.resolve(null)),
    [ontologyId],
  );
  const classifyTargets = useMemo(
    () =>
      (segments.data?.items ?? [])
        .filter((seg) => seg.kind === "business" || seg.kind === "shared")
        .sort((a, b) => b.member_count - a.member_count)
        .map((seg) => ({
          value: seg.id,
          label: `${seg.display_name}（${seg.member_count}）`,
        })),
    [segments.data],
  );
  const [classifyTarget, setClassifyTarget] = useState<string | undefined>();

  const groups = useMemo(() => queue.data?.groups ?? [], [queue.data]);
  const activeGroup: ReviewGroup | null = groups[0] ?? null;
  // isRelation 只指「关系三元组」这条队列：关系表虽然挂在关系页，但它是对象，
  // 列、判定接口、改判按钮全走对象那一套——尤其是改判，误判的关系表要能调回业务对象。
  const isRelation = kind === "relation";
  const isBridgeScope = kind === "bridge";
  /** 侧栏与顶栏的计数口径跟着范围走，三者各算各的。 */
  const relationScope = kind !== "object";
  const isReviewedView = status === "reviewed";
  // 「待归类业务对象」那一组：判成业务对象却连不成簇，只确认角色它仍然进不了任何
  // 业务地图，那个板块也永远不会变空。所以确认按钮在这里是禁用的，出路是归类或改判。
  // 判定由服务端给（同一条规则在后端 segment_placement 写一次），前端不重算。
  const needsClassification = Boolean(activeGroup?.requires_classification);
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

  /**
   * 组头的判定用**组内最低**的把握与得分：能不能整组确认，取决于最弱的那一条。
   * 角色本身是分组键的一部分，全组必然相同，所以只有把握/得分需要取值。
   */
  const { groupVerdict, groupVerdictNote } = useMemo(() => {
    const objects = isRelation ? [] : (members as ObjectTypeSummary[]);
    const confidences = objects
      .map((m) => m.role_confidence)
      .filter((v): v is number => typeof v === "number");
    const scores = objects
      .map((m) => (typeof m.role_signals?.score === "number" ? m.role_signals.score : null))
      .filter((v): v is number => v != null);
    const verdict = roleVerdict({
      table_role: activeGroup?.table_role,
      role_confidence: confidences.length ? Math.min(...confidences) : undefined,
      role_signals: scores.length ? { score: Math.min(...scores) } : undefined,
    });
    // 取了最低值就得说是最低值：组内 10 张 95% 配 1 张 60%，只写「60%」会读成全组都虚。
    const spans: string[] = [];
    if (confidences.length && Math.min(...confidences) !== Math.max(...confidences)) {
      spans.push(
        `把握 ${Math.round(Math.min(...confidences) * 100)}–${Math.round(
          Math.max(...confidences) * 100,
        )}%`,
      );
    }
    if (scores.length && Math.min(...scores) !== Math.max(...scores)) {
      spans.push(`得分 ${Math.min(...scores).toFixed(1)}–${Math.max(...scores).toFixed(1)}`);
    }
    return {
      groupVerdict: verdict,
      groupVerdictNote: spans.length ? `组内${spans.join("、")}，这里显示的是最低值` : undefined,
    };
  }, [activeGroup, members, isRelation]);

  // 当前组换了就把焦点挪到最该看的那条——右栏判据永远指着某一行。
  useEffect(() => {
    setActiveMemberId(orderedMembers[0]?.id ?? null);
  }, [activeGroup?.key, orderedMembers]);

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

  /**
   * 全组同旗时「机器存疑」列只剩破折号，宽度还给对象名/三元组——
   * 中栏的宽度是零和的，一列 200px 只用来显示「跟组头说的一样」不值。
   */
  const flagColumnWidth = useMemo(
    () =>
      members.some((m) =>
        (memberFlags.get(m.id) ?? []).some((f) => !flagSummary.commonKeys.has(f.key)),
      )
        ? 150
        : 64,
    [members, memberFlags, flagSummary],
  );

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
        setExcluded((prev) => ({ ...prev, [activeGroup.key]: [] }));
        // 报服务端实际改了几条：已经是目标状态的不计数，别让人以为多判了。
        // 改判成业务对象却连不成簇的那批仍待归类——不说清楚，看起来就像整组判完了。
        const stranded = "pending_classification" in result ? (result.pending_classification ?? 0) : 0;
        const suffix = stranded > 0 ? `，其中 ${stranded} 个仍待归类` : "";
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
   * 一个键位对应「这一屏最该做的那件事」，两个视图各自只有一件。待归类那一组两件都
   * 不是——它缺的是归属，所以这里挡住并说清楚，而不是让人按了没反应。
   */
  const runPrimary = useCallback(() => {
    if (isReviewedView) {
      void applyVerdict({ label: "退回复核", review: true });
      return;
    }
    if (needsClassification) {
      message.warning(`${CLASSIFY_HINT}，或改判为数据表/技术表`);
      return;
    }
    void applyVerdict({ label: "确认", review: false });
  }, [applyVerdict, isReviewedView, needsClassification]);

  /** 归类并确认：挪板块与判复核是同一次请求，后端因此看得到挪过之后的归属。 */
  const classifyGroup = useCallback(() => {
    if (!classifyTarget) {
      message.warning("先选一个业务板块");
      return;
    }
    const name =
      classifyTargets.find((opt) => opt.value === classifyTarget)?.label ?? "所选板块";
    void applyVerdict({ segmentId: classifyTarget, review: false, label: `归入 ${name}` });
  }, [applyVerdict, classifyTarget, classifyTargets]);

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
        // 机器凭哪一列认定这条关系：不给这个，动词对不对就只能猜。
        title: "连接键",
        key: "join_key",
        width: 116,
        ellipsis: true,
        render: (_, row) => {
          const key = parseJoinKey(row.source_evidence || row.description);
          return key ? <span className="review-obj-sub">{key}</span> : <NumCell value={null} />;
        },
      },
      {
        title: <MachineMark bare label="机器存疑" />,
        key: "flags",
        width: flagColumnWidth,
        render: (_, row) => (
          <FlagChips flags={memberFlags.get(row.id) ?? []} hiddenKeys={flagSummary.commonKeys} />
        ),
      },
      {
        title: "置信度",
        key: "confidence",
        width: 72,
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
    [excludedIds, activeGroup, toggleMember, memberFlags, flagSummary, flagColumnWidth],
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
          // 固定布局下长表名会被省略，title 让悬停仍能读到全名
          <div title={`${row.display_name}\n${row.name}`}>
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
        width: 54,
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
        width: 54,
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
        width: 54,
        align: "right",
        sorter: (a, b) => a.property_count - b.property_count,
        render: (_, row) => <NumCell value={row.property_count} flag={row.property_count <= 2} />,
      },
      {
        title: "行数",
        key: "row_count",
        width: 68,
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
        // 判据里最贵的一列：数字告诉你这张表长什么样，这一列告诉你**机器自己哪里没底**。
        // 全组共有的旗标压暗（组头已经讲过），只有跟大家不一样的那条会亮起来。
        title: <MachineMark bare label="机器存疑" />,
        key: "flags",
        width: flagColumnWidth,
        render: (_, row) => (
          <FlagChips flags={memberFlags.get(row.id) ?? []} hiddenKeys={flagSummary.commonKeys} />
        ),
      },
    ],
    [excludedIds, activeGroup, toggleMember, memberFlags, flagSummary, flagColumnWidth],
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

  // 「已确认却仍压在待归类里」的存量：门禁上线前判过角色的那批。它们既不在待判队列里，
  // 也进不了业务地图——不给一个入口，谁也不会再想起它们。
  const strandedCount = stats.data?.unclassified_reviewed ?? 0;
  const pendingSegmentId =
    stats.data?.segment_progress?.find((seg) => seg.kind === PENDING_SEGMENT_KIND)?.segment_id ??
    null;
  const total = isBridgeScope ? bridgeTotal : isRelation ? relationTotal : objectScopeTotal;
  const reviewed =
    total - (isBridgeScope ? bridgePending : isRelation ? relationPending : objectScopePending);
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
              // 顶层只有两个 tab；关系页里的「三元组 / 关系表」由下面的次级切换给。
              value={relationScope ? "relation" : "object"}
              onChange={(value) => {
                setKind(value === "relation" ? "relation" : "object");
                setSegmentFilter("");
                setCursor("");
              }}
              options={[
                { label: `对象 ${objectScopePending}`, value: "object" },
                { label: `关系 ${relationPending + bridgePending}`, value: "relation" },
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
                  { label: `三元组 ${relationPending}`, value: "relation" },
                  { label: `关系表 ${bridgePending}`, value: "bridge" },
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
          <Space>
            {strandedCount > 0 && pendingSegmentId && (
              <Tooltip title="这些对象的角色确认过，却仍留在「待归类业务对象」里：不属于任何业务模块，也就不会出现在业务地图上。点开逐组归位。">
                <button
                  type="button"
                  className="review-strand-chip"
                  onClick={() => {
                    setKind("object");
                    setSegmentFilter(pendingSegmentId);
                    setStatus("reviewed");
                    setCursor("");
                  }}
                >
                  待归类未归位 <b>{strandedCount}</b>
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
            <div className="review-pane-label">{relationScope ? "按板块筛选" : "队列"}</div>
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
                { label: `待判 ${queue.data?.pending_total ?? 0}`, value: "pending" },
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
              <span>全部板块</span>
              <span className="review-seg-num">{total - reviewed}</span>
            </button>
            {(stats.data?.segment_progress ?? []).map((seg) => {
              const scoped = segmentScope(seg);
              return (
                <button
                  type="button"
                  key={seg.segment_id}
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
                  {/* 计数跟着当前范围走：对象页排除关系表，关系三元组数边，关系表数
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
                <div className="review-group-head">
                  <div style={{ minWidth: 0, flex: 1 }}>
                    {/* 机器判定是这块屏幕的第一句话：先知道机器判成了什么，再决定认不认。 */}
                    {isRelation ? (
                      <MachineVerdict
                        size="lg"
                        hint={isReviewedView ? "已确认" : undefined}
                        ariaLabel={`机器判定：${getRelationStructureLabel(
                          activeGroup.table_role,
                        )}，${isReviewedView ? "已人工确认" : "待人工确认"}`}
                      >
                        <span className="review-verdict-role review-verdict-role--bridge">
                          {getRelationStructureLabel(activeGroup.table_role)}
                        </span>
                        <span
                          className={`review-verdict-band review-verdict-band--${activeGroup.score_band}`}
                        >
                          {activeGroup.score_band_label}
                        </span>
                      </MachineVerdict>
                    ) : (
                      // 分带标签已经跟在得分刻度后面了（分带是分组键的一部分，全组同值），
                      // 再挂一个同名 Tag 只会跟「机器判定」抢注意力。
                      <div className="review-verdict-line">
                        <VerdictHeadline
                          verdict={groupVerdict}
                          size="lg"
                          note={groupVerdictNote}
                          reviewed={isReviewedView}
                        />
                      </div>
                    )}
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
                    </div>
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
                  <div className="review-group-aside">
                    <span className="review-group-pos">
                      {(queue.data?.group_offset ?? 0) + 1} / {queue.data?.group_total ?? 0} 组
                    </span>
                  </div>
                </div>

                {/* 这一组为什么整体在队列里：共性一次讲清，例外单独点名。 */}
                <div
                  className={`review-group-why${
                    exceptionIds.length > 0 ? " review-group-why--exception" : ""
                  }`}
                >
                  {flagSummary.common.length > 0 ? (
                    <span className="review-group-why-main">
                      <MachineMark bare label="全组共性" />
                      {flagSummary.common.slice(0, 3).map((entry) => (
                        <Tooltip key={entry.flag.key} title={entry.flag.detail}>
                          <span className={`review-flag review-flag--${entry.flag.tone}`}>
                            {entry.flag.label}
                            <i>
                              {entry.count}/{flagSummary.size}
                            </i>
                          </span>
                        </Tooltip>
                      ))}
                    </span>
                  ) : (
                    <span className="review-group-why-main">
                      <MachineMark bare label="本组无共性存疑" />
                      <span className="review-group-why-hint">逐行看「机器存疑」列</span>
                    </span>
                  )}
                  {exceptionIds.length > 0 ? (
                    <Tooltip title="这些成员带有本组其他成员没有的分歧/证据问题，已排在表首">
                      <span className="review-group-why-exception">
                        {exceptionIds.length} 个例外需单独看
                      </span>
                    </Tooltip>
                  ) : (
                    <span className="review-group-why-clean">无例外，可整组处置</span>
                  )}
                </div>

                {/* 这一组缺的不是「角色对不对」，是「归到哪个业务模块」——把缺的那一半说出来，
                    否则界面上只剩一个被禁掉的确认按钮，读起来像是坏了。 */}
                {needsClassification && (
                  <div className="review-classify-note">
                    <PartitionOutlined />
                    <span>
                      这批表判成了业务对象，却在关系图上连不成簇。<b>确认角色不算判完</b>
                      ：归入一个业务板块，它们才会出现在业务地图上；确实不属于任何模块的，
                      改判为数据表/技术表。
                    </span>
                  </div>
                )}

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
                          excludedIds.includes(row.id) ? "review-row--excluded" : "",
                          exceptionSet.has(row.id) ? "review-row--exception" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")
                      }
                      onRow={(row) => ({ onClick: () => setActiveMemberId(row.id) })}
                    />
                  </Spin>
                </div>

                {/* 两行是**排定**的，不是挤出来的：第一行「确认 / 排除 / 跳过」是走流程，
                    第二行「改判为 …」是改结论。挤在一行里会随宽度乱折，且两类动作混在一起。 */}
                <div className="review-actions">
                  <div className="review-actions-row">
                    <Tooltip
                      title={
                        !isReviewedView && needsClassification
                          ? `${CLASSIFY_HINT}——用下面那行归类，或改判为数据表/技术表`
                          : undefined
                      }
                    >
                      {/* 已判视图里主动作换成「退回复核」：回看的目的就是把判错的捞回来。
                          键位不变（A 永远是这一屏最该做的那件事）。 */}
                      <Button
                        type="primary"
                        size="large"
                        danger={isReviewedView}
                        icon={isReviewedView ? <RollbackOutlined /> : undefined}
                        loading={applying}
                        disabled={
                          selectedIds.length === 0 || (!isReviewedView && needsClassification)
                        }
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
                          size="large"
                          icon={<BulbOutlined />}
                          disabled={applying || selectedIds.length === 0}
                          onClick={() => setVerbDrawerOpen(true)}
                        >
                          动词建议
                        </Button>
                      </Tooltip>
                    )}
                    <span className="review-spacer" />
                    {exceptionIds.length > 0 && (
                      <Tooltip title="把带有非共性分歧的成员剔出选择集，先把没争议的一次判掉；再按一次恢复全选">
                        <Button disabled={applying} onClick={toggleExceptions}>
                          <kbd className="review-key">X</kbd>
                          排除 {exceptionIds.length} 个例外
                        </Button>
                      </Tooltip>
                    )}
                    <Button disabled={applying} onClick={skipGroup}>
                      <kbd className="review-key">S</kbd>
                      跳过本组
                    </Button>
                    {/* 常驻两行操作说明读一次就够了，之后每一屏都在占位。收进气泡，
                        把那 40px 还给判据表。 */}
                    <Popover
                      placement="topRight"
                      content={
                        <div style={{ maxWidth: 300, lineHeight: 1.7 }}>
                          组内默认全选，反选掉例外后再判；判完自动进入下一组，误判按 ⌘Z 撤销。
                          「机器存疑」列里压暗的是全组共性，亮起来的才是这一行独有的问题。
                        </div>
                      }
                    >
                      <Button type="text" size="small" icon={<QuestionCircleOutlined />} />
                    </Popover>
                  </div>
                  {needsClassification && (
                    <div className="review-actions-row review-actions-row--classify">
                      <span className="review-actions-label">归类到</span>
                      <Select
                        size="small"
                        showSearch
                        optionFilterProp="label"
                        placeholder="选择业务板块"
                        style={{ minWidth: 210 }}
                        value={classifyTarget}
                        onChange={setClassifyTarget}
                        options={classifyTargets}
                        notFoundContent="本体里还没有业务板块"
                      />
                      <Button
                        type="primary"
                        size="small"
                        icon={<PartitionOutlined />}
                        disabled={applying || selectedIds.length === 0 || !classifyTarget}
                        onClick={classifyGroup}
                      >
                        归类并确认 {selectedIds.length} 个
                      </Button>
                    </div>
                  )}
                  {!isRelation && (
                    <div className="review-actions-row review-actions-row--recast">
                      <span className="review-actions-label">改判为</span>
                      {/* 键位是**位置固定**的（1..4 直连 VERDICTS），所以先带上原下标再过滤：
                          关系表页里「改判关系表」是空操作，去掉，但 1/2/4 的键位不能跟着挪。
                          留下的正是「误判的关系表调回业务对象」这条路。 */}
                      {ROLE_OPTIONS.map((option, index) => ({ option, index }))
                        .filter(({ option }) => !(isBridgeScope && option.value === "bridge"))
                        .map(({ option, index }) => (
                          <Button
                            key={option.value}
                            size="small"
                            disabled={applying || selectedIds.length === 0}
                            onClick={() =>
                              void applyVerdict({
                                role: option.value,
                                label: `改判${option.label}`,
                              })
                            }
                          >
                            <kbd className="review-key">{index + 1}</kbd>
                            {option.label}
                          </Button>
                        ))}
                    </div>
                  )}
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
                {/* 邻居从表格让位到这里：表格里 118px 只放得下两个被截断的名字，
                    这一栏能把方向、对象、关系动词一次说全。 */}
                {activeObject.top_neighbors && activeObject.top_neighbors.length > 0 && (
                  <div className="review-neighbors">
                    <span className="review-neighbors-label">邻居</span>
                    {activeObject.top_neighbors.slice(0, 4).map((n) => (
                      <Tooltip key={n.id} title={n.relation_name}>
                        <span className="review-neighbor">
                          {n.direction === "inbound" ? "←" : "→"} {n.display_name || n.name}
                        </span>
                      </Tooltip>
                    ))}
                  </div>
                )}
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
                <WhyReview flags={memberFlags.get(activeRelation.id) ?? []} />
                <div className="review-evidence-row">
                  <span>连接键</span>
                  <span>
                    {parseJoinKey(activeRelation.source_evidence || activeRelation.description) ||
                      "—"}
                  </span>
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
      </div>
    </PageContainer>
  );
}
