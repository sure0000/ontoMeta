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

from app.connectors.superset import SupersetError
from app.database import SessionLocal
from app.models import DataSource, SupersetAsset
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
    public_base_url="https://bi.example.com",
    enabled=True,
)

# 落点所在的数据源。挂哪条 Superset database（连接）由它决定，不再由全局设置决定。
DS_ID = "ds-superset-service-test"


@pytest.fixture(autouse=True)
def warehouse_ds(db):
    row = DataSource(
        id=DS_ID,
        name="测试 Doris",
        kind="doris",
        purpose="warehouse",
        dsn_secret_ref="mysql://u:p@doris-fe:9030/dws",
        superset_database_id=3,
    )
    db.add(row)
    db.commit()
    yield row
    db.delete(row)
    db.commit()


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
        datasource_id=DS_ID,
    )


class FakeClient:
    """记录调用的 Superset 替身。"""

    def __init__(self, *, existing_dataset: dict | None = None, columns: list[dict] | None = None,
                 databases: list[dict] | None = None):
        self.existing_dataset = existing_dataset
        self.databases = databases or []
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
        self.chart_dashboards: dict[int, list[int]] = {}
        self.alive = {"chart": [], "dashboard": [], "dataset": []}

    # --- database（连接）---
    def list_databases(self, page_size=100):
        return [{"id": d["id"], "database_name": d.get("database_name")} for d in self.databases]

    def get_database(self, database_id):
        for d in self.databases:
            if d["id"] == int(database_id):
                return {"result": d}
        raise SupersetError("get_database", f"HTTP 404 {database_id}")

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
        if "dashboards" in body:
            self.chart_dashboards[int(chart_id)] = list(body["dashboards"])
        return {}

    def get_chart(self, chart_id):
        return {
            "result": {
                "id": int(chart_id),
                "dashboards": [
                    {"id": d} for d in self.chart_dashboards.get(int(chart_id), [])
                ],
            }
        }

    def create_dashboard(self, body):
        self.created_dashboards.append(body)
        return {"id": 5}

    # --- 对账 ---
    def list_charts(self, page_size=100):
        return [{"id": i} for i in self.alive["chart"]]

    def list_dashboards(self, page_size=100):
        return [{"id": i} for i in self.alive["dashboard"]]

    def list_datasets(self, keyword=None, page_size=100):
        return [{"id": i} for i in self.alive["dataset"]]


# ------------------------------------------------------------------ 前置校验


def test_unresolvable_database_points_back_at_the_datasource(db, monkeypatch, warehouse_ds):
    """Superset 里没有指向这个落点数据源的连接时，是"没配好"，不是"调用失败"。"""
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    warehouse_ds.superset_database_id = None  # 清掉缓存，逼它真去比对
    db.commit()
    with pytest.raises(svc.SupersetNotConfigured, match="没有指向"):
        svc.ensure_dataset(db, CFG, FakeClient(databases=[]), "obj:obj-1@serving")


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
    # 3 是这条落点的数据源在 Superset 里的连接编号，不是某个全局设置。
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
    assert result["url"] == "https://bi.example.com/explore/?slice_id=77&standalone=1"


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


def test_dashboard_also_links_every_chart_to_itself(db):
    """放进布局还不够，得真的关联上。

    真机上验过：只写 position_json 建出来的看板，``/dashboard/{id}/charts`` 与
    ``/datasets`` 都是空的（内置示例看板两者都非空），打开就是一张白板。
    关联只能从图那一侧写——看板的 schema 不收 slices。
    """
    fake = FakeClient()
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7, 8])
    assert fake.chart_dashboards == {7: [5], 8: [5]}
    assert result["unlinked_chart_ids"] == []


def test_linking_keeps_the_charts_other_dashboards(db):
    """``PUT /chart`` 的 dashboards 是整体替换：直接写会把图从原看板上摘下来。"""
    fake = FakeClient()
    fake.chart_dashboards[7] = [3]
    svc.create_dashboard(db, CFG, fake, "销售看板", [7])
    assert fake.chart_dashboards[7] == [3, 5]


def test_charts_that_fail_to_link_are_reported_not_swallowed(db, monkeypatch):
    """挂不上的图必须回上去。回执说"完成"、用户点开是空白，比报错更难查。"""
    from app.connectors.superset import SupersetError

    fake = FakeClient()
    monkeypatch.setattr(
        fake, "get_chart",
        lambda cid: (_ for _ in ()).throw(SupersetError("get_chart", "HTTP 404")),
    )
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7, 8])
    assert result["unlinked_chart_ids"] == [7, 8]
    assert result["dashboard_id"] == 5  # 看板还在，别让调用方重建一个


def test_empty_dashboard_is_refused(db):
    with pytest.raises(ValueError, match="至少要放一张图"):
        svc.create_dashboard(db, CFG, FakeClient(), "空看板", [])


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


# ------------------------------------------------------------- 跳转地址


def test_links_jump_into_superset_fullscreen(db):
    """点过去是为了看这一个东西，不该带着 Superset 的全局导航。

    ``standalone`` 的取值是 Superset 的约定：1 去导航，2 再去标题栏，3 连筛选栏也去掉。
    两边都用 1：看板要留标题栏（人得知道自己在看哪张看板），图表要留 explore 的控制面板。

    「不可编辑」**不归这个参数管**——标题栏和编辑按钮是同一行，只能整行留或整行去，
    而且它只隐藏 UI，删掉参数就恢复。只读靠角色（没有 ``can_write on Dashboard``）。
    """
    fake = FakeClient()
    dash = svc.create_dashboard(db, CFG, fake, "销售看板", [7])
    assert dash["url"] == "https://bi.example.com/superset/dashboard/5/?standalone=1"

    chart = svc.create_chart(db, CFG, fake, _spec())
    assert chart["url"].endswith("?slice_id=77&standalone=1")


def test_fullscreen_param_is_added_at_render_not_stored(db):
    """``url_path`` 存的是"它在 Superset 里的位置"，全屏与否是展示口径。

    分开之后，改口径不用回头 backfill 已登记的行——这也是**存量**资产也能全屏打开的原因。
    """
    fake = FakeClient()
    result = svc.create_dashboard(db, CFG, fake, "销售看板", [7])
    row = db.get(SupersetAsset, result["asset_id"])
    assert row.url_path == "/superset/dashboard/5/"  # 库里不带参数
    assert svc.serialize_asset(row, CFG)["url"].endswith("?standalone=1")


def test_dataset_links_stay_plain(db):
    """数据集指向的是 Superset 的管理页，全屏没有意义。"""
    row = svc.register_asset(
        db, asset_type="dataset", superset_id=9, title="订单表",
        url_path="/tablemodelview/edit/9",
    )
    assert svc.serialize_asset(row, CFG)["url"] == "https://bi.example.com/tablemodelview/edit/9"


# ------------------------------------------------------------- 列表序列化


def test_asset_list_resolves_refs_into_readable_landings(db, monkeypatch):
    """列表里摆一个 `obj:068504b9-…@serving` 等于没说。

    句柄留给任务配置和 Agent，人要看的是实体名与物理表。
    """
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: _entry())
    row = svc.register_asset(
        db, asset_type="chart", superset_id=31, title="订单量",
        dataset_ref="obj:obj-1@serving",
    )
    landing = svc.serialize_assets(db, [row], CFG)[0]["landing"]
    assert landing["entity_display_name"] == "订单"
    assert landing["physical"] == "dws.ods_erp_order"


def test_unresolvable_ref_is_not_silently_flattened_to_no_landing(db, monkeypatch):
    """引用解析不出来（实体被删/降级）跟"本来就没登记落点"是两回事。

    前者说明这张图的口径断了，后者只是没接治理。压成同一个「—」会把前者藏起来。
    """
    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", lambda *a: None)
    broken = svc.register_asset(
        db, asset_type="chart", superset_id=32, title="断的", dataset_ref="obj:gone@serving",
    )
    plain = svc.register_asset(db, asset_type="chart", superset_id=33, title="没落点")
    items = svc.serialize_assets(db, [broken, plain], CFG)
    assert items[0]["dataset_ref"] and items[0]["landing"] is None  # 断了
    assert items[1]["dataset_ref"] is None and items[1]["landing"] is None  # 本来就没有


def test_same_ref_is_resolved_once(db, monkeypatch):
    """一个落点上通常挂着好几张图，逐行解析是白跑。"""
    calls: list[str] = []

    def _resolve(_db, ref):
        calls.append(ref)
        return _entry()

    monkeypatch.setattr(dataset_catalog, "resolve_dataset_ref", _resolve)
    rows = [
        svc.register_asset(
            db, asset_type="chart", superset_id=40 + i, title=f"图{i}",
            dataset_ref="obj:obj-1@serving",
        )
        for i in range(3)
    ]
    svc.serialize_assets(db, rows, CFG)
    assert calls == ["obj:obj-1@serving"]
