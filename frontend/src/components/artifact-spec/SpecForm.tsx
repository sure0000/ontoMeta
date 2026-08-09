import { Form, Input, Select } from "antd";
import { CronPicker } from "../CronPicker";
import { SPEC_FIELDS, type SpecFieldDef } from "./specFields";
import { useSpecOptions } from "./useSpecOptions";

/**
 * 字段 schema 驱动的结构化 Spec 表单。手填面板与对话提案两处共用。
 *
 * 本组件只按 SPEC_FIELDS[kind] 渲染字段并回调改一个扁平 dict，**不关心 spec/context 语义**——
 * 调用方决定提交时把这个 dict 塞进 `spec`（手填直填）还是 `context`（对话走 drafter 推导）。
 *
 * mode="manual"：required 字段做非空校验（没有 drafter 兜底）。
 * mode="proposal"：required 只作视觉提示（drafter 会推导补全），不硬阻断。
 */
export function SpecForm({
  kind,
  value,
  ontologyId,
  onChange,
  mode,
  skipKeys,
}: {
  kind: string;
  value: Record<string, unknown>;
  ontologyId?: string | null;
  onChange: (key: string, next: unknown) => void;
  mode: "manual" | "proposal";
  /** 要跳过不渲染的字段 key 集合——由外层级联选择接管的字段不在此重复出现。 */
  skipKeys?: Set<string>;
}) {
  const fields = SPEC_FIELDS[kind] ?? [];
  const visible = skipKeys
    ? fields.filter((f) => !skipKeys.has(f.key))
    : fields;
  if (!visible.length) {
    return null;
  }
  return (
    <>
      {visible.map((field) => (
        <SpecFieldControl
          key={field.key}
          def={field}
          value={value[field.key]}
          ontologyId={ontologyId}
          allValues={value}
          onChange={(next) => onChange(field.key, next)}
          requiredMark={mode === "manual"}
        />
      ))}
    </>
  );
}

function SpecFieldControl({
  def,
  value,
  ontologyId,
  allValues,
  onChange,
  requiredMark,
}: {
  def: SpecFieldDef;
  value: unknown;
  ontologyId?: string | null;
  allValues: Record<string, unknown>;
  onChange: (next: unknown) => void;
  requiredMark: boolean;
}) {
  const { options, loading, error } = useSpecOptions(
    def.optionSource,
    ontologyId,
    allValues,
  );

  // 选项拉取失败时给出可见提示：空下拉不该被当成「本来就没数据」。
  const help = error ? (
    <span style={{ color: "#cf1322" }}>选项加载失败，请重试或检查本体/数据源</span>
  ) : (
    def.help
  );

  return (
    <Form.Item
      label={def.label}
      required={requiredMark && def.required}
      help={help}
      validateStatus={error ? "error" : undefined}
      // 有说明文字的项要留出说明那一行的高度，否则它会压到下一项的标签上。
      style={{ marginBottom: help ? 28 : 12 }}
    >
      {renderControl(def, value, options, loading, onChange)}
    </Form.Item>
  );
}

function renderControl(
  def: SpecFieldDef,
  value: unknown,
  options: { value: string; label: string }[],
  loading: boolean,
  onChange: (next: unknown) => void,
) {
  switch (def.control) {
    case "text":
      return (
        <Input
          value={(value as string) ?? ""}
          placeholder={def.default ? `默认 ${String(def.default)}` : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "textarea":
      return (
        <Input.TextArea
          rows={2}
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "select":
    case "engineSelect":
    case "businessLogicSelect":
      return (
        <Select
          value={(value as string) ?? undefined}
          loading={loading}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={def.default ? `默认 ${String(def.default)}` : "请选择"}
          options={options}
          onChange={(v) => onChange(v)}
        />
      );
    case "objectSingle":
    case "propertySingle":
      return (
        <Select
          value={(value as string) ?? undefined}
          loading={loading}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={def.control === "propertySingle" ? "请选择本体字段" : "请选择本体对象"}
          options={options}
          onChange={(v) => onChange(v)}
        />
      );
    case "multiSelect":
    case "objectMultiSelect":
    case "propertyMultiSelect":
      return (
        <Select
          mode="multiple"
          value={(value as string[]) ?? []}
          loading={loading}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="可多选"
          options={options}
          onChange={(v) => onChange(v)}
        />
      );
    case "stringList":
      return (
        <Select
          mode="tags"
          value={(value as string[]) ?? []}
          placeholder="输入后回车添加"
          open={false}
          onChange={(v) => onChange(v)}
        />
      );
    case "cron":
      return (
        <CronPicker
          value={(value as string) ?? ""}
          size="middle"
          onChange={(v) => onChange(v)}
        />
      );
    default:
      return null;
  }
}
