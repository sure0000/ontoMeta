"""P1 维度模型扩展：完整的 Kimball 式建模制品。

核心概念：
- 业务过程 (Business Process)
- 粒度 (Grain)
- 事实表 (Fact Table)
- 度量 (Measure) 与可加性
- 维度 (Dimension)
- 一致性维度 (Conformed Dimension)
- 角色扮演维度 (Role-Playing Dimension)
- 缓慢变化维度 (SCD Type)
- 代理键 (Surrogate Key)
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


# ============================================================================
# 度量定义
# ============================================================================


class MeasureSpec(BaseModel):
    """度量规格。"""
    
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    
    # 数据类型
    data_type: str = "numeric"  # numeric/money/percentage/count
    
    # 可加性
    additive_type: Literal["additive", "semi_additive", "non_additive"] = "additive"
    
    # 如果是 semi_additive，指定不可加维度（通常是时间）
    non_additive_dimensions: list[str] = Field(default_factory=list)
    
    # 聚合函数
    aggregation: str = "sum"  # sum/avg/min/max/count/count_distinct
    
    # 口径引用（从本体或已有业务逻辑）
    ontology_field_ref: str | None = None
    business_logic_ref: str | None = None
    
    # 计算表达式（简化版，P3 会完整集成 metric_compiler）
    expression: str | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 维度定义
# ============================================================================


class DimensionAttributeSpec(BaseModel):
    """维度属性。"""
    
    name: str
    display_name: str
    data_type: str = "string"
    ontology_field_ref: str | None = None
    
    model_config = {"extra": "forbid"}


class DimensionSpec(BaseModel):
    """维度规格。"""
    
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    
    # 本体对象引用
    ontology_object_ref: str | None = None
    
    # 自然键
    natural_key: list[str] = Field(default_factory=list)
    
    # 代理键策略
    use_surrogate_key: bool = True
    surrogate_key_name: str | None = None
    
    # SCD 类型
    scd_type: Literal["none", "type1", "type2", "type3"] = "none"
    
    # SCD2 需要的字段
    effective_date_column: str | None = None
    expiry_date_column: str | None = None
    current_flag_column: str | None = None
    version_column: str | None = None
    
    # 维度属性
    attributes: list[DimensionAttributeSpec] = Field(default_factory=list)
    
    # 层级（用于上卷下钻）
    hierarchies: list[dict[str, Any]] = Field(default_factory=list)
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 事实表定义
# ============================================================================


class FactTableSpec(BaseModel):
    """事实表规格。"""
    
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    
    # 业务过程
    business_process: str = Field(..., min_length=1)
    
    # 粒度声明（必须明确）
    grain: str = Field(..., min_length=1, max_length=500)
    
    # 事实类型
    fact_type: Literal["transaction", "periodic_snapshot", "accumulating_snapshot"] = "transaction"
    
    # 本体关系引用（如果事实来自某个关系）
    ontology_relation_ref: str | None = None
    ontology_object_ref: str | None = None
    
    # 度量
    measures: list[MeasureSpec] = Field(default_factory=list)
    
    # 维度外键
    dimension_keys: list[str] = Field(default_factory=list)
    
    # 退化维度（直接存在事实表的维度属性）
    degenerate_dimensions: list[str] = Field(default_factory=list)
    
    # 分区策略
    partition_by: str | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 桥接表定义（多对多关系）
# ============================================================================


class BridgeTableSpec(BaseModel):
    """桥接表规格（处理多对多关系）。"""
    
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    
    # 连接的两个维度
    left_dimension: str
    right_dimension: str
    
    # 权重因子（用于分摊度量）
    weight_factor_column: str | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 角色扮演维度
# ============================================================================


class RolePlayingDimensionSpec(BaseModel):
    """角色扮演维度。
    
    例如：订单日期、发货日期、收货日期都引用同一个日期维度，
    但扮演不同角色。
    """
    
    base_dimension: str  # 基础维度名
    role: str  # 角色名（如 order_date, ship_date）
    role_display_name: str
    foreign_key_name: str | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 一致性维度
# ============================================================================


class ConformedDimensionSpec(BaseModel):
    """一致性维度声明。
    
    标记哪些维度是跨多个业务过程/事实表共享的。
    """
    
    dimension_name: str
    shared_across_facts: list[str] = Field(default_factory=list)
    managed_by: str | None = None  # 负责团队/系统
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 完整维度模型规格（扩展版）
# ============================================================================


class DimensionalModelSpecV2(BaseModel):
    """维度模型规格 V2（完整 Kimball 范式）。"""
    
    # 基础信息
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    
    # 业务过程
    business_process: str = Field(..., min_length=1, max_length=200)
    
    # 本体依赖
    ontology_refs: list[dict[str, Any]] = Field(default_factory=list)
    
    # 事实表
    facts: list[FactTableSpec] = Field(default_factory=list)
    
    # 维度
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    
    # 桥接表
    bridges: list[BridgeTableSpec] = Field(default_factory=list)
    
    # 角色扮演维度
    role_playing_dimensions: list[RolePlayingDimensionSpec] = Field(default_factory=list)
    
    # 一致性维度
    conformed_dimensions: list[ConformedDimensionSpec] = Field(default_factory=list)
    
    # 目标引擎配置
    target: dict[str, Any] | None = None
    
    # 校验报告（编译器填充）
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    
    model_config = {"extra": "forbid"}


__all__ = [
    "MeasureSpec",
    "DimensionAttributeSpec",
    "DimensionSpec",
    "FactTableSpec",
    "BridgeTableSpec",
    "RolePlayingDimensionSpec",
    "ConformedDimensionSpec",
    "DimensionalModelSpecV2",
]
