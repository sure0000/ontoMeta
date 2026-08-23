"""Doris warehouse architecture invariants.

This module is deliberately small and dependency-free so the same policy can be
used by API validation, artifact validation and execution code.  A default is
not a fallback: new warehouse work is rejected unless it targets Doris.
"""

from __future__ import annotations

WAREHOUSE_ENGINE = "doris"
ALLOWED_EXECUTION_ENGINES: dict[str, frozenset[str]] = {
    "materialize": frozenset({"doris"}),
    "sync": frozenset({"flink"}),
    "transform": frozenset({"doris"}),
    "metric": frozenset({"doris"}),
    "query": frozenset({"doris"}),
}


def require_doris(engine: str | None, *, operation: str = "数仓任务") -> str:
    """Return Doris or raise; never silently translate another engine to Doris."""
    value = (engine or WAREHOUSE_ENGINE).strip().lower()
    if value != WAREHOUSE_ENGINE:
        raise ValueError(f"{operation}只允许使用 Doris，引擎 {value!r} 已被拒绝")
    return WAREHOUSE_ENGINE


def require_doris_datasource(datasource: object | None, *, operation: str = "数仓任务") -> object:
    """Validate the explicit target datasource without inspecting catalog_name."""
    if datasource is None:
        raise ValueError(f"{operation}必须绑定默认 Doris 数仓数据源")
    if getattr(datasource, "kind", "").lower() != WAREHOUSE_ENGINE:
        raise ValueError(f"{operation}目标数据源必须是 Doris，不能使用 {getattr(datasource, 'kind', None)!r}")
    if getattr(datasource, "purpose", "") != "warehouse":
        raise ValueError(f"{operation}目标数据源必须声明 purpose=warehouse")
    if not getattr(datasource, "enabled", True):
        raise ValueError("默认 Doris 数仓数据源已停用")
    return datasource
