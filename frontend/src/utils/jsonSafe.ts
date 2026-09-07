/**
 * 表单取值 → 可安全 JSON 化的形状。
 *
 * **dayjs 必须转**：antd 的 DatePicker 给的是 dayjs 对象，直接 JSON.stringify
 * 会序列化出一坨内部结构（$D/$M/$y…），后端读不懂。
 *
 * 该转换与决策留痕无关，供建数表单提交等通用入口复用。
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
