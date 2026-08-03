"""backend 档位选择与能力声明。

M14 只含 ``native`` 一档。``capabilities()`` 如实声明它能搬什么——planner 据此把不支持的表
列进 ``unsupported`` 而非静默降级（§3.1）。seatunnel 档（CDC / native 覆盖不了的组合）留待后续。
"""

from __future__ import annotations

import importlib.util

from sync_runner.contract import Capabilities, WireJobSpec

# native 能搬的源/目标平台与装载方式。目标里没有 hive/clickhouse——它们不适合逐行 INSERT，
# 该走 seatunnel 的专用 sink（Stream Load / Hive sink），M14 未含，故此处不谎称支持。
NATIVE_SOURCES = frozenset(
    {"mysql", "mariadb", "postgres", "postgresql", "sqlite", "mssql"}
)
NATIVE_SINKS = frozenset(
    {"mysql", "mariadb", "postgres", "postgresql", "sqlite", "doris", "starrocks"}
)
NATIVE_MODES = frozenset({"full", "incremental"})

# 平台 → 判断驱动是否装齐用的 python 模块名。镜像构建期烘进来，运行期零挂载（§3.1）。
_DRIVER_MODULES = {
    "mysql": "pymysql",
    "mariadb": "pymysql",
    "doris": "pymysql",
    "starrocks": "pymysql",
    "postgres": "psycopg2",
    "postgresql": "psycopg2",
    "mssql": "pyodbc",
    "sqlite": "sqlite3",
}


def installed_drivers() -> list[str]:
    """镜像里实际装到的驱动。``GET /capabilities`` 如实带出，排查连不上时第一个看它。"""
    found = set()
    for platform, module in _DRIVER_MODULES.items():
        if importlib.util.find_spec(module) is not None:
            found.add(platform)
    return sorted(found)


def native_supports(spec: WireJobSpec) -> bool:
    """这张表能否由 native 搬。源/目标平台在支持集内、装载方式受支持、且驱动已装。"""
    drivers = set(installed_drivers())
    src = (spec.source.platform or "").lower()
    dst = (spec.target.platform or "").lower()
    return (
        src in NATIVE_SOURCES
        and dst in NATIVE_SINKS
        and spec.mode in NATIVE_MODES
        and src in drivers
        and dst in drivers
    )


def capabilities() -> Capabilities:
    drivers = set(installed_drivers())
    return Capabilities(
        backends=["native"],
        sources=sorted(NATIVE_SOURCES & drivers),
        sinks=sorted(NATIVE_SINKS & drivers),
        modes=sorted(NATIVE_MODES),
        drivers=sorted(drivers),
    )
