"""物化编排 materialization_runner.run 的编排逻辑（execute_write 被替身，不需活集群）。

真实落库由 test_data_app_executor_write.py 覆盖；这里只验证 runner 如何 组织生成、
按勾选裁剪、DDL 失败时跳过 ETL、前置校验报错、把回执按表归位。生成器按 hive 引擎
产出真实 DDL，故也顺带验证"契约 → 生成 → 待执行语句"这条链路通。
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
            db, {"enabled": False, "dags_dir": "", "jobs_dir": ""}
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


def test_run_generates_and_executes_ddl_then_etl(monkeypatch):
    ids = _seed("full")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
        )

    # 两阶段都被调用（DDL 成功 → ETL 继续）
    assert len(rec.calls) == 2
    # DDL 阶段真的生成了建表语句（至少 customer/sales_order 两张业务对象维表）
    assert receipt["ddl"]["total"] >= 2
    assert receipt["ok"] is True
    # 回执把逐条结果归位到 qualified 表名
    assert all("target" in ps for ps in receipt["ddl"]["per_statement"])
    assert any(ps["target"].endswith(".customer") for ps in receipt["ddl"]["per_statement"])


def test_selected_targets_filters_tables(monkeypatch):
    ids = _seed("filter")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            selected_targets=["customer"],
        )
    # 只物化 customer 一张表
    assert receipt["ddl"]["total"] == 1
    assert receipt["tables"] == [t for t in receipt["tables"] if t.endswith(".customer")]


def test_database_and_table_overrides_rename_targets(monkeypatch):
    """人工指定的目标库/表名要贯穿 DDL 与 ETL——两边名字必须一致，否则装载会落到空表。"""
    ids = _seed("rename")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)

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
        )

    # 勾选按实体名，改名后仍要命中（不能因为改名把自己裁掉）
    assert receipt["tables"] == ["warehouse_prod.dim_customer"]
    assert receipt["ddl"]["total"] == 1
    assert "`warehouse_prod`.`dim_customer`" in rec.calls[0][0]
    # ETL 若有语句，必须指向同一张表（否则装载会落到另一张空表）
    assert all(t.startswith("warehouse_prod.") for t in receipt["etl"]["targets"])


def test_etl_follows_each_contract_load_strategy(monkeypatch):
    """同步方式逐实体设：物化按各表契约的策略生成装载语句，而非一刀切全量。"""
    ids = _seed("perentity")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)

    with SessionLocal() as db:
        service = materialization_runner._contract_service
        service.sync(db, ids["ontology_id"])
        contracts = service.list_contracts(db, ids["ontology_id"])
        names = service.resolve_target_names(db, contracts)
        by_name = {names.get(c.target_id, (None,))[0]: c for c in contracts}
        # customer 走增量（按分区键追加），sales_order 保持全量覆盖
        service.update(
            db,
            by_name["customer"].id,
            {"load_strategy": "incremental", "partition_key": "created_at"},
        )
        service.update(db, by_name["sales_order"].id, {"load_strategy": "full"})
        materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
        )

    etl_sql = "\n".join(rec.calls[1])
    assert "INSERT INTO TABLE dim.customer" in etl_sql
    assert "INSERT OVERWRITE TABLE dim.sales_order" in etl_sql


def test_ddl_failure_skips_etl(monkeypatch):
    ids = _seed("ddlfail")
    rec = _Recorder(fail_first=True)
    monkeypatch.setattr(data_app_executor, "execute_write", rec)

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
        )
    # ETL 未被调用（只有一次 DDL 调用）
    assert len(rec.calls) == 1
    assert receipt["etl"].get("skipped") is True
    assert receipt["ok"] is False


def _enable_airflow(tmp_path, monkeypatch, *, triggered: dict):
    """把 Airflow 配成可用，并把 REST 调用换成记录器（不需要真实 Airflow）。"""
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db,
            {
                "endpoint": "http://airflow:8080",
                "dags_dir": str(tmp_path / "dags"),
                "jobs_dir": str(tmp_path / "jobs"),
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


def test_orchestrated_requires_configured_airflow(monkeypatch):
    """显式要求编排却没配 Airflow —— 报错说清怎么办，不静默回落到直连落库。"""
    ids = _seed("orchunset")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="Airflow"):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
                execute_mode="orchestrated",
            )


def test_defaults_to_direct_when_airflow_unavailable(monkeypatch):
    """没有 Airflow 的开发机保持既有行为：直连落库，链路照样跑通。"""
    ids = _seed("fallback")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)
    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
        )
    assert receipt["execute_mode"] == "direct"
    assert len(rec.calls) == 2  # DDL + ETL 都直连执行了


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
