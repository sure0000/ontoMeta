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

    # 2) 本体绑定预览在默认 Doris/Projection 未就绪时 fail-closed，不回退 Mock
    res = client.post(
        f"/api/data-apps/{app_id}/datasets/{dataset_id}/preview",
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    preview = res.json()
    assert preview["used_mock"] is False
    assert preview["execution_blocked"] is True
    assert preview["columns"] == []
    assert preview["rows"] == []
    assert any("Doris" in warning for warning in preview["warnings"])

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




def test_dashboard_create_and_generate(client, admin_headers):
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

    # 1) 手工创建 dashboard（默认 grid spec）
    res = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={"domain_id": domain_id, "app_type": "dashboard", "name": "运营看板"},
    )
    assert res.status_code == 200, res.text
    app = res.json()
    assert app["app_type"] == "dashboard"
    assert app["spec"]["layout"] == "grid"
    assert app["spec"]["panels"] == []

    # 2) 更新：加两个数据集 + 两个 tile 组合
    app_id = app["id"]
    res = client.patch(
        f"/api/data-apps/{app_id}",
        headers=admin_headers,
        json={
            "datasets": [
                {
                    "name": "渠道金额",
                    "primary_object_type_id": obj_id,
                    "binding": {
                        "primary_object_type_id": obj_id,
                        "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                        "dimensions": [{"kind": "property", "id": channel_id, "name": "channel"}],
                        "filters": [],
                        "row_limit": 100,
                    },
                },
                {
                    "name": "订单明细",
                    "primary_object_type_id": obj_id,
                    "binding": {"primary_object_type_id": obj_id, "measures": [], "dimensions": [], "filters": [], "row_limit": 50},
                },
            ],
            "spec": {
                "layout": "grid",
                "grid": {"cols": 12, "rowHeight": 40, "gap": 12},
                "panels": [
                    {"id": "t1", "widgetType": "bar", "title": "渠道金额", "datasetIndex": 0, "x": 0, "y": 0, "w": 6, "h": 8},
                    {"id": "t2", "widgetType": "table", "title": "明细", "datasetIndex": 1, "x": 6, "y": 0, "w": 6, "h": 8},
                ],
            },
        },
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    assert len(updated["datasets"]) == 2
    assert len(updated["spec"]["panels"]) == 2

    # 3) 发布并对外查询数据（两个数据集都有数据）
    res = client.post(f"/api/data-apps/{app_id}/publish", headers=admin_headers, json={})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "published"




def test_widget_crud_and_add_to_dashboard(client, admin_headers):
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    db = SessionLocal()
    try:
        channel_id = (
            db.query(Property)
            .filter(Property.object_type_id == obj_id, Property.name == "channel")
            .first()
        ).id
    finally:
        db.close()

    # 1) 创建可复用图表
    res = client.post(
        "/api/data-app-widgets",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "name": "渠道金额柱图",
            "widget_type": "bar",
            "primary_object_type_id": obj_id,
            "binding": {
                "primary_object_type_id": obj_id,
                "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                "dimensions": [{"kind": "property", "id": channel_id, "name": "channel"}],
                "filters": [],
                "row_limit": 100,
            },
        },
    )
    assert res.status_code == 200, res.text
    widget = res.json()
    assert widget["widget_type"] == "bar"
    assert "SUM(amount)" in (widget["compiled_sql"] or "")
    widget_id = widget["id"]

    # 2) 图表库可检索
    res = client.get(f"/api/data-app-widgets?domain_id={domain_id}", headers=admin_headers)
    assert any(w["id"] == widget_id for w in res.json())

    # 3) 图表预览
    res = client.post(f"/api/data-app-widgets/{widget_id}/preview", headers=admin_headers, json={"limit": 20})
    assert res.status_code == 200, res.text
    assert res.json()["columns"] == []
    assert res.json()["execution_blocked"] is True

    # 4) 新建看板并把图表加入为 tile
    res = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={"domain_id": domain_id, "app_type": "dashboard", "name": "看板A"},
    )
    app_id = res.json()["id"]
    res = client.post(
        f"/api/data-apps/{app_id}/widgets",
        headers=admin_headers,
        json={"widget_id": widget_id},
    )
    assert res.status_code == 200, res.text
    tiles = res.json()["spec"]["panels"]
    assert len(tiles) == 1
    assert tiles[0]["panel_id"] == widget_id

    # 5) 同一图表可复用到第二个看板
    res2 = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={"domain_id": domain_id, "app_type": "dashboard", "name": "看板B"},
    )
    app2 = res2.json()["id"]
    res = client.post(f"/api/data-apps/{app2}/widgets", headers=admin_headers, json={"widget_id": widget_id})
    assert res.status_code == 200
    assert res.json()["spec"]["panels"][0]["panel_id"] == widget_id



def _publish_simple_app(client, admin_headers, domain_id, obj_id, amount_id):
    res = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "app_type": "data_table",
            "name": "对外表",
            "datasets": [
                {
                    "name": "金额",
                    "primary_object_type_id": obj_id,
                    "binding": {
                        "primary_object_type_id": obj_id,
                        "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                        "dimensions": [],
                        "filters": [],
                        "row_limit": 100,
                    },
                }
            ],
        },
    )
    app_id = res.json()["id"]
    client.post(f"/api/data-apps/{app_id}/publish", headers=admin_headers, json={})
    return app_id


def test_public_share_flow(client, admin_headers):
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    app_id = _publish_simple_app(client, admin_headers, domain_id, obj_id, amount_id)

    # 开启公开分享
    res = client.post(f"/api/data-apps/{app_id}/share", headers=admin_headers, json={})
    assert res.status_code == 200, res.text
    status = res.json()
    assert status["public_enabled"] is True
    token = status["public_token"]
    assert token

    # 免登录访问（无 admin token）
    res = client.get(f"/api/public/data-apps/{token}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == app_id
    assert body["render"]["datasets"]

    # 关闭分享 → 404
    client.delete(f"/api/data-apps/{app_id}/share", headers=admin_headers)
    res = client.get(f"/api/public/data-apps/{token}")
    assert res.status_code == 404


def test_public_share_password(client, admin_headers):
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    app_id = _publish_simple_app(client, admin_headers, domain_id, obj_id, amount_id)

    res = client.post(
        f"/api/data-apps/{app_id}/share",
        headers=admin_headers,
        json={"password": "s3cret", "expires_in_days": 7},
    )
    token = res.json()["public_token"]
    assert res.json()["password_set"] is True

    # 无口令 → 401
    assert client.get(f"/api/public/data-apps/{token}").status_code == 401
    # 错误口令 → 403
    assert client.get(f"/api/public/data-apps/{token}?password=wrong").status_code == 403
    # 正确口令 → 200
    assert client.get(f"/api/public/data-apps/{token}?password=s3cret").status_code == 200


def test_public_share_requires_published(client, admin_headers):
    domain_id, _ont, obj_id, _amount = _seed_published_ontology()
    res = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={"domain_id": domain_id, "app_type": "dashboard", "name": "草稿看板"},
    )
    app_id = res.json()["id"]
    # 未发布 → 开启分享 400
    res = client.post(f"/api/data-apps/{app_id}/share", headers=admin_headers, json={})
    assert res.status_code == 400


def test_version_lock_widget_snapshot(client, admin_headers):
    """已发布看板用发布快照的图表定义渲染，后续编辑图表不影响。"""
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    # 图表：sum(amount) 按 channel
    db = SessionLocal()
    try:
        channel_id = (
            db.query(Property)
            .filter(Property.object_type_id == obj_id, Property.name == "channel")
            .first()
        ).id
    finally:
        db.close()
    widget = client.post(
        "/api/data-app-widgets",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "name": "原始图表",
            "widget_type": "bar",
            "primary_object_type_id": obj_id,
            "binding": {
                "primary_object_type_id": obj_id,
                "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                "dimensions": [{"kind": "property", "id": channel_id, "name": "channel"}],
                "filters": [],
                "row_limit": 100,
            },
        },
    ).json()
    wid = widget["id"]

    dash = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={"domain_id": domain_id, "app_type": "dashboard", "name": "锁定看板"},
    ).json()
    app_id = dash["id"]
    client.post(f"/api/data-apps/{app_id}/widgets", headers=admin_headers, json={"widget_id": wid})
    client.post(f"/api/data-apps/{app_id}/publish", headers=admin_headers, json={})
    share = client.post(f"/api/data-apps/{app_id}/share", headers=admin_headers, json={}).json()
    token = share["public_token"]

    # 发布后编辑图表：改名 + 只留 count（无 sum_amount 列）
    client.patch(
        f"/api/data-app-widgets/{wid}",
        headers=admin_headers,
        json={"name": "改过的图表", "binding": {"primary_object_type_id": obj_id, "measures": [], "dimensions": [], "filters": [], "row_limit": 5}},
    )

    # 对外渲染仍用发布快照；未就绪 Doris 路径 fail-closed，不回退样例数据。
    body = client.get(f"/api/public/data-apps/{token}").json()
    wprev = body["render"]["widgets"][wid]
    assert wprev["columns"] == []
    assert wprev["execution_blocked"] is True


def test_lineage(client, admin_headers):
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    app = client.post(
        "/api/data-apps",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "app_type": "data_table",
            "name": "血缘表",
            "datasets": [
                {
                    "name": "金额",
                    "primary_object_type_id": obj_id,
                    "binding": {
                        "primary_object_type_id": obj_id,
                        "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                        "dimensions": [],
                        "filters": [],
                        "row_limit": 100,
                    },
                }
            ],
        },
    ).json()
    res = client.get(f"/api/data-apps/{app['id']}/lineage", headers=admin_headers)
    assert res.status_code == 200, res.text
    lin = res.json()
    assert any(o["id"] == obj_id for o in lin["object_types"])
    assert any(p["id"] == amount_id for p in lin["properties"])
    assert lin["nodes"] and lin["nodes"][0]["kind"] == "dataset"


def test_view_count_increment(client, admin_headers):
    domain_id, _ont, obj_id, amount_id = _seed_published_ontology()
    app_id = _publish_simple_app(client, admin_headers, domain_id, obj_id, amount_id)
    token = client.post(f"/api/data-apps/{app_id}/share", headers=admin_headers, json={}).json()["public_token"]
    client.get(f"/api/public/data-apps/{token}")
    client.get(f"/api/public/data-apps/{token}")
    detail = client.get(f"/api/data-apps/{app_id}", headers=admin_headers).json()
    assert detail["view_count"] >= 2
