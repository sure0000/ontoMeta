import { Space, Typography, Descriptions, Tag, Alert, Divider } from "antd";
import { CheckCircleOutlined, WarningOutlined } from "@ant-design/icons";
import { SPEC_FIELDS, type SpecFieldDef } from "../artifact-spec/specFields";
import { useSpecOptions } from "../artifact-spec/useSpecOptions";
import { RANGE_STEP_KEYS } from "./TaskConfigForm";

const { Title, Text } = Typography;

interface Props {
  kind: string;
  ontologyId?: string;
  ontologyName: string;
  taskName: string;
  selectedEntities: string[];
  specData: Record<string, unknown>;
}

const KIND_LABEL: Record<string, string> = {
  materialize: "物化任务",
  sync: "数据同步",
  transform: "数据加工",
  metric: "指标任务",
};

const KIND_ACTION: Record<string, string> = {
  materialize: "生成数据表和同步作业",
  sync: "同步数据到目标库",
  transform: "执行数据转换",
  metric: "计算指标并生成聚合表",
};

const KIND_SECTION: Record<string, string> = {
  materialize: "物化配置",
  sync: "同步配置",
  transform: "加工配置",
  metric: "指标配置",
};

const ENTITY_LABEL: Record<string, string> = {
  materialize: "物化范围",
  sync: "同步对象",
  transform: "目标表",
  metric: "业务逻辑",
};

/**
 * 一行「字段 → 值」。值用该字段自己的选项表翻成中文标签——预览里出现裸 id
 * （数据源 uuid、业务逻辑 uuid）时人根本无法核对自己选对没有。
 */
function SpecValueRow({
  def,
  value,
  ontologyId,
  allValues,
}: {
  def: SpecFieldDef;
  value: unknown;
  ontologyId?: string;
  allValues: Record<string, unknown>;
}) {
  const { options } = useSpecOptions(def.optionSource, ontologyId, allValues);
  const label = (v: unknown): string => {
    const raw = String(v);
    return options.find((o) => o.value === raw)?.label ?? raw;
  };

  if (Array.isArray(value)) {
    return (
      <>
        {value.map((v) => (
          <Tag key={String(v)}>{label(v)}</Tag>
        ))}
      </>
    );
  }
  return <>{label(value)}</>;
}

/** 已填写的字段（含默认值未动的也算未填，不展示，避免预览里一堆空行）。 */
function filled(specData: Record<string, unknown>, key: string): boolean {
  const v = specData[key];
  if (v == null || v === "") return false;
  if (Array.isArray(v) && v.length === 0) return false;
  return true;
}

export function TaskPreview({
  kind,
  ontologyId,
  ontologyName,
  taskName,
  selectedEntities,
  specData,
}: Props) {
  const hasEntities = selectedEntities.length > 0;
  // 参数步骤真正渲染过的字段（范围步骤接管的那几个不在这里重复展示）。
  const configFields = (SPEC_FIELDS[kind] ?? []).filter(
    (f) => !RANGE_STEP_KEYS.has(f.key),
  );
  const shownFields = configFields.filter((f) => filled(specData, f.key));
  const missingRequired = configFields.filter(
    (f) => f.required && !filled(specData, f.key),
  );

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Title level={4}>预览确认</Title>
        <Text type="secondary">检查配置信息，确认无误后提交创建</Text>
      </div>

      {missingRequired.length > 0 ? (
        <Alert
          type="error"
          showIcon
          message="必填项未填写"
          description={`请返回「配置参数」补齐：${missingRequired
            .map((f) => f.label)
            .join("、")}`}
        />
      ) : (
        <Alert
          type="success"
          icon={<CheckCircleOutlined />}
          message="配置完成"
          description="请仔细检查以下配置信息，确认后将创建任务。任务创建后需要经过校验和确认才能执行。"
          showIcon
        />
      )}

      <Descriptions title="基本信息" bordered size="small" column={1}>
        <Descriptions.Item label="任务类型">
          <Tag color="blue">{KIND_LABEL[kind]}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="任务名称">
          {taskName.trim() || ontologyName || "（自动生成）"}
        </Descriptions.Item>
        <Descriptions.Item label="本体">
          {ontologyName || "未选择"}
        </Descriptions.Item>
        <Descriptions.Item label="执行操作">
          {KIND_ACTION[kind]}
        </Descriptions.Item>
      </Descriptions>

      <Divider />

      <Descriptions
        title={KIND_SECTION[kind] ?? "任务配置"}
        bordered
        size="small"
        column={1}
      >
        <Descriptions.Item label={ENTITY_LABEL[kind] ?? "作用范围"}>
          {hasEntities ? (
            <Space direction="vertical" size={4}>
              {kind === "materialize" && (
                <Text>已选择 {selectedEntities.length} 个实体</Text>
              )}
              <div style={{ maxHeight: 120, overflow: "auto" }}>
                {selectedEntities.map((e) => (
                  <Tag key={e}>{e}</Tag>
                ))}
              </div>
            </Space>
          ) : kind === "materialize" ? (
            <Text type="warning">
              <WarningOutlined /> 全部实体（未指定则物化本体下所有业务对象和关系）
            </Text>
          ) : (
            <Text type="danger">未选择</Text>
          )}
        </Descriptions.Item>

        {shownFields.map((f) => (
          <Descriptions.Item key={f.key} label={f.label}>
            <SpecValueRow
              def={f}
              value={specData[f.key]}
              ontologyId={ontologyId}
              allValues={specData}
            />
          </Descriptions.Item>
        ))}
      </Descriptions>

      <Alert
        type="info"
        message="下一步操作"
        description={
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            <li>任务创建后将处于"草稿"状态</li>
            <li>系统会自动执行校验，检查配置是否合法</li>
            <li>校验通过后，您需要确认任务（人工审核）</li>
            <li>确认后，任务才能被执行</li>
          </ul>
        }
      />
    </Space>
  );
}
