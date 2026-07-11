import json
import re

from openai import AsyncOpenAI

from app.config import settings
from app.schemas import (
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    EvidenceBundle,
    OntologyDraftOutput,
)
from app.services.relation_terms import compact_relation_term, infer_relation_term
from app.services.relation_structure import infer_relation_structure_type

_LLM_SYSTEM_PROMPT = (
    "你是企业本体建模专家。你的核心任务是将 DataHub 技术元数据提升为业务语义本体，"
    "而不是简单搬运表名和字段名。\n\n"
    "关键原则：本体必须真实反映企业业务对象、业务关系与业务规则，"
    "不应停留在「字段翻译」或「表结构描述」层面。\n\n"
    "根据 DataHub 证据包生成本体草稿 JSON，包含 objectTypes、properties、"
    "relationTypes、evidenceRefs。\n\n"
    "evidenceRefs 必须是字符串数组（如 dataset URN），"
    "不可使用 {reference, type} 等对象结构。\n\n"
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
    "示例对照：\n"
    "- candidate_name=finance_reconciliation_1d_entity, "
    "display_name=财务对账1日汇总 → name=finance_reconciliation, display_name=财务对账\n"
    "- candidate_name=payment_di_entity, "
    "display_name=支付明细日表 → name=payment, display_name=支付\n"
    "- field_name=biz_date, "
    "display_name=业务日期 → name=biz_date, display_name=业务日期\n"
    "- source_object=payment_di_entity → source_object_type_name=payment"
)


class OntologyDraftGenerator:
    """调用 LLM 分步生成本体草稿，Mock 模式下基于证据包规则生成。"""

    def __init__(self, runtime_config=None) -> None:
        timeout = settings.llm_timeout_seconds
        if runtime_config is None:
            self.use_mock = settings.use_mock_llm or not settings.openai_api_key
            self.client = (
                AsyncOpenAI(api_key=settings.openai_api_key, timeout=timeout)
                if not self.use_mock
                else None
            )
            self.model = settings.openai_model
        else:
            self.use_mock = runtime_config.use_mock or not runtime_config.api_key
            self.client = (
                AsyncOpenAI(
                    api_key=runtime_config.api_key,
                    base_url=runtime_config.api_base_url,
                    timeout=timeout,
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

        # 业务逻辑与本体草稿解耦:不再在草稿生成阶段产出 business_logics 及其绑定,
        # 业务逻辑改为在「业务逻辑」页通过代码导入或人工新建独立管理。

        evidence_refs = self._collect_evidence_refs(evidence)

        return OntologyDraftOutput(
            object_types=object_types,
            properties=properties,
            relation_types=relation_types,
            business_logics=[],
            business_logic_object_bindings=[],
            business_logic_property_bindings=[],
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
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        normalized = self._normalize_llm_output(raw)

        # Ensure every relation_type has a display_name (LLM may omit it).
        for rt in normalized.get("relation_types") or []:
            if isinstance(rt, dict) and "display_name" not in rt:
                rt["display_name"] = infer_relation_term(
                    rt.get("kind", "foreign_key"), rt.get("name")
                )

        # 业务逻辑与本体草稿解耦:忽略 LLM 返回的 businessLogics 及绑定,
        # 始终返回空,逻辑改由独立的业务逻辑页管理。
        normalized["business_logics"] = []
        normalized["business_logic_object_bindings"] = []
        normalized["business_logic_property_bindings"] = []
        if not normalized.get("evidence_refs"):
            normalized["evidence_refs"] = self._collect_evidence_refs(evidence)
        return OntologyDraftOutput.model_validate(normalized)

    def _build_prompt(self, evidence: EvidenceBundle) -> str:
        payload = evidence.model_dump(exclude={"business_logics"})
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _normalize_evidence_ref(ref) -> str | None:
        if isinstance(ref, str):
            value = ref.strip()
            return value or None
        if isinstance(ref, dict):
            for key in ("reference", "ref", "urn", "source_ref", "sourceRef"):
                value = ref.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @classmethod
    def _normalize_evidence_refs(cls, refs) -> list[str]:
        if not isinstance(refs, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            value = cls._normalize_evidence_ref(ref)
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
        return sorted(normalized)

    @staticmethod
    def _collect_evidence_refs(evidence: EvidenceBundle) -> list[str]:
        return sorted(
            {
                ref
                for pack in (
                    evidence.object_types,
                    evidence.properties,
                    evidence.relations,
                )
                for item in pack
                for ref in item.evidence_refs
            }
        )

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

        normalized["evidence_refs"] = self._normalize_evidence_refs(
            normalized.get("evidence_refs")
        )

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
