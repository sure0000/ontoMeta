"""对象角色分类器：不依赖表名，判断一张表更像「业务对象」还是「普通数据表」。

表名本身就是治理对象，用命名判定等于用脏数据判脏数据。这里改用**结构、内容、
拓扑**这些独立于命名的信号打分：

1. 主键结构（身份）：
   - 单列且非外键的主键 → 有独立身份 → 业务对象
   - 主键完全由多个外键组成 → 桥接/关系表，本身不是对象（bridge）
   - 无主键 → 更像汇总物化
2. 外键入度（复用/拓扑）：被越多其它表通过外键指向，越像主数据/维度实体
3. 字段语义画像（内容）：
   - 描述性属性占比高（category/attribute/flag）→「在描述一个东西」→ 业务对象
   - 度量占比高（amount）+ 时间粒度 → 「在描述一次计算」→ 数据表/指标

分类是**加权打分 + 证据说明**，而非硬判定：低置信度候选照常生成为 suggested，
但带上 role/reason 交由人工在工作区确认——契合本项目 suggested→人工确认的治理闭环。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 角色取值
ROLE_BUSINESS_OBJECT = "business_object"  # 业务对象（实体）
ROLE_DATA_TABLE = "data_table"  # 普通数据表（汇总/报表/派生结果）
ROLE_BRIDGE = "bridge"  # 桥接/关系表（主键=多外键）

# 语义类型分组（与 EvidenceBuilder._infer_semantic_type 保持一致）
_MEASURE_TYPES = {"amount"}
_DESCRIPTIVE_TYPES = {"category", "attribute", "flag"}
_GRAIN_TYPES = {"datetime"}


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
) -> ClassificationResult:
    """对单张表按结构/内容/拓扑信号打分并分类。

    参数
    ----
    fields: 该表的字段信号列表。
    fk_in_degree: 有多少其它表通过外键指向这张表（跨表拓扑，调用方预先聚合）。
    lineage_upstream / lineage_downstream: 血缘上/下游数量，用于识别「血缘末端
        + 度量为主」的派生汇总表。
    glossary_terms: 数据集上人工挂载的业务术语（已确认的业务概念，最强信号）。
    row_count: profiling 总行数，配合主键 unique_count 做唯一度/粒度确认。

    返回带 role / confidence / reason 的 ClassificationResult。
    """
    glossary_terms = glossary_terms or []
    total = len(fields)
    pk_cols = [f for f in fields if f.is_primary_key]
    pk_all_fk = bool(pk_cols) and all(f.is_foreign_key for f in pk_cols)
    single_business_pk = len(pk_cols) == 1 and not pk_cols[0].is_foreign_key

    measure = sum(1 for f in fields if (f.semantic_type or "") in _MEASURE_TYPES)
    descriptive = sum(1 for f in fields if (f.semantic_type or "") in _DESCRIPTIVE_TYPES)
    has_grain = any((f.semantic_type or "") in _GRAIN_TYPES for f in fields)

    measure_ratio = measure / total if total else 0.0
    descriptive_ratio = descriptive / total if total else 0.0

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

    # 打分归类：正分→业务对象；负分→数据表；中间地带默认业务对象但低置信，留人工确认。
    if score >= 2.0:
        role = ROLE_BUSINESS_OBJECT
    elif score <= -1.0:
        role = ROLE_DATA_TABLE
    else:
        role = ROLE_BUSINESS_OBJECT
        reasons.append("信号不足，暂按业务对象保留，待人工确认")

    confidence = _score_to_confidence(score, role)

    return ClassificationResult(
        role=role,
        confidence=confidence,
        reason="；".join(reasons) or "无显著信号",
        score=score,
        signals={
            "pk_columns": len(pk_cols),
            "fk_in_degree": fk_in_degree,
            "measure_ratio": round(measure_ratio, 2),
            "descriptive_ratio": round(descriptive_ratio, 2),
        },
    )


def _score_to_confidence(score: float, role: str) -> float:
    """把分数映射为 0.5~0.95 的置信度：越极端越确信，中间地带更低。"""
    magnitude = abs(score)
    conf = 0.5 + min(magnitude, 5.0) / 5.0 * 0.45
    # 中间地带（默认保留为业务对象）压低置信度，凸显「需人工确认」。
    if role == ROLE_BUSINESS_OBJECT and score < 2.0:
        conf = min(conf, 0.55)
    return round(conf, 2)
