"""本体对象 → 可用业务 DataSource 的确定性匹配。

``ObjectType.source_ref`` 已声明物理平台与表名；同步表单不应再把全库所有业务源交给用户猜。
本模块只返回与该来源兼容的已启用 ``business_source``：

1. 数据平台必须同族（postgres/postgresql、mysql/mariadb 分别归一）；
2. 若 catalog_name、DSN 默认库或 mapping_json 能进一步命中 source_ref 的库/表，只保留
   有强命中的候选；
3. 没有库级证据时保留同平台候选供用户选择，按证据分数稳定排序。

不向调用方暴露 DSN；DSN 仅在服务端用于读取默认数据库名。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import DataSource, ObjectType
from app.services.source_ref import source_platform_of, source_table_of


_PLATFORM_FAMILY: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
}


def platform_family(value: str | None) -> str:
    key = (value or "").strip().lower()
    return _PLATFORM_FAMILY.get(key, key)


def _dsn_database(dsn: str | None) -> str | None:
    """尽力读取 DSN 默认库；secret 引用/非法 DSN 返回 None，绝不抛。"""
    if not dsn or "://" not in dsn:
        return None
    try:
        database = make_url(dsn).database
        return str(database).strip().lower() if database else None
    except Exception:  # noqa: BLE001 — 匹配增强，解析失败只少一条证据
        return None


def _mapping_mentions(mapping_json: str | None, source_table: str) -> bool:
    if not mapping_json:
        return False
    try:
        payload: Any = json.loads(mapping_json)
    except (TypeError, ValueError):
        return False
    needle = source_table.lower()
    # mapping 形态有历史版本，序列化后做包含判断比另维护多套 schema 更稳；内容是服务端元数据，
    # 不含凭据，且只用于排序/收窄。
    return needle in json.dumps(payload, ensure_ascii=False).lower()


def source_datasource_candidates(
    db: Session,
    object_type: ObjectType,
    *,
    sources: list[DataSource] | None = None,
) -> list[DataSource]:
    """返回与对象 ``source_ref`` 匹配的业务数据源（推荐项在前）。"""
    platform = platform_family(source_platform_of(object_type.source_ref))
    source_table = (source_table_of(object_type.source_ref) or "").strip()
    if not platform or not source_table:
        return []

    qualifiers = {part.lower() for part in source_table.split(".")[:-1] if part}
    candidates: list[tuple[DataSource, int, bool]] = []
    rows = sources if sources is not None else (
        db.query(DataSource)
        .filter(
            DataSource.purpose == "business_source",
            DataSource.enabled.is_(True),
        )
        .order_by(DataSource.name, DataSource.id)
        .all()
    )
    for source in rows:
        if platform_family(source.kind) != platform:
            continue
        score = 100
        strong = False
        catalog = (source.catalog_name or "").strip().lower()
        if catalog and catalog in qualifiers:
            score += 50
            strong = True
        database = _dsn_database(source.dsn_secret_ref)
        if database and database in qualifiers:
            score += 40
            strong = True
        if _mapping_mentions(source.mapping_json, source_table):
            score += 60
            strong = True
        candidates.append((source, score, strong))

    # 一旦存在库/表级强证据，就排除仅“同平台”的宽泛候选；否则用户仍可在同平台连接中选择。
    if any(strong for _source, _score, strong in candidates):
        candidates = [item for item in candidates if item[2]]
    candidates.sort(key=lambda item: (-item[1], item[0].name.lower(), item[0].id))
    return [source for source, _score, _strong in candidates]


__all__ = ["platform_family", "source_datasource_candidates"]
