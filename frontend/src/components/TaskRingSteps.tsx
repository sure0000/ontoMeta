/**
 * 数据任务的**六环确认进度**：需求 → 本体 → 数据 → 执行方案 → 执行 → 结果。
 *
 * 一个任务从对话里的表单向导开始、在任务详情抽屉里走完，中途换了两处界面。此前两处
 * 各画各的（向导画三步、抽屉只有几个按钮），人填完表单会以为"建完了"，实际还差三环
 * 没确认。故进度条只有一份、跨两处共用：无论人在哪一处，看到的都是同一条「一共六环、
 * 现在第几环、还剩几环」。
 *
 * 六环取值与文案是后端契约（`models/chat_bi_ledger.py` 的 `NODE_SEQUENCE`）。
 * 表单会把带 `phase` 的六环随表单一起发下来；抽屉侧没有表单，用这里的默认定义。
 */
import { Steps } from "antd";

export type TaskRing = {
  node: string;
  title: string;
  description?: string;
  /** form = 对话内表单向导确认；artifact = 任务详情抽屉确认。 */
  phase?: string;
};

/** 六环默认定义。后端 `chat_bi_ledger.task_journey_steps()` 是同一份，改要一起改。 */
export const TASK_RINGS: TaskRing[] = [
  { node: "requirement", title: "确认任务需求", phase: "form" },
  { node: "ontology", title: "确认本体/口径", phase: "form" },
  { node: "data", title: "确认数据与参数", phase: "form" },
  { node: "plan", title: "确认执行方案", phase: "artifact" },
  { node: "execute", title: "执行任务", phase: "artifact" },
  { node: "result", title: "确认执行结果", phase: "artifact" },
];

export const TASK_RING_INDEX: Record<string, number> = Object.fromEntries(
  TASK_RINGS.map((r, i) => [r.node, i]),
);

/**
 * 本体环在四类任务里确认的东西不一样（物化确认范围、同步确认本体、加工确认对象、
 * 聚合确认口径），文案随之不同。与后端 `_TASK_CONFIRMATION_LABELS` 是同一份说法——
 * 表单里叫「确认物化范围」、抽屉里却叫「确认本体/口径」，人会以为是两件事。
 */
const ONTOLOGY_RING_TITLE: Record<string, string> = {
  materialize: "确认物化范围",
  sync: "确认同步本体",
  transform: "确认加工对象",
  metric: "确认业务口径",
};

export function ringsForKind(kind?: string): TaskRing[] {
  const title = ONTOLOGY_RING_TITLE[kind ?? ""];
  if (!title) return TASK_RINGS;
  return TASK_RINGS.map((ring) => (ring.node === "ontology" ? { ...ring, title } : ring));
}

/**
 * @param rings   六环定义（表单发下来的优先，缺省用 TASK_RINGS）
 * @param current 当前进行到的环序（0 起）
 * @param finished 全部走完（结果已反馈）——antd 的 current 无法表达"最后一环也完成了"
 */
export function TaskRingSteps({
  rings,
  current,
  finished = false,
  size = "small",
  labelPlacement = "horizontal",
}: {
  rings?: TaskRing[];
  current: number;
  finished?: boolean;
  size?: "small" | "default";
  /** 六个环名并排放不下时（对话里的表单只有 ~700px）改成竖排：名字落到序号下面。 */
  labelPlacement?: "horizontal" | "vertical";
}) {
  const items = (rings?.length ? rings : TASK_RINGS).map((ring) => ({
    key: ring.node,
    title: ring.title,
  }));
  // 间距走 class 不走 inline style：对话里的表单要在进度条下面再压一条分隔线，
  // inline style 的优先级会让那边改不动（见 chat-bi.css 的 .chatbi-form .task-ring-steps）。
  return (
    <Steps
      className="task-ring-steps"
      size={size}
      labelPlacement={labelPlacement}
      current={finished ? items.length : current}
      items={items}
    />
  );
}

/**
 * 制品状态 → 当前走到第几环。
 *
 * 「已执行但结果没反馈」必须停在结果环而不是显示走完——那正是闭环视图要盯的悬挂项，
 * 提前打勾就等于替人把最后一环签了。
 */
export function ringIndexForArtifact(
  status: string,
  resultConfirmed: boolean,
): { current: number; finished: boolean } {
  if (resultConfirmed) return { current: TASK_RING_INDEX.result, finished: true };
  if (status === "succeeded" || status === "failed") {
    return { current: TASK_RING_INDEX.result, finished: false };
  }
  if (status === "confirmed" || status === "executing") {
    return { current: TASK_RING_INDEX.execute, finished: false };
  }
  // drafted / validated：方案刚出来或还没出来，都停在「确认执行方案」这一环。
  return { current: TASK_RING_INDEX.plan, finished: false };
}
