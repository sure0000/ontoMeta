"""seatunnel 档：把作业提交给常驻的 SeaTunnel Zeta 集群（REST v2）。

**为什么需要这一档**：native 档逐行 JDBC 写入，对 Hive 不成立（要写 HDFS 上的 ORC、
要过 metastore），于是目标是 Hive 时所有表都会落进 ``unsupported``、只建表不搬数。
Hive 恰恰是本仓的 ``DEFAULT_ENGINE``。

**为什么是 Zeta REST 而不是起容器**：runner 通道存在的全部意义就是消掉「一次搬运要同时
成立九件事」（`MATERIALIZE_SYNC_STABILITY.md` §1.1）。经 docker.sock 起兄弟容器会把
docker.sock 可达性、宿主机路径、网络名、驱动挂载原样搬回来。走 REST 则只剩一件事：
Zeta 集群可达——作业配置随请求体走，不落盘、不挂载。

**接口形状为实测**（apache/seatunnel:2.3.11，非照抄文档）：

    POST /submit-job?jobName=<名>   body=作业配置 JSON  → {"jobId": "...", "jobName": "..."}
    GET  /job-info/{jobId}          → {"jobStatus": FINISHED|RUNNING|FAILED|..., "errorMsg", "metrics"}
    GET  /overview                  → {"projectVersion": "2.3.11", ...}

⚠ Zeta 的 REST v1（``/hazelcast/rest/maps/*``，5801 端口）在 2.3.11 默认关闭，别用。

**部署前提**（runner 检查不了，缺了会在 SeaTunnel 侧报错，故写在这里）：

- Zeta 集群要能连到源库与 HDFS/metastore（同一张容器网络）；
- 写 Hive 时集群进程需要 ``HADOOP_USER_NAME``（非 Kerberos 的 HDFS 按它认人）——
  这是**集群启动时**的环境变量，REST 提交改不了它，必须配在 Zeta 的部署上；
- 源库的 JDBC 驱动要在集群的 ``lib/`` 里（官方镜像自带 mysql，MariaDB 等要自己放）；
- ``metastore_uri`` 的主机名**不能带下划线，也不能因 DNS 搜索域被拼出下划线**：容器里
  ``hive-metastore`` 会被解析成 ``hive-metastore.<网络名>``，网络名带下划线（如
  ``bigdata_net``）时 Hive 的 URI 解析直接抛
  ``URISyntaxException: Illegal character in hostname``（本机实测）。用 IP 或不带下划线的名字。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from sqlalchemy.engine import URL

from sync_runner import secrets
from sync_runner.contract import WireJobSpec

# 支持的 Zeta 版本前缀。**未知版本拒绝提交**，不猜——2.3.x 之间连接器参数名就有出入
# （Hive sink 的 table_name 那类），拿一个没验过的版本硬发过去只会在执行侧炸。
SUPPORTED_VERSION_PREFIXES = ("2.3.",)

# 该档能写的目标，以及每个目标支持的装载方式。
# **hive 只做 full**：增量要回读目标表的 max(分区键) 才能定水位（§3.3 明确不信调度器给的
# data_interval），而 runner 没有 Hive 查询能力，回读不了。宁可如实少声明一项，
# 也不拿一个可能漏数/重复的水位去跑——要解锁它，得先给 runner 一条读 Hive 的通路。
SINK_MODES: dict[str, list[str]] = {
    "hive": ["full"],
    "doris": ["full", "incremental"],
    "starrocks": ["full", "incremental"],
    "clickhouse": ["full", "incremental"],
}

# 源平台 → SeaTunnel 的 JDBC 驱动类与 jdbc url scheme。未列出的平台不支持。
_JDBC: dict[str, tuple[str, str]] = {
    "mysql": ("com.mysql.cj.jdbc.Driver", "mysql"),
    "mariadb": ("org.mariadb.jdbc.Driver", "mariadb"),
    "postgres": ("org.postgresql.Driver", "postgresql"),
    "postgresql": ("org.postgresql.Driver", "postgresql"),
    "oracle": ("oracle.jdbc.OracleDriver", "oracle"),
    "mssql": ("com.microsoft.sqlserver.jdbc.SQLServerDriver", "sqlserver"),
}

_TERMINAL = {"FINISHED", "FAILED", "CANCELED", "CANCELLED"}


class SeaTunnelError(RuntimeError):
    """seatunnel 档的可读错误。文本直接进作业回执，故要说清是哪一步、该看哪儿。"""


@dataclass
class SeaTunnelResult:
    rows_read: int
    rows_written: int
    job_id: str


def endpoint() -> str:
    """Zeta 集群的 REST 地址。空 = 本部署没有这一档。"""
    return (os.environ.get("SEATUNNEL_REST_ENDPOINT") or "").rstrip("/")


def _http(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        endpoint() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        # 内网服务，不走进程的代理设置（与 ontoMeta 各 connector 同一处置）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
    except urllib.error.HTTPError as exc:
        raise SeaTunnelError(
            f"Zeta {method} {path} → HTTP {exc.code}：{exc.read().decode()[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SeaTunnelError(f"Zeta 不可达（{endpoint()}）：{exc.reason}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SeaTunnelError(f"Zeta {path} 响应不是 JSON：{raw[:200]}") from exc


def version() -> str | None:
    """集群版本；不可达返回 None（不抛，供 capabilities 静默降级为「本档不可用」）。"""
    if not endpoint():
        return None
    try:
        return _http("GET", "/overview", timeout=5).get("projectVersion")
    except SeaTunnelError:
        return None


def available() -> bool:
    """本部署有没有可用的 seatunnel 档：配了地址、连得上、且版本在支持范围内。"""
    got = version()
    return bool(got) and got.startswith(SUPPORTED_VERSION_PREFIXES)


def supports(spec: WireJobSpec) -> bool:
    src = (spec.source.platform or "").lower()
    dst = (spec.target.platform or "").lower()
    return src in _JDBC and spec.mode in SINK_MODES.get(dst, [])


# ---------- 渲染 ----------


def _jdbc_url(url: URL, platform: str, options: dict[str, str]) -> str:
    """SQLAlchemy URL + 平台 → JDBC url。部署方给了 jdbc_url 就以它为准。

    推导只用 host/port/database——**driver 部分不能照搬**（``mysql+pymysql`` 是 Python 侧
    的事，JDBC 不认），故 scheme 按平台查表。
    """
    override = options.get("jdbc_url")
    if override:
        return override
    scheme = _JDBC[platform][1]
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    database = f"/{url.database}" if url.database else ""
    return f"jdbc:{scheme}://{host}{port}{database}"


def _query(spec: WireJobSpec, watermark: str | None) -> str:
    cols = ",\n  ".join(f"`{c.source}` AS `{c.target}`" for c in spec.columns) or "*"
    sql = f"SELECT\n  {cols}\nFROM {spec.source.qualified}"
    if spec.mode == "incremental" and spec.partition_key and watermark:
        sql += f"\nWHERE `{spec.partition_key}` > '{watermark}'"
    return sql


def render(spec: WireJobSpec, watermark: str | None = None) -> dict:
    """WireJobSpec → SeaTunnel 作业配置。

    与 ontoMeta 侧 ``app/warehouse/jobs/seatunnel.py`` 刻意**不共用**：那边产的是给
    docker 通道用的、带 ``${占位符}`` 的配置（凭据由 Airflow 在运行期注入）；这里凭据
    由 runner 自己按别名解析后直接填。渲染细节随 Zeta 版本走，而版本是执行侧的事实，
    故这份渲染归 runner。
    """
    src_platform = (spec.source.platform or "").lower()
    dst_platform = (spec.target.platform or "").lower()
    if src_platform not in _JDBC:
        raise SeaTunnelError(f"seatunnel 档无 {src_platform} 源连接器")
    if spec.mode not in SINK_MODES.get(dst_platform, []):
        raise SeaTunnelError(
            f"seatunnel 档不支持 {dst_platform} + {spec.mode}"
            f"（支持：{SINK_MODES.get(dst_platform) or '无'}）"
        )

    src_url = secrets.resolve(spec.source.alias)
    src_options = secrets.resolve_options(spec.source.alias)
    tgt_options = secrets.resolve_options(spec.target.alias)

    source = {
        "plugin_name": "Jdbc",
        "url": _jdbc_url(src_url, src_platform, src_options),
        "driver": _JDBC[src_platform][0],
        "user": src_url.username or src_options.get("user", ""),
        "password": src_url.password or src_options.get("password", ""),
        "query": _query(spec, watermark),
        "plugin_output": spec.name,
    }

    if dst_platform == "hive":
        metastore = tgt_options.get("metastore_uri")
        if not metastore:
            raise SeaTunnelError(
                f"目标别名「{spec.target.alias}」缺 metastore_uri："
                f"设 SYNC_CONN_{spec.target.alias.upper()}_METASTORE_URI="
                "thrift://<host>:9083，或写进该别名的 secrets json。"
            )
        sink = {
            "plugin_name": "Hive",
            # Hive sink 只认一个 table_name（库.表），拆成 database/table 会报
            # "the options('table_name') are required"（2.3.11 实测）。
            "table_name": spec.target.qualified,
            "metastore_uri": metastore,
            "plugin_input": spec.name,
        }
    else:
        tgt_url = secrets.resolve(spec.target.alias)
        sink = {
            "plugin_name": dst_platform.capitalize(),
            "database": spec.target.database,
            "table": spec.target.table,
            "url": _jdbc_url(tgt_url, dst_platform, tgt_options)
            if dst_platform in _JDBC
            else tgt_options.get("jdbc_url", ""),
            "username": tgt_url.username or "",
            "password": tgt_url.password or "",
            "plugin_input": spec.name,
        }

    return {
        "env": {"job.name": spec.name, "job.mode": "BATCH", "parallelism": 1},
        "source": [source],
        "transform": [],
        "sink": [sink],
    }


# ---------- 执行 ----------


def _rows(metrics: dict, key: str) -> int:
    """从 Zeta 的 metrics 里取行数。它同时给标量与按表的 map，两种形状都认。"""
    value = (metrics or {}).get(key)
    if isinstance(value, dict):
        return sum(int(v) for v in value.values() if str(v).isdigit())
    return int(value) if str(value).isdigit() else 0


def run(
    spec: WireJobSpec,
    *,
    watermark: str | None = None,
    poll_seconds: float = 3.0,
    timeout: float = 6 * 60 * 60,
) -> SeaTunnelResult:
    """提交作业并轮询到终态。失败抛 :class:`SeaTunnelError`，文本进作业回执。"""
    if not endpoint():
        raise SeaTunnelError(
            "未配置 SEATUNNEL_REST_ENDPOINT，seatunnel 档不可用"
        )
    got = version()
    if not got or not got.startswith(SUPPORTED_VERSION_PREFIXES):
        raise SeaTunnelError(
            f"Zeta 版本 {got or '未知'} 不在支持范围 {SUPPORTED_VERSION_PREFIXES}；"
            "连接器参数名在版本间有出入，不拿没验过的版本硬跑。"
        )

    config = render(spec, watermark)
    driver = _JDBC[(spec.source.platform or "").lower()][0]
    try:
        submitted = _http("POST", f"/submit-job?jobName={spec.name}", config, timeout=60)
    except SeaTunnelError as exc:
        # Zeta 把「驱动类加载不到」一路包成 "Unable to create a source for identifier 'Jdbc'"，
        # 字面上像是连接器缺失，真实原因在栈最深处的 SimpleJdbcConnectionProvider.loadDriver
        # （本机实测）。这条报错自己读不出下一步，故在这里翻译成能照做的。
        if "Unable to create a source" in str(exc):
            raise SeaTunnelError(
                f"{exc}\n提示：这条报错通常不是连接器缺失，而是 Zeta 集群的 lib/ 里没有"
                f"源库的 JDBC 驱动（本作业需要 {driver}）。把驱动 jar 放进集群的 "
                "lib/ 目录后重启集群——驱动因授权原因不随镜像分发。"
            ) from exc
        raise

    job_id = str(submitted.get("jobId") or "")
    if not job_id:
        raise SeaTunnelError(f"提交没拿到 jobId：{submitted}")

    deadline = time.monotonic() + timeout
    info: dict = {}
    while True:
        info = _http("GET", f"/job-info/{job_id}", timeout=30)
        status = str(info.get("jobStatus") or "").upper()
        if status in _TERMINAL:
            break
        if time.monotonic() >= deadline:
            raise SeaTunnelError(f"作业 {job_id} 超时未结束（当前 {status}）")
        time.sleep(poll_seconds)

    if status != "FINISHED":
        raise SeaTunnelError(
            f"Zeta 作业 {status}：{info.get('errorMsg') or '无错误信息'}（job {job_id}）"
        )
    metrics = info.get("metrics") or {}
    return SeaTunnelResult(
        rows_read=_rows(metrics, "SourceReceivedCount"),
        rows_written=_rows(metrics, "SinkWriteCount"),
        job_id=job_id,
    )
