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
from app.models import ObjectType, Property, SupersetAsset
from app.services import dataset_catalog
from app.services.settings_service import SettingsService, SupersetRuntimeConfig
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
    embedded_uuid: str | None = None,
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
    if embedded_uuid:
        row.embedded_uuid = embedded_uuid
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
        "embedded_uuid": row.embedded_uuid,
        "title": row.title,
        "url": public_url(cfg, row.url_path) if cfg else row.url_path,
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
    """
    if cfg.database_id is None:
        raise SupersetNotConfigured(
            "未配置 database_id：请在设置页填上 Superset 里指向数仓的 database 编号"
        )
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

    schema, table = _split_physical(entry.physical)
    existing = sc.find_dataset(cfg.database_id, schema, table)
    if existing:
        dataset_id = int(existing["id"])
        created = False
    else:
        result = sc.create_dataset(cfg.database_id, schema, table)
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
        "url": public_url(cfg, asset.url_path),
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
        "url": public_url(cfg, asset.url_path),
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
        "url": public_url(cfg, f"/explore/?slice_id={chart_id}"),
        "viz": spec.viz,
    }


# ---------------------------------------------------------------- 看板


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
    allowed_domains: list[str] | None = None,
) -> dict[str, Any]:
    """建看板并把图**放进布局**。

    只关联不放置，看板打开是空的（见 ``build_position_json`` 的说明）。
    嵌入 uuid 顺手拿一次：拿不到不算失败——那通常只是 Superset 没开
    ``EMBEDDED_SUPERSET``，跳转链接照样可用。
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

    embedded_uuid: str | None = None
    embed_error: str | None = None
    try:
        embedded_uuid = sc.enable_embedded(dashboard_id, allowed_domains or [])
    except SupersetError as exc:
        embed_error = str(exc)

    asset = register_asset(
        db,
        asset_type="dashboard",
        superset_id=dashboard_id,
        title=title,
        url_path=f"/superset/dashboard/{dashboard_id}/",
        ontology_id=ontology_id,
        embedded_uuid=embedded_uuid,
        created_by=created_by,
        created_via=created_via,
        extra={"chart_ids": chart_ids},
    )
    return {
        "dashboard_id": dashboard_id,
        "url": public_url(cfg, asset.url_path),
        "embedded_uuid": embedded_uuid,
        "embed_error": embed_error,
        "chart_ids": chart_ids,
        "asset_id": asset.id,
    }


def guest_token(
    db: Session,
    cfg: SupersetRuntimeConfig,
    sc: SupersetClient,
    asset_id: str,
    *,
    username: str,
    allowed_domains: list[str] | None = None,
) -> str:
    """签发嵌入用的 guest token。**只能在后端调**，前端不得持 Superset 账密。"""
    row = db.get(SupersetAsset, asset_id)
    if row is None or row.asset_type != "dashboard":
        raise ValueError("只有看板可以嵌入")
    if not row.embedded_uuid:
        # 建看板时没拿到（多半那会儿还没开特性开关），这里补一次。
        row.embedded_uuid = sc.enable_embedded(row.superset_id, allowed_domains or [])
        db.commit()
    return sc.guest_token(
        [{"type": "dashboard", "id": row.embedded_uuid}],
        {"username": username, "first_name": username, "last_name": ""},
    )
