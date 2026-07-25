"""对象角色分类器：不依赖表名，判断一张表更像「业务对象」还是「普通数据表」，
以及一类结构上像对象、但根本不是业务治理目标的「技术/系统表」（auth/config/
session/log 等）。

表名本身就是治理对象，用命名判定等于用脏数据判脏数据。这里改用**结构、内容、
拓扑、语义锚定**这些独立于命名的信号打分：

1. 主键结构（身份）：
   - 单列且非外键的主键 → 有独立身份 → 业务对象
   - 主键完全由多个外键组成 → 桥接/关系表，本身不是对象（bridge）
   - 无主键 → 更像汇总物化
2. 外键入度（复用/拓扑）：被越多其它表通过外键指向，越像主数据/维度实体
3. 字段语义画像（内容）：
   - 描述性属性占比高（category/attribute/flag）→「在描述一个东西」→ 业务对象
   - 度量占比高（amount）+ 时间粒度 → 「在描述一次计算」→ 数据表/指标
   - 技术词汇字段占比高（token/secret/hash/config/session 等）→ 系统表
4. 图连通性（拓扑）：一个真实业务对象总要与别的实体发生关联——被外键引用、
   自身引用别表、或有血缘。既不被外键引用、自身也不引用别表、且无血缘 → 与
   业务图脱节的孤岛：配合技术字段可判技术表；即便无技术字段，只要它又缺乏
   内在业务身份（业务命名/描述性属性/术语/唯一主键），也不应仅凭一个主键就
   被识别为业务对象——这类孤岛多为字典/配置/日志/临时表，改判为普通数据表。
5. 语义锚定（内容）：缺少业务命名/描述/已挂术语（只有一个裸技术名）→ 无业务
   身份，配合技术字段增强技术表判定。
6. DataHub 原生标注（tags/subTypes）：被标为 system/technical/internal 等。

分类是**加权打分 + 证据说明**，而非硬判定：低置信度候选照常生成为 suggested，
但带上 role/reason 交由人工在工作区确认——契合本项目 suggested→人工确认的治理闭环。
人工挂载的业务术语（glossary_terms）是最强的业务信号，一旦存在即豁免技术表判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 角色取值
ROLE_BUSINESS_OBJECT = "business_object"  # 业务对象（实体）
ROLE_DATA_TABLE = "data_table"  # 普通数据表（汇总/报表/派生结果）
ROLE_BRIDGE = "bridge"  # 桥接/关系表（主键=多外键）
ROLE_TECHNICAL = "technical"  # 技术/系统表（auth/config/session/log），非业务治理对象

# 语义类型分组（与 EvidenceBuilder._infer_semantic_type 保持一致）
_MEASURE_TYPES = {"amount"}
_DESCRIPTIVE_TYPES = {"category", "attribute", "flag"}
_GRAIN_TYPES = {"datetime"}
_TECHNICAL_TYPES = {"technical"}

# DataHub tag / subType 名称里出现这些词元，视为系统/技术资产的显式标注。
_TECHNICAL_TAG_HINTS = {
    "system",
    "technical",
    "internal",
    "infra",
    "ops",
    "security",
    "audit",
    "log",
    "temp",
    "tmp",
    "staging",
    "deprecated",
    "config",
    "metadata",
}


@dataclass
class FieldSignal:
    """分类所需的单字段信号（从证据字段抽取，独立于表名）。"""

    name: str
    semantic_type: str | None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    # profiling 不同值个数（未开启 profiling 时为 None），用于主键唯一度/粒度分析。
    unique_count: int | None = None


@dataclass
class ClassificationResult:
    role: str
    confidence: float
    reason: str
    score: float = 0.0
    signals: dict = field(default_factory=dict)


def classify_object_role(
    fields: list[FieldSignal],
    *,
    fk_in_degree: int = 0,
    lineage_upstream: int = 0,
    lineage_downstream: int = 0,
    glossary_terms: list[str] | None = None,
    row_count: int | None = None,
    has_business_naming: bool = True,
    subtypes: list[str] | None = None,
    tags: list[str] | None = None,
) -> ClassificationResult:
    """对单张表按结构/内容/拓扑/语义锚定信号打分并分类。

    参数
    ----
    fields: 该表的字段信号列表。
    fk_in_degree: 有多少其它表通过外键指向这张表（跨表拓扑，调用方预先聚合）。
    lineage_upstream / lineage_downstream: 血缘上/下游数量，用于识别「血缘末端
        + 度量为主」的派生汇总表，以及判定图孤岛。
    glossary_terms: 数据集上人工挂载的业务术语（已确认的业务概念，最强信号；
        一旦存在即豁免技术表判定）。
    row_count: profiling 总行数，配合主键 unique_count 做唯一度/粒度确认。
    has_business_naming: 该表是否有人工赋予的业务命名/描述/术语（区别于裸技术表名）。
        为 False 时作为技术表的辅助信号。
    subtypes / tags: DataHub 原生 subType / tag 名称，命中技术词元时作为显式技术信号。

    返回带 role / confidence / reason 的 ClassificationResult。
    """
    glossary_terms = glossary_terms or []
    subtypes = subtypes or []
    tags = tags or []
    total = len(fields)
    pk_cols = [f for f in fields if f.is_primary_key]
    pk_all_fk = bool(pk_cols) and all(f.is_foreign_key for f in pk_cols)
    single_business_pk = len(pk_cols) == 1 and not pk_cols[0].is_foreign_key

    measure = sum(1 for f in fields if (f.semantic_type or "") in _MEASURE_TYPES)
    descriptive = sum(1 for f in fields if (f.semantic_type or "") in _DESCRIPTIVE_TYPES)
    technical = sum(1 for f in fields if (f.semantic_type or "") in _TECHNICAL_TYPES)
    has_grain = any((f.semantic_type or "") in _GRAIN_TYPES for f in fields)
    has_fk_out = any(f.is_foreign_key for f in fields)

    measure_ratio = measure / total if total else 0.0
    descriptive_ratio = descriptive / total if total else 0.0
    technical_ratio = technical / total if total else 0.0

    reasons: list[str] = []

    # 桥接/关系表：主键完全由 2+ 个外键组成，本身不是业务对象。
    if pk_all_fk and len(pk_cols) >= 2:
        return ClassificationResult(
            role=ROLE_BRIDGE,
            confidence=0.8,
            reason=f"主键由 {len(pk_cols)} 个外键组成，判为桥接/关系表",
            score=0.0,
            signals={
                "pk_columns": len(pk_cols),
                "fk_in_degree": fk_in_degree,
                "measure_ratio": round(measure_ratio, 2),
            },
        )

    # -- 技术/系统表评估（正交维度，与业务/数据表打分并行）--
    tech_score, tech_reasons, isolated = _evaluate_technical(
        technical_ratio=technical_ratio,
        fk_in_degree=fk_in_degree,
        has_fk_out=has_fk_out,
        lineage_upstream=lineage_upstream,
        lineage_downstream=lineage_downstream,
        has_business_naming=has_business_naming,
        subtypes=subtypes,
        tags=tags,
    )

    score = 0.0

    # 0) 人工挂载的业务术语：最强信号（已被人确认为业务概念）。
    if glossary_terms:
        score += 3.0
        reasons.append(f"已挂载业务术语「{glossary_terms[0]}」（人工确认）")

    # 1) 身份：主键结构
    if single_business_pk:
        score += 2.0
        reasons.append("单列业务主键，具备独立身份")
        # profiling 确认：主键唯一度≈行数 → 真实实体粒度；明显不唯一 → 疑似汇总粒度。
        pk_unique = pk_cols[0].unique_count
        if pk_unique is not None and row_count:
            ratio = pk_unique / row_count if row_count else 0.0
            if ratio >= 0.98:
                score += 1.0
                reasons.append(f"主键唯一度 {ratio:.0%}，确认实体粒度")
            elif ratio < 0.5:
                score -= 1.0
                reasons.append(f"声明主键实际唯一度仅 {ratio:.0%}，疑似汇总粒度")
    elif not pk_cols:
        score -= 1.0
        reasons.append("无主键（元数据缺失或非实体）")

    # 2) 拓扑：外键入度（被引用度）
    if fk_in_degree >= 3:
        score += 2.0
        reasons.append(f"被 {fk_in_degree} 张表外键引用，疑似主数据/维度实体")
    elif fk_in_degree >= 1:
        score += 1.0
        reasons.append(f"被 {fk_in_degree} 张表外键引用")

    # 3) 内容：字段语义画像
    if descriptive_ratio >= 0.4:
        score += 1.0
        reasons.append(f"描述性属性占比 {descriptive_ratio:.0%}，偏向描述实体")
    if measure_ratio >= 0.3:
        score -= 2.0
        reasons.append(f"度量字段占比 {measure_ratio:.0%}，偏向计算/汇总")
        if has_grain:
            score -= 1.0
            reasons.append("含时间粒度，疑似按周期汇总物化")

    # 拓扑：血缘末端 + 度量为主 → 派生结果表
    if (
        lineage_upstream >= 1
        and lineage_downstream == 0
        and measure_ratio >= 0.3
    ):
        score -= 1.0
        reasons.append("处于血缘下游末端且以度量为主，疑似派生结果")

    # 拓扑：图连通性是业务对象的核心特征——一个真实业务实体总要与别的实体发生
    # 关联（被外键引用、引用别人、或有血缘）。既无任何关联、又缺乏内在业务身份
    # （单列业务主键 + 足够描述性属性 + 人工业务命名）的孤岛表，往往是字典/配置/
    # 日志/临时表，不应仅凭一个主键就被识别为业务对象。人工术语（glossary）是最强
    # 业务信号，一旦存在即豁免该惩罚。
    connected = (
        fk_in_degree >= 1
        or has_fk_out
        or lineage_upstream >= 1
        or lineage_downstream >= 1
    )
    has_intrinsic_identity = (
        single_business_pk and descriptive_ratio >= 0.4 and has_business_naming
    )
    graph_isolated_nonbusiness = (
        not connected and not has_intrinsic_identity and not glossary_terms
    )
    if graph_isolated_nonbusiness:
        score -= 2.0
        reasons.append(
            "与业务图脱节（无外键关联、无血缘）且缺乏内在业务身份，疑似字典/配置/日志等非业务表"
        )

    signals = {
        "pk_columns": len(pk_cols),
        "fk_in_degree": fk_in_degree,
        "measure_ratio": round(measure_ratio, 2),
        "descriptive_ratio": round(descriptive_ratio, 2),
        "technical_ratio": round(technical_ratio, 2),
        "tech_score": round(tech_score, 2),
        "isolated": isolated,
        "connected": connected,
    }

    # 技术表判定：无人工业务术语且技术信号足够强、且不弱于业务信号 → 技术/系统表。
    if not glossary_terms and tech_score >= 3.0 and tech_score >= score:
        return ClassificationResult(
            role=ROLE_TECHNICAL,
            confidence=_score_to_confidence(tech_score, ROLE_TECHNICAL),
            reason="；".join(tech_reasons) or "疑似技术/系统表",
            score=score,
            signals=signals,
        )

    # 打分归类：正分→业务对象；负分→数据表；中间地带看技术信号决定默认方向。
    if score >= 2.0:
        role = ROLE_BUSINESS_OBJECT
    elif score <= -1.0:
        role = ROLE_DATA_TABLE
    elif not glossary_terms and tech_score >= 2.0:
        # 中间地带但带中等技术信号：不再默认业务对象，改为低置信技术表待人工确认。
        role = ROLE_TECHNICAL
        reasons = tech_reasons + ["信号偏技术/系统表，待人工确认"]
    elif graph_isolated_nonbusiness:
        # 中间地带的孤岛表：既无业务关联又无内在业务身份，不再默认业务对象，
        # 改判为普通数据表待人工确认，避免大量无业务关系的独立表被误定为业务对象。
        role = ROLE_DATA_TABLE
        reasons.append("与业务图脱节且无显著业务信号，暂判数据表待人工确认")
    else:
        role = ROLE_BUSINESS_OBJECT
        reasons.append("信号不足，暂按业务对象保留，待人工确认")

    confidence = _score_to_confidence(
        tech_score if role == ROLE_TECHNICAL else score, role
    )

    return ClassificationResult(
        role=role,
        confidence=confidence,
        reason="；".join(reasons) or "无显著信号",
        score=score,
        signals=signals,
    )


def _evaluate_technical(
    *,
    technical_ratio: float,
    fk_in_degree: int,
    has_fk_out: bool,
    lineage_upstream: int,
    lineage_downstream: int,
    has_business_naming: bool,
    subtypes: list[str],
    tags: list[str],
) -> tuple[float, list[str], bool]:
    """聚合技术/系统表信号，返回 (tech_score, 证据说明, 是否图孤岛)。

    - 技术字段词汇（token/secret/hash/config/session 等）是**主信号**；
    - 图孤岛、缺少业务命名是**辅助信号**（仅在已有主信号时叠加，避免小域/
      元数据缺失导致的正常孤立业务表被误判）；
    - DataHub tag/subType 显式标注是**独立强信号**。
    """
    isolated = (
        fk_in_degree == 0
        and not has_fk_out
        and lineage_upstream == 0
        and lineage_downstream == 0
    )

    tech_score = 0.0
    tech_reasons: list[str] = []

    # 主信号：技术字段词汇占比
    if technical_ratio >= 0.4:
        tech_score += 2.5
        tech_reasons.append(
            f"技术词汇字段（token/secret/config 等）占比 {technical_ratio:.0%}，疑似系统表"
        )
    elif technical_ratio >= 0.25:
        tech_score += 1.5
        tech_reasons.append(f"含较多技术词汇字段（占比 {technical_ratio:.0%}）")

    # 独立强信号：DataHub 原生 tag / subType 标注为系统/技术资产
    tag_hit = _match_technical_hint(tags) or _match_technical_hint(subtypes)
    if tag_hit:
        tech_score += 2.0
        tech_reasons.append(f"DataHub 标注「{tag_hit}」，显式标为系统/技术资产")

    # 辅助信号：仅在已有主/强信号时叠加，避免噪声
    if tech_score > 0:
        if isolated:
            tech_score += 1.0
            tech_reasons.append("与业务图脱节（无外键关联、无血缘），疑似孤立系统表")
        if not has_business_naming:
            tech_score += 1.0
            tech_reasons.append("缺少业务命名/描述/术语，只有裸技术表名")

    return tech_score, tech_reasons, isolated


def _match_technical_hint(names: list[str]) -> str | None:
    """返回首个命中技术词元的 tag/subType 名称（不区分大小写），都不命中返回 None。"""
    for name in names:
        lowered = (name or "").lower()
        for hint in _TECHNICAL_TAG_HINTS:
            if hint in lowered:
                return name
    return None


def _score_to_confidence(score: float, role: str) -> float:
    """把分数映射为 0.5~0.95 的置信度：越极端越确信，中间地带更低。"""
    magnitude = abs(score)
    conf = 0.5 + min(magnitude, 5.0) / 5.0 * 0.45
    # 中间地带（默认保留为业务对象）压低置信度，凸显「需人工确认」。
    if role == ROLE_BUSINESS_OBJECT and score < 2.0:
        conf = min(conf, 0.55)
    return round(conf, 2)
