"""Regression tests for the shared query-path optimizations."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import event

from app.database import engine
from app.models import (
    BusinessLogic,
    ChatBiConversation,
    ChatBiMessage,
    DomainContext,
    GovernanceArtifact,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.chat_bi import ChatBiService


def _count_sql(callback):
    statements: list[str] = []

    def before_cursor_execute(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = callback()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return result, statements


def test_resolve_scope_batches_domain_and_ontology_queries(db):
    suffix = uuid4().hex
    domains = [
        DomainContext(
            id=f"domain-{suffix}-{index}",
            datahub_domain_id=f"urn:test:{suffix}:{index}",
            name=f"域 {index}",
        )
        for index in range(3)
    ]
    ontologies = [
        Ontology(
            id=f"ontology-{suffix}-{index}",
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
        )
        for index, domain in enumerate(domains)
        if index != 1
    ]
    domain_ids = [domain.id for domain in domains]
    db.add_all([*domains, *ontologies])
    db.commit()

    service = ChatBiService()
    (resolved_domains, resolved_ontologies), statements = _count_sql(
        lambda: service._resolve_scope(db, [domain_ids[2], domain_ids[0], domain_ids[2]])
    )

    assert [domain.id for domain in resolved_domains] == [domain_ids[2], domain_ids[0]]
    assert [ontology.domain_context_id for ontology in resolved_ontologies] == [
        domain_ids[2],
        domain_ids[0],
    ]
    assert len(statements) == 2


def test_list_conversations_returns_latest_preview_without_loading_history(db):
    suffix = uuid4().hex
    conversation = ChatBiConversation(id=f"conversation-{suffix}", title="优化测试")
    now = datetime.utcnow()
    messages = [
        ChatBiMessage(
            id=f"message-old-{suffix}",
            conversation_id=conversation.id,
            role="user",
            content="旧消息",
            created_at=now - timedelta(days=1),
        ),
        ChatBiMessage(
            id=f"message-new-{suffix}",
            conversation_id=conversation.id,
            role="assistant",
            content="最新消息" * 40,
            created_at=now,
        ),
    ]
    db.add_all([conversation, *messages])
    db.commit()

    result, statements = _count_sql(lambda: ChatBiService().list_conversations(db, []))
    row = next(item for item in result if item["id"] == conversation.id)

    assert row["message_count"] == 2
    assert row["last_message_preview"] == "最新消息" * 25
    # conversation list + grouped count + window-ranked preview
    assert len(statements) == 3


def test_load_merged_knowledge_batches_each_entity_collection(db):
    suffix = uuid4().hex
    ontologies = [
        Ontology(
            id=f"knowledge-ontology-{suffix}-{index}",
            domain_context_id=None,
            status=OntologyStatus.PUBLISHED.value,
        )
        for index in range(2)
    ]
    # SQLite enforces the ontology foreign key only when explicitly enabled in
    # the application, so use real domains to keep this fixture portable.
    domains = [
        DomainContext(
            id=f"knowledge-domain-{suffix}-{index}",
            datahub_domain_id=f"urn:knowledge:{suffix}:{index}",
            name=f"知识域 {index}",
        )
        for index in range(2)
    ]
    for ontology, domain in zip(ontologies, domains, strict=False):
        ontology.domain_context_id = domain.id
    objects = [
        ObjectType(
            id=f"knowledge-object-{suffix}-{index}",
            ontology_id=ontology.id,
            name=f"object_{index}",
            display_name=f"对象 {index}",
        )
        for index, ontology in enumerate(ontologies)
    ]
    properties = [
        Property(
            id=f"knowledge-property-{suffix}-{index}",
            object_type_id=object.id,
            name="id",
            display_name="标识",
            required=True,
        )
        for index, object in enumerate(objects)
    ]
    relations = [
        RelationType(
            id=f"knowledge-relation-{suffix}",
            ontology_id=ontologies[0].id,
            name="relates_to",
            display_name="关联",
            source_object_type_id=objects[0].id,
            target_object_type_id=objects[1].id,
        )
    ]
    logics = [
        BusinessLogic(
            id=f"knowledge-logic-{suffix}-{index}",
            ontology_id=ontology.id,
            name=f"logic_{index}",
            display_name=f"口径 {index}",
            logic_type="metric",
        )
        for index, ontology in enumerate(ontologies)
    ]
    ontology_ids = [ontology.id for ontology in ontologies]
    db.add_all([*domains, *ontologies, *objects, *properties, *relations, *logics])
    db.commit()

    (snapshots, loaded_relations, loaded_logics), statements = _count_sql(
        lambda: ChatBiService()._load_merged_knowledge(db, ontology_ids)
    )

    assert [snapshot.display_name for snapshot in snapshots] == ["对象 0", "对象 1"]
    assert len(loaded_relations) == 1
    assert [logic.display_name for logic in loaded_logics] == ["口径 0", "口径 1"]
    assert len(statements) == 3


def test_task_list_supports_a_lightweight_non_reconciling_view(
    client,
    admin_headers,
    db,
    monkeypatch,
):
    from app.api.deps import agent_pipeline

    artifact = GovernanceArtifact(
        kind="sync",
        name=f"summary-{uuid4().hex}",
        status="succeeded",
        spec_json='{"large":"spec"}',
        validation_report_json='{"large":"validation"}',
        execution_receipt_json='{"large":"receipt"}',
    )
    db.add(artifact)
    db.commit()
    artifact_id = artifact.id

    observed: dict[str, object] = {}
    original = agent_pipeline.list_artifacts

    def spy(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_pipeline, "list_artifacts", spy)
    response = client.get("/api/agents/artifacts", headers=admin_headers)

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == artifact_id)
    assert observed["reconcile"] is False
    assert row["spec"] == {}
    assert row["validation_report"] is None
    assert row["execution_receipt"] is None

    observed_get: dict[str, object] = {}
    original_get = agent_pipeline.get

    def spy_get(*args, **kwargs):
        observed_get.update(kwargs)
        return original_get(*args, **kwargs)

    monkeypatch.setattr(agent_pipeline, "get", spy_get)
    detail = client.get(
        f"/api/agents/artifacts/{artifact_id}?reconcile=false",
        headers=admin_headers,
    )

    assert detail.status_code == 200, detail.text
    assert observed_get["reconcile"] is False
    assert detail.json()["spec"] == {"large": "spec"}
