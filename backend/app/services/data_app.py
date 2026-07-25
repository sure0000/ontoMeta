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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    DataApp,
    DataAppDataset,
    DataAppVersion,
    DataSource,
    DomainContext,
    ObjectType,
    Property,
)
from app.services.common import log_change
from app.services.data_app_executor import ExecutionError, execute_sql, is_read_only
from app.connectors.cube import CubeConnector, CubeExecutionError
from app.services.ontology_query import OntologyQueryService
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.data_app")

APP_TYPES = {"data_table", "screen", "dashboard"}
_AGG_FUNCS = {"sum", "count", "avg", "max", "min"}
_TIME_WINDOW_DAYS = {
    "last_7d": 7,
    "last_30d": 30,
    "last_90d": 90,
    "today": 0,
    "this_month": 30,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        mapping: dict | None = None,
    ) -> DataSource:
        ds = DataSource(
            name=name,
            kind=kind,
            dsn_secret_ref=dsn_secret_ref,
            mapping_json=_dumps(mapping),
        )
        db.add(ds)
        db.commit()
        db.refresh(ds)
        return ds

    def update_data_source(self, db: Session, ds_id: str, **fields: Any) -> DataSource:
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        if "mapping" in fields:
            ds.mapping_json = _dumps(fields.pop("mapping"))
        for key, value in fields.items():
            if value is not None and hasattr(ds, key):
                setattr(ds, key, value)
        db.commit()
        db.refresh(ds)
        return ds

    @staticmethod
    def serialize_data_source(ds: DataSource) -> dict:
        return {
            "id": ds.id,
            "name": ds.name,
            "kind": ds.kind,
            "status": ds.status,
            "mapping": _loads(ds.mapping_json, None),
            "tested_at": ds.tested_at,
            "created_at": ds.created_at,
            "updated_at": ds.updated_at,
        }

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

        # 运行时参数：把参数化筛选/下钻条件合并进 binding.filters 后再编译执行
        effective = dict(binding)
        rt = [f for f in (runtime_filters or []) if isinstance(f, dict) and f.get("ref")]
        if rt:
            effective["filters"] = list(binding.get("filters") or []) + rt
        result = self._compile_sql(db, effective, ontology_id=app.ontology_id)

        warnings = list(result.get("warnings", []))
        # 优先真实数据源执行；失败或未配置时降级为 Mock
        source = db.get(DataSource, ds.data_source_id) if ds.data_source_id else None

        # 路径一：Cube 语义层执行
        if source and source.kind == "cube":
            try:
                columns, rows = self._cube_execute(
                    db, ds, effective, source, security_context=security_context
                )
                return {
                    "dataset_id": ds.id,
                    "compiled_sql": result.get("sql"),
                    "columns": columns,
                    "rows": rows,
                    "used_mock": self._cube_connector(db, source).use_mock,
                    "warnings": warnings,
                }
            except CubeExecutionError as exc:
                warnings.append(f"Cube 执行失败，已降级为示例数据：{exc}")

        # 路径二：真实关系型数据源直连执行；失败或未配置时降级为 Mock
        if source and source.kind not in ("mock", "cube") and source.dsn_secret_ref and result.get("sql"):
            try:
                columns, rows = execute_sql(
                    dsn=source.dsn_secret_ref,
                    sql=result["sql"],
                    limit=limit,
                    mapping=_loads(source.mapping_json, None),
                )
                return {
                    "dataset_id": ds.id,
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
            "dataset_id": ds.id,
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
        self, db: Session, ds: DataAppDataset, binding: dict, source: DataSource,
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
        record = DataAppVersion(
            app_id=app.id,
            version=version,
            spec_snapshot_json=app.spec_json,
            datasets_snapshot_json=_dumps(snapshot_datasets),
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
        """对外只读：返回已发布应用各数据集的数据（可带行级权限上下文）。"""
        app = self.get_published_app(db, app_id)
        if not app:
            raise ValueError("应用不存在或未发布")
        datasets = []
        for ds in app.datasets:
            datasets.append(
                self.preview_dataset(
                    db, app.id, ds.id, limit=limit, security_context=security_context
                )
            )
        return {"app_id": app.id, "datasets": datasets}

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
            spec["tiles"] = [
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

    @staticmethod
    def _default_spec(app_type: str) -> dict:
        if app_type == "dashboard":
            return {
                "layout": "grid",
                "grid": {"cols": 12, "rowHeight": 40, "gap": 12},
                "theme": {"preset": "light", "bg": "#f5f7fa"},
                "filters": [],
                "tiles": [],
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
