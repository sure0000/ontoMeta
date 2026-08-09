import { Space, Typography, Alert, Form, Input } from "antd";
import { SpecForm } from "../artifact-spec/SpecForm";

const { Title, Text } = Typography;

interface Props {
  kind: string;
  ontologyId?: string;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  /** 任务名。留空则由后端按 spec 派生（物化会派生成同名，故这里给人一个覆盖入口）。 */
  name: string;
  onNameChange: (name: string) => void;
  namePlaceholder: string;
}

const KIND_HELP: Record<string, string> = {
  materialize: "配置目标数据源和表生成策略。系统将根据本体定义自动生成 DDL 和数据同步作业。",
  sync: "配置源数据库连接、目标库连接和同步策略（全量/增量）。",
  transform: "配置数据转换规则、分区策略和调度频率。",
  metric: "配置指标计算的时间窗口、聚合维度和更新频率。",
};

/** 数据范围步骤已接管的字段，不在参数步骤重复出现。 */
export const RANGE_STEP_KEYS = new Set([
  "object_type",
  "target_table",
  "business_logic_id",
  "selected_targets",
]);

export function TaskConfigForm({
  kind,
  ontologyId,
  value,
  onChange,
  name,
  onNameChange,
  namePlaceholder,
}: Props) {
  const handleFieldChange = (key: string, val: unknown) => {
    onChange({ ...value, [key]: val });
  };

  const skipKeys = RANGE_STEP_KEYS;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Title level={4}>配置任务参数</Title>
        <Text type="secondary">根据任务类型配置必要的执行参数</Text>
      </div>

      {KIND_HELP[kind] && (
        <Alert message={KIND_HELP[kind]} type="info" showIcon closable />
      )}

      <Form.Item
        label="任务名称"
        help="留空则按配置自动命名；同一本体建多个任务时建议自己起名以便区分"
        style={{ marginBottom: 28 }}
      >
        <Input
          value={name}
          placeholder={namePlaceholder}
          onChange={(e) => onNameChange(e.target.value)}
          allowClear
        />
      </Form.Item>

      <SpecForm
        kind={kind}
        mode="manual"
        value={value}
        ontologyId={ontologyId}
        onChange={handleFieldChange}
        skipKeys={skipKeys}
      />
    </Space>
  );
}
