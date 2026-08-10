"""SyncTool Adapter 注册表——统一执行架构下只剩 Flink。

**历史**：此前有 SeaTunnel/DataX/Flink 多工具选择，registry 按镜像可用性 + 装载方式
能力挑工具。统一执行架构废除多工具，搬运 = Flink SQL on YARN，不再有工具选择。

**为什么保留这个文件**：`get_job_adapter` 与 `resolve_docker_image` 仍被 job_planner /
旧 DAG builder 调用（它们要问「用什么工具」「镜像是什么」）。统一架构下答案恒定，但
调用方暂未重构，故保留本模块作适配层，返回 Flink 的恒定答案。

后续可直接内联到调用处，删除本文件（见 H/I 阶段）。
"""

from __future__ import annotations

from app.warehouse.jobs.base import SyncToolAdapter
from app.warehouse.jobs.flink import FlinkAdapter

# 默认（也是唯一）搬运工具：Flink。
DEFAULT_SYNC_TOOL = "flink"

_TOOLS: dict[str, SyncToolAdapter] = {"flink": FlinkAdapter()}


class UnknownSyncToolError(KeyError):
    """统一架构下只剩 flink，为兼容保留此异常。"""

    def __init__(self, tool: str):
        super().__init__(
            f"未知搬运工具 {tool!r}，统一执行架构下只支持 flink（tool 参数应恒为 'flink'）"
        )


class SyncImageUnavailableError(RuntimeError):
    """统一架构下不再走 docker 镜像（Flink 走 YARN），为兼容保留此异常。"""

    def __init__(self, tool: str, image: str, image_overrides: dict[str, str] | None = None):
        self.tool = tool
        self.image = image
        super().__init__(
            f"统一执行架构下搬运走 Flink SQL on YARN，不再使用 docker 镜像（{image}）。"
        )


def resolve_docker_image(
    adapter: SyncToolAdapter, image_overrides: dict[str, str] | None = None
) -> str:
    """统一架构下不再走 docker 镜像，为兼容保留（返回空串）。

    旧 docker 通道用此函数解析镜像；统一架构下 Flink 走 YARN，无镜像概念。
    调用方（job_planner）暂未重构，故保留空实现避免崩溃。
    """
    return ""  # Flink 走 YARN，无 docker 镜像


def available_sync_tools(image_overrides: dict[str, str] | None = None) -> list[str]:
    """统一架构下恒返回 ['flink']。"""
    return ["flink"]


def get_job_adapter(tool: str | None = None) -> SyncToolAdapter:
    """获取工具适配器——统一架构下恒返回 FlinkAdapter。

    ``tool`` 参数为兼容保留（调用方未重构），实际不再使用（只剩 flink）。
    """
    if tool and tool.lower() != "flink":
        raise UnknownSyncToolError(tool)
    return _TOOLS["flink"]


def list_sync_tools() -> list[str]:
    """统一架构下恒返回 ['flink']。"""
    return ["flink"]
