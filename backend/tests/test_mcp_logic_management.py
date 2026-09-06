"""Business logic management MCP tools and publish approval boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.database import SessionLocal
from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.models import (
    BusinessLogic,
    BusinessLogicCategory,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.services.settings_service import SettingsService


def _env():
    suffix = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:logic-{suffix}", name=f"逻辑域-{suffix}")
        db.add(domain)
        db.flush()
        ontology = Ontology(domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1)
        db.add(ontology)
        db.flush()
        obj = ObjectType(ontology_id=ontology.id, name=f"order_{suffix}", display_name="订单", table_role="business_object", status=EntityStatus.PUBLISHED.value)
        db.add(obj)
        db.flush()
        prop = Property(object_type_id=obj.id, name="amount", display_name="金额", semantic_type="measure", data_type="decimal", status=EntityStatus.PUBLISHED.value)
        db.add(prop)
        cat = BusinessLogicCategory(name=f"销售指标-{suffix}")
        db.add(cat)
        logic = BusinessLogic(
            ontology_id=ontology.id,
            category_id=cat.id,
            name=f"total_{suffix}",
            display_name="订单总额",
            logic_type="metric",
            description="订单金额合计",
            status=EntityStatus.SUGGESTED.value,
            user_created=True,
            origin="manual",
        )
        db.add(logic)
        db.commit()
        return {"domain_id": domain.id, "ontology_id": ontology.id, "object_id": obj.id, "property_id": prop.id, "logic_id": logic.id, "category_id": cat.id}


def _call(name: str, arguments: dict, role: str = "publisher", **auth_kwargs):
    auth = AuthContext(client_type="mcp_local", role=role, principal_name=f"mcp-{role}", **auth_kwargs)
    return asyncio.run(TOOL_REGISTRY[name].execute(arguments, auth))


def test_logic_management_tools_have_expected_roles():
    assert tool_required_role(TOOL_REGISTRY["list_logic_categories"]) == "reader"
    assert tool_required_role(TOOL_REGISTRY["bind_logic_object"]) == "editor"
    assert tool_required_role(TOOL_REGISTRY["review_logic_publish"]) == "reader"
    assert tool_required_role(TOOL_REGISTRY["publish_logic"]) == "publisher"


def test_logic_bindings_and_metadata_use_real_service(env=None):
    env = _env()
    categories = _call("list_logic_categories", {}, "reader")
    assert categories.success
    assert any(row["id"] == env["category_id"] for row in categories.data["categories"])

    updated = _call("update_logic", {"logic_id": env["logic_id"], "description": "订单金额合计（人工确认）"}, "editor")
    assert updated.success, updated.error
    assert updated.data["logic"]["description"] == "订单金额合计（人工确认）"

    object_binding = _call("bind_logic_object", {"logic_id": env["logic_id"], "object_type_id": env["object_id"], "role": "subject"}, "editor")
    property_binding = _call("bind_logic_property", {"logic_id": env["logic_id"], "property_id": env["property_id"], "role": "input"}, "editor")
    assert object_binding.success, object_binding.error
    assert property_binding.success, property_binding.error

    assert _call("unbind_logic_property", {"binding_id": property_binding.data["binding"]["id"]}, "editor").success
    assert _call("unbind_logic_object", {"binding_id": object_binding.data["binding"]["id"]}, "editor").success


def test_logic_publish_review_recompiles_and_publish_requires_host_confirmation(monkeypatch):
    env = _env()
    review = _call("review_logic_publish", {"logic_id": env["logic_id"]}, "reader")
    assert review.success, review.error
    assert review.data["ready"] is True
    digest = review.data["confirmation_digest"]

    monkeypatch.setattr(
        SettingsService,
        "get_mcp_runtime",
        lambda _self, _db: SimpleNamespace(mcp_allow_stdio_interactive_approval=False),
    )
    blocked = _call("publish_logic", {"logic_id": env["logic_id"], "confirmation_digest": digest}, "publisher", principal_id="principal-1")
    assert blocked.success is False
    assert blocked.metadata["gate"] == "host_interactive_approval_disabled"
    with SessionLocal() as db:
        assert db.get(BusinessLogic, env["logic_id"]).status != EntityStatus.PUBLISHED.value


def test_logic_publish_succeeds_only_with_fresh_local_host_confirmation(monkeypatch):
    env = _env()
    monkeypatch.setattr(
        SettingsService,
        "get_mcp_runtime",
        lambda _self, _db: SimpleNamespace(mcp_allow_stdio_interactive_approval=True),
    )
    review = _call("review_logic_publish", {"logic_id": env["logic_id"]}, "reader")
    digest = review.data["confirmation_digest"]
    published = _call(
        "publish_logic",
        {
            "logic_id": env["logic_id"],
            "confirmation_digest": digest,
            "host_confirmation": {"approved": True, "channel": "ask_user_question", "digest": digest},
        },
        "publisher",
        principal_id="principal-1",
    )
    assert published.success, published.error
    assert published.data["confirmation_status"] == "confirmed"
    with SessionLocal() as db:
        assert db.get(BusinessLogic, env["logic_id"]).status == EntityStatus.PUBLISHED.value
