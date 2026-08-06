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
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_TECHNICAL,
)
from app.services.common import make_async_http_client
from app.services.object_naming import dedupe_object_names
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

# 每个对象的 LLM 角色否决：candidate_name -> {"role": str, "reason": str|None}。
# LLM 在命名同时给出「是否真实业务实体」的语义判断，用于修正结构启发式
# 漏判的技术/系统表（如 auth/session/config）；零额外 LLM 调用（复用对象命名调用）。
RoleOverride = dict[str, str | None]
RoleOverrides = dict[str, RoleOverride]


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
    "3) 为每条关系(relations)生成有业务含义的关系名。关系名是**三元组谓词**，"
    "要能读成「源对象 [关系名] 目标对象」一句话(如「订单 属于 客户」)，而非"
    "「结算生成」这种「产出物+动作」名词。区分两个关系范畴：\n"
    "  • 业务关联关系(有外键，structure_type=foreign_key)：两个业务实体之间的"
    "真实业务事实，从外键字段语义判断，如「属于」「包含」「下单」「引用」「拥有」。"
    "若 description 形如「X 引用主数据 Y」，说明是单据引用主数据，按目标取"
    "「位于」「属于」「采用」「引用」这类引用谓词。\n"
    "  • 溯源/派生关系(血缘，description 形如「血缘：A 加工至 B」，无外键)：这属于"
    "数据溯源(对齐 PROV-O 的 wasDerivedFrom)，不是业务事实。两端都是业务对象，优先"
    "判业务语义：源与目标是**同一业务实体的生命周期阶段**(如 潜客商机→商机、"
    "线索→商机、客户→潜在客户)写「转化」；目标是源的**明细/子表**(如 订单→订单明细)"
    "写「包含」；否则按目标变换类型取**方向前向的谓词**：目标是汇总就写「汇总为」，"
    "对账写「对账为」，统计/报表写「统计为」，结算写「结算为」，标签/画像写「刻画」，"
    "实在拿不准才写「派生出」；不要写「生成」「加工」「派生」「关联」「处理」这类无区分度的默认词。\n\n"
    "输入是一份证据 JSON(含 object_types、properties、relations)。你需要输出 JSON，"
    "包含三个字段：objectTypes(数组)、properties(数组)、relations(数组)。\n\n"
    "objectTypes 中每个元素必须包含：\n"
    "- source_ref：原样回传输入中该对象的 source_dataset_urn(务必逐字保留，用于回链，"
    "不可省略或改写)。\n"
    "- name：英文标识名，由 candidate_name 去掉技术前后缀推导而来"
    "(如 payment、refund、finance_reconciliation)。\n"
    "- display_name：中文业务语义名称(如「支付」「退款」「财务对账」)，"
    "由 display_name 去掉技术后缀推导而来。\n"
    "- description：可选，一句话业务解释。\n"
    "- role_hint：可选，对该表是否为真实业务实体的语义判断。**只依据证据**"
    "(表名/列名、列注释 description、示例数据 sample_values、血缘 description)判断，"
    "不要依据你自己拟的 name/display_name。判据是：**数据治理/业务建模是否会把它"
    "当作一个需要被治理、被业务流程引用的核心业务概念**——不要被『有主键+属性、"
    "结构上长得像实体』迷惑，很多展示内容/配置表结构上也像实体但并非业务对象。"
    "以下几类都填 role_hint='technical'(业务人员不会把它当作一个业务概念)：\n"
    "  • 技术/系统表：鉴权 auth、会话 session、配置 config、日志 log、任务调度、"
    "元数据、临时表；\n"
    "  • 站点/运营内容(CMS)：关于我们、团队成员/团队介绍、隐私政策、FAQ、公告、"
    "Banner/文案等——是对外展示的内容文案，不是受治理的业务实体；\n"
    "  • 用户偏好/参数：用户设置、系统参数、字典/枚举配置——是配置项而非业务实体。\n"
    "另有一类**业务事实/关系表**填 role_hint='bridge'：整表记录的是一次**业务动作/事件/"
    "流水**，而不是一类业务『东西』——如维修、清算、理赔、巡检、派工、报工、交易、调整、"
    "结算、过账、核销、收付款、审批、盘点、出入库、退换货等。判据是**每行是一次发生"
    "(occurrence)而非一个实体实例**：真正的业务对象是它引用的键(设备/客户/账户/工单…)，"
    "这张表本身是连接这些键的业务事实。某父表的**明细/子表**(订单明细、支付流水)同样"
    "填 'bridge'。注意与派生汇总区分：血缘下游的统计/汇总结果表不是业务事实，勿填 bridge。\n"
    "当它确实是一个真实世界业务实体(客户/订单/商品等，有独立业务身份、被业务流程"
    "引用)时填 role_hint='business_object'；拿不准则省略。可附 role_reason 简述理由。\n"
    "- evidence_gap：可选，一句话说明你判断该表时**缺了什么证据**(如"
    "「无列注释，仅凭表名推断」「未开样例，类型不明」)，供人工按需补证据。\n\n"
    "properties 中每个元素必须包含：\n"
    "- object_source_ref：原样回传该属性所属对象的 source_dataset_urn(与所属"
    "object_types 条目的 source_dataset_urn 一致，逐字保留，用于回链)。\n"
    "- field_name：原样回传输入中的 field_name(逐字保留，不可省略或改写)。\n"
    "- display_name：结合字段名、description、sample_values 推断出的中文业务属性名"
    "(如「支付金额」「退款状态」「客户等级」)。\n\n"
    "relations 中每个元素必须包含：\n"
    "- name：原样回传输入中该关系的 name(逐字保留，用于回链，不可省略或改写)。\n"
    "- display_name：结合两端对象业务含义与 description 证据推断出的简短关系谓词"
    "(不超过 8 个汉字，只写动词/短语，不写完整句子，且能读成「源 [谓词] 目标」)："
    "外键关系如「属于」「包含」「下单」「引用」「位于」；血缘/溯源关系如「转化」「包含」"
    "「汇总为」「对账为」「统计为」「结算为」「刻画」「派生出」。避免千篇一律地写"
    "「生成」「派生」「关联」「加工」「处理」这类看不出差异的默认词。\n\n"
    "示例：\n"
    "- 输入 candidate_name=payment_di_entity, display_name=支付明细日表, "
    "source_dataset_urn=urn:li:dataset:xxx → "
    "{source_ref:'urn:li:dataset:xxx', name:'payment', display_name:'支付'}\n"
    "- 输入 candidate_name=finance_reconciliation_1d_entity, display_name=财务对账1日汇总 → "
    "{name:'finance_reconciliation', display_name:'财务对账'}\n"
    "- 输入 candidate_name=equip_repair_entity, display_name=设备维修工单 → "
    "{name:'equip_repair', display_name:'设备维修', role_hint:'bridge', "
    "role_reason:'每行是一次维修事件，真正的业务对象是它引用的设备与维修工'}\n"
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
    "{name:'order_feeds_settlement', display_name:'结算为'}"
)

# 关系命名分块流水线专用的系统提示：与对象/属性命名流水线并发独立执行，
# 输入只含 relations(待命名)与两端对象的概要 object_types(仅供业务背景参考，
# 不要求为其命名)，因此提示词只覆盖关系命名这一件事。
_LLM_RELATION_SYSTEM_PROMPT = (
    "你是企业本体建模专家。你的任务是为每条关系(relations)生成有业务含义的关系名，"
    "结合两端对象的业务语义(参考 object_types 中的 display_name/description)与该关系"
    "自身 description 中的证据(外键字段、血缘加工说明等)推断。关系名是**三元组"
    "谓词**，要能读成「源对象 [关系名] 目标对象」一句话(如「订单 属于 客户」)，"
    "而非「结算生成」这种「产出物+动作」名词。区分两个关系范畴：\n"
    "- 业务关联关系(有外键，structure_type=foreign_key)：两个业务实体间的真实业务"
    "事实，从外键字段语义判断，如「属于」「包含」「下单」「引用」「审核」。若 description"
    "形如「X 引用主数据 Y」，是单据引用主数据，按目标取「位于」「属于」「采用」「引用」。\n"
    "- 溯源/派生关系(血缘，description 形如「血缘：A 加工至 B」，无外键)：属于数据"
    "溯源(对齐 PROV-O 的 wasDerivedFrom)，不是业务事实。两端都是业务对象，优先判"
    "业务语义：源与目标是**同一业务实体的生命周期阶段**(潜客商机→商机、线索→商机、"
    "客户→潜在客户)写「转化」；目标是源的**明细/子表**(订单→订单明细)写「包含」；"
    "否则按 target_object 的变换类型取**方向前向的谓词**：目标是对账结果写「对账为」，"
    "目标是汇总/统计/报表写「汇总为」「统计为」，目标是结算数据写「结算为」，目标是"
    "标签/画像写「刻画」，实在拿不准才写「派生出」。\n\n"
    "无论哪一类，都不要写「生成」「派生」「关联」「加工」「处理」这类无信息量、看不出"
    "两端具体业务差异、随便哪条关系都能套用的默认词。\n\n"
    "输入是一份证据 JSON，包含 object_types(关系两端对象的业务背景，无需为其命名，"
    "也不会被使用)与 relations(需要命名的关系列表)。你需要输出 JSON，只包含一个"
    "字段：relations(数组)。\n\n"
    "relations 中每个元素必须包含：\n"
    "- name：原样回传输入中该关系的 name(逐字保留，用于回链，不可省略或改写)。\n"
    "- display_name：能读成「源 [谓词] 目标」的简短关系谓词(不超过 8 个汉字，只写"
    "动词/短语，不写完整句子，如「属于」「包含」「下单」「位于」「转化」「汇总为」"
    "「对账为」「统计为」「结算为」「刻画」「派生出」)。\n\n"
    "示例：\n"
    "- 输入 relations 中一条：name=payment_to_order, source_object=payment_di_entity, "
    "target_object=order_di_entity, structure_type=foreign_key, "
    "description='支付明细日表 通过外键 order_id 关联 order_di_entity' → "
    "{name:'payment_to_order', display_name:'属于'}\n"
    "- 输入 relations 中一条：name=order_feeds_settlement, structure_type=fact_table, "
    "description='血缘：订单明细 加工至 结算汇总' → "
    "{name:'order_feeds_settlement', display_name:'结算为'}\n"
    "- 输入 relations 中一条：name=order_feeds_reconciliation, structure_type=other, "
    "description='血缘：订单支付流水 加工至 财务对账结果' → "
    "{name:'order_feeds_reconciliation', display_name:'对账为'}"
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
            api_key = settings.openai_api_key
            base_url = None
            self.model = settings.openai_model
        else:
            api_key = runtime_config.api_key
            base_url = runtime_config.api_base_url
            self.model = runtime_config.model
        # 未配置 LLM(无 api_key) → client=None：结构仍由证据确定性组装，只是跳过
        # 「业务命名增强」这一步(用证据 candidate_name),不臆造数据,也不报错。
        self.client = (
            AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                http_client=make_async_http_client(),
            )
            if api_key
            else None
        )
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
        # 无 LLM：纯确定性命名(证据 candidate_name)，结构零丢失。
        if self.client is None:
            return self._build_draft_from_evidence(evidence, {}, {}, {}, {})
        # 分批闸门：表数与字符预算都在限额内才一次拿到命名增强，否则分块。
        fits_table_batch = len(evidence.object_types) <= settings.draft_chunk_table_batch_size
        fits_char_budget = len(self._build_prompt(evidence)) <= settings.llm_context_budget_chars
        if fits_table_batch and fits_char_budget:
            overrides, property_overrides, relation_overrides, role_overrides = (
                await self._llm_overrides(evidence)
            )
        else:
            overrides, property_overrides, relation_overrides, role_overrides = (
                await self._llm_overrides_chunked(evidence, progress_cb, checkpoint)
            )
        # 结构始终由全量证据确定性组装：对象/属性/关系一个都不会丢。
        return self._build_draft_from_evidence(
            evidence, overrides, property_overrides, relation_overrides, role_overrides
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
        role_overrides: RoleOverrides | None = None,
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
        role_overrides = role_overrides or {}

        object_types, properties, name_map = self._build_object_types_from_evidence(
            evidence, overrides, property_overrides, role_overrides
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
        role_overrides: RoleOverrides | None = None,
    ) -> tuple[list[DraftObjectType], list[DraftProperty], dict[str, str]]:
        """确定性组装对象与属性；返回 (object_types, properties, candidate→name 映射)。

        映射供关系组装环节按 candidate_name 解析 obj_name，也供「仅生成业务
        关系」场景在不重新命名对象的前提下按 source_dataset_urn 回链已入库对象。

        role_overrides 是 LLM 的角色语义否决（可选）：当 LLM 判定某表为技术/系统
        表（如 auth）、业务事实/关系表（如维修/清算，role_hint=bridge）或反之时，
        覆盖确定性启发式得出的 table_role，并在 role_reason
        上注明由 LLM 判定；未命中时保留启发式结果。
        """
        role_overrides = role_overrides or {}
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

        # 标识名去碰撞：不同源表被 LLM/启发式压成同名（如 Frappe 的
        # tabProcess Period Closing Voucher 与 tabPeriod Closing Voucher 都成
        # period_closing_voucher）会在发布期触发「对象标识重复」。撞名组改用源表名
        # 消歧。properties/relations 均经由 name_map 取名，故此处修正会自动下传。
        name_map.update(
            dedupe_object_names(
                [
                    (ot.candidate_name, name_map[ot.candidate_name], ot.source_dataset_urn)
                    for ot in evidence.object_types
                ]
            )
        )

        object_types = [
            DraftObjectType(
                name=name_map[ot.candidate_name],
                display_name=display_map[ot.candidate_name],
                description=desc_map[ot.candidate_name],
                source_ref=ot.source_dataset_urn,
                confidence=ot.confidence,
                row_count=ot.row_count,
                # 结构判定证据随对象一路带下去；与 LLM 是否改判标签无关（signals 是
                # 分类器的原始观测，分歧时仍是有效证据）。_resolve_role 不含该键，无冲突。
                role_signals=ot.role_signals,
                **self._resolve_role(ot, role_overrides.get(ot.candidate_name)),
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
                sample_values=item.sample_values,
                unique_count=item.unique_count,
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
                mapping_object_type_name=(
                    resolve(item.mapping_object) if item.mapping_object else None
                ),
            )
            for item in evidence.relations
        ]

    # ------------------------------------------------------------------
    # LLM 对象命名 + 属性中文名增强 + 关系业务名增强
    # ------------------------------------------------------------------
    async def _llm_overrides(
        self, evidence: EvidenceBundle
    ) -> tuple[ObjectOverrides, PropertyOverrides, RelationOverrides, RoleOverrides]:
        """单次调用：拿到全量对象的命名增强、属性的中文名增强、关系的业务名增强与角色否决。"""
        raw = await self._call_llm_objects(evidence)
        return (
            self._parse_object_overrides(raw, evidence),
            self._parse_property_overrides(raw, evidence),
            self._parse_relation_overrides(raw, evidence),
            self._parse_role_overrides(raw, evidence),
        )

    async def _llm_overrides_chunked(
        self,
        evidence: EvidenceBundle,
        progress_cb: ProgressCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> tuple[ObjectOverrides, PropertyOverrides, RelationOverrides, RoleOverrides]:
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

        (merged_objects, merged_properties, merged_roles), merged_relations = (
            await asyncio.gather(
                self._run_object_chunks(object_sub_bundles, checkpoint, advance),
                self._run_relation_chunks(relation_sub_bundles, checkpoint, advance),
            )
        )
        return merged_objects, merged_properties, merged_relations, merged_roles

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
    ) -> tuple[ObjectOverrides, PropertyOverrides, RoleOverrides]:
        """并发执行对象/属性命名分块，按内容哈希缓存，返回合并后的增强字典（含角色否决）。"""
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
                "roles": self._parse_role_overrides(raw, sub),
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
        merged_roles: RoleOverrides = {}
        for result in results:
            merged_objects.update(result.get("objects") or {})
            merged_roles.update(result.get("roles") or {})
            for candidate, field_map in (result.get("properties") or {}).items():
                merged_properties.setdefault(candidate, {}).update(field_map)
        return merged_objects, merged_properties, merged_roles

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
        if self.client is None:
            overrides, property_overrides, role_overrides = {}, {}, {}
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
                role_overrides = self._parse_role_overrides(raw, evidence)
            else:
                object_sub_bundles = self._split_object_chunks(evidence)
                advance = self._make_progress_advancer(
                    progress_cb, len(object_sub_bundles)
                )
                overrides, property_overrides, role_overrides = (
                    await self._run_object_chunks(
                        object_sub_bundles, checkpoint, advance
                    )
                )
        object_types, properties, _name_map = self._build_object_types_from_evidence(
            evidence, overrides, property_overrides, role_overrides
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
        if self.client is None:
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
        return self._coerce_llm_response(content, primary_list_key="object_types")

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
        return self._coerce_llm_response(content, primary_list_key="relations")

    @staticmethod
    def _coerce_llm_response(content: str, *, primary_list_key: str) -> dict:
        """把 LLM 返回文本解析为 parse_* 期望的顶层字典，容忍不规范输出。

        并非所有 provider/代理都遵守 ``response_format=json_object``：走自定义
        ``base_url`` 的模型可能返回**顶层 JSON 数组**（裸的对象/关系列表），
        或干脆返回非法 JSON。此前直接 ``json.loads`` 后交给 ``_parse_*``，一旦
        拿到 list，首个 ``raw.get(...)`` 就抛 ``'list' object has no attribute
        'get'``，整份草稿生成失败。

        这里在入口做归一化：
        - 顶层是 dict：原样返回。
        - 顶层是 ``[dict]`` 单元素包裹：拆包（常见的「用数组裹一层」写法）。
        - 顶层是其它数组：按调用方语境归到 ``primary_list_key``（对象命名调用
          归为 object_types，关系命名调用归为 relations），尽力保留命名增强。
        - 非法 JSON / 其它类型：回退空字典。

        任何一种回退都不丢结构——草稿结构由证据确定性组装，命名增强缺失时
        按现有规则回退（见 ``_build_draft_from_evidence``）。
        """
        try:
            data = json.loads(content or "{}")
        except (json.JSONDecodeError, TypeError):
            logger.warning("LLM 返回非法 JSON，跳过命名增强并回退确定性命名")
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            if len(data) == 1 and isinstance(data[0], dict):
                return data[0]
            logger.warning(
                "LLM 返回顶层数组（未遵守 json_object），按 %s 归一化",
                primary_list_key,
            )
            return {primary_list_key: data}
        logger.warning(
            "LLM 返回非对象/数组 JSON（%s），跳过命名增强", type(data).__name__
        )
        return {}

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

    def _parse_role_overrides(
        self, raw: dict, evidence: EvidenceBundle
    ) -> RoleOverrides:
        """把 LLM 返回的对象数组中的 role_hint 回链到证据 candidate_name，得到角色否决字典。

        回链复用与对象命名相同的三级兑底（source_ref → candidate_name → refine 同名）；
        role_hint 必须是 business_object / technical / bridge 之一才写入，其余值（含拿不准时省略）
        跳过，保留确定性启发式的分类结果。
        """
        objects = raw.get("object_types")
        if not isinstance(objects, list):
            objects = raw.get("objectTypes")
        if not isinstance(objects, list):
            return {}

        lookup = self._build_candidate_lookup(evidence)
        allowed = {ROLE_BUSINESS_OBJECT, ROLE_TECHNICAL, ROLE_BRIDGE}

        overrides: RoleOverrides = {}
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            candidate = self._resolve_candidate(obj, lookup)
            if candidate is None:
                continue
            role_hint = self._first_present(obj, ["role_hint", "roleHint"])
            if not role_hint:
                continue
            role_hint = role_hint.strip().lower()
            if role_hint not in allowed:
                continue
            overrides[candidate] = {
                "role": role_hint,
                "reason": self._first_present(obj, ["role_reason", "roleReason"]),
                "evidence_gap": self._first_present(
                    obj, ["evidence_gap", "evidenceGap"]
                ),
            }
        return overrides

    @staticmethod
    def _resolve_role(ot, override: RoleOverride | None) -> dict[str, Any]:
        """结合确定性启发式角色与 LLM 角色判定，返回 DraftObjectType 的角色字段。

        两个**相互独立**的判定源：结构启发式(object_classifier) 与 LLM 语义判断。

        - 一致：互证信号，保留启发式结果（含其原有置信度/待复核状态）。
        - 分歧：**不再让 LLM 静默否决**（旧行为把分歧「吃掉」，直接以 0.75 覆盖）。
          分歧点恰是最该人工看的地方——展示 LLM 的语义判定（对结构贫瘠的源更可信），
          但**标记待复核并下调置信度**，原因里并陈两方观点与 LLM 报告的证据缺口，
          交人工裁定。置信度是固定的低值，不由 LLM 自身生成物推导（反循环）。
        """
        base = {
            "table_role": ot.table_role,
            "role_confidence": ot.role_confidence,
            "role_reason": ot.role_reason,
        }
        if not override:
            return base
        llm_role = (override.get("role") or "").strip()
        if llm_role not in (ROLE_BUSINESS_OBJECT, ROLE_TECHNICAL, ROLE_BRIDGE):
            return base
        if llm_role == ot.table_role:
            # 一致：保留启发式（若启发式本就待复核，仍保留其待复核状态）。
            return base

        # bridge = 业务事实/关系表（记录一次动作/事件，真正的对象是它引用的键）。
        role_labels = {
            ROLE_BUSINESS_OBJECT: "业务对象",
            ROLE_TECHNICAL: "技术/系统表",
            ROLE_BRIDGE: "业务事实/关系表",
        }
        label = role_labels.get(llm_role, llm_role)
        heur_label = role_labels.get(ot.table_role, ot.table_role)
        note = f"[待复核] 启发式↔LLM 角色分歧：LLM 判为{label}"
        llm_reason = (override.get("reason") or "").strip()
        if llm_reason:
            note += f"（{llm_reason}）"
        note += f"；启发式判为{heur_label}"
        heur_reason = (ot.role_reason or "").removeprefix("[待复核]").strip()
        if heur_reason:
            note += f"（{heur_reason}）"
        evidence_gap = (override.get("evidence_gap") or "").strip()
        if evidence_gap:
            note += f"；证据缺口：{evidence_gap}"
        # 分歧固定低置信(0.5)——凸显「需人工确认」，且不受 LLM 自身生成物影响。
        return {"table_role": llm_role, "role_confidence": 0.5, "role_reason": note}

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
