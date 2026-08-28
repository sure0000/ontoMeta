import { Descriptions, Tooltip } from "antd";

import type { LogicLanding, ObjectLanding } from "../types";
import type { Tone } from "./StatusBadge";

/**
 * 物理落点徽标：这个业务对象落到哪张物理表了。
 *
 * 任务产出的表**不是**新的业务对象，而是既有对象的物理投影。落点由后端从接入契约
 * 与仓库 Projection 聚合（`services/object_landing`），`state` 也在后端汇总——
 * 这里只负责把它翻译成一句人话，不重算判定。
 */

const LANDING_LABELS: Record<ObjectLanding["state"], string> = {
  not_landed: "未落地",
  registered: "待搬数",
  schema_ready: "表已建",
  syncing: "搬运中",
  landed: "已落地",
  stale: "待刷新",
  failed: "落地失败",
};

const LANDING_TONES: Record<ObjectLanding["state"], Tone> = {
  not_landed: "default",
  registered: "default",
  schema_ready: "blue",
  syncing: "processing",
  landed: "green",
  stale: "gold",
  failed: "red",
};

/** 落点状态的中文说法。选择器里要把「为什么不能选」写进选项文案，也用这一份。 */
export function landingStateLabel(state: ObjectLanding["state"] | string): string {
  return LANDING_LABELS[state as ObjectLanding["state"]] || state;
}

/** 对象落点与口径落点共用这枚徽标：状态取值同源，只是口径没有 ODS 那一段。 */
type AnyLanding = ObjectLanding | LogicLanding;

function tooltipText(landing: AnyLanding): string {
  const lines: string[] = [];
  const odsTable = "ods_table" in landing ? landing.ods_table : undefined;
  const odsMode = "ods_mode" in landing ? landing.ods_mode : undefined;
  if (odsTable) {
    lines.push(`ODS：${odsTable}${odsMode ? `（${odsMode}）` : ""}`);
  }
  if (landing.serving_table) {
    lines.push(`服务层：${landing.serving_table}`);
  }
  if (landing.last_success_at) {
    lines.push(`最近成功：${new Date(landing.last_success_at).toLocaleString()}`);
  }
  return lines.join("\n") || "尚无落点明细";
}

interface Props {
  landing?: AnyLanding;
  /** 未登记落点时是否显示灰色「未落地」。列表里默认不显示，避免整屏噪声。 */
  showWhenAbsent?: boolean;
}

export function LandingBadge({ landing, showWhenAbsent = false }: Props) {
  if (!landing) {
    if (!showWhenAbsent) return null;
    return (
      <span className="status-pill status-pill--default">
        <span className="status-pill-dot" />
        {LANDING_LABELS.not_landed}
      </span>
    );
  }
  return (
    <Tooltip title={<span style={{ whiteSpace: "pre-line" }}>{tooltipText(landing)}</span>}>
      <span>
        <LandingStateBadge state={landing.state} />
      </span>
    </Tooltip>
  );
}

/**
 * 落点状态的裸徽标。**中文说法只此一处**：数据集目录与对象卡片说的是同一套状态，
 * 各写一份就会出现「已落地」与「可用」两种叫法指同一件事。
 */
export function LandingStateBadge({ state }: { state: ObjectLanding["state"] }) {
  return (
    <span className={`status-pill status-pill--${LANDING_TONES[state] || "default"}`}>
      <span className="status-pill-dot" />
      {LANDING_LABELS[state] || state}
    </span>
  );
}

interface PanelProps {
  landing?: ObjectLanding;
}

/**
 * 对象详情里的「物理落点」块：这个对象被哪些任务落成了物理表。
 *
 * 没有落点时给一句明确的话而不是空白——「还没物化/同步」是有效信息，不是缺数据。
 */
export function ObjectLandingPanel({ landing }: PanelProps) {
  if (!landing) {
    return (
      <Descriptions column={1} size="small" title="物理落点">
        <Descriptions.Item label="状态">
          <span style={{ color: "var(--om-text-muted, #999)" }}>
            尚未落地：该对象还没有被物化或同步任务落成物理表。
          </span>
        </Descriptions.Item>
      </Descriptions>
    );
  }
  return (
    <Descriptions column={{ xs: 1, md: 2, xl: 4 }} size="small" title="物理落点">
      <Descriptions.Item label="状态">
        <LandingBadge landing={landing} />
      </Descriptions.Item>
      <Descriptions.Item label="ODS 表">
        {landing.ods_table || "-"}
        {landing.ods_mode ? `（${landing.ods_mode}）` : ""}
      </Descriptions.Item>
      <Descriptions.Item label="服务层表">
        {landing.serving_table || "-"}
        {landing.serving_layer ? `（${landing.serving_layer}）` : ""}
      </Descriptions.Item>
      <Descriptions.Item label="最近成功">
        {landing.last_success_at ? new Date(landing.last_success_at).toLocaleString() : "-"}
      </Descriptions.Item>
    </Descriptions>
  );
}
