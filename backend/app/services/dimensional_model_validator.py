"""维度模型校验器。

职责：
- 粒度一致性校验
- 度量可加性校验
- 维度引用完整性
- SCD 配置完整性
- 代理键冲突检测
- 角色扮演维度引用
- 一致性维度约束
"""

from typing import Any

from app.schemas.dimensional_model import DimensionalModelSpecV2


class DimensionalModelValidator:
    """维度模型校验器。"""
    
    def __init__(self, model_spec: DimensionalModelSpecV2):
        self.spec = model_spec
        self.issues: list[dict[str, Any]] = []
    
    def validate(self) -> list[dict[str, Any]]:
        """执行所有校验规则。"""
        self.issues = []
        
        self._validate_business_process()
        self._validate_facts()
        self._validate_dimensions()
        self._validate_dimension_references()
        self._validate_role_playing_dimensions()
        self._validate_conformed_dimensions()
        self._validate_bridges()
        
        return self.issues
    
    def _add_issue(
        self,
        severity: str,
        category: str,
        message: str,
        context: dict[str, Any] | None = None,
    ):
        """添加校验问题。"""
        self.issues.append({
            "severity": severity,  # error/warning/info
            "category": category,
            "message": message,
            "context": context or {},
        })
    
    def _validate_business_process(self):
        """校验业务过程。"""
        if not self.spec.business_process:
            self._add_issue(
                "error",
                "business_process",
                "必须声明业务过程",
            )
    
    def _validate_facts(self):
        """校验事实表。"""
        if not self.spec.facts:
            self._add_issue(
                "warning",
                "facts",
                "维度模型没有定义任何事实表",
            )
            return
        
        fact_names = set()
        
        for fact in self.spec.facts:
            # 检查重名
            if fact.name in fact_names:
                self._add_issue(
                    "error",
                    "facts",
                    f"事实表名称重复: {fact.name}",
                    {"fact": fact.name},
                )
            fact_names.add(fact.name)
            
            # 检查粒度声明
            if not fact.grain:
                self._add_issue(
                    "error",
                    "grain",
                    f"事实表 {fact.name} 必须明确声明粒度",
                    {"fact": fact.name},
                )
            
            # 检查度量
            if not fact.measures:
                self._add_issue(
                    "warning",
                    "measures",
                    f"事实表 {fact.name} 没有定义度量",
                    {"fact": fact.name},
                )
            
            # 检查维度外键
            if not fact.dimension_keys and not fact.degenerate_dimensions:
                self._add_issue(
                    "warning",
                    "dimensions",
                    f"事实表 {fact.name} 没有关联任何维度",
                    {"fact": fact.name},
                )
            
            # 校验度量
            self._validate_measures(fact)
    
    def _validate_measures(self, fact):
        """校验度量定义。"""
        measure_names = set()
        
        for measure in fact.measures:
            if measure.name in measure_names:
                self._add_issue(
                    "error",
                    "measures",
                    f"事实表 {fact.name} 中度量名称重复: {measure.name}",
                    {"fact": fact.name, "measure": measure.name},
                )
            measure_names.add(measure.name)
            
            # 半可加度量必须声明不可加维度
            if measure.additive_type == "semi_additive":
                if not measure.non_additive_dimensions:
                    self._add_issue(
                        "warning",
                        "additivity",
                        f"半可加度量 {measure.name} 应声明不可加维度",
                        {
                            "fact": fact.name,
                            "measure": measure.name,
                        },
                    )
            
            # 不可加度量不能用 sum
            if measure.additive_type == "non_additive":
                if measure.aggregation == "sum":
                    self._add_issue(
                        "error",
                        "additivity",
                        f"不可加度量 {measure.name} 不能使用 sum 聚合",
                        {
                            "fact": fact.name,
                            "measure": measure.name,
                        },
                    )
    
    def _validate_dimensions(self):
        """校验维度定义。"""
        if not self.spec.dimensions:
            self._add_issue(
                "warning",
                "dimensions",
                "维度模型没有定义任何维度",
            )
            return
        
        dim_names = set()
        
        for dim in self.spec.dimensions:
            # 检查重名
            if dim.name in dim_names:
                self._add_issue(
                    "error",
                    "dimensions",
                    f"维度名称重复: {dim.name}",
                    {"dimension": dim.name},
                )
            dim_names.add(dim.name)
            
            # 如果使用代理键，检查名称
            if dim.use_surrogate_key and not dim.surrogate_key_name:
                self._add_issue(
                    "info",
                    "surrogate_key",
                    f"维度 {dim.name} 使用代理键但未指定名称，将使用默认命名",
                    {"dimension": dim.name},
                )
            
            # 校验 SCD
            self._validate_scd(dim)
    
    def _validate_scd(self, dim):
        """校验 SCD 配置。"""
        if dim.scd_type == "type2":
            missing = []
            if not dim.effective_date_column:
                missing.append("effective_date_column")
            if not dim.expiry_date_column:
                missing.append("expiry_date_column")
            if not dim.current_flag_column:
                missing.append("current_flag_column")
            
            if missing:
                self._add_issue(
                    "error",
                    "scd",
                    f"维度 {dim.name} 配置为 SCD Type 2，但缺少必要字段: {', '.join(missing)}",
                    {"dimension": dim.name, "missing_fields": missing},
                )
        
        elif dim.scd_type == "type3":
            if not dim.version_column:
                self._add_issue(
                    "warning",
                    "scd",
                    f"维度 {dim.name} 配置为 SCD Type 3，建议指定 version_column",
                    {"dimension": dim.name},
                )
    
    def _validate_dimension_references(self):
        """校验事实表对维度的引用。"""
        available_dims = {dim.name for dim in self.spec.dimensions}
        
        for fact in self.spec.facts:
            for dim_key in fact.dimension_keys:
                if dim_key not in available_dims:
                    self._add_issue(
                        "error",
                        "dimension_reference",
                        f"事实表 {fact.name} 引用了不存在的维度: {dim_key}",
                        {"fact": fact.name, "dimension": dim_key},
                    )
    
    def _validate_role_playing_dimensions(self):
        """校验角色扮演维度。"""
        available_dims = {dim.name for dim in self.spec.dimensions}
        
        for rp_dim in self.spec.role_playing_dimensions:
            if rp_dim.base_dimension not in available_dims:
                self._add_issue(
                    "error",
                    "role_playing",
                    f"角色扮演维度 {rp_dim.role} 引用了不存在的基础维度: {rp_dim.base_dimension}",
                    {
                        "role": rp_dim.role,
                        "base_dimension": rp_dim.base_dimension,
                    },
                )
    
    def _validate_conformed_dimensions(self):
        """校验一致性维度。"""
        available_dims = {dim.name for dim in self.spec.dimensions}
        fact_names = {fact.name for fact in self.spec.facts}
        
        for conf_dim in self.spec.conformed_dimensions:
            # 检查维度是否存在
            if conf_dim.dimension_name not in available_dims:
                self._add_issue(
                    "error",
                    "conformed_dimension",
                    f"一致性维度声明引用了不存在的维度: {conf_dim.dimension_name}",
                    {"dimension": conf_dim.dimension_name},
                )
            
            # 检查事实表引用
            for fact_ref in conf_dim.shared_across_facts:
                if fact_ref not in fact_names:
                    self._add_issue(
                        "warning",
                        "conformed_dimension",
                        f"一致性维度 {conf_dim.dimension_name} 引用了不存在的事实表: {fact_ref}",
                        {
                            "dimension": conf_dim.dimension_name,
                            "fact": fact_ref,
                        },
                    )
    
    def _validate_bridges(self):
        """校验桥接表。"""
        available_dims = {dim.name for dim in self.spec.dimensions}
        
        for bridge in self.spec.bridges:
            if bridge.left_dimension not in available_dims:
                self._add_issue(
                    "error",
                    "bridge",
                    f"桥接表 {bridge.name} 引用了不存在的左侧维度: {bridge.left_dimension}",
                    {"bridge": bridge.name, "dimension": bridge.left_dimension},
                )
            
            if bridge.right_dimension not in available_dims:
                self._add_issue(
                    "error",
                    "bridge",
                    f"桥接表 {bridge.name} 引用了不存在的右侧维度: {bridge.right_dimension}",
                    {"bridge": bridge.name, "dimension": bridge.right_dimension},
                )


def validate_dimensional_model(spec: DimensionalModelSpecV2) -> list[dict[str, Any]]:
    """便捷函数：校验维度模型并返回问题列表。"""
    validator = DimensionalModelValidator(spec)
    return validator.validate()


__all__ = ["DimensionalModelValidator", "validate_dimensional_model"]
