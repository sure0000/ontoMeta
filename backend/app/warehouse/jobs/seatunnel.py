"""SeaTunnel 作业渲染器（M9 默认实现）。

选它作默认的理由见 `MATERIALIZE_ORCHESTRATION.md` §4：仓库已有先例
（``agents/executors/sync.py`` 就在产 SeaTunnel 配置），且 Bigtop Manager 的 Extra stack
已纳管 SeaTunnel，不新增运维面。

产出结构对齐 SeaTunnel 的 ``env`` / ``source`` / ``transform`` / ``sink`` 四段。
⚠ **需实施前验证**：SeaTunnel 各版本间连接器参数名有出入（目标版本见
``docker/orchestration/.env.example`` 的 ``IMG_SEATUNNEL``），首次真实提交须核对
所用版本的连接器文档。本模块只负责结构正确、无凭据、幂等。
"""

from __future__ import annotations

from app.warehouse.jobs.base import JobSpec, SyncToolAdapter

# 源平台 → SeaTunnel 连接器插件。JDBC 系共用 Jdbc 插件，靠 driver/url 区分。
_JDBC_PLATFORMS = frozenset({"mysql", "mariadb", "postgres", "postgresql", "oracle", "mssql"})
_JDBC_DRIVERS: dict[str, str] = {
    "mysql": "com.mysql.cj.jdbc.Driver",
    "mariadb": "org.mariadb.jdbc.Driver",
    "postgres": "org.postgresql.Driver",
    "postgresql": "org.postgresql.Driver",
    "oracle": "oracle.jdbc.OracleDriver",
    "mssql": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
}
# CDC 场景的专用源插件；未列出的平台不支持 CDC，由 supports() 拦下。
_CDC_PLUGINS: dict[str, str] = {
    "mysql": "MySQL-CDC",
    "mariadb": "MySQL-CDC",  # MariaDB 走 MySQL 协议与 binlog
    "postgres": "Postgres-CDC",
    "postgresql": "Postgres-CDC",
}
# 目标平台 → sink 插件。Doris/StarRocks 有专用 sink（走 Stream Load，远快于 JDBC 逐行）。
_SINK_PLUGINS: dict[str, str] = {
    "hive": "Hive",
    "doris": "Doris",
    "starrocks": "StarRocks",
    "clickhouse": "Clickhouse",
}


class SeaTunnelAdapter(SyncToolAdapter):
    name = "seatunnel"
    # 与 docker/orchestration/.env.example 的 IMG_SEATUNNEL 对齐。
    docker_image = "apache/seatunnel:2.3.11"
    jobs_mount_dir = "/opt/seatunnel/jobs"
    driver_lib_dir = "/opt/seatunnel/lib"

    def supports(self, mode: str) -> bool:
        return mode in {"full", "incremental", "cdc"}

    def airflow_command(
        self, config_path: str, variables: dict[str, str] | None = None
    ) -> list[str]:
        # 水位由调度器注入 ${watermark}（Airflow 的 data_interval_start），补数自动回区间。
        command = [
            "/opt/seatunnel/bin/seatunnel.sh",
            "--config",
            config_path,
            "-e",
            "local",
            "-i",
            "watermark={{ data_interval_start }}",
        ]
        # SeaTunnel 的 ${…} 只认 -i 传进来的变量，**不读环境变量**（实测：只设 env
        # 时报 "No suitable driver found for ${ERP_READONLY_URL}"，占位符原样没替换）。
        # 值仍是 {{ conn.… }} 表达式，运行时才渲染，产物里不含凭据。
        for key in sorted(variables or {}):
            command += ["-i", f"{key}={variables[key]}"]
        return command

    def supports_cdc_from(self, platform: str) -> bool:
        return platform.lower() in _CDC_PLUGINS

    # ---------- 渲染 ----------

    def render(self, job: JobSpec) -> dict:
        return {
            "env": self._env(job),
            "source": [self._source(job)],
            "transform": [],  # 列改名在 source 的 query 里完成，不额外起 transform
            "sink": [self._sink(job)],
        }

    def _env(self, job: JobSpec) -> dict:
        return {
            "job.name": job.name,
            "job.mode": "STREAMING" if job.mode == "cdc" else "BATCH",
            "parallelism": 1,
        }

    def _source(self, job: JobSpec) -> dict:
        platform = job.source.platform.lower()
        if job.mode == "cdc":
            plugin = _CDC_PLUGINS.get(platform)
            if plugin is None:
                # 走到这里说明 planner 没拦住；宁可显式报错也不悄悄退回全量。
                raise ValueError(f"平台 {platform} 无 CDC 连接器，不能生成 CDC 作业")
            return {
                "plugin_name": plugin,
                "base-url": self.placeholder(job.source.alias, "URL"),
                "username": self.placeholder(job.source.alias, "USER"),
                "password": self.placeholder(job.source.alias, "PASSWORD"),
                "table-names": [job.source.qualified],
            }
        if platform not in _JDBC_PLATFORMS:
            raise ValueError(f"平台 {platform} 暂无 JDBC 连接器映射")
        source: dict = {
            "plugin_name": "Jdbc",
            "url": self.placeholder(job.source.alias, "URL"),
            "driver": _JDBC_DRIVERS[platform],
            "user": self.placeholder(job.source.alias, "USER"),
            "password": self.placeholder(job.source.alias, "PASSWORD"),
            "query": self._query(job),
        }
        if job.mode == "incremental" and job.partition_key:
            # 分区列同时作为分片列；水位由调度器注入 ${watermark}（Airflow 的
            # data_interval_start），与 M3 生成 ETL 的口径一致。
            source["partition_column"] = job.partition_key
        return source

    def _query(self, job: JobSpec) -> str:
        """SELECT 源列 AS 目标列。列映射与 M3 的 ETL 同源，不另算一套。"""
        cols = ",\n  ".join(f"`{c.source}` AS `{c.target}`" for c in job.columns) or "*"
        sql = f"SELECT\n  {cols}\nFROM {job.source.qualified}"
        if job.mode == "incremental" and job.partition_key:
            sql += f"\nWHERE `{job.partition_key}` >= '${{watermark}}'"
        return sql

    def _sink(self, job: JobSpec) -> dict:
        platform = job.target.platform.lower()
        plugin = _SINK_PLUGINS.get(platform)
        if plugin is None:
            raise ValueError(f"目标引擎 {platform} 无 sink 连接器映射")
        sink: dict = {
            "plugin_name": plugin,
            # 表由 M3 的 DDL 建（本体反补的注释/分区/主键声明只在那条路径上），
            # 绝不让 sink 自动建表把这些绕过去。
            "save_mode_create_template": "NONE",
            "data_save_mode": "APPEND_DATA" if job.mode != "full" else "DROP_DATA",
        }
        if plugin == "Hive":
            # Hive sink 的表名是**一个** table_name（库.表），没有 database/table 两项——
            # 拆开写会报 "the options('table_name') are required"（2.3.11 实测）。
            sink["table_name"] = job.target.qualified
            sink["metastore_uri"] = self.placeholder(job.target.alias, "METASTORE_URI")
        else:
            sink["database"] = job.target.database
            sink["table"] = job.target.table
            sink["url"] = self.placeholder(job.target.alias, "URL")
            sink["username"] = self.placeholder(job.target.alias, "USER")
            sink["password"] = self.placeholder(job.target.alias, "PASSWORD")
        return sink
