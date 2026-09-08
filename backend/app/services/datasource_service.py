"""数据源与 Doris 数仓配置服务。

职责只剩一层：**连哪些库**——数据源登记 / 拨测 / 库表内省，以及默认数仓（Doris）配置。
它是物化、搬运、Agent 取数共用的地基，``resolve_domain_data_source`` 是全平台唯一
的选源口。

自研的数据应用（Dashboard/Panel、口径编译、预览、发布、公开分享）已整体下线，
图表与看板改由 Apache Superset 承载（见 ``services/superset_service.py``）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import DataSource, DorisWarehouseConfig
from app.services.data_app_executor import (
    ExecutionError,
    execute_sql,
)
from app.services.data_app_executor import (
    list_databases as execute_list_databases,
)
from app.services.data_app_executor import (
    list_tables as execute_list_tables,
)
from app.warehouse.policy import WAREHOUSE_ENGINE, require_doris_datasource

# 与前端 DataSourcesModal 的 KIND_PROFILES 对齐：host 类走结构化连接串、file 类是本地文件。
_HOST_DSN_KINDS = {"postgres", "mysql", "hive", "doris", "starrocks", "clickhouse"}
_FILE_DSN_KINDS = {"sqlite", "duckdb"}
_TIME_WINDOW_DAYS = {
    "last_7d": 7,
    "last_30d": 30,
    "last_90d": 90,
    "today": 0,
    "this_month": 30,
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _merge_dsn_password(new_dsn: str, old_dsn: str | None) -> str:
    """编辑时连接字段回显但密码不回显：若新 DSN 没带密码而旧 DSN 有，则沿用旧密码。

    这样用户改了主机/端口/库却把密码留空时不会把密码清掉（对齐「留空＝保持不变」）。
    非 SQLAlchemy URL 解析失败时原样返回，不做合并。
    """
    if not old_dsn:
        return new_dsn
    try:
        new_url = make_url(new_dsn)
        old_url = make_url(old_dsn)
    except Exception:  # noqa: BLE001 - 非 SQLAlchemy URL，无密码概念
        return new_dsn
    if new_url.password is None and old_url.password is not None:
        return new_url.set(password=old_url.password).render_as_string(
            hide_password=False
        )
    return new_dsn


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def resolve_domain_data_source(db: Session) -> DataSource | None:
    """Resolve the explicit default Doris warehouse, fail-closed.

    Business sources, catalog names, timestamps and row order cannot affect a
    query target.
    """
    explicit = db.query(DataSource).filter(
        (DataSource.purpose == "warehouse") | DataSource.is_default_warehouse.is_(True)
    ).all()
    if explicit:
        candidates = [
            s for s in explicit
            if s.kind == WAREHOUSE_ENGINE
            and s.is_default_warehouse
            and s.enabled
            and (s.dsn_secret_ref or "").strip()
            and s.status != "error"
        ]
        if len(candidates) != 1:
            return None
        try:
            return require_doris_datasource(candidates[0], operation="查询")
        except ValueError:
            return None

    # No explicit warehouse means no executable query target. Business sources
    # are never promoted implicitly by recency or catalog markers.
    return None


class DataSourceService:
    """数据源与数仓配置的登记、拨测与内省。"""

    # ------------------------------------------------------------- data sources

    def list_data_sources(self, db: Session) -> list[DataSource]:
        return db.query(DataSource).order_by(desc(DataSource.created_at)).all()

    def create_data_source(
        self, db: Session, *, name: str, kind: str, dsn_secret_ref: str | None,
        mapping: dict | None = None, catalog_name: str | None = None,
        purpose: str = "business_source", is_default_warehouse: bool = False,
        enabled: bool = True,
    ) -> DataSource:
        kind = kind.lower().strip()
        if purpose not in {"business_source", "warehouse"}:
            raise ValueError("数据源 purpose 只能是 business_source 或 warehouse")
        if purpose == "warehouse" and kind != WAREHOUSE_ENGINE:
            raise ValueError("warehouse 数据源必须使用 Doris")
        if is_default_warehouse and purpose != "warehouse":
            raise ValueError("只有 warehouse 数据源可以设为默认数仓")
        if is_default_warehouse:
            existing = db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
            for row in existing:
                row.is_default_warehouse = False
        ds = DataSource(
            name=name, kind=kind, purpose=purpose,
            is_default_warehouse=is_default_warehouse, enabled=enabled,
            dsn_secret_ref=dsn_secret_ref, mapping_json=_dumps(mapping),
            catalog_name=catalog_name,
        )
        db.add(ds)
        db.commit()
        db.refresh(ds)
        return ds

    def update_data_source(self, db: Session, ds_id: str, **fields: Any) -> DataSource:
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        # 连接字段回显但密码不回显：新 DSN 缺密码时沿用旧密码，避免改主机顺手清空密码。
        new_dsn = fields.get("dsn_secret_ref")
        if new_dsn:
            fields["dsn_secret_ref"] = _merge_dsn_password(new_dsn, ds.dsn_secret_ref)
        if "mapping" in fields:
            ds.mapping_json = _dumps(fields.pop("mapping"))
        next_purpose = fields.get("purpose", ds.purpose)
        next_kind = str(fields.get("kind", ds.kind)).lower().strip()
        next_default = fields.get("is_default_warehouse", ds.is_default_warehouse)
        if next_purpose not in {"business_source", "warehouse"}:
            raise ValueError("数据源 purpose 只能是 business_source 或 warehouse")
        if next_purpose == "warehouse" and next_kind != WAREHOUSE_ENGINE:
            raise ValueError("warehouse 数据源必须使用 Doris")
        if next_default and next_purpose != "warehouse":
            raise ValueError("只有 warehouse 数据源可以设为默认数仓")
        if next_default:
            for row in db.query(DataSource).filter(DataSource.id != ds.id).all():
                if row.is_default_warehouse:
                    row.is_default_warehouse = False
        for key, value in fields.items():
            if value is not None and hasattr(ds, key):
                setattr(ds, key, value)
        db.commit()
        db.refresh(ds)
        return ds

    @staticmethod
    def _dsn_components(kind: str, dsn: str | None) -> dict:
        """把存量 DSN 拆成可安全回显的非机密字段，密码只返回是否已设置。

        - host 类（postgres/mysql/hive/doris/starrocks/clickhouse）：解析主机/端口/库/账号
        - 文件类（sqlite/duckdb）：取文件路径
        解析失败时静默降级为空，不影响其它字段返回。
        """
        out: dict[str, Any] = {
            "dsn_set": bool(dsn),
            "host": None,
            "port": None,
            "database": None,
            "username": None,
            "password": None,
            "password_set": False,
            "password_hint": None,
            "path": None,
        }
        if not dsn:
            return out
        if kind in _HOST_DSN_KINDS:
            try:
                u = make_url(dsn)
            except Exception:  # noqa: BLE001 - 存量脏数据不应 500
                return out
            out["host"] = u.host
            out["port"] = u.port
            out["database"] = u.database
            out["username"] = u.username
            # Password is a secret: only expose the presence hint.  The UI must
            # ask the user to re-enter it; PATCH with an empty password keeps the
            # managed value via _merge_dsn_password.
            out["password"] = None
            out["password_set"] = bool(u.password)
            out["password_hint"] = "已配置" if u.password else None
        elif kind in _FILE_DSN_KINDS:
            try:
                out["path"] = make_url(dsn).database
            except Exception:  # noqa: BLE001
                out["path"] = None
        return out

    @staticmethod
    def serialize_data_source(ds: DataSource) -> dict:
        base = {
            "id": ds.id,
            "name": ds.name,
            "kind": ds.kind,
            "purpose": ds.purpose,
            "is_default_warehouse": ds.is_default_warehouse,
            "enabled": ds.enabled,
            "status": ds.status,
            "mapping": _loads(ds.mapping_json, None),
            "catalog_name": ds.catalog_name,
            "tested_at": ds.tested_at,
            "created_at": ds.created_at,
            "updated_at": ds.updated_at,
        }
        base.update(DataSourceService._dsn_components(ds.kind, ds.dsn_secret_ref))
        return base

    def get_doris_config(self, db: Session) -> DorisWarehouseConfig | None:
        return db.query(DorisWarehouseConfig).first()

    def save_doris_config(self, db: Session, data: dict[str, Any]) -> DorisWarehouseConfig:
        ds = db.get(DataSource, data.get("warehouse_datasource_id"))
        if not ds:
            raise ValueError("Doris 数仓数据源不存在")
        require_doris_datasource(ds, operation="Doris 配置")
        if not ds.is_default_warehouse:
            raise ValueError("Doris 配置必须绑定默认 warehouse DataSource")
        if data.get("enabled", True):
            if not str(data.get("query_host") or "").strip():
                raise ValueError("启用 Doris 必须配置 FE SQL query_host")
            if not [str(x).strip() for x in data.get("fenodes") or [] if str(x).strip()]:
                raise ValueError("启用 Doris 必须配置至少一个 FE HTTP fenode")
        config = self.get_doris_config(db)
        token = "".join(c for c in ds.id.lower() if c.isalnum())[:12]
        if config is None:
            config = DorisWarehouseConfig(
                id="default",
                warehouse_datasource_id=ds.id,
                airflow_ddl_conn_id=f"ontometa_doris_{token}_ddl",
                airflow_etl_conn_id=f"ontometa_doris_{token}_etl",
                airflow_flink_conn_id=f"ontometa_doris_{token}_flink",
            )
            db.add(config)
        for key, value in data.items():
            if key in {"fenodes", "benodes"}:
                value = _dumps(value)
                key = f"{key}_json"
            if key == "reader_dsn_secret_ref":
                if not value:
                    continue  # blank means keep existing secret
                # 设置页不回显密码；编辑主机/端口/库时，用原 reader DSN（首配时用
                # DataSource DSN）补回密码，避免一次普通保存把受管凭据清空。
                # reader_dsn_secret_ref 也允许 secret://alias 这类不透明引用；只有设置页
                # 提交的是实际 Doris SQLAlchemy DSN 时才做密码合并，不能改写 secret 引用。
                if str(value).startswith(("mysql://", "mysql+pymysql://")):
                    value = _merge_dsn_password(
                        value,
                        config.reader_dsn_secret_ref or ds.dsn_secret_ref,
                    )
            if key in {
                "airflow_ddl_conn_id", "airflow_etl_conn_id", "airflow_flink_conn_id"
            } and not value:
                continue  # keep deterministic ids unless explicitly overridden
            if hasattr(config, key):
                setattr(config, key, value)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def serialize_doris_config(config: DorisWarehouseConfig) -> dict[str, Any]:
        out = {c.name: getattr(config, c.name) for c in config.__table__.columns}
        out["fenodes"] = _loads(out.pop("fenodes_json"), [])
        out["benodes"] = _loads(out.pop("benodes_json", None), [])
        secret = out.pop("reader_dsn_secret_ref", None)
        out["reader_dsn_set"] = bool(secret)
        out["reader_dsn_hint"] = "已配置" if secret else None
        return out

    def delete_data_source(self, db: Session, ds_id: str) -> None:
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        db.delete(ds)
        db.commit()

    def test_data_source(self, db: Session, ds_id: str) -> DataSource:
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        if ds.kind == "mock" or not ds.dsn_secret_ref:
            ds.status = "ok" if ds.kind == "mock" else "untested"
        else:
            # 真实连接测试：执行一条最小只读查询
            try:
                execute_sql(dsn=ds.dsn_secret_ref, sql="SELECT 1", limit=1)
                ds.status = "ok"
            except ExecutionError as exc:
                ds.status = "error"
                ds.tested_at = _now()
                db.commit()
                db.refresh(ds)
                raise ValueError(f"连接测试失败：{exc}") from exc
        ds.tested_at = _now()
        db.commit()
        db.refresh(ds)
        return ds

    def _writable_dsn(self, db: Session, ds_id: str) -> str:
        """取数据源的连接串；mock / 未配置连接的源无从内省，明确报错而非返回空列表。"""
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        if ds.kind == "mock":
            raise ValueError("Mock 数据源为内置样例，无库表可读取")
        if not ds.dsn_secret_ref:
            raise ValueError(f"数据源「{ds.name}」未配置连接串（dsn）")
        return ds.dsn_secret_ref

    def list_databases(self, db: Session, ds_id: str) -> list[str]:
        """目标源上的库列表，供物化选落库位置。"""
        dsn = self._writable_dsn(db, ds_id)
        try:
            return execute_list_databases(dsn)
        except ExecutionError as exc:
            raise ValueError(str(exc)) from exc

    def list_tables(self, db: Session, ds_id: str, database: str | None) -> list[str]:
        """某个库下已有的表，供物化推荐表名并提示「已存在（覆盖写）」。"""
        dsn = self._writable_dsn(db, ds_id)
        try:
            return execute_list_tables(dsn, database)
        except ExecutionError as exc:
            raise ValueError(str(exc)) from exc

    # ----------------------------------------------------------------- app CRUD
