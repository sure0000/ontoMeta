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


class SyncImageUnavailableError(RuntimeError):
    """选中的工具在本部署里没有可用镜像。

    刻意在**提交前**抛：DAG 一旦落盘并触发，失败会以 Airflow 的
    ``pull access denied for …`` 形式出现在任务日志最深处，读起来像环境故障，
    而真实原因是「这个工具在这套部署里就没配镜像」。
    """

    def __init__(self, tool: str, image: str, image_overrides: dict[str, str] | None = None):
        self.tool = tool
        self.image = image
        # 「改用哪个」要按**本部署**算：部署方给别的工具配过镜像的，那些也是可选项。
        alternatives = [t for t in available_sync_tools(image_overrides) if t != tool]
        super().__init__(
            f"搬运工具「{tool}」没有可用的执行镜像：{image} 无官方发行版，"
            f"需自行构建后用环境变量 ONTOMETA_SYNC_TOOL_IMAGES={tool}=<你的镜像> 指定；"
            f"或改用有可用镜像的工具（{', '.join(alternatives) or '暂无'}）。"
        )


def resolve_docker_image(
    adapter: SyncToolAdapter, image_overrides: dict[str, str] | None = None
) -> str:
    """该工具在本部署里实际要跑的镜像。

    部署方的覆盖优先于适配器默认值——适配器只知道「官方镜像叫什么」，镜像在这套
    部署里叫什么（私有 registry、自建 tag）属部署事实。无覆盖且无官方镜像即报错。
    """
    override = (image_overrides or {}).get(adapter.name)
    if override:
        return override
    if not adapter.has_official_image:
        raise SyncImageUnavailableError(
            adapter.name, adapter.docker_image, image_overrides
        )
    return adapter.docker_image


def available_sync_tools(image_overrides: dict[str, str] | None = None) -> list[str]:
    """镜像真的拿得到的工具。UI 据此禁用其余选项，不给人挖坑。"""
    return sorted(
        name
        for name, a in _TOOLS.items()
        if a.has_official_image or (image_overrides or {}).get(name)
    )


def get_job_adapter(tool: str | None = None) -> SyncToolAdapter:
    key = (tool or DEFAULT_SYNC_TOOL).lower()
    try:
        return _TOOLS[key]
    except KeyError:
        raise UnknownSyncToolError(tool or "") from None


def list_sync_tools() -> list[str]:
    return sorted(_TOOLS)
