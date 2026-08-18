/**
 * 决策留痕的前端小工具。
 *
 * 两件事：把表单值转成能安全 JSON 化的形状；把留痕调用统一成 fire-and-forget。
 *
 * **留痕绝不能影响用户正在做的事**——所有调用都吞掉错误。后端那侧同样恒返回 200，
 * 这里再兜一层是为了网络层失败（断网、超时）也不冒泡到 UI。
 */
import { api } from "../../api";

/**
 * 转成可安全 JSON 化的值。
 *
 * **dayjs 必须转**：antd 的 DatePicker 给的是 dayjs 对象，直接 JSON.stringify
 * 会序列化出一坨内部结构（$D/$M/$y…），存进账本后既读不懂也没法比对。
 */
export function toJsonSafe(value: unknown): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value === "object") {
    const maybeDayjs = value as { toISOString?: () => string; $d?: unknown };
    if (typeof maybeDayjs.toISOString === "function" && "$d" in maybeDayjs) {
      return maybeDayjs.toISOString();
    }
    if (value instanceof Date) return value.toISOString();
    if (Array.isArray(value)) return value.map(toJsonSafe);
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = toJsonSafe(v);
    }
    return out;
  }
  return value;
}

type DecisionBody = Parameters<typeof api.recordChatBiDecision>[1];

/** 记一条决策留痕；失败静默。无 conversationId 时直接跳过。 */
export function recordDecisionQuietly(
  conversationId: string | undefined,
  body: DecisionBody,
): void {
  if (!conversationId) return;
  void api.recordChatBiDecision(conversationId, body).catch(() => {});
}

/** 表单字段的默认值集合——即 agent 的预设，用作留痕里的「机器基线」。 */
export function formDefaults(
  fields: { name: string; default?: unknown }[] | undefined,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of fields ?? []) {
    if (f.default !== undefined && f.default !== null) out[f.name] = f.default;
  }
  return out;
}

/**
 * 「认可 / 存疑」轻量确认的入参。
 *
 * 这三个环（本体/数据/结果）今天在界面上**纯展示、无动作**，故留痕靠显式表态而非
 * 副作用推断。刻意做成**非闸门**：不点不拦，答案照看照用——强制会破坏现有对话手感，
 * 而目标是「保留人的选择」，不是「多设关卡」。
 */
export interface AckTarget {
  conversationId?: string;
  messageId?: string;
  blockId?: string;
  /** requirement | ontology | data | plan | execute | result */
  node: string;
  stage: string;
  /** 人可读的一句话，追踪页列表直接展示——须自带上下文，脱离原对话也读得懂。 */
  summary: string;
  refKind?: string;
  refId?: string;
  /** 被认可的内容摘要（不是原始结果集——账本不做数据副本，见服务端 _JSON_CAP）。 */
  chosen?: unknown;
}

/**
 * 表态并留痕。返回本次的 outcome 供组件回显。
 *
 * dedup_key 只到 (会话, 环, 块)，**不带 outcome**：先「认可」后改「存疑」是真实的改主意，
 * 应覆盖成最终态而不是两条打架的记录（服务端按 dedup_key 幂等取最终态）。
 */
export function recordAck(target: AckTarget, accepted: boolean): void {
  const { conversationId, messageId, blockId, node, stage, summary, refKind, refId, chosen } =
    target;
  recordDecisionQuietly(conversationId, {
    node,
    stage,
    trigger: accepted ? "ack_accept" : "ack_doubt",
    outcome: accepted ? "accepted" : "rejected",
    message_id: messageId,
    block_id: blockId,
    summary: `${accepted ? "认可" : "存疑"}：${summary}`,
    chosen: chosen === undefined ? undefined : toJsonSafe(chosen),
    ref_kind: refKind,
    ref_id: refId,
    dedup_key: blockId ? `${conversationId}:${node}:${stage}:${blockId}` : undefined,
  });
}
