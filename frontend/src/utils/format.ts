/** 将 ISO 时间字符串格式化为 "YYYY/M/D HH:MM" 本地显示。 */
export function formatDateTime(value?: string | null): string | null {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** 将对象转换为 URLSearchParams，自动跳过空值与 false。
 * 数组值以重复键发出（适配 FastAPI list 查询参数）。 */
export function buildQuery(
  params: Record<string, string | string[] | number | boolean | null | undefined>,
): string {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === false || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) if (v != null && v !== "") sp.append(key, String(v));
      continue;
    }
    sp.set(key, String(value));
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}
