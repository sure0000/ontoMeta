import json
import re

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
from app.services.relation_terms import compact_relation_term, infer_relation_term
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
    obj_name_map: dict[str, str] | None = None,
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
                semantic_name = (obj_name_map or {}).get(ot.candidate_name, ot.candidate_name)
                bindings.append(
                    DraftBusinessLogicObjectBinding(
                        logic_name=logic.name,
                        object_type_name=semantic_name,
                        role="subject",
                        confidence=min(0.6, logic.confidence),
                    )
                )
    return bindings


def _infer_property_bindings(
    evidence: EvidenceBundle,
    obj_name_map: dict[str, str] | None = None,
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
                identifier_obj_name = (obj_name_map or {}).get(prop.object_candidate_name, prop.object_candidate_name)
                bindings.append(
                    DraftBusinessLogicPropertyBinding(
                        logic_name=logic.name,
                        object_type_name=identifier_obj_name,
                        field_name=prop.field_name,
                        role="input",
                        confidence=min(0.55, logic.confidence),
                    )
                )
    return bindings


class OntologyDraftGenerator:
    """调用 LLM 分步生成本体草稿，Mock 模式下基于证据包规则生成。"""

    def __init__(self, runtime_config=None) -> None:
        if runtime_config is None:
            self.use_mock = settings.use_mock_llm or not settings.openai_api_key
            self.client = OpenAI(api_key=settings.openai_api_key) if not self.use_mock else None
            self.model = settings.openai_model
        else:
            self.use_mock = runtime_config.use_mock or not runtime_config.api_key
            self.client = (
                OpenAI(
                    api_key=runtime_config.api_key,
                    base_url=runtime_config.api_base_url,
                )
                if not self.use_mock
                else None
            )
            self.model = runtime_config.model

    async def generate(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        if self.use_mock:
            return self._generate_from_evidence(evidence)
        return await self._generate_with_llm(evidence)

    def _generate_from_evidence(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        # Build two name maps:
        #   obj_identifier_map: candidate_name -> English identifier (e.g. payment)
        #   obj_display_map:    candidate_name -> Chinese display name (e.g. 支付)
        obj_identifier_map = {
            ot.candidate_name: self._refine_identifier_name(ot.candidate_name)
            for ot in evidence.object_types
        }
        obj_display_map = {
            ot.candidate_name: self._refine_semantic_name(ot.display_name, ot.candidate_name)
            for ot in evidence.object_types
        }

        object_types = [
            DraftObjectType(
                name=obj_identifier_map[item.candidate_name],
                display_name=obj_display_map[item.candidate_name],
                description=item.description,
                source_ref=item.source_dataset_urn,
                confidence=item.confidence,
            )
            for item in evidence.object_types
        ]

        properties = [
            DraftProperty(
                object_type_name=obj_identifier_map.get(item.object_candidate_name, item.object_candidate_name),
                name=self._refine_property_name(item.display_name, item.field_name),
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
                source_object_type_name=obj_identifier_map.get(item.source_object, item.source_object),
                target_object_type_name=obj_identifier_map.get(item.target_object, item.target_object),
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

        object_bindings = _infer_object_bindings(evidence, obj_identifier_map)
        property_bindings = _infer_property_bindings(evidence, obj_identifier_map)

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

    @staticmethod
    def _refine_semantic_name(display_name: str | None, candidate_name: str) -> str:
        """Extract a concise business semantic name from display_name.

        Strips trailing technical suffixes like 1日汇总, 日表, 维表, 明细表 etc.
        Falls back to candidate_name only if display_name is absent.
        """
        if not display_name:
            return candidate_name
        cleaned = re.sub(
            r"(1日汇总|[1-9]日汇总|日表|日汇总|明细表|维表|日明细|汇总表|明细|全量|增量|快照|视图)$",
            "",
            display_name,
        )
        return cleaned.strip() or display_name

    @staticmethod
    def _refine_identifier_name(candidate_name: str) -> str:
        """Clean a technical candidate_name into a concise English identifier.

        Strips DataHub layer prefixes (ods_, dwd_, etc.) and suffixes
        (_entity, _di, _1d, etc.) to produce a business-friendly English name.
        """
        name = candidate_name
        for prefix in ("ods_", "dwd_", "dws_", "ads_", "dim_", "fact_"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        suffixes = [
            "_1d_entity", "_7d_entity", "_30d_entity",
            "_di_entity", "_df_entity", "_d_entity",
            "_entity", "_1d", "_7d", "_30d",
            "_di", "_df", "_d",
        ]
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        return name or candidate_name

    @staticmethod
    def _refine_property_name(display_name: str | None, field_name: str) -> str:
        """Return English property identifier name from field_name."""
        if not field_name:
            return display_name or ""
        return field_name

    async def _generate_with_llm(self, evidence: EvidenceBundle) -> OntologyDraftOutput:
        prompt = self._build_prompt(evidence)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业本体建模专家。你的核心任务是将 DataHub 技术元数据提升为业务语义本体，"
                        "而不是简单搬运表名和字段名。\n\n"
                        "关键原则：本体必须真实反映企业业务对象、业务关系与业务规则，"
                        "不应停留在「字段翻译」或「表结构描述」层面。\n\n"
                        "根据 DataHub 证据包生成本体草稿 JSON，包含 objectTypes、properties、"
                        "relationTypes、businessLogics、businessLogicObjectBindings、"
                        "businessLogicPropertyBindings、evidenceRefs。\n\n"
                        "命名要求（最重要）：\n"
                        "- objectTypes 的 name 是英文标识名（如 payment、refund、finance_reconciliation），"
                        "由 candidate_name 去掉技术前缀和后缀推导而来；"
                        "display_name 是中文业务语义名称（如「支付」「退款」「财务对账」），"
                        "由 display_name 去掉技术后缀推导而来。\n"
                        "- properties 的 name 是英文标识名（如 payment_amount、refund_status、biz_date），"
                        "直接使用证据包中的 field_name；"
                        "display_name 是中文业务语义名称（如「支付金额」「退款状态」「业务日期」），"
                        "优先使用证据包中的 display_name。\n"
                        "- properties 的 object_type_name 必须使用对应 objectTypes 的 name"
                        "（即英文标识名），不可使用证据包中的 object_candidate_name（技术名）。\n"
                        "- relationTypes 的 source_object_type_name 和 target_object_type_name"
                        "必须使用对应 objectTypes 的 name（英文标识名），"
                        "不可使用 source_object/target_object（技术名）。\n"
                        "- relationTypes 的 displayName 必须是 2-6 字的业务关系动词"
                        "（如「属于」「包含」「下单」），不可写完整句子，详细说明放在 description。\n"
                        "- 所有实体的 source_ref 应保留原始技术引用（如 dataset urn），用于溯源。\n\n"
                        "所有实体状态应为 suggested，附带 confidence 与 source_ref。\n\n"
                        "businessLogicObjectBindings / businessLogicPropertyBindings 用于显式声明"
                        "每条业务逻辑依赖哪些对象和字段，role 可取 subject/dimension/output（对象）"
                        "或 input/output/filter/group（字段）。"
                        "binding 中的 object_type_name 和 field_name"
                        "应使用 objectTypes/properties 的 name（英文标识名）。\n\n"
                        "示例对照：\n"
                        "- candidate_name=finance_reconciliation_1d_entity, "
                        "display_name=财务对账1日汇总 → name=finance_reconciliation, display_name=财务对账\n"
                        "- candidate_name=payment_di_entity, "
                        "display_name=支付明细日表 → name=payment, display_name=支付\n"
                        "- field_name=biz_date, "
                        "display_name=业务日期 → name=biz_date, display_name=业务日期\n"
                        "- source_object=payment_di_entity → source_object_type_name=payment"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        normalized = self._normalize_llm_output(raw)

        obj_identifier_map = {
            ot.candidate_name: self._refine_identifier_name(ot.candidate_name)
            for ot in evidence.object_types
        }
        # Ensure every relation_type has a display_name (LLM may omit it).
        for rt in normalized.get("relation_types") or []:
            if isinstance(rt, dict) and "display_name" not in rt:
                rt["display_name"] = infer_relation_term(
                    rt.get("kind", "foreign_key"), rt.get("name")
                )

        if not normalized.get("business_logic_object_bindings") and not normalized.get(
            "business_logic_property_bindings"
        ):
            normalized["business_logic_object_bindings"] = [
                b.model_dump() for b in _infer_object_bindings(evidence, obj_identifier_map)
            ]
            normalized["business_logic_property_bindings"] = [
                b.model_dump() for b in _infer_property_bindings(evidence, obj_identifier_map)
            ]
        return OntologyDraftOutput.model_validate(normalized)

    def _build_prompt(self, evidence: EvidenceBundle) -> str:
        return json.dumps(evidence.model_dump(), ensure_ascii=False, indent=2)

    def _normalize_llm_output(self, raw: dict) -> dict:
        top_level = {
            "objectTypes": "object_types",
            "relationTypes": "relation_types",
            "businessLogics": "business_logics",
            "businessLogicObjectBindings": "business_logic_object_bindings",
            "businessLogicPropertyBindings": "business_logic_property_bindings",
            "evidenceRefs": "evidence_refs",
        }
        normalized = dict(raw)
        for src, dst in top_level.items():
            if src in normalized and dst not in normalized:
                normalized[dst] = normalized.pop(src)

        # Rename evidence field names to draft field names.
        # For object_types, if LLM used candidate_name, refine it to English identifier.
        field_renames = {
            "object_types": {"candidate_name": "name"},
            "properties": {"object_candidate_name": "object_type_name", "field_name": "name"},
            "relation_types": {"source_object": "source_object_type_name", "target_object": "target_object_type_name", "displayName": "display_name"},
        }
        for section, renames in field_renames.items():
            items = normalized.get(section)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                for old, new in renames.items():
                    if old in item and new not in item:
                        val = item.pop(old)
                        if section == "object_types" and old == "candidate_name":
                            val = self._refine_identifier_name(val)
                        item[new] = val

        return normalized
