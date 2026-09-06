"""存储形态的维度模型 JSON → ``DimensionalModelSpecV2``。

**为什么需要这层**：同一个概念在本子系统里长出了两套词汇，谁也不认识谁——

    存储（DimensionalModel 的 JSON 列）   规格（DimensionalModelSpecV2）
    fact_tables                          facts
    source_object_id                     ontology_object_ref
    surrogate_key                        surrogate_key_name / use_surrogate_key
    scd_type: "scd2"                     scd_type: "type2"
    scd_config: {effective_date, ...}    effective_date_column / expiry_date_column / ...
    dimension.role_playing               顶层 role_playing_dimensions[]

于是校验器（写给规格）对着实际落库的数据一条也跑不了，整个 ``dimensional_model_validator``
成了没有任何调用方的死代码。补编译能力之前必须先把这条缝补上，否则只会长出**第三套**词汇。

本模块是**唯一**的翻译处：服务层的校验与编译都从这里拿规格，不各自解析 JSON。
两套词汇都接受（``fact_tables`` 与 ``facts`` 都认），以便存量数据与新写入并存。
"""

from __future__ import annotations

from typing import Any

from app.schemas.dimensional_model import (
    ConformedDimensionSpec,
    DimensionalModelSpecV2,
    DimensionAttributeSpec,
    DimensionSpec,
    FactTableSpec,
    MeasureSpec,
    RolePlayingDimensionSpec,
)

#: 存储用的 SCD 字面值 → 规格字面值。两边各写各的，映射只此一处。
_SCD_STORAGE_TO_SPEC = {
    "": "none",
    "none": "none",
    "scd0": "none",
    "scd1": "type1",
    "type1": "type1",
    "scd2": "type2",
    "type2": "type2",
    "scd3": "type3",
    "type3": "type3",
}

#: 规格字面值 → ``MaterializationContract.scd_type`` 的合法取值。
#: 契约只表达得了 none/scd1/scd2——type3（列内多版本）没有对应的落层语义，
#: 编译时必须**报出来**而不是悄悄降级成 scd1，那会让人以为历史留住了。
SPEC_SCD_TO_CONTRACT = {"none": "none", "type1": "scd1", "type2": "scd2"}


def _literal(raw: Any, allowed: tuple[str, ...], field: str) -> str:
    """把存储里的自由字符串收窄到规格允许的字面值集合。

    **不静默兜底成默认值**：``additive_type`` 写错时默认成 "additive"，会让一个本不可加的
    度量被求和——那是错数，而且没有任何提示。宁可让它带着原值往下走，由 Pydantic 报出
    「只允许 additive / semi_additive / non_additive」，最终经 validate_model 变成一条
    人看得懂的 schema issue。这里只负责**类型可核**，不负责替数据擦屁股。
    """
    value = str(raw)
    return value if value in allowed else value


def _first(source: dict[str, Any], *names: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非空的键。两套词汇共存时用它抹平差异。"""
    for name in names:
        value = source.get(name)
        if value not in (None, "", [], {}):
            return value
    return default


def _measure(raw: dict[str, Any]) -> MeasureSpec:
    name = str(_first(raw, "name", default="") or "")
    return MeasureSpec(
        name=name,
        display_name=str(_first(raw, "display_name", "name", default=name) or name),
        description=raw.get("description"),
        data_type=str(_first(raw, "data_type", default="numeric")),
        # 不合法值不在这里兜底，交给 Pydantic 报错（见 _literal 的说明）
        additive_type=_literal(  # type: ignore[arg-type]
            _first(raw, "additive_type", default="additive"),
            ("additive", "semi_additive", "non_additive"),
            "additive_type",
        ),
        non_additive_dimensions=list(_first(raw, "non_additive_dimensions", default=[])),
        aggregation=str(_first(raw, "aggregation", default="sum")),
        ontology_field_ref=_first(raw, "ontology_field_ref", "field"),
        business_logic_ref=raw.get("business_logic_ref"),
        expression=raw.get("expression"),
    )


def _attribute(raw: dict[str, Any]) -> DimensionAttributeSpec:
    name = str(_first(raw, "name", default="") or "")
    return DimensionAttributeSpec(
        name=name,
        display_name=str(_first(raw, "display_name", "name", default=name) or name),
        data_type=str(_first(raw, "data_type", default="string")),
        ontology_field_ref=_first(raw, "ontology_field_ref", "field"),
    )


def _dimension(raw: dict[str, Any]) -> DimensionSpec:
    name = str(_first(raw, "name", default="") or "")
    scd_config = raw.get("scd_config") or {}
    surrogate = _first(raw, "surrogate_key_name", "surrogate_key")
    return DimensionSpec(
        name=name,
        display_name=str(_first(raw, "display_name", "name", default=name) or name),
        description=raw.get("description"),
        ontology_object_ref=_first(raw, "ontology_object_ref", "source_object_id"),
        natural_key=list(_first(raw, "natural_key", default=[])),
        # 存储侧没有 use_surrogate_key 这个开关，只有「填没填 surrogate_key」。
        use_surrogate_key=bool(_first(raw, "use_surrogate_key", default=bool(surrogate))),
        surrogate_key_name=surrogate,
        scd_type=_SCD_STORAGE_TO_SPEC.get(  # type: ignore[arg-type]
            str(_first(raw, "scd_type", default="none")).lower(), "none"
        ),
        # SCD 列在存储侧收在 scd_config 里，规格侧是平铺的三个字段。
        effective_date_column=_first(raw, "effective_date_column") or scd_config.get("effective_date"),
        expiry_date_column=_first(raw, "expiry_date_column") or scd_config.get("expiration_date"),
        current_flag_column=_first(raw, "current_flag_column") or scd_config.get("current_flag"),
        version_column=_first(raw, "version_column") or scd_config.get("version"),
        attributes=[_attribute(a) for a in (raw.get("attributes") or []) if isinstance(a, dict)],
        hierarchies=list(_first(raw, "hierarchies", default=[])),
    )


def _fact(raw: dict[str, Any], *, model_process: str, model_grain: str) -> FactTableSpec:
    name = str(_first(raw, "name", default="") or "")
    return FactTableSpec(
        name=name,
        display_name=str(_first(raw, "display_name", "name", default=name) or name),
        description=raw.get("description"),
        # 事实表没自带业务过程/粒度时，继承模型级的声明——模型级那两个字段是必填的，
        # 拿不到就退回空串让校验器去报「必须明确声明粒度」，不在这里替它编一个。
        business_process=str(_first(raw, "business_process", default=model_process) or "未声明"),
        grain=str(_first(raw, "grain", default=model_grain) or ""),
        fact_type=_literal(  # type: ignore[arg-type]
            _first(raw, "fact_type", default="transaction"),
            ("transaction", "periodic_snapshot", "accumulating_snapshot"),
            "fact_type",
        ),
        ontology_relation_ref=raw.get("ontology_relation_ref"),
        ontology_object_ref=_first(raw, "ontology_object_ref", "source_object_id"),
        measures=[_measure(m) for m in (raw.get("measures") or []) if isinstance(m, dict)],
        dimension_keys=list(_first(raw, "dimension_keys", default=[])),
        degenerate_dimensions=list(_first(raw, "degenerate_dimensions", default=[])),
        partition_by=_first(raw, "partition_by", "partition_key"),
    )


def _role_playing(model_dimensions: list[dict[str, Any]], declared: list[Any]) -> list[RolePlayingDimensionSpec]:
    """角色扮演维度：顶层声明 + 维度内联 ``role_playing`` 两种写法都收。"""
    out: list[RolePlayingDimensionSpec] = []
    for raw in declared or []:
        if not isinstance(raw, dict):
            continue
        out.append(
            RolePlayingDimensionSpec(
                base_dimension=str(_first(raw, "base_dimension", default="") or ""),
                role=str(_first(raw, "role", default="") or ""),
                role_display_name=str(_first(raw, "role_display_name", "role", default="") or ""),
                foreign_key_name=raw.get("foreign_key_name"),
            )
        )
    for dim in model_dimensions:
        inline = dim.get("role_playing")
        if not isinstance(inline, dict):
            continue
        base = str(_first(inline, "base_dimension", "base", default="") or "")
        role = str(_first(inline, "role", default=dim.get("name")) or "")
        out.append(
            RolePlayingDimensionSpec(
                base_dimension=base,
                role=role,
                role_display_name=str(_first(inline, "role_display_name", default=role) or role),
                foreign_key_name=inline.get("foreign_key_name"),
            )
        )
    return out


def to_spec(model) -> DimensionalModelSpecV2:
    """``DimensionalModel`` 行 → 规格对象。不写库，纯翻译。"""
    facts_raw = [f for f in (model.fact_tables or []) if isinstance(f, dict)]
    dims_raw = [d for d in (model.dimensions or []) if isinstance(d, dict)]
    conformed_raw = [c for c in (model.conformed_dimensions or []) if isinstance(c, dict)]

    return DimensionalModelSpecV2(
        name=model.name,
        display_name=model.display_name,
        description=model.description,
        business_process=model.business_process or "未声明",
        facts=[_fact(f, model_process=model.business_process or "", model_grain=model.grain or "") for f in facts_raw],
        dimensions=[_dimension(d) for d in dims_raw],
        role_playing_dimensions=_role_playing(dims_raw, []),
        conformed_dimensions=[
            ConformedDimensionSpec(
                dimension_name=str(_first(c, "dimension_name", "name", default="") or ""),
                shared_across_facts=list(_first(c, "shared_across_facts", default=[])),
                managed_by=c.get("managed_by"),
            )
            for c in conformed_raw
        ],
    )


__all__ = ["to_spec", "SPEC_SCD_TO_CONTRACT"]
