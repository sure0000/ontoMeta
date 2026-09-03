"""审核的两条收口：已判可回看重判；角色决定板块，没有第三态。

两件事都在钉同一句话：**判定要能被看见、被追回**。
- 判完的板块如果从队列里彻底消失，审核就成了单向的——判错了只能靠记忆去翻卡片墙。
- 「待归类业务对象」这个中间板块已经取消：是业务对象/关系表的一定落在业务板块下，
  其余的落系统表。归不进业务模块又确实是业务对象的，留在系统表里由
  ``stranded_in_system`` 数出来，审核台标红提示「移动到板块」，而不是拿门禁挡住判定。
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
    SEGMENT_KIND_BUSINESS,
    SEGMENT_KIND_SYSTEM,
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


# --------------------------------------------------- 落位不变量：角色决定板块


def test_confirming_a_stranded_object_is_allowed_but_reported(client, admin_headers):
    """归不进业务模块的业务对象不再被门禁挡住，但要如实报出来。

    旧行为是 400 拒绝，人只看到一个被禁掉的按钮。现在判定照常成立，返回里带
    ``stranded_in_system``，审核台据此提示「还有 N 个在系统表里，挑个业务板块移过去」。
    """
    _seed(
        "gate",
        objects=[{"name": "tab_orphan_order", "fallback": SEGMENT_KIND_SYSTEM}],
    )
    obj_id = _object("tab_orphan_order").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "needs_review": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert resp.json()["stranded_in_system"] == 1
    assert _object("tab_orphan_order").needs_review is False


def test_moving_to_a_business_segment_is_one_call(client, admin_headers):
    """「移动到销售 + 确认」是一次调用：先挪板块，再判复核。"""
    _, seg_ids = _seed(
        "classify",
        objects=[
            {"name": "tab_lead_note", "fallback": SEGMENT_KIND_SYSTEM},
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
    assert resp.json()["stranded_in_system"] == 0

    moved = _object("tab_lead_note")
    assert moved.segment_id == sales_id
    assert moved.needs_review is False
    # 人工移板块要钉住，机器重生成不能再把它拨回系统表
    assert "segment_id" in json.loads(moved.overridden_fields or "[]")


def test_recasting_to_data_table_resettles_into_the_system_board(client, admin_headers):
    """改判数据表：对象落到系统表板块，判定随之成立。"""
    _, seg_ids = _seed(
        "recast-down",
        objects=[
            {"name": "tab_sync_log", "segment": "销售"},
            {"name": "tab_sales_order", "segment": "销售"},
        ],
    )
    obj_id = _object("tab_sync_log").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "table_role": "data_table"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stranded_in_system"] == 0

    obj = _object("tab_sync_log")
    assert obj.needs_review is False
    assert obj.segment_id != seg_ids["销售"]
    with SessionLocal() as db:
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_SYSTEM


def test_recasting_to_business_object_is_adopted_by_name_family(client, admin_headers):
    """反过来：技术表改判成业务对象，靠命名族亲和直接落进对应的业务模块。

    这就是「待归类」被取消后的出路——机器不再把它挂起，而是给它一个真实的归属。
    """
    _, seg_ids = _seed(
        "recast-up",
        objects=[
            {"name": "tab_sales_order", "segment": "销售"},
            {
                "name": "tab_sales_route_history",
                "role": "technical",
                "fallback": SEGMENT_KIND_SYSTEM,
            },
        ],
    )
    obj_id = _object("tab_sales_route_history").id

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "table_role": "business_object"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert resp.json()["stranded_in_system"] == 0

    obj = _object("tab_sales_route_history")
    assert obj.segment_id == seg_ids["销售"]
    assert obj.needs_review is False
    # 亲和归位是机器落位，不是人工钉死的归属：下次重聚类仍可以改
    assert "segment_id" not in json.loads(obj.overridden_fields or "[]")


def test_recasting_to_business_object_without_affinity_stays_in_system(
    client, admin_headers
):
    """既无邻居也无同族的，留在系统表——但调用方要能说出「其中 1 个还在系统表里」。"""
    _seed(
        "recast-up-blind",
        objects=[
            {
                "name": "tab_route_history",
                "role": "technical",
                "fallback": SEGMENT_KIND_SYSTEM,
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
    assert resp.json()["stranded_in_system"] == 1
    with SessionLocal() as db:
        obj = _object("tab_route_history")
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_SYSTEM


def test_non_business_role_is_evicted_from_a_business_segment(client, admin_headers):
    """业务板块里不该躺着技术表：改判非业务角色即移出到系统表。

    这是「其余的分配在系统表」这条规矩的另一半——只往里补不往外清，业务地图会
    慢慢混进一堆管道表。人工钉过板块的除外（见下一条）。
    """
    _seed("evict", objects=[{"name": "tab_purchase_log", "segment": "采购"}])
    before = _object("tab_purchase_log")

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [before.id], "table_role": "technical"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    after = _object("tab_purchase_log")
    assert after.segment_id != before.segment_id
    with SessionLocal() as db:
        assert db.get(OntologySegment, after.segment_id).kind == SEGMENT_KIND_SYSTEM


def test_manual_segment_choice_survives_a_role_change(client, admin_headers):
    """人工移过板块的对象，机器不再按角色把它挪走——人工归属是最终解释。"""
    _, seg_ids = _seed(
        "pinned",
        objects=[
            {"name": "tab_manual_pick", "fallback": SEGMENT_KIND_SYSTEM},
            {"name": "tab_purchase_order", "segment": "采购"},
        ],
    )
    obj_id = _object("tab_manual_pick").id
    client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "segment_id": seg_ids["采购"]},
        headers=admin_headers,
    )

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "table_role": "technical"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert _object("tab_manual_pick").segment_id == seg_ids["采购"]


# ---------------------------------------------------------------- 统计口径


def test_stats_expose_segment_kind_and_the_stranded_backlog(client, admin_headers):
    """「业务对象却压在系统表里」的那批要能被数出来，否则它们谁也看不见。"""
    ontology_id, _ = _seed(
        "stats",
        objects=[
            {"name": "tab_gate_a", "fallback": SEGMENT_KIND_SYSTEM},
            # 已确认过、却仍不属于任何业务模块：不在待判队列里，只能靠这个数字捞回来
            {
                "name": "tab_gate_b",
                "fallback": SEGMENT_KIND_SYSTEM,
                "needs_review": False,
            },
            # 系统表里的技术表是正常归宿，不算归错地方
            {
                "name": "tab_gate_tech",
                "role": "technical",
                "fallback": SEGMENT_KIND_SYSTEM,
            },
            {"name": "tab_gate_c", "segment": "采购", "needs_review": False},
        ],
    )
    stats = client.get(
        f"/api/ontologies/{ontology_id}/review-stats", headers=admin_headers
    ).json()

    assert stats["stranded_total"] == 2
    assert stats["stranded_reviewed"] == 1
    kinds = {row["segment_name"]: row["kind"] for row in stats["segment_progress"]}
    assert kinds["系统表"] == SEGMENT_KIND_SYSTEM
    assert kinds["采购"] == SEGMENT_KIND_BUSINESS


def test_queue_group_carries_segment_kind(client, admin_headers):
    """审核台靠 stranded_in_system 认出「这一组归错了地方，先移板块」。"""
    ontology_id, _ = _seed(
        "group-kind",
        objects=[
            {"name": "tab_gate_d", "fallback": SEGMENT_KIND_SYSTEM},
            {"name": "tab_gate_tech", "role": "technical", "fallback": SEGMENT_KIND_SYSTEM},
        ],
    )
    queue = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    business = next(g for g in queue["groups"] if g["table_role"] == "business_object")
    assert business["segment_kind"] == SEGMENT_KIND_SYSTEM
    assert business["stranded_in_system"] is True
    # 技术表待在系统表里是它的归宿，不该被催着移板块
    technical = next(g for g in queue["groups"] if g["table_role"] == "technical")
    assert technical["stranded_in_system"] is False


# ------------------------------------------------------- 落位漂移（存量自愈）


def test_role_board_drift_heals_on_judgement(client, admin_headers):
    """存量漂移：角色早就改对了、板块却没跟着挪的那批。

    它们再点一次同样的改判是空操作——不自愈就永远躺在错板块里。判定发生时顺手对齐，
    比让人手动挪板块可靠。
    """
    ontology_id, seg_ids = _seed(
        "drift",
        objects=[
            {"name": "tab_bank_hook", "role": "technical", "segment": "资金"},
        ],
    )
    obj_id = _object("tab_bank_hook").id

    queue = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    group = next(g for g in queue["groups"] if g["table_role"] == "technical")
    assert group["stranded_in_system"] is False

    resp = client.patch(
        "/api/object-types/batch",
        json={"ids": [obj_id], "needs_review": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    obj = _object("tab_bank_hook")
    assert obj.needs_review is False
    assert obj.segment_id != seg_ids["资金"]
    with SessionLocal() as db:
        assert db.get(OntologySegment, obj.segment_id).kind == SEGMENT_KIND_SYSTEM
    # 自愈不是人工归属：下次重聚类仍可以把它收编，所以不钉 overridden
    assert "segment_id" not in json.loads(obj.overridden_fields or "[]")
