import { Alert, Cascader, Form, Select, Spin } from "antd";
import { useState, useEffect, useCallback } from "react";
import { api } from "../../api";
import type { OntologySummary, DomainContext } from "../../types";

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
            // 同步必须能定位源表；没有物理源表的对象置灰并说明原因，
            // 而不是让人选完、提交、再在 drafter 里被拒。
            // 判据用 source_provenance 而非 source_ref 是否为空：人工建模对象的
            // source_ref 是 `manual:<源>:<标识>`，非空却没有任何表可搬。
            const manual = o.source_provenance === "manual";
            const unsyncable =
              kind === "sync" && (manual || o.source_provenance !== "datahub");
            return {
              value: o.name,
              label: unsyncable
                ? `${o.display_name || o.name}（${manual ? "手工建模对象，需先物化" : "无源表"}，不可同步）`
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
                ? `${b.display_name || b.name}（未绑定对象，不可建任务）`
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
              ? "该本体下还没有业务逻辑。口径（指标 / 标签 / 规则）须先在「业务逻辑」中定义，再回来建任务。"
              : "该本体下还没有业务对象。",
          );
        } else if (selectable.length === 0) {
          setEmptyHint(
            entityConfig.source === "businessLogics"
              ? "该本体下的业务逻辑都还没绑定对象，无法建任务；请先在「业务逻辑」里绑定主对象。"
              : "该本体下的对象都没有物理源表，无法建同步任务。人工建模的对象只需「物化」把表建出来给业务用；要同步数据，请先完成数据源采集接入。",
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

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: "40px 0" }}>
        <Spin tip="加载中..." />
      </div>
    );
  }

  // 级联本身的第一层就是本体：同步/加工/聚合不再单独摆一个本体下拉。
  // 此前两者并存且那个下拉是 disabled 的，而级联又只在「已经选了本体」之后才渲染
  // ——新建同步任务时第 2 步既选不了本体、也没有级联可点，向导在这里彻底走不下去。
  const cascadePicksOntology = kind !== "materialize" && entityConfig !== undefined;

  return (
    <Form layout="vertical" style={{ maxWidth: 640 }}>
      {!cascadePicksOntology && (
        <Form.Item label="本体" required style={{ marginBottom: 16 }}>
          <Select
            placeholder="选择本体"
            showSearch
            allowClear
            optionFilterProp="label"
            value={ontologyId}
            onChange={handleOntologyChange}
            options={ontologies.map((o) => ({
              value: o.id,
              label: `${domainName(o.domain_context_id)} v${o.version}（${o.status}）`,
            }))}
          />
        </Form.Item>
      )}

      {entityConfig && (cascadePicksOntology || ontologyId) && (
        <Form.Item
          label={cascadePicksOntology ? `本体与${entityConfig.label}` : entityConfig.label}
          required={kind !== "materialize"}
          extra={kind === "materialize" ? "留空 = 该本体下全部实体" : undefined}
          style={{ marginBottom: 16 }}
        >
          {kind === "materialize" ? (
            <Select
              mode="multiple"
              placeholder="选择要物化的实体"
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
        </Form.Item>
      )}

      {ontologyId && entityConfig && emptyHint && (
        <Alert message={emptyHint} type="warning" showIcon />
      )}
    </Form>
  );
}
