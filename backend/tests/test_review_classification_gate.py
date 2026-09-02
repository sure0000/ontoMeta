"""审核的两条收口：已判可回看重判；「待归类业务对象」必须先归位才算判完。

两件事都在钉同一句话：**判定要能被看见、被追回**。
- 判完的板块如果从队列里彻底消失，审核就成了单向的——判错了只能靠记忆去翻卡片墙。
- 「待归类业务对象」里的表判成业务对象却连不成簇，只确认角色的话它既进不了业务地图，
  也不再出现在待判队列里，那个板块从此永远不会变空。
"""

from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologySegment,
    OntologyStatus,
)
from app.services.segment_kinds import (
    SEGMENT_KIND_PENDING,
    SEGMENT_KIND_TECHNICAL,
)
from app.services.segment_placement import ensure_fallback_segment


def _seed(tag: str, *, objects: list[dict]) -> tuple[str, dict[str, str]]:
    """建本体；``segment`` 给业务板块名，``fallback`` 给兜底板块 kind。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:gate-{tag}", name=f"gate-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()

        seg_ids: dict[str, str] = {}
        for name in {o["segment"] for o in objects if o.get("segment")}:
            seg = OntologySegment(
                ontology_id=ontology.id,
                name=name,
                display_name=name,
                kind="business",
                member_count=0,
            )
            db.add(seg)
            db.flush()
            seg_ids[name] = seg.id
        for kind in {o["fallback"] for o in objects if o.get("fallback")}:
            seg_ids[kind] = ensure_fallback_segment(db, ontology.id, kind).id

        for spec in objects:
            segment_id = seg_ids.get(spec.get("segment") or spec.get("fallback"))
            db.add(
                ObjectType(
                    ontology_id=ontology.id,
                    name=spec["name"],
                    display_name=spec["name"],
                    table_role=spec.get("role", "business_object"),
                    segment_id=segment_id,
                    needs_review=spec.get("needs_review", True),
                    role_signals=json.dumps({"score": spec.get("score", 2.5)}),
                    status="suggested",
                )
            )
        db.commit()
        return ontology.id, seg_ids


def _object(name: str) -> ObjectType:
    with SessionLocal() as db:
        obj = db.query(ObjectType).filter(ObjectType.name == name).one()
        db.expunge(obj)
        return obj


# ---------------------------------------------------------------- 已判可回看


def test_reviewed_view_returns_the_judged_half_with_the_same_group_keys(
    client, admin_headers
):
    """判完的组不是消失了，只是换到了另一半：两个视图用同一套 key。"""
    ontology_id, _ = _seed(
        "reviewed",
        objects=[
            {"name": "tab_sales_order", "segment": "销售"},
            {"name": "tab_sales_invoice", "segment": "销售", "needs_review": False},
            {"name": "tab_sales_person", "segment": "销售", "needs_review": False},
        ],
    )

    pending = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    reviewed = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?status=reviewed",
        headers=admin_headers,
    ).json()

    assert pending["status"] == "pending"
    assert reviewed["status"] == "reviewed"
    # 同一组：key 一致，只是成员换了一半
    assert [g["key"] for g in pending["groups"]] == [g["key"] for g in reviewed["groups"]]
    assert {m["name"] for m in pending["groups"][0]["members"]} == {"tab_sales_order"}
    assert {m["name"] for m in reviewed["groups"][0]["members"]} == {
        "tab_sales_invoice",
        "tab_sales_person",
    }
    # 两个视图里的角标各自成立
    assert pending["pending_total"] == 1
    assert pending["reviewed_total"] == 2
    assert reviewed["pending_total"] == 1


def test_fully_judged_segment_is_still_reachable(client, admin_headers):
    """整块判完之后，待判视图是空的，已判视图仍然能把它原样打开。"""
    ontology_id, seg_ids = _seed(
        "done",
        objects=[
            {"name": "tab_stock_entry", "segment": "库存", "needs_review": False},
            {"name": "tab_stock_ledger", "segment": "库存", "needs_review": False},
        ],
    )
    base = f"/api/ontologies/{ontology_id}/review-queue?segment_id={seg_ids['库存']}"

    assert client.get(base, headers=admin_headers).json()["groups"] == []
    reviewed = client.get(f"{base}&status=reviewed", headers=admin_headers).json()
    assert len(reviewed["groups"]) == 1
    assert {m["name"] for m in reviewed["groups"][0]["members"]} == {
        "tab_stock_entry",
        "tab_stock_ledger",
    }


def test_reviewed_members_can_be_sent_back_to_the_queue(client, admin_headers):
    """回看之后要能改主意：退回复核 = 批量置 needs_review=true。"""
    ontology_id, _ = _seed(
        "reopen",
        objects=[{"name": "tab_item_price", "segment": "商品", "needs_review": False}],
    )
    obj_id = _object("tab_item_price").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "needs_review": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert _object("tab_item_price").needs_review is True

    queue = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    assert [m["name"] for g in queue["groups"] for m in g["members"]] == ["tab_item_price"]


# ---------------------------------------------------------------- 待归类门禁


def test_confirming_an_unclassified_object_is_refused(client, admin_headers):
    """只确认角色不算判完：它仍然不属于任何业务模块。"""
    _seed(
        "gate",
        objects=[{"name": "tab_orphan_order", "fallback": SEGMENT_KIND_PENDING}],
    )
    obj_id = _object("tab_orphan_order").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "needs_review": False},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "待归类业务对象" in resp.json()["detail"]
    # 拒绝之后什么都没改（不留半个改动在会话里）
    assert _object("tab_orphan_order").needs_review is True

    single = client.patch(
        f"/api/object-types/{obj_id}",
        json={"needs_review": False},
        headers=admin_headers,
    )
    assert single.status_code == 400


def test_classify_and_confirm_is_one_call(client, admin_headers):
    """「归类到销售 + 确认」是一次调用：门禁看的是挪过板块之后的归属。"""
    _, seg_ids = _seed(
        "classify",
        objects=[
            {"name": "tab_lead_note", "fallback": SEGMENT_KIND_PENDING},
            {"name": "tab_sales_order", "segment": "销售"},
        ],
    )
    sales_id = seg_ids["销售"]
    obj_id = _object("tab_lead_note").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "segment_id": sales_id, "needs_review": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert resp.json()["pending_classification"] == 0

    moved = _object("tab_lead_note")
    assert moved.segment_id == sales_id
    assert moved.needs_review is False
    # 人工归类要钉住，机器重生成不能再把它拨回待归类
    assert "segment_id" in json.loads(moved.overridden_fields or "[]")


def test_recasting_to_data_table_resettles_out_of_the_pending_board(
    client, admin_headers
):
    """改判数据表是另一条出路：对象落到「技术表」板块，判定随之成立。"""
    _seed(
        "recast-down",
        objects=[{"name": "tab_sync_log", "fallback": SEGMENT_KIND_PENDING}],
    )
    obj_id = _object("tab_sync_log").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "table_role": "data_table"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending_classification"] == 0

    obj = _object("tab_sync_log")
    assert obj.needs_review is False
    with SessionLocal() as db:
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_TECHNICAL


def test_recasting_to_business_object_lands_in_pending_and_stays_unreviewed(
    client, admin_headers
):
    """反过来也成立：技术表改判成业务对象只完成了一半，它还得被归位。"""
    _seed(
        "recast-up",
        objects=[
            {
                "name": "tab_route_history",
                "role": "technical",
                "fallback": SEGMENT_KIND_TECHNICAL,
            }
        ],
    )
    obj_id = _object("tab_route_history").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "table_role": "business_object"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    # 改了，但没判完——界面要能说出「其中 1 个仍待归类」
    assert resp.json()["updated"] == 1
    assert resp.json()["pending_classification"] == 1

    obj = _object("tab_route_history")
    assert obj.needs_review is True
    with SessionLocal() as db:
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_PENDING


def test_business_segment_membership_survives_a_role_change(client, admin_headers):
    """只动兜底板块：业务板块的归属是聚类/人工的判断，改角色不该把对象踢出去。"""
    _seed(
        "keep-segment",
        objects=[{"name": "tab_purchase_log", "segment": "采购"}],
    )
    before = _object("tab_purchase_log")

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [before.id], "table_role": "data_table"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert _object("tab_purchase_log").segment_id == before.segment_id


# ---------------------------------------------------------------- 统计口径


def test_stats_expose_segment_kind_and_the_unclassified_backlog(client, admin_headers):
    """存量里「已确认却仍待归类」的那批要能被数出来，否则它们谁也看不见。"""
    ontology_id, _ = _seed(
        "stats",
        objects=[
            {"name": "tab_gate_a", "fallback": SEGMENT_KIND_PENDING},
            # 门禁上线前留下的：角色确认过，却仍不属于任何业务模块
            {
                "name": "tab_gate_b",
                "fallback": SEGMENT_KIND_PENDING,
                "needs_review": False,
            },
            {"name": "tab_gate_c", "segment": "采购", "needs_review": False},
        ],
    )
    stats = client.get(
        f"/api/ontologies/{ontology_id}/review-stats", headers=admin_headers
    ).json()

    assert stats["unclassified_total"] == 2
    assert stats["unclassified_reviewed"] == 1
    kinds = {row["segment_name"]: row["kind"] for row in stats["segment_progress"]}
    assert kinds["待归类业务对象"] == SEGMENT_KIND_PENDING
    assert kinds["采购"] == "business"


def test_queue_group_carries_segment_kind(client, admin_headers):
    """审核台靠 requires_classification 认出「这一组不能只按 A 确认」。"""
    ontology_id, _ = _seed(
        "group-kind",
        objects=[{"name": "tab_gate_d", "fallback": SEGMENT_KIND_PENDING}],
    )
    queue = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    assert queue["groups"][0]["segment_kind"] == SEGMENT_KIND_PENDING
    assert queue["groups"][0]["requires_classification"] is True


# ------------------------------------------------------- 落位漂移（存量自愈）


def test_technical_stranded_in_the_pending_board_heals_on_judgement(
    client, admin_headers
):
    """存量漂移：先落进待归类、后来被重判成技术表却没跟着挪的那批。

    它们的角色已经是对的，「改判技术表」是空操作——不自愈就永远卡在门禁上，
    既确认不了也归类不动。实测 erpnext 的待归类板块里有 6 张这样的表。
    """
    ontology_id, seg_ids = _seed(
        "drift",
        objects=[
            {
                "name": "tab_bank_hook",
                "role": "technical",
                "fallback": SEGMENT_KIND_PENDING,
            }
        ],
    )
    obj_id = _object("tab_bank_hook").id

    # 这一组不该被要求归类：确认它会把它挪到「技术表」板块，那才是它的归宿
    queue = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    group = next(g for g in queue["groups"] if g["table_role"] == "technical")
    assert group["segment_kind"] == SEGMENT_KIND_PENDING
    assert group["requires_classification"] is False

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "needs_review": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    obj = _object("tab_bank_hook")
    assert obj.needs_review is False
    assert obj.segment_id != seg_ids[SEGMENT_KIND_PENDING]
    with SessionLocal() as db:
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_TECHNICAL
    # 自愈不是人工归属：下次重聚类仍可以把它收编，所以不钉 overridden
    assert "segment_id" not in json.loads(obj.overridden_fields or "[]")
