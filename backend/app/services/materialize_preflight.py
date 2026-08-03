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

from app.config import Settings
from app.connectors.airflow import AirflowClient, AirflowError
from app.models.data_app import DataSource
from app.services.materialization_contract import MaterializationContractService
from app.services.materialization_runner import _warehouse_conn_id
from app.services.settings_service import SettingsService

_settings = SettingsService()
_contract_service = MaterializationContractService()
_env = Settings()

# 状态取值。warn 不阻断提交，fail 视 blocking 决定是否阻断。
PASS = "pass"
WARN = "warn"
FAIL = "fail"

# preflight 落地的 sentinel DAG：写进 dags 目录，看 Airflow 多久解析到它。
# 内容是一个最小合法 DAG，探测完即删——留一个报错的 .py 会污染 Airflow 的 import errors。
_SENTINEL_DAG_TEMPLATE = '''"""ontoMeta preflight sentinel（自动生成，探测完即删，可安全删除）。"""
import pendulum
from airflow import DAG

with DAG(
    dag_id="{dag_id}",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["ontometa", "preflight"],
):
    pass
'''


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
        _add_conn_and_batch(report, db, ontology_id, ds, selected_targets, airflow)
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

    _check_batch_size(report, db, ontology_id, selected_targets)
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


def _check_dag_dir_visible(
    report: PreflightReport, client: AirflowClient, airflow
) -> None:
    """专治失败模式 #3：ontoMeta 写的是自己文件系统的路径，Airflow 却按另一个路径挂载。

    做法：往 dags 目录写一个 sentinel DAG，轮询 ``GET /dags/{id}`` 看 Airflow 多久解析到。
    先验本地可写（这一步单独就能抓到「ontoMeta 根本写不进去」）；再看 Airflow 侧能否看见。
    超时不硬失败——Airflow 的 dag_dir_list_interval 默认 300s（⚠ §8.1），慢≠不一致，
    故降级为提醒，并把两种可能都写清楚。
    """
    dags_dir = airflow.dags_dir
    sentinel_id = f"ontometa_preflight_{uuid.uuid4().hex[:8]}"
    path = os.path.join(dags_dir, f"{sentinel_id}.py")
    try:
        os.makedirs(dags_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_SENTINEL_DAG_TEMPLATE.format(dag_id=sentinel_id))
    except OSError as exc:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 目录双向可见",
                status=FAIL,
                blocking=True,
                detail=f"ontoMeta 写不进 DAG 投递目录：{exc}",
                next_step=(
                    f"确认 {dags_dir} 存在且 ontoMeta 进程有写权限（airflow_dags_dir 环境变量）。"
                ),
            )
        )
        return

    timeout = _env.ontometa_preflight_sentinel_timeout
    deadline = time.monotonic() + timeout
    seen = False
    try:
        while True:
            try:
                seen = client.dag_exists(sentinel_id)
            except AirflowError:
                seen = False  # 鉴权/网络问题已由前面的项报过，这里不重复
            if seen or time.monotonic() >= deadline:
                break
            time.sleep(1.5)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if seen:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 目录双向可见",
                status=PASS,
                blocking=True,
                detail=f"Airflow 在 {timeout:.0f}s 内解析到了 sentinel，两侧目录一致。",
            )
        )
    else:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 目录双向可见",
                status=WARN,
                blocking=False,
                detail=(
                    f"sentinel 已写入 {path}，但 {timeout:.0f}s 内 Airflow 未解析到它。"
                ),
                next_step=(
                    "两种可能：①ontoMeta 的 dags 目录与 Airflow 容器挂载的不是同一个"
                    "（会导致提交后 --config 指向的文件在 Airflow 侧不存在），请核对"
                    f"airflow_dags_dir（{dags_dir}）与 Airflow 的 dags_folder 是否同一路径；"
                    "②目录一致但 dag_dir_list_interval 较长（默认 300s），此项可忽略。"
                ),
            )
        )


def _selected_contracts(
    db: Session, ontology_id: str, selected_targets: list[str] | None
):
    """选中的、已标记物化的契约。selected_targets 为实体名（非物理表名）。"""
    contracts = _contract_service.list_contracts(
        db, ontology_id, materialized_only=True
    )
    if selected_targets:
        names = _contract_service.resolve_target_names(db, contracts)
        wanted = set(selected_targets)
        contracts = [
            c for c in contracts if (names.get(c.target_id) or (None,))[0] in wanted
        ]
    return contracts


def _check_batch_size(
    report: PreflightReport,
    db: Session,
    ontology_id: str,
    selected_targets: list[str] | None,
) -> None:
    """表数 vs 单 DAG 上限。M16 前不会自动分批，故超限只提醒「本次仍塞进一个 DAG」。"""
    count = len(_selected_contracts(db, ontology_id, selected_targets))
    limit = _env.ontometa_max_tasks_per_dag
    if count > limit:
        report.add(
            PreflightItem(
                key="batch_size",
                label="批次规模",
                status=WARN,
                blocking=False,
                detail=f"本次 {count} 张表，超过单 DAG 上限 {limit}。",
                next_step=(
                    "当前版本仍会塞进一个 DAG（自动分批见 M16）；可先勾选一部分实体分批"
                    "提交，避免一次拉起过多并发容器、且单表失败拖红整轮。"
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
) -> None:
    """Airflow 不可达时的补充项：建表连接无从查（标 fail），批次规模仍可本地算。"""
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
    _check_batch_size(report, db, ontology_id, selected_targets)
