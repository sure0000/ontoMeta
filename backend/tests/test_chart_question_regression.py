"""客户分组柱状图那次事故的回归用例。

一个问题（「按上级客户分组统计下级数量，画个柱状图」）连问四次都没能给出正确实现，
SQL 本身每次都写对了，卡住的全是 SQL 周围的闸门。本文件钉住其中三条：

1. 跨域会话里，一个没建仓的域会连坐所有查询（``owning_ontology_ids``）；
2. 不选域时按对象取数的工具只问字母序第一个本体（``_ontology_owning``）；
3. 就绪结论陈旧时，被拦的那条路径自己去对账（``reconcile_blocking_runs``）。

F4 把系统自己的状态断言当幻觉那条，在 test_formal_grounding.py 里钉。
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import pytest

from app.models import (
    DataSource,
    DomainContext,
    GovernanceArtifact,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.agent_grounding import FactLedger
from app.services.query_routing import owning_ontology_ids, readiness_error


@pytest.fixture
def landscape(db):
    """两个已发布本体：``rich`` 建了仓且对象可查，``bare`` 什么都没有。

    还原事故现场：会话没选域 → 两个本体都在场 → 只碰 rich 的 SQL 也被判未就绪。
    """
    token = uuid4().hex[:8]
    doris = DataSource(
        id=f"doris-{token}",
        name=f"Doris-{token}",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030/ods",
    )
    made: dict[str, object] = {"doris": doris, "token": token}
    db.add(doris)

    for label in ("rich", "bare"):
        domain = DomainContext(
            id=f"domain-{label}-{token}",
            datahub_domain_id=f"urn:li:domain:{label}{token}",
            name=f"{label}-{token}",
        )
        ontology = Ontology(
            id=f"ontology-{label}-{token}",
            domain_context_id=domain.id,
            status="published",
            version=1,
        )
        db.add_all([domain, ontology])
        made[f"{label}_domain"] = domain
        made[f"{label}_ontology"] = ontology

    obj = ObjectType(
        id=f"object-{token}",
        ontology_id=f"ontology-rich-{token}",
        name="customer_group",
        display_name="客户分组",
        status="published",
    )
    other = ObjectType(
        id=f"object-bare-{token}",
        ontology_id=f"ontology-bare-{token}",
        name="golden_probe",
        display_name="金标探针",
        status="published",
    )
    db.add_all([obj, other])
    db.flush()

    deployment = OntologyWarehouseDeployment(
        id=f"deploy-{token}",
        ontology_id=f"ontology-rich-{token}",
        ontology_version=1,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    projection = WarehouseObjectProjection(
        id=f"proj-{token}",
        deployment_id=deployment.id,
        object_type_id=obj.id,
        ods_database="ods",
        ods_table="ods_erpnext_tab_customer_group",
        serving_layer="ods",
        serving_database="ods",
        serving_table="ods_erpnext_tab_customer_group",
        schema_status="ready",
        sync_status="ready",
        transform_status="not_required",
        queryable=True,
    )
    db.add(projection)
    db.commit()
    made["object"] = obj
    made["projection"] = projection
    yield made

    db.query(WarehouseObjectProjection).filter(
        WarehouseObjectProjection.deployment_id == deployment.id
    ).delete(synchronize_session=False)
    db.query(IngestionContract).filter(
        IngestionContract.object_type_id == obj.id
    ).delete(synchronize_session=False)
    db.query(OntologyWarehouseDeployment).filter(
        OntologyWarehouseDeployment.id == deployment.id
    ).delete(synchronize_session=False)
    for label in ("rich", "bare"):
        db.query(ObjectType).filter(
            ObjectType.ontology_id == f"ontology-{label}-{token}"
        ).delete(synchronize_session=False)
        db.query(Ontology).filter(
            Ontology.id == f"ontology-{label}-{token}"
        ).delete(synchronize_session=False)
        db.query(DomainContext).filter(
            DomainContext.id == f"domain-{label}-{token}"
        ).delete(synchronize_session=False)
    db.query(DataSource).filter(DataSource.id == doris.id).delete(
        synchronize_session=False
    )
    db.commit()


def test_unbuilt_ontology_in_scope_does_not_block_the_query(db, landscape):
    """只碰 rich 的 SQL，不该因为在场的 bare 没建仓而被判未就绪。"""
    scope = [
        landscape["bare_ontology"].id,  # 字母序/锚点位，事故里就是它排在前面
        landscape["rich_ontology"].id,
    ]
    assert (
        readiness_error(
            db,
            datasource=landscape["doris"],
            ontology_ids=scope,
            object_names=["customer_group"],
        )
        is None
    )


def test_scope_narrows_to_the_ontologies_actually_referenced(db, landscape):
    scope = [landscape["bare_ontology"].id, landscape["rich_ontology"].id]
    assert owning_ontology_ids(
        db, ontology_ids=scope, object_names=["customer_group"]
    ) == [landscape["rich_ontology"].id]


def test_unresolvable_object_keeps_full_scope(db, landscape):
    """解析不出归属时不静默放行：维持原作用域，由下游报「对象未覆盖」。"""
    scope = [landscape["bare_ontology"].id, landscape["rich_ontology"].id]
    assert (
        owning_ontology_ids(db, ontology_ids=scope, object_names=["nope"]) == scope
    )
    assert readiness_error(
        db,
        datasource=landscape["doris"],
        ontology_ids=scope,
        object_names=["nope"],
    )


def test_object_tool_anchors_on_the_owning_ontology(db, landscape):
    """按对象取数的工具要落到真正拥有该对象的在场本体，而不是锚点本体。"""
    from app.services.chat_bi import ChatBiService

    scope = [landscape["bare_ontology"].id, landscape["rich_ontology"].id]
    anchor = scope[0]
    obj = landscape["object"]

    assert (
        ChatBiService._ontology_owning(db, scope, obj.id, anchor)
        == landscape["rich_ontology"].id
    )
    assert (
        ChatBiService._ontology_owning(db, scope, "customer_group", anchor)
        == landscape["rich_ontology"].id
    )
    # 认不出的 token 退回锚点，不猜。
    assert ChatBiService._ontology_owning(db, scope, "unknown_thing", anchor) == anchor


def test_in_flight_sync_is_reconciled_before_refusing(db, landscape, monkeypatch):
    """就绪结论陈旧时先对账再重判：一次早已跑完的同步不该把表锁在不可查上。"""
    from app.services import query_readiness

    obj = landscape["object"]
    projection = landscape["projection"]
    contract = IngestionContract(
        id=f"contract-{landscape['token']}",
        ontology_id=obj.ontology_id,
        ontology_version=1,
        object_type_id=obj.id,
        source_datasource_id=landscape["doris"].id,
        source_physical_table="erp.tab_customer_group",
        doris_datasource_id=landscape["doris"].id,
        target_ods_database="ods",
        target_ods_table="ods_erpnext_tab_customer_group",
        mode="full",
        status="running",  # 中间态：镜像时把 queryable 压成 False
    )
    projection.queryable = False
    db.add(contract)
    artifact = GovernanceArtifact(
        id=f"artifact-{landscape['token']}",
        kind="sync",
        name="同步 · 客户分组 → 数仓 ODS",
        ontology_id=obj.ontology_id,
        status="executing",
        execution_receipt_json=json.dumps(
            {
                "execute_mode": "orchestrated",
                "ingestion_contract_id": contract.id,
                "state": "queued",  # 回执冻在提交那一刻——事故现场就是这个样子
            }
        ),
        executed_at=datetime(2026, 8, 30, 4, 8, 2),
    )
    db.add(artifact)
    db.commit()

    assert readiness_error(
        db,
        datasource=landscape["doris"],
        ontology_ids=[obj.ontology_id],
        object_names=["customer_group"],
    )
    assert query_readiness.blocking_contracts(
        db, ontology_ids=[obj.ontology_id], object_names=["customer_group"]
    ) == [contract]
    # 契约↔制品靠回执里的 ingestion_contract_id 串起来
    assert query_readiness._sync_artifact_of(db, contract.id).id == artifact.id

    # 对账把契约推平（真实路径是问 Airflow；这里替身只演「运行其实早已结束」）。
    def _settled(_db, artifact_id):  # noqa: ANN001
        assert artifact_id == artifact.id
        artifact.status = "succeeded"
        contract.status = "ready"
        projection.queryable = True
        _db.commit()
        return artifact

    monkeypatch.setattr("app.api.deps.agent_pipeline.get", _settled, raising=False)
    assert query_readiness.reconcile_blocking_runs(
        db, ontology_ids=[obj.ontology_id], object_names=["customer_group"]
    )
    assert (
        readiness_error(
            db,
            datasource=landscape["doris"],
            ontology_ids=[obj.ontology_id],
            object_names=["customer_group"],
        )
        is None
    )

    db.query(GovernanceArtifact).filter(
        GovernanceArtifact.id == artifact.id
    ).delete(synchronize_session=False)
    db.commit()


def test_readiness_detail_names_the_blocking_run(db, landscape):
    """拒答要说清卡在哪——空话一句是模型空转重试三次的直接原因。"""
    from app.services import query_readiness

    obj = landscape["object"]
    db.add(
        IngestionContract(
            id=f"contract-detail-{landscape['token']}",
            ontology_id=obj.ontology_id,
            ontology_version=1,
            object_type_id=obj.id,
            source_datasource_id=landscape["doris"].id,
            source_physical_table="erp.tab_customer_group",
            doris_datasource_id=landscape["doris"].id,
            target_ods_database="ods",
            target_ods_table="ods_erpnext_tab_customer_group",
            mode="full",
            status="running",
        )
    )
    db.commit()

    detail = query_readiness.readiness_detail(
        db, ontology_ids=[obj.ontology_id], object_names=["customer_group"]
    )
    assert "客户分组" in detail
    assert "从未成功落数" in detail


def test_unexecuted_run_sql_step_is_not_green():
    """没跑成的 run_sql 不能显示成 succeeded——绿勾配一句「未就绪」看起来像查成功了。

    同时钉住另一半：它**仍要入账**（``is_error`` 保持 False），否则模型如实解释
    「这张表暂时查不了」时会因账本里没有这些表名被 F4 判成幻觉。
    """
    from app.services.chat_bi import ChatBiService

    blocked = {"executed": False, "reason": "未就绪", "proved": {"tables": ["customer_group"]}}
    assert ChatBiService._tool_did_not_act("run_sql", blocked)
    assert not ChatBiService._tool_did_not_act(
        "run_sql", {"executed": True, "rows": [{"n": 1}]}
    )
    assert not ChatBiService._tool_did_not_act("search_objects", {"items": []})

    led = FactLedger()
    ChatBiService._ledger_register(led, "run_sql", blocked, False)
    assert led.has_entity_named("customer_group")
