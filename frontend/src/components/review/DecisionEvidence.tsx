import { Descriptions, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  describeSignals,
  getRoleMeta,
  reasonClauses,
  ROLE_SCORE_THRESHOLD,
} from "../../utils/role";
import type { SignalDirection, SignalItem } from "../../utils/role";
import type { ObjectTypeSummary } from "../../types";

const { Text } = Typography;

export function DirectionTag({ direction }: { direction: SignalDirection }) {
  if (direction === "business") return <Tag color="green">↑ 倾向业务对象</Tag>;
  if (direction === "nonbusiness") return <Tag color="orange">↓ 倾向非业务</Tag>;
  return <Tag>中性</Tag>;
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
  const clauses = reasonClauses(obj.role_reason);
  const evidence = describeSignals(obj.role_signals);
  const hasSignals = evidence.items.length > 0;

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

      {evidence.score != null && (
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">综合得分 </Text>
          <Text strong>{evidence.score.toFixed(1)}</Text>
          <Text type="secondary"> （≥ {ROLE_SCORE_THRESHOLD.toFixed(1)} 判为业务对象）</Text>
        </div>
      )}

      {hasSignals && (
        <Table
          className="om-table"
          size="small"
          rowKey="key"
          pagination={false}
          dataSource={evidence.items}
          columns={
            compact
              ? EVIDENCE_COLUMNS.filter((col) => col.key !== "direction")
              : EVIDENCE_COLUMNS
          }
        />
      )}

      {clauses.length > 0 && (
        <div style={{ marginTop: hasSignals ? 14 : 0 }}>
          <Text type="secondary">判定说明</Text>
          <ul style={{ margin: "4px 0 0", paddingInlineStart: 18, lineHeight: 1.8 }}>
            {clauses.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {!hasSignals && clauses.length === 0 && (
        <Text type="secondary">暂无判定证据（下次重新生成后可见结构化信号）。</Text>
      )}
    </>
  );
}
