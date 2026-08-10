"""数据应用（Data App）服务。

阶段 1（MVP）职责：
- DataApp / DataAppDataset / DataSource / DataAppVersion 的 CRUD
- Binding Compiler：把数据集口径绑定（本体对象/字段/业务逻辑）编译为确定性 SQL
- Mock Executor：无数据源时按字段语义造数，保证「预览」可体验
- 预览：编译 SQL + 执行（Mock）返回列/行
- 发布：走二次确认 + 冻结版本快照
- 由 Chat BI 口径拆解生成应用草稿

Grounding：数据集必须落地到已发布本体的真实实体，否则拒绝保存/编译。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    DataApp,
    DataAppDataset,
    DataAppVersion,
    DataAppWidget,
    DataSource,
    DomainContext,
    ObjectType,
    Property,
)
from app.services.common import log_change
from app.auth import hash_api_key
from app.services.data_app_executor import (
    ExecutionError,
    execute_sql,
    is_read_only,
    list_databases as execute_list_databases,
    list_tables as execute_list_tables,
)
from app.connectors.cube import CubeConnector, CubeExecutionError
from app.services.ontology_query import OntologyQueryService
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.data_app")

APP_TYPES = {"data_table", "screen", "dashboard"}
_AGG_FUNCS = {"sum", "count", "avg", "max", "min"}
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
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _merge_dsn_password(new_dsn: str, old_dsn: str | None) -> str:
    """编辑时连接字段回显但密码不回显：若新 DSN 没带密码而旧 DSN 有，则沿用旧密码。

    这样用户改了主机/端口/库却把密码留空时不会把密码清掉（对齐「留空＝保持不变」）。
    非 URL 形态（如 cube 的裸地址）解析失败时原样返回，不做合并。
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


def resolve_domain_data_source(
    db: Session, target_catalog: str | None = None
) -> DataSource | None:
    """统一选源（warehouse-first）。查询侧唯一入口，chat_bi / ontology_ladder 共用。

    StarRocks 多目录架构下，DataSource 分两类：
    - warehouse 源（catalog_name 为 NULL/"internal"）：数仓投影，本体物化落这里，默认查这里
    - 源库 catalog 引用（catalog_name="erp"/"crm"/...）：源系统在 StarRocks 里注册的 JDBC
      catalog，**显式 target_catalog 才查**——查源库成为可审计的显式动作，而非「取最新」碰运气。

    策略：
    - ``target_catalog`` 指定：精确匹配该 catalog 名的可用源；匹配不到返回 None
      （run_sql 据此降级为「仅建议 SQL」，不让 agent 悄悄换源）
    - 未指定：优先 warehouse 源（catalog_name 为空/"internal"）；
      无显式 warehouse 源时退化为取最新可用的源（兼容存量库无 catalog_name 的过渡期）
    - 同组多候选时按更新时间取最新（与存量行为一致，避免行序不确定导致的选择漂移）
    """
    usable = [
        s
        for s in db.query(DataSource).all()
        if (s.dsn_secret_ref or "").strip() and s.kind != "mock"
    ]
    if not usable:
        return None

    def _latest(candidates: list[DataSource]) -> DataSource | None:
        if not candidates:
            return None
        candidates.sort(key=lambda s: (s.updated_at or s.created_at), reverse=True)
        return candidates[0]

    if target_catalog and target_catalog != "warehouse":
        return _latest(
            [s for s in usable if (s.catalog_name or "") == target_catalog]
        )
    # warehouse 源：catalog_name 为空或 "internal"（两种标记等价）
    warehouse = [s for s in usable if (s.catalog_name or "").strip() in ("", "internal")]
    if warehouse:
        return _latest(warehouse)
    if target_catalog == "warehouse":
        return None
    # 退化：存量库全部带 catalog_name 标记时取最新可用（兼容过渡期）
    return _latest(usable)


class DataAppService:
    """数据应用编排：绑定→编译→预览→发布。"""

    def __init__(self) -> None:
        self.query_service = OntologyQueryService()
        self.settings_service = SettingsService()

    def _cube_connector(self, db: Session, source: "DataSource | None" = None) -> CubeConnector:
        """从设置页（DB）构造 Cube 连接器；数据源可覆盖 api_url。"""
        runtime = self.settings_service.get_cube_runtime(db)
        conn = CubeConnector.from_runtime(runtime)
        if source and source.dsn_secret_ref:
            conn.api_url = source.dsn_secret_ref.rstrip("/")
        return conn

    # ------------------------------------------------------------- data sources

    def list_data_sources(self, db: Session) -> list[DataSource]:
        return db.query(DataSource).order_by(desc(DataSource.created_at)).all()

    def create_data_source(
        self, db: Session, *, name: str, kind: str, dsn_secret_ref: str | None,
        mapping: dict | None = None, catalog_name: str | None = None,
    ) -> DataSource:
        ds = DataSource(
            name=name,
            kind=kind,
            dsn_secret_ref=dsn_secret_ref,
            mapping_json=_dumps(mapping),
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
        for key, value in fields.items():
            if value is not None and hasattr(ds, key):
                setattr(ds, key, value)
        db.commit()
        db.refresh(ds)
        return ds

    @staticmethod
    def _dsn_components(kind: str, dsn: str | None) -> dict:
        """把存量 DSN 拆成可回显的非机密字段。密码从不返回，只给 password_set。

        - host 类（postgres/mysql/hive/doris/starrocks/clickhouse）：解析主机/端口/库/账号
        - 文件类（sqlite/duckdb）：取文件路径
        - cube：dsn 存的就是 API 地址，原样给 url
        解析失败时静默降级为空，不影响其它字段返回。
        """
        out: dict[str, Any] = {
            "dsn_set": bool(dsn),
            "host": None,
            "port": None,
            "database": None,
            "username": None,
            "password_set": False,
            "path": None,
            "url": None,
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
            out["password_set"] = bool(u.password)
        elif kind in _FILE_DSN_KINDS:
            try:
                out["path"] = make_url(dsn).database
            except Exception:  # noqa: BLE001
                out["path"] = None
        elif kind == "cube":
            out["url"] = dsn
        return out

    @staticmethod
    def serialize_data_source(ds: DataSource) -> dict:
        base = {
            "id": ds.id,
            "name": ds.name,
            "kind": ds.kind,
            "status": ds.status,
            "mapping": _loads(ds.mapping_json, None),
            "catalog_name": ds.catalog_name,
            "tested_at": ds.tested_at,
            "created_at": ds.created_at,
            "updated_at": ds.updated_at,
        }
        base.update(DataAppService._dsn_components(ds.kind, ds.dsn_secret_ref))
        return base

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

    def list_apps(
        self, db: Session, *, domain_id: str | None = None, app_type: str | None = None
    ) -> list[DataApp]:
        q = db.query(DataApp)
        if domain_id:
            q = q.filter(DataApp.domain_id == domain_id)
        if app_type:
            q = q.filter(DataApp.app_type == app_type)
        return q.order_by(desc(DataApp.updated_at)).all()

    def create_app(
        self,
        db: Session,
        *,
        domain_id: str,
        app_type: str,
        name: str | None,
        description: str | None,
        source: str,
        spec: dict | None,
        datasets: list[dict] | None,
    ) -> DataApp:
        if app_type not in APP_TYPES:
            raise ValueError(f"不支持的应用类型：{app_type}")
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ontology = self.query_service.get_published_ontology(db, domain_id)

        app = DataApp(
            domain_id=domain_id,
            ontology_id=ontology.id if ontology else None,
            app_type=app_type,
            name=name or (
                "数据看板"
                if app_type == "dashboard"
                else "数据大屏"
                if app_type == "screen"
                else "数据表格"
            ),
            description=description,
            source=source,
            spec_json=_dumps(spec or self._default_spec(app_type)),
        )
        db.add(app)
        db.flush()

        for ds_input in datasets or []:
            self._upsert_dataset(db, app, ds_input, ontology_id=app.ontology_id)

        log_change(db, "data_app", app.id, "create", summary=app.name)
        db.commit()
        db.refresh(app)
        return app

    def get_app(self, db: Session, app_id: str) -> DataApp | None:
        return (
            db.query(DataApp)
            .options(joinedload(DataApp.datasets))
            .filter(DataApp.id == app_id)
            .first()
        )

    def update_app(
        self,
        db: Session,
        app_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        spec: dict | None = None,
        datasets: list[dict] | None = None,
    ) -> DataApp:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        if name is not None:
            app.name = name
        if description is not None:
            app.description = description
        if spec is not None:
            app.spec_json = _dumps(spec)
        # 已发布应用被编辑：回到 draft，递增草稿版本号
        if app.status == "published":
            app.status = "draft"
            app.current_version = (app.published_version or app.current_version) + 1

        if datasets is not None:
            existing = {d.id: d for d in app.datasets}
            keep_ids: set[str] = set()
            for ds_input in datasets:
                ds = self._upsert_dataset(
                    db, app, ds_input, ontology_id=app.ontology_id
                )
                keep_ids.add(ds.id)
            for ds_id, ds in existing.items():
                if ds_id not in keep_ids:
                    db.delete(ds)

        log_change(db, "data_app", app.id, "update", summary=app.name)
        db.commit()
        db.refresh(app)
        return app

    def delete_app(self, db: Session, app_id: str) -> None:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        log_change(db, "data_app", app_id, "delete", summary=app.name)
        db.delete(app)
        db.commit()

    # ------------------------------------------------------------------ dataset

    def _upsert_dataset(
        self,
        db: Session,
        app: DataApp,
        ds_input: dict,
        *,
        ontology_id: str | None,
    ) -> DataAppDataset:
        binding = ds_input.get("binding") or {}
        primary_id = ds_input.get("primary_object_type_id") or binding.get(
            "primary_object_type_id"
        )
        compiled = self._compile_sql(db, binding, ontology_id=ontology_id)

        ds_id = ds_input.get("id")
        ds = db.get(DataAppDataset, ds_id) if ds_id else None
        if ds is None or ds.app_id != app.id:
            ds = DataAppDataset(app_id=app.id)
            db.add(ds)
        ds.name = ds_input.get("name") or "数据集"
        ds.primary_object_type_id = primary_id
        ds.binding_json = _dumps(binding)
        ds.compiled_sql = compiled.get("sql")
        ds.data_source_id = ds_input.get("data_source_id")
        db.flush()
        return ds

    # -------------------------------------------------------- binding compiler

    def _compile_sql(
        self, db: Session, binding: dict, *, ontology_id: str | None
    ) -> dict:
        """把口径绑定编译为确定性 SQL。返回 {sql, warnings, grounded}。

        表名/列名严格取自本体实体的 name，不臆造。业务逻辑度量内联其口径摘要。
        """
        warnings: list[str] = []
        primary_id = binding.get("primary_object_type_id")
        obj = db.get(ObjectType, primary_id) if primary_id else None
        if not obj:
            return {
                "sql": None,
                "warnings": ["未指定或无法解析主对象，无法编译 SQL"],
                "grounded": False,
            }
        if ontology_id and obj.ontology_id != ontology_id:
            warnings.append("主对象不属于当前已发布本体")

        select_parts: list[str] = []
        group_parts: list[str] = []
        where_parts: list[str] = []

        # 维度
        for dim in binding.get("dimensions") or []:
            col = self._resolve_column_name(db, dim)
            if col:
                select_parts.append(col)
                group_parts.append(col)
            else:
                warnings.append(f"维度无法落地：{dim.get('name') or dim.get('id')}")

        # 度量
        has_measure = False
        for measure in binding.get("measures") or []:
            ref = measure.get("ref") or {}
            agg = str(measure.get("agg") or "sum").lower()
            if agg not in _AGG_FUNCS:
                agg = "sum"
            if ref.get("kind") == "business_logic":
                logic = db.get(BusinessLogic, ref.get("id")) if ref.get("id") else None
                if logic:
                    expr = logic.expression_summary or f"/* {logic.name} */ COUNT(*)"
                    select_parts.append(f"({expr}) AS {logic.name}")
                    has_measure = True
                else:
                    warnings.append("业务逻辑度量无法落地")
                continue
            col = self._resolve_column_name(db, ref)
            if col:
                select_parts.append(f"{agg.upper()}({col}) AS {agg}_{col}")
                has_measure = True
            else:
                warnings.append(f"度量无法落地：{ref.get('name') or ref.get('id')}")

        if not has_measure and group_parts:
            select_parts.append("COUNT(*) AS record_count")
            has_measure = True

        # 过滤条件
        op_map = {"eq": "=", "ne": "!=", "gt": ">", "lt": "<", "ge": ">=", "le": "<="}
        for flt in binding.get("filters") or []:
            ref = flt.get("ref") or {}
            col = self._resolve_column_name(db, ref)
            if not col:
                warnings.append(f"过滤字段无法落地：{ref.get('name') or ref.get('id')}")
                continue
            op = str(flt.get("op") or "eq").lower()
            value = flt.get("value")
            if op == "like":
                where_parts.append(f"{col} LIKE '%{value}%'")
            else:
                sql_op = op_map.get(op, "=")
                literal = f"'{value}'" if isinstance(value, str) else value
                where_parts.append(f"{col} {sql_op} {literal}")

        # 时间范围
        time_range = binding.get("time_range") or {}
        tr_ref = time_range.get("ref") or {}
        tr_col = self._resolve_column_name(db, tr_ref) if tr_ref else None
        window = time_range.get("window")
        if tr_col and window in _TIME_WINDOW_DAYS:
            days = _TIME_WINDOW_DAYS[window]
            where_parts.append(
                f"{tr_col} >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)"
            )

        if not select_parts:
            # 明细查询：取主对象若干字段
            props = (
                db.query(Property)
                .filter(Property.object_type_id == obj.id)
                .order_by(Property.name)
                .limit(8)
                .all()
            )
            select_parts = [p.name for p in props] or ["*"]

        row_limit = int(binding.get("row_limit") or 100)
        sql_lines = [f"SELECT {', '.join(select_parts)}", f"FROM {obj.name}"]
        if where_parts:
            sql_lines.append("WHERE " + " AND ".join(where_parts))
        if group_parts and has_measure:
            sql_lines.append("GROUP BY " + ", ".join(group_parts))
        sql_lines.append(f"LIMIT {row_limit};")
        return {"sql": "\n".join(sql_lines), "warnings": warnings, "grounded": True}

    def _resolve_column_name(self, db: Session, ref: dict | None) -> str | None:
        if not ref:
            return None
        kind = ref.get("kind")
        if kind and kind not in {"property", "object_type"}:
            return None
        ref_id = ref.get("id")
        if ref_id:
            prop = db.get(Property, ref_id)
            if prop:
                return prop.name
        # 回退到 name（不臆造，只用于已有绑定名）
        name = ref.get("name")
        return name or None

    # ------------------------------------------------------------------ compile

    def compile_dataset(self, db: Session, app_id: str, dataset_id: str) -> dict:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        ds = db.get(DataAppDataset, dataset_id)
        if not ds or ds.app_id != app_id:
            raise ValueError("数据集不存在")
        binding = _loads(ds.binding_json, {})
        result = self._compile_sql(db, binding, ontology_id=app.ontology_id)
        ds.compiled_sql = result.get("sql")
        db.commit()
        return {
            "dataset_id": ds.id,
            "compiled_sql": result.get("sql"),
            "grounded": result.get("grounded", False),
            "warnings": result.get("warnings", []),
        }

    # ------------------------------------------------------------------ preview

    def preview_dataset(
        self,
        db: Session,
        app_id: str,
        dataset_id: str,
        limit: int = 50,
        runtime_filters: list[dict] | None = None,
        security_context: dict | None = None,
    ) -> dict:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        ds = db.get(DataAppDataset, dataset_id)
        if not ds or ds.app_id != app_id:
            raise ValueError("数据集不存在")
        binding = _loads(ds.binding_json, {})

        # 基线编译（不含运行时参数）持久化，供评审展示
        base = self._compile_sql(db, binding, ontology_id=app.ontology_id)
        ds.compiled_sql = base.get("sql")
        db.commit()

        out = self._execute_binding(
            db,
            binding=binding,
            ontology_id=app.ontology_id,
            data_source_id=ds.data_source_id,
            limit=limit,
            runtime_filters=runtime_filters,
            security_context=security_context,
        )
        out["dataset_id"] = ds.id
        return out

    def _execute_binding(
        self,
        db: Session,
        *,
        binding: dict,
        ontology_id: str | None,
        data_source_id: str | None,
        limit: int = 50,
        runtime_filters: list[dict] | None = None,
        security_context: dict | None = None,
    ) -> dict:
        """通用执行核：编译绑定(含运行时筛选) → Cube/真实源/Mock 执行 → columns/rows。

        被数据集预览与图表(Widget)预览共用。
        """
        effective = dict(binding)
        rt = [f for f in (runtime_filters or []) if isinstance(f, dict) and f.get("ref")]
        if rt:
            effective["filters"] = list(binding.get("filters") or []) + rt
        result = self._compile_sql(db, effective, ontology_id=ontology_id)
        warnings = list(result.get("warnings", []))
        source = db.get(DataSource, data_source_id) if data_source_id else None

        # 路径一：Cube 语义层
        if source and source.kind == "cube":
            try:
                columns, rows = self._cube_execute(
                    db, effective, source, security_context=security_context
                )
                return {
                    "compiled_sql": result.get("sql"),
                    "columns": columns,
                    "rows": rows,
                    "used_mock": False,
                    "warnings": warnings,
                }
            except CubeExecutionError as exc:
                warnings.append(f"Cube 执行失败，已降级为示例数据：{exc}")

        # 路径二：真实关系型数据源直连
        if source and source.kind not in ("mock", "cube") and source.dsn_secret_ref and result.get("sql"):
            try:
                columns, rows = execute_sql(
                    dsn=source.dsn_secret_ref,
                    sql=result["sql"],
                    limit=limit,
                    mapping=_loads(source.mapping_json, None),
                )
                return {
                    "compiled_sql": result.get("sql"),
                    "columns": columns,
                    "rows": rows,
                    "used_mock": False,
                    "warnings": warnings,
                }
            except ExecutionError as exc:
                warnings.append(f"真实数据源执行失败，已降级为示例数据：{exc}")

        columns, rows = self._mock_execute(db, effective, limit=limit)
        rows = self._apply_runtime_filters_to_mock(db, rows, rt)
        return {
            "compiled_sql": result.get("sql"),
            "columns": columns,
            "rows": rows,
            "used_mock": True,
            "warnings": warnings,
        }

    def _apply_runtime_filters_to_mock(
        self, db: Session, rows: list[dict], runtime_filters: list[dict]
    ) -> list[dict]:
        """Mock 模式下按运行时 eq 过滤器裁剪示例行，让参数化/下钻可见生效。"""
        if not runtime_filters or not rows:
            return rows
        for flt in runtime_filters:
            op = str(flt.get("op") or "eq").lower()
            if op != "eq":
                continue
            col = self._resolve_column_name(db, flt.get("ref") or {})
            value = flt.get("value")
            if not col or value is None:
                continue
            if any(col in r for r in rows):
                rows = [r for r in rows if str(r.get(col)) == str(value)]
        return rows

    def _cube_execute(
        self, db: Session, binding: dict, source: DataSource,
        *, security_context: dict | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """把数据集绑定翻译为 Cube 查询并执行。"""
        obj = db.get(ObjectType, binding.get("primary_object_type_id"))
        if not obj:
            raise CubeExecutionError("未解析主对象，无法构造 Cube 查询")
        connector = self._cube_connector(db, source)
        cube_query = connector.build_query(
            object_name=obj.name,
            measures=binding.get("measures") or [],
            dimensions=binding.get("dimensions") or [],
            filters=binding.get("filters") or [],
            time_range=binding.get("time_range"),
            limit=int(binding.get("row_limit") or 100),
        )
        return connector.query(cube_query, security_context=security_context)

    def _build_cube_objects(self, db: Session, ontology_id: str) -> list[dict]:
        """汇总本体对象/属性/业务逻辑/关系，组装为 CubeConnector.generate_model 的输入。"""
        from app.models import RelationType

        objects_rows = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        )
        obj_by_id = {o.id: o for o in objects_rows}
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .all()
        )
        logic_by_obj: dict[str, list[dict]] = {}
        for logic in logics:
            for b in getattr(logic, "object_bindings", []) or []:
                logic_by_obj.setdefault(b.object_type_id, []).append(
                    {
                        "name": logic.name,
                        "display_name": logic.display_name,
                        "agg": "sum",
                        "sql": logic.expression_summary or logic.name,
                    }
                )
        # 关系 → joins（挂在 source 对象上）
        relations = (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .all()
        )
        joins_by_obj: dict[str, list[dict]] = {}
        for rel in relations:
            src = obj_by_id.get(rel.source_object_type_id)
            tgt = obj_by_id.get(rel.target_object_type_id)
            if not src or not tgt:
                continue
            join = self._relation_to_join(rel, src, tgt)
            if join:
                joins_by_obj.setdefault(src.id, []).append(join)
        return [
            {
                "name": o.name,
                "display_name": o.display_name,
                "sql_table": o.name,
                "properties": [
                    {
                        "name": p.name,
                        "display_name": p.display_name,
                        "data_type": p.data_type,
                        "semantic_type": p.semantic_type,
                    }
                    for p in o.properties
                ],
                "measures": logic_by_obj.get(o.id, []),
                "joins": joins_by_obj.get(o.id, []),
            }
            for o in objects_rows
        ]

    @staticmethod
    def _relation_to_join(rel, src, tgt) -> dict | None:
        from app.connectors.cube import cube_name

        # 关系基数 → Cube relationship
        card = (rel.cardinality or "").lower().replace(" ", "")
        if card in {"n:1", "many_to_one", "many-to-one", "n-1"}:
            relationship = "many_to_one"
        elif card in {"1:n", "one_to_many", "one-to-many", "1-n"}:
            relationship = "one_to_many"
        elif card in {"1:1", "one_to_one", "one-to-one"}:
            relationship = "one_to_one"
        else:
            # N:N 需桥接表，此处不自动生成
            return None
        src_cube = cube_name(src.name)
        tgt_cube = cube_name(tgt.name)
        # 外键：优先从 source_evidence 推断，否则约定 <target>_id = <target>.id
        fk = None
        pk = "id"
        try:
            ev = _loads(rel.source_evidence, {}) or {}
            fk = ev.get("foreign_key") or ev.get("source_field") or ev.get("fk_column")
            pk = ev.get("target_field") or ev.get("pk_column") or pk
        except Exception:  # noqa: BLE001
            pass
        if not fk:
            fk = f"{tgt.name}_id"
        return {
            "target_cube": tgt_cube,
            "relationship": relationship,
            "sql": f"${{{src_cube}}}.{fk} = ${{{tgt_cube}}}.{pk}",
        }

    def generate_cube_model(self, db: Session, ontology_id: str) -> dict:
        """为一个本体生成 Cube data model（含预聚合/refreshKey/joins）。"""
        objects = self._build_cube_objects(db, ontology_id)
        return self._cube_connector(db).generate_model(objects=objects)

    def generate_cube_model_files(self, db: Session, ontology_id: str) -> dict:
        """生成可直接部署的 Cube 文件（model/cubes/*.js + cube.js，含 RLS）。"""
        connector = self._cube_connector(db)
        model = connector.generate_model(
            objects=self._build_cube_objects(db, ontology_id)
        )
        return connector.generate_model_files(model)

    def _mock_execute(
        self, db: Session, binding: dict, *, limit: int
    ) -> tuple[list[dict], list[dict]]:
        """按字段语义生成确定性 Mock 行数据（无物理数据源时）。"""
        columns: list[dict] = []
        col_keys: list[tuple[str, str]] = []  # (key, semantic)

        for dim in binding.get("dimensions") or []:
            key = self._resolve_column_name(db, dim) or (dim.get("name") or "dim")
            title = dim.get("display_name") or key
            columns.append({"key": key, "title": title})
            col_keys.append((key, "dimension"))

        measures = binding.get("measures") or []
        if measures:
            for measure in measures:
                ref = measure.get("ref") or {}
                agg = str(measure.get("agg") or "sum").lower()
                if ref.get("kind") == "business_logic":
                    key = ref.get("name") or "metric"
                    title = ref.get("display_name") or key
                else:
                    col = self._resolve_column_name(db, ref) or (ref.get("name") or "measure")
                    key = f"{agg}_{col}"
                    title = f"{agg.upper()}({ref.get('display_name') or col})"
                columns.append({"key": key, "title": title})
                col_keys.append((key, "measure"))
        elif binding.get("dimensions"):
            columns.append({"key": "record_count", "title": "记录数"})
            col_keys.append(("record_count", "measure"))

        if not columns:
            # 明细：取主对象字段
            primary_id = binding.get("primary_object_type_id")
            props = (
                db.query(Property)
                .filter(Property.object_type_id == primary_id)
                .order_by(Property.name)
                .limit(6)
                .all()
                if primary_id
                else []
            )
            for p in props:
                columns.append({"key": p.name, "title": p.display_name or p.name})
                col_keys.append((p.name, p.semantic_type or "text"))
            if not columns:
                columns.append({"key": "value", "title": "value"})
                col_keys.append(("value", "text"))

        n = min(max(limit, 1), 20)
        rows: list[dict] = []
        for i in range(n):
            row: dict[str, Any] = {}
            for key, semantic in col_keys:
                row[key] = self._mock_value(key, semantic, i)
            rows.append(row)
        return columns, rows

    @staticmethod
    def _mock_value(key: str, semantic: str, i: int) -> Any:
        seed = int(hashlib.md5(f"{key}:{i}".encode()).hexdigest(), 16)
        if semantic in {"measure", "amount"} or "amount" in key or key.startswith(("sum_", "avg_")):
            return round(1000 + (seed % 90000) / 10.0, 2)
        if semantic in {"count", "record_count"} or key == "record_count" or key.startswith("count_"):
            return seed % 500 + 1
        if semantic in {"date"} or "date" in key or "time" in key:
            month = (seed % 12) + 1
            day = (seed % 27) + 1
            return f"2026-{month:02d}-{day:02d}"
        if semantic == "dimension":
            return f"{key}-{chr(65 + (i % 6))}"
        return f"{key}_{i + 1}"

    # ------------------------------------------------------------------ publish

    def publish_app(
        self,
        db: Session,
        app_id: str,
        *,
        version_comment: str | None = None,
        operator: str | None = None,
    ) -> DataApp:
        app = self.get_app(db, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        # grounding 校验：至少一个数据集可落地编译
        if app.app_type == "data_table" or app.datasets:
            if not any(ds.compiled_sql for ds in app.datasets):
                raise ValueError("发布前请先为应用配置可落地的数据集（无可编译 SQL）")

        version = app.current_version
        snapshot_datasets = [
            {
                "id": ds.id,
                "name": ds.name,
                "primary_object_type_id": ds.primary_object_type_id,
                "binding": _loads(ds.binding_json, {}),
                "compiled_sql": ds.compiled_sql,
            }
            for ds in app.datasets
        ]
        # 引用版本锁定：快照看板所引用的图表定义，后续图表编辑不影响已发布看板
        snapshot_widgets: dict[str, dict] = {}
        spec = _loads(app.spec_json, {})
        for tile in self._spec_panels(spec):
            wid = self._panel_ref_id(tile)
            if wid and wid not in snapshot_widgets:
                w = db.get(DataAppWidget, wid)
                if w:
                    snapshot_widgets[wid] = {
                        "id": w.id,
                        "name": w.name,
                        "widget_type": w.widget_type,
                        "binding": _loads(w.binding_json, {}),
                        "viz": _loads(w.viz_json, None),
                        "data_source_id": w.data_source_id,
                        "ontology_id": w.ontology_id,
                        "compiled_sql": w.compiled_sql,
                    }
        record = DataAppVersion(
            app_id=app.id,
            version=version,
            spec_snapshot_json=app.spec_json,
            datasets_snapshot_json=_dumps(snapshot_datasets),
            widgets_snapshot_json=_dumps(snapshot_widgets),
            diff_summary=version_comment or f"发布 v{version}",
            operator=operator,
        )
        db.add(record)
        app.status = "published"
        app.published_version = version
        app.published_at = _now()
        log_change(db, "data_app", app.id, "publish", operator=operator, summary=f"v{version}")
        db.commit()
        db.refresh(app)
        return app

    def list_versions(self, db: Session, app_id: str) -> list[DataAppVersion]:
        return (
            db.query(DataAppVersion)
            .filter(DataAppVersion.app_id == app_id)
            .order_by(desc(DataAppVersion.version))
            .all()
        )

    # ------------------------------------------------------- public (read-only)

    def list_published_apps(
        self, db: Session, *, domain_id: str | None = None
    ) -> list[DataApp]:
        q = db.query(DataApp).filter(DataApp.status == "published")
        if domain_id:
            q = q.filter(DataApp.domain_id == domain_id)
        return q.order_by(desc(DataApp.published_at)).all()

    def get_published_app(self, db: Session, app_id: str) -> DataApp | None:
        app = self.get_app(db, app_id)
        if not app or app.status != "published":
            return None
        return app

    def query_published_app_data(
        self, db: Session, app_id: str, *, limit: int = 100,
        security_context: dict | None = None,
    ) -> dict:
        """对外只读：返回已发布应用各数据集与图表 tile 的数据。"""
        app = self.get_published_app(db, app_id)
        if not app:
            raise ValueError("应用不存在或未发布")
        return self._render_app_data(db, app, limit=limit, security_context=security_context)

    def _render_app_data(
        self, db: Session, app: DataApp, *, limit: int = 100,
        security_context: dict | None = None,
    ) -> dict:
        """汇总一个应用的渲染数据：各数据集预览 + （看板）各图表 tile 预览。

        已发布应用优先用【发布快照】中的图表定义执行（引用版本锁定），
        图表后续被编辑不会改变已发布看板的呈现。
        """
        datasets = [
            self.preview_dataset(db, app.id, ds.id, limit=limit, security_context=security_context)
            for ds in app.datasets
        ]
        frozen = self._published_widget_snapshot(db, app)
        widgets: dict[str, dict] = {}
        spec = _loads(app.spec_json, {})
        for tile in self._spec_panels(spec):
            wid = self._panel_ref_id(tile)
            if not wid or wid in widgets:
                continue
            snap = frozen.get(wid)
            try:
                if snap:
                    out = self._execute_binding(
                        db,
                        binding=snap.get("binding") or {},
                        ontology_id=snap.get("ontology_id"),
                        data_source_id=snap.get("data_source_id"),
                        limit=limit,
                        security_context=security_context,
                    )
                    out["widget_id"] = wid
                    widgets[wid] = out
                else:
                    widgets[wid] = self.preview_widget(
                        db, wid, limit=limit, security_context=security_context
                    )
            except ValueError:
                continue
        return {"app_id": app.id, "datasets": datasets, "widgets": widgets}

    def _published_widget_snapshot(self, db: Session, app: DataApp) -> dict[str, dict]:
        """取已发布版本的图表快照（无则空，退回实时）。"""
        if app.status != "published" or not app.published_version:
            return {}
        record = (
            db.query(DataAppVersion)
            .filter(
                DataAppVersion.app_id == app.id,
                DataAppVersion.version == app.published_version,
            )
            .order_by(desc(DataAppVersion.created_at))
            .first()
        )
        if not record:
            return {}
        return _loads(record.widgets_snapshot_json, {}) or {}

    def record_view(self, db: Session, app: DataApp) -> None:
        """轻量访问计数（对外只读页调用）。"""
        try:
            app.view_count = (app.view_count or 0) + 1
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    def lineage(self, db: Session, app_id: str) -> dict:
        """看板血缘：看板 → 图表/数据集 → 本体对象/字段。"""
        app = self.get_app(db, app_id)
        if not app:
            raise ValueError("数据应用不存在")

        obj_ids: set[str] = set()
        prop_ids: set[str] = set()

        def collect(binding: dict) -> tuple[set[str], set[str]]:
            o: set[str] = set()
            p: set[str] = set()
            if binding.get("primary_object_type_id"):
                o.add(binding["primary_object_type_id"])
            for m in binding.get("measures") or []:
                ref = m.get("ref") or {}
                if ref.get("kind") == "property" and ref.get("id"):
                    p.add(ref["id"])
            for d in binding.get("dimensions") or []:
                if d.get("id"):
                    p.add(d["id"])
            tr = (binding.get("time_range") or {}).get("ref") or {}
            if tr.get("id"):
                p.add(tr["id"])
            return o, p

        nodes: list[dict] = []
        # 本地数据集
        for ds in app.datasets:
            o, p = collect(_loads(ds.binding_json, {}))
            obj_ids |= o
            prop_ids |= p
            nodes.append({"kind": "dataset", "id": ds.id, "name": ds.name,
                          "object_type_ids": sorted(o), "property_ids": sorted(p)})
        # 引用图表
        spec = _loads(app.spec_json, {})
        seen_w: set[str] = set()
        for tile in self._spec_panels(spec):
            wid = self._panel_ref_id(tile)
            if not wid or wid in seen_w:
                continue
            seen_w.add(wid)
            w = db.get(DataAppWidget, wid)
            if not w:
                continue
            o, p = collect(_loads(w.binding_json, {}))
            obj_ids |= o
            prop_ids |= p
            nodes.append({"kind": "widget", "id": w.id, "name": w.name,
                          "object_type_ids": sorted(o), "property_ids": sorted(p)})

        objects = [
            {"id": o.id, "name": o.name, "display_name": o.display_name}
            for o in db.query(ObjectType).filter(ObjectType.id.in_(obj_ids)).all()
        ] if obj_ids else []
        props = [
            {"id": p.id, "name": p.name, "display_name": p.display_name,
             "object_type_id": p.object_type_id}
            for p in db.query(Property).filter(Property.id.in_(prop_ids)).all()
        ] if prop_ids else []
        return {
            "app_id": app.id,
            "name": app.name,
            "nodes": nodes,
            "object_types": objects,
            "properties": props,
        }

    # ----------------------------------------------------------- public share

    def enable_public_share(
        self, db: Session, app_id: str, *, password: str | None = None,
        expires_in_days: int | None = None,
    ) -> DataApp:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        if app.status != "published":
            raise ValueError("请先发布后再开启公开分享")
        if not app.public_token:
            app.public_token = secrets.token_urlsafe(24)
        app.public_enabled = True
        app.public_password_hash = hash_api_key(password) if password else None
        if expires_in_days and expires_in_days > 0:
            from datetime import timedelta

            app.public_expires_at = _now() + timedelta(days=expires_in_days)
        else:
            app.public_expires_at = None
        log_change(db, "data_app", app.id, "share_enable")
        db.commit()
        db.refresh(app)
        return app

    def disable_public_share(self, db: Session, app_id: str) -> DataApp:
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        app.public_enabled = False
        log_change(db, "data_app", app.id, "share_disable")
        db.commit()
        db.refresh(app)
        return app

    def get_public_app(self, db: Session, token: str, *, password: str | None = None) -> DataApp:
        """校验公开分享：启用/未过期/口令。失败抛 ValueError（endpoint 映射 403/404）。"""
        app = (
            db.query(DataApp)
            .options(joinedload(DataApp.datasets))
            .filter(DataApp.public_token == token)
            .first()
        )
        if not app or not app.public_enabled:
            raise ValueError("分享链接不存在或已关闭")
        if app.public_expires_at and _now() > app.public_expires_at:
            raise ValueError("分享链接已过期")
        if app.public_password_hash:
            if not password:
                raise ValueError("__PASSWORD_REQUIRED__")
            if hash_api_key(password) != app.public_password_hash:
                raise ValueError("访问口令错误")
        return app

    def public_share_status(self, app: DataApp) -> dict:
        return {
            "public_enabled": app.public_enabled,
            "public_token": app.public_token if app.public_enabled else None,
            "password_set": bool(app.public_password_hash),
            "public_expires_at": app.public_expires_at,
        }

    @staticmethod
    def serialize_public_app(app: DataApp, *, detail: bool = False) -> dict:
        data = {
            "id": app.id,
            "app_type": app.app_type,
            "name": app.name,
            "description": app.description,
            "published_version": app.published_version,
            "published_at": app.published_at,
        }
        if detail:
            data["spec"] = _loads(app.spec_json, {})
            data["datasets"] = [
                {
                    "id": ds.id,
                    "name": ds.name,
                    "compiled_sql": ds.compiled_sql,
                }
                for ds in app.datasets
            ]
        return data

    # ------------------------------------------------------------ serialization

    def serialize_app(self, db: Session, app: DataApp, *, detail: bool = False) -> dict:
        data = {
            "id": app.id,
            "domain_id": app.domain_id,
            "app_type": app.app_type,
            "name": app.name,
            "description": app.description,
            "status": app.status,
            "source": app.source,
            "current_version": app.current_version,
            "published_version": app.published_version,
            "published_at": app.published_at,
            "view_count": app.view_count,
            "created_at": app.created_at,
            "updated_at": app.updated_at,
        }
        if detail:
            data["ontology_id"] = app.ontology_id
            data["spec"] = _loads(app.spec_json, self._default_spec(app.app_type))
            data["datasets"] = [self.serialize_dataset(d) for d in app.datasets]
        return data

    @staticmethod
    def serialize_dataset(ds: DataAppDataset) -> dict:
        return {
            "id": ds.id,
            "app_id": ds.app_id,
            "name": ds.name,
            "primary_object_type_id": ds.primary_object_type_id,
            "binding": _loads(ds.binding_json, {}),
            "compiled_sql": ds.compiled_sql,
            "data_source_id": ds.data_source_id,
            "created_at": ds.created_at,
            "updated_at": ds.updated_at,
        }

    # ------------------------------------------------------ generate from chat

    async def generate_from_chat(
        self,
        db: Session,
        *,
        domain_id: str,
        app_type: str,
        question: str,
        conversation_id: str | None = None,
        name: str | None = None,
        caliber_decomposition: list[dict] | None = None,
        referenced_objects: list[dict] | None = None,
    ) -> DataApp:
        """基于 Chat BI 口径拆解生成数据应用草稿。

        一致性保证：若前端传入用户已看到的回答载荷（caliber_decomposition /
        referenced_objects），则直接复用，**不重新调用 LLM**，确保生成的应用
        与对话中展示的口径完全一致；仅当未传时才回退到重新 ask()。
        """
        from app.services.chat_bi import ChatBiService

        if app_type not in APP_TYPES:
            raise ValueError(f"不支持的应用类型：{app_type}")

        caliber = caliber_decomposition or []
        refs = referenced_objects or []
        if not caliber and not refs:
            # 未携带载荷（如直接调 API）：回退到重新问数
            answer = await ChatBiService().ask(db, domain_id=domain_id, question=question)
            if answer.get("grounding_refused") or (
                not answer.get("referenced_objects") and not answer.get("caliber_decomposition")
            ):
                raise ValueError(
                    "无法基于已发布本体将该问题落地为数据应用：未命中对象或口径。"
                )
            caliber = answer.get("caliber_decomposition") or []
            refs = answer.get("referenced_objects") or []

        binding = self._binding_from_caliber(
            db,
            caliber=caliber,
            referenced_objects=refs,
        )
        if not binding.get("primary_object_type_id"):
            raise ValueError(
                "无法基于已发布本体将该问题落地为数据应用：未命中主对象。"
            )
        dataset = {
            "name": (question[:40] or "数据集"),
            "primary_object_type_id": binding.get("primary_object_type_id"),
            "binding": binding,
        }
        spec = self._spec_from_generation(app_type, question, binding)
        app_name = name or (question[:30] if question else None)
        return self.create_app(
            db,
            domain_id=domain_id,
            app_type=app_type,
            name=app_name,
            description=f"由 Data Agent 生成：{question}"[:255],
            source="chat_generated",
            spec=spec,
            datasets=[dataset],
        )

    def _binding_from_caliber(
        self,
        db: Session,
        *,
        caliber: list[dict],
        referenced_objects: list[dict],
    ) -> dict:
        """把口径拆解 references 映射为数据集绑定。"""
        primary_id: str | None = None
        measures: list[dict] = []
        dimensions: list[dict] = []
        time_range: dict | None = None

        for item in caliber:
            for ref in item.get("references") or []:
                kind = ref.get("kind")
                if kind == "object_type" and not primary_id and ref.get("id"):
                    primary_id = ref["id"]
                elif kind == "business_logic" and ref.get("id"):
                    measures.append({"ref": ref, "agg": "sum"})
                elif kind == "property" and ref.get("id"):
                    prop = db.get(Property, ref["id"])
                    is_amount = prop and (
                        (prop.semantic_type or "") == "amount"
                        or "amount" in (prop.name or "")
                    )
                    is_date = prop and (
                        prop.semantic_type == "date" or "date" in (prop.name or "")
                    )
                    label = item.get("label", "")
                    if "度量" in label or is_amount:
                        measures.append({"ref": ref, "agg": "sum"})
                    elif "时间" in label or is_date:
                        time_range = {"ref": ref, "window": "last_30d"}
                    else:
                        dimensions.append(ref)

        if not primary_id and referenced_objects:
            primary_id = (referenced_objects[0] or {}).get("id")

        return {
            "primary_object_type_id": primary_id,
            "measures": measures,
            "dimensions": dimensions,
            "filters": [],
            "time_range": time_range,
            "row_limit": 100,
        }

    def _spec_from_generation(
        self, app_type: str, question: str, binding: dict
    ) -> dict:
        has_dim = bool(binding.get("dimensions"))
        widget_type = "bar" if has_dim else "kpi"
        if app_type == "dashboard":
            spec = self._default_spec("dashboard")
            spec["panels"] = [
                {
                    "id": "t1",
                    "widgetType": widget_type,
                    "title": question[:40],
                    "datasetIndex": 0,
                    "x": 0,
                    "y": 0,
                    "w": 6,
                    "h": 8,
                }
            ]
            return spec
        if app_type == "screen":
            return {
                "layout": "screen",
                "canvas": {"width": 1920, "height": 1080, "bg": "#0b1a2e"},
                "widgets": [
                    {
                        "id": "w1",
                        "type": widget_type,
                        "title": question[:40],
                        "rect": {"x": 80, "y": 80, "w": 760, "h": 420},
                        "datasetIndex": 0,
                    }
                ],
            }
        return {
            "layout": "table",
            "datasetIndex": 0,
            "columns": [],
            "pagination": {"pageSize": 20},
        }

    # ---------------------------------------------------------------- widgets

    def list_widgets(
        self, db: Session, *, domain_id: str | None = None, q: str | None = None,
        widget_type: str | None = None,
    ) -> list[DataAppWidget]:
        query = db.query(DataAppWidget)
        if domain_id:
            query = query.filter(DataAppWidget.domain_id == domain_id)
        if widget_type:
            query = query.filter(DataAppWidget.widget_type == widget_type)
        if q:
            query = query.filter(DataAppWidget.name.ilike(f"%{q}%"))
        return query.order_by(desc(DataAppWidget.updated_at)).all()

    def create_widget(
        self, db: Session, *, domain_id: str, name: str | None, description: str | None,
        widget_type: str, primary_object_type_id: str | None, binding: dict,
        viz: dict | None, data_source_id: str | None, source: str = "manual",
    ) -> DataAppWidget:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ontology = self.query_service.get_published_ontology(db, domain_id)
        ontology_id = ontology.id if ontology else None
        primary_id = primary_object_type_id or (binding or {}).get("primary_object_type_id")
        compiled = self._compile_sql(db, binding or {}, ontology_id=ontology_id)
        w = DataAppWidget(
            domain_id=domain_id,
            ontology_id=ontology_id,
            name=name or "未命名图表",
            description=description,
            widget_type=widget_type or "table",
            primary_object_type_id=primary_id,
            binding_json=_dumps(binding or {}),
            viz_json=_dumps(viz),
            compiled_sql=compiled.get("sql"),
            data_source_id=data_source_id,
            source=source,
        )
        db.add(w)
        db.flush()
        log_change(db, "data_app_widget", w.id, "create", summary=w.name)
        db.commit()
        db.refresh(w)
        return w

    def get_widget(self, db: Session, widget_id: str) -> DataAppWidget | None:
        return db.get(DataAppWidget, widget_id)

    def update_widget(self, db: Session, widget_id: str, **fields: Any) -> DataAppWidget:
        w = db.get(DataAppWidget, widget_id)
        if not w:
            raise ValueError("图表不存在")
        if "binding" in fields and fields["binding"] is not None:
            binding = fields.pop("binding")
            w.binding_json = _dumps(binding)
            w.primary_object_type_id = binding.get("primary_object_type_id") or w.primary_object_type_id
            compiled = self._compile_sql(db, binding, ontology_id=w.ontology_id)
            w.compiled_sql = compiled.get("sql")
        if "viz" in fields:
            w.viz_json = _dumps(fields.pop("viz"))
        for key, value in fields.items():
            if value is not None and hasattr(w, key):
                setattr(w, key, value)
        log_change(db, "data_app_widget", w.id, "update", summary=w.name)
        db.commit()
        db.refresh(w)
        return w

    def delete_widget(self, db: Session, widget_id: str) -> None:
        w = db.get(DataAppWidget, widget_id)
        if not w:
            raise ValueError("图表不存在")
        log_change(db, "data_app_widget", widget_id, "delete", summary=w.name)
        db.delete(w)
        db.commit()

    def preview_widget(
        self, db: Session, widget_id: str, *, limit: int = 50,
        runtime_filters: list[dict] | None = None, security_context: dict | None = None,
    ) -> dict:
        w = db.get(DataAppWidget, widget_id)
        if not w:
            raise ValueError("图表不存在")
        binding = _loads(w.binding_json, {})
        base = self._compile_sql(db, binding, ontology_id=w.ontology_id)
        w.compiled_sql = base.get("sql")
        db.commit()
        out = self._execute_binding(
            db,
            binding=binding,
            ontology_id=w.ontology_id,
            data_source_id=w.data_source_id,
            limit=limit,
            runtime_filters=runtime_filters,
            security_context=security_context,
        )
        out["widget_id"] = w.id
        return out

    def serialize_widget(self, w: DataAppWidget) -> dict:
        return {
            "id": w.id,
            "domain_id": w.domain_id,
            "ontology_id": w.ontology_id,
            "name": w.name,
            "description": w.description,
            "widget_type": w.widget_type,
            "primary_object_type_id": w.primary_object_type_id,
            "binding": _loads(w.binding_json, {}),
            "viz": _loads(w.viz_json, None),
            "compiled_sql": w.compiled_sql,
            "data_source_id": w.data_source_id,
            "status": w.status,
            "source": w.source,
            "created_at": w.created_at,
            "updated_at": w.updated_at,
        }

    async def generate_widget_from_chat(
        self, db: Session, *, domain_id: str, question: str, widget_type: str = "bar",
        name: str | None = None, caliber_decomposition: list[dict] | None = None,
        referenced_objects: list[dict] | None = None, dashboard_id: str | None = None,
    ) -> DataAppWidget:
        """由 Data Agent 口径生成一个可复用图表（可直接加入看板）。"""
        from app.services.chat_bi import ChatBiService

        caliber = caliber_decomposition or []
        refs = referenced_objects or []
        if not caliber and not refs:
            answer = await ChatBiService().ask(db, domain_id=domain_id, question=question)
            if answer.get("grounding_refused") or (
                not answer.get("referenced_objects") and not answer.get("caliber_decomposition")
            ):
                raise ValueError("无法基于已发布本体生成图表：未命中对象或口径。")
            caliber = answer.get("caliber_decomposition") or []
            refs = answer.get("referenced_objects") or []

        binding = self._binding_from_caliber(db, caliber=caliber, referenced_objects=refs)
        if not binding.get("primary_object_type_id"):
            raise ValueError("无法基于已发布本体生成图表：未命中主对象。")
        # 无维度时默认指标卡
        if widget_type == "bar" and not binding.get("dimensions"):
            widget_type = "kpi"
        w = self.create_widget(
            db,
            domain_id=domain_id,
            name=name or (question[:40] if question else "图表"),
            description=f"由 Data Agent 生成：{question}"[:255],
            widget_type=widget_type,
            primary_object_type_id=binding.get("primary_object_type_id"),
            binding=binding,
            viz=None,
            data_source_id=None,
            source="chat_generated",
        )
        if dashboard_id:
            self.add_widget_to_dashboard(db, dashboard_id, w.id)
        return w

    def add_widget_to_dashboard(self, db: Session, app_id: str, widget_id: str) -> DataApp:
        """把一个可复用面板（Panel）追加到看板。"""
        app = db.get(DataApp, app_id)
        if not app:
            raise ValueError("数据应用不存在")
        if app.app_type != "dashboard":
            raise ValueError("仅看板支持添加面板资产")
        w = db.get(DataAppWidget, widget_id)
        if not w:
            raise ValueError("面板不存在")
        spec = _loads(app.spec_json, self._default_spec("dashboard"))
        panels = list(self._spec_panels(spec))
        # 简单排版：每行两个
        idx = len(panels)
        panel = {
            "id": f"t{uuid.uuid4().hex[:8]}",
            "widgetType": w.widget_type,
            "title": w.name,
            "panel_id": w.id,
            "x": (idx % 2) * 6,
            "y": (idx // 2) * 8,
            "w": 6,
            "h": 8,
        }
        panels.append(panel)
        self._set_spec_panels(spec, panels)
        app.spec_json = _dumps(spec)
        if app.status == "published":
            app.status = "draft"
            app.current_version = (app.published_version or app.current_version) + 1
        log_change(db, "data_app", app.id, "add_widget", summary=w.name)
        db.commit()
        db.refresh(app)
        return app

    # -------------------------------------------------- panel(tile) spec helpers

    @staticmethod
    def _spec_panels(spec: dict) -> list[dict]:
        """读取看板面板（Panel）列表，兼容旧字段 tiles。"""
        if not isinstance(spec, dict):
            return []
        return spec.get("panels") or spec.get("tiles") or []

    @staticmethod
    def _panel_ref_id(panel: dict) -> str | None:
        """面板引用的可复用图表(Panel)ID，兼容旧字段 widget_id。"""
        return panel.get("panel_id") or panel.get("widget_id")

    @staticmethod
    def _set_spec_panels(spec: dict, panels: list[dict]) -> None:
        """写入 panels 并清理旧 tiles 字段。"""
        spec["panels"] = panels
        spec.pop("tiles", None)

    @staticmethod
    def _default_spec(app_type: str) -> dict:
        if app_type == "dashboard":
            return {
                "layout": "grid",
                "grid": {"cols": 12, "rowHeight": 40, "gap": 12},
                "theme": {"preset": "light", "bg": "#f5f7fa"},
                "filters": [],
                "panels": [],
            }
        if app_type == "screen":
            return {
                "layout": "screen",
                "canvas": {"width": 1920, "height": 1080, "bg": "#0b1a2e"},
                "widgets": [],
            }
        return {
            "layout": "table",
            "datasetId": None,
            "columns": [],
            "pagination": {"pageSize": 20},
        }
