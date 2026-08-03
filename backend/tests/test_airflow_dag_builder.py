"""DAG 生成：结构、幂等、凭据、以及「建表不交给搬运工具」这条铁律。

DAG 源码在这里只做**语法与结构**校验（ast 解析 + 关键调用检查）；用 Airflow 的 DagBag
真正解析属集成验证，需要装 airflow 包，见 M10 的本地验证步骤。
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
    assert "ONTOMETA_SYNC_TOOL_IMAGES" in str(exc.value)


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
