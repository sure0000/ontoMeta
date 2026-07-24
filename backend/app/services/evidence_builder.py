import re

from app.schemas import (
    DataHubDomainBundle,
    EvidenceBundle,
    LogicEvidencePack,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
    RelationEvidencePack,
)
from app.services.object_classifier import FieldSignal, classify_object_role
from app.services.relation_terms import infer_relation_term
from app.services.relation_structure import infer_relation_structure_type


def _to_snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def _infer_object_name(dataset_name: str) -> str:
    base = _to_snake(dataset_name)
    if base.endswith("s"):
        return base
    return f"{base}_entity" if not base.endswith("_entity") else base


class EvidenceBuilder:
    """将 DataHub 原始输入整理为 LLM 证据包。"""

    def build(
        self,
        bundle: DataHubDomainBundle,
        *,
        include_business_logics: bool = False,
    ) -> EvidenceBundle:
        object_types: list[ObjectTypeEvidencePack] = []
        properties: list[PropertyEvidencePack] = []
        relations: list[RelationEvidencePack] = []
        business_logics: list[LogicEvidencePack] = []

        dataset_name_map = {ds.urn: ds.name for ds in bundle.datasets}
        # 跨表拓扑：先聚合“每张表被多少张其它表通过外键指向”（入度）
        # 与血缘上/下游数量，供对象角色分类器使用。
        fk_in_degree, lineage_up, lineage_down = self._build_topology(bundle)

        # 关系描述用的业务展示名映射:候选名(source_object/target_object)必须
        # 仍由技术名(ds.name)推导，保证与 object_types 的 candidate_name 一致；
        # 但描述文本改用业务展示名，让 LLM 拿到的关系语义证据是「订单明细
        # 加工至 结算汇总」而非「order_di_entity 加工至 settlement_1d_entity」，
        # 才有足够信息推断出具体业务关系词，而不是笼统落回「派生」。
        dataset_display_by_urn = {
            ds.urn: (ds.display_name or ds.name) for ds in bundle.datasets
        }
        dataset_by_name = {ds.name: ds for ds in bundle.datasets}

        for dataset in bundle.datasets:
            object_name = _infer_object_name(dataset.name)
            role = classify_object_role(
                [
                    FieldSignal(
                        name=f.name,
                        semantic_type=self._infer_semantic_type(f),
                        is_primary_key=f.is_primary_key,
                        is_foreign_key=f.is_foreign_key,
                        unique_count=f.unique_count,
                    )
                    for f in dataset.fields
                ],
                fk_in_degree=fk_in_degree.get(dataset.name, 0),
                lineage_upstream=lineage_up.get(dataset.urn, 0),
                lineage_downstream=lineage_down.get(dataset.urn, 0),
                glossary_terms=dataset.glossary_terms,
                row_count=dataset.row_count,
            )
            # 保留原启发式（维表）作为命名置信度；对象是否为业务对象另走 role。
            is_dimension = dataset.name.startswith("dim_") or "维" in (dataset.display_name or "")
            confidence = 0.85 if is_dimension else 0.65

            object_types.append(
                ObjectTypeEvidencePack(
                    candidate_name=object_name,
                    display_name=dataset.display_name or dataset.name,
                    description=dataset.description,
                    source_dataset_urn=dataset.urn,
                    confidence=confidence,
                    evidence_refs=[dataset.urn, bundle.domain.id],
                    table_role=role.role,
                    role_confidence=role.confidence,
                    role_reason=role.reason,
                )
            )

            for field in dataset.fields:
                semantic = self._infer_semantic_type(field)
                properties.append(
                    PropertyEvidencePack(
                        object_candidate_name=object_name,
                        field_name=field.name,
                        display_name=field.display_name or field.name,
                        description=field.description,
                        data_type=field.data_type,
                        semantic_type=semantic,
                        sample_values=field.sample_values,
                        confidence=0.7 if field.display_name else 0.55,
                        evidence_refs=[f"{dataset.urn}#{field.name}"],
                    )
                )

                if field.is_foreign_key and field.foreign_key_target:
                    target_table = field.foreign_key_target.split(".")[0]
                    target_object = _infer_object_name(target_table)
                    source_label = dataset.display_name or dataset.name
                    target_ds = dataset_by_name.get(target_table)
                    target_label = (
                        (target_ds.display_name or target_ds.name)
                        if target_ds
                        else target_table
                    )
                    relations.append(
                        RelationEvidencePack(
                            name=f"{object_name}_to_{target_object}",
                            display_name=infer_relation_term("foreign_key", field.name),
                            source_object=object_name,
                            target_object=target_object,
                            cardinality="many_to_one",
                            structure_type="foreign_key",
                            description=(
                                f"{source_label} 通过外键 {field.name} 关联 {target_label}"
                                f"（{field.foreign_key_target}）"
                            ),
                            confidence=0.8,
                            evidence_refs=[f"{dataset.urn}#{field.name}"],
                        )
                    )

        for lineage in bundle.lineages:
            source_name = dataset_name_map.get(lineage.source_urn, lineage.source_urn)
            target_name = dataset_name_map.get(lineage.target_urn, lineage.target_urn)
            source_obj = _infer_object_name(source_name)
            target_obj = _infer_object_name(target_name)
            # 描述用业务展示名(取不到时才退回技术名)，供 LLM 与确定性兜底推断使用。
            source_label = dataset_display_by_urn.get(lineage.source_urn, source_name)
            target_label = dataset_display_by_urn.get(lineage.target_urn, target_name)
            relations.append(
                RelationEvidencePack(
                    name=f"{source_obj}_feeds_{target_obj}",
                    display_name=infer_relation_term("lineage", target_label=target_label),
                    source_object=source_obj,
                    target_object=target_obj,
                    cardinality="one_to_many",
                    structure_type=infer_relation_structure_type(
                        f"血缘：{source_label} 加工至 {target_label}"
                    ),
                    description=f"血缘：{source_label} 加工至 {target_label}",
                    confidence=0.6,
                    evidence_refs=[lineage.source_urn, lineage.target_urn],
                )
            )

        if include_business_logics:
            for logic in bundle.logic_evidences:
                logic_type = "metric" if logic.name in {"gmv", "revenue", "amount"} else "tag"
                business_logics.append(
                    LogicEvidencePack(
                        name=_to_snake(logic.name),
                        display_name=logic.name,
                        logic_type=logic_type,
                        description=logic.description,
                        expression_summary=logic.expression,
                        source_type=logic.source_type,
                        source_ref=logic.source_ref,
                        confidence=0.65,
                        evidence_refs=[logic.source_ref or logic.name],
                    )
                )

        return EvidenceBundle(
            object_types=object_types,
            properties=properties,
            relations=relations,
            business_logics=business_logics,
        )

    def _build_topology(
        self, bundle: DataHubDomainBundle
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """聚合跨表拓扑信号（不依赖表名含义）：

        - fk_in_degree: 按表名计数“有多少张不同的表通过外键指向它”。
        - lineage_up / lineage_down: 按 URN 计数血缘上/下游数量。
        """
        fk_in_degree: dict[str, set[str]] = {}
        for ds in bundle.datasets:
            for f in ds.fields:
                if f.is_foreign_key and f.foreign_key_target:
                    target_table = f.foreign_key_target.split(".")[0]
                    fk_in_degree.setdefault(target_table, set()).add(ds.name)
        fk_counts = {table: len(refs) for table, refs in fk_in_degree.items()}

        lineage_up: dict[str, int] = {}
        lineage_down: dict[str, int] = {}
        for lin in bundle.lineages:
            lineage_down[lin.source_urn] = lineage_down.get(lin.source_urn, 0) + 1
            lineage_up[lin.target_urn] = lineage_up.get(lin.target_urn, 0) + 1
        return fk_counts, lineage_up, lineage_down

    def _infer_semantic_type(self, field) -> str:
        name = field.name.lower()
        if field.is_primary_key or name.endswith("_id"):
            return "identifier"
        if "date" in name or "time" in name:
            return "datetime"
        if "amount" in name or "price" in name:
            return "amount"
        if "status" in name or "type" in name or "level" in name:
            return "category"
        if "tag" in name or name.startswith("is_"):
            return "flag"
        return "attribute"

