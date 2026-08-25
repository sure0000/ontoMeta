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

import posixpath

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError
from app.models.data_app import DataSource
from app.services.job_planner import (
    DEFAULT_SOURCE_ALIAS,
    DEFAULT_TARGET_ALIAS,
    JobPlan,
    JobPlanner,
)
from app.services import flink_params
from app.services.materialization_contract import MaterializationContractService
from app.services.materialization_runner import (
    Emit,
    _source_conn_id,
    _warehouse_conn_id,
    build_embedded_airflow_connections,
)
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
    source_datasource_id: str | None = None,
    source_database: str | None = None,
    managed_connections: bool = False,
    emit: Emit = "ddl",
    load_strategy: str | None = None,
    incremental_column: str | None = None,
    initial_watermark: str | None = None,
    flink_task_params: dict[str, Any] | None = None,
) -> PreflightReport:
    """跑一遍提交前自检，返回逐项结构化结果。**不落产物、不触发运行。**

    自检要有意义，预演的就必须是**本次真会提交的那个作业**，所以搬运侧的几个参数要跟着
    任务走，而不是回头去读契约（契约是「这张表平时怎么搬」，Spec 是「这一次怎么搬」）：

    Args:
        emit: ``"ddl"`` = 物化（只建表，不产 Flink 作业）；``"dml"`` = 同步（搬运）。
            决定是否做 Flink 前置条件那一组检查，与 ``materialization_runner`` 同义。
        load_strategy: 本次同步的装载方式（Spec 的 ``mode``），压过契约。
        incremental_column / initial_watermark: 本次同步的增量字段与水位。
        flink_task_params: 本任务的 Flink 覆盖（含 checkpoint 目录），优先于设置页默认。
    """
    report = PreflightReport()

    ds = db.get(DataSource, target_datasource_id)
    source_ds = db.get(DataSource, source_datasource_id) if source_datasource_id else None
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
            report,
            db,
            ontology_id,
            ds,
            selected_targets,
            airflow,
            engine,
            emit=emit,
            load_strategy=load_strategy,
            incremental_column=incremental_column,
            initial_watermark=initial_watermark,
            flink_task_params=flink_task_params,
        )
        return report

    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
    )
    try:
        _check_api_auth(report, client)
        _check_api_version(report, client)
        if managed_connections:
            _check_managed_connections(
                report,
                db,
                ds,
                source_ds=source_ds,
                source_database=source_database,
            )
        else:
            _check_warehouse_conn(report, client, ds)
            _check_doris_flink_conn(report, db, client, ds)
            if source_datasource_id:
                _check_source_conn(report, client, source_ds, source_datasource_id)
        _check_dag_dir_visible(report, client, airflow)
    finally:
        client.close()

    _check_execution_channel(
        report,
        db,
        ontology_id,
        airflow,
        ds,
        engine,
        selected_targets,
        emit=emit,
        load_strategy=load_strategy,
        incremental_column=incremental_column,
        initial_watermark=initial_watermark,
        flink_task_params=flink_task_params,
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


def _check_api_version(report: PreflightReport, client: AirflowClient) -> None:
    """自探实例暴露的 REST 版本，如实报出来。

    这里**不再和任何设置项对照**：版本已不是配置项，客户端 404 时会自协商（见
    ``connectors/airflow.py``）。留着这一项是因为它仍有诊断价值——下发报 404 时，
    「实例暴露的是 v2」与「探不到 openapi.json」是两条完全不同的线索。
    """
    detected = client.detect_api_version()
    if detected is None:
        report.add(
            PreflightItem(
                key="airflow_api_version",
                label="REST 版本",
                status=WARN,
                blocking=False,
                detail="探不到 openapi.json，无法确认该实例的 REST 版本。",
                next_step=(
                    "下发按 v1 起步、404 时自动改试 v2；若两者都不通，"
                    "核对 endpoint 是否指向 Airflow webserver 本身而非其前置代理的子路径。"
                ),
            )
        )
        return
    report.add(
        PreflightItem(
            key="airflow_api_version",
            label="REST 版本",
            status=PASS,
            blocking=False,
            detail=f"实测该实例暴露的是 {detected}（下发时自动按此版本请求）。",
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


def _check_source_conn(
    report: PreflightReport,
    client: AirflowClient,
    ds: DataSource | None,
    datasource_id: str,
) -> None:
    """同步任务的源库凭据必须在 Airflow 中以确定性 conn_id 存在。"""
    if ds is None:
        report.add(
            PreflightItem(
                key="source_conn",
                label="源库连接",
                status=FAIL,
                blocking=True,
                detail=f"源数据源 {datasource_id} 不存在，无法推导 Airflow Connection。",
                next_step="重新选择一个有效且已启用的业务源数据源。",
            )
        )
        return

    conn_id = _source_conn_id(ds)
    try:
        client.get_connection(conn_id)
    except AirflowError as exc:
        text = str(exc)
        if "403" in text:
            report.add(
                PreflightItem(
                    key="source_conn",
                    label="源库连接",
                    status=WARN,
                    blocking=False,
                    detail=f"无权读取 Connection（403），无法确认「{conn_id}」是否存在。",
                    next_step=f"用有 Connections 读权限的账号核对 conn_id={conn_id}。",
                )
            )
        else:
            report.add(
                PreflightItem(
                    key="source_conn",
                    label="源库连接",
                    status=FAIL,
                    blocking=True,
                    detail=f"Airflow 源库 Connection「{conn_id}」不可用：{exc}",
                    next_step=(
                        f"在 Airflow 创建 conn_id={conn_id}，并配置源库「{ds.name}」"
                        "的类型、主机、端口、数据库和账号密码。"
                    ),
                )
            )
        return

    report.add(
        PreflightItem(
            key="source_conn",
            label="源库连接",
            status=PASS,
            blocking=True,
            detail=f"Connection「{conn_id}」存在，Flink 可读取源库。",
        )
    )


def _check_managed_connections(
    report: PreflightReport,
    db: Session,
    target_ds: DataSource | None,
    *,
    source_ds: DataSource | None,
    source_database: str | None,
) -> None:
    """自包含 DAG 只校验本地连接参数是否足够，运行首任务会负责注册。"""
    if target_ds is None:
        report.add(PreflightItem(
            key="warehouse_conn",
            label="建表连接",
            status=FAIL,
            blocking=True,
            detail="目标数据源不存在，无法生成 DAG 内置 Connection。",
            next_step="重新选择有效的目标数据源。",
        ))
        return
    try:
        payloads = build_embedded_airflow_connections(
            db,
            target_ds,
            source_ds=source_ds,
            source_alias=_source_conn_id(source_ds) if source_ds else None,
            source_database=source_database,
        )
    except Exception as exc:  # noqa: BLE001 - 转成结构化 preflight 结果
        report.add(PreflightItem(
            key="managed_connections",
            label="DAG 内置连接",
            status=FAIL,
            blocking=True,
            detail=str(exc),
            next_step="补齐数据源 DSN、账号密码、默认数据库与 Doris fenodes 后重试。",
        ))
        return

    labels = ["建表连接"]
    if source_ds is not None:
        labels.extend(["Doris Flink 写入连接", "源库连接"])
    for payload, label in zip(payloads, labels):
        report.add(PreflightItem(
            key=(
                "warehouse_conn" if label == "建表连接"
                else "doris_flink_conn" if label.startswith("Doris")
                else "source_conn"
            ),
            label=label,
            status=PASS,
            blocking=True,
            detail=f"Connection「{payload['conn_id']}」将由 DAG 首任务自动创建或更新。",
        ))


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
            password=airflow.ssh_password or None,
        )
        dags_dir = airflow.dags_dir
        # mkdir -p 再测可写：目录不存在是常态（首次投递会建），不该报成失败。
        delivery._ssh(f"mkdir -p '{dags_dir}' && test -w '{dags_dir}'")
    except DagDeliveryError as exc:
        detail = str(exc)
        # 建不出来时最常见的是「填了个 Airflow 根本没在扫的路径」（如照抄官方镜像的
        # /opt/airflow/dags，而那台是原生安装）。此时该改配置，而不是去 sudo mkdir。
        if "Permission denied" in detail or "mkdir" in detail:
            detail += (
                f"\n提示：这里用的是「已保存」的 DAG 目录 {airflow.dags_dir}"
                "（表单里刚改、还没点保存的值不参与拨测）。它要填的是那台 Airflow "
                "已经在扫描的目录，不是新建一个目录给它——投进别处 Airflow 也不会去看。"
                "Airflow 跑在容器里时，/opt/airflow/dags 是容器内路径，而 SSH 投递落在"
                "宿主机上，该填宿主机上挂载到它的那个目录；原生安装则是 "
                "$AIRFLOW_HOME/dags（通常 ~/airflow/dags）。"
            )
        return False, detail
    return True, f"{delivery.target}:{airflow.dags_dir} 可写，投递管道连通。"


def _check_doris_flink_conn(
    report: PreflightReport, db: Session, client: AirflowClient, ds: DataSource | None
) -> None:
    """Verify the separate Doris Connector connection (FE HTTP/fenodes)."""
    if ds is None or getattr(ds, "kind", None) != "doris" or not hasattr(db, "query"):
        return
    from app.models import DorisWarehouseConfig

    config = (
        db.query(DorisWarehouseConfig)
        .filter(DorisWarehouseConfig.warehouse_datasource_id == ds.id)
        .first()
    )
    if config is None or not config.airflow_flink_conn_id:
        report.add(PreflightItem(
            key="doris_flink_conn",
            label="Doris Flink 写入连接",
            status=FAIL,
            blocking=True,
            detail="未配置 DorisWarehouseConfig/Flink Connection ID。",
            next_step="在数据源设置中保存 8030 fenodes，并创建确定性的 *_flink Airflow Connection。",
        ))
        return
    try:
        connection = client.get_connection(config.airflow_flink_conn_id)
    except AirflowError as exc:
        report.add(PreflightItem(
            key="doris_flink_conn",
            label="Doris Flink 写入连接",
            status=FAIL,
            blocking=True,
            detail=f"Airflow Connection {config.airflow_flink_conn_id} 不可用：{exc}",
            next_step="创建/修复该 Connection，并在 extra 中配置 fenodes 与 jdbc_url。",
        ))
        return
    extra = connection.get("extra") or connection.get("extra_dejson") or {}
    if isinstance(extra, str):
        import json
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    missing = [key for key in ("fenodes", "jdbc_url") if not (extra or {}).get(key)]
    report.add(PreflightItem(
        key="doris_flink_conn",
        label="Doris Flink 写入连接",
        status=FAIL if missing else PASS,
        blocking=True,
        detail=(
            f"Connection extra 缺少：{', '.join(missing)}"
            if missing else f"{config.airflow_flink_conn_id} 已配置 fenodes/jdbc_url。"
        ),
        next_step=("补齐 Connection extra 后重试。" if missing else None),
    ))


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
        _check_dag_dir_matches_instance(report, client, airflow, ssh_ok=True)
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
                "②已填 SSH 密码且 ontoMeta 侧装了 sshpass（或留空密码、走本机 ~/.ssh 免密身份）；"
                "③DAG 目录填的是那台 Airflow 已在扫描的 dags_folder（点「从 Airflow 读取」取回），"
                "且该用户对它有写权限。以上都在设置页 → Airflow → DAG 投递。"
            ),
        )
    )
    _check_dag_dir_matches_instance(report, client, airflow, ssh_ok=False)


def dags_folder_of(client: AirflowClient) -> str | None:
    """问 Airflow 自己：你在扫哪个目录（``core.dags_folder``）。

    ``expose_config=False`` 时读不到，返回 None——这不是错误，只是 REST 这条路没法对账，
    还可以走 SSH 那条（见 :func:`dags_folder_via_ssh`）。
    """
    return client.get_config_option("core", "dags_folder")


def dags_folder_via_ssh(airflow) -> str | None:
    """REST 不肯说时，改在那台机器上问它自己。

    ``expose_config=False`` 是默认配置，不是异常状态，所以「读不到就让人自己去核对」
    等于把最常见的一种失败（投递目录压根不是 Airflow 扫的那个）永久留在盲区里。而投递
    本来就走 SSH——同一台主机、同一把钥匙，这里不新增任何信任面：
    ``airflow config get-value core dags_folder`` 既认 airflow.cfg，也认
    ``AIRFLOW__CORE__DAGS_FOLDER``。

    读不到就返回 None（没装 airflow / 非交互 shell 的 PATH 上没有 / AIRFLOW_HOME 不同），
    退回「无法对账」的提醒——拿一个未必是那台调度器在用的路径去对账，比不对账更糟。

    模块级函数是为了可注入：单测覆盖它即可，不必真起 ssh（同 :func:`probe_ssh_pipeline`）。
    """
    if not getattr(airflow, "ssh_host", ""):
        return None
    from app.services.dag_delivery import get_delivery

    try:
        delivery = get_delivery(
            airflow.ssh_host,
            user=airflow.ssh_user or None,
            port=airflow.ssh_port,
            password=airflow.ssh_password or None,
        )
        proc = delivery._ssh("airflow config get-value core dags_folder")
    except Exception:  # noqa: BLE001 — 旁路对账失败不该带走整份自检
        return None
    # airflow CLI 常在 stdout 里夹带 deprecation 警告，取最后一行；不是绝对路径就当没读到。
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    return tail if tail.startswith("/") else None


def _within(child: str, parent: str) -> bool:
    """child 是否就是 parent 或落在 parent 之下（两边都按 posix 规范化）。"""
    c = posixpath.normpath(child.rstrip("/") or "/")
    p = posixpath.normpath(parent.rstrip("/") or "/")
    return c == p or c.startswith(p + "/")


def _check_dag_dir_matches_instance(
    report: PreflightReport, client: AirflowClient, airflow, *, ssh_ok: bool
) -> None:
    """把投递目录和实例自报的 ``core.dags_folder`` 对一次账。

    治的是失败模式 #3 最隐蔽的一种：SSH 通、目录可写、rsync 成功、回执一片绿，可
    Airflow 从头到尾没扫过那个路径，只能等 dag_parse_timeout 超时才暴露。

    **只提醒、不阻断**：容器部署下两者本来就不同——``dags_folder`` 是**容器内**路径
    （官方镜像恒为 ``/opt/airflow/dags``），而 SSH 投递落在**宿主机**文件系统上，该填的
    是挂载到那个容器路径的宿主机目录。ontoMeta 看不见这层映射，故不一致只是"值得核对"，
    不是"必错"。软链接同理。
    """
    folder = dags_folder_of(client)
    reading = "REST /config"
    if not folder and ssh_ok:
        # expose_config 关着不等于查不到：投递管道刚验通，就用它去问那台机器本人。
        folder = dags_folder_via_ssh(airflow)
        reading = "SSH 在主机上读取"
    if not folder:
        report.add(
            PreflightItem(
                key="dag_dir_matches_instance",
                label="DAG 目录与实例一致",
                status=WARN,
                blocking=False,
                detail=(
                    "该实例关掉了 expose_config，读不到 core.dags_folder"
                    + ("，SSH 上也没问出来（远端没有 airflow 命令或 AIRFLOW_HOME 不同）" if ssh_ok else "")
                    + "，无法对账。"
                ),
                next_step=(
                    f"手动核对 Airflow 主机上的 dags_folder 是否就是 {airflow.dags_dir}"
                    "（在那台机器上跑 `airflow config get-value core dags_folder`，"
                    "或看 airflow.cfg 的 [core] dags_folder / AIRFLOW__CORE__DAGS_FOLDER）。"
                ),
            )
        )
        return
    if _within(airflow.dags_dir, folder):
        report.add(
            PreflightItem(
                key="dag_dir_matches_instance",
                label="DAG 目录与实例一致",
                status=PASS,
                blocking=False,
                detail=f"实例扫描 {folder}（{reading}），投递目录 {airflow.dags_dir} 在其中。",
            )
        )
        return
    report.add(
        PreflightItem(
            key="dag_dir_matches_instance",
            label="DAG 目录与实例一致",
            status=WARN,
            blocking=False,
            detail=(
                f"实例扫描的是 {folder}（{reading}），投递目录配的是 {airflow.dags_dir}，两者不一致。"
            ),
            next_step=(
                f"若 Airflow 是**直接装在这台机器上**：把 DAG 目录改成 {folder}"
                "（点「从 Airflow 读取」可直接填入），否则产物投过去没人扫。"
                f"若 Airflow 跑在**容器里**：{folder} 是容器内路径，这里要填宿主机上"
                "挂载到它的那个目录（docker inspect / compose 的 volumes 可查），"
                "此时不一致是正常的。"
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
    *,
    emit: Emit,
    load_strategy: str | None,
    incremental_column: str | None,
    initial_watermark: str | None,
    flink_task_params: dict[str, Any] | None,
) -> None:
    """统一执行架构：搬运一律走 Flink SQL on YARN。前置条件只两件：JAR 与 checkpoint_dir。

    **只对搬运（``emit="dml"``，即同步）体检。** 物化（``emit="ddl"``）压根不调 JobPlanner、
    不产 Flink 作业——建表是 ``SQLExecuteQueryOperator`` 直连目标仓（见
    ``materialization_runner._run_orchestrated``）。此前不分 emit 一律预演搬运，物化于是会
    被别人契约上的装载方式判出「缺 checkpoint」而拒绝提交，人还查不到那张表是谁。

    - **flink_sql_runner_jar**：缺它同步退回「仅产出」（不执行），标 WARN 提醒。
    - **flink_checkpoint_dir**：**只有 CDC** 这种常驻流作业需要它持久化读位点。
      ``incremental`` 是带水位谓词的**有界 batch**（见 ``generate_move_sql``），跑完即退，
      不碰 checkpoint；它的编译期硬条件是主键 + 增量字段 + 初始水位，那三项由
      ``_check_sync_spec`` 按**本任务的** Spec 直接判，不在这里借 checkpoint 之名喊话。

    **预演必须与真跑同参**：``load_strategy``（Spec 里选的全量/增量）、落点恒为 ODS、增量
    字段与水位都按本次任务传入。否则一条「全量」同步会因为该对象**契约**上写着 incremental
    而被预演成流式作业，报一个它根本不会遇到的阻断——而人在表单上从没选过 CDC。
    """
    if emit != "dml":
        return

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

    # 可搬性预演：本次有几张表、各是什么装载方式。参数与 ``run_sync`` 一一对应。
    from app.services.job_planner import JobPlanner
    from app.services.ods_naming import ODS_DATABASE

    entities = list(selected_targets or [])
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
            # 本次运行的装载方式覆盖：人在表单上选的「全量」必须压过契约上的 incremental/cdc，
            # 与 ``materialization_runner.run_sync`` 传的是同一个值。
            load_strategy=load_strategy,
            # 同步落点恒为 ODS 库（见 ods_naming）。不传它，预演出来的是分层库表，作业名
            # 都写着 sync_dim_xxx——而同步从不写 dim，只是把预演结果说成了另一件事。
            target_ods_database=ODS_DATABASE,
            incremental_columns=(
                {e: incremental_column for e in entities} if incremental_column else None
            ),
            initial_watermarks=(
                {e: initial_watermark for e in entities} if initial_watermark else None
            ),
        )
    except Exception:
        # 计划失败（本体不存在/无契约）不在 execution_channel 检查范围，别的项会报。
        return

    # 任务级 checkpoint 目录优先于设置页（与 flink_params.resolve_config 同一口径）：
    # 只看全局的话，人在这条任务上填了目录仍会被拦。
    checkpoint_dir = flink_params.resolve_checkpoint_dir(airflow, flink_task_params)
    cdc_jobs = [j.name for j in plan.jobs if j.mode == "cdc"]
    if cdc_jobs and not checkpoint_dir:
        report.add(
            PreflightItem(
                key="flink_checkpoint",
                label="Flink Checkpoint 目录",
                status=FAIL,
                blocking=True,
                detail=(
                    f"本次有 {len(cdc_jobs)} 张 CDC 表（如 {cdc_jobs[0]}），"
                    "但未配置 Flink Checkpoint 目录。CDC 是流式作业、需 checkpoint 持久化读位点，"
                    "否则重启会重搬。编译期会直接报错，提交无法成功。"
                ),
                next_step=(
                    "在本任务的「Checkpoint 目录」填写（file://… 本地 或 hdfs://… 集群），"
                    "或到 设置 → Airflow/Flink 配一个全局默认；"
                    "也可把装载方式改为全量/增量，两者都是有界批作业，不需要 checkpoint。"
                ),
            )
        )
    elif cdc_jobs:
        report.add(
            PreflightItem(
                key="flink_checkpoint",
                label="Flink Checkpoint 目录",
                status=PASS,
                blocking=False,
                detail=f"checkpoint 目录已配置：{checkpoint_dir[:60]}…（CDC 可用）",
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
    *,
    emit: Emit = "ddl",
    load_strategy: str | None = None,
    incremental_column: str | None = None,
    initial_watermark: str | None = None,
    flink_task_params: dict[str, Any] | None = None,
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
        report,
        db,
        ontology_id,
        airflow,
        ds,
        engine,
        selected_targets,
        emit=emit,
        load_strategy=load_strategy,
        incremental_column=incremental_column,
        initial_watermark=initial_watermark,
        flink_task_params=flink_task_params,
    )
    _check_batch_size(report, db, ontology_id, selected_targets, airflow.max_tasks_per_dag)
