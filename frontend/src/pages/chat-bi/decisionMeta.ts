/**
 * 决策留痕的展示元数据。
 *
 * 对话内总结块与独立追踪页看的是同一份数据，**六环的顺序与文案必须一模一样**——
 * 两处各写一份，早晚会出现「同一条记录在聊天里叫『执行方案确认』、在追踪页叫
 * 『方案确认』」这种让人怀疑是不是两条记录的分叉。故在此定义一份。
 *
 * 取值（node / outcome 的字符串）是后端契约，见 `models/chat_bi_ledger.py` 的
 * `DecisionNode` / `DecisionOutcome` 与 `NODE_SEQUENCE`。改名要两边一起改。
 */

/** 六环的固定顺序与中文名（须与后端 NODE_SEQUENCE 一致）。 */
export const NODE_SEQUENCE: Array<{ value: string; label: string }> = [
  { value: "requirement", label: "需求确认" },
  { value: "ontology", label: "本体确认" },
  { value: "data", label: "数据确认" },
  { value: "plan", label: "执行方案确认" },
  { value: "execute", label: "执行任务" },
  { value: "result", label: "结果确认" },
];

export const NODE_LABEL: Record<string, string> = {
  ...Object.fromEntries(NODE_SEQUENCE.map((n) => [n.value, n.label])),
  // 归一兜底：后端对未知 node 记成 other 而不是丢记录，展示上也得有个名字。
  other: "其他",
};

/** 表态结论的中文名。四态与后端 DecisionOutcome 一一对应。 */
export const OUTCOME_LABEL: Record<string, string> = {
  accepted: "已确认",
  modified: "已改后确认",
  rejected: "已否决",
  skipped: "已跳过",
};

/** antd Tag 色。modified 用 blue 而非 warning——改参数后确认是正常路径，不是异常。 */
export const OUTCOME_COLOR: Record<string, string> = {
  accepted: "green",
  modified: "blue",
  rejected: "red",
  skipped: "default",
};
