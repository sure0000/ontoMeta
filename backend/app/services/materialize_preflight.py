"""物化提交前自检（Preflight Gate，M13）。

**为什么存在**：现在的物化把一类失败推迟到「点提交之后 1–3 分钟、Airflow 任务日志最深处」，
报错文本又都不指向真实原因（见 `MATERIALIZE_SYNC_STABILITY.md` §1）。这些失败里有一部分
在**提交前就能问出来**——Airflow 到底连不连得上、鉴权是不是真能用、建表要用的 Connection
在不在、ontoMeta 写 DAG 的目录 Airflow 到底看不看得见。本模块把这几项逐条查了，每项失败都
给**可照做的下一步**，而不是等真起了容器才炸。

**只查、不改**：preflight 不落任何产物、不触发任何运行，可以随便重跑。它覆盖的是九件必须
同时成立的事里、提交前能验证的那几件（#1 镜像已由既有逻辑在提交前拦、本模块补 #3/#6/#9）；
#2/#4/#5 只有真起搬运容器才知道，M13 不假装能查（见 §6 退路的明确代价）。

**阻断项 vs 提醒项**：``blocking=True`` 的失败应禁掉「提交」按钮；``blocking=False`` 的只提醒，
可显式忽略——否则 preflight 会退化成「一律忽略」的走过场（§7）。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError
from app.models.data_app import DataSource
from app.services.job_planner import (
    DEFAULT_SOURCE_ALIAS,
    DEFAULT_TARGET_ALIAS,
    JobPlan,
    JobPlanner,
)
from app.services.materialization_contract import MaterializationContractService
from app.services.materialization_runner import _warehouse_conn_id
from app.services.settings_service import SettingsService
from app.services.sync_tool_resolver import (
    SyncToolResolutionError,
    required_modes,
    resolve_sync_tool,
)

_settings = SettingsService()
_contract_service = MaterializationContractService()
_job_planner = JobPlanner()

# 状态取值。warn 不阻断提交，fail 视 blocking 决定是否阻断。
PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class PreflightItem:
    """单项检查结果。``next_step`` 是失败时该照做的下一步，不是又一句报错。"""

    key: str
    label: str
    status: str  # PASS / WARN / FAIL
    blocking: bool
    detail: str
    next_step: str | None = None


@dataclass
class PreflightReport:
    items: list[PreflightItem] = field(default_factory=list)

    def add(self, item: PreflightItem) -> None:
        self.items.append(item)

    @property
    def blocking_failures(self) -> list[PreflightItem]:
        return [i for i in self.items if i.status == FAIL and i.blocking]

    @property
    def ok(self) -> bool:
        """无阻断失败即可提交（提醒项与非阻断失败不拦）。"""
        return not self.blocking_failures


def run_preflight(
    db: Session,
    ontology_id: str,
    *,
    target_datasource_id: str,
    engine: str,
    selected_targets: list[str] | None = None,
) -> PreflightReport:
    """跑一遍提交前自检，返回逐项结构化结果。**不落产物、不触发运行。**"""
    report = PreflightReport()

    ds = db.get(DataSource, target_datasource_id)
    airflow = _settings.get_airflow_runtime(db)

    # 1) Airflow 是否配置可用 + /health 连通。不通则后续 Airflow 相关项全无意义，
    #    直接短路：把依赖它的检查标成 fail 而不是逐个再抛一遍连接错误。
    reachable = _check_airflow_reachable(report, airflow)
    if not reachable:
        report.add(
            PreflightItem(
                key="airflow_api_auth",
                label="Airflow API 鉴权",
                status=FAIL,
                blocking=True,
                detail="Airflow 不可达，无法验证 API 鉴权。",
                next_step="先解决上一项 Airflow 连通性。",
            )
        )
        _add_conn_and_batch(
            report, db, ontology_id, ds, selected_targets, airflow, engine
        )
        return report

    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    try:
        _check_api_auth(report, client)
        _check_api_version(report, client, airflow.api_version)
        _check_warehouse_conn(report, client, ds)
        _check_dag_dir_visible(report, client, airflow)
    finally:
        client.close()

    _check_execution_channel(
        report, db, ontology_id, airflow, ds, engine, selected_targets
    )
    _check_batch_size(report, db, ontology_id, selected_targets, airflow.max_tasks_per_dag)
    return report


def _check_airflow_reachable(report: PreflightReport, airflow) -> bool:
    if not airflow.available:
        report.add(
            PreflightItem(
                key="airflow_reachable",
                label="Airflow 可达",
                status=FAIL,
                blocking=True,
                detail="未配置可用的 Airflow（需在设置页填 endpoint 并启用）。",
                next_step="到 系统设置 → Airflow 填写 endpoint 并启用后重试。",
            )
        )
        return False
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    try:
        client.health()
    except AirflowError as exc:
        report.add(
            PreflightItem(
                key="airflow_reachable",
                label="Airflow 可达",
                status=FAIL,
                blocking=True,
                detail=str(exc),
                next_step=(
                    f"确认 endpoint（{airflow.endpoint}）是否写对、是否被反向代理挡了"
                    "登录页；/health 在 2.x 默认匿名可读，若这里回登录页多半是代理配置。"
                ),
            )
        )
        return False
    finally:
        client.close()
    report.add(
        PreflightItem(
            key="airflow_reachable",
            label="Airflow 可达",
            status=PASS,
            blocking=True,
            detail=f"{airflow.endpoint} /health 正常。",
        )
    )
    return True


def _check_api_auth(report: PreflightReport, client: AirflowClient) -> None:
    """探带版本前缀的 REST API，确认鉴权真的能用（/health 匿名可读会给假绿灯）。"""
    try:
        client.ping_api()
    except AirflowError as exc:
        report.add(
            PreflightItem(
                key="airflow_api_auth",
                label="Airflow API 鉴权",
                status=FAIL,
                blocking=True,
                detail=str(exc),
                next_step=(
                    "最常见是没开 basic_auth 后端（2.x 默认只有 session，仅供 Web UI）。"
                    "在 Airflow 侧加 AIRFLOW__API__AUTH_BACKENDS="
                    "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session；"
                    "若用 token 则确认它未过期。"
                ),
            )
        )
        return
    report.add(
        PreflightItem(
            key="airflow_api_auth",
            label="Airflow API 鉴权",
            status=PASS,
            blocking=True,
            detail="GET /dags 鉴权通过，下发 DagRun 不会 401。",
        )
    )


def _check_api_version(
    report: PreflightReport, client: AirflowClient, configured: str
) -> None:
    """自探 REST 版本并与设置对照。不匹配不硬失败——给出应改成哪个版本。"""
    detected = client.detect_api_version()
    if detected is None:
        report.add(
            PreflightItem(
                key="airflow_api_version",
                label="REST 版本",
                status=WARN,
                blocking=False,
                detail="探不到 openapi.json，无法自动确认 REST 版本。",
                next_step=(
                    f"当前按 {configured} 下发；若下发 DagRun 报 404，"
                    "手动核对该实例是 /api/v1（2.x）还是 /api/v2（3.x）。"
                ),
            )
        )
        return
    if detected != configured:
        report.add(
            PreflightItem(
                key="airflow_api_version",
                label="REST 版本",
                status=WARN,
                blocking=False,
                detail=f"设置里是 {configured}，实测该实例暴露的是 {detected}。",
                next_step=f"把 Airflow 设置的 api_version 改成 {detected}，否则请求会 404。",
            )
        )
        return
    report.add(
        PreflightItem(
            key="airflow_api_version",
            label="REST 版本",
            status=PASS,
            blocking=False,
            detail=f"设置与实测一致（{detected}）。",
        )
    )


def _check_warehouse_conn(
    report: PreflightReport, client: AirflowClient, ds: DataSource | None
) -> None:
    """建表任务按 conn_id 连目标仓。Connection 不存在 = 渲染期整个 DAG 一起红（失败模式 #6）。"""
    if ds is None:
        report.add(
            PreflightItem(
                key="warehouse_conn",
                label="建表连接",
                status=FAIL,
                blocking=True,
                detail="目标数据源不存在，无法推导建表 Connection。",
                next_step="重新选择一个有效的目标数据源。",
            )
        )
        return
    conn_id = _warehouse_conn_id(ds)
    try:
        client.get_connection(conn_id)
    except AirflowError as exc:
        text = str(exc)
        if "404" in text:
            report.add(
                PreflightItem(
                    key="warehouse_conn",
                    label="建表连接",
                    status=FAIL,
                    blocking=True,
                    detail=f"Airflow 里没有建表要用的 Connection「{conn_id}」。",
                    next_step=(
                        f"在 Airflow 建一个 conn_id={conn_id} 的 Connection，指向目标仓"
                        f"「{ds.name}」。缺它会导致提交后所有任务在渲染期一起失败。"
                    ),
                )
            )
        elif "403" in text:
            # ⚠ §8.2：只读账号可能对 /connections 无权。降级为「无法确认」而非判死。
            report.add(
                PreflightItem(
                    key="warehouse_conn",
                    label="建表连接",
                    status=WARN,
                    blocking=False,
                    detail=f"无权读取 Connection（403），无法确认「{conn_id}」是否存在。",
                    next_step=(
                        f"用有 Connections 读权限的账号可确认；或直接在 Airflow 侧核对"
                        f" conn_id={conn_id} 是否已配好。"
                    ),
                )
            )
        else:
            report.add(
                PreflightItem(
                    key="warehouse_conn",
                    label="建表连接",
                    status=WARN,
                    blocking=False,
                    detail=text,
                    next_step=f"手动核对 Airflow 里的 conn_id={conn_id}。",
                )
            )
        return
    report.add(
        PreflightItem(
            key="warehouse_conn",
            label="建表连接",
            status=PASS,
            blocking=True,
            detail=f"Connection「{conn_id}」存在，建表任务可用。",
        )
    )


def probe_ssh_pipeline(airflow) -> tuple[bool, str]:
    """探 SSH 投递管道：能连上 Airflow 主机、且 DAG 目录可写吗？

    模块级函数（而非内联 subprocess）是为了可注入——测试 monkeypatch 它就能造出
    「通 / 连不上 / 目录不可写」三种结果。此前的 git 管道检查因为直接 subprocess.run
    而无法被测，至今零覆盖。

    Returns:
        ``(ok, detail)``——detail 在失败时是给用户看的原因。
    """
    from app.services.dag_delivery import DagDeliveryError, get_delivery

    try:
        delivery = get_delivery(
            airflow.ssh_host,
            user=airflow.ssh_user or None,
            port=airflow.ssh_port,
            key_path=airflow.ssh_key_path or None,
            password=airflow.ssh_password or None,
        )
        dags_dir = airflow.dags_dir
        # mkdir -p 再测可写：目录不存在是常态（首次投递会建），不该报成失败。
        delivery._ssh(f"mkdir -p '{dags_dir}' && test -w '{dags_dir}'")
    except DagDeliveryError as exc:
        return False, str(exc)
    return True, f"{delivery.target}:{airflow.dags_dir} 可写，投递管道连通。"


def _check_dag_dir_visible(
    report: PreflightReport, client: AirflowClient, airflow
) -> None:
    """专治失败模式 #3：产物没能真正到达 Airflow 读的那个目录。

    SSH 投递下这一项验的是**管道本身**：连得上主机吗、DAG 目录可写吗。这两步过了，
    产物就能落到 Airflow 主机的 dags 目录里。

    不再写 sentinel DAG 探测：那套做法假设 ontoMeta 与 Airflow 共享文件系统——往本地
    dags 目录写个文件再看 Airflow 认不认。SSH 拓扑下本地根本没有那个目录，写了也只是
    在 ontoMeta 自己机器上留垃圾，必然假 WARN。
    """
    if not getattr(airflow, "ssh_host", ""):
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 投递管道（SSH）",
                status=FAIL,
                blocking=True,
                detail="未配置 SSH 主机，产物无法投递到 Airflow。",
                next_step="在设置页 → Airflow → DAG 投递填写 SSH 主机（本机验证可填 localhost）。",
            )
        )
        return

    ok, detail = probe_ssh_pipeline(airflow)
    if ok:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 投递管道（SSH）",
                status=PASS,
                blocking=True,
                detail=detail,
            )
        )
        return
    report.add(
        PreflightItem(
            key="dag_dir_visible",
            label="DAG 投递管道（SSH）",
            status=FAIL,
            blocking=True,
            detail=detail,
            next_step=(
                "确认：①SSH 主机/端口/用户名正确且主机可达；"
                "②私钥路径在 **ontoMeta 侧**可读（或已填 SSH 密码且目标机装了 sshpass）；"
                "③该用户对 DAG 目录有写权限。以上都在设置页 → Airflow → DAG 投递。"
            ),
        )
    )


def _check_execution_channel(
    report: PreflightReport,
    db: Session,
    ontology_id: str,
    airflow,
    ds,
    engine: str,
    selected_targets: list[str] | None,
) -> None:
    """统一执行架构：搬运一律走 Flink SQL on YARN。前置条件只两件：JAR 与 checkpoint_dir。

    - **flink_sql_runner_jar**：缺它**同步**退回「仅产出」（不执行），preflight 标 WARN
      提醒。物化不受影响——建表是 ``SQLExecuteQueryOperator`` 直连目标仓，与 Flink 无关。
    - **flink_checkpoint_dir**：增量/CDC 是流式作业需要它持久化读位点；含 timestamp 分区键
      的表默认 incremental，缺 checkpoint 会在编译期硬失败。这里先验可搬性，若有增量表
      但无 checkpoint，标 FAIL。全量搬运不需要 checkpoint。

    不再有 sync_channel/runner/docker 多通道——那套已废除，preflight 不再探 runner probe。
    """
    runner_jar = (airflow.flink_sql_runner_jar or "").strip()
    if not runner_jar:
        report.add(
            PreflightItem(
                key="flink_jar",
                label="Flink SqlRunner JAR",
                status=WARN,
                blocking=False,
                detail=(
                    "未配置 Flink SqlRunner JAR，数据同步不执行、只产出 SQL（handoff 模式）。"
                    "物化（建表）不经 Flink，不受影响。"
                ),
                next_step="在 设置 → Airflow/Flink 填写「Flink SqlRunner JAR」路径。",
            )
        )

    # 可搬性预演：本次有几张表、各是什么装载方式（含增量/CDC 与否）。
    from app.services.job_planner import JobPlanner

    planner = JobPlanner()
    try:
        plan = planner.build(
            db,
            ontology_id,
            engine=engine,
            tool="flink",
            target_alias=_warehouse_conn_id(ds) if ds is not None else DEFAULT_TARGET_ALIAS,
            selected_targets=selected_targets,
            runner_capabilities=None,
        )
    except Exception:
        # 计划失败（本体不存在/无契约）不在 execution_channel 检查范围，别的项会报。
        return

    incremental_or_cdc = [
        j.name for j in plan.jobs if j.mode in ("incremental", "cdc")
    ]
    checkpoint_dir = (airflow.flink_checkpoint_dir or "").strip()
    if incremental_or_cdc and not checkpoint_dir:
        report.add(
            PreflightItem(
                key="flink_checkpoint",
                label="Flink Checkpoint 目录",
                status=FAIL,
                blocking=True,
                detail=(
                    f"本次有 {len(incremental_or_cdc)} 张增量/CDC 表（如 {incremental_or_cdc[0]}），"
                    "但未配置 Flink Checkpoint 目录。增量/CDC 是流式作业、需 checkpoint 持久化读位点，"
                    "否则重启会重搬。编译期会直接报错，提交无法成功。"
                ),
                next_step=(
                    "在 设置 → Airflow/Flink 填写「Flink Checkpoint 目录」（file://… 本地 或 "
                    "hdfs://… 集群）；或把这批表的契约改为全量（mode=full），全量不需要 checkpoint。"
                ),
            )
        )
    elif incremental_or_cdc and checkpoint_dir:
        report.add(
            PreflightItem(
                key="flink_checkpoint",
                label="Flink Checkpoint 目录",
                status=PASS,
                blocking=False,
                detail=f"checkpoint 目录已配置：{checkpoint_dir[:60]}…（增量/CDC 可用）",
                next_step=None,
            )
        )






def _check_batch_size(
    report: PreflightReport,
    db: Session,
    ontology_id: str,
    selected_targets: list[str] | None,
    limit_per_dag: int,
) -> None:
    """表数 vs 单 DAG 上限。超限会自动按上限拆成多个 DAG（M16），这里只是先说清会拆几个。"""
    count = len(_contract_service.list_selected(db, ontology_id, selected_targets))
    limit = limit_per_dag
    if count > limit:
        report.add(
            PreflightItem(
                key="batch_size",
                label="批次规模",
                status=WARN,
                blocking=False,
                detail=f"本次 {count} 张表，超过单 DAG 上限 {limit}。",
                next_step=(
                    "会按 cron 分组后自动拆成多个 DAG（M16），每批各自触发、可单独重跑，"
                    "无需人工分批；提交耗时会随批数增加。"
                ),
            )
        )
        return
    report.add(
        PreflightItem(
            key="batch_size",
            label="批次规模",
            status=PASS,
            blocking=False,
            detail=f"本次 {count} 张表，未超过单 DAG 上限 {limit}。",
        )
    )


def _add_conn_and_batch(
    report: PreflightReport,
    db: Session,
    ontology_id: str,
    ds: DataSource | None,
    selected_targets: list[str] | None,
    airflow,
    engine: str,
) -> None:
    """Airflow 不可达时的补充项。

    建表连接无从查（标 fail），但**执行通道与批次规模跟 Airflow 无关**，照查不误——
    一次 preflight 应尽量把能问的都问了，而不是因为第一项红了就少报几条。
    """
    conn_id = _warehouse_conn_id(ds) if ds is not None else "?"
    report.add(
        PreflightItem(
            key="warehouse_conn",
            label="建表连接",
            status=FAIL,
            blocking=True,
            detail=f"Airflow 不可达，无法确认 Connection「{conn_id}」是否存在。",
            next_step="先解决 Airflow 连通性。",
        )
    )
    _check_execution_channel(
        report, db, ontology_id, airflow, ds, engine, selected_targets
    )
    _check_batch_size(report, db, ontology_id, selected_targets, airflow.max_tasks_per_dag)
