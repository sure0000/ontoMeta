"""数据治理规约的 schema + 内置默认规约。

**平移原则**：默认规约里 ``enforced=True`` 的规则，接进 Validation Gate（G1）后
所复现的行为必须与现状**逐字节一致**；现状未强制、属于本次新增的约束，一律先给
``enforced=False``（advisory，仅声明不阻断），避免 G0→G1 引入回归。每条平移规则都
标注了它的现状锚点（源文件:行为），G1 接线时按锚点核对。

规约此刻是**纯数据**——本模块不 import 任何闸门，反向依赖（validation → governance）
留给 G1 建立，方向不能倒。层级/必填/凭据的字面值由 `tests/test_governance_standard.py`
对齐各自的 source-of-truth，防止两处漂移。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Mapping

from sqlalchemy.orm import Session

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Rule:
    """一条规约条款。

    ``code`` 是稳定机器码——接闸门后直接作 ``ValidationIssue.code``，故平移既有检查的
    规则须**沿用原 code**（如 ``missing_required_field`` / ``credential_in_spec``），保证
    遥测/拒绝码分布不因规约引入而变（对齐 FORMAL/V2 的盯法）。
    """

    code: str
    description: str
    severity: Severity = "error"
    # 是否允许带**理由**豁免（学 materialize_preflight 的 blocking=False + 显式忽略，
    # 豁免连同理由进 provenance，不许退化成「一律忽略」）。
    waivable: bool = False
    # G0 平移开关：True=复现现状已有的硬约束；False=仅声明、G1 先不阻断（advisory）。
    enforced: bool = True


# ---------------------------------------------------------------------------
# 各分区 schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NamingStandard:
    """命名规约。平移自 ``warehouse_generator.TargetNaming``。"""

    # 库名 = 层，或 层_前缀（有 prefix 时）。锚点：TargetNaming.database_of。
    database_pattern: str = "{layer}[_{prefix}]"
    # 物理表名默认取本体实体技术名（可被物化弹窗覆写）。锚点：TargetNaming.table_of。
    table_from_entity_name: bool = True
    # 标识符大小写约定。现状未强制 → advisory。
    identifier_case: str = "snake_case"
    # 建表禁用的保留字（小写比对）。现状未检查 → advisory；先给常见 SQL 关键字子集。
    reserved_words: tuple[str, ...] = (
        "select", "from", "where", "table", "order", "group",
        "by", "join", "index", "primary", "key", "user",
    )
    rules: tuple[Rule, ...] = (
        Rule(
            code="naming_layer_prefix",
            description="目标库名必须是「层」或「层_前缀」（dim/dwd/dws/ads[_<prefix>]）",
            severity="error",
            # 现状由生成器强制构造，非校验拦截 → G1 作 advisory 观测，避免误伤人工覆写库名。
            enforced=False,
        ),
        Rule(
            code="naming_snake_case",
            description="库/表/列标识符须为 snake_case",
            severity="warning",
            enforced=False,
        ),
        Rule(
            code="naming_reserved_word",
            description="表名/列名不得使用 SQL 保留字",
            severity="warning",
            enforced=False,
        ),
    )


@dataclass(frozen=True)
class LayeringStandard:
    """分层规约。平移自 ``materialization_contract`` 的落层逻辑 + ``models.warehouse``。

    注意：分层不是建模范式，只是物化契约的属性（见 MaterializationLayer docstring）；
    本体的对象/关系图才是模型主轴。这里只把「哪类实体落哪层」固化成可校验的映射。
    """

    layers: tuple[str, ...] = ("dim", "dwd", "dws", "ads")
    # 对象角色 → 目标层。锚点：materialization_contract.py:80-97。
    role_to_layer: Mapping[str, str] = field(
        default_factory=lambda: {
            "business_object": "dim",  # 业务对象 → 维度表
            "bridge": "dwd",           # 桥/关系实现表 → DWD
        }
    )
    # 关系 structure_type → 目标层。锚点：materialization_contract.py:114-135。
    structure_to_layer: Mapping[str, str] = field(
        default_factory=lambda: {
            "fact_table": "dwd",
            "bridge_table": "dwd",
        }
    )
    # 业务逻辑（指标）→ ADS。锚点：materialization_contract.py:164。
    business_logic_layer: str = "ads"
    # 允许的依赖方向：上层可引用下层，反之不可（ads→dws→dwd→dim→source）。
    # 现状未校验 → advisory。
    dependency_order: tuple[str, ...] = ("ads", "dws", "dwd", "dim", "source")
    rules: tuple[Rule, ...] = (
        Rule(
            code="layering_role_layer",
            description="对象/关系的落层必须符合角色映射（business_object→dim、bridge/fact→dwd、指标→ads）",
            severity="error",
            # 现状由契约生成决定，非事后校验；G1 先 advisory，避免与人工契约调整冲突。
            enforced=False,
        ),
        Rule(
            code="layering_dep_direction",
            description="跨层依赖只能自上而下引用（不得让 dim 依赖 ads）",
            severity="warning",
            enforced=False,
        ),
    )


@dataclass(frozen=True)
class RequiredMetadataStandard:
    """必备元数据规约。

    ``per_artifact`` 平移自 ``agents/validation.py:161-167`` 的 ``_REQUIRED_FIELDS``（**enforced**，
    G1 接线后须逐字节复现）。表级元数据（comment/owner/pk/partition）现状未强制 → advisory。
    """

    # 各制品类型的必填字段。source-of-truth：validation._REQUIRED_FIELDS（由测试对齐）。
    per_artifact: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "cluster": ("hosts", "services"),
            "sync": ("source", "target"),
            "transform": ("target_table", "ontology_id"),
            "metric": ("metric_name",),
            "materialize": ("ontology_id", "target_datasource_id"),
        }
    )
    # 建表必备：现状未强制，先 advisory。本体反补 comment 是这套架构的核心卖点，
    # 故 comment 定为将来最该收紧的一条。
    table_comment_required: bool = True
    table_owner_required: bool = True
    primary_key_required: bool = True
    # 超过该行数的表必须声明分区键（0 = 不要求）。advisory。
    partition_required_over_rows: int = 10_000_000
    rules: tuple[Rule, ...] = (
        Rule(
            code="missing_required_field",  # 沿用既有 code（validation.py:176/189）
            description="制品缺少其类型的必填字段（含 metric 必须绑定主对象）",
            severity="error",
            enforced=True,
        ),
        Rule(
            code="table_comment_missing",
            description="每张物理表必须有 comment（由本体 display_name/description 反补）",
            severity="warning",
            waivable=True,
            enforced=False,
        ),
        Rule(
            code="table_owner_missing",
            description="每张物理表必须声明 owner",
            severity="warning",
            waivable=True,
            enforced=False,
        ),
        Rule(
            code="primary_key_missing",
            description="每张表须有主键，或带理由显式豁免",
            severity="warning",
            waivable=True,
            enforced=False,
        ),
        Rule(
            code="partition_missing",
            description="超过阈值行数的大表须声明分区键",
            severity="warning",
            waivable=True,
            enforced=False,
        ),
    )


@dataclass(frozen=True)
class TypeStandard:
    """类型规约：语义类型 → 允许/禁止的物理类型。现状由各 Adapter.map_type 决定，
    此处只声明跨引擎的硬底线（如金额禁浮点），先 advisory。"""

    # semantic_type → (must_be_one_of, forbidden)
    semantic_rules: Mapping[str, Mapping[str, tuple[str, ...]]] = field(
        default_factory=lambda: {
            "money": {"must": ("decimal",), "forbid": ("float", "double", "real")},
        }
    )
    rules: tuple[Rule, ...] = (
        Rule(
            code="type_semantic_mismatch",
            description="金额等语义类型必须用 decimal，禁用浮点（精度不可协商）",
            severity="warning",
            enforced=False,
        ),
    )


@dataclass(frozen=True)
class SecurityStandard:
    """安全规约。平移自 ``agents/validation.py:196-214`` 的凭据检查（**enforced**）。"""

    # Spec 里出现即判违规的疑似凭据词元。source-of-truth：validation.py:203（由测试对齐）。
    forbidden_tokens: tuple[str, ...] = (
        "password", "secret", "token", "private_key", "credential",
    )
    # 指向密钥存储的引用后缀——正是鼓励的写法，放行。锚点：validation.py:200。
    allowed_ref_suffixes: tuple[str, ...] = ("_ref", "_alias")
    rules: tuple[Rule, ...] = (
        Rule(
            code="credential_in_spec",  # 沿用既有 code（validation.py:208）
            description="Spec 不得承载明文凭据，只能放指向密钥存储的 *_ref/*_alias 引用",
            severity="error",
            enforced=True,
        ),
    )


@dataclass(frozen=True)
class TaskStandard:
    """任务规约：sync/transform/materialize 作业的底线。多数已在 M15/M16 实现，
    此处把参数提为规约、把「必须成立」的性质声明成规则。"""

    # 全量装载必须走 staging + 原子切换（M15）。docker 通道尚未改造 → 先 advisory。
    full_load_must_stage: bool = True
    # DAG 单批任务数上限。锚点：M16 分批 ≤50。
    max_batch_size: int = 50
    # 缺省装载策略。锚点：LoadStrategy.FULL；memory: etl-generator-shared-seam（缺省 full）。
    default_load_strategy: str = "full"
    rules: tuple[Rule, ...] = (
        Rule(
            code="task_batch_size",
            description="单个 DAG 批次任务数不得超过上限（默认 50）",
            severity="warning",
            enforced=False,
        ),
        Rule(
            code="task_full_load_staging",
            description="全量装载必须先落 staging 再原子切换，失败不得清空正式表",
            severity="warning",
            enforced=False,
        ),
    )


# ---------------------------------------------------------------------------
# 顶层规约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernanceStandard:
    """一份完整的数据治理规约（单一活跃版本）。"""

    version: str
    naming: NamingStandard = field(default_factory=NamingStandard)
    layering: LayeringStandard = field(default_factory=LayeringStandard)
    required_metadata: RequiredMetadataStandard = field(
        default_factory=RequiredMetadataStandard
    )
    types: TypeStandard = field(default_factory=TypeStandard)
    security: SecurityStandard = field(default_factory=SecurityStandard)
    tasks: TaskStandard = field(default_factory=TaskStandard)

    def all_rules(self) -> list[Rule]:
        """扁平化所有条款，便于遍历/展示。"""
        rules: list[Rule] = []
        for section in (
            self.naming,
            self.layering,
            self.required_metadata,
            self.types,
            self.security,
            self.tasks,
        ):
            rules.extend(getattr(section, "rules", ()))
        return rules

    def enforced_rules(self) -> list[Rule]:
        """G1 接闸门后会真正拦截的条款（enforced=True）。"""
        return [r for r in self.all_rules() if r.enforced]

    def rule(self, code: str) -> Rule | None:
        for r in self.all_rules():
            if r.code == code:
                return r
        return None

    def to_dict(self) -> dict:
        return {"version": self.version, **{
            k: asdict(getattr(self, k))
            for k in ("naming", "layering", "required_metadata", "types", "security", "tasks")
        }}

    def compile_prompt_card(self) -> str:
        """编译成给 agent 的**简短**约束卡（G2 塞进建数 skill 的 prompt_overlay）。

        只列人读要点，不倾倒整份 JSON——对齐「知识包裁剪」的教训（memory:
        chatbi-sends-full-ontology-413），避免又把大 blob 塞进 prompt。
        """
        lines = [f"# 数据治理规约 v{self.version}（建表/建任务须遵循）", ""]
        lines.append("命名：")
        lines.append(f"  - 库名 = 层[_前缀]，层 ∈ {{{', '.join(self.layering.layers)}}}")
        lines.append(f"  - 标识符 {self.naming.identifier_case}，禁用 SQL 保留字")
        lines.append("落层：business_object→dim，bridge/fact→dwd，指标→ads")
        lines.append("必备元数据：每表须有 comment(本体反补)、owner、主键(或带理由豁免)")
        lines.append("类型：金额用 decimal，禁浮点")
        lines.append(
            f"任务：全量装载走 staging+原子切换；单批 ≤{self.tasks.max_batch_size}；"
            f"缺省装载 {self.tasks.default_load_strategy}"
        )
        lines.append("安全：Spec 禁明文凭据，只放 *_ref / *_alias 引用")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 内置默认规约
# ---------------------------------------------------------------------------
#
# 版本号语义化：major 变更 = 收紧了某条 enforced 规则（可能拒掉存量），需配套 re-lint。
DEFAULT_STANDARD = GovernanceStandard(version="1.0.0")


def active_standard(db: Session | None = None) -> GovernanceStandard:
    """取当前生效的规约。

    无 ``db`` → 内置 ``DEFAULT_STANDARD``（无库上下文的调用，如 is_blocking 判 severity）。
    有 ``db`` → 委托 ``GovernanceStandardService`` 读已发布版本（无发布记录时同样回落默认）。
    延迟 import 打破 standard ↔ service 的环：service 依赖本模块的 DEFAULT_STANDARD/注册表。
    """
    if db is None:
        return DEFAULT_STANDARD
    from app.services.governance_standard import GovernanceStandardService

    return GovernanceStandardService().get_active(db)
