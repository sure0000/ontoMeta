/**
 * 确认闭环卡：六环 + 悬挂项。
 *
 * 对话内总结块与决策追踪页抽屉共用同一个组件——两处画的是同一件事，各写一份的话，
 * 迟早出现「聊天里说 4/6、追踪页说 3/6」这种没人能解释的差异。
 *
 * **恒画六格、未走到的标灰而非隐藏**：「哪一环没走」正是管理要看的东西，
 * 把没走的藏起来就只剩一份自我表扬。
 */
import type { ChatBiDecisionClosure } from "../../types";
import { OUTCOME_LABEL } from "./decisionMeta";

export function ClosureCard({ closure }: { closure: ChatBiDecisionClosure }) {
  if (!closure?.nodes?.length) return null;
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
