import { useState } from "react";
import { Button, Popover, Space, Switch, Tag, Tooltip, Typography, message } from "antd";
import { LockOutlined } from "@ant-design/icons";

import { api } from "../api";

const { Text } = Typography;

/** 各实体的可合并字段（与后端 ontology_merge 的字段表一一对应）。 */
const MERGEABLE_FIELDS: Record<string, { field: string; label: string }[]> = {
  object_type: [
    { field: "name", label: "标识名" },
    { field: "display_name", label: "业务名" },
    { field: "table_role", label: "对象角色" },
    { field: "description", label: "描述" },
    { field: "role_reason", label: "角色依据" },
  ],
  relation_type: [
    { field: "display_name", label: "业务名" },
    { field: "cardinality", label: "基数" },
    { field: "structure_type", label: "结构类型" },
    { field: "description", label: "描述" },
  ],
  property: [
    { field: "display_name", label: "业务名" },
    { field: "data_type", label: "数据类型" },
    { field: "semantic_type", label: "语义类型" },
    { field: "description", label: "描述" },
  ],
};

/** 描述性字段：机器可持续刷新，钉住它等于让角色依据/描述永远停在当前措辞。 */
const DESCRIPTIVE = new Set(["description", "role_reason"]);

interface Props {
  entityType: "object_type" | "relation_type" | "property";
  entityId: string;
  pinnedFields?: string[];
  onChanged?: () => void;
}

/**
 * 人工权威字段面板：把「哪些字段机器已经改不动了」摆到台面上，并且可以交还给机器。
 *
 * 此前钉住是单向且不可见的——人工改过一次即永久钉住，`overridden_fields` 只在一个
 * tooltip 里露个名字，唯一的解冻入口是冲突面板里选「采纳上游」（还得先撞上冲突）。
 * `POST /api/fields/pin` 一直存在但前端零调用。随着人工投入增加，再生成的有效面
 * 越来越小，而用户看不到这件事正在发生。
 */
export function FieldAuthorityPanel({
  entityType,
  entityId,
  pinnedFields = [],
  onChanged,
}: Props) {
  const [pinned, setPinned] = useState<string[]>(pinnedFields);
  const [busy, setBusy] = useState<string | null>(null);
  const fields = MERGEABLE_FIELDS[entityType] ?? [];

  const toggle = async (field: string, next: boolean) => {
    setBusy(field);
    try {
      await api.setFieldPin({ entity_type: entityType, entity_id: entityId, field, pinned: next });
      setPinned((prev) => (next ? [...prev, field] : prev.filter((f) => f !== field)));
      message.success(next ? "已钉住，机器不会再改这个字段" : "已放开，交回机器接管");
      onChanged?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const content = (
    <div style={{ width: 300 }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        钉住的字段再生成时机器只提冲突、不改值。已发布对象的结构性字段会在发布时自动钉住。
      </Text>
      <div style={{ marginTop: 12 }}>
        {fields.map((f) => (
          <div
            key={f.field}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "6px 0",
            }}
          >
            <Space size={6}>
              <span>{f.label}</span>
              {DESCRIPTIVE.has(f.field) && (
                <Tooltip title="描述性字段：不钉住时机器会持续刷新">
                  <Tag style={{ marginInlineEnd: 0 }}>描述性</Tag>
                </Tooltip>
              )}
            </Space>
            <Switch
              size="small"
              checked={pinned.includes(f.field)}
              loading={busy === f.field}
              onChange={(next) => void toggle(f.field, next)}
            />
          </div>
        ))}
      </div>
    </div>
  );

  return (
    <Popover content={content} title="人工权威字段" trigger="click" placement="bottomRight">
      <Button icon={<LockOutlined />}>
        已钉住 {pinned.length}
      </Button>
    </Popover>
  );
}
