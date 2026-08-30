"""StarRocks 多目录：源库外部 catalog 的 DDL 生成与同步。

统一查询网关（warehouse-first）的接线层：``DataSource.catalog_name`` 非空且非
``"internal"`` 的行（如 ``catalog_name="erp"``），就是要在 StarRocks 里注册的
外部 JDBC catalog。本模块把「把源库注册进数仓目录」从手写 SQL 变成两个动作：

1. **生成**：``generate_catalog_ddl`` 从 DataSource 的连接串推导 JDBC URI 与驱动，
   产出 ``CREATE EXTERNAL CATALOG`` 语句；
2. **同步**：``sync_catalogs`` 对可执行的 FE（MySQL 协议）执行这些 DDL——
   已存在的 catalog 跳过（幂等），不重复创建。

驱动映射：mysql → ``com.mysql.cj.jdbc.Driver``（mysql-connector-j）、
postgres → ``org.postgresql.Driver``。``driver_url`` 必须能被 FE/BE 节点拉到
（http(s) 或 file:// 路径；本地单机开发把 jar 放到 BE 同机即可）。

写侧约束：本模块只做**建目录**（catalog 级别的 DDL），不做数据搬运——源库数据
进数仓统一走 Flink SQL，
JDBC catalog 只读。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import DataSource

logger = logging.getLogger("ontometa.catalog_sync")

# kind → (JDBC driver_class, 官方驱动 jar 的 maven 坐标路径)
_DRIVERS: dict[str, tuple[str, str]] = {
    "mysql": (
        "com.mysql.cj.jdbc.Driver",
        "https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar",
    ),
    "postgres": (
        "org.postgresql.Driver",
        "https://repo1.maven.org/maven2/org/postgresql/postgresql/42.3.3/postgresql-42.3.3.jar",
    ),
}

# 视为「仓库源」的 catalog 名：不生成外部 catalog。
_WAREHOUSE_MARKERS = {"", "internal", "warehouse"}


def is_warehouse_source(ds: DataSource) -> bool:
    """catalog_name 为空/"internal" 的源是仓库投影,不需要外部 catalog。"""
    return (ds.catalog_name or "").strip() in _WAREHOUSE_MARKERS


def jdbc_uri_for(ds: DataSource) -> str | None:
    """从 SQLAlchemy DSN 推导 StarRocks JDBC catalog 的 jdbc_uri。

    mysql+pymysql://u:p@host:port/db → jdbc:mysql://host:port/db（db 可省略）
    postgresql+psycopg://u:p@host:port/db → jdbc:postgresql://host:port/db
    其他 scheme / 解析失败返回 None（调用方跳过该源）。
    """
    dsn = (ds.dsn_secret_ref or "").strip()
    if not dsn:
        return None
    try:
        u = make_url(dsn)
    except Exception:  # noqa: BLE001 - 脏 DSN 不应让同步整体失败
        return None
    scheme = (u.drivername or "").lower()
    if not u.host:
        return None
    if scheme.startswith("mysql"):
        prefix = "jdbc:mysql://"
    elif scheme.startswith(("postgres", "postgresql")):
        prefix = "jdbc:postgresql://"
    else:
        return None
    port = f":{u.port}" if u.port else ""
    db = f"/{u.database}" if u.database else ""
    return f"{prefix}{u.host}{port}{db}"


def generate_catalog_ddl(ds: DataSource) -> str | None:
    """生成 CREATE EXTERNAL CATALOG DDL;不可用的源（无 JDBC URI/驱动/密码）返回 None。"""
    if is_warehouse_source(ds) or not (ds.catalog_name or "").strip():
        return None
    jdbc_uri = jdbc_uri_for(ds)
    if not jdbc_uri:
        return None
    driver = _DRIVERS.get((ds.kind or "").lower())
    if not driver:
        return None
    driver_class, driver_jar = driver
    try:
        u = make_url(ds.dsn_secret_ref)
    except Exception:  # noqa: BLE001
        return None
    user = u.username or ""
    password = u.password or ""
    if not password:
        # 允许无密码（本地开发常免密）；但显式给空串占位,避免属性缺键
        pass
    props = {
        "type": "jdbc",
        "user": user,
        "password": password,
        "jdbc_uri": jdbc_uri,
        "driver_url": driver_jar,
        "driver_class": driver_class,
    }
    prop_lines = ",\n  ".join(f'"{k}" = "{v}"' for k, v in props.items())
    return (
        f'CREATE EXTERNAL CATALOG {ds.catalog_name} PROPERTIES (\n'
        f'  {prop_lines}\n'
        f');'
    )


def sync_catalogs(fe_dsn: str, sources: list[DataSource]) -> list[dict[str, Any]]:
    """对 StarRocks FE 同步外部 catalog:已存在的跳过,缺失的创建。

    返回逐源回执:``[{name, kind, existed|created|skipped, error?}]``。
    FE 说话 MySQL 协议（starrocks+pymysql://root:@127.0.0.1:9030）。
    这是**建目录**专用写操作,不走只读的 ``execute_sql``。
    """
    existing = _existing_catalogs(fe_dsn)
    results: list[dict[str, Any]] = []
    for ds in sources:
        name = (ds.catalog_name or "").strip()
        if is_warehouse_source(ds):
            continue
        ddl = generate_catalog_ddl(ds)
        if ddl is None:
            results.append({"name": name, "kind": ds.kind, "skipped": True,
                            "error": "无可用 JDBC 连接串/驱动,跳过"})
            continue
        if name in existing:
            results.append({"name": name, "kind": ds.kind, "existed": True})
            continue
        try:
            engine = create_engine(fe_dsn, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text(ddl))
            results.append({"name": name, "kind": ds.kind, "created": True})
            logger.info("catalog %s created on %s", name, fe_dsn.split("@")[-1])
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "kind": ds.kind, "created": False,
                            "error": str(exc)[:300]})
    return results


def _existing_catalogs(fe_dsn: str) -> set[str]:
    engine = create_engine(fe_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("SHOW CATALOGS").fetchall()
        return {str(r[0]).strip() for r in rows if r}
    except Exception as exc:  # noqa: BLE001 - FE 不可达时整体报错,由调用方兜底
        raise RuntimeError(f"StarRocks FE 不可达（{fe_dsn.split('@')[-1]}）：{exc}") from exc


def sync_all_catalogs(db: Session) -> dict[str, Any]:
    """编排：找仓库源(FE) + 收集源库 catalog 引用 → 同步。

    供 ``POST /api/data-sources/sync-catalogs`` 使用。仓库源必须 kind=starrocks/doris
    （多目录只在它身上成立）；找不到或引擎不符时给出可读错误,不动任何数据。
    """
    sources = db.query(DataSource).all()
    warehouse = resolve_warehouse_source(sources)
    if warehouse is None:
        return {"ok": False,
                "error": "未配置仓库源（kind=starrocks/doris 且 catalog_name 为空/internal），无法同步目录"}
    fe_dsn = (warehouse.dsn_secret_ref or "").strip()
    if not fe_dsn:
        return {"ok": False, "error": f"仓库源「{warehouse.name}」未配置连接串"}
    try:
        receipts = sync_catalogs(fe_dsn, sources)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "fe": fe_dsn.split("@")[-1], "receipts": receipts}


def resolve_warehouse_source(sources: list[DataSource]) -> DataSource | None:
    """从 DataSource 列表里找仓库源：catalog_name 空/"internal" 且 kind 是多目录引擎。"""
    for s in sources:
        if not is_warehouse_source(s):
            continue
        if (s.kind or "").lower() in ("starrocks", "doris"):
            return s
    return None
