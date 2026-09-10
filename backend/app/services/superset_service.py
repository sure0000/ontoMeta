"""Superset 编排：把治理好的落点与图表规格推过去，把 id 与链接登记回来。

分工是刻意的：
* **Superset 负责呈现**——图怎么画、怎么存、怎么分享，全在那边，ontoMeta 不复制一份。
* **ontoMeta 负责口径**——数据集只能从**已发布本体的落点**建（``dataset_ref``），
  顺带把本体的中文名与描述推成 Superset 的 ``verbose_name``/``description``。
  这一步是"图表不脱离治理"的落点，也是不让 Agent 在裸表上乱画的全部理由。

登记簿（``SupersetAsset``）只是索引：``state`` 由显式对账写入，绝不拿本地行冒充存在性。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.superset import SupersetClient, SupersetError
from app.models import BusinessLogic, ObjectType, Ontology, Property, SupersetAsset
from app.services import dataset_catalog
from app.services.settings_service import SettingsService, SupersetRuntimeConfig
from app.services.superset_database import (
    SupersetDatabaseUnresolved,
    resolve_database_id,
)
from app.services.superset_spec import ChartSpec, build_position_json, compile_chart

_settings = SettingsService()


class SupersetNotConfigured(RuntimeError):
    """组件没启用/没配全。与"调用失败"分开：这条要把人指回设置页，而不是让人查网络。"""


def runtime(db: Session) -> SupersetRuntimeConfig:
    cfg = _settings.get_superset_runtime(db)
    if not cfg.enabled:
        raise SupersetNotConfigured(
            "Superset 组件未启用：请在设置页「基础设施」填好连接、拨测通过后启用"
        )
    if not cfg.configured:
        raise SupersetNotConfigured("Superset 连接不完整：缺少地址或账号密码")
    return cfg


def client(cfg: SupersetRuntimeConfig) -> SupersetClient:
    return SupersetClient(cfg.base_url, username=cfg.username, password=cfg.password)


def public_url(cfg: SupersetRuntimeConfig, url_path: str) -> str:
    """拼用户点得开的地址。用 ``public_base_url`` 而不是后端访问地址。"""
    return f"{cfg.public_base_url}{url_path}" if url_path else cfg.public_base_url


#: 跳转到 Superset 时的全屏参数。取值是 Superset 的 ``standalone`` 约定：
#: ``1`` 去掉顶部导航，``2`` 再去掉标题栏，``3`` 连筛选栏一起去掉。
#:
#: ontoMeta 是入口，点过去是为了**看这一个东西**，不是为了进 Superset 逛，所以去掉它的
#: 全局导航。两边都用 ``1``：看板要留标题栏（人得知道自己在看哪张看板），图表要留
#: explore 的控制面板（那正是"打开这张图"要看的）。数据集指向的是管理页，不加。
#:
#: **"不可编辑"不靠这个参数**。标题栏与「编辑看板」按钮是同一行，``standalone`` 只能整行
#: 留或整行去；而且它只隐藏 UI，把参数从地址栏删掉就全恢复。真正的只读靠角色——
#: 用没有 ``can_write on Dashboard`` 的账号看，Superset 自己就不渲染编辑按钮。
_STANDALONE_MODE = {"dashboard": "1", "chart": "1"}


def open_url(cfg: SupersetRuntimeConfig, asset_type: str, url_path: str) -> str:
    """用户点开时的地址：在 ``public_url`` 之上补全屏参数。

    参数在**拼地址时**加，不写进 ``url_path``——后者是"这个对象在 Superset 里的位置"，
    全屏与否是展示口径。分开之后，改口径不用回头 backfill 已登记的行。
    """
    mode = _STANDALONE_MODE.get(asset_type)
    if not mode or not url_path:
        return public_url(cfg, url_path)
    separator = "&" if "?" in url_path else "?"
    return public_url(cfg, f"{url_path}{separator}standalone={mode}")


# ------------------------------------------------------------------ 登记簿


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def register_asset(
    db: Session,
    *,
    asset_type: str,
    superset_id: int,
    title: str,
    url_path: str = "",
    viz_type: str | None = None,
    dataset_ref: str | None = None,
    superset_dataset_id: int | None = None,
    ontology_id: str | None = None,
    domain_id: str | None = None,
    created_by: str | None = None,
    created_via: str = "mcp",
    extra: dict[str, Any] | None = None,
) -> SupersetAsset:
    """登记一条资产（按 ``(asset_type, superset_id)`` 幂等）。

    重复建同一个东西时更新而不是插新行——两行指着同一个 Superset 对象，
    "这张图是谁建的"就有了两个答案。
    """
    row = db.execute(
        select(SupersetAsset).where(
            SupersetAsset.asset_type == asset_type,
            SupersetAsset.superset_id == superset_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = SupersetAsset(asset_type=asset_type, superset_id=superset_id)
        db.add(row)
    row.title = title
    row.url_path = url_path
    row.viz_type = viz_type
    row.dataset_ref = dataset_ref
    row.superset_dataset_id = superset_dataset_id
    row.ontology_id = ontology_id
    row.domain_id = domain_id
    row.created_by = row.created_by or created_by
    row.created_via = created_via
    # 刚建出来的东西是确实存在的，这一条是"亲眼见过"，不是猜。
    row.state = "active"
    row.last_seen_at = _now()
    if extra is not None:
        row.extra_json = json.dumps(extra, ensure_ascii=False)
    db.commit()
    db.refresh(row)
    return row


def serialize_asset(row: SupersetAsset, cfg: SupersetRuntimeConfig | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "asset_type": row.asset_type,
        "superset_id": row.superset_id,
        "title": row.title,
        "url": open_url(cfg, row.asset_type, row.url_path) if cfg else row.url_path,
        "url_path": row.url_path,
        "viz_type": row.viz_type,
        "dataset_ref": row.dataset_ref,
        "superset_dataset_id": row.superset_dataset_id,
        "ontology_id": row.ontology_id,
        "domain_id": row.domain_id,
        "created_by": row.created_by,
        "created_via": row.created_via,
        "state": row.state,
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _ontology_info(
    db: Session, ontology_id: str | None, cache: dict[str, tuple[str | None, str | None]]
) -> tuple[str | None, str | None]:
    """本体 id → ``(展示名, 数据域 id)``。

    本体行自己没有名字，名字在它挂的数据域上；数据域 id 前端还要用来拼工作台里的
    对象详情路径（``/workspace/{domain_id}/objects/{object_id}``）。
    """
    if not ontology_id:
        return None, None
    if ontology_id not in cache:
        row = db.get(Ontology, ontology_id)
        domain = row.domain_context if row is not None else None
        cache[ontology_id] = (getattr(domain, "name", None), getattr(domain, "id", None))
    return cache[ontology_id]


def _landing_view(
    db: Session, entry: Any, cache: dict[str, tuple[str | None, str | None]]
) -> dict[str, Any]:
    """目录项 → 列表要展示的落点。带上实体 id、本体名与数据域 id，前端据此跳本地详情页。"""
    owner = (
        db.get(ObjectType, entry.entity_id)
        if entry.entity_kind == dataset_catalog.KIND_OBJECT
        else db.get(BusinessLogic, entry.entity_id)
    )
    ontology_id = getattr(owner, "ontology_id", None)
    ontology_name, domain_id = _ontology_info(db, ontology_id, cache)
    return {
        "ref": entry.ref,
        "entity_id": entry.entity_id,
        "entity_kind": entry.entity_kind,
        "entity_name": entry.entity_name,
        "entity_display_name": entry.entity_display_name,
        "physical": entry.physical,
        "layer": entry.layer,
        "ontology_id": ontology_id,
        "ontology_name": ontology_name,
        # 详情页链接要走工作台路由——``/ontology/{id}`` 只对**已发布**对象成立，
        # 草稿/已编辑的对象点过去是「Object type not found」。
        "domain_id": domain_id,
    }


def _member_chart_refs(db: Session, rows: list[SupersetAsset]) -> dict[int, str]:
    """看板成员图表 → 它们各自的落点引用。

    看板自己没有 ``dataset_ref``（它是多张图的集合，可能跨落点），但「这个看板的数字
    来自哪几张表」正是治理上最该回答的问题，而答案在成员图表的登记行里。
    只有**经 ontoMeta 建的**图表才有登记行；用户直接在 Superset 里拼进去的查不到。
    """
    wanted: set[int] = set()
    for row in rows:
        if row.asset_type != "dashboard":
            continue
        wanted.update(_dashboard_chart_ids(row))
    if not wanted:
        return {}
    charts = db.execute(
        select(SupersetAsset).where(
            SupersetAsset.asset_type == "chart",
            SupersetAsset.superset_id.in_(wanted),
        )
    ).scalars().all()
    return {c.superset_id: c.dataset_ref for c in charts if c.dataset_ref}


def _dashboard_chart_ids(row: SupersetAsset) -> list[int]:
    try:
        extra = json.loads(row.extra_json) if row.extra_json else {}
    except ValueError:
        return []
    ids = extra.get("chart_ids") if isinstance(extra, dict) else None
    return [int(c) for c in ids if isinstance(c, int | str) and str(c).isdigit()] if ids else []


def serialize_assets(
    db: Session, rows: list[SupersetAsset], cfg: SupersetRuntimeConfig | None = None
) -> list[dict[str, Any]]:
    """批量序列化，并把落点解析成人话。

    列表里光摆一个 ``obj:068504b9-…@serving`` 等于没说：人看不出这张图建在「客户」上。
    句柄该留在 Tooltip 里给任务配置和 Agent 用，表面上要给实体名与所属本体。

    ``landings`` 是**列表**：图表恒为 0 或 1 个，看板可能跨多个落点（由成员图表推导）。
    空列表有三种含义，靠 ``dataset_ref`` 与 ``asset_type`` 分辨，前端据此措辞：
    有 ref 却空 = 引用解析不出来（口径断了）；无 ref 的图 = 本来就没接治理；
    看板为空 = 成员图表没有一张在 ontoMeta 登记过。

    按 ref 去重后再解析——同一个落点上通常挂着好几张图，逐行解析是白跑。
    """
    resolved: dict[str, dict[str, Any] | None] = {}
    onto_cache: dict[str, tuple[str | None, str | None]] = {}
    member_refs = _member_chart_refs(db, rows)

    def _resolve(ref: str) -> dict[str, Any] | None:
        if ref not in resolved:
            entry = dataset_catalog.resolve_dataset_ref(db, ref)
            resolved[ref] = _landing_view(db, entry, onto_cache) if entry is not None else None
        return resolved[ref]

    out: list[dict[str, Any]] = []
    for row in rows:
        data = serialize_asset(row, cfg)
        if row.dataset_ref:
            refs = [row.dataset_ref]
        elif row.asset_type == "dashboard":
            refs = [member_refs[c] for c in _dashboard_chart_ids(row) if c in member_refs]
        else:
            refs = []
        # dict.fromkeys 保序去重：看板里同一个落点挂着多张图是常态。
        data["landings"] = [
            view for view in (_resolve(r) for r in dict.fromkeys(refs)) if view is not None
        ]
        out.append(data)
    return out


def list_assets(
    db: Session,
    *,
    asset_type: str | None = None,
    ontology_id: str | None = None,
    keyword: str | None = None,
    limit: int = 100,
) -> list[SupersetAsset]:
    stmt = select(SupersetAsset).where(SupersetAsset.enabled.is_(True))
    if asset_type:
        stmt = stmt.where(SupersetAsset.asset_type == asset_type)
    if ontology_id:
        stmt = stmt.where(SupersetAsset.ontology_id == ontology_id)
    rows = list(db.execute(stmt.order_by(SupersetAsset.created_at.desc())).scalars().all())
    if keyword:
        needle = keyword.strip().lower()
        rows = [r for r in rows if needle in (r.title or "").lower()]
    return rows[:limit]


def unlink(db: Session, asset_id: str) -> bool:
    """只解除登记，**不删 Superset 里的东西**。

    删外部系统里的对象是另一个决定，得在那边做——这里删掉别人正在看的看板，
    平台上却只表现为"从列表里消失了"。
    """
    row = db.get(SupersetAsset, asset_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def reconcile(db: Session, cfg: SupersetRuntimeConfig, sc: SupersetClient) -> dict[str, int]:
    """与 Superset 对一次账：那边还在的置 ``active``，没了的置 ``missing``。

    不做删除——登记行本身是"我们建过它"的记录，即使对象已被人在 Superset 里删掉，
    这条记录仍然回答了"当时建了什么"。
    """
    alive: dict[str, set[int]] = {
        "chart": {int(c["id"]) for c in sc.list_charts(page_size=100) if c.get("id")},
        "dashboard": {int(d["id"]) for d in sc.list_dashboards(page_size=100) if d.get("id")},
        "dataset": {int(d["id"]) for d in sc.list_datasets(page_size=100) if d.get("id")},
    }
    counts = {"active": 0, "missing": 0}
    now = _now()
    for row in db.execute(select(SupersetAsset)).scalars().all():
        present = row.superset_id in alive.get(row.asset_type, set())
        row.state = "active" if present else "missing"
        counts["active" if present else "missing"] += 1
        if present:
            row.last_seen_at = now
    db.commit()
    return counts


# --------------------------------------------------------------- 数据集


def _split_physical(physical: str) -> tuple[str | None, str]:
    """``库.表`` → ``(schema, table)``；没带库名就只有表名。"""
    text = (physical or "").strip().strip("`")
    if "." in text:
        schema, table = text.rsplit(".", 1)
        return schema.strip().strip("`") or None, table.strip().strip("`")
    return None, text


def _semantic_columns(db: Session, entity_kind: str, entity_id: str) -> dict[str, dict[str, str]]:
    """本体属性 → ``{物理列名小写: {verbose_name, description}}``。

    按属性名大小写不敏感匹配 Superset 发现的列：物理列名由物化按属性名生成，但大小写
    与个别改名并不保证一致。匹配不上的列原样留着——宁可少推一列中文名，也不能把
    某个字段的说明安到另一个字段头上。
    """
    if entity_kind != dataset_catalog.KIND_OBJECT:
        return {}
    obj = db.get(ObjectType, entity_id)
    if obj is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    props = db.execute(
        select(Property).where(Property.object_type_id == obj.id)
    ).scalars().all()
    for prop in props:
        key = (prop.name or "").strip().lower()
        if not key:
            continue
        meta: dict[str, str] = {}
        if prop.display_name and prop.display_name != prop.name:
            meta["verbose_name"] = prop.display_name
        if prop.description:
            meta["description"] = prop.description
        if meta:
            out[key] = meta
    return out


def _push_semantics(
    db: Session, sc: SupersetClient, dataset_id: int, entry: dataset_catalog.DatasetEntry
) -> int:
    """把本体语义推成 Superset 的列注释。返回推了几列。

    这是「复用已发布本体建数据集」相对于「在裸表上作图」的全部价值：业务在 Superset
    里看到的是中文字段名与口径说明，而不是 ods_xxx 的物理列名。
    """
    semantics = _semantic_columns(db, entry.entity_kind, entry.entity_id)
    if not semantics:
        return 0
    detail = sc.get_dataset(dataset_id).get("result") or {}
    columns = detail.get("columns") or []
    payload: list[dict[str, Any]] = []
    touched = 0
    for col in columns:
        name = str(col.get("column_name") or "")
        meta = semantics.get(name.strip().lower())
        # Superset 的 PUT 是整体替换列集合：没匹配上的列也要原样带回去，
        # 只带改动的那几列会把其余列删掉。
        item: dict[str, Any] = {"id": col.get("id"), "column_name": name}
        if meta:
            item.update(meta)
            touched += 1
        elif col.get("verbose_name"):
            item["verbose_name"] = col["verbose_name"]
        payload.append(item)
    if touched:
        sc.update_dataset(dataset_id, {"columns": payload})
    return touched


def ensure_dataset(
    db: Session,
    cfg: SupersetRuntimeConfig,
    sc: SupersetClient,
    dataset_ref: str,
    *,
    created_by: str | None = None,
    created_via: str = "mcp",
) -> dict[str, Any]:
    """落点引用 → Superset dataset（幂等）。

    只接受目录里的引用（``obj:<id>@serving`` / ``obj:<id>@ods`` / ``logic:<id>@ads``）：
    数据集必须建在本体认领过的落点上，否则口径就脱离治理了。

    挂哪条 Superset database（连接）由**落点自己的数据源**决定，不是全局配置——见
    ``services/superset_database``。库则由 ``库.表`` 拆出来单独当 ``schema`` 传。
    """
    entry = dataset_catalog.resolve_dataset_ref(db, dataset_ref)
    if entry is None:
        raise ValueError(
            f"认不出这个落点引用：{dataset_ref!r}。"
            "先用 list_datasets 查可用的落点（形如 obj:<id>@serving）"
        )
    if not entry.source_ready:
        raise ValueError(
            f"{entry.entity_display_name} 的落点还没建出来（当前状态 {entry.state}）："
            "先把物化/同步任务跑通，再建数据集"
        )

    try:
        database_id = resolve_database_id(db, sc, entry.datasource_id)
    except SupersetDatabaseUnresolved as exc:
        # 对调用方来说这仍是"没配好"，不是"调用失败"：要把人指回去补连接，而不是查网络。
        raise SupersetNotConfigured(str(exc)) from exc

    schema, table = _split_physical(entry.physical)
    existing = sc.find_dataset(database_id, schema, table)
    if existing:
        dataset_id = int(existing["id"])
        created = False
    else:
        result = sc.create_dataset(database_id, schema, table)
        dataset_id = int(result.get("id") or (result.get("result") or {}).get("id"))
        created = True
        # 新建的数据集可能还没有列，不刷新的话后面建图选不到字段。
        try:
            sc.refresh_dataset(dataset_id)
        except SupersetError:
            pass

    pushed = _push_semantics(db, sc, dataset_id, entry)
    ontology_id = None
    if entry.entity_kind == dataset_catalog.KIND_OBJECT:
        obj = db.get(ObjectType, entry.entity_id)
        ontology_id = obj.ontology_id if obj else None

    asset = register_asset(
        db,
        asset_type="dataset",
        superset_id=dataset_id,
        title=entry.entity_display_name or table,
        url_path=f"/tablemodelview/edit/{dataset_id}",
        dataset_ref=entry.ref,
        superset_dataset_id=dataset_id,
        ontology_id=ontology_id,
        created_by=created_by,
        created_via=created_via,
        extra={"physical": entry.physical, "layer": entry.layer},
    )
    detail = sc.get_dataset(dataset_id).get("result") or {}
    return {
        "dataset_id": dataset_id,
        "created": created,
        "physical": entry.physical,
        "dataset_ref": entry.ref,
        "semantics_pushed": pushed,
        "columns": [
            {
                "name": c.get("column_name"),
                "label": c.get("verbose_name") or c.get("column_name"),
                "type": c.get("type"),
                "is_temporal": bool(c.get("is_dttm")),
            }
            for c in (detail.get("columns") or [])
        ],
        "metrics": [m.get("metric_name") for m in (detail.get("metrics") or [])],
        "url": open_url(cfg, "dataset", asset.url_path),
        "asset_id": asset.id,
    }


# ---------------------------------------------------------------- 图表


def _chart_body(spec: ChartSpec) -> dict[str, Any]:
    params, query_context = compile_chart(spec)
    return {
        "slice_name": spec.name,
        "viz_type": params["viz_type"],
        "datasource_id": spec.dataset_id,
        "datasource_type": "table",
        # Superset 收的是 JSON **字符串**，不是嵌套对象。
        "params": json.dumps(params, ensure_ascii=False),
        "query_context": json.dumps(query_context, ensure_ascii=False),
    }


def create_chart(
    db: Session,
    cfg: SupersetRuntimeConfig,
    sc: SupersetClient,
    spec: ChartSpec,
    *,
    dataset_ref: str | None = None,
    ontology_id: str | None = None,
    created_by: str | None = None,
    created_via: str = "mcp",
) -> dict[str, Any]:
    body = _chart_body(spec)
    result = sc.create_chart(body)
    chart_id = int(result.get("id") or (result.get("result") or {}).get("id"))
    asset = register_asset(
        db,
        asset_type="chart",
        superset_id=chart_id,
        title=spec.name,
        url_path=f"/explore/?slice_id={chart_id}",
        viz_type=spec.viz,
        dataset_ref=dataset_ref,
        superset_dataset_id=spec.dataset_id,
        ontology_id=ontology_id,
        created_by=created_by,
        created_via=created_via,
    )
    return {
        "chart_id": chart_id,
        "url": open_url(cfg, "chart", asset.url_path),
        "viz": spec.viz,
        "asset_id": asset.id,
    }


def update_chart(
    db: Session,
    cfg: SupersetRuntimeConfig,
    sc: SupersetClient,
    chart_id: int,
    spec: ChartSpec,
) -> dict[str, Any]:
    """整体改写一张图的口径与形态（"把柱状改折线"是高频需求）。

    改的是同一个 Superset 对象，故登记行也就地更新——另起一行会让同一张图在
    平台上出现两次。
    """
    body = _chart_body(spec)
    sc.update_chart(chart_id, body)
    row = db.execute(
        select(SupersetAsset).where(
            SupersetAsset.asset_type == "chart", SupersetAsset.superset_id == chart_id
        )
    ).scalar_one_or_none()
    if row is not None:
        row.title = spec.name
        row.viz_type = spec.viz
        row.superset_dataset_id = spec.dataset_id
        row.state = "active"
        row.last_seen_at = _now()
        db.commit()
    return {
        "chart_id": chart_id,
        "url": open_url(cfg, "chart", f"/explore/?slice_id={chart_id}"),
        "viz": spec.viz,
    }


# ---------------------------------------------------------------- 看板


def _attach_charts(sc: SupersetClient, dashboard_id: int, chart_ids: list[int]) -> list[int]:
    """把图**关联**到看板上，返回没挂上的 chart id。

    只写 ``position_json`` 建出来的是一张空看板：布局里确实有一格写着 chartId，但
    Superset 的看板↔图关联（``dashboard_slices``）没建立，``/dashboard/{id}/charts``
    与 ``/datasets`` 全空，前端没有东西可渲染。实测内置示例看板这两个端点都非空，
    我们建的是 0/0——放置和关联两件事都得做，缺哪个都是打开空白。

    关联只能从图这一侧写：看板的 POST/PUT schema 不收 ``slices``，而
    ``PUT /api/v1/chart/{id}`` 收 ``dashboards``。那是**整体替换**，所以先读回这张图
    现有的看板再追加——否则把图从它原来所在的看板上摘了下来。
    """
    failed: list[int] = []
    for chart_id in chart_ids:
        try:
            body = sc.get_chart(chart_id).get("result") or {}
            existing = {
                int(d["id"])
                for d in (body.get("dashboards") or [])
                if isinstance(d, dict) and d.get("id") is not None
            }
            sc.update_chart(chart_id, {"dashboards": sorted(existing | {int(dashboard_id)})})
        except (SupersetError, TypeError, ValueError):
            failed.append(chart_id)
    return failed


def create_dashboard(
    db: Session,
    cfg: SupersetRuntimeConfig,
    sc: SupersetClient,
    title: str,
    chart_ids: list[int],
    *,
    ontology_id: str | None = None,
    created_by: str | None = None,
    created_via: str = "mcp",
) -> dict[str, Any]:
    """建看板、把图**放进布局**、再把图**关联**到看板。

    三件事缺一不可：只关联不放置，看板打开是空的（见 ``build_position_json``）；
    只放置不关联，看板打开同样是空的（见 ``_attach_charts``）。

    看板只给跳转链接，不做内嵌——ontoMeta 侧不签 guest token、不持有 embedded uuid。
    """
    if not chart_ids:
        raise ValueError("看板至少要放一张图")
    position = build_position_json(chart_ids)
    result = sc.create_dashboard({
        "dashboard_title": title,
        "published": True,
        "position_json": json.dumps(position, ensure_ascii=False),
        "json_metadata": json.dumps({"chart_configuration": {}}, ensure_ascii=False),
    })
    dashboard_id = int(result.get("id") or (result.get("result") or {}).get("id"))
    unlinked = _attach_charts(sc, dashboard_id, chart_ids)

    asset = register_asset(
        db,
        asset_type="dashboard",
        superset_id=dashboard_id,
        title=title,
        url_path=f"/superset/dashboard/{dashboard_id}/",
        ontology_id=ontology_id,
        created_by=created_by,
        created_via=created_via,
        extra={"chart_ids": chart_ids},
    )
    return {
        "dashboard_id": dashboard_id,
        "url": open_url(cfg, "dashboard", asset.url_path),
        "chart_ids": chart_ids,
        "unlinked_chart_ids": unlinked,
        "asset_id": asset.id,
    }


