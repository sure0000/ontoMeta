import { Collapse, Form, Input, InputNumber, Select } from "antd";
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
  disabled = false,
}: {
  kind: string;
  value: Record<string, unknown>;
  ontologyId?: string | null;
  onChange: (key: string, next: unknown) => void;
  mode: "manual" | "proposal";
  /** 要跳过不渲染的字段 key 集合——由外层级联选择接管的字段不在此重复出现。 */
  skipKeys?: Set<string>;
  /** 已确认的对话提案锁定参数；修改必须重新走确认向导。 */
  disabled?: boolean;
}) {
  const fields = SPEC_FIELDS[kind] ?? [];
  const visible = skipKeys ? fields.filter((f) => !skipKeys.has(f.key)) : fields;
  if (!visible.length) {
    return null;
  }
  // 调优项（Flink 执行参数）折起来：它们留空就跟随设置页默认，摆在主表单里会淹没必填项。
  const basic = visible.filter((f) => !f.advanced);
  const advanced = visible.filter((f) => f.advanced);
  const render = (field: SpecFieldDef) => (
    <SpecFieldControl
      key={field.key}
      def={field}
      value={value[field.key]}
      ontologyId={ontologyId}
      allValues={value}
      onChange={(next) => onChange(field.key, next)}
      requiredMark={mode === "manual"}
      disabled={disabled}
    />
  );
  // 已经填过的（编辑模式回填 / 上一步填过）默认展开——折叠起来会让人以为自己没填。
  const advancedTouched = advanced.some((f) => {
    const v = value[f.key];
    return v != null && v !== "" && !(Array.isArray(v) && v.length === 0);
  });

  return (
    <>
      {basic.map(render)}
      {advanced.length > 0 && (
        <Collapse
          size="small"
          style={{ marginBottom: 12 }}
          defaultActiveKey={advancedTouched ? ["advanced"] : []}
          items={[
            {
              key: "advanced",
              label: "高级：Flink 执行参数（留空则跟随设置页的默认值）",
              children: <>{advanced.map(render)}</>,
            },
          ]}
        />
      )}
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
  disabled,
}: {
  def: SpecFieldDef;
  value: unknown;
  ontologyId?: string | null;
  allValues: Record<string, unknown>;
  onChange: (next: unknown) => void;
  requiredMark: boolean;
  disabled: boolean;
}) {
  const { options, loading, error } = useSpecOptions(def.optionSource, ontologyId, allValues);

  // 说明走 extra、报错走 help：extra 参与布局（不必再手算 marginBottom 给它腾行），
  // help 是校验位，红字只留给「选项拉取失败」这种真出错的情况——空下拉不该被当成
  // 「本来就没数据」。
  return (
    <Form.Item
      label={def.label}
      required={requiredMark && def.required}
      extra={error ? undefined : def.help}
      help={error ? "选项加载失败，请重试或检查本体/数据源" : undefined}
      validateStatus={error ? "error" : undefined}
      style={{ marginBottom: 16 }}
    >
      {renderControl(def, value, options, loading, onChange, disabled)}
    </Form.Item>
  );
}

function renderControl(
  def: SpecFieldDef,
  value: unknown,
  options: { value: string; label: string }[],
  loading: boolean,
  onChange: (next: unknown) => void,
  disabled: boolean,
) {
  switch (def.control) {
    case "text":
      return (
        <Input
          value={(value as string) ?? ""}
          disabled={disabled}
          placeholder={def.default ? `默认 ${String(def.default)}` : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "number":
      return (
        <InputNumber
          value={(value as number) ?? null}
          disabled={disabled}
          min={def.min}
          max={def.max}
          style={{ width: 200 }}
          placeholder={def.default != null ? `默认 ${String(def.default)}` : "跟随设置页"}
          // 清空要落成 undefined（而不是 null）：后端把空值当"没填 = 跟随设置页"，
          // 落一个 null 进 Spec 会让人以为这里显式配过。
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "textarea":
      return (
        <Input.TextArea
          rows={2}
          disabled={disabled}
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
          disabled={disabled}
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
          disabled={disabled}
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
          disabled={disabled}
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
          disabled={disabled}
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
          disabled={disabled}
          onChange={(v) => onChange(v)}
        />
      );
    default:
      return null;
  }
}
