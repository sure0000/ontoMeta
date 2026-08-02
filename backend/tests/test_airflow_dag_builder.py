"""DAG 生成：结构、幂等、凭据、以及「建表不交给搬运工具」这条铁律。

DAG 源码在这里只做**语法与结构**校验（ast 解析 + 关键调用检查）；用 Airflow 的 DagBag
真正解析属集成验证，需要装 airflow 包，见 M10 的本地验证步骤。
"""

from __future__ import annotations

import ast
import json

from app.services.airflow_dag_builder import AirflowDagBuilder, dag_id_for
from app.warehouse.jobs import ColumnMapping, JobEndpoint, JobPlan, JobSpec

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
    # datax
    dx = _bundle(tool="datax").spec
    assert dx["tool"] == "datax"
    assert dx["tasks"][0]["image"] == "ontometa/datax:latest"
    assert "datax.py" in " ".join(dx["tasks"][0]["command"])
    # flink
    fl = _bundle(tool="flink").spec
    assert fl["tool"] == "flink"
    assert fl["tasks"][0]["image"] == "apache/flink:1.18"
    # DAG 骨架不硬编镜像：读 task 的 image/command
    assert 'image=task["image"]' in _bundle().dag_source
    assert "seatunnel_image" not in json.dumps(_bundle().spec)


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
