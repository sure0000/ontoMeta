"""本体 → 多引擎物理投影的适配层。

本体是引擎无关的逻辑模型；本包负责把 LogicalSchema 渲染成各引擎 DDL。
引擎特定逻辑只允许存在于 ``adapters/`` 下的实现中。
"""

from app.warehouse.capabilities import (
    Capabilities,
    CapabilityError,
    CapabilityGap,
    ConstraintSupport,
    GapSeverity,
    check_table,
)
from app.warehouse.logical_schema import (
    LogicalColumn,
    LogicalConstraint,
    LogicalSchema,
    LogicalTable,
)
from app.warehouse.registry import (
    DEFAULT_ENGINE,
    UnknownEngineError,
    get_adapter,
    list_adapters,
    list_engines,
)

__all__ = [
    "Capabilities",
    "CapabilityError",
    "CapabilityGap",
    "ConstraintSupport",
    "GapSeverity",
    "check_table",
    "LogicalColumn",
    "LogicalConstraint",
    "LogicalSchema",
    "LogicalTable",
    "DEFAULT_ENGINE",
    "UnknownEngineError",
    "get_adapter",
    "list_adapters",
    "list_engines",
]
