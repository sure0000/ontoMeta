"""数据应用（Data App）API：创建 / 预览 / 发布 / 对话生成 端到端。"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)


def _seed_published_ontology() -> tuple[str, str, str, str]:
    """返回 (domain_id, ontology_id, object_type_id, amount_property_id)。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"urn:app:{uuid.uuid4()}", name="订单域"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
            generated_by="llm",
        )
        db.add(ontology)
        db.flush()
        obj = ObjectType(
            ontology_id=ontology.id,
            name="orders",
            display_name="订单",
            status="published",
        )
        db.add(obj)
        db.flush()
        channel = Property(
            object_type_id=obj.id,
            name="channel",
            display_name="渠道",
            data_type="string",
            semantic_type="category",
            status="published",
        )
        amount = Property(
            object_type_id=obj.id,
            name="amount",
            display_name="金额",
            data_type="decimal",
            semantic_type="amount",
            status="published",
        )
        db.add_all([channel, amount])
        db.commit()
        return domain.id, ontology.id, obj.id, amount.id
    finally:
        db.close()


def test_data_app_full_flow(client, admin_headers):
    domain_id, ontology_id, obj_id, amount_id = _seed_published_ontology()

    # 拉取字段 id（channel）
    db = SessionLocal()
    try:
        channel = (
            db.query(Property)
            .filter(Property.object_type_id == obj_id, Property.name == "channel")
            .first()
        )
        channel_id = channel.id
    finally:
        db.close()

    # 1) 创建数据表格应用（按渠道汇总金额）
    payload = {
        "domain_id": domain_id,
        "app_type": "data_table",
        "name": "渠道金额表",
        "datasets": [
            {
                "name": "渠道金额",
                "primary_object_type_id": obj_id,
                "binding": {
                    "primary_object_type_id": obj_id,
                    "measures": [
                        {"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}
                    ],
                    "dimensions": [
                        {"kind": "property", "id": channel_id, "name": "channel", "display_name": "渠道"}
                    ],
                    "filters": [],
                    "row_limit": 100,
                },
            }
        ],
    }
    res = client.post("/api/data-apps", headers=admin_headers, json=payload)
    assert res.status_code == 200, res.text
    app = res.json()
    app_id = app["id"]
    assert app["status"] == "draft"
    assert len(app["datasets"]) == 1
    dataset = app["datasets"][0]
    assert dataset["compiled_sql"] is not None
    assert "SUM(amount)" in dataset["compiled_sql"]
    assert "GROUP BY channel" in dataset["compiled_sql"]
    dataset_id = dataset["id"]

    # 2) 预览（Mock 数据）
    res = client.post(
        f"/api/data-apps/{app_id}/datasets/{dataset_id}/preview",
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    preview = res.json()
    assert preview["used_mock"] is True
    assert len(preview["columns"]) == 2  # channel + sum_amount
    assert len(preview["rows"]) > 0

    # 3) 发布
    res = client.post(
        f"/api/data-apps/{app_id}/publish",
        headers=admin_headers,
        json={"version_comment": "首次发布"},
    )
    assert res.status_code == 200, res.text
    published = res.json()
    assert published["status"] == "published"
    assert published["published_version"] == 1

    # 4) 版本列表
    res = client.get(f"/api/data-apps/{app_id}/versions", headers=admin_headers)
    assert res.status_code == 200, res.text
    versions = res.json()
    assert len(versions) == 1
    assert versions[0]["version"] == 1

    # 5) 列表可见
    res = client.get(f"/api/data-apps?domain_id={domain_id}", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert any(a["id"] == app_id for a in res.json())


def test_generate_app_from_chat(client, admin_headers):
    domain_id, _, obj_id, _ = _seed_published_ontology()

    res = client.post(
        "/api/chat-bi/generate-app",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "app_type": "data_table",
            "question": "最近 30 天各渠道的订单金额合计是多少？",
        },
    )
    assert res.status_code == 200, res.text
    app = res.json()
    assert app["source"] == "chat_generated"
    assert app["app_type"] == "data_table"
    assert len(app["datasets"]) >= 1


def test_generate_app_refuses_ungrounded(client, admin_headers):
    domain_id, *_ = _seed_published_ontology()
    res = client.post(
        "/api/chat-bi/generate-app",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "app_type": "screen",
            "question": "关于火箭发射的无关问题 zzz",
        },
    )
    assert res.status_code == 400, res.text


def test_generate_app_reuses_provided_caliber(client, admin_headers):
    """点击生成时复用对话已展示的口径，而非重新 ask（保证一致性）。"""
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    db = SessionLocal()
    try:
        channel = (
            db.query(Property)
            .filter(Property.object_type_id == obj_id, Property.name == "channel")
            .first()
        )
        channel_id = channel.id
    finally:
        db.close()

    caliber = [
        {"label": "主对象", "references": [{"kind": "object_type", "id": obj_id, "name": "orders"}]},
        {"label": "度量字段", "references": [{"kind": "property", "id": amount_id, "name": "amount"}]},
        {"label": "维度", "references": [{"kind": "property", "id": channel_id, "name": "channel"}]},
    ]
    res = client.post(
        "/api/chat-bi/generate-app",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "app_type": "data_table",
            "question": "各渠道金额合计",
            "caliber_decomposition": caliber,
            "referenced_objects": [{"id": obj_id, "name": "orders"}],
        },
    )
    assert res.status_code == 200, res.text
    app = res.json()
    ds = app["datasets"][0]
    assert ds["primary_object_type_id"] == obj_id
    binding = ds["binding"]
    measure_ids = [m["ref"]["id"] for m in binding["measures"]]
    dim_ids = [d["id"] for d in binding["dimensions"]]
    assert amount_id in measure_ids
    assert channel_id in dim_ids
    # 复用口径 → 编译 SQL 与该口径一致
    assert "SUM(amount)" in (ds["compiled_sql"] or "")
    assert "GROUP BY channel" in (ds["compiled_sql"] or "")
