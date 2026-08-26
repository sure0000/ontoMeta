import { Alert, Descriptions, Space, Tag, Typography } from "antd";
import { WarningOutlined } from "@ant-design/icons";
import { SPEC_FIELDS, isFieldVisible, type SpecFieldDef } from "../artifact-spec/specFields";
import { useSpecOptions } from "../artifact-spec/useSpecOptions";
import { RANGE_STEP_KEYS } from "./TaskConfigForm";

const { Text } = Typography;

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
  sync: "把源头数据同步进数仓 ODS",
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
  // 与表单同一份可见性判定：预览里不该出现「当前装载方式根本用不到、也不会提交」的字段，
  // 否则人在最后一步确认的是一份与实际提交不一致的清单。
  const configFields = (SPEC_FIELDS[kind] ?? []).filter(
    (f) => !RANGE_STEP_KEYS.has(f.key) && isFieldVisible(f, specData),
  );
  const shownFields = configFields.filter((f) => filled(specData, f.key));
  const missingRequired = configFields.filter((f) => f.required && !filled(specData, f.key));
  /**
   * 作用范围里的实体存的是技术名（对象 name / 业务逻辑 id）。最后这一步是给人核对的，
   * 摆一个 `customer_group`（聚合任务更是一串 uuid）等于让人对着 id 点确认——
   * 用与「数据范围」那一步同一份候选表换回业务名。
   */
  const entitySource = kind === "metric" ? "businessLogics" : "objectTypes";
  const { options: entityOptions } = useSpecOptions(
    { kind: entitySource },
    ontologyId,
    {},
  );
  const entityLabel = (value: string): string =>
    entityOptions.find((o) => o.value === value)?.label ?? value;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {/* 只在真的挡路时才占一条 Alert：一切正常时「配置完成」那条横幅什么也没说，
          而下面的两张表本身就是给人核对的内容。 */}
      {missingRequired.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`请返回补齐：${missingRequired.map((f) => f.label).join("、")}`}
        />
      )}

      <Descriptions title="基本信息" bordered size="small" column={1}>
        <Descriptions.Item label="任务类型">
          <Tag color="blue">{KIND_LABEL[kind]}</Tag>
        </Descriptions.Item>
        {/* 留空时后端按 Spec 派生（如「同步 · 客户分组 → 数仓 ODS」），不是本体名——
            此前这里回填本体名，等于在最后一步告诉人任务会叫「erpnext v1」。 */}
        <Descriptions.Item label="任务名称">
          {taskName.trim() || "（按配置自动命名）"}
        </Descriptions.Item>
        <Descriptions.Item label="本体">{ontologyName || "未选择"}</Descriptions.Item>
        <Descriptions.Item label="执行操作">{KIND_ACTION[kind]}</Descriptions.Item>
      </Descriptions>

      <Descriptions title={KIND_SECTION[kind] ?? "任务配置"} bordered size="small" column={1}>
        <Descriptions.Item label={ENTITY_LABEL[kind] ?? "作用范围"}>
          {hasEntities ? (
            <Space direction="vertical" size={4}>
              {kind === "materialize" && <Text>已选择 {selectedEntities.length} 个实体</Text>}
              <div style={{ maxHeight: 120, overflow: "auto" }}>
                {selectedEntities.map((e) => (
                  <Tag key={e}>{entityLabel(e)}</Tag>
                ))}
              </div>
            </Space>
          ) : kind === "materialize" ? (
            <Text type="warning">
              <WarningOutlined /> 全部实体
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

      <Text type="secondary" style={{ fontSize: 12 }}>
        创建后为草稿，需经校验与人工确认才会执行。
      </Text>
    </Space>
  );
}
