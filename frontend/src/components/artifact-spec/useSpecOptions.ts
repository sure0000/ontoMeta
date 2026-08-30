import { useEffect, useState } from "react";
import { api } from "../../api";
import { CLEANSING_RULES, type OptionSource } from "./specFields";

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

  // 依赖字段的当前值：databases 依赖目标数据源，properties 依赖选定的对象。
  const dependsKey =
    optionSource?.kind === "databases"
      ? optionSource.dependsOn
      : optionSource?.kind === "properties"
        ? optionSource.scopeField
        : undefined;
  const dependsValue = dependsKey ? (allValues[dependsKey] as string | undefined) : undefined;

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
            // scopeField 指定了对象就只列那张表的列：同步的主键/增量字段/sequence 列必须
            // 在目标表上，全本体混列会让人选到一个执行期才发现不存在的字段。对象还没选时
            // 不发请求——空下拉比一份跨对象的错候选好。
            if (optionSource.scopeField && !dependsValue) break;
            const props = await api.listOntologyProperties(
              ontologyId,
              optionSource.scopeField ? dependsValue : undefined,
            );
            // 字段名在本体内可能跨对象重名；用对象名消歧展示，value 仍是字段 name
            next = props.map((p) => ({
              value: p.name,
              label: optionSource.scopeField
                ? `${p.display_name || p.name}（${p.name}）`
                : `${p.display_name || p.name}（${p.object_type_name}）`,
            }));
            break;
          }
          case "businessLogics": {
            if (!ontologyId) break;
            // 与 Data Agent 的 metric_task_options 保持同一候选边界：只展示已发布且已
            // 形式化的口径。未形式化口径没有可执行 AST，选出来也会在校验阶段被阻断。
            const page = await api.listBusinessLogics({ ontologyId, publishedOnly: true });
            next = page.items.filter((b) => Boolean(b.expression_json)).map((b) => ({
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
            next = list
              .filter((d) => !optionSource.purpose || d.purpose === optionSource.purpose)
              .filter((d) => !optionSource.engine || d.kind === optionSource.engine)
              .filter((d) => !optionSource.defaultOnly || d.is_default_warehouse === true)
              .filter(
                (d) =>
                  !optionSource.executableOnly ||
                  (d.enabled !== false && d.dsn_set === true),
              )
              .map((d) => ({
                value: d.id,
                label: `${d.name}（${d.kind}${d.is_default_warehouse ? " · 默认" : ""}）`,
              }));
            // 上面的过滤说的是「现在可以选哪些」，可 Spec 里存着的那条可能已经掉出候选
            // （源库停用、目标仓不再是默认仓）。不补回来的话，下拉和只读预览都只剩一串
            // uuid——而人恰恰是在这种时候最需要看清它到底是谁。补进来但标注清楚。
            const current = optionSource.selfField
              ? String(allValues[optionSource.selfField] ?? "")
              : "";
            if (current && !next.some((o) => o.value === current)) {
              const stale = list.find((d) => d.id === current);
              if (stale) {
                next = [
                  ...next,
                  {
                    value: stale.id,
                    label: `${stale.name}（${stale.kind} · 不在候选范围）`,
                  },
                ];
              }
            }
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
  }, [
    optionSource?.kind,
    optionSource?.kind === "dataSources" ? optionSource.purpose : undefined,
    optionSource?.kind === "dataSources" ? optionSource.engine : undefined,
    optionSource?.kind === "dataSources" ? optionSource.defaultOnly : undefined,
    optionSource?.kind === "dataSources" ? optionSource.executableOnly : undefined,
    // 当前值：它掉出候选集时要被补回候选，故值一变就得重算。
    optionSource?.kind === "dataSources" && optionSource.selfField
      ? allValues[optionSource.selfField]
      : undefined,
    ontologyId,
    dependsValue,
  ]);

  return { options, loading, error };
}
