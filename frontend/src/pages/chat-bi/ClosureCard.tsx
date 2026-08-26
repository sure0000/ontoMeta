/**
 * 确认闭环卡：六环 + 本会话任务 + 悬挂项。
 *
 * 对话内总结块与决策追踪页抽屉共用同一个组件——两处画的是同一件事，各写一份的话，
 * 迟早出现「聊天里说 4/6、追踪页说 3/6」这种没人能解释的差异。
 *
 * **恒画六格、未走到的标灰而非隐藏**：「哪一环没走」正是管理要看的东西，
 * 把没走的藏起来就只剩一份自我表扬。
 *
 * **任务行是重新进入后三环的唯一入口**：方案/执行/结果三环在任务详情抽屉里确认，而抽屉
 * 此前只在「刚提交完那一下」弹出一次，制品 id 只活在组件的 useState 里——人不小心关掉
 * 窗口（或刷新页面），这条任务就在对话里彻底失联，剩下三环再也走不到。清单来自服务端
 * （chat_bi_conversation_tasks），故重新进入不依赖任何内存态。
 *
 * 六环本身仍是**只读聚合视图**：一个会话可能建了好几条任务，六环是它们的并集，点某一环
 * 无从知道要进的是哪一条；入口因此挂在任务行上，每行明确指向一条制品。
 */
import { Button } from "antd";
import type { ChatBiClosureTask, ChatBiDecisionClosure } from "../../types";
import { OUTCOME_LABEL } from "./decisionMeta";

const KIND_LABEL: Record<string, string> = {
  sync: "同步",
  transform: "加工",
  metric: "聚合",
  materialize: "物化",
};

/**
 * 任务行最多画几条。
 *
 * 一条会话反复重试同一件事是常态（真实数据里见过同一张表 7 条制品），全画出来会把一张
 * 总结卡撑成一份列表。截断本身不是问题，**不说**才是——超出部分在下面明写还剩几条，
 * 而不是悄悄消失让人以为只有这些。
 */
const MAX_TASK_ROWS = 5;

/**
 * 制品状态 → 该任务眼下停在哪一环、按钮该说什么。
 *
 * 与 `TaskRingSteps.ringIndexForArtifact` 同一套判据（drafted/validated 停在方案环、
 * confirmed/executing 在执行环、succeeded/failed 在结果环）。这里多给一句人话文案：
 * 「继续确认方案」比「查看」更能说清点下去要做什么。
 */
function taskAction(status?: string | null): { label: string; hint: string } {
  switch (status) {
    case "confirmed":
      return { label: "去执行", hint: "方案已确认，等待执行" };
    case "executing":
      return { label: "查看进度", hint: "执行中" };
    case "succeeded":
      return { label: "确认结果", hint: "已执行，等待结果确认" };
    case "failed":
      return { label: "查看失败原因", hint: "执行失败" };
    default:
      // drafted / validated / 读不到状态：都停在「确认执行方案」这一环。
      return { label: "继续确认方案", hint: "已生成执行方案，等待确认" };
  }
}

function TaskRow({
  task,
  onEnter,
}: {
  task: ChatBiClosureTask;
  onEnter: (artifactId: string) => void;
}) {
  const { label, hint } = taskAction(task.status);
  return (
    <div className="chatbi-closure-task">
      <div className="chatbi-closure-task-main">
        <span className="chatbi-closure-task-name">
          {task.kind ? `${KIND_LABEL[task.kind] ?? task.kind} ` : ""}
          {task.name}
        </span>
        <span className="chatbi-closure-task-hint">{hint}</span>
      </div>
      <Button size="small" type="link" onClick={() => onEnter(task.artifact_id)}>
        {label}
      </Button>
    </div>
  );
}

export function ClosureCard({
  closure,
  onEnterTask,
}: {
  closure: ChatBiDecisionClosure;
  /** 不传即纯只读（决策追踪页就是这样用的）。 */
  onEnterTask?: (artifactId: string) => void;
}) {
  if (!closure?.nodes?.length) return null;
  const tasks = closure.tasks ?? [];
  return (
    <div className="chatbi-closure">
      <div className="chatbi-closure-head">
        <span className="chatbi-closure-title">确认闭环</span>
        <span className="chatbi-closure-count">
          {closure.reached_count}/{closure.total_count} 环已确认
        </span>
      </div>
      <div className="chatbi-closure-nodes">
        {closure.nodes.map((n) => (
          <div
            key={n.node}
            className={`chatbi-closure-node${n.reached ? " chatbi-closure-node--on" : ""}`}
            title={n.summary ?? (n.reached ? "" : "尚未确认")}
          >
            <span className="chatbi-closure-node-label">{n.label}</span>
            <span className="chatbi-closure-node-state">
              {n.reached ? (OUTCOME_LABEL[n.latest_outcome ?? ""] ?? n.latest_outcome) : "未确认"}
            </span>
          </div>
        ))}
      </div>
      {onEnterTask && tasks.length > 0 && (
        <div className="chatbi-closure-tasks">
          {tasks.slice(0, MAX_TASK_ROWS).map((t) => (
            <TaskRow key={t.artifact_id} task={t} onEnter={onEnterTask} />
          ))}
          {tasks.length > MAX_TASK_ROWS && (
            <div className="chatbi-closure-task-more">
              另有 {tasks.length - MAX_TASK_ROWS} 条更早的任务，在「任务」页可见
            </div>
          )}
        </div>
      )}
      {closure.dangling.length > 0 && (
        <ul className="chatbi-closure-dangling">
          {closure.dangling.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
