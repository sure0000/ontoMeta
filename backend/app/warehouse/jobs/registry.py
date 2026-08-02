"""SyncTool Adapter 注册表。

Planner 只通过这里拿 Adapter——不得直接 import 具体工具实现，否则工具逻辑会渗出去。
与 ``app/warehouse/registry.py``（Dialect Adapter）同构，刻意保持一致的形状。
"""

from __future__ import annotations

from app.warehouse.jobs.base import SyncToolAdapter
from app.warehouse.jobs.datax import DataXAdapter
from app.warehouse.jobs.flink import FlinkAdapter
from app.warehouse.jobs.seatunnel import SeaTunnelAdapter

# 默认搬运工具：仓库既有 SyncExecutor 已在产其配置，且 BM 已纳管，不新增运维面。
DEFAULT_SYNC_TOOL = "seatunnel"

_TOOLS: dict[str, SyncToolAdapter] = {
    a.name: a for a in (SeaTunnelAdapter(), DataXAdapter(), FlinkAdapter())
}


class UnknownSyncToolError(KeyError):
    def __init__(self, tool: str):
        super().__init__(f"未知搬运工具 {tool!r}，可选：{', '.join(sorted(_TOOLS))}")


def get_job_adapter(tool: str | None = None) -> SyncToolAdapter:
    key = (tool or DEFAULT_SYNC_TOOL).lower()
    try:
        return _TOOLS[key]
    except KeyError:
        raise UnknownSyncToolError(tool or "") from None


def list_sync_tools() -> list[str]:
    return sorted(_TOOLS)
