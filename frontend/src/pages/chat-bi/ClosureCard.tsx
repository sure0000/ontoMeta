/**
 * 确认闭环卡：**一条数据任务一张**，画这条任务自己的六环 + 它的悬挂项。
 *
 * 对话内总结块与决策追踪页抽屉共用同一个组件——两处画的是同一件事，各写一份的话，
 * 迟早出现「聊天里说 4/6、追踪页说 3/6」这种没人能解释的差异。
 *
 * **闭环的粒度是任务，不是会话**。此前是会话级的一组六环，两处都错：
 * ① 随口问一句数、在结果上点个「认可」就点亮了 data 环，于是一次纯查询也顶着一张
 *   六环卡——那次查询根本没有要闭的环，卡片只是在自说自话；
 * ② 一条会话连着建三条任务，三份确认被并成一组六环，"哪一环是给哪条任务走的"从图上
 *   读不出来，点某一环也无从知道该进哪条任务。
 * 故：没有任务就一张卡都不画（`ClosureCards` 直接返回 null），有几条任务画几张。
 *
 * **恒画六格、未走到的标灰而非隐藏**：「哪一环没走」正是管理要看的东西，
 * 把没走的藏起来就只剩一份自我表扬。
 *
 * **卡片是重新进入后三环的唯一入口**：方案/执行/结果三环在任务详情抽屉里确认，而抽屉
 * 此前只在「刚提交完那一下」弹出一次，制品 id 只活在组件的 useState 里——人不小心关掉
 * 窗口（或刷新页面），这条任务就在对话里彻底失联，剩下三环再也走不到。清单来自服务端
 * （chat_bi_conversation_tasks），故重新进入不依赖任何内存态。
 */
import { Button } from "antd";
import type { ChatBiClosureTask } from "../../types";
import { OUTCOME_LABEL } from "./decisionMeta";

const KIND_LABEL: Record<string, string> = {
  sync: "同步",
  transform: "加工",
  metric: "聚合",
  materialize: "物化",
};

/**
 * 最多画几张卡。
 *
 * 一条会话反复重试同一件事是常态（真实数据里见过同一张表 7 条制品），每条都摊开六环
 * 会把对话末尾撑成一份列表。截断本身不是问题，**不说**才是——超出部分在下面明写还剩
 * 几条，而不是悄悄消失让人以为只有这些。
 */
const MAX_TASK_CARDS = 3;

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

/** 前三环在对话里的表单向导确认，靠表单的 confirmation_id 归属到某条任务。 */
const FORM_RINGS = ["requirement", "ontology", "data"];

/**
 * 卡头标题：类型 + 任务名。
 *
 * 制品名常常**自带类型前缀**（Drafter 生成的就叫「同步 · 客户分组 → 数仓 ODS」），
 * 无条件再加一次就读成「同步 · 同步 · 客户分组 → 数仓 ODS」。名字里已经有了就不重复。
 */
function taskTitle(task: ChatBiClosureTask): string {
  const kind = task.kind ? (KIND_LABEL[task.kind] ?? task.kind) : "";
  const name = task.name ?? "";
  if (!kind || name.startsWith(kind)) return name || kind;
  return `${kind} · ${name}`;
}

/** 一条任务的闭环卡。`onEnterTask` 不传即纯只读（决策追踪页就是这样用的）。 */
export function ClosureCard({
  task,
  onEnterTask,
}: {
  task: ChatBiClosureTask;
  onEnterTask?: (artifactId: string) => void;
}) {
  if (!task?.nodes?.length) return null;
  const { label, hint } = taskAction(task.status);
  /**
   * 闭环按任务分开之前建的任务，关联上没落 confirmation_id，前三环无从归属，只能标灰。
   *
   * 标灰是对的（不猜、不把别的任务的确认扣到它头上），但**光标灰是在冤枉人**：那三环
   * 当初真的逐环确认过，只是记录挂不回这条任务。故这里明说一句，而不是让人对着三个
   * 「未确认」以为自己漏了三步。
   */
  const orphanedFormRings =
    !task.confirmation_id && task.nodes.every((n) => !(FORM_RINGS.includes(n.node) && n.reached));
  return (
    <div className="chatbi-closure">
      <div className="chatbi-closure-head">
        <span className="chatbi-closure-title">{taskTitle(task)}</span>
        <span className="chatbi-closure-count">
          {task.reached_count}/{task.total_count} 环已确认
        </span>
        {onEnterTask && (
          <Button size="small" type="link" onClick={() => onEnterTask(task.artifact_id)}>
            {label}
          </Button>
        )}
      </div>
      {onEnterTask && <div className="chatbi-closure-task-hint">{hint}</div>}
      <div className="chatbi-closure-nodes">
        {task.nodes.map((n) => (
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
      {orphanedFormRings && (
        <div className="chatbi-closure-legacy">
          这条任务建于闭环按任务分开之前：前三环的确认记录归属不到具体任务，故此处标灰；
          记录本身在决策追踪页仍可查。
        </div>
      )}
      {task.dangling.length > 0 && (
        <ul className="chatbi-closure-dangling">
          {task.dangling.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * 会话里所有任务的闭环卡。
 *
 * **没有任务就整块不渲染**——这正是「随口一问不该冒出闭环卡」的落点：纯查询不建任务，
 * 它在账本里留下的表态（对 SQL / 结果 / 映射的认可）属于留痕，不属于任何一条任务的环，
 * 在决策追踪页的时间线上照样看得到。
 */
export function ClosureCards({
  tasks,
  onEnterTask,
}: {
  tasks: ChatBiClosureTask[] | undefined;
  onEnterTask?: (artifactId: string) => void;
}) {
  const shown = (tasks ?? []).slice(0, MAX_TASK_CARDS);
  if (!shown.length) return null;
  return (
    <>
      {shown.map((task) => (
        <ClosureCard key={task.artifact_id} task={task} onEnterTask={onEnterTask} />
      ))}
      {(tasks?.length ?? 0) > shown.length && (
        <div className="chatbi-closure-task-more">
          另有 {(tasks?.length ?? 0) - shown.length} 条更早的任务，在「任务」页可见
        </div>
      )}
    </>
  );
}
