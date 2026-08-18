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


def _check_git_sync_pipeline(report: PreflightReport, airflow) -> None:
    """git-sync 投递的管道检查：验证 git 仓库与 remote 可达性。

    local 投递的 sentinel 探测在 git-sync 下不成立（本地写的文件不 push 就到不了 Airflow），
    而提交一次性 sentinel 又会污染 git 历史。改为直接验证 git 管道本身：
    1. dags_dir 在不在一个 git 仓库里
    2. 配置的 remote 存不存在、能不能连通（git ls-remote）

    这两步过了，投递管道就是通的——产物能 commit + push、Airflow 侧 git-sync 能拉到。
    不验「Airflow 侧配没配 git-sync」（那是 Airflow 部署的事，ontoMeta 管不到），只验
    「ontoMeta 这头能不能推」。
    """
    import subprocess
    from pathlib import Path

    dags_dir = airflow.dags_dir
    remote_name = getattr(airflow, "git_remote", "origin") or "origin"

    # 1. dags_dir 在不在 git 仓库里
    current = Path(dags_dir).resolve()
    repo_root = None
    for parent in [current] + list(current.parents):
        if (parent / ".git").is_dir():
            repo_root = str(parent)
            break

    if not repo_root:
        auto_init = getattr(airflow, "git_auto_init", False)
        if auto_init:
            report.add(
                PreflightItem(
                    key="dag_dir_visible",
                    label="DAG 投递管道（git-sync）",
                    status=PASS,
                    blocking=False,
                    detail=(
                        f"{dags_dir} 当前不是 git 仓库，但 git_auto_init=True，"
                        "首次投递时会自动 git init。"
                    ),
                )
            )
        else:
            report.add(
                PreflightItem(
                    key="dag_dir_visible",
                    label="DAG 投递管道（git-sync）",
                    status=FAIL,
                    blocking=True,
                    detail=f"{dags_dir} 不在 git 仓库中，且 git_auto_init=False。",
                    next_step=(
                        f"在 {dags_dir} 执行 `git init` 并配置 remote，"
                        "或在设置页开启 Airflow 的「git 自动初始化」（git_auto_init）。"
                    ),
                )
            )
        return

    # 2. remote 能不能连通
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", remote_name],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        remote_url = subprocess.run(
            ["git", "remote", "get-url", remote_name],
            cwd=repo_root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 投递管道（git-sync）",
                status=PASS,
                blocking=True,
                detail=(
                    f"git 仓库：{repo_root}\n"
                    f"remote {remote_name}：{remote_url}\n"
                    "ls-remote 成功，投递管道连通。"
                ),
            )
        )
    except subprocess.TimeoutExpired:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 投递管道（git-sync）",
                status=FAIL,
                blocking=True,
                detail=f"git ls-remote {remote_name} 超时（10s），remote 可能不可达。",
                next_step=(
                    f"检查 git remote（在 {repo_root} 执行 `git remote -v`），"
                    "确认推送凭据（SSH key / HTTPS token）已配置且有效。"
                ),
            )
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 投递管道（git-sync）",
                status=FAIL,
                blocking=True,
                detail=f"git ls-remote {remote_name} 失败：{stderr or '无输出'}",
                next_step=(
                    f"在 {repo_root} 执行 `git remote add {remote_name} <URL>` 配置 remote，"
                    "或检查推送凭据（SSH key / HTTPS token）。"
                ),
            )
        )


# 历史投递的 DAG 至少这么老还没被 Airflow 认领，才算「路径不一致」的确凿证据。
# 取值要明显大于 Airflow 的 dag_dir_list_interval 默认值（300s），否则会把「扫得慢」
# 误判成「路径错」——宁可退回 sentinel 探测给个提醒，也不要误报阻断。
_DELIVERED_DAG_PROOF_AGE = 900.0


def _delivered_dag_ids(dags_dir: str) -> list[tuple[str, float]]:
    """ontoMeta 已投进 dags 目录的 (dag_id, 文件年龄秒)。sentinel 自己不算数。

    **递归遍历**：产物按 ``<dags_dir>/ontometa/<artifact_id>/`` 子目录聚合后，DAG 文件
    不再在根目录平铺。这里用 ``os.walk`` 同时认到子目录布局与历史的平铺布局
    （两者文件名都仍是 ``{dag_id}.py``），否则子目录方案下此对账永远空、退化成只靠 sentinel。
    """
    out: list[tuple[str, float]] = []
    now = time.time()
    for root, _dirs, files in os.walk(dags_dir):
        for name in files:
            if not name.startswith("ontometa_") or not name.endswith(".py"):
                continue
            dag_id = name[: -len(".py")]
            if dag_id.startswith("ontometa_preflight_"):
                continue
            try:
                age = now - os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            out.append((dag_id, age))
    return out


def _check_delivered_dags_visible(
    report: PreflightReport, client: AirflowClient, airflow
) -> bool:
    """用**历史投递**判定两侧目录是否一致；判得了就 True（调用方不必再赌 sentinel）。

    这是对 sentinel 探测的补强。sentinel 只等 ``preflight_sentinel_timeout``（默认 20s），
    而 Airflow 的 ``dag_dir_list_interval`` 默认 300s——正常环境也会超时，于是那条永远是
    「可能 A 可能 B」的提醒，久了就没人看，真出问题时反而被当噪声划过去。

    历史投递没有这个歧义：一个 15 分钟前就写进去的 DAG 文件，Airflow 若仍不认，那与扫描
    快慢无关，就是两侧路径不是同一个。这时才敢给 blocking。
    """
    dags_dir = airflow.dags_dir
    delivered = _delivered_dag_ids(dags_dir)
    if not delivered:
        return False  # 头一次投，没有历史可依，交给 sentinel

    try:
        visible = set(client.list_dag_ids(pattern="ontometa"))
    except AirflowError:
        return False  # 读不到 DAG 列表（鉴权/网络）已由别的项报过

    hit = [d for d, _ in delivered if d in visible]
    if hit:
        report.add(
            PreflightItem(
                key="dag_dir_visible",
                label="DAG 目录双向可见",
                status=PASS,
                blocking=True,
                detail=(
                    f"Airflow 已认领 ontoMeta 投在 {dags_dir} 的 "
                    f"{len(hit)}/{len(delivered)} 个 DAG，两侧目录一致。"
                ),
            )
        )
        return True

    oldest_age = max(age for _, age in delivered)
    if oldest_age < _DELIVERED_DAG_PROOF_AGE:
        return False  # 投得太新，分不清「路径错」还是「还没扫到」→ 交给 sentinel

    airflow_folder = client.get_config_option("core", "dags_folder")
    where = (
        f"Airflow 自报的 dags_folder 是 {airflow_folder}，ontoMeta 投的是 {dags_dir}——两者不是同一个目录。"
        if airflow_folder
        else (
            f"Airflow 未开放配置查询（expose_config=False），无法直接读出它的 dags_folder；"
            f"ontoMeta 投的是 {dags_dir}。"
        )
    )
    report.add(
        PreflightItem(
            key="dag_dir_visible",
            label="DAG 目录双向可见",
            status=FAIL,
            blocking=True,
            detail=(
                f"ontoMeta 已在 {dags_dir} 投了 {len(delivered)} 个 DAG 文件，最早的一个已存在 "
                f"{oldest_age / 60:.0f} 分钟，Airflow 却一个都没认领。这不是扫描慢（远超 "
                f"dag_dir_list_interval 默认的 300s），是两侧读写的不是同一个目录。" + where
            ),
            next_step=(
                "二选一对齐路径："
                f"①把 Airflow 的 core.dags_folder 改成 {dags_dir} 后重启 scheduler；"
                "②或到 设置 → Airflow → DAG 投递目录，把它改成 Airflow 实际的 dags_folder"
                "（在 Airflow 的 airflow.cfg 里查 dags_folder，或开 expose_config 后由本检查自动读出）。"
                "改完重跑本自检，本项应转为通过。"
            ),
        )
    )
    return True


def _check_dag_dir_visible(
    report: PreflightReport, client: AirflowClient, airflow
) -> None:
    """专治失败模式 #3：ontoMeta 写的是自己文件系统的路径，Airflow 却按另一个路径挂载。

    做法：往 dags 目录写一个 sentinel DAG，轮询 ``GET /dags/{id}`` 看 Airflow 多久解析到。
    先验本地可写（这一步单独就能抓到「ontoMeta 根本写不进去」）；再看 Airflow 侧能否看见。
    超时不硬失败——Airflow 的 dag_dir_list_interval 默认 300s（⚠ §8.1），慢≠不一致，
    故降级为提醒，并把两种可能都写清楚。

    **git-sync 投递例外**：产物经 git push → Airflow 侧 git-sync 拉取，写一个本地
    sentinel 再删掉根本进不了远程，探测必然假 WARN。故 git 模式改验「git 管道通不通」
    ——仓库在不在、remote 能不能连——不污染 git 历史塞一次性 sentinel。
    """
    if (getattr(airflow, "dag_delivery_method", "local") or "local").strip().lower() in (
        "git",
        "git-sync",
        "gitsync",
    ):
        _check_git_sync_pipeline(report, airflow)
        return

    # 先用历史投递对账：判得了就不必再赌 sentinel 的 20s 时序（且能给出 blocking 结论）。
    if _check_delivered_dags_visible(report, client, airflow):
        return

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
                    f"确认 {dags_dir} 存在且 ontoMeta 进程有写权限（设置页 → Airflow → DAG 投递目录）。"
                ),
            )
        )
        return

    timeout = airflow.preflight_sentinel_timeout
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

    - **flink_sql_runner_jar**：缺它退回「仅产出」（不执行），preflight 标 WARN 提醒。
    - **flink_checkpoint_dir**：增量/CDC 是流式作业需要它持久化读位点；含 timestamp 分区键
      的表默认 incremental，缺 checkpoint 会在编译期硬失败。这里先验可搬性，若有增量表
      但无 checkpoint，标 FAIL。全量物化不需要 checkpoint。

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
                    "未配置 Flink SqlRunner JAR，搬运不执行、只产出 SQL（handoff 模式）。"
                    "建表仍会产 DDL、可用物化触发，但数据搬运需配上 JAR 并重跑。"
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
