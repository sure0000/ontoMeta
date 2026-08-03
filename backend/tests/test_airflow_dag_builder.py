"""DAG 生成：结构、幂等、凭据、以及「建表不交给搬运工具」这条铁律。

DAG 源码在这里只做**语法与结构**校验（ast 解析 + 桩模块建图）：Airflow 2.10 装不进本仓后端
所用的 Python 版本，同一个 venv 里做不到真解析。**真 Airflow 的 DagBag 解析是 `make dag-parse`**
（在 Airflow 镜像里跑，见 `scripts/dag_parse_check.py`）——Operator 关键字合不合法、provider
在不在、连线在真 BaseOperator 上成不成立，只有它说了算，本文件的桩验不了。改 DAG 模板后两边都要跑。
"""

from __future__ import annotations

import ast
import json

import pytest

from app.services.airflow_dag_builder import AirflowDagBuilder, dag_id_for
from app.warehouse.jobs import (
    ColumnMapping,
    JobEndpoint,
    JobPlan,
    JobSpec,
    SyncImageUnavailableError,
)

_builder = AirflowDagBuilder()


def _job(name: str, layer: str = "dim", table: str = "customer") -> JobSpec:
    return JobSpec(
        name=name,
        source=JobEndpoint(
            alias="erp_readonly", platform="mariadb", database="erp_ods", table="tab_customer"
        ),
        target=JobEndpoint(
            alias="warehouse_default", platform="hive", database="dim", table=table
        ),
        columns=(ColumnMapping(source="cust_id", target="customer_id"),),
        layer=layer,
        source_urn="urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.tab_customer,PROD)",
    )


def _bundle(**kwargs):
    plan = JobPlan(jobs=(_job("sync_dim_customer"), _job("sync_dwd_order", "dwd", "orders")))
    return _builder.build(
        ontology_id="11112222-3333-4444-5555-666677778888",
        plan=plan,
        ddl_statements={
            "dim.customer": "CREATE TABLE IF NOT EXISTS `dim`.`customer` (…)",
            "dwd.orders": "CREATE TABLE IF NOT EXISTS `dwd`.`orders` (…)",
        },
        **kwargs,
    )


def test_dag_source_is_valid_python():
    bundle = _bundle()
    ast.parse(bundle.dag_source)  # 语法错误的 DAG 会让 Airflow 整个目录报 import error


def test_ddl_carries_no_trailing_semicolon():
    """spec 里的 DDL 是逐条交给 DB-API execute() 的，一次只能一条语句。

    生成器产出的是脚本形态（带 ``;``）；Hive 收到末尾分号会直接
    ``ParseException: extraneous input ';' expecting EOF``，建表全挂。
    """
    bundle = _builder.build(
        ontology_id="11112222-3333-4444-5555-666677778888",
        plan=JobPlan(jobs=(_job("sync_dim_customer"),)),
        ddl_statements={
            "dim.customer": "CREATE TABLE `dim`.`customer` (id INT);",
            "dwd.orders": "CREATE TABLE `dwd`.`orders` (id INT)\n;\n",
        },
    )
    assert [s for s in bundle.spec["ddl"] if s.endswith(";")] == []
    assert bundle.spec["ddl"][0].endswith(")")


def test_job_config_is_mounted_into_sync_container():
    """``--config`` 指向的作业配置必须真的挂进搬运容器。

    任务容器是 worker 经 docker.sock 起的**兄弟容器**，挂载 source 必须是宿主机路径；
    不挂就是 SeaTunnel 的 ``file … not existed``（真实实例上踩过）。
    """
    bundle = _builder.build(
        ontology_id="11112222-3333-4444-5555-666677778888",
        plan=JobPlan(jobs=(_job("sync_dim_customer"),)),
        ddl_statements={"dim.customer": "CREATE TABLE `dim`.`customer` (id INT)"},
        jobs_host_dir="/host/seatunnel/jobs",
    )
    assert bundle.spec["jobs_host_dir"] == "/host/seatunnel/jobs"
    assert bundle.spec["jobs_mount"] == "/opt/seatunnel/jobs"
    # 挂载点与命令里的 --config 路径必须同一个前缀，否则挂了也找不到
    command = bundle.spec["tasks"][0]["command"]
    assert any(str(a).startswith(bundle.spec["jobs_mount"]) for a in command)
    assert "mounts=_JOB_MOUNTS" in bundle.dag_source


def test_credential_placeholders_are_injected_at_runtime():
    """作业配置里的 ``${别名_字段}`` 必须有注入端，否则搬运任务拿不到连接串。

    注入走 Airflow Connection，且值是 Jinja 表达式——**运行时**才解析，
    产物里不能出现任何真实凭据。
    """
    task = _bundle().spec["tasks"][0]
    env = task["env"]

    # 别名即 conn_id：源 erp_readonly、目标 warehouse_default（见 _job()）
    assert env["ERP_READONLY_USER"] == "{{ conn.erp_readonly.login }}"
    assert env["ERP_READONLY_PASSWORD"] == "{{ conn.erp_readonly.password }}"
    assert env["ERP_READONLY_URL"].startswith("jdbc:mariadb://{{ conn.erp_readonly.host }}")
    # Hive 目标的 metastore 地址推不出来，从 Connection 的 extra 取
    assert "metastore_uri" in env["WAREHOUSE_DEFAULT_METASTORE_URI"]

    # 每个占位符都要有对应的注入项，不能只定义不注入
    for value in env.values():
        assert value.startswith("{{") or "{{ conn." in value


def test_dag_passes_credentials_via_templated_environment():
    """environment 必须交给 DockerOperator，且走模板字段而非解析期求值。"""
    source = _bundle().dag_source
    assert "environment=task[\"env\"]" in source


def test_sync_operator_disables_template_file_loading():
    """搬运命令里的 ``.sh`` 是参数，不是要加载的模板文件。

    DockerOperator 的 template_ext 默认含 ``.sh``，Airflow 会把它当 DAG 目录下的模板
    文件去读，每个搬运任务在渲染期就失败（真实实例上踩过）。
    """
    source = _bundle().dag_source
    assert "template_ext = ()" in source
    # 任务必须用清空过 template_ext 的子类，不能直接用 DockerOperator
    assert "op = _SyncOperator(" in source


def test_docker_operator_kwargs_are_real_parameters():
    """DockerOperator 的关键字必须是它真有的参数。

    语法合法但参数名写错时，Airflow 是在**解析期**抛 AirflowException（"Invalid
    arguments were passed"），整个 DAG 导入失败——``ast.parse`` 那条测试看不出来。
    真实实例上踩过 ``mounts_tmp_dir``（正确的是单数 ``mount_tmp_dir``），钉住。
    """
    source = _bundle().dag_source
    # providers-docker 的参数名，逐个核对过；写错任何一个都会导入失败。
    assert "mount_tmp_dir=False" in source
    assert "mounts_tmp_dir" not in source


def test_dag_id_is_stable_per_ontology():
    """同一本体反复物化更新同一个 DAG，不堆垃圾 DAG。"""
    assert dag_id_for("aaaa-bbbb") == dag_id_for("aaaa-bbbb")
    assert dag_id_for("aaaa-bbbb") != dag_id_for("cccc-dddd")
    assert _bundle().dag_id.startswith("ontometa_materialize_")


def test_dag_id_suffix_and_concurrency_gates():
    """M16：按 cron 分组/分批的 dag_id 后缀 + 并发闸门 + 重试进入 DAG。"""
    bundle = _bundle(dag_id_suffix="c1a2b3c4_b0", max_active_tasks=8)
    assert bundle.dag_id.endswith("__c1a2b3c4_b0")
    assert bundle.spec["max_active_tasks"] == 8
    # 并发闸门与重试写进 DAG 骨架
    assert "max_active_tasks=_SPEC" in bundle.dag_source
    assert "retries" in bundle.dag_source
    assert "retry_exponential_backoff" in bundle.dag_source
    # 无后缀时退回单 DAG 命名（兼容 M16 前）
    assert not _bundle().dag_id.endswith("__")


def test_create_tables_runs_generated_ddl():
    """建表必须跑 M3 的 DDL：本体反补的注释/分区/主键声明只在那条路径上。"""
    bundle = _bundle()
    assert "SQLExecuteQueryOperator" in bundle.dag_source
    assert '_SPEC["ddl"]' in bundle.dag_source
    # DDL 按目标表名排序进 spec，保证幂等
    assert bundle.spec["ddl_targets"] == ["dim.customer", "dwd.orders"]
    assert len(bundle.spec["ddl"]) == 2


def test_sync_tasks_depend_on_create_tables_and_layer_order():
    bundle = _bundle()
    assert bundle.spec["layer_order"] == ["dim", "dwd"]
    assert {t["task_id"] for t in bundle.spec["tasks"]} == {
        "sync_dim_customer",
        "sync_dwd_order",
    }
    # 编排关系写在 DAG 骨架里：create_tables 先行，随后按层串
    assert "previous >> group" in bundle.dag_source
    assert "create_tables" in bundle.dag_source


def test_tasks_declare_lineage_inlets():
    """血缘的上游用本体的 source_ref（本就是 DataHub URN），供插件自动上报。"""
    task = _bundle().spec["tasks"][0]
    assert task["inlets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.tab_customer,PROD)"
    ]
    # 下游 URN 需部署环境的 fabric，M10 不臆造，留给 M11 注入
    assert task["outlets"] == []


def test_target_urn_builder_injects_outlets():
    """M11：注入 target_urn_builder 后，outlets 填真实目标表 URN，供插件上报下游血缘。"""
    from app.connectors.datahub import build_dataset_urn

    bundle = _bundle(
        target_urn_builder=lambda job: build_dataset_urn(
            job.target.platform, job.target.qualified, "PROD"
        )
    )
    task = bundle.spec["tasks"][0]
    assert task["outlets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:hive,dim.customer,PROD)"
    ]


def test_no_credentials_in_dag_or_spec():
    """产物里只能有 conn_id 与占位符。"""
    bundle = _bundle()
    blob = bundle.dag_source + json.dumps(bundle.spec, ensure_ascii=False) + json.dumps(
        bundle.job_files, ensure_ascii=False
    )
    for leaked in ("password=", "jdbc:mysql://", "root@", "secret"):
        assert leaked not in blob
    assert bundle.spec["warehouse_conn_id"] == "warehouse_default"
    assert "${ERP_READONLY_PASSWORD}" in json.dumps(bundle.job_files, ensure_ascii=False)


def test_schedule_comes_from_contract_cron():
    assert _bundle(schedule="0 2 * * *").spec["schedule"] == "0 2 * * *"
    # 不定时 → schedule=None，只能手动触发
    assert _bundle().spec["schedule"] is None


def test_watermark_injected_by_scheduler():
    """增量水位由 Airflow 的 data_interval_start 注入，不再是无人赋值的占位符。"""
    # 命令由工具 Adapter 产出并落入 spec 的每个 task；DockerOperator 的 command 是模板字段。
    task = _bundle().spec["tasks"][0]
    assert any("data_interval_start" in part for part in task["command"])


def test_tasks_carry_tool_image_and_command():
    """镜像与命令按工具逐任务写入 spec，DAG 骨架对工具无感（工具可插拔的落地）。"""
    # 默认 seatunnel
    st = _bundle().spec["tasks"][0]
    assert st["image"] == "apache/seatunnel:2.3.11"
    assert st["command"][0].endswith("seatunnel.sh")
    # datax：无官方镜像，须由部署方指定（见 test_tool_without_image_is_rejected）
    dx = _bundle(tool="datax", image_overrides={"datax": "acme/datax:3.0"}).spec
    assert dx["tool"] == "datax"
    assert dx["tasks"][0]["image"] == "acme/datax:3.0"
    assert "datax.py" in " ".join(dx["tasks"][0]["command"])
    # flink
    fl = _bundle(tool="flink").spec
    assert fl["tool"] == "flink"
    assert fl["tasks"][0]["image"] == "apache/flink:1.18"
    # DAG 骨架不硬编镜像：读 task 的 image/command
    assert 'image=task["image"]' in _bundle().dag_source
    assert "seatunnel_image" not in json.dumps(_bundle().spec)


def test_tool_without_image_is_rejected_before_any_artifact():
    """无可用镜像 → 在 build 就失败，绝不产出注定 pull 404 的 DAG。

    这正是线上那次失败的形态：DataX 的 ``ontometa/datax:latest`` 是占位名，
    DAG 照样生成、照样触发，直到 Airflow 任务里报
    ``pull access denied for ontometa/datax`` 才暴露。
    """
    with pytest.raises(SyncImageUnavailableError) as exc:
        _bundle(tool="datax")
    # 报错要说清怎么修，而不是只说「不可用」
    assert "SYNC_TOOL_IMAGES" in str(exc.value)


def test_image_override_is_deployment_fact_not_adapter_default():
    """部署方的镜像覆盖优先于适配器默认值（私有 registry / 自建 tag）。"""
    spec = _bundle(image_overrides={"seatunnel": "registry.internal/seatunnel:2.3.11"}).spec
    assert spec["tool_image"] == "registry.internal/seatunnel:2.3.11"
    assert all(t["image"] == "registry.internal/seatunnel:2.3.11" for t in spec["tasks"])


def test_jobs_mount_follows_the_tool():
    """作业配置挂到各工具自己的目录，产物里不出现别的工具的路径。"""
    assert _bundle().spec["jobs_mount"] == "/opt/seatunnel/jobs"
    dx = _bundle(tool="datax", image_overrides={"datax": "acme/datax:3.0"}).spec
    assert dx["jobs_mount"] == "/opt/datax/jobs"
    assert all(t["config_file"].startswith("/opt/datax/jobs/") for t in dx["tasks"])
    # 显式给的挂载点仍然优先（部署方的挂载约定说了算）
    assert _bundle(jobs_mount="/mnt/jobs").spec["jobs_mount"] == "/mnt/jobs"


def test_build_is_idempotent():
    a, b = _bundle(), _bundle()
    assert a.dag_source == b.dag_source
    assert json.dumps(a.spec, sort_keys=True) == json.dumps(b.spec, sort_keys=True)
    assert json.dumps(a.job_files, sort_keys=True) == json.dumps(b.job_files, sort_keys=True)


def test_write_persists_three_artifacts(tmp_path):
    bundle = _bundle()
    dags_dir, jobs_dir = tmp_path / "dags", tmp_path / "jobs"
    written = bundle.write(str(dags_dir), str(jobs_dir))

    assert (dags_dir / bundle.dag_filename).exists()
    assert (dags_dir / bundle.spec_filename).exists()
    assert len(list(jobs_dir.iterdir())) == len(bundle.job_files)
    assert written["dag"].endswith(".py")
    # 边车 JSON 是 DAG 的输入，两者必须同名对应
    spec = json.loads((dags_dir / bundle.spec_filename).read_text(encoding="utf-8"))
    assert spec["dag_id"] == bundle.dag_id
    assert bundle.spec_filename in bundle.dag_source


# ---------- runner 通道（M14） ----------


def _runner_bundle(**kwargs):
    plan = JobPlan(jobs=(_job("sync_dim_customer"), _job("sync_dwd_order", "dwd", "orders")))
    return _builder.build(
        ontology_id="11112222-3333-4444-5555-666677778888",
        plan=plan,
        ddl_statements={
            "dim.customer": "CREATE TABLE `dim`.`customer` (id INT);",
            "dwd.orders": "CREATE TABLE `dwd`.`orders` (id INT);",
        },
        channel="runner",
        runner_endpoint="http://sync-runner:8088/",
        engine="hive",
        **kwargs,
    )


def test_runner_dag_is_valid_python_and_uses_python_operator():
    bundle = _runner_bundle()
    ast.parse(bundle.dag_source)  # 语法错误会让 Airflow 整个目录 import error
    assert "PythonOperator" in bundle.dag_source
    # runner 通道不该出现 docker 通道的任何痕迹
    assert "DockerOperator" not in bundle.dag_source
    assert "docker.sock" not in bundle.dag_source
    assert "_JOB_MOUNTS" not in bundle.dag_source


def test_runner_spec_carries_channel_endpoint_and_wire_jobspec():
    spec = _runner_bundle().spec
    assert spec["sync_channel"] == "runner"
    # 末尾斜杠被规整掉
    assert spec["runner_endpoint"] == "http://sync-runner:8088"
    task = spec["tasks"][0]
    # 每个任务带 JobSpec 的线格式（随请求体发给 runner），凭据不在内
    assert task["job_spec"]["source"]["alias"] == "erp_readonly"
    assert task["job_spec"]["target"]["platform"] == "hive"
    assert task["job_spec"]["columns"] == [
        {"source": "cust_id", "target": "customer_id"}
    ]


def test_runner_spec_drops_docker_only_fields():
    spec = _runner_bundle().spec
    for gone in ("jobs_host_dir", "driver_jars", "docker_network", "tool_image", "jobs_mount"):
        assert gone not in spec, gone
    # runner 通道不产作业配置文件（作业声明随请求体走）
    assert _runner_bundle().job_files == {}


def test_runner_dag_has_no_credentials_or_host_paths():
    bundle = _runner_bundle()
    blob = bundle.dag_source + json.dumps(bundle.spec, ensure_ascii=False)
    for leaked in ("password=", "jdbc:", "root@", "/var/run/docker.sock", "/host/"):
        assert leaked not in blob


def test_runner_keeps_create_tables_sql_task():
    """建表仍是 SQL 任务，本体 DDL 那条铁律不因通道改变。"""
    bundle = _runner_bundle()
    assert "SQLExecuteQueryOperator" in bundle.dag_source
    assert bundle.spec["ddl_targets"] == ["dim.customer", "dwd.orders"]


def test_runner_preserves_lineage_inlets_outlets():
    """M11 血缘与通道无关：inlets/outlets 与 docker 通道同口径。"""
    from app.connectors.datahub import build_dataset_urn

    bundle = _runner_bundle(
        target_urn_builder=lambda job: build_dataset_urn(
            job.target.platform, job.target.qualified, "PROD"
        )
    )
    task = bundle.spec["tasks"][0]
    assert task["inlets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.tab_customer,PROD)"
    ]
    assert task["outlets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:hive,dim.customer,PROD)"
    ]


def test_runner_build_is_idempotent():
    a, b = _runner_bundle(), _runner_bundle()
    assert a.dag_source == b.dag_source
    assert json.dumps(a.spec, sort_keys=True) == json.dumps(b.spec, sort_keys=True)


# ---------- 图结构：真的执行一遍建图逻辑（M15 staging + 层间串联） ----------


class _FakeOp:
    """桩 Operator：只记 task_id 与上游，支持 ``>>``（右侧可为单个或列表）。

    登记在类级 ``created`` 上，好让 docker 通道那条 ``class _SyncOperator(DockerOperator)``
    的继承路径也一样能被收集到（它不经工厂函数）。
    """

    created: list = []

    def __init__(self, **kwargs):
        self.task_id = kwargs.get("task_id")
        self.kwargs = kwargs
        self.upstream: list[str] = []
        _FakeOp.created.append(self)

    def __rshift__(self, other):
        for op in other if isinstance(other, list) else [other]:
            op.upstream.append(self.task_id)
        return other


def _build_graph(bundle, tmp_path):
    """用桩模块把生成的 DAG 源码**执行**一遍，返回 {task_id: 上游 task_id 列表}。

    ``ast.parse`` 只看语法。层间怎么串、swap 挂在哪，只有真的跑一遍建图逻辑才验得出来
    ——``previous >> group`` 在第二层起是 ``list >> list``，语法完全合法、执行必 TypeError，
    整个 DAG 文件在 Airflow 解析期导入失败。这条测试就是为了钉住这类。
    """
    import sys
    import types

    dags_dir = tmp_path / "dags"
    bundle.write(str(dags_dir), str(tmp_path / "jobs"))

    _FakeOp.created = []

    def _module(name: str, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        return mod

    class _DagCtx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    stubs = {
        "pendulum": _module(
            "pendulum", datetime=lambda *a, **k: None, duration=lambda **k: None
        ),
        "airflow": _module("airflow", DAG=_DagCtx),
        "airflow.exceptions": _module("airflow.exceptions", AirflowException=RuntimeError),
        "airflow.operators": _module("airflow.operators"),
        "airflow.operators.python": _module("airflow.operators.python", PythonOperator=_FakeOp),
        "airflow.providers": _module("airflow.providers"),
        "airflow.providers.common": _module("airflow.providers.common"),
        "airflow.providers.common.sql": _module("airflow.providers.common.sql"),
        "airflow.providers.common.sql.operators": _module("airflow.providers.common.sql.operators"),
        "airflow.providers.common.sql.operators.sql": _module(
            "airflow.providers.common.sql.operators.sql", SQLExecuteQueryOperator=_FakeOp
        ),
        # docker 通道用：DockerOperator 被 _SyncOperator 继承，故必须是个类。
        "airflow.providers.docker": _module("airflow.providers.docker"),
        "airflow.providers.docker.operators": _module("airflow.providers.docker.operators"),
        "airflow.providers.docker.operators.docker": _module(
            "airflow.providers.docker.operators.docker", DockerOperator=_FakeOp
        ),
        "docker": _module("docker"),
        "docker.types": _module("docker.types", Mount=lambda **kw: kw),
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        exec(
            compile(bundle.dag_source, str(dags_dir / bundle.dag_filename), "exec"),
            {"__file__": str(dags_dir / bundle.dag_filename), "__name__": "gen_dag"},
        )
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return {op.task_id: op.upstream for op in _FakeOp.created}, list(_FakeOp.created)


def test_runner_dag_graph_chains_layers_without_list_shift(tmp_path):
    """层间串联必须真的建得起来：第二层起 previous 是 list，写成 previous >> group 会
    在 Airflow 解析期 TypeError，整个 DAG 目录 import error。"""
    graph, _ = _build_graph(_runner_bundle(), tmp_path)
    # dim 层接 create_tables；dwd 层接 dim 层该表链条的末端（有 staging 时是 swap）
    assert graph["sync_dim_customer"] == ["create_tables"]
    assert graph["sync_dwd_order"] == ["swap_sync_dim_customer"]


def test_full_load_writes_staging_and_swaps(tmp_path):
    """M15 接进运行时：全量搬进 staging，成功后由 swap 任务切到正式表。"""
    bundle = _runner_bundle()
    spec = bundle.spec
    task = next(t for t in spec["tasks"] if t["task_id"] == "sync_dim_customer")
    # 搬运写的是 staging 表，展示/血缘用的仍是正式表
    assert task["job_spec"]["target"]["table"] == "customer__stg_manual"
    assert task["target"] == "dim.customer"
    assert task["write_target"] == "dim.customer__stg_manual"
    # staging 建表跟在正式表 DDL 之后（CREATE ... LIKE 要求正式表已存在）
    assert any("customer__stg_manual" in s for s in spec["staging_ddl"])
    assert '_SPEC["ddl"] + (_SPEC.get("staging_ddl") or [])' in bundle.dag_source

    graph, _ = _build_graph(bundle, tmp_path)
    # swap 只接自己那张表的搬运任务：单表失败不会连累别的表，也不会切一张没搬完的表
    assert graph["swap_sync_dim_customer"] == ["sync_dim_customer"]


def test_staging_name_is_stable_per_batch(tmp_path):
    """staging 名用批次后缀而非 run_id：每次失败的运行不会各留一张不会回收的临时表。"""
    a = _runner_bundle(dag_id_suffix="c1a2b3c4_b0")
    b = _runner_bundle(dag_id_suffix="c1a2b3c4_b0")
    assert a.spec["staging_ddl"] == b.spec["staging_ddl"]
    assert any("customer__stg_c1a2b3c4_b0" in s for s in a.spec["staging_ddl"])


def test_incremental_load_skips_staging(tmp_path):
    """增量是往正式表追加，搬进 staging 再整表切换会把存量换没——必须不走 staging。"""
    from dataclasses import replace as _replace

    job = _replace(_job("sync_dim_customer"), mode="incremental", partition_key="created_at")
    bundle = _builder.build(
        ontology_id="1111",
        plan=JobPlan(jobs=(job,)),
        ddl_statements={"dim.customer": "CREATE TABLE `dim`.`customer` (id INT);"},
        channel="runner",
        runner_endpoint="http://sync-runner:8088",
        engine="hive",
    )
    task = bundle.spec["tasks"][0]
    assert task["job_spec"]["target"]["table"] == "customer"  # 直接写正式表
    assert task["swap"] == []
    assert bundle.spec["staging_ddl"] == []
    graph, _ = _build_graph(bundle, tmp_path)
    assert "swap_sync_dim_customer" not in graph


def test_staging_can_be_disabled(tmp_path):
    """⚠ 各引擎切换的原子性需真实实例核实（§8.3），故留一键退回。"""
    bundle = _runner_bundle(staging=False)
    task = bundle.spec["tasks"][0]
    assert task["job_spec"]["target"]["table"] == "customer"
    assert task["swap"] == []
    assert bundle.spec["staging_ddl"] == []


def test_docker_channel_also_stages_and_swaps(tmp_path):
    """docker 通道同样走 staging：SeaTunnel 的 DROP_DATA 是先删后写，失败即丢数据。"""
    bundle = _bundle()
    task = next(t for t in bundle.spec["tasks"] if t["task_id"] == "sync_dim_customer")
    assert task["write_target"] == "dim.customer__stg_manual"
    # 作业配置里的 sink 指向 staging 表，而不是正式表
    conf = bundle.job_files[f"{bundle.dag_id}__sync_dim_customer.json"]
    assert conf["sink"][0]["table_name"] == "dim.customer__stg_manual"


def test_docker_dag_graph_chains_layers_without_list_shift(tmp_path):
    """docker 通道的层间串联有同一个 list >> list 问题，同样钉住（它是 runner 的回退路径）。"""
    graph, _ = _build_graph(_bundle(), tmp_path)
    assert graph["sync_dim_customer"] == ["create_tables"]
    assert graph["sync_dwd_order"] == ["swap_sync_dim_customer"]
    assert graph["swap_sync_dim_customer"] == ["sync_dim_customer"]
