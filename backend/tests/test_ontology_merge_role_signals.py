"""role_signals 是机器每次生成重算的证据,应像 role_confidence 一样直接覆盖持久化,
且不进入三方合并/冲突流程(不是用户可编辑字段)。本文件验证:
  1) 首次生成持久化为可解析 JSON;
  2) 再生成时被新证据直接覆盖;
  3) 覆盖 role_signals 不牵动其它字段的冲突判定——人工修正仍保留。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus
from app.schemas import DraftObjectType
from app.services.edit import _mark_overridden
from app.services.ontology_merge import MergeReport, OntologyMergeService


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _fresh_ontology(db) -> Ontology:
    domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="测试域")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="llm"
    )
    db.add(ontology)
    db.flush()
    return ontology


def _obj(urn: str, *, signals: dict, confidence: float) -> DraftObjectType:
    return DraftObjectType(
        name="customer",
        display_name="客户",
        description=None,
        source_ref=urn,
        confidence=0.6,
        table_role="business_object",
        role_confidence=confidence,
        role_reason="被 3 张表外键引用，疑似主数据/维度实体",
        role_signals=signals,
    )


def test_role_signals_persisted_and_overwritten_without_conflict():
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        urn = "urn:li:dataset:customer"

        # 1) 首次生成 → 持久化为可解析 JSON。
        report = MergeReport()
        merge.merge_objects(
            db,
            ontology.id,
            [_obj(urn, signals={"score": 4.0, "needs_review": False,
                                 "role": "business_object",
                                 "signals": {"fk_in_degree": 3, "pk_columns": 1}},
                  confidence=0.9)],
            [],
            "gen1",
            report,
        )
        db.commit()
        obj = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).one()
        stored = json.loads(obj.role_signals)
        assert stored["score"] == 4.0
        assert stored["signals"]["fk_in_degree"] == 3

        # 2) 人工改了 display_name（受保护字段）。
        obj.display_name = "客户实体"
        _mark_overridden(obj, ["display_name"])
        db.commit()

        # 3) 再生成，机器给出新证据 → role_signals 直接覆盖，人工修正不受影响。
        report2 = MergeReport()
        merge.merge_objects(
            db,
            ontology.id,
            [_obj(urn, signals={"score": 5.0, "needs_review": False,
                                 "role": "business_object",
                                 "signals": {"fk_in_degree": 4, "pk_columns": 1}},
                  confidence=0.95)],
            [],
            "gen2",
            report2,
        )
        db.commit()
        db.refresh(obj)
        stored2 = json.loads(obj.role_signals)
        assert stored2["score"] == 5.0
        assert stored2["signals"]["fk_in_degree"] == 4
        # 人工修正保留 → 证明 role_signals 覆盖没有牵动冲突/合并流程。
        assert obj.display_name == "客户实体"
    finally:
        db.close()
