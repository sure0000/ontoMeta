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
 * 一次表态的身份，用于把已表态状态回显到对应的块上。
 *
 * **必须带 messageId**：`blockId` 是 `answer_to_blocks` 给的位置序号 `b0/b1…`，
 * 同一位置在每一轮里都叫同一个名字。只按 blockId 归并，第三轮的结果表会顶着
 * 第一轮的「已认可」出现。
 *
 * 流式刚产出的消息还没落库、拿不到 id，此时返回 null——调用方退回纯本地态。
 * 宁可这一轮不回显，也不能张冠李戴。
 */
export function ackKey(
  messageId: string | undefined,
  node: string,
  stage: string,
  blockId: string | undefined,
): string | null {
  if (!messageId || !blockId) return null;
  return `${messageId}:${node}:${stage}:${blockId}`;
}

/**
 * 表态并留痕。
 *
 * **刻意不传 dedup_key**。账本是追加式的（见 models/chat_bi_ledger 模块头），服务端
 * 命中 dedup_key 时返回既有记录而**不改写**它——若把 (会话,环,块) 当去重键，
 * 先「认可」后改「存疑」这条改判就会被静默丢掉，而界面照样把「存疑」点亮，
 * 账本与人看到的东西直接对不上。改判本身是有价值的信息，追加一条、由闭环取最新即可。
 *
 * 重复点同一个结论由调用方按已表态状态挡掉（见 AckControl），不靠服务端去重。
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
  });
}
