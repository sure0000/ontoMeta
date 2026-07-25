import { Button, Input, Space, Tag } from "antd";
import { FilterOutlined } from "@ant-design/icons";
import type { RuntimeFilter, ScreenParam } from "../types";

export interface DrillFilter {
  column: string;
  value: string;
}

/** 把「全局参数值」与「下钻条件」合并为运行时过滤器 */
export function buildRuntimeFilters(
  params: ScreenParam[],
  paramValues: Record<string, string>,
  drills: DrillFilter[],
): RuntimeFilter[] {
  const out: RuntimeFilter[] = [];
  for (const p of params) {
    const v = paramValues[p.id];
    if (v !== undefined && v !== "") {
      out.push({
        ref: { kind: "property", name: p.column },
        op: p.op || "eq",
        value: v,
      });
    }
  }
  for (const d of drills) {
    out.push({ ref: { kind: "property", name: d.column }, op: "eq", value: d.value });
  }
  return out;
}

export interface ParamBarProps {
  params: ScreenParam[];
  values: Record<string, string>;
  drills: DrillFilter[];
  onChange: (values: Record<string, string>) => void;
  onClearDrill: (index: number) => void;
  onApply: () => void;
}

/** 参数化筛选栏 + 下钻条件展示（编辑器 / 只读页共用） */
export function ParamBar({
  params,
  values,
  drills,
  onChange,
  onClearDrill,
  onApply,
}: ParamBarProps) {
  if (params.length === 0 && drills.length === 0) return null;
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 12,
        padding: "8px 12px",
        marginBottom: 12,
        background: "var(--om-surface, #f8fafc)",
        borderRadius: 8,
      }}
    >
      <FilterOutlined style={{ color: "#64748b" }} />
      {params.map((p) => (
        <Space key={p.id} size={4}>
          <span style={{ color: "#64748b", fontSize: 13 }}>{p.label}</span>
          <Input
            size="small"
            style={{ width: 140 }}
            placeholder={p.column}
            value={values[p.id] ?? ""}
            onChange={(e) => onChange({ ...values, [p.id]: e.target.value })}
            onPressEnter={onApply}
            allowClear
          />
        </Space>
      ))}
      {params.length > 0 && (
        <Button size="small" type="primary" onClick={onApply}>
          应用
        </Button>
      )}
      {drills.map((d, i) => (
        <Tag
          key={`${d.column}-${i}`}
          color="blue"
          closable
          onClose={() => onClearDrill(i)}
        >
          下钻：{d.column} = {d.value}
        </Tag>
      ))}
    </div>
  );
}
