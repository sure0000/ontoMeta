"""物化编排 materialization_runner.run：总是交 Airflow 编排（不再有直连落库）。

验证：产出 DAG + 搬运作业并触发一次运行、按勾选/覆盖裁剪与重命名、触发失败不丢产物、
前置校验（无数据源/无 dsn/未配 Airflow）报错。生成器按 hive 引擎产出真实 DDL。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.models.data_app import DataSource
from app.services import data_app_executor, materialization_runner

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp_ods.{table},PROD)"


@pytest.fixture(autouse=True)
def _init_db(client):
    """拉起 session 级 client 以建表（runner 测试直接用 SessionLocal，不走 API）。

    ``airflow_settings`` 是单例行，用例改了会漏给后续用例（默认执行方式随之改变），
    故每个用例结束后复位成「未配置」。
    """
    yield client
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db, {"enabled": False}
        )


def _seed(tag: str, *, with_dsn: bool = True) -> dict:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mr-{tag}", name=f"mr-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(ontology)
        db.flush()

        customer = ObjectType(
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            table_role="business_object",
            source_ref=_URN.format(table="tab_customer"),
        )
        order = ObjectType(
            ontology_id=ontology.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
            source_ref=_URN.format(table="tab_order"),
        )
        db.add_all([customer, order])
        db.flush()
        # 有列才有可生成的 ETL SELECT；created_at 同时作为增量装载的分区键候选。
        db.add_all(
            [
                Property(
                    object_type_id=customer.id,
                    name="customer_id",
                    display_name="客户ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
                Property(
                    object_type_id=customer.id,
                    name="created_at",
                    display_name="创建时间",
                    data_type="timestamp",
                    semantic_type="datetime",
                ),
                Property(
                    object_type_id=order.id,
                    name="sales_order_id",
                    display_name="订单ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
            ]
        )
        db.add(
            RelationType(
                ontology_id=ontology.id,
                name="places",
                display_name="下单",
                source_object_type_id=customer.id,
                target_object_type_id=order.id,
                structure_type="fact_table",
            )
        )
        ds = DataSource(
            name=f"target-{tag}",
            kind="doris",
            dsn_secret_ref="sqlite:///unused-stubbed" if with_dsn else None,
        )
        db.add(ds)
        db.commit()
        return {"ontology_id": ontology.id, "datasource_id": ds.id}


class _Recorder:
    """替身 execute_write：记录每次调用，按需模拟成功/失败。"""

    def __init__(self, fail_first: bool = False):
        self.calls: list[list[str]] = []
        self.fail_first = fail_first

    def __call__(self, *, dsn, statements, mapping=None, timeout_seconds=60):
        self.calls.append(list(statements))
        failed = self.fail_first and len(self.calls) == 1
        per = [
            {
                "index": i,
                "ok": not failed,
                **({"error": "boom"} if failed else {}),
            }
            for i in range(len(statements))
        ]
        return {
            "total": len(statements),
            "executed": 0 if failed else len(statements),
            "failed": len(statements) if failed else 0,
            "error": "boom" if failed else None,
            "per_statement": per,
        }


def test_selected_targets_filters_tables(tmp_path, monkeypatch):
    ids = _seed("filter")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            selected_targets=["customer"],
            artifact_id="art-filter",
        )
    # 只物化 customer 一张表
    assert receipt["tables"] == [t for t in receipt["tables"] if t.endswith(".customer")]
    assert len(receipt["tables"]) == 1


def test_database_and_table_overrides_rename_targets(tmp_path, monkeypatch):
    """人工指定的目标库/表名要贯穿到产物（建表 DDL 与搬运作业同一目标）。"""
    ids = _seed("rename")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        service = materialization_runner._contract_service
        service.sync(db, ids["ontology_id"])
        contracts = service.list_contracts(db, ids["ontology_id"])
        names = service.resolve_target_names(db, contracts)
        customer_contract = next(
            c for c in contracts if names.get(c.target_id, (None,))[0] == "customer"
        )
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            database_overrides={"dim": "warehouse_prod"},
            table_overrides={customer_contract.id: "dim_customer"},
            selected_targets=["customer"],
            artifact_id="art-rename",
        )

    # 勾选按实体名，改名后仍要命中（不能因为改名把自己裁掉）
    assert receipt["tables"] == ["warehouse_prod.dim_customer"]
    # 建表 DDL 落盘到 DAG spec，重命名后的表名在其中
    import json as _json

    spec_path = next((tmp_path / "dags").glob("*.json"))
    spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    assert any("warehouse_prod" in s and "dim_customer" in s for s in spec["ddl"])


def _enable_airflow(tmp_path, monkeypatch, *, triggered: dict):
    """把 Airflow 配成可用，并把 REST 调用换成记录器（不需要真实 Airflow）。

    投递目录不再入设置，改由 config 环境变量给默认：直接 monkeypatch 成 tmp 路径。
    """
    from app.config import settings as env_settings
    from app.services.settings_service import SettingsService

    monkeypatch.setattr(env_settings, "airflow_dags_dir", str(tmp_path / "dags"))
    monkeypatch.setattr(env_settings, "airflow_jobs_dir", str(tmp_path / "jobs"))
    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db,
            {
                "endpoint": "http://airflow:8080",
                "enabled": True,
            },
        )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def unpause_dag(self, dag_id):
            triggered["unpaused"] = dag_id

        def trigger_dag(self, dag_id, *, dag_run_id, conf=None):
            triggered["dag_id"] = dag_id
            triggered["run_id"] = dag_run_id
            return {"dag_run_id": dag_run_id, "state": "queued"}

        def run_url(self, dag_id, run_id):
            return f"http://airflow:8080/dags/{dag_id}/grid?dag_run_id={run_id}"

        def close(self):
            triggered["closed"] = True

    monkeypatch.setattr(materialization_runner, "AirflowClient", _FakeClient)


def test_orchestrated_mode_writes_dag_and_triggers(tmp_path, monkeypatch):
    """默认路径：产出 DAG + 作业配置并触发一次运行，**不在本进程里落库**。"""
    ids = _seed("orch")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered)

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-1",
        )

    # 关键：没有任何直连写库
    assert rec.calls == []
    assert receipt["execute_mode"] == "orchestrated"
    assert receipt["ok"] is True
    assert receipt["state"] == "queued"
    # run_id 取制品 id → 重复提交在 Airflow 侧因 run_id 冲突而幂等
    assert receipt["dag_run_id"] == "ontometa__artifact-1"
    assert triggered["run_id"] == receipt["dag_run_id"]
    assert triggered["unpaused"] == receipt["dag_id"]

    # 产物真的落盘了：DAG + 边车 JSON + 每个作业一个配置
    dags = sorted(p.name for p in (tmp_path / "dags").iterdir())
    jobs = sorted(p.name for p in (tmp_path / "jobs").iterdir())
    assert any(n.endswith(".py") for n in dags) and any(n.endswith(".json") for n in dags)
    assert len(jobs) == len(receipt["jobs"]) >= 1


def test_orchestrated_reports_trigger_failure_without_losing_artifacts(tmp_path, monkeypatch):
    """触发失败时产物已落盘：回执要如实说「产物已出、触发失败」，不能让人以为什么都没发生。"""
    ids = _seed("orchfail")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered)

    def _boom(self, dag_id, *, dag_run_id, conf=None):
        raise materialization_runner.AirflowError("trigger_dag", "HTTP 404 dag not found")

    monkeypatch.setattr(materialization_runner.AirflowClient, "trigger_dag", _boom)

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-2",
        )

    assert receipt["ok"] is False
    assert "404" in receipt["error"]
    assert receipt["state"] == "failed"
    assert list((tmp_path / "dags").iterdir())  # 产物仍在，可人工排查后重试


def test_errors_when_airflow_unavailable(monkeypatch):
    """未配可用 Airflow 时直接报错——不再静默回退到直连落库。"""
    ids = _seed("unset")
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="Airflow"):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )


def test_missing_datasource_raises(monkeypatch):
    ids = _seed("nods")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id="does-not-exist",
                engine="hive",
            )


def test_datasource_without_dsn_raises(monkeypatch):
    ids = _seed("nodsn", with_dsn=False)
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )
