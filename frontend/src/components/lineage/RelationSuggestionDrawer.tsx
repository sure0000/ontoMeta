import { CheckOutlined, CloseOutlined, CloudUploadOutlined } from "@ant-design/icons";
import { Alert, Button, Drawer, Empty, Popconfirm, Progress, Space, Tag, Tooltip } from "antd";
import type { RelationCandidate, RelationInferenceTask } from "../../types";

/**
 * 智能关系补充的建议层。
 *
 * 人在这里审的是**键族**，不是两两关系：一个族展开动辄几千对（实测人员编号族 3886 对），
 * 逐对审没人审得完，而判断「这 40 张表里的 bl_bh/ry_bh 是不是同一个人员编号」只需要一眼。
 * 所以确认/否决的粒度是整族，展开成对是确认之后的事。
 *
 * 三类判定分开呈现，因为处置完全不同：实体键要确认、码表多半也要、not_a_key 只是留个痕
 * （它连确认按钮都不给——没有可确认的关系）。
 */

interface Props {
  open: boolean;
  onClose: () => void;
  task: RelationInferenceTask | null;
  candidates: RelationCandidate[];
  deciding: string | null;
  onDecide: (candidate: RelationCandidate, state: "confirmed" | "rejected") => void;
  onCreateTask: () => void;
  onApply: () => void;
  applying: boolean;
  onRun: () => void;
  running: boolean;
}

const VERDICT_META: Record<string, { label: string; color: string; hint: string }> = {
  entity_key: {
    label: "实体键",
    color: "success",
    hint: "跨表复用的业务实体标识——这些表都在谈同一个实体",
  },
  dimension_code: {
    label: "码表键",
    color: "processing",
    hint: "分类/编码体系的取值，连接的是同一套码表而非同一个实体",
  },
  not_a_key: {
    label: "不是键",
    color: "default",
    hint: "枚举值、随机串等，不构成关联",
  },
};

const CARDINALITY_LABEL: Record<string, string> = {
  one_to_one: "1:1",
  one_to_many: "1:N",
  many_to_one: "N:1",
  many_to_many: "N:M",
};

function CandidateCard({
  candidate,
  deciding,
  onDecide,
  onCreateTask,
}: {
  candidate: RelationCandidate;
  deciding: string | null;
  onDecide: Props["onDecide"];
  onCreateTask: Props["onCreateTask"];
}) {
  const meta = VERDICT_META[candidate.verdict] ?? VERDICT_META.not_a_key;
  const decided =
    candidate.state === "confirmed" ||
    candidate.state === "rejected" ||
    candidate.state === "applied";
  const isKey = candidate.verdict !== "not_a_key";
  const hasAnchor = candidate.members.some((member) => member.near_unique);
  const busy = deciding === candidate.id;

  return (
    <div className={`lin-cand${candidate.state === "confirmed" ? " lin-cand--on" : ""}`}>
      <div className="lin-cand-head">
        <Tooltip title={meta.hint}>
          <Tag color={meta.color} variant="filled">
            {meta.label}
          </Tag>
        </Tooltip>
        <span className="lin-cand-title">
          {isKey ? (
            <>
              <b>{candidate.entity_name}</b>
              <span className="lin-cand-key">· {candidate.key_name}</span>
            </>
          ) : (
            <span className="lin-cand-key">{candidate.value_shape}</span>
          )}
        </span>
        <span className="lin-cand-conf" title="模型对该判定的把握">
          {Math.round(candidate.confidence * 100)}%
        </span>
      </div>

      <div className="lin-cand-meta">
        <code>{candidate.value_shape}</code>
        <i />
        {candidate.column_count} 列 / {candidate.table_count} 表
        {isKey && (
          <>
            <i />
            展开 {candidate.pair_count} 对
          </>
        )}
      </div>

      {candidate.sample_values.length > 0 && (
        <div className="lin-cand-samples">
          {candidate.sample_values.slice(0, 3).map((value) => (
            <code key={value}>{value}</code>
          ))}
        </div>
      )}

      {candidate.reason && <p className="lin-cand-reason">{candidate.reason}</p>}

      {isKey && !hasAnchor && (
        <Alert
          type="warning"
          showIcon
          message="本域没有该实体的主数据表"
          description="当前只能确认跨表关联；如需主表，请先发起建数任务。"
          action={
            <Button size="small" type="link" onClick={onCreateTask}>
              建数任务
            </Button>
          }
          className="lin-cand-no-anchor"
        />
      )}

      <div className="lin-cand-cols" title="该族涉及的列（按表）">
        {candidate.members.slice(0, 6).map((member) => (
          <span key={`${member.table}.${member.column}`}>
            {member.table}.<b>{member.column}</b>
            {member.near_unique && (
              <Tooltip title="本表内近似唯一——它是这个实体的候选主表">
                <Tag color="success">主表</Tag>
              </Tooltip>
            )}
          </span>
        ))}
        {candidate.members.length > 6 && (
          <span className="lin-cand-more">…另有 {candidate.members.length - 6} 列</span>
        )}
      </div>

      {candidate.pairs.length > 0 && (
        <div className="lin-cand-pairs">
          {candidate.pairs.slice(0, 3).map((pair) => (
            <span key={`${pair.source_table}.${pair.source_column}->${pair.target_table}`}>
              {pair.source_table} → {pair.target_table}
              <Tag>{CARDINALITY_LABEL[pair.cardinality] ?? pair.cardinality}</Tag>
            </span>
          ))}
        </div>
      )}

      <div className="lin-cand-acts">
        {decided ? (
          <Tag color={candidate.state === "confirmed" ? "success" : "default"} variant="filled">
            {candidate.state === "applied"
              ? "已写回 DataHub"
              : candidate.state === "confirmed"
                ? "已确认"
                : "已否决"}
            {candidate.decided_by ? ` · ${candidate.decided_by}` : ""}
          </Tag>
        ) : isKey ? (
          <>
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              loading={busy}
              onClick={() => onDecide(candidate, "confirmed")}
            >
              确认整族
            </Button>
            <Button
              size="small"
              icon={<CloseOutlined />}
              disabled={busy}
              onClick={() => onDecide(candidate, "rejected")}
            >
              否决
            </Button>
          </>
        ) : (
          <span className="lin-cand-note">判为「不是键」，无可确认的关系</span>
        )}
      </div>
    </div>
  );
}

export function RelationSuggestionDrawer({
  open,
  onClose,
  task,
  candidates,
  deciding,
  onDecide,
  onCreateTask,
  onApply,
  applying,
  onRun,
  running,
}: Props) {
  const grouped = {
    entity_key: candidates.filter((c) => c.verdict === "entity_key"),
    dimension_code: candidates.filter((c) => c.verdict === "dimension_code"),
    not_a_key: candidates.filter((c) => c.verdict === "not_a_key"),
  };
  const confirmed = candidates.filter((c) => c.state === "confirmed").length;

  return (
    <Drawer
      title="智能关系补充"
      placement="right"
      width={460}
      open={open}
      onClose={onClose}
      className="lin-cand-drawer"
      extra={
        <Space size={6}>
          {confirmed > 0 && (
            <Popconfirm
              title="将已确认键族写回 DataHub 外键元数据？"
              description="系统会先读取并合并现有 schemaMetadata，不会写入血缘图。"
              okText="确认写回"
              cancelText="取消"
              onConfirm={onApply}
            >
              <Button
                size="small"
                icon={<CloudUploadOutlined />}
                loading={applying}
                disabled={running}
              >
                写回外键
              </Button>
            </Popconfirm>
          )}
          <Button size="small" type="primary" loading={running} onClick={onRun}>
            {candidates.length > 0 ? "重新推断" : "开始推断"}
          </Button>
        </Space>
      }
    >
      <p className="lin-cand-intro">
        按<b>值形状</b>把域内字段聚成「键族」，再由 LLM 判定每族是不是真实体键。
        基数与方向由 distinct/rows 算出，不问模型。确认后的族会作为关联证据参与本体生成。
      </p>

      {running && task && (
        <div className="lin-cand-progress">
          <Progress percent={task.progress} size="small" status="active" />
          <span>{task.message ?? "进行中…"}</span>
        </div>
      )}

      {task?.status === "failed" && (
        <Alert
          type="error"
          showIcon
          message="推断失败"
          description={task.error_summary ?? task.message}
          style={{ marginBottom: 12 }}
        />
      )}

      {!running && candidates.length === 0 && (
        <Empty
          description={
            task?.status === "succeeded"
              ? "这个域没有聚出可用的键族"
              : "还没有推断过。点右上角开始。"
          }
        />
      )}

      {candidates.length > 0 && (
        <>
          <div className="lin-cand-summary">
            实体键 <b>{grouped.entity_key.length}</b> · 码表{" "}
            <b>{grouped.dimension_code.length}</b> · 非键{" "}
            <b>{grouped.not_a_key.length}</b>
            {confirmed > 0 && (
              <Tag color="success" variant="filled">
                已确认 {confirmed}
              </Tag>
            )}
          </div>

          {(
            [
              ["entity_key", "实体键"],
              ["dimension_code", "码表键"],
              ["not_a_key", "判为不是键"],
            ] as const
          ).map(([key, label]) =>
            grouped[key].length === 0 ? null : (
              <section key={key} className="lin-cand-section">
                <h4>
                  {label}
                  <span>{grouped[key].length}</span>
                </h4>
                {grouped[key].map((candidate) => (
                  <CandidateCard
                    key={candidate.id}
                    candidate={candidate}
                    deciding={deciding}
                    onDecide={onDecide}
                    onCreateTask={onCreateTask}
                  />
                ))}
              </section>
            ),
          )}
        </>
      )}
    </Drawer>
  );
}
