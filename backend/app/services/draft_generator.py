import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.config import settings
from app.schemas import (
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    EvidenceBundle,
    OntologyDraftOutput,
)
from app.services.relation_terms import compact_relation_term
from app.services.relation_structure import infer_relation_structure_type
from app.services.common import make_async_http_client
from app.services.draft_checkpoint import chunk_key
from app.services.evidence_chunker import split_evidence

logger = logging.getLogger(__name__)

# 进度回调：(已完成步数, 总步数) -> None，用于分块生成时逐块回报进度。
ProgressCallback = Callable[[int, int], Awaitable[None]]

# 每个对象的业务命名增强：candidate_name -> {name, display_name, description}
ObjectOverride = dict[str, str | None]
ObjectOverrides = dict[str, ObjectOverride]

# 每个对象下属性的中文业务名增强：candidate_name -> {field_name: display_name}
PropertyOverride = dict[str, str]
PropertyOverrides = dict[str, PropertyOverride]


class CheckpointStore(Protocol):
    """分块检查点存储接口(由 task 层提供 DB 实现，测试可注入内存实现)。

    存储的是「每块的命名增强」——``{"objects": ObjectOverrides, "properties":
    PropertyOverrides}``，而非整份草稿——结构由证据确定性生成，检查点只缓存 LLM
    的命名结果，用于失败重试时跳过重复调用。
    """

    def load(self, key: str) -> "dict[str, Any] | None": ...

    def save(self, key: str, value: "dict[str, Any]") -> None: ...


# 说明：草稿的「结构」(有哪些对象、哪些属性、如何归属、有哪些关系)完全由证据
# 确定性生成，保证零丢失；LLM 只负责把技术名「提升」为业务名——对象的
# name/display_name/description，以及属性的中文 display_name。属性的英文标识
# 名/数据类型/语义类型/归属对象始终来自证据，LLM 未覆盖或解析失败时属性
# display_name 回退现状(display_name or field_name)，因此不会因 LLM 输出不规范
# 而丢字段。关系(relations)始终规则生成，不经过 LLM。
_LLM_SYSTEM_PROMPT = (
    "你是企业本体建模专家。你的任务包含两部分：\n"
    "1) 把 DataHub 技术元数据中的每个对象(表)提升为业务语义命名，而不是简单搬运表名；\n"
    "2) 为每个对象下的属性(字段)生成中文业务属性名——结合字段名、列注释(description)、"
    "示例数据(sample_values)推断业务含义，而不是把字段名直译成中文。\n\n"
    "输入是一份证据 JSON(含 object_types、properties、relations)。你只需输出 JSON，"
    "包含两个字段：objectTypes(数组)、properties(数组)。relations 仅供你理解对象语义，"
    "不要输出它。\n\n"
    "objectTypes 中每个元素必须包含：\n"
    "- source_ref：原样回传输入中该对象的 source_dataset_urn(务必逐字保留，用于回链，"
    "不可省略或改写)。\n"
    "- name：英文标识名，由 candidate_name 去掉技术前后缀推导而来"
    "(如 payment、refund、finance_reconciliation)。\n"
    "- display_name：中文业务语义名称(如「支付」「退款」「财务对账」)，"
    "由 display_name 去掉技术后缀推导而来。\n"
    "- description：可选，一句话业务解释。\n\n"
    "properties 中每个元素必须包含：\n"
    "- object_source_ref：原样回传该属性所属对象的 source_dataset_urn(与所属"
    "object_types 条目的 source_dataset_urn 一致，逐字保留，用于回链)。\n"
    "- field_name：原样回传输入中的 field_name(逐字保留，不可省略或改写)。\n"
    "- display_name：结合字段名、description、sample_values 推断出的中文业务属性名"
    "(如「支付金额」「退款状态」「客户等级」)。\n\n"
    "示例：\n"
    "- 输入 candidate_name=payment_di_entity, display_name=支付明细日表, "
    "source_dataset_urn=urn:li:dataset:xxx → "
    "{source_ref:'urn:li:dataset:xxx', name:'payment', display_name:'支付'}\n"
    "- 输入 candidate_name=finance_reconciliation_1d_entity, display_name=财务对账1日汇总 → "
    "{name:'finance_reconciliation', display_name:'财务对账'}\n"
    "- 输入 object_candidate_name=customer_entity, source_dataset_urn=urn:li:dataset:yyy, "
    "field_name=lvl_cd, description=null, sample_values=['普通','黄金','铂金'] → "
    "{object_source_ref:'urn:li:dataset:yyy', field_name:'lvl_cd', display_name:'客户等级'}\n"
    "- 输入 field_name=order_amt, description='订单金额(分)' → "
    "{field_name:'order_amt', display_name:'订单金额'}"
)


class OntologyDraftGenerator:
    """生成本体草稿。

    结构(对象/属性/关系)由证据确定性组装，保证零丢失；LLM(非 Mock 模式)对
    对象做业务命名增强，并为属性生成中文业务名。表数或字符预算超限时对
    「命名增强」这一步按表数分批(字符预算兜底细分)，并支持断点续跑。
    """

    def __init__(self, runtime_config=None) -> None:
        timeout = settings.llm_timeout_seconds
        if runtime_config is None:
            self.use_mock = settings.use_mock_llm or not settings.openai_api_key
            self.client = (
                AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    timeout=timeout,
                    http_client=make_async_http_client(),
                )
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
                    http_client=make_async_http_client(),
                )
                if not self.use_mock
                else None
            )
            self.model = runtime_config.model

    async def generate(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> OntologyDraftOutput:
        # Mock 路径：无 LLM，纯确定性命名。
        if self.use_mock:
            return self._build_draft_from_evidence(evidence, {}, {})
        # 分批闸门：表数与字符预算都在限额内才一次拿到命名增强，否则分块。
        fits_table_batch = len(evidence.object_types) <= settings.draft_chunk_table_batch_size
        fits_char_budget = len(self._build_prompt(evidence)) <= settings.llm_context_budget_chars
        if fits_table_batch and fits_char_budget:
            overrides, property_overrides = await self._llm_overrides(evidence)
        else:
            overrides, property_overrides = await self._llm_overrides_chunked(
                evidence, progress_cb, checkpoint
            )
        # 结构始终由全量证据确定性组装：对象/属性/关系一个都不会丢。
        return self._build_draft_from_evidence(evidence, overrides, property_overrides)

    # ------------------------------------------------------------------
    # 确定性组装(零丢失核心)
    # ------------------------------------------------------------------
    def _build_draft_from_evidence(
        self,
        evidence: EvidenceBundle,
        overrides: ObjectOverrides | None = None,
        property_overrides: PropertyOverrides | None = None,
    ) -> OntologyDraftOutput:
        """从证据确定性组装完整草稿；overrides/property_overrides 提供对象与
        属性的业务命名增强。

        每个对象、每个属性、每条关系都来自证据，overrides 缺失或未匹配时回退到
        确定性命名(refine)；property_overrides 缺失或未匹配时属性 display_name
        回退现状(display_name or field_name)。因此结构完整、必填字段齐全，
        不存在丢失或校验失败。
        """
        overrides = overrides or {}
        property_overrides = property_overrides or {}

        name_map: dict[str, str] = {}
        display_map: dict[str, str] = {}
        desc_map: dict[str, str | None] = {}
        for ot in evidence.object_types:
            ov = overrides.get(ot.candidate_name) or {}
            ov_name = (ov.get("name") or "").strip()
            ov_display = (ov.get("display_name") or "").strip()
            ov_desc = ov.get("description")
            name_map[ot.candidate_name] = ov_name or self._refine_identifier_name(
                ot.candidate_name
            )
            display_map[ot.candidate_name] = ov_display or self._refine_semantic_name(
                ot.display_name, ot.candidate_name
            )
            desc_map[ot.candidate_name] = (
                ov_desc if (ov_desc and str(ov_desc).strip()) else ot.description
            )

        def obj_name(candidate: str) -> str:
            return name_map.get(candidate) or self._refine_identifier_name(candidate)

        object_types = [
            DraftObjectType(
                name=name_map[ot.candidate_name],
                display_name=display_map[ot.candidate_name],
                description=desc_map[ot.candidate_name],
                source_ref=ot.source_dataset_urn,
                confidence=ot.confidence,
            )
            for ot in evidence.object_types
        ]

        def property_display_name(item) -> str:
            ov_display = (
                property_overrides.get(item.object_candidate_name, {}).get(
                    item.field_name
                )
                or ""
            ).strip()
            return ov_display or item.display_name or item.field_name

        properties = [
            DraftProperty(
                object_type_name=obj_name(item.object_candidate_name),
                name=self._refine_property_name(item.display_name, item.field_name),
                display_name=property_display_name(item),
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
                source_object_type_name=obj_name(item.source_object),
                target_object_type_name=obj_name(item.target_object),
                cardinality=self._normalize_cardinality(item.cardinality),
                structure_type=item.structure_type
                or infer_relation_structure_type(item.description),
                source_evidence=item.description
                or (", ".join(item.evidence_refs) if item.evidence_refs else None),
                confidence=item.confidence,
            )
            for item in evidence.relations
        ]

        return OntologyDraftOutput(
            object_types=object_types,
            properties=properties,
            relation_types=relation_types,
            business_logics=[],
            business_logic_object_bindings=[],
            business_logic_property_bindings=[],
            evidence_refs=self._collect_evidence_refs(evidence),
        )

    # ------------------------------------------------------------------
    # LLM 对象命名 + 属性中文名增强
    # ------------------------------------------------------------------
    async def _llm_overrides(
        self, evidence: EvidenceBundle
    ) -> tuple[ObjectOverrides, PropertyOverrides]:
        """单次调用：拿到全量对象的命名增强与属性的中文名增强。"""
        raw = await self._call_llm_objects(evidence)
        return (
            self._parse_object_overrides(raw, evidence),
            self._parse_property_overrides(raw, evidence),
        )

    async def _llm_overrides_chunked(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> tuple[ObjectOverrides, PropertyOverrides]:
        """超预算(表数或字符)时按表分批拿命名/属性中文名增强。

        分块单元本身极小(仅命名增强，不含完整草稿)，结构随后由全量证据确定性
        组装。断点续跑：每块结果按内容哈希落库，失败重试跳过已完成块。
        单块 LLM 失败不吞噬——异常向上抛出由任务层标记失败并可重试续跑。
        """
        sub_bundles, _cross = split_evidence(
            evidence,
            settings.llm_context_budget_chars,
            settings.draft_chunk_table_batch_size,
        )
        total_steps = len(sub_bundles)
        logger.info(
            "draft chunked enrichment: sub_bundles=%d", len(sub_bundles)
        )

        semaphore = asyncio.Semaphore(max(1, settings.draft_chunk_max_concurrency))
        progress_lock = asyncio.Lock()
        checkpoint_lock = asyncio.Lock()
        completed = 0

        async def _advance() -> None:
            nonlocal completed
            if progress_cb is not None:
                async with progress_lock:
                    completed += 1
                    await progress_cb(completed, total_steps)

        async def run_chunk(sub: EvidenceBundle) -> dict[str, Any]:
            key = chunk_key(self._build_prompt(sub))
            if checkpoint is not None:
                cached = checkpoint.load(key)
                if cached is not None:
                    logger.info("draft chunk cache hit key=%s", key[:12])
                    await _advance()
                    return cached
            async with semaphore:
                raw = await self._call_llm_objects(sub)
            result = {
                "objects": self._parse_object_overrides(raw, sub),
                "properties": self._parse_property_overrides(raw, sub),
            }
            if checkpoint is not None:
                async with checkpoint_lock:
                    checkpoint.save(key, result)
            await _advance()
            return result

        results = await asyncio.gather(*(run_chunk(sub) for sub in sub_bundles))
        merged_objects: ObjectOverrides = {}
        merged_properties: PropertyOverrides = {}
        for result in results:
            merged_objects.update(result.get("objects") or {})
            for candidate, field_map in (result.get("properties") or {}).items():
                merged_properties.setdefault(candidate, {}).update(field_map)
        return merged_objects, merged_properties

    async def _call_llm_objects(self, evidence: EvidenceBundle) -> dict:
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
        return json.loads(content)

    @staticmethod
    def _build_candidate_lookup(evidence: EvidenceBundle) -> dict[str, Any]:
        """构建对象回链用的三级查找表：source_ref → candidate → refine 后同名。"""
        refined_to_candidate: dict[str, str] = {}
        for ot in evidence.object_types:
            refined_to_candidate.setdefault(
                OntologyDraftGenerator._refine_identifier_name(ot.candidate_name),
                ot.candidate_name,
            )
        return {
            "dataset_to_candidate": {
                ot.source_dataset_urn: ot.candidate_name for ot in evidence.object_types
            },
            "candidate_set": {ot.candidate_name for ot in evidence.object_types},
            "refined_to_candidate": refined_to_candidate,
        }

    @classmethod
    def _resolve_candidate(cls, obj: dict, lookup: dict[str, Any]) -> str | None:
        """按 source_ref → candidate_name → refine 后同名 三级兜底回链到 candidate_name。

        任意一路命中即用；都不命中返回 None(调用方据此跳过该条增强，结构不丢)。
        """
        src = cls._first_present(
            obj,
            [
                "source_ref",
                "sourceRef",
                "source_dataset_urn",
                "object_source_ref",
                "objectSourceRef",
            ],
        )
        if src and src in lookup["dataset_to_candidate"]:
            return lookup["dataset_to_candidate"][src]
        cand = cls._first_present(obj, ["candidate_name"])
        if cand and cand in lookup["candidate_set"]:
            return cand
        nm = cls._first_present(obj, ["name"])
        if nm and nm in lookup["refined_to_candidate"]:
            return lookup["refined_to_candidate"][nm]
        return None

    def _parse_object_overrides(
        self, raw: dict, evidence: EvidenceBundle
    ) -> ObjectOverrides:
        """把 LLM 返回的对象数组回链到证据 candidate_name，得到命名增强字典。

        回链多路兜底：source_ref(数据集 URN) → candidate_name → 与 refine 后同名。
        任意一路命中即用；都不命中则跳过该对象的增强(结构仍由证据保证，不丢)。
        """
        objects = raw.get("object_types")
        if not isinstance(objects, list):
            objects = raw.get("objectTypes")
        if not isinstance(objects, list):
            return {}

        lookup = self._build_candidate_lookup(evidence)

        overrides: ObjectOverrides = {}
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            candidate = self._resolve_candidate(obj, lookup)
            if candidate is None:
                continue
            description = obj.get("description")
            overrides[candidate] = {
                "name": self._first_present(obj, ["name"]),
                "display_name": self._first_present(
                    obj, ["display_name", "displayName"]
                ),
                "description": description
                if isinstance(description, str) and description.strip()
                else None,
            }
        return overrides

    def _parse_property_overrides(
        self, raw: dict, evidence: EvidenceBundle
    ) -> PropertyOverrides:
        """把 LLM 返回的属性数组回链到证据 (candidate_name, field_name)，得到中文名增强。

        所属对象的回链复用与对象增强相同的三级兜底；field_name 必须与证据中该
        对象下实际存在的字段完全一致才写入，避免 LLM 编造字段名污染结果。任意
        一步未命中则跳过该条(属性结构仍由证据保证，display_name 回退现状)。
        """
        items = raw.get("properties")
        if not isinstance(items, list):
            return {}

        lookup = self._build_candidate_lookup(evidence)
        fields_by_object: dict[str, set[str]] = {}
        for prop in evidence.properties:
            fields_by_object.setdefault(prop.object_candidate_name, set()).add(
                prop.field_name
            )

        overrides: PropertyOverrides = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate = self._resolve_candidate(item, lookup)
            if candidate is None:
                continue
            field_name = self._first_present(item, ["field_name", "fieldName"])
            if not field_name or field_name not in fields_by_object.get(candidate, set()):
                continue
            display_name = self._first_present(
                item, ["display_name", "displayName"]
            )
            if not display_name:
                continue
            overrides.setdefault(candidate, {})[field_name] = display_name
        return overrides

    @staticmethod
    def _first_present(data: dict, keys: list[str]) -> str | None:
        """返回 data 中首个非空(去空格后)键值，按 keys 顺序。"""
        for key in keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    # ------------------------------------------------------------------
    # 命名启发式与工具
    # ------------------------------------------------------------------
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

        Strips technical suffixes (_entity, _di, _1d, etc.) to produce
        a business-friendly English name. DataHub layer prefixes are preserved.
        """
        name = candidate_name
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

    def _build_prompt(self, evidence: EvidenceBundle) -> str:
        payload = evidence.model_dump(exclude={"business_logics"})
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

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
