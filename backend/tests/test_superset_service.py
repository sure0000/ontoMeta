"""Superset 编排：数据集只从落点建、语义推过去、登记簿不冒充存在性。

这里不连真 Superset（连接器已在 ``test_superset_component.py`` 用 MockTransport 覆盖），
用一个记录调用的替身，把注意力放在**编排决策**上：

* 落点没建出来 / 引用认不出 → 当场拒绝，并说清下一步做什么；
* 建数据集是幂等的（已存在就复用），不会在 Superset 里堆同名数据集；
* 推语义时**整份列集合回传**——只带改动的那几列会把其余列删掉；
* 登记簿按 ``(类型, superset_id)`` 幂等；``state`` 只由"亲眼见过"或对账写入。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import SupersetAsset
from app.services import dataset_catalog
from app.services import superset_service as svc
from app.services.settings_service import SupersetRuntimeConfig
from app.services.superset_spec import ChartSpec, MetricSpec


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def clean_assets(db):
    """登记簿是全局表，用例之间互不干扰。"""
    yield
    for row in db.query(SupersetAsset).all():
        db.delete(row)
    db.commit()


CFG = SupersetRuntimeConfig(
    base_url="http://superset:8088",
    username="admin",
    password="pw",
    database_id=3,
    public_base_url="https://bi.example.com",
    enabled=True,
)


def _entry(*, state: str = dataset_catalog.LANDED, physical: str = "dws.ods_erp_order") -> dataset_catalog.DatasetEntry:
    return dataset_catalog.DatasetEntry(
        ref="obj:obj-1@serving",
        entity_kind=dataset_catalog.KIND_OBJECT,
        entity_id="obj-1",
        entity_name="order",
        entity_display_name="订单",
        slot=dataset_catalog.SLOT_SERVING,
        layer="dwd",
        physical=physical,
        state=state,
        queryable=True,
    )


class FakeClient:
    """记录调用的 Superset 替身。"""

    def __init__(self, *, existing_dataset: dict | None = None, columns: list[dict] | None = None):
        self.existing_dataset = existing_dataset
        self.columns = columns if columns is not None else [
            {"id": 1, "column_name": "order_id", "type": "BIGINT"},
            {"id": 2, "column_name": "amount", "type": "DECIMAL", "verbose_name": "手工填的"},
        ]
        self.created_datasets: list[tuple] = []
        self.updated_datasets: list[tuple[int, dict]] = []
        self.refreshed: list[int] = []
        self.created_charts: list[dict] = []
        self.updated_charts: list[tuple[int, dict]] = []
        self.created_dashboards: list[dict] = []
        self.embedded: list[tuple[int, list[str]]] = []
        self.alive = {"chart": [], "dashboard": [], "dataset": []}

    # --- dataset ---
    def find_dataset(self, database_id, schema, table):
        return self.existing_dataset

    def create_dataset(self, database_id, schema, table):
        self.created_datasets.append((database_id, schema, table))
        return {"id": 42}

    def refresh_dataset(self, dataset_id):
        self.refreshed.append(dataset_id)
        return {}

    def get_dataset(self, dataset_id):
        return {"result": {"columns": self.columns, "metrics": [{"metric_name": "count"}]}}

    def update_dataset(self, dataset_id, body):
        self.updated_datasets.append((dataset_id, body))
        return {}

    # --- chart / dashboard ---
    def create_chart(self, body):
        self.created_charts.append(body)
        return {"id": 77}

    def update_chart(self, chart_id, body):
        self.updated_charts.append((chart_id, body))
        return {}

    def create_dashboard(self, body):
        self.created_dashboards.append(body)
        return {"id": 5}

    def enable_embedded(self, dashboard_id, allowed_domains):
        self.embedded.append((dashboard_id, allowed_domains))
        return "uuid-abc"

    def guest_token(self, resources, user):
        return "guest-token"

    # --- 对账 ---
    def list_charts(self, page_size=100):
        return [{"id": i} for i in self.alive["chart"]]

    def list_dashboards(self, page_size=100):
        return [{"id": i} for i in self.alive["dashboard"]]

    def list_datasets(self, keyword=None, page_size=100):
        return [{"id": i} for i in self.alive["dataset"]]


# ------------------------------------------------------------------ 前置校验


def test_missing_database_id_points_back_at_settings(db, monkeypatch):
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    cfg = SupersetRuntimeConfig(**{**CFG.__dict__, "database_id": None})
    with pytest.raises(svc.SupersetNotConfigured, match="database_id"):
        svc.ensure_dataset(db, cfg, FakeClient(), "obj:obj-1@serving")


def test_unknown_ref_tells_you_where_to_look(db):
    with pytest.raises(ValueError, match="list_datasets"):
        svc.ensure_dataset(db, CFG, FakeClient(), "obj:does-not-exist@serving")


def test_landing_not_built_yet_is_refused(db, monkeypatch):
    """落点没建出来就建数据集，只会得到一个查不出数的数据集。"""
    monkeypatch.setattr(
        dataset_catalog, "resolve_dataset_ref", lambda *a: _entry(state="pending")
    )
    with pytest.raises(ValueError, match="落点还没建出来"):
        svc.ensure_dataset(db, CFG, FakeClient(), "obj:obj-1@serving")


# ------------------------------------------------------------------ 数据集


def test_creates_dataset_and_splits_the_physical_name(db, monkeypatch):
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    fake = FakeClient()
    result = svc.ensure_dataset(db, CFG, fake, "obj:obj-1@serving")
    assert fake.created_datasets == [(3, "dws", "ods_erp_order")]
    assert fake.refreshed == [42]  # 不刷新的话建图时选不到字段
    assert result["dataset_id"] == 42
    assert result["created"] is True
    assert result["url"].startswith("https://bi.example.com")


def test_existing_dataset_is_reused_not_duplicated(db, monkeypatch):
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    fake = FakeClient(existing_dataset={"id": 9})
    result = svc.ensure_dataset(db, CFG, fake, "obj:obj-1@serving")
    assert result["dataset_id"] == 9
    assert result["created"] is False
    assert fake.created_datasets == []


def test_semantics_push_sends_back_every_column(db, monkeypatch):
    """Superset 的 PUT 整体替换列集合：漏带的列会被删掉。"""
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    monkeypatch.setattr(
        svc,
        "_semantic_columns",
        lambda *a: {"order_id": {"verbose_name": "订单号", "description": "业务主键"}},
    )
    fake = FakeClient()
    result = svc.ensure_dataset(db, CFG, fake, "obj:obj-1@serving")

    assert result["semantics_pushed"] == 1
    (_, body), = fake.updated_datasets
    names = [c["column_name"] for c in body["columns"]]
    assert names == ["order_id", "amount"]  # 没匹配上的列也要原样回传
    assert body["columns"][0]["verbose_name"] == "订单号"
    assert body["columns"][1]["verbose_name"] == "手工填的"  # 别人手填的不能被抹掉


def test_no_semantics_means_no_write(db, monkeypatch):
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    monkeypatch.setattr(svc, "_semantic_columns", lambda *a: {})
    fake = FakeClient()
    svc.ensure_dataset(db, CFG, fake, "obj:obj-1@serving")
    assert fake.updated_datasets == []


# ------------------------------------------------------------------ 图表


def _spec() -> ChartSpec:
    return ChartSpec(
        name="各渠道订单量",
        viz="bar",
        dataset_id=42,
        dimensions=["channel"],
        metrics=[MetricSpec(aggregate="SUM", column="amount")],
    )


def test_chart_body_carries_params_and_query_context_as_strings(db):
    """Superset 收的是 JSON 字符串；传嵌套对象会被拒或存成坏值。"""
    fake = FakeClient()
    result = svc.create_chart(db, CFG, fake, _spec(), dataset_ref="obj:obj-1@serving")
    body = fake.created_charts[0]
    assert isinstance(body["params"], str)
    assert isinstance(body["query_context"], str)
    assert json.loads(body["query_context"])["form_data"] == json.loads(body["params"])
    assert body["datasource_type"] == "table"
    assert result["url"] == "https://bi.example.com/explore/?slice_id=77"


def test_updating_a_chart_updates_the_same_registry_row(db):
    fake = FakeClient()
    svc.create_chart(db, CFG, fake, _spec())
    svc.update_chart(db, CFG, fake, 77, ChartSpec(**{**_spec().__dict__, "viz": "line"}))

    rows = db.query(SupersetAsset).filter(SupersetAsset.asset_type == "chart").all()
    assert len(rows) == 1  # 另起一行会让同一张图在平台上出现两次
    assert rows[0].viz_type == "line"


# ------------------------------------------------------------------ 看板


def test_dashboard_places_every_chart_in_the_layout(db):
    fake = FakeClient()
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7, 8])
    position = json.loads(fake.created_dashboards[0]["position_json"])
    assert {"CHART-7", "CHART-8"} <= set(position)
    assert result["embedded_uuid"] == "uuid-abc"


def test_dashboard_survives_embedding_being_off(db, monkeypatch):
    """没开 EMBEDDED_SUPERSET 只是不能内嵌，跳转链接照样可用——不该整个失败。"""
    from app.connectors.superset import SupersetError

    fake = FakeClient()
    monkeypatch.setattr(
        fake, "enable_embedded",
        lambda *a: (_ for _ in ()).throw(SupersetError("enable_embedded", "HTTP 404")),
    )
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7])
    assert result["dashboard_id"] == 5
    assert result["embedded_uuid"] is None
    assert "404" in result["embed_error"]


def test_empty_dashboard_is_refused(db):
    with pytest.raises(ValueError, match="至少要放一张图"):
        svc.create_dashboard(db, CFG, FakeClient(), "空看板", [])


def test_guest_token_only_for_dashboards(db):
    fake = FakeClient()
    svc.create_chart(db, CFG, fake, _spec())
    chart = db.query(SupersetAsset).filter(SupersetAsset.asset_type == "chart").one()
    with pytest.raises(ValueError, match="只有看板"):
        svc.guest_token(db, CFG, fake, chart.id, username="alice")


def test_guest_token_backfills_the_embed_uuid(db):
    fake = FakeClient()
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7])
    row = db.get(SupersetAsset, result["asset_id"])
    row.embedded_uuid = None
    db.commit()

    assert svc.guest_token(db, CFG, fake, row.id, username="alice") == "guest-token"
    db.refresh(row)
    assert row.embedded_uuid == "uuid-abc"


# ------------------------------------------------------------------ 登记簿


def test_registering_the_same_object_twice_updates_one_row(db):
    svc.register_asset(db, asset_type="chart", superset_id=1, title="旧名")
    svc.register_asset(db, asset_type="chart", superset_id=1, title="新名")
    rows = db.query(SupersetAsset).all()
    assert len(rows) == 1
    assert rows[0].title == "新名"


def test_reconcile_marks_missing_without_deleting(db):
    """对账只改状态：登记行本身回答了"我们当时建了什么"，删掉就没人能回答了。"""
    svc.register_asset(db, asset_type="chart", superset_id=1, title="还在")
    svc.register_asset(db, asset_type="chart", superset_id=2, title="被人删了")
    fake = FakeClient()
    fake.alive["chart"] = [1]

    counts = svc.reconcile(db, CFG, fake)
    assert counts == {"active": 1, "missing": 1}
    states = {r.superset_id: r.state for r in db.query(SupersetAsset).all()}
    assert states == {1: "active", 2: "missing"}


def test_unlink_does_not_touch_superset(db):
    asset = svc.register_asset(db, asset_type="dashboard", superset_id=5, title="看板")
    assert svc.unlink(db, asset.id) is True
    assert db.query(SupersetAsset).count() == 0
    assert svc.unlink(db, "nope") is False
