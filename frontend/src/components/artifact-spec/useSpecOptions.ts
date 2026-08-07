import { useEffect, useState } from "react";
import { api } from "../../api";
import {
  BM_MANAGED_SERVICES,
  CLEANSING_RULES,
  type OptionSource,
} from "./specFields";

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * 按 optionSource 解析出下拉选项。本体类来源（对象/字段/业务逻辑）随 ontologyId 变化重拉；
 * databases 类随所依赖字段（target_datasource_id）变化重拉。静态/闭集来源同步返回，不发请求。
 *
 * 对象类下拉的 value 用对象 **name**（spec 里对象引用是 name，校验闸门按 ObjectType.name 查）；
 * 业务逻辑用 **id**（spec 里是 business_logic_id）。
 */
export function useSpecOptions(
  optionSource: OptionSource | undefined,
  ontologyId: string | null | undefined,
  allValues: Record<string, unknown>,
): { options: SelectOption[]; loading: boolean; error: boolean } {
  const [options, setOptions] = useState<SelectOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  // databases 依赖的字段当前值（仅当来源是 databases 时有意义）
  const dependsKey =
    optionSource?.kind === "databases" ? optionSource.dependsOn : undefined;
  const dependsValue = dependsKey
    ? (allValues[dependsKey] as string | undefined)
    : undefined;

  useEffect(() => {
    if (!optionSource) {
      setOptions([]);
      return;
    }

    // 同步来源：无需请求
    if (optionSource.kind === "static") {
      setOptions(optionSource.options);
      return;
    }
    if (optionSource.kind === "cleansingRules") {
      setOptions(CLEANSING_RULES);
      return;
    }
    if (optionSource.kind === "bmServices") {
      setOptions(BM_MANAGED_SERVICES);
      return;
    }

    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError(false);
      try {
        let next: SelectOption[] = [];
        switch (optionSource.kind) {
          case "objectTypes": {
            if (!ontologyId) break;
            // 不限 publishedOnly：drafter 按 ontology_id 查全部对象（不过滤发布态），
            // 真实 DataHub 本体多为未发布草稿；若这里只列已发布，下拉会空而 drafter 其实能解析。
            const page = await api.listObjectTypes({
              ontologyId,
              publishedOnly: false,
            });
            next = page.items.map((o) => ({
              value: o.name,
              label: o.display_name || o.name,
            }));
            break;
          }
          case "properties": {
            if (!ontologyId) break;
            const props = await api.listOntologyProperties(ontologyId);
            // 字段名在本体内可能跨对象重名；用对象名消歧展示，value 仍是字段 name
            next = props.map((p) => ({
              value: p.name,
              label: `${p.display_name || p.name}（${p.object_type_name}）`,
            }));
            break;
          }
          case "businessLogics": {
            if (!ontologyId) break;
            const page = await api.listBusinessLogics({ ontologyId });
            next = page.items.map((b) => ({
              value: b.id,
              label: b.display_name || b.name,
            }));
            break;
          }
          case "engines": {
            const out = await api.listWarehouseEngines();
            next = out.engines
              .filter((e) => e.implemented)
              .map((e) => ({ value: e.name, label: e.name }));
            break;
          }
          case "dataSources": {
            const list = await api.listDataSources();
            next = list.map((d) => ({
              value: d.id,
              label: `${d.name}（${d.kind}）`,
            }));
            break;
          }
          case "databases": {
            if (!dependsValue) break;
            const { databases } = await api.listDataSourceDatabases(dependsValue);
            next = databases.map((name) => ({ value: name, label: name }));
            break;
          }
        }
        if (!cancelled) setOptions(next);
      } catch {
        if (!cancelled) {
          setOptions([]);
          setError(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [optionSource?.kind, ontologyId, dependsValue]);

  return { options, loading, error };
}
