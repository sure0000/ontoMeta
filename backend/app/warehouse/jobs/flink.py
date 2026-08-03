"""Flink 作业渲染器（工具可插拔的第三个实现）。

Flink 既能做批量（有界流），又是 CDC 的主力（Flink CDC 连接器覆盖 MySQL/Postgres 等），
故三种装载方式都支持，CDC 源平台由 ``_CDC_PLATFORMS`` 声明。

产出为一份**结构化 pipeline 配置**（source/transform/sink 三段），对齐 Flink CDC 3.x
的 pipeline 语义。⚠ **需实施前验证**：Flink CDC 3.x 的 pipeline 原生用 YAML，本模块
先产等价的 JSON（与 DagBundle 的落盘方式统一）；接入真实 Flink（M12）时按所用版本
转成 YAML 或 Flink SQL。本模块只负责结构正确、无凭据、幂等。
"""

from __future__ import annotations

from app.warehouse.jobs.base import JobSpec, SyncToolAdapter

# Flink CDC 有连接器的源平台。未列出的平台不支持 CDC，由 supports_cdc_from 拦下。
_CDC_PLATFORMS: dict[str, str] = {
    "mysql": "mysql-cdc",
    "mariadb": "mysql-cdc",  # MariaDB 走 MySQL 协议与 binlog
    "postgres": "postgres-cdc",
    "postgresql": "postgres-cdc",
}
# 批量读取的 JDBC 连接器（全量 / 增量按水位）。
_JDBC_PLATFORMS = frozenset(_CDC_PLATFORMS) | {"oracle", "mssql"}
# 目标平台 → Flink sink 连接器。
_SINKS: dict[str, str] = {
    "hive": "hive",
    "doris": "doris",
    "starrocks": "starrocks",
    "clickhouse": "clickhouse",
}


class FlinkAdapter(SyncToolAdapter):
    name = "flink"
    docker_image = "apache/flink:1.18"
    jobs_mount_dir = "/opt/flink/jobs"
    driver_lib_dir = "/opt/flink/lib"

    def supports(self, mode: str) -> bool:
        return mode in {"full", "incremental", "cdc"}

    def supports_cdc_from(self, platform: str) -> bool:
        return platform.lower() in _CDC_PLATFORMS

    def airflow_command(
        self, config_path: str, variables: dict[str, str] | None = None
    ) -> list[str]:
        # 水位经 -D 传入，供批量 source 的 scan 条件里 ${watermark} 取值。
        return [
            "/opt/flink/bin/flink",
            "run",
            "-p",
            "1",
            config_path,
            "-Dwatermark={{ data_interval_start }}",
        ]

    # ---------- 渲染 ----------

    def render(self, job: JobSpec) -> dict:
        return {
            "pipeline": {"name": job.name, "parallelism": 1},
            "source": self._source(job),
            "transform": self._transform(job),
            "sink": self._sink(job),
        }

    def _source(self, job: JobSpec) -> dict:
        platform = job.source.platform.lower()
        if job.mode == "cdc":
            connector = _CDC_PLATFORMS.get(platform)
            if connector is None:
                # planner 应已拦下；宁可显式报错也不悄悄退回全量。
                raise ValueError(f"平台 {platform} 无 Flink CDC 连接器，不能生成 CDC 作业")
        else:
            if platform not in _JDBC_PLATFORMS:
                raise ValueError(f"平台 {platform} 暂无 Flink JDBC 连接器映射")
            connector = "jdbc"
        source: dict = {
            "connector": connector,
            "url": self.placeholder(job.source.alias, "URL"),
            "username": self.placeholder(job.source.alias, "USER"),
            "password": self.placeholder(job.source.alias, "PASSWORD"),
            "table": job.source.qualified,
        }
        if job.mode == "incremental" and job.partition_key:
            # 水位由调度器注入 ${watermark}，批量 source 用它裁增量区间。
            source["scan.filter"] = f"`{job.partition_key}` >= '${{watermark}}'"
        return source

    def _transform(self, job: JobSpec) -> list[dict]:
        # 列改名在 transform 段声明：源列 → 目标列（目标列名 = 本体属性名）。
        return [{"source": c.source, "target": c.target} for c in job.columns]

    def _sink(self, job: JobSpec) -> dict:
        platform = job.target.platform.lower()
        connector = _SINKS.get(platform)
        if connector is None:
            raise ValueError(f"目标引擎 {platform} 无 Flink sink 连接器映射")
        return {
            "connector": connector,
            "url": self.placeholder(job.target.alias, "URL"),
            "username": self.placeholder(job.target.alias, "USER"),
            "password": self.placeholder(job.target.alias, "PASSWORD"),
            "database": job.target.database,
            "table": job.target.table,
            # 表由 M3 的 DDL 建，sink 不自动建表（本体反补的注释/分区/主键只在那条路径上）。
            "sink.auto-create": False,
        }
