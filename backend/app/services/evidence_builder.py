import re

from app.schemas import (
    DataHubDomainBundle,
    EvidenceBundle,
    LogicEvidencePack,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
    RelationEvidencePack,
)
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

        for dataset in bundle.datasets:
            object_name = _infer_object_name(dataset.name)
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
                        confidence=0.7 if field.display_name else 0.55,
                        evidence_refs=[f"{dataset.urn}#{field.name}"],
                    )
                )

                if field.is_foreign_key and field.foreign_key_target:
                    target_table = field.foreign_key_target.split(".")[0]
                    target_object = _infer_object_name(target_table)
                    source_label = dataset.display_name or dataset.name
                    target_label = target_table
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
            relations.append(
                RelationEvidencePack(
                    name=f"{source_obj}_feeds_{target_obj}",
                    display_name=infer_relation_term("lineage"),
                    source_object=source_obj,
                    target_object=target_obj,
                    cardinality="one_to_many",
                    structure_type=infer_relation_structure_type(
                        f"血缘：{source_name} 加工至 {target_name}"
                    ),
                    description=f"血缘：{source_name} 加工至 {target_name}",
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

