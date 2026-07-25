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
from app.services.ontology_query import OntologyQueryService

logger = logging.getLogger("ontometa.data_app")

APP_TYPES = {"data_table", "screen"}
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

    # ------------------------------------------------------------- data sources

    def list_data_sources(self, db: Session) -> list[DataSource]:
        return db.query(DataSource).order_by(desc(DataSource.created_at)).all()

    def create_data_source(
        self, db: Session, *, name: str, kind: str, dsn_secret_ref: str | None
    ) -> DataSource:
        ds = DataSource(name=name, kind=kind, dsn_secret_ref=dsn_secret_ref)
        db.add(ds)
        db.commit()
        db.refresh(ds)
        return ds

    def update_data_source(self, db: Session, ds_id: str, **fields: Any) -> DataSource:
        ds = db.get(DataSource, ds_id)
        if not ds:
            raise ValueError("数据源不存在")
        for key, value in fields.items():
            if value is not None and hasattr(ds, key):
                setattr(ds, key, value)
        db.commit()
        db.refresh(ds)
        return ds

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
        # 阶段 1：mock 类型直接标记可用；真实连接测试延后到阶段 2
        ds.status = "ok" if ds.kind == "mock" else "untested"
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
            name=name or ("数据大屏" if app_type == "screen" else "数据表格"),
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
        self, db: Session, app_id: str, dataset_id: str, limit: int = 50
    ) -> dict:
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

        columns, rows = self._mock_execute(db, binding, limit=limit)
        return {
            "dataset_id": ds.id,
            "compiled_sql": result.get("sql"),
            "columns": columns,
            "rows": rows,
            "used_mock": True,
            "warnings": result.get("warnings", []),
        }

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
    ) -> DataApp:
        """复用 Chat BI 口径拆解结果，生成一个数据应用草稿。"""
        from app.services.chat_bi import ChatBiService

        if app_type not in APP_TYPES:
            raise ValueError(f"不支持的应用类型：{app_type}")
        answer = await ChatBiService().ask(db, domain_id=domain_id, question=question)
        if answer.get("grounding_refused") or (
            not answer.get("referenced_objects") and not answer.get("caliber_decomposition")
        ):
            raise ValueError(
                "无法基于已发布本体将该问题落地为数据应用：未命中对象或口径。"
            )

        binding = self._binding_from_caliber(
            db,
            caliber=answer.get("caliber_decomposition") or [],
            referenced_objects=answer.get("referenced_objects") or [],
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
            description=f"由智能问数生成：{question}"[:255],
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
        if app_type == "screen":
            has_dim = bool(binding.get("dimensions"))
            widget_type = "bar" if has_dim else "kpi"
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
