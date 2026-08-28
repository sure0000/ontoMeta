"""按表裁剪的增量建模：只对新表跑 LLM，而不是为几张表重扫整个域。

钉住三件事：
1. 证据裁剪是过滤而非重建，裁剪后的包与全域包同形；
2. 裁剪范围随任务行落库并被重试继承——生成跑在独立子进程，参数只能走库；
3. 未建模表清单排除已建模 / 人工删除 / 平台自建落点三类，尤其是最后一类——
   把自己造的 ODS 表当新表再建一遍，正是本方案要根除的重复。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.models import (
    DataSource,
    DomainContext,
    DraftGenerationTask,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.schemas import (
    EvidenceBundle,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
    RelationEvidencePack,
)
from app.services.draft_task_service import _loads_list, _progress_of
from app.services.evidence_builder import scope_evidence


def _urn(table: str, platform: str = "mysql") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},erp.{table},PROD)"


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        object_types=[
            ObjectTypeEvidencePack(
                candidate_name="customer",
                display_name="客户",
                source_dataset_urn=_urn("customer"),
            ),
            ObjectTypeEvidencePack(
                candidate_name="order",
                display_name="订单",
                source_dataset_urn=_urn("order"),
            ),
            ObjectTypeEvidencePack(
                candidate_name="invoice",
                display_name="发票",
                source_dataset_urn=_urn("invoice"),
            ),
        ],
        properties=[
            PropertyEvidencePack(
                object_candidate_name="customer", field_name="id", display_name="ID"
            ),
            PropertyEvidencePack(
                object_candidate_name="order", field_name="id", display_name="ID"
            ),
            PropertyEvidencePack(
                object_candidate_name="invoice", field_name="id", display_name="ID"
            ),
        ],
        relations=[
            RelationEvidencePack(
                name="customer_order",
                display_name="客户下单",
                source_object="customer",
                target_object="order",
            ),
            RelationEvidencePack(
                name="order_invoice",
                display_name="订单开票",
                source_object="order",
                target_object="invoice",
            ),
        ],
    )


# --- 证据裁剪 ---------------------------------------------------------------


def test_empty_scope_returns_bundle_unchanged():
    """不传范围就是全域——增量入口不改变默认行为。"""
    evidence = _bundle()
    assert scope_evidence(evidence, None) is evidence
    assert scope_evidence(evidence, []) is evidence


def test_scope_keeps_only_selected_datasets_and_their_properties():
    scoped = scope_evidence(_bundle(), [_urn("order")])
    assert [o.candidate_name for o in scoped.object_types] == ["order"]
    assert [p.object_candidate_name for p in scoped.properties] == ["order"]


def test_scope_drops_relations_with_an_end_outside_the_subset():
    """只留半条边会让关系指向一个本次并不生成的对象。"""
    scoped = scope_evidence(_bundle(), [_urn("customer"), _urn("order")])
    assert [r.name for r in scoped.relations] == ["customer_order"]


def test_scope_keeps_relations_fully_inside_the_subset():
    scoped = scope_evidence(_bundle(), [_urn("order"), _urn("invoice")])
    assert [r.name for r in scoped.relations] == ["order_invoice"]


def test_unknown_urn_scopes_to_nothing():
    """选中的表不在证据里 → 空包。运行时据此报错，不静默生成 0 个对象。"""
    scoped = scope_evidence(_bundle(), [_urn("does_not_exist")])
    assert scoped.object_types == []


# --- 裁剪范围随任务行落库 ----------------------------------------------------


def test_loads_list_treats_broken_json_as_no_scope():
    """范围读坏就退回全域：多生成不丢数据，少生成才会让人以为某张表建过了。"""
    assert _loads_list(None) == []
    assert _loads_list("not json") == []
    assert _loads_list('{"a": 1}') == []
    assert _loads_list('["a", "b"]') == ["a", "b"]


def test_progress_reports_scoped_table_count():
    task = DraftGenerationTask(
        id="t1",
        domain_context_id="d1",
        scope="objects",
        status="queued",
        progress=0,
        dataset_urns_json=json.dumps([_urn("a"), _urn("b")]),
    )
    assert _progress_of(task).scoped_table_count == 2

    task.dataset_urns_json = None
    assert _progress_of(task).scoped_table_count == 0


# --- 未建模表清单 ------------------------------------------------------------


@pytest.fixture
def scope_seed(db):
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:scope-{token}", name=f"scope-{token}"
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="draft", version=1)
    db.add(ontology)
    db.flush()
    modeled = ObjectType(
        ontology_id=ontology.id,
        name=f"customer_{token}",
        display_name="客户",
        source_ref=_urn("customer"),
    )
    deleted = ObjectType(
        ontology_id=ontology.id,
        name=f"legacy_{token}",
        display_name="历史表",
        source_ref=_urn("legacy"),
        deleted_by_user=True,
    )
    db.add_all([modeled, deleted])
    doris = DataSource(
        name=f"Doris-{token}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.commit()
    return domain, ontology, modeled, doris


def _fake_bundle(domain, tables: list[tuple[str, str]]):
    """构造一个只带 urn/name 的 DataHubDomainBundle 替身。"""
    from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput

    return DataHubDomainBundle(
        domain=DomainInput(id=domain.datahub_domain_id, name=domain.name),
        datasets=[
            DatasetInput(urn=_urn(t, platform), name=f"erp.{t}", platform=platform)
            for t, platform in tables
        ],
    )


def _run_list(db, domain, tables, monkeypatch):
    """同步跑一次清单：本仓没有 pytest-asyncio，异步用例一律 asyncio.run。"""
    from app.services import unmodeled_tables as mod

    async def _fake_fetch(_db, _domain):
        return _fake_bundle(domain, tables)

    monkeypatch.setattr(mod, "fetch_domain_bundle", _fake_fetch)
    # 证据组装与缓存回填不是本用例的被测对象，且要真实 DataHub 字段才跑得动。
    monkeypatch.setattr(mod.EvidenceBuilder, "build", lambda self, b, **kw: EvidenceBundle())
    monkeypatch.setattr(mod.draft_evidence_cache, "save", lambda *a, **kw: None)
    return asyncio.run(mod.list_unmodeled_tables(db, domain.id))


def test_unmodeled_excludes_already_modeled_tables(db, scope_seed, monkeypatch):
    domain, _ontology, _modeled, _doris = scope_seed
    items, total = _run_list(
        db, domain, [("customer", "mysql"), ("shipment", "mysql")], monkeypatch
    )
    assert total == 2
    assert [t.name for t in items] == ["erp.shipment"]


def test_unmodeled_excludes_user_deleted_tables(db, scope_seed, monkeypatch):
    """人工删过的对象机器不会复活，列出来点了不会有反应。"""
    domain, _ontology, _modeled, _doris = scope_seed
    items, _total = _run_list(
        db, domain, [("legacy", "mysql"), ("shipment", "mysql")], monkeypatch
    )
    assert [t.name for t in items] == ["erp.shipment"]


def test_unmodeled_excludes_platform_created_landing_tables(
    db, scope_seed, monkeypatch
):
    """平台自己建的 ODS 表被采集回同一个域时必须挡掉。

    不挡就会给它生成一个对象——而那个对象正是它自己的投影源，凭空多出一份重复。
    """
    domain, ontology, modeled, doris = scope_seed
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=modeled.id,
            ods_database="erp",
            ods_table="ods_erp_customer",
            sync_status="ready",
        )
    )
    db.commit()

    items, _total = _run_list(
        db, domain, [("ods_erp_customer", "doris"), ("shipment", "mysql")], monkeypatch
    )
    assert [t.name for t in items] == ["erp.shipment"]


# --- 起草入口：范围落库、不清缓存、重试继承 -----------------------------------


@pytest.fixture
def workspace_service(monkeypatch):
    """绕开 LLM 就绪检查：本组用例测的是范围如何落库，不是 LLM。"""
    from app.services.query import WorkspaceService

    service = WorkspaceService()
    monkeypatch.setattr(type(service), "_ensure_llm_ready", lambda self, db: None)
    return service


def test_scoped_start_persists_the_selection(db, scope_seed, workspace_service):
    """范围必须落到任务行上：生成跑在独立子进程，只拿 task_id 回查。"""
    domain, *_ = scope_seed
    urns = [_urn("shipment"), _urn("payment")]

    progress = workspace_service.start_object_generation(db, domain.id, dataset_urns=urns)

    assert progress.scope == "objects"
    assert progress.scoped_table_count == 2
    task = db.get(DraftGenerationTask, progress.task_id)
    assert json.loads(task.dataset_urns_json) == urns
    assert "2 张表" in (task.message or "")


def test_scoped_start_keeps_evidence_cache_and_checkpoints(
    db, scope_seed, workspace_service, monkeypatch
):
    """裁剪生成不清缓存/检查点。

    这些 urn 正是未建模表清单刚从 DataHub 拉回来的那批（清单接口顺手回填了缓存）；
    清掉只会让每挑几张表就重付一次分钟级抓取，还会抹掉一次全域生成的续跑点。
    """
    domain, *_ = scope_seed
    calls: list[str] = []
    monkeypatch.setattr(
        type(workspace_service),
        "_reset_evidence_for_fresh_run",
        staticmethod(lambda domain_id: calls.append(domain_id)),
    )

    workspace_service.start_object_generation(db, domain.id, dataset_urns=[_urn("a")])
    assert calls == []


def test_full_start_still_resets_evidence(db, scope_seed, workspace_service, monkeypatch):
    """不带范围就是全域生成：照旧清缓存重抓，行为与历史一致。"""
    domain, *_ = scope_seed
    calls: list[str] = []
    monkeypatch.setattr(
        type(workspace_service),
        "_reset_evidence_for_fresh_run",
        staticmethod(lambda domain_id: calls.append(domain_id)),
    )

    progress = workspace_service.start_object_generation(db, domain.id)
    assert calls == [domain.id]
    assert progress.scoped_table_count == 0
    assert db.get(DraftGenerationTask, progress.task_id).dataset_urns_json is None


def test_empty_list_is_full_domain_not_an_empty_scope(
    db, scope_seed, workspace_service, monkeypatch
):
    """前端「仅生成业务对象」按钮发的就是 ``dataset_urns: []``。

    空数组必须与不传等价（全域），否则那个按钮会变成「一张表都不生成」。
    """
    domain, *_ = scope_seed
    calls: list[str] = []
    monkeypatch.setattr(
        type(workspace_service),
        "_reset_evidence_for_fresh_run",
        staticmethod(lambda domain_id: calls.append(domain_id)),
    )

    progress = workspace_service.start_object_generation(db, domain.id, dataset_urns=[])

    assert calls == [domain.id]
    assert progress.scoped_table_count == 0
    assert db.get(DraftGenerationTask, progress.task_id).dataset_urns_json is None


def test_retry_inherits_the_scope(db, scope_seed, workspace_service):
    """丢了范围，重试一次就从「只生成 3 张表」悄悄变成全域重扫。"""
    domain, *_ = scope_seed
    urns = [_urn("shipment")]
    first = workspace_service.start_object_generation(db, domain.id, dataset_urns=urns)
    failed = db.get(DraftGenerationTask, first.task_id)
    failed.status = "failed"
    failed.error_summary = "boom"
    db.commit()

    retried = workspace_service.retry_draft_generation(db, domain.id, first.task_id)

    assert retried.scoped_table_count == 1
    assert json.loads(
        db.get(DraftGenerationTask, retried.task_id).dataset_urns_json
    ) == urns
