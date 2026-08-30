"""本体形式化类型系统：受控枚举 + 语义代数。

形式化校验的公共地基。基数、语义类型从「自由文本」升级为**枚举**后，
下列问题才变得**可判定**：
  - 一个字段能不能被 SUM？（语义类型 = measure 才行）
  - 两个对象能不能 JOIN 而不使度量翻倍？（看基数）
  - 一条 SQL / 一个口径是否语义合法？

本模块只定义「取值域」与「能力谓词」，不触库、不依赖 ORM，供 SQL 语义证明器
（sql_soundness）、断言校验器（answer_verifier）、发布不变式（ontology_formal）
共用——杜绝各处各判、语义分叉。

设计见 FORMAL_VALIDATION_IMPL.md 第一部分。
"""

from __future__ import annotations

import enum


class Cardinality(str, enum.Enum):
    """关系基数。方向语义：``source`` 端 → ``target`` 端。

    - ONE_TO_MANY：一个 source 对应多个 target（如 客户 1—N 订单）
    - MANY_TO_ONE：多个 source 对应一个 target（如 订单 N—1 客户）
    - MANY_TO_MANY：多对多，必经桥表（如 订单 N—M 标签）
    """

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class SemanticType(str, enum.Enum):
    """字段语义类型（决定该字段在查询里「能做什么」）。

    与 object_classifier 的字段画像分组对齐：measure≈度量、categorical/textual≈描述性、
    temporal≈时间粒度、technical≈技术字段。
    """

    IDENTIFIER = "identifier"    # 主键 / 外键 / 业务编号：可 JOIN、可 COUNT DISTINCT，不可 SUM
    MEASURE = "measure"          # 金额 / 数量：可 SUM/AVG，不宜作分组主维
    TEMPORAL = "temporal"        # 日期 / 时间：可时间窗过滤 / GROUP BY，不可 SUM
    CATEGORICAL = "categorical"  # 类别 / 状态 / 枚举：可 GROUP BY / 等值过滤，不可 SUM
    TEXTUAL = "textual"          # 名称 / 自由文本：仅可 LIKE / 展示，不可聚合
    TECHNICAL = "technical"      # 技术字段（token/hash/config）：默认不入业务查询
    UNKNOWN = "unknown"          # 迁移期占位：无法归一时用它，进复核，不阻断存量


# ---------------------------------------------------------------------------
# 语义代数：一个语义类型「能做什么」。SQL 证明器与发布不变式都查这里。
# 保守原则：UNKNOWN 一律返回 False（拿不准即禁止 → 拒答优先，绝不错答）。
# ---------------------------------------------------------------------------
_AGGREGATABLE = frozenset({SemanticType.MEASURE})
# TEXTUAL 可分组：按名称/编码分组（「按上级客户分组统计下级数量」）是常规分析，不是口径错误。
# 真正要拦的是 **按度量原值分组**（仍拦）与 **SUM 非度量**（仍拦）。
#
# 排除 TEXTUAL 曾让证明器在真实本体上几乎拦下一切分组：本体生成把绝大多数字段标成
# attribute（→ TEXTUAL），实测某 ERP 本体 2466 个已发布字段里 2016 个是 attribute，
# 可分组的只剩 17%。后果不是「更安全」，而是模型绕过守卫——被拒后改成拉明细行自己
# 心算，再被答案校验判成「未经查询证实的数值」。守卫把它想防的行为逼了出来。
#
# UNKNOWN 仍不可分组：那是「归一不出来」的占位，保守原则照旧（拿不准即禁止）。
_GROUPABLE = frozenset(
    {
        SemanticType.IDENTIFIER,
        SemanticType.TEMPORAL,
        SemanticType.CATEGORICAL,
        SemanticType.TEXTUAL,
    }
)
_JOINABLE = frozenset({SemanticType.IDENTIFIER})
_FILTERABLE = frozenset(
    {
        SemanticType.IDENTIFIER,
        SemanticType.TEMPORAL,
        SemanticType.CATEGORICAL,
        SemanticType.MEASURE,
    }
)


def can_aggregate(st: SemanticType) -> bool:
    """可否 SUM/AVG（只有度量可以）。"""
    return st in _AGGREGATABLE


def can_group_by(st: SemanticType) -> bool:
    """可否作 GROUP BY 维度（标识/时间/类别/文本可以；度量与 UNKNOWN 不可）。"""
    return st in _GROUPABLE


def can_join_on(st: SemanticType) -> bool:
    """可否作 JOIN 键（只有标识类可以）。"""
    return st in _JOINABLE


def can_filter(st: SemanticType) -> bool:
    """可否作 WHERE 过滤列。"""
    return st in _FILTERABLE


# ---------------------------------------------------------------------------
# 归一化：把存量自由文本 / LLM 输出映射到枚举。
# 认不出 → 基数返回 None、语义类型返回 UNKNOWN（均不抛错，交由复核队列处理）。
# ---------------------------------------------------------------------------
_CARDINALITY_ALIASES: dict[str, Cardinality] = {
    "one_to_one": Cardinality.ONE_TO_ONE,
    "1:1": Cardinality.ONE_TO_ONE,
    "1-1": Cardinality.ONE_TO_ONE,
    "one-to-one": Cardinality.ONE_TO_ONE,
    "one_to_many": Cardinality.ONE_TO_MANY,
    "1:n": Cardinality.ONE_TO_MANY,
    "1:m": Cardinality.ONE_TO_MANY,
    "1-n": Cardinality.ONE_TO_MANY,
    "one-to-many": Cardinality.ONE_TO_MANY,
    "many_to_one": Cardinality.MANY_TO_ONE,
    "n:1": Cardinality.MANY_TO_ONE,
    "m:1": Cardinality.MANY_TO_ONE,
    "n-1": Cardinality.MANY_TO_ONE,
    "many-to-one": Cardinality.MANY_TO_ONE,
    "many_to_many": Cardinality.MANY_TO_MANY,
    "n:m": Cardinality.MANY_TO_MANY,
    "n:n": Cardinality.MANY_TO_MANY,
    "m:n": Cardinality.MANY_TO_MANY,
    "many-to-many": Cardinality.MANY_TO_MANY,
}


def normalize_cardinality(value: str | None) -> Cardinality | None:
    """自由文本基数 → 枚举。认不出返回 None（表示「未知」，扇出判定据此保守拒答）。"""
    if value is None:
        return None
    if isinstance(value, Cardinality):
        return value
    key = str(value).strip().lower().replace(" ", "")
    return _CARDINALITY_ALIASES.get(key)


# 语义类型别名：容纳历史值与常见近义写法。
_SEMANTIC_ALIASES: dict[str, SemanticType] = {
    "identifier": SemanticType.IDENTIFIER,
    "id": SemanticType.IDENTIFIER,
    "key": SemanticType.IDENTIFIER,
    "pk": SemanticType.IDENTIFIER,
    "fk": SemanticType.IDENTIFIER,
    "code": SemanticType.IDENTIFIER,
    "measure": SemanticType.MEASURE,
    "amount": SemanticType.MEASURE,
    "metric": SemanticType.MEASURE,
    "quantity": SemanticType.MEASURE,
    "number": SemanticType.MEASURE,
    "temporal": SemanticType.TEMPORAL,
    "datetime": SemanticType.TEMPORAL,
    "date": SemanticType.TEMPORAL,
    "time": SemanticType.TEMPORAL,
    "timestamp": SemanticType.TEMPORAL,
    "categorical": SemanticType.CATEGORICAL,
    "category": SemanticType.CATEGORICAL,
    "enum": SemanticType.CATEGORICAL,
    "flag": SemanticType.CATEGORICAL,
    "status": SemanticType.CATEGORICAL,
    "boolean": SemanticType.CATEGORICAL,
    "bool": SemanticType.CATEGORICAL,
    "textual": SemanticType.TEXTUAL,
    "text": SemanticType.TEXTUAL,
    "attribute": SemanticType.TEXTUAL,
    "name": SemanticType.TEXTUAL,
    "string": SemanticType.TEXTUAL,
    "description": SemanticType.TEXTUAL,
    "technical": SemanticType.TECHNICAL,
    "system": SemanticType.TECHNICAL,
    "internal": SemanticType.TECHNICAL,
}


def normalize_semantic_type(value: str | None) -> SemanticType:
    """自由文本语义类型 → 枚举。认不出返回 UNKNOWN（保守：不可聚合/分组/JOIN）。"""
    if value is None:
        return SemanticType.UNKNOWN
    if isinstance(value, SemanticType):
        return value
    key = str(value).strip().lower()
    return _SEMANTIC_ALIASES.get(key, SemanticType.UNKNOWN)


def is_valid_cardinality(value: str | None) -> bool:
    """给 pydantic / 服务层校验用：是否为可识别的基数取值。"""
    return normalize_cardinality(value) is not None


def is_valid_semantic_type(value: str | None) -> bool:
    """给 pydantic / 服务层校验用：是否为可识别（非 UNKNOWN）的语义类型。"""
    return normalize_semantic_type(value) is not SemanticType.UNKNOWN


__all__ = [
    "Cardinality",
    "SemanticType",
    "can_aggregate",
    "can_group_by",
    "can_join_on",
    "can_filter",
    "normalize_cardinality",
    "normalize_semantic_type",
    "is_valid_cardinality",
    "is_valid_semantic_type",
]
