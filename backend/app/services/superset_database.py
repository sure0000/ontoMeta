"""数据源 → Superset database（**连接**）的解析。

Superset 建数据集要指定挂在哪条 database 上。这里的立场是：**这件事由落点自己的数据源
决定，不由全局配置决定**。本体绑定的是一组数据源（源库、Doris，将来还会有别的引擎），
落点建在哪个数据源上，就该用 Superset 里指向那个数据源的连接。

「库」不走这条路——库由落点的 ``库.表`` 拆出来，作为 ``schema`` 单独传给 Superset。
这里解析的只是「连接」。

结果缓存在 ``data_sources.superset_database_id``：解析一次就落库，之后零网络请求；
那一列同时也是人工兜底通道（自动匹配认不出来时直接 PATCH 数据源）。

认不出来就报错，**绝不回落到某条默认连接**——指错连接不会立刻失败，只会让图表安静地
画在另一台机器的同名表上。
"""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.connectors.superset import SupersetClient, SupersetError
from app.models import DataSource


class SupersetDatabaseUnresolved(RuntimeError):
    """在 Superset 里找不到（或找到多条）指向这个数据源的 database 连接。"""


def _endpoint(dsn: str | None) -> tuple[str, int | None, str | None] | None:
    """DSN → ``(host, port, database)``；解析不出来返回 ``None``。

    端口可能没写（走驱动默认值），这时按「不比端口」处理，而不是替它猜一个默认端口——
    猜错会让本来该命中的连接落空。
    """
    if not dsn:
        return None
    try:
        url = make_url(dsn)
    except Exception:  # noqa: BLE001 — 存量脏 DSN 不该让建图整条挂掉
        return None
    host = (url.host or "").strip().lower()
    if not host:
        return None
    return host, url.port, (url.database or "").strip() or None


def _describe(endpoint: tuple[str, int | None, str | None]) -> str:
    host, port, database = endpoint
    where = f"{host}:{port}" if port else host
    return f"{where}/{database}" if database else where


def _superset_endpoints(sc: SupersetClient) -> list[tuple[int, str, tuple[str, int | None, str | None]]]:
    """Superset 侧每条 database 的 ``(id, 名称, 连接地址)``。

    URI 只能从 ``/connection`` 子资源读——``GET /api/v1/database/{id}`` 的回包里没有
    ``sqlalchemy_uri`` 这个键（Superset 6.1 实测）。库的条数是个位数，且解析结果会落库
    缓存，不构成热路径。个别连接读不到或 URI 解析不了就跳过——它本来也匹配不上。
    """
    out: list[tuple[int, str, tuple[str, int | None, str | None]]] = []
    for item in sc.list_databases():
        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            detail = sc.get_database_connection(int(raw_id))
        except SupersetError:
            continue
        body = detail.get("result") or detail
        endpoint = _endpoint(body.get("sqlalchemy_uri"))
        if endpoint is None:
            continue
        name = str(body.get("database_name") or item.get("database_name") or raw_id)
        out.append((int(raw_id), name, endpoint))
    return out


def _inventory(sc: SupersetClient) -> str:
    """把 Superset 现有连接如实列出来，用于「认不出来」时的错误信息。

    只列能解析出地址的那些会说谎：读不到 URI 的连接同样是真实存在的连接，把它们说成
    「一条都没有」，人就会跑去建一条已经有了的连接。所以这里按 ``list_databases``
    的全量列，地址读不到就写「地址未知」。
    """
    parsed = {did: ep for did, _name, ep in _superset_endpoints(sc)}
    rows = []
    for item in sc.list_databases():
        raw_id = item.get("id")
        if raw_id is None:
            continue
        did = int(raw_id)
        name = str(item.get("database_name") or did)
        where = _describe(parsed[did]) if did in parsed else "地址未知"
        rows.append(f"{name}(#{did} → {where})")
    return "；".join(rows) or "（一条都没有）"


def resolve_database_id(db: Session, sc: SupersetClient, datasource_id: str | None) -> int:
    """数据源 → Superset database id。命中即写回缓存。"""
    if not datasource_id:
        raise SupersetDatabaseUnresolved(
            "这条落点没有记录它建在哪个数据源上，无法确定该用 Superset 的哪条连接。"
            "这属于数据异常（落点的数据源绑定是必填的），请检查该对象的接入契约或数仓部署记录。"
        )
    ds = db.get(DataSource, datasource_id)
    if ds is None:
        raise SupersetDatabaseUnresolved(f"数据源 {datasource_id} 不存在")
    if ds.superset_database_id is not None:
        return int(ds.superset_database_id)

    local = _endpoint(ds.dsn_secret_ref)
    if local is None:
        raise SupersetDatabaseUnresolved(
            f"数据源「{ds.name}」没有可解析的连接地址，无法在 Superset 里比对出对应的 database。"
            f"可以直接给这个数据源填上 superset_database_id。"
        )

    candidates = _superset_endpoints(sc)
    host, port, database = local
    same_host = [
        (did, name, ep)
        for did, name, ep in candidates
        # 端口任一侧没写就不比端口：写死默认端口会把本该命中的连接判掉。
        if ep[0] == host and (port is None or ep[1] is None or ep[1] == port)
    ]
    exact = [item for item in same_host if database and item[2][2] == database]

    picked = exact if len(exact) == 1 else same_host
    if len(picked) == 1:
        database_id = picked[0][0]
        ds.superset_database_id = database_id
        db.commit()
        return database_id

    listed = _inventory(sc)
    if not picked:
        raise SupersetDatabaseUnresolved(
            f"Superset 里没有指向数据源「{ds.name}」（{_describe(local)}）的 database 连接。"
            f"现有连接：{listed}。请在 Superset 里建好这条连接，或直接给该数据源填上 superset_database_id。"
            "（注意：Superset 跑在容器里时，它记的主机名可能是容器内部名——"
            "同一个库两边写法不同就匹配不上，这种情况直接指定 superset_database_id。）"
        )
    raise SupersetDatabaseUnresolved(
        f"Superset 里有多条连接都指向数据源「{ds.name}」（{_describe(local)}），无法确定用哪条："
        f"{'；'.join(f'{name}(#{did})' for did, name, _ in picked)}。"
        f"请给该数据源填上 superset_database_id 指定一条。"
    )


__all__ = ["SupersetDatabaseUnresolved", "resolve_database_id"]
