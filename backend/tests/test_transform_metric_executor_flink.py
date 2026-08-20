"""P1-4/P1-5：transform/metric executor 集成测试。

验收：有 datasource+SqlRunner JAR 时返回 dag_run_id/run_url；无配置时退回「仅产出」。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.executors.transform import TransformExecutor
from app.agents.executors.metric import MetricExecutor
from app.database import SessionLocal
from app.models import DataSource
from tests.support.delivery import make_runner_jar


def _make_datasource(ds_id: str = "ds-123") -> None:
    """建一个目标数据源（幂等：已存在则跳过）。"""
    with SessionLocal() as db:
        if db.get(DataSource, ds_id):
            return
        db.add(DataSource(id=ds_id, name="warehouse", kind="hive", dsn_secret_ref="hive://..."))
        db.commit()


@pytest.fixture
def transform_spec():
    return {
        "ontology_id": "onto-123",
        "target_table": "customer",
        "engine": "hive",
        "database_prefix": "erp",
        "execution_mode": "batch",
        "cleansing_rules": [],
    }


@pytest.fixture
def metric_spec():
    return {
        "ontology_id": "onto-123",
        "metric_name": "gmv",
        "engine": "hive",
        "database_prefix": "erp",
        "target_layer": "ads",
        "execution_mode": "batch",
        "subject_objects": ["sales_order"],
        "group_by": [],
        "expression": "SUM(amount)",
    }


def test_transform_without_datasource_returns_handoff(transform_spec):
    """未配 target_datasource_id 退回「仅产出」（不报错）。"""
    executor = TransformExecutor()
    with patch.object(executor, "_artifacts") as mock_artifacts:
        mock_artifacts.return_value = {"engine": "hive", "sql": "SELECT 1;", "target_table": "customer"}
        receipt = executor.execute(transform_spec, context={})
    assert receipt["handoff"] == "DolphinScheduler"
    assert "未配置 target_datasource_id" in receipt["note"]


def test_metric_without_datasource_returns_handoff(metric_spec):
    """metric 同上。"""
    executor = MetricExecutor()
    with patch.object(executor, "_artifacts") as mock_artifacts:
        mock_artifacts.return_value = {"engine": "hive", "ddl": "CREATE TABLE ads_gmv (...);", "sql": "INSERT ..."}
        receipt = executor.execute(metric_spec, context={})
    assert receipt["handoff"] == "DolphinScheduler"
    assert "未配置 target_datasource_id" in receipt["note"]


def test_transform_with_datasource_but_no_runner_jar_returns_handoff(transform_spec):
    """有 datasource 但无 SqlRunner JAR 时，run_flink_sql 内部退回「仅产出」。"""
    _make_datasource("ds-123")

    with patch("app.services.flink_job_runner._settings") as settings:
        settings.get_airflow_runtime.return_value = MagicMock(
            available=True, flink_sql_runner_jar=""  # 未配 JAR
        )
        with patch("app.agents.executors.transform._generator.build_flink_etl_input") as mock_input:
                mock_input.return_value = {
                    "source_table": MagicMock(name="src_customer", columns=()),
                    "target_table": MagicMock(name="customer", qualified_name="dim_erp.customer", columns=()),
                    "source_physical": "erp_ods.tab_customer",
                    "target_physical": "dim_erp.customer",
                    "source_platform": "hive",
                    "target_platform": "hive",
                    "select_body": "SELECT `cust_id` AS `customer_id` FROM `src_customer`",
                }
                executor = TransformExecutor()
                receipt = executor.execute(
                    {**transform_spec, "target_datasource_id": "ds-123"},
                    context={"artifact_id": "artifact-456"},
                )

    assert receipt["execute_mode"] == "handoff"
    assert "未配置 Flink SqlRunner JAR" in receipt["note"]


def test_transform_with_full_config_triggers_flink(transform_spec, tmp_path):
    """有 datasource+SqlRunner JAR+Airflow 时，返回 dag_run_id/run_url（Airflow mock 解析到 DAG）。"""
    _make_datasource("ds-123")

    with patch("app.services.flink_job_runner._settings") as settings:
            airflow = MagicMock(
                available=True,
                dags_dir=str(tmp_path / "dags"),
                endpoint="http://airflow",
                max_active_tasks_per_dag=16,
                dag_parse_timeout=0.1,
                # Flink 执行参数现来自 Airflow 运行期配置（DB）。jar 是 ontoMeta 侧
                # 真实路径（随包分发），指向 /opt 的占位路径现在会被 jar 读取报错。
                flink_sql_runner_jar=make_runner_jar(tmp_path),
                flink_sql_runner_class="com.ontometa.flink.SqlRunner",
                flink_bin="flink",
                flink_deploy_target="yarn-per-job",
                flink_parallelism=1,
                flink_yarn_queue="",
                flink_checkpoint_dir="",
            )
            settings.get_airflow_runtime.return_value = airflow
            with patch("app.services.flink_job_runner.AirflowClient") as client_cls:
                client = MagicMock()
                client_cls.return_value = client
                client.dag_exists.return_value = True
                client.trigger_dag.return_value = {"state": "queued"}
                client.run_url.return_value = "http://airflow/dags/x/grid"

                with patch("app.agents.executors.transform._generator.build_flink_etl_input") as mock_input:
                    mock_input.return_value = {
                        "source_table": MagicMock(name="src_customer", columns=()),
                        "target_table": MagicMock(name="customer", qualified_name="dim_erp.customer", columns=()),
                        "source_physical": "erp_ods.tab_customer",
                        "target_physical": "dim_erp.customer",
                        "source_platform": "hive",
                        "target_platform": "hive",
                        "select_body": "SELECT `cust_id` AS `customer_id` FROM `src_customer`",
                    }
                    executor = TransformExecutor()
                    receipt = executor.execute(
                        {**transform_spec, "target_datasource_id": "ds-123"},
                        context={"artifact_id": "artifact-456"},
                    )

    assert receipt["execute_mode"] == "flink_on_yarn"
    assert receipt["dag_run_id"] == "ontometa__artifact-456"
    assert receipt["state"] == "queued"
    assert "http://airflow" in receipt["run_url"]


def test_metric_source_table_comes_from_ontology_not_hardcoded_dwd(monkeypatch):
    """主对象的源表按本体+契约解析，不写死 dwd_ 前缀。

    此前拼的是 f"dwd_{prefix}.{entity}"：prefix 为空就得到 `dwd_.brand` 这种根本不存在
    的库名，且写死 dwd 层（对象可能物化在 dim/dws），列还照抄了结果表。
    """
    from app.agents.executors.metric import MetricExecutor
    from app.warehouse import LogicalColumn, LogicalTable

    subject = LogicalTable(
        name="brand", database="dim", layer="dim", entity_name="brand",
        columns=(LogicalColumn("brand_id", "bigint", "identifier"),
                 LogicalColumn("amount", "decimal", "amount")),
    )
    monkeypatch.setattr(
        MetricExecutor, "_subject_table", staticmethod(lambda db, spec, entity: subject)
    )
    captured: dict = {}
    import app.agents.executors.metric as mod

    monkeypatch.setattr(
        mod, "generate_flink_sql", lambda **kw: captured.update(kw) or "-- sql --"
    )
    import app.services.flink_job_runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "run_flink_sql", lambda *a, **k: {"execute_mode": "handoff"}
    )
    with patch.object(mod, "SessionLocal") as session_cls:
        db = MagicMock()
        # 口径没有形式化 AST → 走 _build_sql 老路，本用例只关心源表怎么解析
        db.get.return_value = MagicMock(id="ds1", name="dw", expression_json=None)
        session_cls.return_value.__enter__.return_value = db
        MetricExecutor().execute(
            {"metric_name": "gmv", "business_logic_id": "bl1", "engine": "hive",
             "subject_objects": ["brand"], "target_datasource_id": "ds1",
             "expression": "SUM(amount)"},
            {},
        )
    assert captured["source_physical"] == "dim.brand"
    assert "dwd_." not in captured["source_physical"]
    # 源表的列来自主对象，不是结果表的 stat_date/metric_value
    assert [c.name for c in captured["source_table"].columns] == ["brand_id", "amount"]
