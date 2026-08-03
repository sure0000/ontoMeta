"""DataX 作业渲染器（工具可插拔的第二个实现）。

DataX 是阿里开源的离线批量同步框架，作业配置是单个 JSON：``job.content[].reader`` /
``writer`` 两段 + ``job.setting``。它**只做批量**（全量 / 按水位增量），没有 CDC——
故 ``supports("cdc")`` 显式为假，由 planner 拦下，不静默降级。

⚠ **需实施前验证**：DataX 无官方 Docker 镜像，``docker_image`` 是占位默认，实际
部署需自备（把 DataX 发行包打进镜像）。各 reader/writer 的参数名随插件版本有出入，
首次真实提交须核对所用版本文档。本模块只负责结构正确、无凭据、幂等。
"""

from __future__ import annotations

from app.warehouse.jobs.base import JobSpec, SyncToolAdapter

# 源平台 → DataX reader 插件。JDBC 系共用各自的 *reader。
_READERS: dict[str, str] = {
    "mysql": "mysqlreader",
    "mariadb": "mysqlreader",  # MariaDB 走 MySQL 协议
    "postgres": "postgresqlreader",
    "postgresql": "postgresqlreader",
    "oracle": "oraclereader",
    "mssql": "sqlserverreader",
}
# 目标平台 → DataX writer 插件。
_WRITERS: dict[str, str] = {
    "hive": "hdfswriter",
    "doris": "doriswriter",
    "starrocks": "starrockswriter",
    "clickhouse": "clickhousewriter",
    "mysql": "mysqlwriter",
    "postgres": "postgresqlwriter",
    "postgresql": "postgresqlwriter",
}


class DataXAdapter(SyncToolAdapter):
    name = "datax"
    # DataX 无官方镜像；部署方需自备（见模块 docstring）。这个名字**在任何 registry
    # 上都不存在**，故 has_official_image=False：未经 SYNC_TOOL_IMAGES 指到
    # 自建镜像时，提交会被显式拦下，而不是生成一个注定 pull 404 的 DAG。
    docker_image = "ontometa/datax:latest"
    has_official_image = False
    jobs_mount_dir = "/opt/datax/jobs"
    driver_lib_dir = "/opt/datax/plugin/reader/mysqlreader/libs"

    def supports(self, mode: str) -> bool:
        # 只做批量：全量 / 按水位增量。CDC 不支持。
        return mode in {"full", "incremental"}

    def airflow_command(
        self, config_path: str, variables: dict[str, str] | None = None
    ) -> list[str]:
        # 水位经 -p 传入 JVM 参数，供 reader 的 where 里 ${watermark} 取值。
        return [
            "python",
            "/opt/datax/bin/datax.py",
            config_path,
            "-p",
            "-Dwatermark={{ data_interval_start }}",
        ]

    # ---------- 渲染 ----------

    def render(self, job: JobSpec) -> dict:
        return {
            "job": {
                "setting": {"speed": {"channel": 1}},
                "content": [
                    {
                        "reader": self._reader(job),
                        "writer": self._writer(job),
                    }
                ],
            }
        }

    def _reader(self, job: JobSpec) -> dict:
        platform = job.source.platform.lower()
        plugin = _READERS.get(platform)
        if plugin is None:
            raise ValueError(f"平台 {platform} 暂无 DataX reader 映射")
        connection: dict = {
            "jdbcUrl": [self.placeholder(job.source.alias, "URL")],
        }
        param: dict = {
            "username": self.placeholder(job.source.alias, "USER"),
            "password": self.placeholder(job.source.alias, "PASSWORD"),
            # 列改名不在 reader 做（DataX 靠列序对齐 reader/writer）；目标列名见 writer。
            "column": [c.source for c in job.columns] or ["*"],
            "connection": [connection],
        }
        if job.mode == "incremental" and job.partition_key:
            # 水位由 -Dwatermark 注入，reader 用 where 裁增量区间。
            connection["table"] = [job.source.qualified]
            param["where"] = f"`{job.partition_key}` >= '${{watermark}}'"
        else:
            connection["table"] = [job.source.qualified]
        return {"name": plugin, "parameter": param}

    def _writer(self, job: JobSpec) -> dict:
        platform = job.target.platform.lower()
        plugin = _WRITERS.get(platform)
        if plugin is None:
            raise ValueError(f"目标引擎 {platform} 无 DataX writer 映射")
        param: dict = {
            "username": self.placeholder(job.target.alias, "USER"),
            "password": self.placeholder(job.target.alias, "PASSWORD"),
            # 目标列名 = 本体属性名，与 reader 列一一对应（靠列序对齐）。
            "column": [c.target for c in job.columns] or ["*"],
            # 表由 M3 的 DDL 建，writer 不建表（preSql 只清全量场景的数据）。
            "connection": [
                {
                    "jdbcUrl": self.placeholder(job.target.alias, "URL"),
                    "table": [job.target.qualified],
                }
            ],
        }
        if job.mode == "full":
            param["preSql"] = [f"TRUNCATE TABLE {job.target.qualified}"]
        return {"name": plugin, "parameter": param}
