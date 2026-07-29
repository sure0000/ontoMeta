import { DatabaseOutlined } from "@ant-design/icons";
import { Alert, Button, Descriptions, Select, Space, Switch, Tag, Tooltip, message } from "antd";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { SectionCard } from "./SectionCard";
import type {
  MaterializationContract,
  MaterializationContractUpdateInput,
  MaterializationLayer,
  MaterializationLoadStrategy,
  MaterializationScdType,
  MaterializationTargetKind,
} from "../types";

const LAYER_OPTIONS: { value: MaterializationLayer; label: string }[] = [
  { value: "dim", label: "DIM 维度" },
  { value: "dwd", label: "DWD 明细事实" },
  { value: "dws", label: "DWS 汇总" },
  { value: "ads", label: "ADS 应用" },
];

const STRATEGY_OPTIONS: { value: MaterializationLoadStrategy; label: string }[] = [
  { value: "full", label: "全量" },
  { value: "incremental", label: "增量" },
  { value: "cdc", label: "CDC" },
];

const SCD_OPTIONS: { value: MaterializationScdType; label: string }[] = [
  { value: "none", label: "不留历史" },
  { value: "scd1", label: "SCD1 覆盖" },
  { value: "scd2", label: "SCD2 拉链" },
];

// 完整适配层的目标引擎。Hive 为权威写入路径，其余由其派生。
const ENGINE_OPTIONS = ["hive", "doris", "iceberg", "starrocks", "clickhouse"].map((e) => ({
  value: e,
  label: e,
}));

interface Props {
  ontologyId?: string;
  targetKind: MaterializationTargetKind;
  targetId: string;
}

/** 物化契约面板：本体不承载的落地配置（层/引擎/增量/分区/SCD）。
 *
 * 人工在此修改的字段会被「钉住」，机器重新推导默认值时不再覆盖。
 */
export function MaterializationContractPanel({ ontologyId, targetKind, targetId }: Props) {
  const [contract, setContract] = useState<MaterializationContract | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    try {
      const rows = await api.listMaterializationContracts(ontologyId, {
        target_kind: targetKind,
      });
      setContract(rows.find((r) => r.target_id === targetId) ?? null);
    } catch {
      setContract(null);
    } finally {
      setLoading(false);
    }
  }, [ontologyId, targetKind, targetId]);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = async (body: MaterializationContractUpdateInput) => {
    if (!contract) return;
    setSaving(true);
    try {
      setContract(await api.updateMaterializationContract(contract.id, body));
      message.success("已保存，该字段已钉住，机器重推导不会覆盖");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const sync = async () => {
    if (!ontologyId) return;
    setSaving(true);
    try {
      const result = await api.syncMaterializationContracts(ontologyId);
      message.success(
        `已推导：新增 ${result.created}、更新 ${result.updated}、保留人工配置 ${result.skipped_pinned} 项`,
      );
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "推导失败");
    } finally {
      setSaving(false);
    }
  };

  if (!ontologyId) return null;

  const pinned = (column: string) =>
    contract?.pinned_fields.includes(column) ? (
      <Tooltip title="已由人工钉住，机器重推导不会覆盖">
        <Tag color="blue">已钉住</Tag>
      </Tooltip>
    ) : null;

  return (
    <SectionCard
      title="物化契约"
      icon={<DatabaseOutlined />}
      extra={
        <Button size="small" onClick={sync} loading={saving}>
          按本体重新推导
        </Button>
      }
    >
      {!contract ? (
        <Alert
          type="info"
          showIcon
          title={loading ? "加载中…" : "尚未生成物化契约"}
          description={
            loading ? undefined : "点击右上角「按本体重新推导」，为本体实体生成默认落地配置。"
          }
        />
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {contract.derivation_reason && (
            <Alert type="info" showIcon title={`推导依据：${contract.derivation_reason}`} />
          )}
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label={<>是否物化 {pinned("materialized")}</>}>
              <Switch
                checked={contract.materialized}
                loading={saving}
                onChange={(v) => patch({ materialized: v })}
              />
            </Descriptions.Item>
            <Descriptions.Item label={<>目标层 {pinned("target_layer")}</>}>
              <Select
                size="small"
                style={{ width: 160 }}
                value={contract.target_layer}
                options={LAYER_OPTIONS}
                disabled={saving}
                onChange={(v) => patch({ target_layer: v })}
              />
            </Descriptions.Item>
            <Descriptions.Item label={<>目标引擎 {pinned("target_engines")}</>}>
              <Select
                size="small"
                mode="multiple"
                style={{ width: 260 }}
                value={contract.engines}
                options={ENGINE_OPTIONS}
                disabled={saving}
                onChange={(v) => patch({ engines: v })}
              />
            </Descriptions.Item>
            <Descriptions.Item label={<>加载策略 {pinned("load_strategy")}</>}>
              <Select
                size="small"
                style={{ width: 160 }}
                value={contract.load_strategy}
                options={STRATEGY_OPTIONS}
                disabled={saving}
                onChange={(v) => patch({ load_strategy: v })}
              />
            </Descriptions.Item>
            <Descriptions.Item label={<>分区键 {pinned("partition_key")}</>}>
              {contract.partition_key ? (
                <Tag>{contract.partition_key}</Tag>
              ) : (
                <span className="muted">无</span>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={<>历史留存 {pinned("scd_type")}</>}>
              <Select
                size="small"
                style={{ width: 160 }}
                value={contract.scd_type}
                options={SCD_OPTIONS}
                disabled={saving}
                onChange={(v) => patch({ scd_type: v })}
              />
            </Descriptions.Item>
          </Descriptions>
        </Space>
      )}
    </SectionCard>
  );
}
