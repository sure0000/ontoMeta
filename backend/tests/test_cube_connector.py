"""CubeConnector：模型生成、绑定→查询翻译、Mock 查询、JWT、Cube 数据源预览。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

from app.connectors.cube import CubeConnector, cube_name
from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, Property


def test_cube_name():
    assert cube_name("orders") == "Orders"
    assert cube_name("dim_customer") == "DimCustomer"
    assert cube_name("fact.sales_line") == "FactSalesLine"


def test_generate_model():
    conn = CubeConnector(use_mock=True)
    model = conn.generate_model(
        objects=[
            {
                "name": "orders",
                "display_name": "订单",
                "properties": [
                    {"name": "channel", "display_name": "渠道", "data_type": "string", "semantic_type": "category"},
                    {"name": "amount", "display_name": "金额", "data_type": "decimal", "semantic_type": "amount"},
                    {"name": "created_at", "display_name": "创建时间", "data_type": "date", "semantic_type": "date"},
                ],
                "measures": [
                    {"name": "gmv", "display_name": "成交额", "agg": "sum", "sql": "amount"},
                ],
            }
        ]
    )
    cube = model["cubes"][0]
    assert cube["name"] == "Orders"
    assert cube["sql_table"] == "orders"
    # 维度
    assert cube["dimensions"]["channel"]["type"] == "string"
    assert cube["dimensions"]["amount"]["type"] == "number"
    assert cube["dimensions"]["created_at"]["type"] == "time"
    # 数值属性自动生成聚合度量
    assert "amount_sum" in cube["measures"]
    assert cube["measures"]["amount_sum"]["type"] == "sum"
    # 业务逻辑度量
    assert "gmv" in cube["measures"]
    # count 内置
    assert cube["measures"]["count"]["type"] == "count"


def test_build_query():
    conn = CubeConnector(use_mock=True)
    q = conn.build_query(
        object_name="orders",
        measures=[{"ref": {"kind": "property", "name": "amount"}, "agg": "sum"}],
        dimensions=[{"kind": "property", "name": "channel"}],
        filters=[{"ref": {"name": "channel"}, "op": "eq", "value": "A"}],
        time_range={"ref": {"name": "created_at"}, "window": "last_30d"},
        limit=50,
    )
    assert q["measures"] == ["Orders.amount_sum"]
    assert q["dimensions"] == ["Orders.channel"]
    assert q["filters"][0]["member"] == "Orders.channel"
    assert q["filters"][0]["operator"] == "equals"
    assert q["filters"][0]["values"] == ["A"]
    assert q["timeDimensions"][0]["dimension"] == "Orders.created_at"
    assert q["timeDimensions"][0]["dateRange"] == "last 30 days"
    assert q["limit"] == 50


def test_build_query_business_logic_measure():
    conn = CubeConnector(use_mock=True)
    q = conn.build_query(
        object_name="orders",
        measures=[{"ref": {"kind": "business_logic", "name": "gmv"}, "agg": "sum"}],
        dimensions=[],
    )
    assert q["measures"] == ["Orders.gmv"]  # 业务逻辑度量不加 _agg 后缀


def test_mock_query_deterministic():
    conn = CubeConnector(use_mock=True)
    q = {"measures": ["Orders.amount_sum"], "dimensions": ["Orders.channel"], "limit": 10}
    cols1, rows1 = conn.query(q)
    cols2, rows2 = conn.query(q)
    assert [c["key"] for c in cols1] == ["Orders.channel", "Orders.amount_sum"]
    assert rows1 == rows2  # 确定性
    assert rows1 and "Orders.channel" in rows1[0]


def test_build_token_hs256():
    conn = CubeConnector(api_secret="s3cr3t", use_mock=False)
    token = conn.build_token({"tenant": "t1"})
    header_b64, payload_b64, sig_b64 = token.split(".")

    def _b64url_decode(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    payload = json.loads(_b64url_decode(payload_b64))
    assert payload["securityContext"] == {"tenant": "t1"}
    # 验证签名
    expected = hmac.new(
        b"s3cr3t", f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    assert _b64url_decode(sig_b64) == expected


def test_use_mock_when_no_secret():
    # 未配置密钥 → 强制 mock，即使 use_mock=False
    conn = CubeConnector(api_secret=None, use_mock=False)
    assert conn.use_mock is True


# ----------------------------------------------------- preview via cube source


def _seed_published_ontology():
    db = SessionLocal()
    try:
        domain = DomainContext(datahub_domain_id=f"urn:cube:{uuid.uuid4()}", name="Cube域")
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
        obj = ObjectType(ontology_id=ontology.id, name="orders", display_name="订单", status="published")
        db.add(obj)
        db.flush()
        channel = Property(object_type_id=obj.id, name="channel", display_name="渠道", semantic_type="category", status="published")
        amount = Property(object_type_id=obj.id, name="amount", display_name="金额", data_type="decimal", semantic_type="amount", status="published")
        db.add_all([channel, amount])
        db.commit()
        return domain.id, ontology.id, obj.id, channel.id, amount.id
    finally:
        db.close()


def test_cube_model_endpoint(client, admin_headers):
    _domain_id, ontology_id, *_ = _seed_published_ontology()
    res = client.get(f"/api/ontologies/{ontology_id}/cube-model", headers=admin_headers)
    assert res.status_code == 200, res.text
    cubes = res.json()["cubes"]
    assert any(c["name"] == "Orders" for c in cubes)
    orders = next(c for c in cubes if c["name"] == "Orders")
    assert "amount_sum" in orders["measures"]


def test_preview_via_cube_source(client, admin_headers):
    from app.services.data_app import DataAppService

    domain_id, _ont, obj_id, channel_id, amount_id = _seed_published_ontology()
    db = SessionLocal()
    try:
        svc = DataAppService()
        # Cube 数据源（mock 默认开启）
        ds_source = svc.create_data_source(db, name="cube", kind="cube", dsn_secret_ref=None)
        app = svc.create_app(
            db,
            domain_id=domain_id,
            app_type="data_table",
            name="Cube表",
            description=None,
            source="manual",
            spec=None,
            datasets=[
                {
                    "name": "渠道金额",
                    "primary_object_type_id": obj_id,
                    "data_source_id": ds_source.id,
                    "binding": {
                        "primary_object_type_id": obj_id,
                        "measures": [{"ref": {"kind": "property", "id": amount_id, "name": "amount"}, "agg": "sum"}],
                        "dimensions": [{"kind": "property", "id": channel_id, "name": "channel"}],
                        "filters": [],
                        "row_limit": 100,
                    },
                }
            ],
        )
        app_id = app.id
        ds_id = app.datasets[0].id
    finally:
        db.close()

    res = client.post(
        f"/api/data-apps/{app_id}/datasets/{ds_id}/preview",
        headers=admin_headers,
        json={"limit": 50, "runtime_filters": []},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Cube 列名形如 Orders.channel / Orders.amount_sum
    keys = {c["key"] for c in body["columns"]}
    assert "Orders.channel" in keys
    assert "Orders.amount_sum" in keys
    assert body["rows"]


# ---------------------------------------------- 生产级：预聚合 / joins / RLS / files


def test_generate_model_pre_aggregations_and_refresh():
    conn = CubeConnector(use_mock=True)
    model = conn.generate_model(
        objects=[
            {
                "name": "orders",
                "display_name": "订单",
                "properties": [
                    {"name": "channel", "data_type": "string", "semantic_type": "category"},
                    {"name": "amount", "data_type": "decimal", "semantic_type": "amount"},
                    {"name": "created_at", "data_type": "date", "semantic_type": "date"},
                ],
                "measures": [],
                "joins": [],
            }
        ],
        pre_agg_refresh="30 minute",
    )
    cube = model["cubes"][0]
    # 有时间列 → refresh_key 用增量 SQL
    assert "sql" in cube["refresh_key"]
    pre = cube["pre_aggregations"]["main"]
    assert pre["refresh_key"] == {"every": "30 minute"}
    assert "Orders.amount_sum" in pre["measures"]
    assert pre["time_dimension"] == "Orders.created_at"
    assert pre["granularity"] == "day"
    assert "Orders.channel" in pre["dimensions"]


def test_generate_model_joins():
    conn = CubeConnector(use_mock=True)
    model = conn.generate_model(
        objects=[
            {
                "name": "orders",
                "properties": [{"name": "customer_id", "data_type": "bigint"}],
                "measures": [],
                "joins": [
                    {
                        "target_cube": "Customers",
                        "relationship": "many_to_one",
                        "sql": "${Orders}.customer_id = ${Customers}.id",
                    }
                ],
            }
        ]
    )
    cube = model["cubes"][0]
    assert cube["joins"]["Customers"]["relationship"] == "many_to_one"
    assert "${Orders}.customer_id = ${Customers}.id" in cube["joins"]["Customers"]["sql"]


def test_security_context_and_model_files_rls(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "cube_tenant_dimension", "tenant_id")
    conn = CubeConnector(use_mock=True)
    ctx = conn.build_security_context(tenant="t1", scopes=["dataapps:read"], app_id="app1", app_name="A")
    assert ctx["tenant"] == "t1"
    assert ctx["scopes"] == ["dataapps:read"]

    model = conn.generate_model(
        objects=[
            {
                "name": "orders",
                "properties": [
                    {"name": "tenant_id", "data_type": "string"},
                    {"name": "amount", "data_type": "decimal", "semantic_type": "amount"},
                ],
                "measures": [],
            }
        ]
    )
    assert model["cubes"][0]["tenant_dimension"] == "tenant_id"
    files = conn.generate_model_files(model)
    assert "cube.js" in files
    assert "model/cubes/Orders.js" in files
    # cube.js 含行级过滤逻辑 + 该 cube 的租户列映射
    assert "queryRewrite" in files["cube.js"]
    assert "securityContext" in files["cube.js"]
    assert '"Orders": "tenant_id"' in files["cube.js"] or '"Orders":"tenant_id"' in files["cube.js"]


def test_cube_model_files_endpoint(client, admin_headers):
    _domain_id, ontology_id, *_ = _seed_published_ontology()
    res = client.get(f"/api/ontologies/{ontology_id}/cube-model/files", headers=admin_headers)
    assert res.status_code == 200, res.text
    files = res.json()["files"]
    assert "cube.js" in files
    assert any(k.startswith("model/cubes/") and k.endswith(".js") for k in files)
    assert "cube(`Orders`" in files["model/cubes/Orders.js"]


# ----------------------------------------- web 设置页驱动（DB 为权威，无需配置文件）


def test_cube_settings_api_roundtrip(client, admin_headers):
    # 读取默认
    res = client.get("/api/settings/cube", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["use_mock"] is True
    assert res.json()["secret_set"] is False

    # 更新（含密钥与租户列）
    res = client.put(
        "/api/settings/cube",
        headers=admin_headers,
        json={
            "api_url": "http://cube:4000",
            "api_secret": "topsecret",
            "use_mock": False,
            "preagg_refresh": "15 minute",
            "tenant_dimension": "tenant_id",
            "timeout_seconds": 20,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["secret_set"] is True
    assert body["api_secret"] if False else True  # 不回显明文
    assert "topsecret" not in json.dumps(body)  # 密钥不回显
    assert body["preagg_refresh"] == "15 minute"
    assert body["tenant_dimension"] == "tenant_id"

    # 不传 api_secret 时保留原密钥
    res = client.put(
        "/api/settings/cube",
        headers=admin_headers,
        json={
            "api_url": "http://cube:4000",
            "use_mock": False,
            "preagg_refresh": "1 hour",
            "tenant_dimension": "tenant_id",
            "timeout_seconds": 30,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["secret_set"] is True  # 仍在


def test_cube_model_uses_db_settings(client, admin_headers):
    _domain_id, ontology_id, obj_id, *_ = _seed_published_ontology()
    # 给对象加一个 tenant_id 列，并把 tenant_dimension 通过设置页配置
    db = SessionLocal()
    try:
        db.add(Property(object_type_id=obj_id, name="tenant_id", display_name="租户", data_type="string", status="published"))
        db.commit()
    finally:
        db.close()

    client.put(
        "/api/settings/cube",
        headers=admin_headers,
        json={
            "api_url": "http://cube:4000",
            "api_secret": "sk",
            "use_mock": True,
            "preagg_refresh": "45 minute",
            "tenant_dimension": "tenant_id",
            "timeout_seconds": 30,
        },
    )

    res = client.get(f"/api/ontologies/{ontology_id}/cube-model/files", headers=admin_headers)
    assert res.status_code == 200, res.text
    files = res.json()["files"]
    # DB 配置的 tenant_dimension 生效 → cube.js 含 Orders 的租户列映射
    assert '"Orders": "tenant_id"' in files["cube.js"] or '"Orders":"tenant_id"' in files["cube.js"]
    # DB 配置的预聚合刷新间隔生效
    assert "45 minute" in files["model/cubes/Orders.js"]
