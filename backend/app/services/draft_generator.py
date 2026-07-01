import json

from openai import OpenAI

from app.config import settings
from app.schemas import (
    DraftBusinessLogic,
    DraftBusinessLogicObjectBinding,
    DraftBusinessLogicPropertyBinding,
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    EvidenceBundle,
    OntologyDraftOutput,
)
from app.services.relation_terms import compact_relation_term
from app.services.relation_structure import infer_relation_structure_type


def _logic_blob(item) -> str:
    parts = [
        item.name,
        item.display_name,
        item.description or "",
        item.expression_summary or "",
        item.source_ref or "",
    ]
    return " ".join(parts).lower()


def _dataset_table_from_urn(urn: str) -> str | None:
    """从 DataHub dataset urn 中提取表名（如 order.fact_order）。"""
    if not urn:
        return None
    parts = urn.split(",")
    if len(parts) >= 2:
        return parts[1].rstrip(")")
    return None


def _object_match_tokens(ot) -> list[str]:
    tokens = [ot.candidate_name, ot.display_name]
    table = _dataset_table_from_urn(ot.source_dataset_urn)
    if table:
        tokens.append(table)
        # 也加入去掉前缀的简表名（fact_order / dim_customer / ads_xxx）
        for prefix in ("dim_", "fact_", "dwd_", "dws_", "ads_", "ods_"):
            if table.startswith(prefix):
                tokens.append(table[len(prefix):])
                break
    return [t for t in tokens if t]


def _infer_object_bindings(
    evidence: EvidenceBundle,
) -> list[DraftBusinessLogicObjectBinding]:
    bindings: list[DraftBusinessLogicObjectBinding] = []
    seen: set[tuple[str, str]] = set()
    for logic in evidence.business_logics:
        blob = _logic_blob(logic)
        for ot in evidence.object_types:
            key = (logic.name, ot.candidate_name)
            if key in seen:
                continue
            tokens = _object_match_tokens(ot)
            if not tokens:
                continue
            if any(t.lower() in blob for t in tokens):
                seen.add(key)
                bindings.append(
                    DraftBusinessLogicObjectBinding(
                        logic_name=logic.name,
                        object_type_name=ot.candidate_name,
                        role="subject",
                        confidence=min(0.6, logic.confidence),
                    )
                )
    return bindings


def _infer_property_bindings(
    evidence: EvidenceBundle,
) -> list[DraftBusinessLogicPropertyBinding]:
    bindings: list[DraftBusinessLogicPropertyBinding] = []
    seen: set[tuple[str, str, str]] = set()
    for logic in evidence.business_logics:
        blob = _logic_blob(logic)
        for prop in evidence.properties:
            key = (logic.name, prop.object_candidate_name, prop.field_name)
            if key in seen:
                continue
            tokens = [t for t in (prop.field_name, prop.display_name) if t]
            if not tokens:
                continue
            if any(t.lower() in blob for t in tokens):
                seen.add(key)
                bindings.append(
                    DraftBusinessLogicPropertyBinding(
                        logic_name=logic.name,
                        object_type_name=prop.object_candidate_name,
                        field_name=prop.field_name,
                        role="input",
                        confidence=min(0.55, logic.confidence),
                    )
                )
    return bindings


class OntologyDraftGenerator:
    """调用 LLM 分步生成本体草稿，Mock 模式下基于证据包规则生成。"""

    def __init__(self) -> None:
        self.use_mock = settings.use_mock_llm or not settings.openai_api_key
        self.client = OpenAI(api_key=settings.openai_api_key) if not self.use_mock else None
        self.model = settings.openai_model

    async def generate(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        if self.use_mock:
            return self._generate_from_evidence(evidence)
        return await self._generate_with_llm(evidence)

    def _generate_from_evidence(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        object_types = [
            DraftObjectType(
                name=item.candidate_name,
                display_name=item.display_name,
                description=item.description,
                source_ref=item.source_dataset_urn,
                confidence=item.confidence,
            )
            for item in evidence.object_types
        ]

        properties = [
            DraftProperty(
                object_type_name=item.object_candidate_name,
                name=item.field_name,
                display_name=item.display_name,
                description=item.description,
                data_type=item.data_type,
                semantic_type=item.semantic_type,
                source_field_ref=item.evidence_refs[0] if item.evidence_refs else None,
                required=item.semantic_type == "identifier",
                confidence=item.confidence,
            )
            for item in evidence.properties
        ]

        relation_types = [
            DraftRelationType(
                name=item.name,
                display_name=compact_relation_term(item.display_name),
                description=item.description,
                source_object_type_name=item.source_object,
                target_object_type_name=item.target_object,
                cardinality=self._normalize_cardinality(item.cardinality),
                structure_type=item.structure_type
                or infer_relation_structure_type(item.description),
                source_evidence=item.description
                or (", ".join(item.evidence_refs) if item.evidence_refs else None),
                confidence=item.confidence,
            )
            for item in evidence.relations
        ]

        business_logics = [
            DraftBusinessLogic(
                name=item.name,
                display_name=item.display_name,
                logic_type=item.logic_type,
                description=item.description,
                expression_summary=item.expression_summary,
                source_type=item.source_type,
                source_ref=item.source_ref,
                confidence=item.confidence,
            )
            for item in evidence.business_logics
        ]

        object_bindings = _infer_object_bindings(evidence)
        property_bindings = _infer_property_bindings(evidence)

        evidence_refs = sorted(
            {
                ref
                for pack in (
                    evidence.object_types,
                    evidence.properties,
                    evidence.relations,
                    evidence.business_logics,
                )
                for item in pack
                for ref in item.evidence_refs
            }
        )

        return OntologyDraftOutput(
            object_types=object_types,
            properties=properties,
            relation_types=relation_types,
            business_logics=business_logics,
            business_logic_object_bindings=object_bindings,
            business_logic_property_bindings=property_bindings,
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _normalize_cardinality(cardinality: str | None) -> str | None:
        if not cardinality:
            return None
        mapping = {
            "many_to_one": "N:1",
            "one_to_many": "1:N",
            "one_to_one": "1:1",
            "many_to_many": "N:M",
        }
        return mapping.get(cardinality, cardinality)

    async def _generate_with_llm(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        prompt = self._build_prompt(evidence)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业本体建模专家。根据 DataHub 证据包生成本体草稿 JSON，"
                        "包含 objectTypes、properties、relationTypes、businessLogics、"
                        "businessLogicObjectBindings、businessLogicPropertyBindings、evidenceRefs。"
                        "所有实体状态应为 suggested，附带 confidence 与 source_ref。"
                        "relationTypes 的 displayName 必须是 2-6 字的业务关系动词"
                        "（如「属于」「包含」「下单」），不可写完整句子，详细说明放在 description。"
                        "businessLogicObjectBindings / businessLogicPropertyBindings 用于显式声明"
                        "每条业务逻辑依赖哪些对象和字段，role 可取 subject/dimension/output（对象）"
                        "或 input/output/filter/group（字段）。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        normalized = self._normalize_llm_output(raw)
        if not normalized.get("business_logic_object_bindings") and not normalized.get(
            "business_logic_property_bindings"
        ):
            normalized["business_logic_object_bindings"] = [
                b.model_dump() for b in _infer_object_bindings(evidence)
            ]
            normalized["business_logic_property_bindings"] = [
                b.model_dump() for b in _infer_property_bindings(evidence)
            ]
        return OntologyDraftOutput.model_validate(normalized)

    def _build_prompt(self, evidence: EvidenceBundle) -> str:
        return json.dumps(evidence.model_dump(), ensure_ascii=False, indent=2)

    def _normalize_llm_output(self, raw: dict) -> dict:
        mapping = {
            "objectTypes": "object_types",
            "relationTypes": "relation_types",
            "businessLogics": "business_logics",
            "businessLogicObjectBindings": "business_logic_object_bindings",
            "businessLogicPropertyBindings": "business_logic_property_bindings",
            "evidenceRefs": "evidence_refs",
        }
        normalized = dict(raw)
        for src, dst in mapping.items():
            if src in normalized and dst not in normalized:
                normalized[dst] = normalized.pop(src)
        return normalized
