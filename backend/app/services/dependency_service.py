"""依赖组件统一部署管理服务（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。

每个依赖组件在 ``dependency_components`` 表里一行：选一种部署方式（external/docker/k8s/
bare_metal），部署成功自动回写连接信息，或 external 时手填。上层功能经 ``get_*_runtime``
投影消费（Phase 1 起接读取侧；Phase 0 本服务独立运作，不碰既有五表）。

ERPNext 等外部源库不在此纳管——它们是外部数据源，走 ``DataSource``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DependencyComponent

_REPO_ROOT = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------- 组件目录

# 组件 key → (展示名, 是否多实例)。单例组件在应用层保证每 key 至多一行。
COMPONENT_CATALOG: dict[str, tuple[str, bool]] = {
    "llm": ("LLM / 嵌入服务", True),
    "datahub": ("DataHub（GMS + 前端）", False),
    "airflow": ("Airflow 调度", False),
    "seatunnel": ("SeaTunnel 搬运", False),
    "warehouse": ("目标数仓（Doris/Hive/MySQL/…）", True),
    "sync_runner": ("sync-runner", False),
    "cube": ("Cube 语义层", False),
    "postgres": ("ontoMeta 自身 PostgreSQL", False),
    "bigtop": ("Bigtop Manager（Hive 备选）", False),
}

SINGLETON_KEYS = {k for k, (_, multi) in COMPONENT_CATALOG.items() if not multi}
MULTI_KEYS = {k for k, (_, multi) in COMPONENT_CATALOG.items() if multi}

# 连接字段 schema：每 key 的 connection JSON 形态。
# 字段元组 (字段名, 类型, 是否机密, 是否必填, 默认值/None)。
# 类型：str/int/bool；secret=True 时读侧只回 *_set + *_hint（掩码）。
ConnectionField = tuple[str, str, bool, bool, Any]
CONNECTION_SCHEMAS: dict[str, list[ConnectionField]] = {
    "llm": [
        ("provider", "str", False, False, "deepseek"),
        ("api_base_url", "str", False, True, "https://api.deepseek.com"),
        ("api_key", "str", True, False, None),
        ("model", "str", False, True, "deepseek-v4-flash"),
    ],
    "datahub": [
        ("gms_url", "str", False, True, "http://localhost:8080"),
        ("frontend_url", "str", False, True, "http://localhost:9002"),
        ("token", "str", True, False, None),
        ("fabric", "str", False, False, "PROD"),
    ],
    "airflow": [
        ("endpoint", "str", False, True, "http://localhost:8081"),
        ("username", "str", False, False, None),
        ("password", "str", True, False, None),
        ("token", "str", True, False, None),
        ("api_version", "str", False, False, "v1"),
    ],
    "seatunnel": [
        ("rest_endpoint", "str", False, True, "http://localhost:5801"),
    ],
    "warehouse": [
        ("sqlalchemy_url", "str", True, True, None),
        ("dialect", "str", False, True, "doris"),
    ],
    "sync_runner": [
        ("endpoint", "str", False, True, "http://localhost:8098"),
        ("token", "str", True, False, None),
    ],
    "cube": [
        ("api_url", "str", False, True, "http://localhost:4000"),
        ("api_secret", "str", True, False, None),
        ("preagg_refresh", "str", False, False, "1 hour"),
        ("tenant_dimension", "str", False, False, None),
        ("timeout_seconds", "int", False, False, 30),
    ],
    "postgres": [
        ("sqlalchemy_url", "str", True, True, None),
    ],
    "bigtop": [
        ("api_url", "str", False, True, "http://localhost:18080"),
    ],
}

# 部署参数 schema：按 mode（跨组件通用）。
# Phase 0 只落地结构；docker/k8s/bare_metal 的实际部署在 Phase 3 实现。
DEPLOY_MODES = ["external", "docker", "k8s", "bare_metal"]
DEPLOY_SPEC_SCHEMAS: dict[str, list[ConnectionField]] = {
    "external": [],
    "docker": [
        ("image", "str", False, False, None),
        ("compose_file", "str", False, False, None),
        ("network", "str", False, False, None),
    ],
    "k8s": [
        ("namespace", "str", False, False, "default"),
        ("manifest", "str", False, False, None),
    ],
    "bare_metal": [
        ("host", "str", False, True, None),
    ],
}

# 物理机模式：按组件给出「登记一台已装好的物理服务」所需的参数（host + 端口 + 凭据）。
# deploy() 据此拼出 connection 并拨测——这是物理机模式的「部署执行」：
# 不 SSH 装软件（那需运维脚本与授权），而是登记现成服务 + 自动回收连接 + 验活。
BARE_METAL_PARAMS: dict[str, list[ConnectionField]] = {
    "datahub": [
        ("host", "str", False, True, None), ("gms_port", "int", False, False, 8080),
        ("frontend_port", "int", False, False, 9002), ("token", "str", True, False, None),
    ],
    "airflow": [
        ("host", "str", False, True, None), ("port", "int", False, False, 8081),
        ("username", "str", False, False, None), ("password", "str", True, False, None),
        ("token", "str", True, False, None), ("api_version", "str", False, False, "v1"),
    ],
    "seatunnel": [("host", "str", False, True, None), ("port", "int", False, False, 5801)],
    "warehouse": [
        ("host", "str", False, True, None), ("port", "int", False, True, None),
        ("dialect", "str", False, True, "doris"), ("username", "str", False, True, "root"),
        ("password", "str", True, False, None), ("database", "str", False, False, ""),
    ],
    "sync_runner": [
        ("host", "str", False, True, None), ("port", "int", False, False, 8098),
        ("token", "str", True, False, None),
    ],
    "cube": [
        ("host", "str", False, True, None), ("port", "int", False, False, 4000),
        ("api_secret", "str", True, False, None),
    ],
    "postgres": [
        ("host", "str", False, True, None), ("port", "int", False, False, 5432),
        ("username", "str", False, True, "ontometa"), ("password", "str", True, True, None),
        ("database", "str", False, True, "ontometa"),
    ],
    "bigtop": [("host", "str", False, True, None), ("port", "int", False, False, 18080)],
    "llm": [
        ("host", "str", False, True, None), ("port", "int", False, True, None),
        ("path", "str", False, False, "/v1"), ("api_key", "str", True, False, None),
        ("model", "str", False, True, ""), ("provider", "str", False, False, "openai-compatible"),
    ],
}

# Docker 模式：compose 片段 + 要探测的服务名与容器端口（用于 docker compose port 回收映射）。
DOCKER_PARAMS: dict[str, list[ConnectionField]] = {
    key: [
        ("compose_file", "str", False, False, None),
        ("service", "str", False, False, None),
        ("container_port", "int", False, False, None),
    ]
    for key in COMPONENT_CATALOG
}


def build_bare_metal_connection(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """从物理机参数（host + 端口 + 凭据）拼出 connection。"""
    h = (spec.get("host") or "").strip()
    if not h:
        raise ValueError("缺少 host")
    if key == "datahub":
        return {"gms_url": f"http://{h}:{spec.get('gms_port', 8080)}",
                "frontend_url": f"http://{h}:{spec.get('frontend_port', 9002)}",
                "token": spec.get("token"), "fabric": "PROD"}
    if key == "airflow":
        return {"endpoint": f"http://{h}:{spec.get('port', 8081)}",
                "username": spec.get("username"), "password": spec.get("password"),
                "token": spec.get("token"), "api_version": spec.get("api_version", "v1")}
    if key == "seatunnel":
        return {"rest_endpoint": f"http://{h}:{spec.get('port', 5801)}"}
    if key in ("warehouse", "postgres"):
        dialect = spec.get("dialect", "doris" if key == "warehouse" else "postgresql")
        user = spec.get("username", "root")
        pwd = spec.get("password") or ""
        port = spec.get("port", 5432 if key == "postgres" else 9030)
        db = spec.get("database", "")
        # postgresql 走 psycopg 驱动；其余数仓走 pymysql（MySQL 线协议）
        driver = "+psycopg" if dialect == "postgresql" else "+pymysql"
        return {"sqlalchemy_url": f"{dialect}{driver}://{user}:{pwd}@{h}:{port}/{db}", "dialect": dialect}
    if key == "sync_runner":
        return {"endpoint": f"http://{h}:{spec.get('port', 8098)}", "token": spec.get("token")}
    if key == "cube":
        return {"api_url": f"http://{h}:{spec.get('port', 4000)}", "api_secret": spec.get("api_secret"),
                "preagg_refresh": "1 hour", "tenant_dimension": None, "timeout_seconds": 30}
    if key == "bigtop":
        return {"api_url": f"http://{h}:{spec.get('port', 18080)}"}
    if key == "llm":
        return {"provider": spec.get("provider", "openai-compatible"),
                "api_base_url": f"http://{h}:{spec.get('port')}{spec.get('path', '/v1')}",
                "api_key": spec.get("api_key"), "model": spec.get("model", "")}
    raise ValueError(f"组件 {key} 暂不支持物理机部署")

DEPLOY_STATUSES = ["not_deployed", "deploying", "deployed", "failed", "connected"]


# --------------------------------------------------------------------- 工具


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


def _mask_connection(key: str, conn: dict[str, Any]) -> dict[str, Any]:
    """连接信息脱敏：secret 字段只回 *_set + *_hint，非 secret 原样。"""
    out: dict[str, Any] = {}
    for name, _typ, secret, _req, _default in CONNECTION_SCHEMAS.get(key, []):
        val = conn.get(name)
        if secret:
            out[f"{name}_set"] = bool(val)
            out[f"{name}_hint"] = mask_secret(val if isinstance(val, str) else None)
        else:
            out[name] = val
    # 保留 schema 之外已存的字段（向前兼容），非机密原样带出
    known = {f[0] for f in CONNECTION_SCHEMAS.get(key, [])}
    for k, v in conn.items():
        if k not in known and not k.endswith("_set") and not k.endswith("_hint"):
            out[k] = v
    return out


def _validate_connection(
    key: str, conn: dict[str, Any], *, require: bool = True
) -> dict[str, Any]:
    """按 schema 校验连接信息：必填非空、类型粗验。返回清洗后的 dict。

    ``require=False``：跳过必填校验——bare_metal/docker/k8s 的连接是**部署时**
    从 deploy_spec 拼出来的，创建/编辑时连接还是空的，此刻不该以「必填」拦下。
    """
    schema = CONNECTION_SCHEMAS.get(key)
    if schema is None:
        raise ValueError(f"未知组件类型: {key}")
    cleaned: dict[str, Any] = {}
    for name, typ, _secret, req, default in schema:
        val = conn.get(name, default)
        if require and req and (val is None or (isinstance(val, str) and not val.strip())):
            raise ValueError(f"连接字段 {name} 必填")
        if val is not None and typ == "int":
            try:
                val = int(val)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"连接字段 {name} 须为整数") from exc
        cleaned[name] = val
    return cleaned


# --------------------------------------------------------------------- 服务


@dataclass
class ProbeResult:
    ok: bool
    message: str
    latency_ms: int | None = None


class DependencyComponentService:
    """依赖组件 CRUD + schema 分发 + 拨测分派 + 部署分派（Phase 0：部署仅 external 可用）。"""

    # ---- schema 自描述（供前端表单生成）----
    def schema(self) -> dict[str, Any]:
        def field_list(fields: list[ConnectionField]) -> list[dict[str, Any]]:
            return [
                {"name": n, "type": t, "secret": s, "required": r, "default": d}
                for n, t, s, r, d in fields
            ]

        return {
            "components": [
                {"key": k, "label": label, "multi": multi}
                for k, (label, multi) in COMPONENT_CATALOG.items()
            ],
            "connection_schemas": {
                k: field_list(fields) for k, fields in CONNECTION_SCHEMAS.items()
            },
            "deploy_modes": DEPLOY_MODES,
            "deploy_spec_schemas": {
                m: field_list(fields) for m, fields in DEPLOY_SPEC_SCHEMAS.items()
            },
            "bare_metal_params": {
                k: field_list(fields) for k, fields in BARE_METAL_PARAMS.items()
            },
            "docker_params": {
                k: field_list(fields) for k, fields in DOCKER_PARAMS.items()
            },
            "deploy_statuses": DEPLOY_STATUSES,
        }

    # ---- 查询 ----
    def list_components(self, db: Session) -> list[DependencyComponent]:
        rows = db.execute(
            select(DependencyComponent).order_by(
                DependencyComponent.key, DependencyComponent.is_default.desc()
            )
        ).scalars().all()
        return list(rows)

    def get_component(self, db: Session, component_id: str) -> DependencyComponent | None:
        return db.get(DependencyComponent, component_id)

    def _get_singleton(self, db: Session, key: str) -> DependencyComponent | None:
        return db.execute(
            select(DependencyComponent).where(DependencyComponent.key == key)
        ).scalar_one_or_none()

    # ---- 序列化（脱敏）----
    def to_out(self, row: DependencyComponent) -> dict[str, Any]:
        return {
            "id": row.id,
            "key": row.key,
            "name": row.name,
            "deploy_mode": row.deploy_mode,
            "deploy_spec": _loads(row.deploy_spec_json),
            "deploy_status": row.deploy_status,
            "deploy_error": row.deploy_error,
            "connection": _mask_connection(row.key, _loads(row.connection_json)),
            "enabled": row.enabled,
            "is_default": row.is_default,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # ---- 增删改 ----
    def create_component(self, db: Session, data: dict[str, Any]) -> DependencyComponent:
        key = data["key"]
        if key not in COMPONENT_CATALOG:
            raise ValueError(f"未知组件类型: {key}")
        if key in SINGLETON_KEYS and self._get_singleton(db, key):
            raise ValueError(f"组件 {key} 为单例，已存在")
        mode = data.get("deploy_mode", "external")
        # 非 external 模式：连接由部署时自动拼出，此时允许为空
        conn = _validate_connection(key, data.get("connection") or {}, require=(mode == "external"))
        if mode not in DEPLOY_MODES:
            raise ValueError(f"未知部署方式: {mode}")
        row = DependencyComponent(
            key=key,
            name=data.get("name") or COMPONENT_CATALOG[key][0],
            deploy_mode=mode,
            deploy_spec_json=_dumps(data.get("deploy_spec") or {}),
            deploy_status="connected" if mode == "external" and conn else "not_deployed",
            connection_json=_dumps(conn),
            enabled=data.get("enabled", True),
            is_default=data.get("is_default", False),
        )
        if row.is_default:
            self._clear_default(db, key)
        db.add(row)
        db.commit()
        db.refresh(row)
        if row.key == "warehouse":
            self._link_warehouse_datasource(db, row)
        return row

    def update_component(
        self, db: Session, component_id: str, data: dict[str,Any]
    ) -> DependencyComponent | None:
        row = db.get(DependencyComponent, component_id)
        if not row:
            return None
        if "name" in data:
            row.name = data["name"]
        if "deploy_mode" in data and data["deploy_mode"] in DEPLOY_MODES:
            row.deploy_mode = data["deploy_mode"]
        if "deploy_spec" in data:
            row.deploy_spec_json = _dumps(data["deploy_spec"] or {})
        if "enabled" in data:
            row.enabled = data["enabled"]
        if "is_default" in data:
            if data["is_default"]:
                self._clear_default(db, row.key)
            row.is_default = data["is_default"]
        # 连接信息：按 schema 校验；secret 字段留空(None)表示保留原值
        if "connection" in data:
            conn = self._merge_connection(row, data["connection"])
            # 非 external 模式连接可为空（部署时自动拼），只在 external 模式强校验
            row.connection_json = _dumps(_validate_connection(row.key, conn, require=(row.deploy_mode == "external")))
            if row.deploy_mode == "external":
                row.deploy_status = "connected"
        db.commit()
        db.refresh(row)
        if row.key == "warehouse":
            self._link_warehouse_datasource(db, row)
        return row

    def _merge_connection(self, row: DependencyComponent, incoming: dict[str, Any]) -> dict[str, Any]:
        """编辑态 secret 字段留空表示保留原值；非 secret 直接覆盖。"""
        current = _loads(row.connection_json)
        for name, _typ, secret, _req, _default in CONNECTION_SCHEMAS.get(row.key, []):
            if name not in incoming:
                continue
            val = incoming[name]
            if secret and (val is None or val == ""):
                continue  # 保留原值
            current[name] = val
        # schema 之外的字段直接覆盖
        known = {f[0] for f in CONNECTION_SCHEMAS.get(row.key, [])}
        for k, v in incoming.items():
            if k not in known:
                current[k] = v
        return current

    def delete_component(self, db: Session, component_id: str) -> bool:
        row = db.get(DependencyComponent, component_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    def _clear_default(self, db: Session, key: str) -> None:
        for r in db.execute(
            select(DependencyComponent).where(
                DependencyComponent.key == key, DependencyComponent.is_default.is_(True)
            )
        ).scalars().all():
            r.is_default = False

    def _link_warehouse_datasource(self, db: Session, warehouse_row: DependencyComponent) -> None:
        """注册表 warehouse 组件 → 自动创建/更新对应的 DataSource，使其立刻可作为物化目标使用。

        warehouse connection 有 sqlalchemy_url 时，导出为 DataSource（kind 从 dialect 推）。
        DataSource.id 回写到 warehouse 的 deploy_spec._datasource_id 以便双向索引。
        """
        from app.models.data_app import DataSource

        conn = _loads(warehouse_row.connection_json)
        dsn = (conn.get("sqlalchemy_url") or "").strip()
        if not dsn:
            return  # 连接未配，跳过
        dialect = (conn.get("dialect") or "doris").strip().lower()
        # 方言 → DataSource kind
        kind_map = {"doris": "doris", "mysql": "mysql", "postgresql": "postgres",
                    "hive": "hive", "clickhouse": "clickhouse", "starrocks": "doris"}
        kind = kind_map.get(dialect, dialect)
        # 回收已存在的链接
        spec = _loads(warehouse_row.deploy_spec_json)
        existing_ds_id = spec.get("_datasource_id")
        ds = db.get(DataSource, existing_ds_id) if existing_ds_id else None
        if ds:
            ds.name = warehouse_row.name
            ds.kind = kind
            ds.dsn_secret_ref = dsn
            ds.status = "connected" if warehouse_row.deploy_status == "connected" else "untested"
        else:
            ds = DataSource(
                name=warehouse_row.name, kind=kind, dsn_secret_ref=dsn,
                status="connected" if warehouse_row.deploy_status == "connected" else "untested",
            )
            db.add(ds)
            db.flush()
            spec["_datasource_id"] = ds.id
            warehouse_row.deploy_spec_json = _dumps(spec)
        db.commit()

    # ---- Phase 1：投影读/写（供 SettingsService 委托，保持既有返回结构）----

    def _conn(self, db: Session, key: str) -> dict[str, Any]:
        """取单例组件的连接信息（未配置时返回空 dict）。"""
        row = self._get_singleton(db, key)
        return _loads(row.connection_json) if row else {}

    def _upsert_singleton(
        self, db: Session, key: str, name: str, conn: dict[str, Any], enabled: bool = True
    ) -> DependencyComponent:
        row = self._get_singleton(db, key)
        validated = _validate_connection(key, conn)
        if row:
            row.connection_json = _dumps(validated)
            row.name = name
            row.enabled = enabled
            row.deploy_status = "connected"
        else:
            row = DependencyComponent(
                key=key,
                name=name,
                deploy_mode="external",
                deploy_status="connected",
                connection_json=_dumps(validated),
                enabled=enabled,
            )
            db.add(row)
        db.commit()
        db.refresh(row)
        return row

    # -- DataHub --
    def get_datahub(self, db: Session) -> dict[str, Any]:
        row = self._get_singleton(db, "datahub")
        if not row:
            return {}
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    def save_datahub(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        current = self._conn(db, "datahub")
        row = self._upsert_singleton(
            db, "datahub", "DataHub",
            {
                "gms_url": data.get("gms_url", ""),
                "frontend_url": data.get("frontend_url", ""),
                "token": data.get("token") or current.get("token"),
                "fabric": data.get("fabric", "PROD"),
            },
        )
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    # -- Cube --
    def get_cube(self, db: Session) -> dict[str, Any]:
        row = self._get_singleton(db, "cube")
        if not row:
            return {}
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    def save_cube(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        current = self._conn(db, "cube")
        row = self._upsert_singleton(
            db, "cube", "Cube 语义层",
            {
                "api_url": data.get("api_url", ""),
                "api_secret": data.get("api_secret") or current.get("api_secret"),
                "preagg_refresh": data.get("preagg_refresh", "1 hour"),
                "tenant_dimension": data.get("tenant_dimension"),
                "timeout_seconds": data.get("timeout_seconds", 30),
            },
        )
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    # -- LLM (多实例) --
    def list_llm(self, db: Session) -> list[dict[str, Any]]:
        rows = db.execute(
            select(DependencyComponent)
            .where(DependencyComponent.key == "llm")
            .order_by(DependencyComponent.is_default.desc(), DependencyComponent.updated_at.desc())
        ).scalars().all()
        return [self._llm_view(r) for r in rows]

    def _llm_view(self, row: DependencyComponent) -> dict[str, Any]:
        c = _loads(row.connection_json)
        return {
            "id": row.id,
            "name": row.name,
            "provider": c.get("provider", "deepseek"),
            "api_base_url": c.get("api_base_url", ""),
            "api_key": c.get("api_key"),
            "model": c.get("model", ""),
            "is_default": row.is_default,
            "enabled": row.enabled,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def get_llm(self, db: Session, service_id: str) -> dict[str, Any] | None:
        row = db.get(DependencyComponent, service_id)
        if not row or row.key != "llm":
            return None
        return self._llm_view(row)

    def get_default_llm(self, db: Session) -> dict[str, Any] | None:
        row = db.execute(
            select(DependencyComponent).where(
                DependencyComponent.key == "llm",
                DependencyComponent.is_default.is_(True),
                DependencyComponent.enabled.is_(True),
            )
        ).scalar_one_or_none()
        if not row:
            row = db.execute(
                select(DependencyComponent).where(
                    DependencyComponent.key == "llm", DependencyComponent.enabled.is_(True)
                )
            ).scalar_one_or_none()
        return self._llm_view(row) if row else None

    def create_llm(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("is_default"):
            self._clear_default(db, "llm")
        row = DependencyComponent(
            key="llm",
            name=data.get("name", "LLM"),
            deploy_mode="external",
            deploy_status="connected",
            connection_json=_dumps(_validate_connection("llm", {
                "provider": data.get("provider", "deepseek"),
                "api_base_url": data.get("api_base_url", ""),
                "api_key": data.get("api_key"),
                "model": data.get("model", ""),
            })),
            enabled=data.get("enabled", True),
            is_default=data.get("is_default", False),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        if not db.execute(
            select(DependencyComponent).where(
                DependencyComponent.key == "llm", DependencyComponent.is_default.is_(True)
            )
        ).scalar_one_or_none():
            row.is_default = True
            db.commit()
            db.refresh(row)
        return self._llm_view(row)

    def update_llm(self, db: Session, service_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        row = db.get(DependencyComponent, service_id)
        if not row or row.key != "llm":
            return None
        if data.get("is_default"):
            self._clear_default(db, "llm")
        if "name" in data:
            row.name = data["name"]
        if "enabled" in data:
            row.enabled = data["enabled"]
        if "is_default" in data:
            row.is_default = data["is_default"]
        conn = _loads(row.connection_json)
        for f in ("provider", "api_base_url", "model"):
            if f in data:
                conn[f] = data[f]
        if data.get("api_key"):
            conn["api_key"] = data["api_key"]
        row.connection_json = _dumps(conn)
        db.commit()
        db.refresh(row)
        return self._llm_view(row)

    def delete_llm(self, db: Session, service_id: str) -> bool:
        row = db.get(DependencyComponent, service_id)
        if not row or row.key != "llm":
            return False
        was_default = row.is_default
        db.delete(row)
        db.commit()
        if was_default:
            fallback = db.execute(
                select(DependencyComponent).where(DependencyComponent.key == "llm")
                .order_by(DependencyComponent.updated_at.desc())
            ).scalars().first()
            if fallback:
                fallback.is_default = True
                db.commit()
        return True

    # -- 旧表迁移（幂等）：把既有 DatahubSetting/CubeSetting/LlmServiceConfig 搬进注册表 --
    def migrate_from_legacy(self, db: Session) -> None:
        from app.models import DatahubSetting, CubeSetting, LlmServiceConfig, AirflowSetting

        dh = db.get(DatahubSetting, "default")
        if dh and not self._get_singleton(db, "datahub"):
            self._upsert_singleton(db, "datahub", "DataHub", {
                "gms_url": dh.gms_url, "frontend_url": dh.frontend_url,
                "token": dh.token, "fabric": dh.fabric or "PROD",
            })

        cube = db.get(CubeSetting, "default")
        if cube and not self._get_singleton(db, "cube"):
            self._upsert_singleton(db, "cube", "Cube 语义层", {
                "api_url": cube.api_url, "api_secret": cube.api_secret,
                "preagg_refresh": cube.preagg_refresh,
                "tenant_dimension": cube.tenant_dimension,
                "timeout_seconds": cube.timeout_seconds,
            })

        if db.query(LlmServiceConfig).count() > 0 and not db.execute(
            select(DependencyComponent).where(DependencyComponent.key == "llm")
        ).scalars().first():
            for svc in db.query(LlmServiceConfig).all():
                row = DependencyComponent(
                    key="llm", name=svc.name, deploy_mode="external", deploy_status="connected",
                    connection_json=_dumps(_validate_connection("llm", {
                        "provider": svc.provider, "api_base_url": svc.api_base_url,
                        "api_key": svc.api_key, "model": svc.model,
                    })),
                    enabled=svc.enabled, is_default=svc.is_default,
                )
                db.add(row)
            db.commit()

        # Airflow：连接 → airflow 行，sync_runner 连接 → sync_runner 行，编排参数 → airflow 行 extra
        af = db.get(AirflowSetting, "default")
        if af and not self._get_singleton(db, "airflow"):
            self._upsert_singleton(db, "airflow", "Airflow 调度", {
                "endpoint": af.endpoint, "username": af.username,
                "password": af.password, "token": af.token,
                "api_version": af.api_version,
            }, enabled=af.enabled)
            # 编排参数落 extra
            af_row = self._get_singleton(db, "airflow")
            if af_row:
                af_row.deploy_spec_json = _dumps({"extra": {
                    "dags_dir": af.dags_dir, "jobs_dir": af.jobs_dir,
                    "sync_channel": af.sync_channel, "docker_network": af.docker_network,
                    "drivers_dir": af.drivers_dir, "sync_tool_images": af.sync_tool_images,
                    "sync_tool": af.sync_tool, "max_tasks_per_dag": af.max_tasks_per_dag,
                    "max_active_tasks_per_dag": af.max_active_tasks_per_dag,
                    "dag_parse_timeout": af.dag_parse_timeout,
                    "preflight_sentinel_timeout": af.preflight_sentinel_timeout,
                    "staging_swap": af.staging_swap,
                }})
                db.commit()
        if af and not self._get_singleton(db, "sync_runner") and af.sync_runner_endpoint:
            self._upsert_singleton(db, "sync_runner", "sync-runner", {
                "endpoint": af.sync_runner_endpoint, "token": af.sync_runner_token,
            })

    # -- Airflow（连接 + sync_runner + 编排参数 extra，投影为单一 dict）--
    _AIRFLOW_EXTRA_FIELDS = (
        "dags_dir", "jobs_dir", "sync_channel", "docker_network", "drivers_dir",
        "sync_tool_images", "sync_tool", "max_tasks_per_dag", "max_active_tasks_per_dag",
        "dag_parse_timeout", "preflight_sentinel_timeout", "staging_swap",
        # DAG 投递方式（local 默认 / git-sync）与 git 参数。跨机部署时 git-sync 把产物
        # push 到远程仓，Airflow 侧拉取——全部在设置页填，不进配置文件。
        "dag_delivery_method", "git_remote", "git_branch", "git_auto_init",
        "git_author", "git_email",
    )

    def get_airflow(self, db: Session) -> dict[str, Any]:
        af = self._get_singleton(db, "airflow")
        sr = self._get_singleton(db, "sync_runner")
        af_conn = _loads(af.connection_json) if af else {}
        sr_conn = _loads(sr.connection_json) if sr else {}
        extra = (_loads(af.deploy_spec_json) if af else {}).get("extra", {})
        out: dict[str, Any] = {
            "endpoint": af_conn.get("endpoint", ""),
            "username": af_conn.get("username"),
            "password": af_conn.get("password"),
            "token": af_conn.get("token"),
            "api_version": af_conn.get("api_version", "v1"),
            "enabled": af.enabled if af else False,
            "sync_runner_endpoint": sr_conn.get("endpoint", ""),
            "sync_runner_token": sr_conn.get("token"),
        }
        for f in self._AIRFLOW_EXTRA_FIELDS:
            out[f] = extra.get(f)
        out["updated_at"] = af.updated_at if af else None
        return out

    def save_airflow(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        current = self._conn(db, "airflow")
        # 连接字段：仅在 data 中出现时更新；password/token 为 None 时保留原值（与旧实现一致）
        af_conn = dict(current)
        for f in ("endpoint", "username", "password", "token", "api_version"):
            if f in data:
                v = data[f]
                if f in ("password", "token") and v is None:
                    continue
                af_conn[f] = v
        af_conn.setdefault("api_version", "v1")
        af_row = self._get_singleton(db, "airflow")
        if af_row:
            af_row.connection_json = _dumps(af_conn)
            af_row.enabled = data.get("enabled", af_row.enabled)
            af_row.deploy_status = "connected" if af_conn.get("endpoint") else "not_deployed"
        else:
            af_row = DependencyComponent(
                key="airflow", name="Airflow 调度", deploy_mode="external",
                deploy_status="connected" if af_conn.get("endpoint") else "not_deployed",
                connection_json=_dumps(af_conn), enabled=data.get("enabled", True),
            )
            db.add(af_row)
        # 编排参数落 extra（仅更新 data 中出现的字段）
        spec = _loads(af_row.deploy_spec_json)
        extra = dict(spec.get("extra", {}))
        for f in self._AIRFLOW_EXTRA_FIELDS:
            if f in data:
                extra[f] = data[f]
        spec["extra"] = extra
        af_row.deploy_spec_json = _dumps(spec)
        # sync_runner 连接行：仅在 data 出现时更新；token 为 None 保留原值
        sr_current = self._conn(db, "sync_runner")
        if "sync_runner_endpoint" in data or "sync_runner_token" in data:
            sr_conn = dict(sr_current)
            if "sync_runner_endpoint" in data:
                sr_conn["endpoint"] = data["sync_runner_endpoint"]
            if "sync_runner_token" in data:
                t = data["sync_runner_token"]
                if t is not None:
                    sr_conn["token"] = t
            sr_row = self._get_singleton(db, "sync_runner")
            if sr_row:
                sr_row.connection_json = _dumps(sr_conn)
            else:
                db.add(DependencyComponent(
                    key="sync_runner", name="sync-runner", deploy_mode="external",
                    deploy_status="connected" if sr_conn.get("endpoint") else "not_deployed",
                    connection_json=_dumps(sr_conn),
                ))
        db.commit()
        return self.get_airflow(db)

    # ---- 拨测 ----
    def probe(self, db: Session, component_id: str) -> ProbeResult:
        row = db.get(DependencyComponent, component_id)
        if not row:
            return ProbeResult(False, "组件不存在")
        conn = _loads(row.connection_json)
        try:
            result = _PROBES[row.key](conn)
        except KeyError:
            return ProbeResult(False, f"组件 {row.key} 暂不支持拨测")
        if result.ok:
            row.deploy_status = "connected"
            row.deploy_error = None
        else:
            row.deploy_status = "failed"
            row.deploy_error = result.message
        db.commit()
        return result

    # ---- 部署 ----
    def deploy(self, db: Session, component_id: str) -> dict[str, Any]:
        """执行部署，自动回收连接信息并拨测。

        - external：不部署，直接拨测已填连接。
        - bare_metal：从 host+端口+凭据拼出 connection，落库后拨测（登记一台现成物理服务）。
        - docker：docker compose up 起服务，docker compose port 回收映射端口，拼 connection 后拨测。
        - k8s：kubectl apply 起服务，kubectl get svc 回收端点，拼 connection 后拨测。
        """
        row = db.get(DependencyComponent, component_id)
        if not row:
            raise ValueError("组件不存在")
        mode = row.deploy_mode
        row.deploy_status = "deploying"
        row.deploy_error = None
        db.commit()

        try:
            if mode == "external":
                result = self.probe(db, component_id)
                return {"status": row.deploy_status, "ok": result.ok, "message": result.message}

            spec = _loads(row.deploy_spec_json)

            if mode == "bare_metal":
                conn = build_bare_metal_connection(row.key, spec)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                db.commit()
                if row.key == "warehouse":
                    self._link_warehouse_datasource(db, row)
                result = self.probe(db, component_id)
                msg = result.message if not result.ok else f"已登记物理机服务并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            if mode == "docker":
                conn = _deploy_docker(row.key, spec)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                db.commit()
                if row.key == "warehouse":
                    self._link_warehouse_datasource(db, row)
                result = self.probe(db, component_id)
                msg = result.message if not result.ok else f"Docker 部署完成并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            if mode == "k8s":
                conn = _deploy_k8s(row.key, spec)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                db.commit()
                if row.key == "warehouse":
                    self._link_warehouse_datasource(db, row)
                result = self.probe(db, component_id)
                msg = result.message if not result.ok else f"K8s 部署完成并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            raise ValueError(f"未知部署方式: {mode}")
        except Exception as exc:  # noqa: BLE001
            row.deploy_status = "failed"
            row.deploy_error = f"{type(exc).__name__}: {exc}"[:500]
            db.commit()
            return {"status": row.deploy_status, "ok": False, "message": row.deploy_error}

    def teardown(self, db: Session, component_id: str) -> dict[str, Any]:
        row = db.get(DependencyComponent, component_id)
        if not row:
            raise ValueError("组件不存在")
        spec = _loads(row.deploy_spec_json)
        mode = row.deploy_mode
        err = None
        if mode == "docker":
            err = _teardown_docker(row.key, spec)
        elif mode == "k8s":
            err = _teardown_k8s(row.key, spec)
        # bare_metal/external 无需卸载（未在远端装东西）
        row.deploy_status = "not_deployed"
        row.deploy_error = err
        db.commit()
        return {"status": row.deploy_status, "message": err}


# --------------------------------------------------------------------- 拨测实现

import time  # noqa: E402
from app.services.common import make_http_client  # noqa: E402


def _probe_http(url: str, timeout: float = 8.0) -> ProbeResult:
    import httpx

    start = time.perf_counter()
    try:
        with make_http_client() as client:
            resp = client.get(url, timeout=timeout)
        ok = 200 <= resp.status_code < 400
        return ProbeResult(
            ok,
            f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code}: {resp.text[:120]}",
            int((time.perf_counter() - start) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])


def _probe_llm(conn: dict[str, Any]) -> ProbeResult:
    from openai import OpenAI

    base = (conn.get("api_base_url") or "").strip()
    model = (conn.get("model") or "").strip()
    key = conn.get("api_key") or "EMPTY"
    if not base or not model:
        return ProbeResult(False, "缺少 api_base_url 或 model")
    start = time.perf_counter()
    try:
        client = OpenAI(api_key=key, base_url=base, timeout=15, max_retries=0, http_client=make_http_client())
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1)
        return ProbeResult(True, "连接成功", int((time.perf_counter() - start) * 1000))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])


def _probe_sqlalchemy(conn: dict[str, Any]) -> ProbeResult:
    from sqlalchemy import create_engine, text

    url = (conn.get("sqlalchemy_url") or "").strip()
    if not url:
        return ProbeResult(False, "缺少 sqlalchemy_url")
    start = time.perf_counter()
    try:
        eng = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return ProbeResult(True, "连接成功", int((time.perf_counter() - start) * 1000))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])


def _probe_airflow(conn: dict[str, Any]) -> ProbeResult:
    """两步拨测：/health 探通，再打带版本前缀的 REST 探鉴权（复用既有 AirflowClient 逻辑）。"""
    from app.connectors.airflow import AirflowClient, AirflowError

    endpoint = (conn.get("endpoint") or "").strip()
    if not endpoint:
        return ProbeResult(False, "缺少 endpoint")
    client = AirflowClient(
        endpoint,
        username=conn.get("username"),
        password=conn.get("password"),
        token=conn.get("token"),
        api_version=conn.get("api_version") or "v1",
    )
    start = time.perf_counter()
    try:
        client.health()
        client.ping_api()
        return ProbeResult(True, "连接成功", int((time.perf_counter() - start) * 1000))
    except AirflowError as exc:
        return ProbeResult(False, str(exc)[:300])
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])
    finally:
        client.close()


def _probe_sync_runner(conn: dict[str, Any]) -> ProbeResult:
    from app.connectors.sync_runner import SyncRunnerClient, SyncRunnerError

    endpoint = (conn.get("endpoint") or "").strip()
    if not endpoint:
        return ProbeResult(False, "缺少 endpoint")
    client = SyncRunnerClient(endpoint, token=conn.get("token"))
    start = time.perf_counter()
    try:
        client.list_secrets()
        return ProbeResult(True, "连接成功", int((time.perf_counter() - start) * 1000))
    except SyncRunnerError as exc:
        return ProbeResult(False, str(exc)[:300])
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])
    finally:
        client.close()


_PROBES: dict[str, Any] = {
    "llm": _probe_llm,
    "datahub": lambda c: _probe_http(f"{(c.get('gms_url') or '').rstrip('/')}/config"),
    "airflow": _probe_airflow,
    "seatunnel": lambda c: _probe_http(f"{(c.get('rest_endpoint') or '').rstrip('/')}/api/v1/info"),
    "warehouse": _probe_sqlalchemy,
    "sync_runner": _probe_sync_runner,
    "cube": lambda c: _probe_http(f"{(c.get('api_url') or '').rstrip('/')}/livez"),
    "postgres": _probe_sqlalchemy,
    "bigtop": lambda c: _probe_http(c.get("api_url", "")),
}


# --------------------------------------------------------------------- Docker/K8s 部署

import subprocess  # noqa: E402


def _run(cmd: list[str], timeout: float = 120) -> tuple[int, str, str]:
    """跑一条命令，返回 (returncode, stdout, stderr)。超时则杀。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _deploy_docker(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """docker compose up + docker compose port 回收映射端口 → 拼 connection。

    需 deploy_spec: compose_file（仓库内路径）、service（compose 服务名）、container_port。
    缺 compose_file 时回退到 docker/components/<key>.yml；都没有则报错。
    """
    compose_file = spec.get("compose_file") or str(_REPO_ROOT / "docker" / "components" / f"{key}.yml")
    service = spec.get("service") or key
    container_port = spec.get("container_port")
    if not container_port:
        # 各组件默认容器端口
        defaults = {"datahub": 8080, "airflow": 8080, "seatunnel": 5801, "cube": 4000,
                    "sync_runner": 8098, "postgres": 5432, "bigtop": 18080}
        container_port = defaults.get(key)
    if not container_port:
        raise ValueError("docker 部署需指定 container_port")
    rc, _, err = _run(["docker", "compose", "-f", compose_file, "up", "-d"], timeout=180)
    if rc != 0:
        raise RuntimeError(f"docker compose up 失败: {err or '未知错误'}（compose_file={compose_file}）")
    rc, out, err = _run(["docker", "compose", "-f", compose_file, "port", service, str(container_port)])
    if rc != 0:
        raise RuntimeError(f"docker compose port 失败: {err}（service={service}, port={container_port}）")
    # out 形如 "0.0.0.0:8080" 或 ":::8080"
    host_port = out.split(":")[-1].strip()
    if not host_port.isdigit():
        raise RuntimeError(f"无法解析映射端口: {out!r}")
    # 复用 bare_metal 的连接拼装逻辑（host=localhost + 端口=映射端口）
    bm_spec = {**spec, "host": "localhost"}
    # 把 container_port 映射到 bare_metal 参数里对应的端口字段
    _fill_port(bm_spec, key, int(host_port))
    return build_bare_metal_connection(key, bm_spec)


def _fill_port(spec: dict[str, Any], key: str, port: int) -> None:
    """把回收到的端口填进 bare_metal 参数里该组件的端口字段。"""
    port_fields = {
        "datahub": "gms_port", "airflow": "port", "seatunnel": "port",
        "warehouse": "port", "sync_runner": "port", "cube": "port",
        "postgres": "port", "bigtop": "port", "llm": "port",
    }
    f = port_fields.get(key)
    if f:
        spec[f] = port


def _teardown_docker(key: str, spec: dict[str, Any]) -> str | None:
    compose_file = spec.get("compose_file") or str(_REPO_ROOT / "docker" / "components" / f"{key}.yml")
    rc, _, err = _run(["docker", "compose", "-f", compose_file, "down"], timeout=120)
    return None if rc == 0 else f"docker compose down 失败: {err}"


def _deploy_k8s(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """kubectl apply + kubectl get svc 回收端点 → 拼 connection。

    需 deploy_spec: manifest（YAML 文本，多文档）或其内含一个名为 <key> 的 Service。
    namespace 默认 default。回收 Service 的第一个 node/host 端口。
    """
    namespace = spec.get("namespace", "default")
    manifest = spec.get("manifest")
    if not manifest:
        raise ValueError("k8s 部署需提供 manifest（YAML 文本）")
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        path = f.name
    rc, _, err = _run(["kubectl", "apply", "-n", namespace, "-f", path], timeout=180)
    if rc != 0:
        raise RuntimeError(f"kubectl apply 失败: {err}")
    # 取同名 Service 的端口
    rc, out, err = _run(
        ["kubectl", "get", "svc", key, "-n", namespace, "-o", "jsonpath={.spec.ports[0].nodePort}"]
    )
    port_str = out.strip() if rc == 0 else ""
    if not port_str or not port_str.isdigit():
        # 试 clusterIP:port
        rc, out, _ = _run(
            ["kubectl", "get", "svc", key, "-n", namespace,
             "-o", "jsonpath={.spec.clusterIP}:{.spec.ports[0].port}"]
        )
        if rc == 0 and ":" in out:
            host, p = out.split(":", 1)
            bm_spec = {**spec, "host": host}
            _fill_port(bm_spec, key, int(p))
            return build_bare_metal_connection(key, bm_spec)
        raise RuntimeError(f"无法回收 Service 端口: {err or out}")
    bm_spec = {**spec, "host": "localhost"}
    _fill_port(bm_spec, key, int(port_str))
    return build_bare_metal_connection(key, bm_spec)


def _teardown_k8s(key: str, spec: dict[str, Any]) -> str | None:
    namespace = spec.get("namespace", "default")
    manifest = spec.get("manifest")
    if not manifest:
        return None
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        path = f.name
    rc, _, err = _run(["kubectl", "delete", "-n", namespace, "-f", path, "--ignore-not-found=true"], timeout=120)
    return None if rc == 0 else f"kubectl delete 失败: {err}"
