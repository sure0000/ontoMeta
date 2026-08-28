import { Tag, Tooltip } from "antd";

import type { FieldProvenance } from "../types";

const ORIGIN_META: Record<string, { label: string; color: string }> = {
  machine: { label: "机器生成", color: "blue" },
  // 新建入口（人工建模 / 派生建模）写的是 `user`，编辑路径写的是 `manual`（见后端
  // edit._mark_overridden）。两个值都得认——漏一个，人刚建的对象会被标成「机器生成」。
  user: { label: "人工新建", color: "purple" },
  manual: { label: "人工新建", color: "purple" },
  machine_edited: { label: "人工修正", color: "cyan" },
};

interface Props {
  provenance?: FieldProvenance;
  /** 是否显示"机器生成"这类默认徽标；列表里可只在有修正/冲突时显示 */
  showMachine?: boolean;
}

/**
 * 字段级溯源徽标：直观区分机器生成 / 人工修正 / 上游有更新(冲突待复核)。
 */
export function ProvenanceBadge({ provenance, showMachine = false }: Props) {
  if (!provenance) return null;
  const { origin = "machine", has_conflict, upstream_removed, pinned_fields } = provenance;

  const tags = [];

  if (has_conflict) {
    const fields = provenance.conflicts ? Object.keys(provenance.conflicts) : [];
    tags.push(
      <Tooltip key="conflict" title={`上游有更新，与你的修改冲突：${fields.join("、")}`}>
        <Tag color="warning">上游有更新·待复核</Tag>
      </Tooltip>,
    );
  }

  if (upstream_removed) {
    tags.push(
      <Tooltip key="removed" title="上游已删除该项，因含人工内容而保留">
        <Tag color="error">上游已删除</Tag>
      </Tooltip>,
    );
  }

  const meta = ORIGIN_META[origin] ?? ORIGIN_META.machine;
  if (origin !== "machine" || showMachine) {
    const pinned = pinned_fields && pinned_fields.length > 0;
    tags.push(
      <Tooltip
        key="origin"
        title={pinned ? `人工已钉住字段：${pinned_fields!.join("、")}` : meta.label}
      >
        <Tag color={meta.color}>{meta.label}</Tag>
      </Tooltip>,
    );
  }

  if (tags.length === 0) return null;
  return <>{tags}</>;
}
