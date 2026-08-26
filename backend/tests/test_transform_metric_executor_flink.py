"""Doris-native transform execution and remaining metric compatibility tests."""

from __future__ import annotations

import re

import inspect
from unittest.mock import MagicMock, patch

from app.agents.executors.transform import TransformExecutor
from app.services.doris_sql_dag_builder import build_doris_sql_dag
from tests.support.delivery import LocalTransportDelivery


def test_transform_without_datasource_returns_doris_handoff():
    executor = TransformExecutor()
    with patch.object(executor, "_artifacts") as artifacts:
        artifacts.return_value = {
            "engine": "doris",
            "compute_engine": "doris",
            "source_tables": ["ods.customer"],
            "target_table": "dim.customer",
            "target_logical_table": MagicMock(),
            "sql": "INSERT OVERWRITE TABLE `dim`.`customer` SELECT * FROM `ods`.`customer`;",
            "applied_rules": [],
            "unapplied_rules": [],
            "rule_notes": [],
        }
        receipt = executor.execute({"ontology_id": "o", "target_table": "customer"}, {})
    assert receipt["execute_mode"] == "handoff"
    assert receipt["compute_engine"] == "doris"
    assert "Doris SQL" in receipt["note"]


def test_transform_module_has_no_flink_runner_import():
    import app.agents.executors.transform as module

    source = inspect.getsource(module)
    assert "flink_job_runner" not in source
    assert "FlinkSqlTask" not in source
    assert "generate_flink_sql" not in source


def test_doris_sql_dag_has_quality_gate_and_no_bash_operator():
    bundle = build_doris_sql_dag(
        artifact_id="artifact-1",
        kind="transform",
        conn_id="ontometa_doris_x_etl",
        setup_sql=["CREATE TABLE staging LIKE target"],
        execute_sql=["INSERT INTO staging SELECT * FROM ods.customer"],
        quality_sql=["SELECT COUNT(*) >= 0 FROM staging"],
        publish_sql=["ALTER TABLE target REPLACE WITH TABLE staging"],
    )
    assert "SQLCheckOperator" in bundle.dag_source
    assert "BashOperator" not in bundle.dag_source
    assert bundle.spec["execute_sql"] == ["INSERT INTO staging SELECT * FROM ods.customer"]
    assert bundle.spec["publish_sql"]


def test_doris_job_runner_delivers_and_triggers(tmp_path):
    from app.services import doris_job_runner as runner

    airflow = MagicMock(
        available=True,
        dags_dir=str(tmp_path / "dags"),
        endpoint="http://airflow",
        username=None,
        password=None,
        dag_parse_timeout=0.1,
    )
    airflow.build_delivery.return_value = LocalTransportDelivery()
    with patch.object(runner._settings, "get_airflow_runtime", return_value=airflow), patch.object(
        runner, "AirflowClient"
    ) as client_cls:
        client = MagicMock()
        client_cls.return_value = client
        client.dag_exists.return_value = True
        client.trigger_dag.return_value = {"state": "queued"}
        client.run_url.return_value = "http://airflow/dag"
        receipt = runner.run_doris_sql(
            MagicMock(),
            artifact_id="artifact-1",
            kind="transform",
            conn_id="ontometa_doris_x_etl",
            execute_sql=["INSERT INTO target SELECT * FROM source"],
            source_tables=["ods.customer"],
            target_tables=["dim.customer"],
        )
    assert receipt["compute_engine"] == "doris"
    assert receipt["execute_mode"] == "orchestrated"
    # 带本次提交时刻：同一个制品重跑得到新 run_id，不撞 Airflow 的 409
    assert re.fullmatch(r"ontometa__artifact-1__\d{8}T\d{6}Z", receipt["dag_run_id"])


def test_transform_reconciliation_opens_projection_only_after_success(db):
    import uuid
    from app.models import (
        DataSource, DomainContext, ObjectType, Ontology,
        OntologyWarehouseDeployment, WarehouseObjectProjection,
    )
    from app.services.transform_reconciliation import reconcile_transform_receipt

    token = uuid.uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:tr-{token}", name=f"tr-{token}")
    db.add(domain); db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=3)
    db.add(ontology); db.flush()
    obj = ObjectType(ontology_id=ontology.id, name="customer", display_name="客户")
    ds = DataSource(name="Doris", kind="doris", purpose="warehouse", dsn_secret_ref="mysql://d")
    db.add_all([obj, ds]); db.flush()
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id, ontology_version=3, doris_datasource_id=ds.id,
        status="schema_ready",
    )
    db.add(deployment); db.flush()
    projection = WarehouseObjectProjection(
        deployment_id=deployment.id, object_type_id=obj.id,
        schema_status="ready", sync_status="ready", transform_status="running",
        queryable=False,
    )
    db.add(projection); db.commit()
    receipt = {
        "compute_engine": "doris", "ontology_id": ontology.id,
        "ontology_version": 3, "datasource_id": ds.id, "object_type_id": obj.id,
    }
    reconcile_transform_receipt(db, receipt=receipt, airflow_state="success")
    db.refresh(projection)
    assert projection.transform_status == "ready"
    assert projection.queryable is True


def test_metric_reconciliation_opens_ads_only_after_success(db):
    import uuid
    from app.models import (
        BusinessLogic, DataSource, DomainContext, Ontology,
        OntologyWarehouseDeployment, WarehouseLogicProjection,
    )
    from app.services.metric_reconciliation import reconcile_metric_receipt

    token = uuid.uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:metric-{token}", name=f"metric-{token}")
    db.add(domain); db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=4)
    db.add(ontology); db.flush()
    logic = BusinessLogic(
        ontology_id=ontology.id, name="gmv", display_name="GMV",
        logic_type="metric", status="published", expression_json="{}",
    )
    ds = DataSource(name="Doris", kind="doris", purpose="warehouse", dsn_secret_ref="mysql://d")
    db.add_all([logic, ds]); db.flush()
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id, ontology_version=4,
        doris_datasource_id=ds.id, status="ready",
    )
    db.add(deployment); db.flush()
    projection = WarehouseLogicProjection(
        deployment_id=deployment.id, business_logic_id=logic.id,
        serving_database="ads", serving_table="gmv", status="running",
    )
    db.add(projection); db.commit()
    receipt = {"compute_engine": "doris", "logic_projection_id": projection.id}
    reconcile_metric_receipt(db, receipt=receipt, airflow_state="success")
    db.refresh(projection)
    assert projection.status == "ready"
    assert projection.queryable is True


def test_metric_executor_has_no_flink_dependency():
    import inspect
    import app.agents.executors.metric as module

    source = inspect.getsource(module)
    assert "flink_job_runner" not in source
    assert "generate_flink_sql" not in source
    assert "FlinkSqlTask" not in source
