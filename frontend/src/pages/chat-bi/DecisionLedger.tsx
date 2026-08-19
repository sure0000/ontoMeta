/**
 * 决策留痕的会话级读侧（P1）。
 *
 * 写侧（`ledger.ts`）是 fire-and-forget 的，读侧要解决它留下的两个问题：
 *
 * 1. **表态要能回显**。刷新页面后 `AckControl` 的本地 state 归零，一条已经认可过的
 *    结果又摆出两颗没按过的按钮——人会以为没记上，于是再点一次。
 * 2. **总结要跟得上**。对话内总结块由后端在**出这条消息时**投影，此后用户再表态，
 *    快照就旧了。读侧在每次写入后重取，让最后一块总结显示当前态。
 *
 * 故整个会话只取一次 `GET /closure`（它同时含六环聚合与完整时间线），两个消费者共用。
 * 取不到就退化成"本地态 + 后端快照"，与接入前的行为一致——闭环视图是观察层，
 * 挂了不该让对话不能用。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../../api";
import { ackKey } from "./ledger";
import type { ChatBiDecision, ChatBiDecisionClosure } from "../../types";

interface DecisionLedgerValue {
  closure: ChatBiDecisionClosure | null;
  /** 该表态点此前的结论；未表态过为 undefined。 */
  ackOf: (key: string | null) => "accepted" | "rejected" | undefined;
  /** 写入留痕后调用，异步重取闭环。 */
  notifyWritten: () => void;
}

const DecisionLedgerContext = createContext<DecisionLedgerValue>({
  closure: null,
  ackOf: () => undefined,
  notifyWritten: () => {},
});

export function useDecisionLedger(): DecisionLedgerValue {
  return useContext(DecisionLedgerContext);
}

/** 时间线 → 每个表态点的最终结论。同一点多次表态取最后一条（人改主意，留最终态）。 */
function indexAcks(records: ChatBiDecision[]): Map<string, "accepted" | "rejected"> {
  const out = new Map<string, "accepted" | "rejected">();
  for (const rec of records) {
    if (rec.trigger !== "ack_accept" && rec.trigger !== "ack_doubt") continue;
    const key = ackKey(rec.message_id ?? undefined, rec.node, rec.stage ?? "", rec.block_id ?? undefined);
    if (!key) continue;
    out.set(key, rec.trigger === "ack_accept" ? "accepted" : "rejected");
  }
  return out;
}

export function DecisionLedgerProvider({
  conversationId,
  children,
}: {
  conversationId?: string;
  children: ReactNode;
}) {
  const [closure, setClosure] = useState<ChatBiDecisionClosure | null>(null);
  // 留痕是 fire-and-forget 的，写请求返回时后端可能还没提交完；直接重取会读到旧值。
  // 小延迟 + 合并连续写（一次表单提交会连写多条）——比给写侧加 await 便宜得多。
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchClosure = useCallback(
    (id: string) => {
      api
        .getChatBiClosure(id)
        .then((data) => setClosure(data))
        // 闭环取不到就维持现状：块自带后端快照，表态维持本地态。不弹错、不打断对话。
        .catch(() => {});
    },
    [],
  );

  useEffect(() => {
    setClosure(null);
    if (!conversationId) return;
    fetchClosure(conversationId);
  }, [conversationId, fetchClosure]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const notifyWritten = useCallback(() => {
    if (!conversationId) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => fetchClosure(conversationId), 600);
  }, [conversationId, fetchClosure]);

  const acks = useMemo(() => indexAcks(closure?.records ?? []), [closure]);

  const value = useMemo<DecisionLedgerValue>(
    () => ({
      closure,
      ackOf: (key) => (key ? acks.get(key) : undefined),
      notifyWritten,
    }),
    [closure, acks, notifyWritten],
  );

  return (
    <DecisionLedgerContext.Provider value={value}>{children}</DecisionLedgerContext.Provider>
  );
}
