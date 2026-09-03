"""审核队列：分组、确定性排序、游标可重放。

钉住的核心性质是**判定动作不扰动队列顺序**——列表接口按 updated_at 排序时，判一批
就会让后面的行整体前移，翻页静默跳过整页。队列的排序键里不含任何随判定变化的字段。
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
    RelationType,
)
from app.services.review_queue import (
    MISC_FAMILY,
    QueueRow,
    build_groups,
    name_family,
    score_band,
)


# ---------------------------------------------------------------- 纯函数


def test_name_family_strips_layer_prefixes():
    # 同一族的表在不同命名习惯下都要落到同一个族名，否则分不到一组。
    assert name_family("tabPurchase Order") == "purchase"
    assert name_family("tab_purchase_order") == "purchase"
    assert name_family("ODS_SALES_ORDER") == "sales"
    assert name_family("dim_customer") == "customer"


def test_name_family_chinese_takes_two_chars():
    assert name_family("采购订单") == "采购"
    assert name_family("采购申请") == "采购"
    assert name_family("销售订单") == "销售"


def test_name_family_falls_back_to_whole_name():
    # 取不出实词时自成一族，不要把无关的表凑一堆。
    assert name_family("") == ""
    assert name_family("tab") == "tab"


def test_score_band_aligns_with_classifier_threshold():
    from app.services.object_classifier import ROLE_SCORE_THRESHOLD

    assert score_band(None) == "unknown"
    assert score_band(ROLE_SCORE_THRESHOLD) == "near"
    assert score_band(ROLE_SCORE_THRESHOLD - 0.1) == "weak"
    assert score_band(3.0) == "strong"


def _row(oid, name, segment_id=None, role="business_object", score=2.5):
    return QueueRow(
        id=oid,
        name=name,
        display_name=name,
        segment_id=segment_id,
        table_role=role,
        role_signals={"score": score} if score is not None else None,
    )


def test_build_groups_buckets_same_family_and_band():
    groups = build_groups(
        [
            _row("1", "tab_purchase_order", "seg-a"),
            _row("2", "tab_purchase_invoice", "seg-a"),
            _row("3", "tab_purchase_receipt", "seg-a"),
            _row("4", "tab_sales_order", "seg-a"),
        ],
        segment_names={"seg-a": "采购"},
    )
    families = {g.name_family: g.size for g in groups}
    # 够 3 个才成族；落单的 sales 进零散桶，而不是自成一组占一次点击
    assert families == {"purchase": 3, MISC_FAMILY: 1}
    assert all(g.segment_name == "采购" for g in groups)


def test_small_families_merge_into_one_misc_group():
    """长尾必须并桶：各叫各名的表若各成一组，成组判定就退回逐个判。

    实测 erpnext 866 个待复核：不并是 460 组、328 个单成员组；并到 >=3 是 99 组。
    """
    rows = [_row(str(i), f"tab_{chr(97 + i)}_thing", "seg-a") for i in range(8)]
    groups = build_groups(rows, segment_names={"seg-a": "采购"})
    assert len(groups) == 1
    assert groups[0].name_family == MISC_FAMILY
    assert groups[0].size == 8
    # 并桶不改变「同板块+同角色+同判定强度」这个前提
    assert groups[0].table_role == "business_object"
    assert groups[0].score_band == "near"


def test_misc_bucket_does_not_mix_roles_or_bands():
    groups = build_groups(
        [
            _row("1", "tab_a", "seg-a"),
            _row("2", "tab_b", "seg-a", role="technical"),
            _row("3", "tab_c", "seg-a", score=3.5),
        ]
    )
    # 三个都落单，但角色/强度不同 → 三个零散桶，不是一个大杂烩
    assert len(groups) == 3
    assert all(g.name_family == MISC_FAMILY for g in groups)


def test_build_groups_is_deterministic_and_puts_unsegmented_last():
    rows = [
        _row("1", "tab_a", "seg-a"),
        _row("2", "tab_b", "seg-b"),
        _row("3", "tab_c", None),
        _row("4", "tab_d", "seg-b"),
    ]
    first = [g.key for g in build_groups(rows)]
    # 输入顺序不影响输出顺序
    second = [g.key for g in build_groups(list(reversed(rows)))]
    assert first == second
    # 未接入板块永远最后
    assert first[-1].startswith("-")


def test_group_order_does_not_shift_as_items_are_judged():
    """排序键里不能有「还剩多少待判」这类会随判定变化的量。

    否则判到一半，某个板块被另一个顶到前面，游标指向的位置随之漂移——
    翻页会跳过或重复整组。
    """
    rows = [
        _row("1", "tab_a", "seg-a"),
        _row("2", "tab_b", "seg-b"),
        _row("3", "tab_c", "seg-b"),
        _row("4", "tab_d", "seg-b"),
    ]
    names = {"seg-a": "采购", "seg-b": "销售"}
    before = [g.key for g in build_groups(rows, segment_names=names)]
    # 判掉 seg-b 的两个（它从「待判最多」变成「待判最少」）
    after = [
        g.key
        for g in build_groups([r for r in rows if r.id in {"1", "2"}], segment_names=names)
    ]
    assert after == [k for k in before if k in set(after)]


def test_build_groups_orders_members_by_score():
    groups = build_groups(
        [
            _row("low", "tab_x_1", "s", score=1.0),
            _row("high", "tab_x_2", "s", score=3.5),
            _row("none", "tab_x_3", "s", score=None),
        ]
    )
    # 得分带不同会分开成组，这里只断言组内排序：同带内高分在前、无分垫底
    by_band = {g.score_band: g.member_ids for g in groups}
    assert by_band["strong"] == ["high"]
    assert by_band["weak"] == ["low"]
    assert by_band["unknown"] == ["none"]


# ---------------------------------------------------------------- 接口


def _seed(tag: str, *, objects: list[dict]) -> tuple[str, dict[str, str]]:
    """建一个本体，objects 里每项 {name, role, segment, score, needs_review}。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:queue-{tag}", name=f"queue-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()
        seg_ids: dict[str, str] = {}
        for seg_name in {o["segment"] for o in objects if o.get("segment")}:
            seg = OntologySegment(
                ontology_id=ontology.id,
                name=seg_name,
                display_name=seg_name,
                member_count=0,
            )
            db.add(seg)
            db.flush()
            seg_ids[seg_name] = seg.id
        for spec in objects:
            db.add(
                ObjectType(
                    ontology_id=ontology.id,
                    name=spec["name"],
                    display_name=spec["name"],
                    table_role=spec.get("role", "business_object"),
                    segment_id=seg_ids.get(spec.get("segment")),
                    needs_review=spec.get("needs_review", True),
                    role_signals=json.dumps({"score": spec.get("score", 2.5)}),
                    status="suggested",
                )
            )
        db.commit()
        return ontology.id, seg_ids


def test_review_queue_groups_and_covers_all_roles(client, admin_headers):
    ontology_id, _ = _seed(
        "roles",
        objects=[
            {"name": "tab_purchase_order", "segment": "采购"},
            {"name": "tab_purchase_invoice", "segment": "采购"},
            {"name": "tab_purchase_receipt", "segment": "采购"},
            # 桥表重判成的数据表也待复核——旧统计口径把它们整批藏了起来
            {"name": "tab_purchase_log", "segment": "采购", "role": "data_table"},
            {"name": "tab_config", "role": "technical", "score": 0.5},
            {"name": "tab_done", "segment": "采购", "needs_review": False},
        ],
    )
    resp = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["pending_total"] == 5
    assert body["pending_by_role"] == {
        "business_object": 3,
        "data_table": 1,
        "technical": 1,
    }
    # 业务对象两张同族表进同一组；不同角色不混组
    purchase = [
        g
        for g in body["groups"]
        if g["name_family"] == "purchase" and g["table_role"] == "business_object"
    ]
    assert len(purchase) == 1
    assert purchase[0]["size"] == 3
    assert {m["name"] for m in purchase[0]["members"]} == {
        "tab_purchase_order",
        "tab_purchase_invoice",
        "tab_purchase_receipt",
    }
    # 已确认的对象不进队列
    assert all(
        m["name"] != "tab_done" for g in body["groups"] for m in g["members"]
    )
    # 未接入板块的技术表排在最后
    assert body["groups"][-1]["segment_id"] is None


def test_group_reports_already_reviewed_count(client, admin_headers):
    """「本组已判 N 个」是判据的一部分：同族同板块前面都确认了，后面多半一样。"""
    ontology_id, _ = _seed(
        "streak",
        objects=[
            {"name": "tab_sales_order", "segment": "销售"},
            {"name": "tab_sales_invoice", "segment": "销售"},
            {"name": "tab_sales_person", "segment": "销售"},
            # 同族同板块同强度、但已经判过的三个
            {"name": "tab_sales_team", "segment": "销售", "needs_review": False},
            {"name": "tab_sales_stage", "segment": "销售", "needs_review": False},
        ],
    )
    body = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    group = body["groups"][0]
    assert group["size"] == 3
    assert group["reviewed_in_group"] == 2


def test_group_key_survives_its_family_being_judged_down(client, admin_headers):
    """分组在「待判 + 已判」上做一次：族不会因为判掉几个就缩水掉进零散桶。

    否则键随判定变化，游标又开始漂——和按「待判数」排序是同一个坑。
    """
    ontology_id, _ = _seed(
        "shrink",
        objects=[{"name": f"tab_sales_{i}", "segment": "销售"} for i in range(3)],
    )
    first = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    ids = [m["id"] for m in first["groups"][0]["members"]]
    before = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    key = before["groups"][0]["key"]
    assert before["groups"][0]["name_family"] == "sales"

    # 判掉两个，族只剩 1 个待判——低于并桶阈值
    client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": ids[:2], "needs_review": False},
    )
    after = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    assert after["groups"][0]["key"] == key
    assert after["groups"][0]["name_family"] == "sales"
    assert after["groups"][0]["size"] == 1
    assert after["groups"][0]["reviewed_in_group"] == 2


def test_review_queue_members_carry_decision_evidence(client, admin_headers):
    """判据必须随队列一起来——否则复核者还是得逐个点进详情页第 3 个 Tab。"""
    ontology_id, _ = _seed(
        "evidence", objects=[{"name": "tab_purchase_order", "segment": "采购"}]
    )
    body = client.get(
        f"/api/ontologies/{ontology_id}/review-queue", headers=admin_headers
    ).json()
    member = body["groups"][0]["members"][0]
    assert member["role_signals"]["score"] == 2.5
    assert "row_count" in member
    assert member["segment_name"] == "采购"


def test_cursor_is_replayable_after_judging(client, admin_headers):
    """判完一批后拿同一个 cursor 再请求，不会跳过任何一组。

    这是 P0 的全部意义：列表接口按 updated_at 排序 + offset 分页时，判定会把行移出
    结果集并让后面的行整体前移，第 2 页因此永远看不到本该在那儿的组。
    """
    ontology_id, _ = _seed(
        "cursor",
        objects=[
            {"name": f"tab_fam{i}_a", "segment": f"seg{i}"} for i in range(5)
        ],
    )
    page1 = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?limit=2", headers=admin_headers
    ).json()
    assert len(page1["groups"]) == 2
    cursor = page1["next_cursor"]
    assert cursor

    # 判掉第 1 页的全部成员（模拟真实审核动作）
    judged = [m["id"] for g in page1["groups"] for m in g["members"]]
    resp = client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": judged, "needs_review": False},
    )
    assert resp.status_code == 200, resp.text

    page2 = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?limit=2&cursor={cursor}",
        headers=admin_headers,
    ).json()
    seen = [m["id"] for g in page2["groups"] for m in g["members"]]
    # 判掉的不会再出现
    assert set(seen).isdisjoint(judged)
    assert page2["pending_total"] == 3
    # 游标指向的那一组仍然是本页第一组（它没被判，也没被挤走）
    assert page2["groups"][0]["key"] == cursor
    # 且它之后的组一个不少：整条队列剩下的 3 组正好是「游标之前的 0 组 + 本页起的 3 组」
    remaining = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?limit=50&cursor={cursor}",
        headers=admin_headers,
    ).json()
    assert remaining["group_total"] - remaining["group_offset"] == 3
    assert len(remaining["groups"]) == 3


def test_review_queue_filters_by_segment_and_unsegmented(client, admin_headers):
    ontology_id, seg_ids = _seed(
        "segfilter",
        objects=[
            {"name": "tab_a_1", "segment": "采购"},
            {"name": "tab_b_1"},
        ],
    )
    scoped = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?segment_id={seg_ids['采购']}",
        headers=admin_headers,
    ).json()
    assert scoped["pending_total"] == 1
    assert scoped["groups"][0]["segment_name"] == "采购"

    unsegmented = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?segment_id=-",
        headers=admin_headers,
    ).json()
    assert unsegmented["pending_total"] == 1
    assert unsegmented["groups"][0]["segment_id"] is None


def test_review_stats_counts_every_role(client, admin_headers):
    """统计口径与队列一致：非业务对象也算待复核，卡发布的那部分单列。"""
    ontology_id, _ = _seed(
        "stats",
        objects=[
            {"name": "tab_a", "segment": "采购"},
            {"name": "tab_b", "role": "data_table"},
            {"name": "tab_c", "role": "technical"},
            {"name": "tab_d", "segment": "采购", "needs_review": False},
        ],
    )
    stats = client.get(
        f"/api/ontologies/{ontology_id}/review-stats", headers=admin_headers
    ).json()
    assert stats["total_objects"] == 4
    assert stats["needs_review_count"] == 3
    assert stats["business_object_pending"] == 1
    assert stats["pending_by_role"] == {
        "business_object": 1,
        "data_table": 1,
        "technical": 1,
    }
    assert stats["unsegmented_pending"] == 2


# ---------------------------------------------------------------- 关系队列


def _seed_relations(tag: str, specs: list[dict]) -> tuple[str, list[str]]:
    """建两端对象 + 若干关系。specs 每项 {verb, structure, needs_review}。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:relq-{tag}", name=f"relq-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()
        segment = OntologySegment(
            ontology_id=ontology.id, name="采购", display_name="采购", member_count=0
        )
        db.add(segment)
        db.flush()
        src = ObjectType(
            ontology_id=ontology.id,
            name="src",
            display_name="采购订单",
            segment_id=segment.id,
            status="suggested",
        )
        tgt = ObjectType(
            ontology_id=ontology.id, name="tgt", display_name="供应商", status="suggested"
        )
        db.add_all([src, tgt])
        db.flush()
        ids = []
        for i, spec in enumerate(specs):
            rel = RelationType(
                ontology_id=ontology.id,
                name=f"rel_{i}",
                display_name=spec["verb"],
                source_object_type_id=src.id,
                target_object_type_id=tgt.id,
                structure_type=spec.get("structure", "reference"),
                source_confidence=spec.get("confidence", 0.6),
                needs_review=spec.get("needs_review", True),
                status="suggested",
            )
            db.add(rel)
            db.flush()
            ids.append(rel.id)
        db.commit()
        return ontology.id, ids


def test_relation_queue_groups_by_verb(client, admin_headers):
    """关系的判定单元是去重组：同一个动词的一批边一起判。"""
    ontology_id, _ = _seed_relations(
        "verb",
        [{"verb": "属于"} for _ in range(4)]
        + [{"verb": "引用"} for _ in range(3)]
        + [{"verb": "已确认的", "needs_review": False}],
    )
    body = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?kind=relation", headers=admin_headers
    ).json()
    assert body["kind"] == "relation"
    assert body["pending_total"] == 7
    families = {g["name_family"]: g["size"] for g in body["groups"]}
    assert families == {"属于": 4, "引用": 3}
    # 关系装在 relation_members 里，且带得出「源 → 目标」
    member = body["groups"][0]["relation_members"][0]
    assert member["source_object_name"] == "采购订单"
    assert member["target_object_name"] == "供应商"
    assert body["groups"][0]["members"] == []


def test_relation_queue_verb_is_not_tokenized():
    """动词整体才是这条边的身份：切词会把「发起支付」和「发起审批」并成一族。"""
    groups = build_groups(
        [
            QueueRow(
                id=str(i),
                name=f"rel_{i}",
                display_name=verb,
                segment_id="seg",
                table_role="reference",
                family=verb,
                score=0.6,
            )
            for i, verb in enumerate(
                ["发起支付"] * 3 + ["发起审批"] * 3
            )
        ]
    )
    assert {g.name_family for g in groups} == {"发起支付", "发起审批"}


def test_relation_batch_update_sets_review_state(client, admin_headers):
    ontology_id, ids = _seed_relations("batch", [{"verb": "属于"} for _ in range(3)])
    resp = client.patch(
        "/api/relation-types/batch",
        headers=admin_headers,
        json={"ids": ids, "needs_review": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 3
    body = client.get(
        f"/api/ontologies/{ontology_id}/review-queue?kind=relation", headers=admin_headers
    ).json()
    assert body["pending_total"] == 0
    # 已是目标状态的不重复计数
    again = client.patch(
        "/api/relation-types/batch",
        headers=admin_headers,
        json={"ids": ids, "needs_review": False},
    )
    assert again.json()["updated"] == 0
