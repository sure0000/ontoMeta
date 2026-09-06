from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import (
    DataSource,
    DomainContext,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.sync_reconciliation import reconcile_sync_receipt


def _contract(db) -> IngestionContract:
    token = uuid4().hex
    domain = DomainContext(
        id=f"domain-{token}",
        datahub_domain_id=f"urn:li:domain:{token}",
        name="sales",
    )
    ontology = Ontology(
        id=f"ontology-{token}",
        domain_context_id=domain.id,
        status="published",
        version=1,
    )
    obj = ObjectType(
        id=f"object-{token}",
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        status="published",
        source_ref="urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)",
    )
    datasource = DataSource(
        id=f"doris-{token}",
        name="默认 Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@doris/ods",
    )
    # 源侧也要是一个**配好连接**的数据源：落数核对要回源表数一次行数，
    # 源连不上时走的是「未能比对」分支，测不到真正的比对逻辑。
    source = DataSource(
        id=f"source-{token}",
        name="ERP 源库",
        kind="mysql",
        purpose="business_source",
        is_default_warehouse=False,
        dsn_secret_ref="mysql+pymysql://reader@erp/erp",
    )
    contract = IngestionContract(
        id=f"contract-{token}",
        ontology_id=f"ontology-{token}",
        ontology_version=1,
        object_type_id=f"object-{token}",
        source_datasource_id=f"source-{token}",
        source_physical_table="erp.customer",
        doris_datasource_id=datasource.id,
        target_ods_database="ods",
        target_ods_table="ods_sales_customer",
        mode="full",
        status="submitted",
    )
    db.add_all([domain, ontology, obj, datasource, source, contract])
    db.commit()
    return contract


def _cleanup(db, contract: IngestionContract) -> None:
    datasource_id = contract.doris_datasource_id
    ontology_id = contract.ontology_id
    object_id = contract.object_type_id
    ontology = db.get(Ontology, ontology_id)
    domain_id = ontology.domain_context_id if ontology else None
    db.query(WarehouseObjectProjection).filter(
        WarehouseObjectProjection.object_type_id == object_id
    ).delete(synchronize_session=False)
    db.query(OntologyWarehouseDeployment).filter(
        OntologyWarehouseDeployment.ontology_id == ontology_id
    ).delete(synchronize_session=False)
    db.delete(contract)
    db.flush()
    obj = db.get(ObjectType, object_id)
    if obj is not None:
        db.delete(obj)
    if ontology is not None:
        db.delete(ontology)
    if domain_id:
        domain = db.get(DomainContext, domain_id)
        if domain is not None:
            db.delete(domain)
    for ds_id in (datasource_id, contract.source_datasource_id):
        datasource = db.get(DataSource, ds_id)
        if datasource is not None:
            db.delete(datasource)
    db.commit()


@pytest.fixture
def contract(db):
    """建契约并**无论用例成败都清理**。

    改之前 ``_cleanup`` 写在每条用例末尾，一条断言失败就会把「默认 Doris」留在库里，
    下一条建同名默认仓时撞 ``is_default_warehouse`` 唯一约束——真正的失败原因被一条
    IntegrityError 盖住。
    """
    item = _contract(db)
    try:
        yield item
    finally:
        _cleanup(db, item)


def _counts(*, source: int | None, target: int):
    """按 SQL 里出现的表名分派行数，模拟源库与 Doris 两次 COUNT(*)。

    ``source=None`` 表示源库连不上（抛错），用来走「未能比对」分支。
    """
    def _fake(**kwargs):
        sql = kwargs.get("sql", "")
        if "erp" in sql and "customer" in sql and "ods_sales_customer" not in sql:
            if source is None:
                raise RuntimeError("源库连接被拒绝")
            value = source
        else:
            value = target
        return ([{"key": "row_count", "title": "row_count"}], [{"row_count": value}])

    return _fake


def _patch_counts(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "app.services.sync_reconciliation.execute_sql", _counts(**kwargs)
    )


def _reconcile(db, contract, state="success"):
    return reconcile_sync_receipt(
        db, receipt={"ingestion_contract_id": contract.id}, airflow_state=state
    )


def test_row_counts_matching_the_source_verify_clean(db, contract, monkeypatch):
    """源与目标都是 42 行 —— 这才是唯一无保留的成功。"""
    _patch_counts(monkeypatch, source=42, target=42)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is True
    assert evidence["status"] == "verified"
    assert evidence["row_count"] == 42
    assert evidence["source_row_count"] == 42
    assert evidence["comparison"] == "match"
    assert evidence["caveat"] is None
    assert "error" not in evidence

    db.refresh(contract)
    assert contract.status == "ready"
    assert contract.last_success_at is not None

    projection = (
        db.query(WarehouseObjectProjection)
        .filter(WarehouseObjectProjection.object_type_id == contract.object_type_id)
        .one()
    )
    assert projection.ods_database == "ods"
    assert projection.ods_table == "ods_sales_customer"
    assert projection.queryable is True

    from app.services.query_routing import projection_mapping

    mapping = projection_mapping(
        db,
        datasource=db.get(DataSource, contract.doris_datasource_id),
        ontology_ids=[contract.ontology_id],
        object_names=["customer"],
    )
    assert mapping["tables"] == {"customer": "ods.ods_sales_customer"}


def test_partial_load_is_a_failure_not_a_green_check(db, contract, monkeypatch):
    """源 500 行、目标 50 行 —— 改之前这是绿的。

    目标表此刻装着一份不完整的数据，而下游（Projection / Data Agent）会把它当成这个
    对象的全量。判成功比判失败危险得多：错误答案没有任何标记。
    """
    _patch_counts(monkeypatch, source=500, target=50)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is False
    assert evidence["status"] == "failed"
    assert evidence["comparison"] == "mismatch"
    # 报错要把两个数字都给出来，否则用户还得自己去两边数
    assert "500" in evidence["error"] and "50" in evidence["error"]

    db.refresh(contract)
    assert contract.status == "failed"


def test_zero_rows_with_an_empty_source_passes_but_says_so(db, contract, monkeypatch):
    """源也是 0 行：确实没有可搬的数据，判通过——但必须说出「这张表里没数据」。"""
    _patch_counts(monkeypatch, source=0, target=0)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is True
    assert evidence["empty"] is True
    assert evidence["comparison"] == "match"
    assert evidence["caveat"], "0 行必须带说明，不能只给一个绿勾"
    assert "没有数据" in evidence["caveat"]

    db.refresh(contract)
    assert contract.status == "ready"


def test_zero_rows_without_a_source_count_is_flagged(db, contract, monkeypatch):
    """目标 0 行且源数不到 —— 分不清「源本来就空」还是「这次没搬动」，必须点明。

    这正是审计里那两条实例（库存结账、代码表目录）当时的处境：界面显示
    「Doris 落数验证通过 · 共 0 行」，没有任何线索说明它意味着什么。
    """
    _patch_counts(monkeypatch, source=None, target=0)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is True
    assert evidence["empty"] is True
    assert evidence["comparison"] == "unavailable"
    assert "无法确认" in evidence["caveat"]


def test_unreachable_source_does_not_fail_a_good_sync(db, contract, monkeypatch):
    """数不到源不是失败：数据确实搬进去了，只是这次没核对成。"""
    _patch_counts(monkeypatch, source=None, target=42)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is True
    assert evidence["comparison"] == "unavailable"
    assert evidence["source_row_count"] is None
    assert "没有做行数核对" in evidence["caveat"]
    assert evidence["comparison_note"]

    db.refresh(contract)
    assert contract.status == "ready"


def test_incremental_mode_skips_the_row_count_comparison(db, contract, monkeypatch):
    """增量模式下目标表跨多次运行累积，目标 > 源是正常的。

    拿全量计数去比只会制造假失败——这里源 10 目标 999，必须仍判通过。
    """
    contract.mode = "incremental"
    db.commit()
    _patch_counts(monkeypatch, source=10, target=999)

    evidence = _reconcile(db, contract)

    assert evidence["verified"] is True
    assert evidence["comparison"] == "skipped_incremental"
    assert evidence["source_row_count"] is None
    assert "增量" in evidence["caveat"]


def test_airflow_failure_still_reported_as_failure(db, contract, monkeypatch):
    _patch_counts(monkeypatch, source=42, target=42)

    evidence = _reconcile(db, contract, state="failed")

    assert evidence["verified"] is False
    assert evidence["status"] == "failed"
    db.refresh(contract)
    assert contract.status == "failed"
