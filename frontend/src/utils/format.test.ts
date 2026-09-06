import { describe, expect, it } from "vitest";
import { buildQuery, formatDateTime } from "./format";
import { toJsonSafe } from "./jsonSafe";

describe("buildQuery", () => {
  it("跳过空值与 false，不发出无意义的参数", () => {
    expect(buildQuery({ a: 1, b: null, c: undefined, d: "", e: false })).toBe("?a=1");
  });

  it("false 被跳过而 0 保留", () => {
    // 这条容易写错成 `if (!value) continue`，那样 0 会一起被吞掉——
    // 而 limit=0 / offset=0 是有意义的取值
    expect(buildQuery({ n: 0 })).toBe("?n=0");
  });

  it("数组以重复键发出（FastAPI 的 list 查询参数约定）", () => {
    expect(buildQuery({ ids: ["a", "b"] })).toBe("?ids=a&ids=b");
  });

  it("全空时返回空串，不留一个光秃秃的问号", () => {
    expect(buildQuery({ a: null })).toBe("");
  });
});

describe("formatDateTime", () => {
  it("空值返回 null，交给调用方决定显示什么", () => {
    expect(formatDateTime(null)).toBeNull();
    expect(formatDateTime(undefined)).toBeNull();
    expect(formatDateTime("")).toBeNull();
  });

  it("解析不了的字符串原样返回，不显示 Invalid Date", () => {
    expect(formatDateTime("不是时间")).toBe("不是时间");
  });

  it("分钟补零", () => {
    const out = formatDateTime("2026-09-05T08:05:00");
    expect(out).toMatch(/2026\/9\/5 \d{2}:05/);
  });
});

describe("toJsonSafe", () => {
  it("dayjs 对象转成 ISO 字符串", () => {
    // antd DatePicker 给的是 dayjs 对象，直接 JSON.stringify 会序列化出
    // $D/$M/$y 一坨内部结构，后端读不懂
    const fakeDayjs = { $d: new Date("2026-09-05T00:00:00Z"), toISOString: () => "2026-09-05T00:00:00.000Z" };
    expect(toJsonSafe(fakeDayjs)).toBe("2026-09-05T00:00:00.000Z");
  });

  it("Date 也转 ISO", () => {
    expect(toJsonSafe(new Date("2026-09-05T00:00:00Z"))).toBe("2026-09-05T00:00:00.000Z");
  });

  it("递归处理嵌套数组与对象", () => {
    const input = { rows: [{ at: new Date("2026-01-01T00:00:00Z"), name: "a" }], n: 1 };
    expect(toJsonSafe(input)).toEqual({
      rows: [{ at: "2026-01-01T00:00:00.000Z", name: "a" }],
      n: 1,
    });
  });

  it("原始值与 null 原样通过", () => {
    expect(toJsonSafe(null)).toBeNull();
    expect(toJsonSafe(undefined)).toBeUndefined();
    expect(toJsonSafe("x")).toBe("x");
    expect(toJsonSafe(3)).toBe(3);
    expect(toJsonSafe(false)).toBe(false);
  });
});
