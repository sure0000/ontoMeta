"""搬运作业适配层：本体 → 与工具无关的 JobSpec → 各搬运工具的作业配置。

与同级 ``adapters/``（本体 → 各引擎 DDL）并列：那边管「目标表长什么样」，
这边管「数据怎么搬过去」。工具特定逻辑只允许存在于本包的各 Adapter 实现中。
"""

from app.warehouse.jobs.base import (
    LOAD_MODES,
    ColumnMapping,
    JobEndpoint,
    JobPlan,
    JobSpec,
    SyncToolAdapter,
)
from app.warehouse.jobs.registry import (
    DEFAULT_SYNC_TOOL,
    UnknownSyncToolError,
    get_job_adapter,
    list_sync_tools,
)

__all__ = [
    "LOAD_MODES",
    "ColumnMapping",
    "JobEndpoint",
    "JobPlan",
    "JobSpec",
    "SyncToolAdapter",
    "DEFAULT_SYNC_TOOL",
    "UnknownSyncToolError",
    "get_job_adapter",
    "list_sync_tools",
]
