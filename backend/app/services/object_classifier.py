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

# 退维（反规范化引用值）识别：这些列是**被引用实体的值拷贝或退化标识**，不是本表的
# 自有属性。信号贫瘠源（无 FK 声明）里它们会伪装成描述性属性，把关系/事实表的描述性
# 占比灌高、并让 own_attr_count>0 而躲过 facet-B，误判为业务对象。仅用两条高精度信号
# （宁可少扣不误伤），词根裸配 DocType 一类留待有样本值/基数的实时层确认。
_ECHO_SUFFIXES = ("_name", "_title", "_code", "_abbr", "_group", "_currency")
_POLY_REF_SUFFIXES = ("_no", "_id", "_name")


def _detect_reference_values(fields: "list[FieldSignal]") -> set[str]:
    """返回应视为「退维引用值」而非自有属性的列名（小写）。

    1. 多态标识对：同一词根同时出现 `X_type` 与 `X_no`/`X_id`/`X_name`（如
       `voucher_type`+`voucher_no`）——`X_type` 指明目标表、`X_*` 是其键值，
       二者都是退化引用而非属性。单独一个 `X_type`（状态/类别枚举）不算。
    2. 兄弟回声：`{sib}_{name|title|code|abbr|group|currency}` 且 `{sib}` 本身
       是同表另一列（Frappe fetch_from 反规范化拷贝，如 `asset`+`asset_name`）。
    """
    names = {f.name.lower() for f in fields}
    ref: set[str] = set()
    for n in names:
        if not n.endswith("_type") or len(n) <= len("_type"):
            continue
        stem = n[: -len("_type")]
        members = [stem + s for s in _POLY_REF_SUFFIXES if stem + s in names]
        if members:  # _type 之外至少还有一个引用值列 → 判定为多态引用对
            ref.add(n)
            ref.update(members)
    for n in names:
        for suf in _ECHO_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 1 and n[: -len(suf)] in names:
                ref.add(n)
                break
    return ref

# 业务环节最小成员数：只有隶属于某个业务环节（关系图上 >= 该值成员的聚类）的表才
# 可能是业务对象。孤立表（1 成员）与孤对（2 成员）不构成一个业务环节。
_MIN_SEGMENT_SIZE = 3

# 业务对象判定阈值：综合得分 >= 该值判为业务对象。审核队列的「判定强度分带」
# （review_queue.score_band）与前端 utils/role.ts 的 ROLE_SCORE_THRESHOLD 都对齐它，
# 三处必须是同一个数——否则复核界面标注的「刚过线/未过线」与实际分类结果对不上。
ROLE_SCORE_THRESHOLD = 2.0

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
    needs_review: bool = False
    signals: dict = field(default_factory=dict)


def classify_object_role(
    fields: list[FieldSignal],
    *,
    fk_in_degree: int = 0,
    distinct_fk_targets: int = 0,
    lineage_upstream: int = 0,
    lineage_downstream: int = 0,
    glossary_terms: list[str] | None = None,
    row_count: int | None = None,
    has_business_naming: bool = True,
    subtypes: list[str] | None = None,
    tags: list[str] | None = None,
    is_child_table: bool = False,
    fact_name_token: str | None = None,
    weak_fact_name_token: str | None = None,
    segment_size: int | None = None,
) -> ClassificationResult:
    """对单张表按结构/内容/拓扑/语义锚定信号打分并分类。

    参数
    ----
    fields: 该表的字段信号列表。
    fk_in_degree: 有多少其它表通过外键指向这张表（跨表拓扑，调用方预先聚合）。
    distinct_fk_targets: 这张表的外键指向多少张**不同**的目标表（facet B：引用多个
        不同实体、自身缺乏独立属性者疑似关系/桥接而非业务对象；调用方预聚合）。
    lineage_upstream / lineage_downstream: 血缘上/下游数量，用于识别「血缘末端
        + 度量为主」的派生汇总表，以及判定图孤岛。
    glossary_terms: 数据集上人工挂载的业务术语（已确认的业务概念，最强信号；
        一旦存在即豁免技术表判定）。
    row_count: profiling 总行数，配合主键 unique_count 做唯一度/粒度确认。
    has_business_naming: 该表是否有人工赋予的业务命名/描述/术语（区别于裸技术表名）。
        为 False 时作为技术表的辅助信号。
    subtypes / tags: DataHub 原生 subType / tag 名称，命中技术词元时作为显式技术信号。
    fact_name_token: 表名/含义命中的事件动词或结构性事实/明细特征词（如「调整」「交易」「明细」
        「记录」），由调用方经 fact_naming.detect_fact_name 预判。命中即表明这是一次业务**事实**
        或某父表的**明细行**而非实体——真正的业务对象是它引用的键。作为独立于结构的语义信号
        （结构 PK/FK 在信号贫瘠源上失效时常是唯一线索）：压低业务分并把本会判为业务对象者改判
        事实/关系表（复用 bridge），标 needs_review 交人工确认；挂了业务术语则豁免。
    segment_size: 该表在关系图上所属聚类（业务环节）的成员数，由调用方在整图上跑社区
        检测预聚合。**必要条件**：只有隶属于某个业务环节（成员数 >= _MIN_SEGMENT_SIZE）
        的表才可能是业务对象；孤立表/孤对本会判业务对象者改判数据表 + needs_review。
        None 表示调用方未提供该信号（如直接单测），按不降级处理，保持向后兼容；人工
        业务术语(glossary)已确认业务身份，豁免此门槛。

    返回带 role / confidence / reason 的 ClassificationResult。
    """
    glossary_terms = glossary_terms or []
    subtypes = subtypes or []
    tags = tags or []

    # 明细/子表（源画像判定，如 Frappe 的 parent/parenttype 锚点列）：隶属某父
    # DocType，是其明细行（一次业务事实）而非独立业务实体。按建模原则「明细表/事实表
    # 都是业务关系」判为关系表(bridge) + 待复核，真正的业务对象落在其引用的键上。挂了
    # 人工业务术语则豁免（人工已认定为业务概念）。
    if is_child_table and not glossary_terms:
        return ClassificationResult(
            role=ROLE_BRIDGE,
            confidence=0.6,
            reason=(
                "明细/子表（含 parent/parenttype 等子表锚点，隶属父表）——按原则判为业务"
                "关系(bridge)：它是父表的明细行(一次业务事实)，真正的业务对象落在其引用的键上，待人工确认"
            ),
            score=0.0,
            needs_review=True,
            signals={"is_child_table": True},
        )

    total = len(fields)
    pk_cols = [f for f in fields if f.is_primary_key]
    pk_all_fk = bool(pk_cols) and all(f.is_foreign_key for f in pk_cols)
    single_business_pk = len(pk_cols) == 1 and not pk_cols[0].is_foreign_key

    measure = sum(1 for f in fields if (f.semantic_type or "") in _MEASURE_TYPES)
    # 退维引用值列：既非自有描述属性也非度量，从描述性计数中剔除，避免把反规范化
    # 拷贝/退化标识当作实体属性而高估业务对象倾向。
    ref_value_cols = _detect_reference_values(fields)

    def _is_ref_value(f: "FieldSignal") -> bool:
        return f.name.lower() in ref_value_cols

    descriptive = sum(
        1
        for f in fields
        if (f.semantic_type or "") in _DESCRIPTIVE_TYPES and not _is_ref_value(f)
    )
    technical = sum(1 for f in fields if (f.semantic_type or "") in _TECHNICAL_TYPES)
    has_grain = any((f.semantic_type or "") in _GRAIN_TYPES for f in fields)
    has_fk_out = any(f.is_foreign_key for f in fields)

    # 自有属性：排除主键、外键与退维引用值后，实体自身的描述/度量字段数——用于区分
    # 「关联实体」（有自有属性，如订单）与「纯关系/桥接」（几乎只有引用，如用户收藏）。
    own_fields = [
        f
        for f in fields
        if not f.is_primary_key and not f.is_foreign_key and not _is_ref_value(f)
    ]
    own_descriptive = sum(
        1 for f in own_fields if (f.semantic_type or "") in _DESCRIPTIVE_TYPES
    )
    own_measure = sum(1 for f in own_fields if (f.semantic_type or "") in _MEASURE_TYPES)
    own_attr_count = own_descriptive + own_measure

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

    # facet B：代理主键的多外键关联表——经典 pk_all_fk 规则（主键=纯外键）漏判的
    # 关系/事实表。引用了 >=2 个不同实体、自身没有任何独立属性、且无人反向引用 →
    # 本质是关系而非实体（如「用户收藏」只有 user_id/product_id）。遵循「宁可漏降
    # 不可错降」：仅在自有属性为 0 时才降级；有任何自有属性或被反向引用者（如订单）
    # 留待下方按对象打分并标记 needs_review。挂了业务术语则一律豁免。
    multi_fk = distinct_fk_targets >= 2
    if multi_fk and own_attr_count == 0 and fk_in_degree == 0 and not glossary_terms:
        return ClassificationResult(
            role=ROLE_BRIDGE,
            confidence=0.6,
            reason=(
                f"引用 {distinct_fk_targets} 个不同实体、自身无独立属性且无人反向引用，"
                "疑似关系/桥接表而非业务对象（代理主键，经典规则漏判）"
            ),
            score=0.0,
            needs_review=True,
            signals={
                "distinct_fk_targets": distinct_fk_targets,
                "own_attr_count": own_attr_count,
                "fk_in_degree": fk_in_degree,
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

    # 4) 语义命名：表名/含义为动作/事件动词（xx调整、xx交易）或结构性事实/明细特征
    # （xx明细、xx事实表、xx记录）→ 一次业务事实/明细而非实体。独立于结构的信号——真实 ERP
    # 无 PK/FK 时，这常是识别事实表的唯一线索。挂了人工术语（已确认为业务概念）则豁免。
    # 压低业务分，最终归类处再据此把业务对象改判事实表。
    fact_named = bool(fact_name_token) and not glossary_terms
    if fact_named:
        score -= 2.0
        reasons.append(
            f"表名/含义含事实/明细特征词「{fact_name_token}」，疑似一次业务事实/明细"
            f"（{fact_name_token}）而非实体，真正的业务对象是它引用的键"
        )

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
        "distinct_fk_targets": distinct_fk_targets,
        "own_attr_count": own_attr_count,
        "measure_ratio": round(measure_ratio, 2),
        "descriptive_ratio": round(descriptive_ratio, 2),
        "technical_ratio": round(technical_ratio, 2),
        "tech_score": round(tech_score, 2),
        "isolated": isolated,
        "connected": connected,
    }
    if fact_named:
        signals["fact_name_token"] = fact_name_token
    if segment_size is not None:
        signals["segment_size"] = segment_size

    # 技术表判定：无人工业务术语且技术信号足够强、且不弱于业务信号 → 技术/系统表。
    if not glossary_terms and tech_score >= 3.0 and tech_score >= score:
        tech_conf = _score_to_confidence(tech_score, ROLE_TECHNICAL)
        return ClassificationResult(
            role=ROLE_TECHNICAL,
            confidence=tech_conf,
            reason="；".join(tech_reasons) or "疑似技术/系统表",
            score=score,
            needs_review=tech_conf < 0.7,
            signals=signals,
        )

    # 打分归类：正分→业务对象；负分→数据表；中间地带看技术信号决定默认方向。
    if score >= ROLE_SCORE_THRESHOLD:
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

    # 事实/动词命名改判：动词/事件表是「事实关系」而非业务对象——复用 bridge 角色。
    # 仅当它本会被判为业务对象时改判（精准针对「把一次动作误当实体」这一危害）。
    # 被多表外键引用（fk_in_degree>=3）本身**不**否决事实判定——交易头(如收货/结算/订单)
    # 本就会被下游单据引用；此时仍判关系表(bridge)，但在理由里标注其枢纽属性，
    # needs_review 交人工确认是否实为命名欠佳的主数据。data_table/technical 维持原判
    # （派生汇总/系统表不必强扭成关系表）。
    if fact_named and role == ROLE_BUSINESS_OBJECT:
        role = ROLE_BRIDGE
        if fk_in_degree >= 3:
            reasons.append(
                f"据命名判为业务事实/关系表(bridge)；虽被 {fk_in_degree} 张表外键引用"
                "（疑似枢纽），仍按「事实即关系」优先判为关系表，请人工确认是否实为主数据"
            )
        else:
            reasons.append(
                "据命名判为业务事实/关系表(bridge)，真正的业务对象应落在其引用的键上"
            )

    # 弱事实命名 + 结构证据：订单/发票/工单 这类歧义名词单凭命名不判事实（可能是实体），
    # 但当它同时**引用多个不同维度**（distinct_fk_targets>=2）**且含度量字段**（measure>=1）
    # 时，结构上就是一张交易/事实表——把被主键+被引用信号带偏成业务对象的交易头改判
    # 关系表(bridge) + 待复核。无度量、少外键的参考维度（订单类型/付款方式）结构不达标，
    # 不受影响。挂了人工术语则豁免。
    weak_fact = bool(weak_fact_name_token) and not glossary_terms
    weak_fact_structural = weak_fact and distinct_fk_targets >= 2 and measure >= 1
    if weak_fact_structural and role == ROLE_BUSINESS_OBJECT:
        role = ROLE_BRIDGE
        reasons.append(
            f"命名含交易名词「{weak_fact_name_token}」且引用 {distinct_fk_targets} 个不同维度、"
            f"含 {measure} 个度量字段，结构上是一次交易/事实——判为业务关系(bridge)，"
            "真正的业务对象应落在其引用的维度键上，待人工确认"
        )
        signals["weak_fact_name_token"] = weak_fact_name_token

    # 业务环节归属（关系图结构，**必要条件**）：只有隶属于某个业务环节的表才可能是业务
    # 对象。业务环节 = 关系图上社区检测得到的 >= _MIN_SEGMENT_SIZE 成员聚类；孤立表/孤对
    # 不属于任何环节。本会判为业务对象、却不隶属任何环节者改判数据表待人工确认。人工业务
    # 术语(glossary)已确认业务身份，豁免；segment_size 为 None 表示未提供该信号，不降级。
    segment_demoted = False
    if (
        role == ROLE_BUSINESS_OBJECT
        and segment_size is not None
        and segment_size < _MIN_SEGMENT_SIZE
        and not glossary_terms
    ):
        role = ROLE_DATA_TABLE
        segment_demoted = True
        reasons.append(
            f"不隶属任何业务环节（关系图聚类仅 {segment_size} 成员，"
            f"少于 {_MIN_SEGMENT_SIZE}），暂不作为业务对象，待人工确认"
        )

    confidence = _score_to_confidence(
        tech_score if role == ROLE_TECHNICAL else score, role
    )

    # facet B：引用多个不同实体、但因有自有属性/被反向引用而保留为对象者（如订单），
    # 是「关联实体」——保留为业务对象，但标记待复核，请人确认是对象而非纯关系。
    associative_suspect = multi_fk and role == ROLE_BUSINESS_OBJECT
    if associative_suspect:
        reasons.append(
            f"引用 {distinct_fk_targets} 个不同实体但具自有属性/被引用，"
            "疑似关联实体，请确认为对象而非纯关系"
        )
    needs_review = (
        confidence < 0.7
        or associative_suspect
        or fact_named
        or weak_fact_structural
        or segment_demoted
    )

    return ClassificationResult(
        role=role,
        confidence=confidence,
        reason="；".join(reasons) or "无显著信号",
        score=score,
        needs_review=needs_review,
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
