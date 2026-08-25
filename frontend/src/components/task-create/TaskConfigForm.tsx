import { Form, Input } from "antd";
import { SpecForm } from "../artifact-spec/SpecForm";

interface Props {
  kind: string;
  ontologyId?: string;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  /** 任务名。留空则由后端按 spec 派生（物化会派生成同名，故这里给人一个覆盖入口）。 */
  name: string;
  onNameChange: (name: string) => void;
  namePlaceholder: string;
  /** 额外跳过的字段（叠加在 RANGE_STEP_KEYS 之上）。sync 分步时用来只显示当前步的字段。 */
  extraSkipKeys?: Set<string>;
  /** 是否显示任务名输入框（仅在最后一个配置步骤显示）。 */
  showNameInput?: boolean;
}

/** 数据范围步骤已接管的字段，不在参数步骤重复出现。 */
export const RANGE_STEP_KEYS = new Set([
  "object_type",
  "target_table",
  "business_logic_id",
  "selected_targets",
]);

/**
 * 向导的参数步骤。
 *
 * **不再重复步骤标题与整段说明**：顶部 Steps 已经写着这一步叫什么、干什么，正文里
 * 再摆一遍标题 + 副标题 + 一条 Alert，等于同一句话说三遍，把真正要填的控件挤到下面。
 * 字段自己的说明也只留「不看就会填错」的那一句（见 specFields 的 help）。
 */
export function TaskConfigForm({
  kind,
  ontologyId,
  value,
  onChange,
  name,
  onNameChange,
  namePlaceholder,
  extraSkipKeys,
  showNameInput = true,
}: Props) {
  const handleFieldChange = (key: string, val: unknown) => {
    onChange({ ...value, [key]: val });
  };

  const skipKeys = extraSkipKeys
    ? new Set([...RANGE_STEP_KEYS, ...extraSkipKeys])
    : RANGE_STEP_KEYS;

  return (
    <Form layout="vertical" style={{ maxWidth: 640 }}>
      {showNameInput && (
        <Form.Item label="任务名称" extra="留空则按配置自动命名" style={{ marginBottom: 16 }}>
          <Input
            value={name}
            placeholder={namePlaceholder}
            onChange={(e) => onNameChange(e.target.value)}
            allowClear
          />
        </Form.Item>
      )}

      <SpecForm
        kind={kind}
        mode="manual"
        value={value}
        ontologyId={ontologyId}
        onChange={handleFieldChange}
        skipKeys={skipKeys}
      />
    </Form>
  );
}
