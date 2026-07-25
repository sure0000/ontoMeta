"""场景 D：从已发布本体派生修订草稿，再生成走三方合并产出冲突。"""

from __future__ import annotations

import uuid

import pytest

from app.database import Base, SessionLocal, engine
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.schemas import DraftObjectType
from app.services.ontology_merge import MergeReport, OntologyMergeService
from app.services.ontology_revision import OntologyRevisionService


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _published_domain(db) -> tuple[str, str]:
    domain = DomainContext(datahub_domain_id=f"urn:rev:{uuid.uuid4()}", name="演进域")
    db.add(domain)
    db.flush()
    published = Ontology(
        domain_context_id=domain.id,
        status=OntologyStatus.PUBLISHED.value,
        version=1,
        generated_by="llm",
    )
    db.add(published)
    db.flush()
    obj = ObjectType(
        ontology_id=published.id, name="order", display_name="订单",
        source_ref="urn:li:dataset:order", status=EntityStatus.PUBLISHED.value,
    )
    db.add(obj)
    db.flush()
    db.add(
        Property(
            object_type_id=obj.id, name="amt", display_name="订单金额",
            source_field_ref="amt", required=False, status=EntityStatus.PUBLISHED.value,
        )
    )
    db.commit()
    return domain.id, published.id


def test_revision_draft_clones_and_seeds_authority():
    db = SessionLocal()
    try:
        domain_id, _ = _published_domain(db)
        draft = OntologyRevisionService().create_revision_draft(db, domain_id)
        assert draft.status == OntologyStatus.DRAFT.value

        obj = (
            db.query(ObjectType).filter(ObjectType.ontology_id == draft.id).one()
        )
        assert obj.display_name == "订单"
        # 已发布值作为人工权威：全部字段钉住
        assert "display_name" in obj.pinned_fields
        assert obj.origin == "machine_edited"
        prop = db.query(Property).filter(Property.object_type_id == obj.id).one()
        assert prop.display_name == "订单金额"
        assert "display_name" in prop.pinned_fields

        # 再生成：机器给出不同 display_name → 与已发布权威值冲突
        report = MergeReport()
        incoming = DraftObjectType(
            name="order", display_name="订单信息", source_ref="urn:li:dataset:order",
        )
        OntologyMergeService().merge_objects(
            db, draft.id, [incoming], [], "gen1", report
        )
        db.commit()
        db.refresh(obj)
        assert obj.display_name == "订单"  # 权威值保留
        assert obj.has_conflict
        assert report.to_dict()["summary"]["conflict"] == 1
    finally:
        db.rollback()
        db.close()


def test_revision_requires_published_and_no_existing_draft():
    db = SessionLocal()
    try:
        # 无已发布本体
        domain = DomainContext(datahub_domain_id=f"urn:rev:{uuid.uuid4()}", name="空域")
        db.add(domain)
        db.commit()
        with pytest.raises(ValueError):
            OntologyRevisionService().create_revision_draft(db, domain.id)
    finally:
        db.rollback()
        db.close()
