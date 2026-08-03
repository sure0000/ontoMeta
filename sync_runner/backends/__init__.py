"""backend 档位选择与能力声明。

两档：``native``（内置 JDBC 分批搬运）与 ``seatunnel``（提交给常驻 Zeta 集群）。
``capabilities()`` 如实声明**实际能搬什么**——planner 据此把不支持的表列进 ``unsupported``
而非静默降级（§3.1）。

**挑档规则**：native 优先，它搬不了的交给 seatunnel。理由是 native 不依赖任何外部集群、
启动开销为零、且增量能回读目标表水位；seatunnel 只在 native 覆盖不到的地方出场
（典型是 Hive 目标——要写 HDFS 上的 ORC、要过 metastore，逐行 JDBC 不成立）。
"""

from __future__ import annotations

import importlib.util

from sync_runner.backends import seatunnel
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


def pick_backend(spec: WireJobSpec) -> str | None:
    """这张表交给哪一档搬。都搬不了返回 None——**不静默降级**，由调用方明确失败。"""
    if native_supports(spec):
        return "native"
    if seatunnel.available() and seatunnel.supports(spec):
        return "seatunnel"
    return None


def capabilities() -> Capabilities:
    """两档合起来的实际能力。

    ``sinks``/``modes`` 是扁平集合，只用于人读与粗判；**门禁要看 ``sink_modes``**——
    两个扁平集合分别判会放过不存在的组合（seatunnel 能写 hive 但只做全量、native 能做
    增量但写不了 hive，合看会让「hive + 增量」通过，跑到执行侧才失败）。
    """
    drivers = set(installed_drivers())
    native_sinks = NATIVE_SINKS & drivers
    backends = ["native"]

    # {目标: 支持的装载方式}。native 的每个 sink 都支持它声明的全部 mode。
    sink_modes: dict[str, set[str]] = {s: set(NATIVE_MODES) for s in native_sinks}
    sources = set(NATIVE_SOURCES & drivers)

    if seatunnel.available():
        backends.append("seatunnel")
        # seatunnel 档的源走 JDBC，与 runner 本地装没装 Python 驱动无关。
        sources |= set(seatunnel._JDBC)
        for sink, modes in seatunnel.SINK_MODES.items():
            sink_modes.setdefault(sink, set()).update(modes)

    return Capabilities(
        backends=backends,
        sources=sorted(sources),
        sinks=sorted(sink_modes),
        modes=sorted({m for modes in sink_modes.values() for m in modes}),
        drivers=sorted(drivers),
        sink_modes={s: sorted(m) for s, m in sorted(sink_modes.items())},
    )
