"""治理智能体：五种制品共用一条「草稿 → 校验 → 确认 → 执行 → 溯源」流水线。

与读侧的 Data Agent（Chat BI）是同一智能体的两类技能：读侧问数，写侧造数。

四类实现按风险由低到高注册：④指标 → ③ETL → ①同步 → ⓪部署。
"""

from app.agents import registry
from app.agents.drafters.cluster import ClusterDrafter
from app.agents.drafters.materialize import MaterializeDrafter
from app.agents.drafters.metric import MetricDrafter
from app.agents.drafters.sync import SyncDrafter
from app.agents.drafters.transform import TransformDrafter
from app.agents.executors.cluster import ClusterExecutor
from app.agents.executors.materialize import MaterializeExecutor
from app.agents.executors.metric import MetricExecutor
from app.agents.executors.sync import SyncExecutor
from app.agents.executors.transform import TransformExecutor


def register_builtin_agents() -> None:
    """注册内置实现。幂等，可重复调用。"""
    registry.register("metric", MetricDrafter(), MetricExecutor())
    registry.register("transform", TransformDrafter(), TransformExecutor())
    registry.register("sync", SyncDrafter(), SyncExecutor())
    registry.register("cluster", ClusterDrafter(), ClusterExecutor())
    registry.register("materialize", MaterializeDrafter(), MaterializeExecutor())


register_builtin_agents()

__all__ = ["registry", "register_builtin_agents"]
