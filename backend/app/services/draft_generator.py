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
from app.services.relation_terms import compact_relation_term, validate_relation_term
from app.services.relation_structure import infer_relation_structure_type
from app.services.common import make_async_http_client
from app.services.draft_checkpoint import chunk_key
from app.services.evidence_chunker import split_evidence, split_relations

logger = logging.getLogger(__name__)

# 进度回调：(已完成步数, 总步数) -> None，用于分块生成时逐块回报进度。
ProgressCallback = Callable[[int, int], Awaitable[None]]

# 每个对象的业务命名增强：candidate_name -> {name, display_name, description}
ObjectOverride = dict[str, str | None]
ObjectOverrides = dict[str, ObjectOverride]

# 每个对象下属性的中文业务名增强：candidate_name -> {field_name: display_name}
PropertyOverride = dict[str, str]
PropertyOverrides = dict[str, PropertyOverride]

# 每条关系的业务语义名增强：relation name -> display_name
RelationOverrides = dict[str, str]


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
# name/display_name/description，属性的中文 display_name，以及关系的业务语义
# display_name。属性的英文标识名/数据类型/语义类型/归属对象、关系的两端对象/
# 基数/结构类型始终来自证据，LLM 未覆盖或解析失败时属性 display_name 回退现状
# (display_name or field_name)、关系 display_name 回退规则生成的默认词
# (infer_relation_term)，因此不会因 LLM 输出不规范而丢字段。
_LLM_SYSTEM_PROMPT = (
    "你是企业本体建模专家。你的任务包含三部分：\n"
    "1) 把 DataHub 技术元数据中的每个对象(表)提升为业务语义命名，而不是简单搬运表名；\n"
    "2) 为每个对象下的属性(字段)生成中文业务属性名——结合字段名、列注释(description)、"
    "示例数据(sample_values)推断业务含义，而不是把字段名直译成中文；\n"
    "3) 为每条关系(relations)生成有业务含义的关系名——结合两端对象的业务语义"
    "(source_object/target_object，可参考你自己给出的 objectTypes 命名)与"
    "description 中的证据(外键字段、血缘加工说明等)推断。血缘类关系(两端"
    "无外键，只有「血缘：A 加工至 B」这类描述)应主要依据 target 对象的业务"
    "含义命名——它是产出物，如目标是对账结果就写「对账生成」，目标是统计"
    "报表就写「统计汇总」，而不是不加区分地写「派生」「关联」「加工」「处理」"
    "这类无信息量、体现不出产出物是什么的默认词。\n\n"
    "输入是一份证据 JSON(含 object_types、properties、relations)。你需要输出 JSON，"
    "包含三个字段：objectTypes(数组)、properties(数组)、relations(数组)。\n\n"
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
    "relations 中每个元素必须包含：\n"
    "- name：原样回传输入中该关系的 name(逐字保留，用于回链，不可省略或改写)。\n"
    "- display_name：结合两端对象业务含义与 description 证据推断出的简短业务关系词"
    "(不超过 8 个汉字，只写动词/短语，不写完整句子，如「支付」「退款」「审核」"
    "「结算生成」「统计汇总」「对账生成」「清洗加工」)，须体现该关系具体产出"
    "什么/做什么，避免千篇一律地写「派生」「关联」「加工」「处理」这类"
    "不同关系都能套用、看不出差异的默认词。\n\n"
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
    "{field_name:'order_amt', display_name:'订单金额'}\n"
    "- 输入 relations 中一条：name=payment_to_order, source_object=payment_di_entity, "
    "target_object=order_di_entity, structure_type=foreign_key, "
    "description='支付明细日表 通过外键 order_id 关联 order_di_entity' → "
    "{name:'payment_to_order', display_name:'支付'}\n"
    "- 输入 relations 中一条：name=order_feeds_settlement, structure_type=fact_table, "
    "description='血缘：订单明细 加工至 结算汇总' → "
    "{name:'order_feeds_settlement', display_name:'结算生成'}"
)

# 关系命名分块流水线专用的系统提示：与对象/属性命名流水线并发独立执行，
# 输入只含 relations(待命名)与两端对象的概要 object_types(仅供业务背景参考，
# 不要求为其命名)，因此提示词只覆盖关系命名这一件事。
_LLM_RELATION_SYSTEM_PROMPT = (
    "你是企业本体建模专家。你的任务是为每条关系(relations)生成有业务含义的关系名，"
    "结合两端对象的业务语义(参考 object_types 中的 display_name/description)与该关系"
    "自身 description 中的证据(外键字段、血缘加工说明等)推断。\n\n"
    "两类关系的命名侧重点不同：\n"
    "- 有外键(structure_type=foreign_key)的关系：从 description 里的外键字段"
    "语义判断两端是什么业务动作/归属关系，如「支付」「退款」「审核」「属于」"
    "「包含」。\n"
    "- 血缘类关系(description 形如「血缘：A 加工至 B」，没有外键字段)：应主要"
    "依据 target_object 的业务含义命名——它是产出物，如目标是对账结果就写"
    "「对账生成」，目标是统计报表就写「统计汇总」，目标是结算数据就写"
    "「结算生成」。\n\n"
    "无论哪一类，都不要写「派生」「关联」「加工」「处理」这类无信息量、看不出"
    "两端具体业务差异、随便哪条关系都能套用的默认词。\n\n"
    "输入是一份证据 JSON，包含 object_types(关系两端对象的业务背景，无需为其命名，"
    "也不会被使用)与 relations(需要命名的关系列表)。你需要输出 JSON，只包含一个"
    "字段：relations(数组)。\n\n"
    "relations 中每个元素必须包含：\n"
    "- name：原样回传输入中该关系的 name(逐字保留，用于回链，不可省略或改写)。\n"
    "- display_name：简短业务关系词(不超过 8 个汉字，只写动词/短语，不写完整句子，"
    "如「支付」「退款」「审核」「结算生成」「统计汇总」「对账生成」「清洗加工」)，"
    "须体现该关系具体产出什么/做什么。\n\n"
    "示例：\n"
    "- 输入 relations 中一条：name=payment_to_order, source_object=payment_di_entity, "
    "target_object=order_di_entity, structure_type=foreign_key, "
    "description='支付明细日表 通过外键 order_id 关联 order_di_entity' → "
    "{name:'payment_to_order', display_name:'支付'}\n"
    "- 输入 relations 中一条：name=order_feeds_settlement, structure_type=fact_table, "
    "description='血缘：订单明细 加工至 结算汇总' → "
    "{name:'order_feeds_settlement', display_name:'结算生成'}\n"
    "- 输入 relations 中一条：name=order_feeds_reconciliation, structure_type=other, "
    "description='血缘：订单支付流水 加工至 财务对账结果' → "
    "{name:'order_feeds_reconciliation', display_name:'对账生成'}"
)


class OntologyDraftGenerator:
    """生成本体草稿。

    结构(对象/属性/关系)由证据确定性组装，保证零丢失；LLM(非 Mock 模式)对
    对象做业务命名增强，并为属性生成中文业务名。表数或字符预算超限时对
    「命名增强」这一步按表数分批(字符预算兜底细分)，并支持断点续跑。
    """

    def __init__(
        self,
        runtime_config=None,
        object_chunk_concurrency: int | None = None,
        relation_chunk_concurrency: int | None = None,
    ) -> None:
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
        # 分块流水线的并发度：可由设置页动态调整(见 SettingsService.get_draft_generation_runtime)，
        # 未显式传入时回退到静态环境配置，保持测试里 OntologyDraftGenerator() 的直接构造方式不变。
        self.object_chunk_concurrency = (
            object_chunk_concurrency
            if object_chunk_concurrency is not None
            else settings.draft_chunk_max_concurrency
        )
        self.relation_chunk_concurrency = (
            relation_chunk_concurrency
            if relation_chunk_concurrency is not None
            else settings.draft_relation_chunk_max_concurrency
        )

    async def generate(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> OntologyDraftOutput:
        # Mock 路径：无 LLM，纯确定性命名。
        if self.use_mock:
            return self._build_draft_from_evidence(evidence, {}, {}, {})
        # 分批闸门：表数与字符预算都在限额内才一次拿到命名增强，否则分块。
        fits_table_batch = len(evidence.object_types) <= settings.draft_chunk_table_batch_size
        fits_char_budget = len(self._build_prompt(evidence)) <= settings.llm_context_budget_chars
        if fits_table_batch and fits_char_budget:
            overrides, property_overrides, relation_overrides = await self._llm_overrides(
                evidence
            )
        else:
            overrides, property_overrides, relation_overrides = (
                await self._llm_overrides_chunked(evidence, progress_cb, checkpoint)
            )
        # 结构始终由全量证据确定性组装：对象/属性/关系一个都不会丢。
        return self._build_draft_from_evidence(
            evidence, overrides, property_overrides, relation_overrides
        )

    # ------------------------------------------------------------------
    # 确定性组装(零丢失核心)
    # ------------------------------------------------------------------
    def _build_draft_from_evidence(
        self,
        evidence: EvidenceBundle,
        overrides: ObjectOverrides | None = None,
        property_overrides: PropertyOverrides | None = None,
        relation_overrides: RelationOverrides | None = None,
    ) -> OntologyDraftOutput:
        """从证据确定性组装完整草稿；overrides/property_overrides/relation_overrides
        提供对象、属性与关系的业务命名增强。

        每个对象、每个属性、每条关系都来自证据，overrides 缺失或未匹配时回退到
        确定性命名(refine)；property_overrides 缺失或未匹配时属性 display_name
        回退现状(display_name or field_name)；relation_overrides 缺失、未匹配或未
        通过 validate_relation_term 校验时回退规则生成的默认词(compact_relation_term)。
        因此结构完整、必填字段齐全，不存在丢失或校验失败。
        """
        overrides = overrides or {}
        property_overrides = property_overrides or {}
        relation_overrides = relation_overrides or {}

        object_types, properties, name_map = self._build_object_types_from_evidence(
            evidence, overrides, property_overrides
        )

        def obj_name(candidate: str) -> str:
            return name_map.get(candidate) or self._refine_identifier_name(candidate)

        relation_types = self._build_relation_types_from_evidence(
            evidence, relation_overrides, resolve_object_name=obj_name
        )

        return OntologyDraftOutput(
            object_types=object_types,
            properties=properties,
            relation_types=relation_types,
            business_logics=[],
            business_logic_object_bindings=[],
            business_logic_property_bindings=[],
            evidence_refs=self._collect_evidence_refs(evidence),
        )

    def _build_object_types_from_evidence(
        self,
        evidence: EvidenceBundle,
        overrides: ObjectOverrides,
        property_overrides: PropertyOverrides,
    ) -> tuple[list[DraftObjectType], list[DraftProperty], dict[str, str]]:
        """确定性组装对象与属性；返回 (object_types, properties, candidate→name 映射)。

        映射供关系组装环节按 candidate_name 解析 obj_name，也供「仅生成业务
        关系」场景在不重新命名对象的前提下按 source_dataset_urn 回链已入库对象。
        """
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

        def obj_name(candidate: str) -> str:
            return name_map.get(candidate) or self._refine_identifier_name(candidate)

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

        return object_types, properties, name_map

    def _build_relation_types_from_evidence(
        self,
        evidence: EvidenceBundle,
        relation_overrides: RelationOverrides,
        resolve_object_name: Callable[[str], str] | None = None,
    ) -> list[DraftRelationType]:
        """确定性组装关系。

        ``resolve_object_name`` 把证据里的 candidate_name(如 payment_di_entity)
        映射为 DraftRelationType 要求的 source/target 对象名：完整草稿场景传入
        按 LLM 命名增强解析的 ``obj_name``；「仅生成业务关系」场景不重新命名
        对象，默认原样回传 candidate_name，由调用方按 source_dataset_urn 回链
        已入库的 ObjectType，避免与对象命名流水线的输出产生不一致。
        """
        resolve = resolve_object_name or (lambda candidate: candidate)

        def relation_display_name(item) -> str:
            ov_display = (relation_overrides.get(item.name) or "").strip()
            if ov_display and validate_relation_term(ov_display) is None:
                return ov_display
            return compact_relation_term(item.display_name)

        return [
            DraftRelationType(
                name=item.name,
                display_name=relation_display_name(item),
                description=item.description,
                source_object_type_name=resolve(item.source_object),
                target_object_type_name=resolve(item.target_object),
                cardinality=self._normalize_cardinality(item.cardinality),
                structure_type=item.structure_type
                or infer_relation_structure_type(item.description),
                source_evidence=item.description
                or (", ".join(item.evidence_refs) if item.evidence_refs else None),
                confidence=item.confidence,
            )
            for item in evidence.relations
        ]

    # ------------------------------------------------------------------
    # LLM 对象命名 + 属性中文名增强 + 关系业务名增强
    # ------------------------------------------------------------------
    async def _llm_overrides(
        self, evidence: EvidenceBundle
    ) -> tuple[ObjectOverrides, PropertyOverrides, RelationOverrides]:
        """单次调用：拿到全量对象的命名增强、属性的中文名增强与关系的业务名增强。"""
        raw = await self._call_llm_objects(evidence)
        return (
            self._parse_object_overrides(raw, evidence),
            self._parse_property_overrides(raw, evidence),
            self._parse_relation_overrides(raw, evidence),
        )

    async def _llm_overrides_chunked(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> tuple[ObjectOverrides, PropertyOverrides, RelationOverrides]:
        """超预算(表数、字符预算或关系数量)时，对象/属性命名与关系命名两条流水线
        完全独立分块、并发执行、独立断点续跑：
        - 对象流水线：``split_evidence`` 按表分块，关系字段清空(关系交由关系
          流水线单独处理，避免同一条关系被两条流水线重复命名、浪费 token)。
        - 关系流水线：``split_relations`` 对*全部*关系分块(含两端对象落在不同
          对象子包的跨块关系)，附带两端对象概要作为业务背景，不等待、不依赖
          对象流水线的输出。

        两条流水线各自用独立的信号量控制并发度，二者又通过外层 gather 并发
        执行，整体并发度可达两者之和，提升生成速度。每块结果按内容哈希
        (含流水线前缀，避免与另一流水线的块误撞键)落库；失败重试时，已完成
        的对象块或关系块直接复用缓存，不会因另一条流水线失败而被拖累重跑。
        单块 LLM 失败不吞噬——异常向上抛出由任务层标记失败并可重试续跑。

        与「仅生成业务对象」(``generate_object_types``)、「仅生成业务关系」
        (``generate_relations``)共用 ``_run_object_chunks``/``_run_relation_chunks``
        两个分块执行原语，区别只在于这里的进度总数覆盖两条流水线之和。
        """
        object_sub_bundles = self._split_object_chunks(evidence)
        relation_sub_bundles = split_relations(
            evidence,
            settings.llm_context_budget_chars,
            settings.draft_chunk_relation_batch_size,
        )

        total_steps = len(object_sub_bundles) + len(relation_sub_bundles)
        logger.info(
            "draft chunked enrichment: object_chunks=%d relation_chunks=%d",
            len(object_sub_bundles),
            len(relation_sub_bundles),
        )

        advance = self._make_progress_advancer(progress_cb, total_steps)

        (merged_objects, merged_properties), merged_relations = await asyncio.gather(
            self._run_object_chunks(object_sub_bundles, checkpoint, advance),
            self._run_relation_chunks(relation_sub_bundles, checkpoint, advance),
        )
        return merged_objects, merged_properties, merged_relations

    def _split_object_chunks(self, evidence: EvidenceBundle) -> list[EvidenceBundle]:
        """按表分块，清空关系字段(关系交由关系流水线单独处理)。"""
        object_sub_bundles, _cross = split_evidence(
            evidence,
            settings.llm_context_budget_chars,
            settings.draft_chunk_table_batch_size,
        )
        return [
            EvidenceBundle(
                object_types=sub.object_types, properties=sub.properties, relations=[]
            )
            for sub in object_sub_bundles
        ]

    @staticmethod
    def _make_progress_advancer(
        progress_cb: ProgressCallback | None, total_steps: int
    ) -> Callable[[], Awaitable[None]]:
        """构造一个可在并发块任务间共享计数的进度推进回调。"""
        progress_lock = asyncio.Lock()
        completed = 0

        async def _advance() -> None:
            nonlocal completed
            if progress_cb is not None:
                async with progress_lock:
                    completed += 1
                    await progress_cb(completed, total_steps)

        return _advance

    async def _run_object_chunks(
        self,
        object_sub_bundles: list[EvidenceBundle],
        checkpoint: CheckpointStore | None,
        advance: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[ObjectOverrides, PropertyOverrides]:
        """并发执行对象/属性命名分块，按内容哈希缓存，返回合并后的增强字典。"""
        checkpoint_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(max(1, self.object_chunk_concurrency))

        async def run_chunk(sub: EvidenceBundle) -> dict[str, Any]:
            key = self._object_chunk_key(self._build_prompt(sub))
            if checkpoint is not None:
                cached = checkpoint.load(key)
                if cached is not None:
                    logger.info("draft object chunk cache hit key=%s", key[:12])
                    if advance is not None:
                        await advance()
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
            if advance is not None:
                await advance()
            return result

        results = await asyncio.gather(*(run_chunk(sub) for sub in object_sub_bundles))

        merged_objects: ObjectOverrides = {}
        merged_properties: PropertyOverrides = {}
        for result in results:
            merged_objects.update(result.get("objects") or {})
            for candidate, field_map in (result.get("properties") or {}).items():
                merged_properties.setdefault(candidate, {}).update(field_map)
        return merged_objects, merged_properties

    async def _run_relation_chunks(
        self,
        relation_sub_bundles: list[EvidenceBundle],
        checkpoint: CheckpointStore | None,
        advance: Callable[[], Awaitable[None]] | None = None,
    ) -> RelationOverrides:
        """并发执行关系命名分块，按内容哈希缓存，返回合并后的关系增强字典。"""
        checkpoint_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(max(1, self.relation_chunk_concurrency))

        async def run_chunk(sub: EvidenceBundle) -> dict[str, Any]:
            key = self._relation_chunk_key(self._build_prompt(sub))
            if checkpoint is not None:
                cached = checkpoint.load(key)
                if cached is not None:
                    logger.info("draft relation chunk cache hit key=%s", key[:12])
                    if advance is not None:
                        await advance()
                    return cached
            async with semaphore:
                raw = await self._call_llm_relations(sub)
            result = {"relations": self._parse_relation_overrides(raw, sub)}
            if checkpoint is not None:
                async with checkpoint_lock:
                    checkpoint.save(key, result)
            if advance is not None:
                await advance()
            return result

        results = await asyncio.gather(
            *(run_chunk(sub) for sub in relation_sub_bundles)
        )
        merged_relations: RelationOverrides = {}
        for result in results:
            merged_relations.update(result.get("relations") or {})
        return merged_relations

    async def generate_object_types(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> tuple[list[DraftObjectType], list[DraftProperty]]:
        """仅生成业务对象+属性的命名增强并组装(不涉及关系)。

        供「仅生成业务对象」入口使用，可与 ``generate_relations`` 完全并行——
        两者互不等待、各自独立分块与并发，契合「对象/关系分开触发」的诉求。
        """
        if self.use_mock:
            overrides, property_overrides = {}, {}
        else:
            fits_table_batch = (
                len(evidence.object_types) <= settings.draft_chunk_table_batch_size
            )
            fits_char_budget = (
                len(self._build_prompt(evidence)) <= settings.llm_context_budget_chars
            )
            if fits_table_batch and fits_char_budget:
                raw = await self._call_llm_objects(evidence)
                overrides = self._parse_object_overrides(raw, evidence)
                property_overrides = self._parse_property_overrides(raw, evidence)
            else:
                object_sub_bundles = self._split_object_chunks(evidence)
                advance = self._make_progress_advancer(
                    progress_cb, len(object_sub_bundles)
                )
                overrides, property_overrides = await self._run_object_chunks(
                    object_sub_bundles, checkpoint, advance
                )
        object_types, properties, _name_map = self._build_object_types_from_evidence(
            evidence, overrides, property_overrides
        )
        return object_types, properties

    async def generate_relations(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> list[DraftRelationType]:
        """仅生成业务关系的命名增强并组装(不涉及对象/属性)。

        ``evidence.object_types`` 仅作为关系命名的业务背景参考(与
        ``_LLM_RELATION_SYSTEM_PROMPT`` 一致)，不会被重新命名——返回的
        ``DraftRelationType.source_object_type_name``/``target_object_type_name``
        原样是证据 candidate_name，调用方需按 source_dataset_urn 回链已入库的
        ObjectType，而不是假设这里产出了新的对象命名。
        """
        if self.use_mock:
            relation_overrides = {}
        else:
            fits_relation_batch = (
                len(evidence.relations) <= settings.draft_chunk_relation_batch_size
            )
            fits_char_budget = (
                len(self._build_prompt(evidence)) <= settings.llm_context_budget_chars
            )
            if fits_relation_batch and fits_char_budget:
                raw = await self._call_llm_relations(evidence)
                relation_overrides = self._parse_relation_overrides(raw, evidence)
            else:
                relation_sub_bundles = split_relations(
                    evidence,
                    settings.llm_context_budget_chars,
                    settings.draft_chunk_relation_batch_size,
                )
                advance = self._make_progress_advancer(
                    progress_cb, len(relation_sub_bundles)
                )
                relation_overrides = await self._run_relation_chunks(
                    relation_sub_bundles, checkpoint, advance
                )
        return self._build_relation_types_from_evidence(evidence, relation_overrides)

    @staticmethod
    def _object_chunk_key(prompt: str) -> str:
        return chunk_key(f"object:{prompt}")

    @staticmethod
    def _relation_chunk_key(prompt: str) -> str:
        return chunk_key(f"relation:{prompt}")

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

    async def _call_llm_relations(self, evidence: EvidenceBundle) -> dict:
        prompt = self._build_prompt(evidence)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _LLM_RELATION_SYSTEM_PROMPT},
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

    def _parse_relation_overrides(
        self, raw: dict, evidence: EvidenceBundle
    ) -> RelationOverrides:
        """把 LLM 返回的关系数组回链到证据 relation name，得到业务名增强。

        name 必须与证据中实际存在的关系 name 完全一致才写入，避免 LLM 编造关系
        污染结果；未命中或校验失败的条目跳过(关系 display_name 回退规则生成的
        默认词，见 relation_display_name 中的 validate_relation_term 校验)。
        """
        items = raw.get("relations")
        if not isinstance(items, list):
            return {}

        relation_names = {rel.name for rel in evidence.relations}

        overrides: RelationOverrides = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = self._first_present(item, ["name"])
            if not name or name not in relation_names:
                continue
            display_name = self._first_present(item, ["display_name", "displayName"])
            if not display_name:
                continue
            overrides[name] = display_name
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
