"""物化编排 materialization_runner.run 的编排逻辑（execute_write 被替身，不需活集群）。

真实落库由 test_data_app_executor_write.py 覆盖；这里只验证 runner 如何 组织生成、
按勾选裁剪、DDL 失败时跳过 ETL、前置校验报错、把回执按表归位。生成器按 hive 引擎
产出真实 DDL，故也顺带验证"契约 → 生成 → 待执行语句"这条链路通。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, RelationType
from app.models.data_app import DataSource
from app.services import data_app_executor, materialization_runner


@pytest.fixture(autouse=True)
def _init_db(client):
    """拉起 session 级 client 以建表（runner 测试直接用 SessionLocal，不走 API）。"""
    return client


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
        )
        order = ObjectType(
            ontology_id=ontology.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
        )
        db.add_all([customer, order])
        db.flush()
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
