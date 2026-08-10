import { Space, Typography, Select, Cascader, Alert, Spin } from "antd";
import { useState, useEffect, useCallback } from "react";
import { api } from "../../api";
import type { OntologySummary, DomainContext } from "../../types";

const { Title, Text } = Typography;

interface CascadeNode {
  value: string;
  label: string;
  children?: CascadeNode[];
  isLeaf?: boolean;
  loading?: boolean;
  disabled?: boolean;
}

interface Props {
  kind: string;
  ontologies: OntologySummary[];
  domains: DomainContext[];
  ontologyId?: string;
  selectedEntities: string[];
  onOntologyChange: (id: string | undefined) => void;
  onEntitiesChange: (entities: string[]) => void;
  loading?: boolean;
}

const KIND_ENTITY_CONFIG: Record<
  string,
  { label: string; source: "objectTypes" | "businessLogics"; multiple: boolean }
> = {
  materialize: {
    label: "可物化实体（业务对象 · 业务关系）",
    source: "objectTypes",
    multiple: true,
  },
  sync: {
    label: "同步对象",
    source: "objectTypes",
    multiple: false,
  },
  transform: {
    label: "目标对象",
    source: "objectTypes",
    multiple: false,
  },
  metric: {
    label: "业务逻辑",
    source: "businessLogics",
    multiple: false,
  },
};

export function TaskDataRangeSelector({
  kind,
  ontologies,
  domains,
  ontologyId,
  selectedEntities,
  onOntologyChange,
  onEntitiesChange,
  loading,
}: Props) {
  const [cascadeOptions, setCascadeOptions] = useState<CascadeNode[]>([]);
  const [cascadeLoading, setCascadeLoading] = useState(false);

  const domainName = useCallback(
    (domainContextId: string): string => {
      const d = domains.find((x) => x.id === domainContextId);
      return d?.name ?? domainContextId;
    },
    [domains],
  );

  const entityConfig = KIND_ENTITY_CONFIG[kind];

  // 物化任务：加载可物化实体树
  useEffect(() => {
    if (kind === "materialize" && ontologyId) {
      setCascadeLoading(true);
      api
        .listMaterializeTargets()
        .then(({ ontologies: onts }) => {
          const targetOnt = onts.find((o) => o.ontology_id === ontologyId);
          if (!targetOnt) {
            setCascadeOptions([]);
            return;
          }

          const nodes: CascadeNode[] = targetOnt.entities.map((e) => {
            const kindLabel = e.kind === "relation_type" ? "关系" : "对象";
            const display = e.display_name || e.name;
            return {
              value: e.name,
              label: `${kindLabel} · ${display} → ${e.table}`,
              isLeaf: true,
            };
          });
          setCascadeOptions(nodes);
        })
        .catch(() => {
          setCascadeOptions([]);
        })
        .finally(() => {
          setCascadeLoading(false);
        });
    }
  }, [kind, ontologyId]);

  // 其他任务类型：懒加载对象/业务逻辑
  const [emptyHint, setEmptyHint] = useState<string | null>(null);

  const loadEntities = useCallback(
    async (selected: CascadeNode[]) => {
      if (!entityConfig) return;
      const target = selected[selected.length - 1];
      target.loading = true;

      try {
        let children: CascadeNode[];
        if (entityConfig.source === "objectTypes") {
          const page = await api.listObjectTypes({
            ontologyId: target.value,
            publishedOnly: false,
          });
          children = page.items.map((o) => {
            // 同步任务必须能定位源表；无 source_ref 的对象置灰并说明原因，
            // 而不是让人选完、提交、再收到「无 source_ref，无法定位源表」。
            const unsyncable = kind === "sync" && !o.source_ref;
            return {
              value: o.name,
              label: unsyncable
                ? `${o.display_name || o.name}（无源表，不可同步）`
                : o.display_name || o.name,
              isLeaf: true,
              disabled: unsyncable,
            };
          });
        } else {
          const page = await api.listBusinessLogics({ ontologyId: target.value });
          children = page.items.map((b) => {
            // 没绑定对象的口径无法确定聚合 SQL 的 FROM 源表，校验闸门会以
            // missing_required_field 阻断确认——在选之前就说清楚，别等建完才拦。
            const unbound = !b.bound_object_count;
            return {
              value: b.id,
              label: unbound
                ? `${b.display_name || b.name}（未绑定对象，不可建指标）`
                : b.display_name || b.name,
              isLeaf: true,
              disabled: unbound,
            };
          });
        }
        target.children = children;
        // 空下拉/全不可选要说清原因，否则人只看到一个点不开的框。
        const selectable = children.filter((c) => !c.disabled);
        if (children.length === 0) {
          setEmptyHint(
            entityConfig.source === "businessLogics"
              ? "该本体下还没有业务逻辑。指标口径须先在「业务逻辑」中定义，再回来建指标任务。"
              : "该本体下还没有业务对象。",
          );
        } else if (selectable.length === 0) {
          setEmptyHint(
            entityConfig.source === "businessLogics"
              ? "该本体下的业务逻辑都还没绑定对象，无法建指标任务；请先在「业务逻辑」里绑定主对象。"
              : "该本体下的对象都没有源表（source_ref），无法建同步任务；请先完成元数据接入。",
          );
        } else {
          setEmptyHint(null);
        }
      } catch {
        target.children = [];
        setEmptyHint("加载失败，请重试或检查本体与数据源配置。");
      }

      target.loading = false;
      setCascadeOptions((prev) => [...prev]);
    },
    [entityConfig, kind],
  );

  // 非物化任务：构建本体级联第一层
  useEffect(() => {
    if (kind !== "materialize" && entityConfig) {
      const nodes: CascadeNode[] = ontologies.map((o) => ({
        value: o.id,
        label: `${domainName(o.domain_context_id)} v${o.version}（${o.status}）`,
        isLeaf: false,
      }));
      setCascadeOptions(nodes);
    }
  }, [kind, ontologies, domainName, entityConfig]);

  const handleOntologyChange = (value: string | undefined) => {
    onOntologyChange(value);
    onEntitiesChange([]);
  };

  const handleEntitiesChange = (value: string[] | string[][]) => {
    if (kind === "materialize") {
      // 物化任务：多选实体名
      const entities = (value as string[]).filter((v) => typeof v === "string");
      onEntitiesChange(entities);
    } else {
      // 其他任务：级联单选 [ontologyId, entityValue]
      const cascadeValue = value as string[];
      if (cascadeValue && cascadeValue.length === 2) {
        const [ontId, entityValue] = cascadeValue;
        onOntologyChange(ontId);
        onEntitiesChange([entityValue]);
      } else {
        onEntitiesChange([]);
      }
    }
  };

  const getHelp = () => {
    switch (kind) {
      case "materialize":
        return "物化任务会为选中的业务对象和关系自动生成数据表。如果不选择具体实体，将物化该本体下的全部实体。";
      case "sync":
        return "选择需要从源系统同步到目标库的数据对象。";
      case "transform":
        return "选择数据加工后要写入的目标对象。";
      case "metric":
        return "选择要计算的业务指标定义，系统将根据定义生成聚合表。";
      default:
        return "";
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: "40px 0" }}>
        <Spin tip="加载中..." />
      </div>
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Title level={4}>选择数据范围</Title>
        <Text type="secondary">指定任务要处理的本体和实体</Text>
      </div>

      {getHelp() && <Alert message={getHelp()} type="info" showIcon closable />}

      <div>
        <div style={{ marginBottom: 8 }}>
          <Text strong>本体</Text>
          <Text type="danger"> *</Text>
        </div>
        <Select
          style={{ width: "100%" }}
          placeholder="选择本体（包含要处理的数据对象定义）"
          showSearch
          allowClear
          optionFilterProp="label"
          value={ontologyId}
          onChange={handleOntologyChange}
          disabled={kind !== "materialize" && entityConfig !== undefined}
          options={ontologies.map((o) => ({
            value: o.id,
            label: `${domainName(o.domain_context_id)} v${o.version}（${o.status}）`,
          }))}
        />
        <div style={{ marginTop: 4 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            本体是数据对象和业务规则的集合，类似数据模型的一个版本快照
          </Text>
        </div>
      </div>

      {ontologyId && entityConfig && (
        <div>
          <div style={{ marginBottom: 8 }}>
            <Text strong>{entityConfig.label}</Text>
            {kind !== "materialize" && <Text type="danger"> *</Text>}
          </div>

          {kind === "materialize" ? (
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              placeholder="选择要物化的实体（留空则物化全部）"
              showSearch
              allowClear
              loading={cascadeLoading}
              optionFilterProp="label"
              value={selectedEntities}
              onChange={(vals) => handleEntitiesChange(vals)}
              options={cascadeOptions.map((node) => ({
                value: node.value,
                label: node.label,
              }))}
            />
          ) : (
            <Cascader
              style={{ width: "100%" }}
              placeholder="先选本体，再选实体"
              showSearch
              allowClear
              expandTrigger="hover"
              options={cascadeOptions}
              loadData={(opts) => loadEntities(opts as CascadeNode[])}
              value={
                ontologyId && selectedEntities.length > 0 ? [ontologyId, selectedEntities[0]] : []
              }
              onChange={(val) => handleEntitiesChange(val as string[])}
            />
          )}

          {emptyHint && (
            <div style={{ marginTop: 8 }}>
              <Alert message={emptyHint} type="warning" showIcon />
            </div>
          )}
        </div>
      )}
    </Space>
  );
}
