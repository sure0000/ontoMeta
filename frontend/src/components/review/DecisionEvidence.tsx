import { Descriptions, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import {
  describeSignals,
  getRoleMeta,
  orderSignalsForVerdict,
  parseDisagreement,
  parseRoleReason,
  reviewFlags,
  roleVerdict,
  ROLE_SCORE_THRESHOLD,
} from "../../utils/role";
import type { SignalDirection, SignalItem } from "../../utils/role";
import type { ObjectTypeSummary } from "../../types";
import { VerdictHeadline, WhyReview } from "./ReviewSignals";

const { Text } = Typography;

export function DirectionTag({ direction }: { direction: SignalDirection }) {
  if (direction === "business") return <Tag color="green">↑ 倾向业务对象</Tag>;
  if (direction === "nonbusiness") return <Tag color="orange">↓ 倾向非业务</Tag>;
  return <Tag>中性</Tag>;
}

/**
 * 紧凑版的方向标：只有一个箭头。
 *
 * 审核工作台原来把「倾向」整列砍掉了——省下了 150px，代价是复核者看到的是一串
 * 没有方向的数字（「描述性字段占比 83%」到底是加分还是减分？），判据栏于是只剩
 * 「可读」而不再「可判」。箭头只要 28px，方向就回来了。
 */
export function DirectionMark({ direction }: { direction: SignalDirection }) {
  const meta =
    direction === "business"
      ? { text: "↑", title: "倾向业务对象", cls: "up" }
      : direction === "nonbusiness"
        ? { text: "↓", title: "倾向非业务", cls: "down" }
        : { text: "·", title: "中性", cls: "flat" };
  return (
    <Tooltip title={meta.title}>
      <span className={`review-dir review-dir--${meta.cls}`}>{meta.text}</span>
    </Tooltip>
  );
}

export const EVIDENCE_COLUMNS: ColumnsType<SignalItem> = [
  { title: "信号", dataIndex: "label", key: "label" },
  { title: "观测值", dataIndex: "value", key: "value", width: 130 },
  {
    title: "倾向",
    key: "direction",
    width: 150,
    render: (_, r) => <DirectionTag direction={r.direction} />,
  },
];

/**
 * 紧凑信号表：不用 antd Table。
 *
 * 右栏只有 300 出头的宽度，Table 的表头、单元格内边距与边框在这个尺度上吃掉近四成
 * 空间，换回来的只有一根分割线。三列定义列表同样的信息占地少一半，行高也压得下来。
 * 反证排在最前（见 orderSignalsForVerdict）；无判别力的信号折成一行，需要时再展开。
 */
function SignalList({ items, role }: { items: SignalItem[]; role?: string | null }) {
  const [showMuted, setShowMuted] = useState(false);
  const ordered = orderSignalsForVerdict(items, role);
  const notable = ordered.filter((i) => i.notable);
  const muted = ordered.filter((i) => !i.notable);
  const rows = showMuted ? [...notable, ...muted] : notable;

  return (
    <div className="review-sig">
      {rows.map((item) => (
        <div
          key={item.key}
          className={`review-sig-row${item.notable ? "" : " review-sig-row--muted"}`}
        >
          <DirectionMark direction={item.direction} />
          <span className="review-sig-label">{item.label}</span>
          <span className="review-sig-value">{item.value}</span>
        </div>
      ))}
      {muted.length > 0 && (
        <button
          type="button"
          className="review-sig-more"
          onClick={() => setShowMuted((v) => !v)}
        >
          {showMuted ? "收起" : `另有 ${muted.length} 项信号无异常`}
        </button>
      )}
    </div>
  );
}

/**
 * 判定依据面板：把 object_classifier 的结构化证据（role_signals）与逐条理由
 * （role_reason）摊开展示，让复核者据证据快速确认或改判。
 *
 * 对象详情页与审核工作台共用同一份——判据只能有一套口径，两处各写一遍迟早分叉。
 * role_signals 为空（存量未重生成）时优雅降级为「判定说明」清单，功能不缺失。
 */
export function DecisionEvidencePanel({
  obj,
  compact = false,
}: {
  obj: ObjectTypeSummary;
  /** 紧凑版：给审核工作台的右栏用，省掉表头与描述块，只留信号与说明。 */
  compact?: boolean;
}) {
  const meta = getRoleMeta(obj.table_role);
  // 复核状态读 needs_review 列（后端的真源）。
  const needsReview = Boolean(obj.needs_review);
  // 板块名传进来，替掉容易误读的第一遍聚类簇规模（见 describeSignals）。
  const evidence = describeSignals(obj.role_signals, { segmentName: obj.segment_name });
  const hasSignals = evidence.items.length > 0;
  const flags = reviewFlags(obj);
  const reason = parseRoleReason(obj.role_reason);
  const disagreement = parseDisagreement(obj.role_reason);

  return (
    <>
      {!compact && (
        <Descriptions column={{ xs: 1, md: 3 }} size="small" style={{ marginBottom: 12 }}>
          <Descriptions.Item label="对象角色">
            <Tag color={meta.color}>{meta.label}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="角色置信度">
            {obj.role_confidence != null ? `${(obj.role_confidence * 100).toFixed(0)}%` : "-"}
          </Descriptions.Item>
          <Descriptions.Item label="复核状态">
            {needsReview ? <Tag color="gold">待复核</Tag> : <Tag color="green">已确认</Tag>}
          </Descriptions.Item>
        </Descriptions>
      )}

      {/* 判定与它的不确定性放最上面：先知道「机器判成什么、为什么心虚」，再看信号。 */}
      {compact && <VerdictHeadline verdict={roleVerdict(obj)} />}
      {needsReview && <WhyReview flags={flags} />}

      {/* LLM 读出的业务含义：全屏唯一一句「这张表到底是干什么的」。
          它此前混在判定说明的项目符号里，与流程记账并列——那是把最有用的一句
          降格成了第 N 条。现在提到最前，并且不分歧时也照样展示。 */}
      {reason.llmReading && (
        <div className="review-reading">
          <span className="review-reading-label">
            LLM 读表
            {reason.llmRole && <b>{reason.llmRole}</b>}
          </span>
          <p>{reason.llmReading}</p>
        </div>
      )}

      {disagreement && (
        <div className="review-split">
          <div className="review-split-side">
            <span className="review-split-who">LLM（读语义）</span>
            <b>{disagreement.llmRole || "—"}</b>
          </div>
          <div className="review-split-side">
            <span className="review-split-who">启发式（读结构）</span>
            <b>{disagreement.heuristicRole || "—"}</b>
          </div>
        </div>
      )}

      {!compact && evidence.score != null && (
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">综合得分 </Text>
          <Text strong>{evidence.score.toFixed(1)}</Text>
          <Text type="secondary"> （≥ {ROLE_SCORE_THRESHOLD.toFixed(1)} 判为业务对象）</Text>
        </div>
      )}

      {hasSignals &&
        (compact ? (
          <SignalList items={evidence.items} role={obj.table_role} />
        ) : (
          <Table
            className="om-table"
            size="small"
            rowKey="key"
            pagination={false}
            dataSource={orderSignalsForVerdict(evidence.items, obj.table_role)}
            columns={EVIDENCE_COLUMNS}
          />
        ))}

      {reason.heuristicClauses.length > 0 && (
        <div className="review-clauses">
          <span className="review-clauses-label">结构判据</span>
          <ul>
            {reason.heuristicClauses.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {!hasSignals && reason.heuristicClauses.length === 0 && !reason.llmReading && (
        <Text type="secondary">暂无判定证据（下次重新生成后可见结构化信号）。</Text>
      )}
    </>
  );
}
