"""依赖组件统一部署管理服务（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。

每个依赖组件在 ``dependency_components`` 表里一行：选一种部署方式（external/docker/k8s/
bare_metal），部署成功自动回写连接信息，或 external 时手填。上层功能经 ``get_*_runtime``
投影消费（Phase 1 起接读取侧；Phase 0 本服务独立运作，不碰既有五表）。

ERPNext 等外部源库不在此纳管——它们是外部数据源，走 ``DataSource``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DependencyComponent

_REPO_ROOT = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------- 组件目录

# 组件 key → (展示名, 是否多实例)。单例组件在应用层保证每 key 至多一行。
COMPONENT_CATALOG: dict[str, tuple[str, bool]] = {
    "llm": ("LLM / 嵌入服务", True),
    "datahub": ("DataHub（GMS + 前端）", False),
    "airflow": ("Airflow 调度", False),
    # 已移除：
    # - sync_runner: 新架构统一走 Flink SQL，不再需要独立搬运服务
    # - seatunnel: 已被 Flink 完全替代，不再作为独立组件
    # - cube: 语义层可选，不是核心依赖，配置生成接口保留供手动部署
    # - postgres: ontoMeta 自身数据库应在环境变量配置，不属于"依赖组件"
    # - warehouse: 目标数仓连接由「数据源」标签页统一管理（DataSourcesPanel 完整 CRUD+测试）
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
        # GraphQL 读取超时（秒）。大域(ERP ~734 表)批量拉取 + 不稳定隧道时 30s 易读超时，默认 90。
        ("request_timeout", "int", False, False, 90),
    ],
    # Airflow 一个组件、两条独立连接，故连接字段也分两组（前端分节渲染、分别拨测）：
    #   · 调度 API：endpoint + 账密。REST 版本不入配置——客户端 404 时自协商。
    #     也没有 token：Airflow 2.x REST 是 basic auth，没有任何部署路径产出过 bearer token，
    #     那个字段填了不生效，只会让人以为配了鉴权。
    #   · DAG 投递（SSH）：主机/端口/用户/目录在 deploy_spec.extra，唯独密码落在这里——
    #     extra 脱敏时原样透传（_SPEC_PRESERVE_KEYS），机密搁那儿会明文回显，且
    #     「留空=保持不变」失效；落进本 schema 才有 _mask_connection/_merge_connection 兜着。
    "airflow": [
        ("endpoint", "str", False, True, "http://localhost:8081"),
        ("username", "str", False, False, None),
        ("password", "str", True, False, None),
        ("ssh_password", "str", True, False, None),
    ],
}

# 组件的连接分组：一个组件可能同时握着几条互不相干的连接（Airflow = 调度 API + SSH 投递），
# 它们各自会通/会断，得能分开拨测、分别显示。key → [(分组 id, 分组名, 该组的连接字段)]。
# 未列出的组件只有一条连接（分组 id 固定 "default"）。
CONNECTION_GROUPS: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "airflow": [
        ("api", "调度 API", ("endpoint", "username", "password")),
        ("ssh", "DAG 投递（SSH）", ("ssh_password",)),
    ],
}


def connection_groups(key: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """取组件的连接分组；单连接组件回一条 default 分组（含全部连接字段）。"""
    groups = CONNECTION_GROUPS.get(key)
    if groups:
        return groups
    return [("default", "连接", tuple(f[0] for f in CONNECTION_SCHEMAS.get(key, [])))]


def primary_connection_group(key: str) -> str:
    """组件"自己那条"连接（第一组）。

    部署完只验它：装完 Airflow 该验的是这台服务本身活没活，而 DAG 投递（SSH）是
    ontoMeta 侧另配的一条连接，还没配就把整次安装判成失败是冤枉的。
    """
    return connection_groups(key)[0][0]

# 部署参数 schema：按 mode（跨组件通用）。
# Phase 0 只落地结构；docker/k8s/bare_metal 的实际部署在 Phase 3 实现。
DEPLOY_MODES = ["external", "docker", "k8s", "bare_metal"]

# 每组件允许的部署方式白名单：未列出的组件默认支持全部 DEPLOY_MODES。
# datahub / llm 的裸机安装随发行版/集群差异极大，无自动化价值，
# 只支持 external（登记已有服务）——前端据此收窄模式选择器，后端据此校验拦非法组合。
COMPONENT_DEPLOY_MODES: dict[str, list[str]] = {
    "datahub": ["external"],
    "llm": ["external"],
}


def allowed_deploy_modes(key: str) -> list[str]:
    """取组件允许的部署方式（无白名单则全支持）。"""
    return COMPONENT_DEPLOY_MODES.get(key, DEPLOY_MODES)


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

# 物理机模式：填目标机 IP + SSH 账密/私钥，ontoMeta 远程 SSH 安装、启动、回收连接、拨测。
# 字段 = 通用 SSH 接入参数（所有组件共用）+ 每组件少量安装参数（端口/安装目录/管理员密码）。
# 服务自身的 API 参数（如 Airflow 的端点）由安装流程自动生成回收，不再要人填。
# 安装编排见 install_recipes.py；deploy() 开 SSH → 派发 recipe → 回收 connection → 拨测。
_SSH_ACCESS_FIELDS: list[ConnectionField] = [
    ("ssh_host", "str", False, True, None),
    ("ssh_port", "int", False, False, 22),
    ("ssh_user", "str", False, True, "root"),
    ("auth_method", "str", False, False, "password"),  # password | key
    ("ssh_password", "str", True, False, None),
    ("ssh_private_key", "text", True, False, None),     # 多行 PEM 私钥
    ("ssh_key_passphrase", "str", True, False, None),
]

# 每组件的安装参数（叠加在 SSH 接入参数之后）。端口给默认值即可，管理员密码等机密标 secret。
_BARE_METAL_INSTALL_PARAMS: dict[str, list[ConnectionField]] = {
    "datahub": [
        ("gms_port", "int", False, False, 8080),
        ("frontend_port", "int", False, False, 9002),
    ],
    "airflow": [
        ("port", "int", False, False, 8081),
        ("admin_username", "str", False, False, "admin"),
        ("admin_password", "str", True, False, None),  # 留空则自动生成并回收
    ],
    "llm": [
        ("port", "int", False, True, None),
        ("path", "str", False, False, "/v1"),
        ("model", "str", False, True, ""),
        ("provider", "str", False, False, "openai-compatible"),
    ],
}

BARE_METAL_PARAMS: dict[str, list[ConnectionField]] = {
    key: [*_SSH_ACCESS_FIELDS, *_BARE_METAL_INSTALL_PARAMS.get(key, [])]
    for key in COMPONENT_CATALOG
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
                "username": spec.get("username"), "password": spec.get("password")}
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


def _spec_schema_for(key: str, mode: str) -> list[ConnectionField]:
    """按部署方式取对应的 deploy_spec 字段 schema（脱敏/合并/校验共用同一张表）。"""
    if mode == "bare_metal":
        return BARE_METAL_PARAMS.get(key, [])
    if mode == "docker":
        return DOCKER_PARAMS.get(key, [])
    if mode == "k8s":
        return DEPLOY_SPEC_SCHEMAS.get("k8s", [])
    return []


# deploy_spec 里由安装流程写回、不属于任何输入 schema 的内部键，脱敏时原样保留。
_SPEC_PRESERVE_KEYS = {"extra", "_datasource_id", "_probe"}


def _mask_deploy_spec(key: str, mode: str, spec: dict[str, Any]) -> dict[str, Any]:
    """deploy_spec 脱敏：secret 字段（SSH 密码/私钥、管理员密码等）只回 *_set + *_hint。

    非 secret 原样带出；schema 之外的内部键（extra/_datasource_id）保留，
    其余未知键为安全起见不外泄。堵住 to_out 明文回显 SSH 凭据的口子。
    """
    out: dict[str, Any] = {}
    schema = _spec_schema_for(key, mode)
    known = {f[0] for f in schema}
    for name, _typ, secret, _req, _default in schema:
        val = spec.get(name)
        if secret:
            out[f"{name}_set"] = bool(val)
            # text 型机密（PEM 私钥）不回尾 4 位：私钥尾部识别价值≈0、敏感度极高，
            # 只回是否已设 + 固定占位；密码等短机密沿用全局 last-4 提示便于用户辨认。
            if _typ == "text":
                out[f"{name}_hint"] = "****" if val else None
            else:
                out[f"{name}_hint"] = mask_secret(val if isinstance(val, str) else None)
        else:
            out[name] = val
    for k, v in spec.items():
        if k in known or k.endswith("_set") or k.endswith("_hint"):
            continue
        if k in _SPEC_PRESERVE_KEYS:
            out[k] = v  # 内部回写字段，原样保留
    return out


def _drop_probe_ledger(row: DependencyComponent) -> None:
    """把逐条拨测记账清掉：配置变了，上次的结论就不再是这份配置的结论。

    只退行状态、不清记账的话，前端那几个逐条 ✓/✗ 会拿着旧配置的结果继续显示——
    地址改对了却仍挂着红叉，是「保存即绿灯」那种假绿灯的镜像版假红灯。
    """
    spec = _loads(row.deploy_spec_json)
    if spec.pop("_probe", None) is not None:
        row.deploy_spec_json = _dumps(spec)


def _merge_deploy_spec(
    key: str, mode: str, current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """编辑态合并 deploy_spec：secret 字段留空(None/"")表示保留原值；其余覆盖。

    与连接信息的 secret-merge 语义一致，避免编辑时清空 SSH 密码/私钥。
    """
    merged = dict(current)
    schema = _spec_schema_for(key, mode)
    secret_names = {f[0] for f in schema if f[2]}
    for k, v in incoming.items():
        if k.endswith("_set") or k.endswith("_hint"):
            continue  # 脱敏回显字段不回写
        if k in secret_names and (v is None or v == ""):
            continue  # 机密留空 = 保留原值
        merged[k] = v
    return merged


# --------------------------------------------------------------------- 服务


@dataclass
class ProbeResult:
    ok: bool
    message: str
    latency_ms: int | None = None
    # 分组拨测明细：[{group, label, ok, message, latency_ms}]。单连接组件也回一条，
    # 前端据此逐条显示「调度 API ✓ / DAG 投递 ✗」而不是把两条连接糊成一个红叉。
    parts: list[dict[str, Any]] = field(default_factory=list)


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
            # 连接分组：一个组件可能握着几条互不相干的连接，前端据此分节渲染并逐条拨测。
            "connection_groups": {
                k: [
                    {"id": gid, "label": label, "fields": list(fields)}
                    for gid, label, fields in connection_groups(k)
                ]
                for k in COMPONENT_CATALOG
            },
            "deploy_modes": DEPLOY_MODES,
            # 每组件允许的部署方式（前端据此收窄模式选择器）；未列出=全支持。
            "component_deploy_modes": {
                k: allowed_deploy_modes(k) for k in COMPONENT_CATALOG
            },
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
            "deploy_spec": _mask_deploy_spec(
                row.key, row.deploy_mode, _loads(row.deploy_spec_json)
            ),
            "deploy_status": row.deploy_status,
            "deploy_error": row.deploy_error,
            "deploy_log": row.deploy_log,
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
        if mode not in allowed_deploy_modes(key):
            raise ValueError(
                f"组件 {key} 不支持部署方式 {mode}（仅支持 {'/'.join(allowed_deploy_modes(key))}）"
            )
        row = DependencyComponent(
            key=key,
            name=data.get("name") or COMPONENT_CATALOG[key][0],
            deploy_mode=mode,
            deploy_spec_json=_dumps(data.get("deploy_spec") or {}),
            deploy_status="not_deployed",
            connection_json=_dumps(conn),
            enabled=data.get("enabled", True),
            is_default=data.get("is_default", False),
        )
        if row.is_default:
            self._clear_default(db, key)
        db.add(row)
        db.commit()
        db.refresh(row)
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
            if data["deploy_mode"] not in allowed_deploy_modes(row.key):
                raise ValueError(
                    f"组件 {row.key} 不支持部署方式 {data['deploy_mode']}"
                    f"（仅支持 {'/'.join(allowed_deploy_modes(row.key))}）"
                )
            row.deploy_mode = data["deploy_mode"]
        # 连接参数分居两处（连接信息 + deploy_spec.extra 里的 SSH 主机/目录），
        # 任一处变动都让上次拨测记账失效。
        touched_config = "deploy_spec" in data or "connection" in data
        if "deploy_spec" in data:
            # secret-merge：机密字段（SSH 密码/私钥等）留空表示保留原值，避免编辑清空
            merged_spec = _merge_deploy_spec(
                row.key, row.deploy_mode, _loads(row.deploy_spec_json), data["deploy_spec"] or {}
            )
            row.deploy_spec_json = _dumps(merged_spec)
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
            # 保存连接≠连接可用：连接信息变了就把状态退回未拨测，避免"填完地址就显示已连接"
            # 的假绿灯。真正的 connected 只由拨测/部署成功回写（见 probe/deploy）。
            row.deploy_status = "not_deployed"
            row.deploy_error = None
        if touched_config:
            _drop_probe_ledger(row)
        db.commit()
        db.refresh(row)
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
        # 保存/迁移连接≠连接可用：一律置未拨测，避免假绿灯。connected 只由拨测成功回写。
        if row:
            row.connection_json = _dumps(validated)
            row.name = name
            row.enabled = enabled
            row.deploy_status = "not_deployed"
        else:
            row = DependencyComponent(
                key=key,
                name=name,
                deploy_mode="external",
                deploy_status="not_deployed",
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
                "request_timeout": data.get("request_timeout")
                or current.get("request_timeout")
                or 90,
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
            deploy_status="not_deployed",
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

    # -- 旧表迁移（幂等）：把既有 DatahubSetting/LlmServiceConfig 搬进注册表 --
    def migrate_from_legacy(self, db: Session) -> None:
        from app.models import DatahubSetting, LlmServiceConfig, AirflowSetting

        dh = db.get(DatahubSetting, "default")
        if dh and not self._get_singleton(db, "datahub"):
            self._upsert_singleton(db, "datahub", "DataHub", {
                "gms_url": dh.gms_url, "frontend_url": dh.frontend_url,
                "token": dh.token, "fabric": dh.fabric or "PROD",
            })

        if db.query(LlmServiceConfig).count() > 0 and not db.execute(
            select(DependencyComponent).where(DependencyComponent.key == "llm")
        ).scalars().first():
            for svc in db.query(LlmServiceConfig).all():
                row = DependencyComponent(
                    key="llm", name=svc.name, deploy_mode="external", deploy_status="not_deployed",
                    connection_json=_dumps(_validate_connection("llm", {
                        "provider": svc.provider, "api_base_url": svc.api_base_url,
                        "api_key": svc.api_key, "model": svc.model,
                    })),
                    enabled=svc.enabled, is_default=svc.is_default,
                )
                db.add(row)
            db.commit()

        # Airflow：连接 + 编排参数 → airflow 行
        af = db.get(AirflowSetting, "default")
        if af and not self._get_singleton(db, "airflow"):
            self._upsert_singleton(db, "airflow", "Airflow 调度", {
                "endpoint": af.endpoint, "username": af.username,
                "password": af.password,
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

    # -- Airflow（连接 + 编排参数 extra，投影为单一 dict）--
    _AIRFLOW_EXTRA_FIELDS = (
        "dags_dir",
        "max_tasks_per_dag", "max_active_tasks_per_dag",
        "dag_parse_timeout", "preflight_sentinel_timeout", "staging_swap",
        # DAG 投递（SSH 单通道）：产物 rsync 到 Airflow 主机后原子切换。
        # ontoMeta / Airflow / Flink 常分处三台机器，故没有"写本地"这种通道。
        # 密码是机密，走 CONNECTION_SCHEMAS 而非这里（extra 不脱敏）。
        # 没有私钥路径：那是 ontoMeta 主机上的文件，归该机 ~/.ssh/config 管；
        # 密码留空即走本机默认身份/agent。
        "ssh_host", "ssh_port", "ssh_user",
        # Flink 执行引擎参数（搬运/计算经 Airflow BashOperator 提交 flink run）。
        # flink_sql_runner_jar 是 **ontoMeta 侧**的 jar 路径：投递时读它的字节随包发到
        # Airflow 主机的 ontometa/_lib/，不再要求预先摆在 Airflow 机器上。
        "flink_sql_runner_jar", "flink_sql_runner_class", "flink_bin",
        "flink_deploy_target", "flink_parallelism", "flink_yarn_queue",
        "flink_checkpoint_dir",
    )

    def get_airflow(self, db: Session) -> dict[str, Any]:
        af = self._get_singleton(db, "airflow")
        af_conn = _loads(af.connection_json) if af else {}
        extra = (_loads(af.deploy_spec_json) if af else {}).get("extra", {})
        out: dict[str, Any] = {
            "endpoint": af_conn.get("endpoint", ""),
            "username": af_conn.get("username"),
            "password": af_conn.get("password"),
            "ssh_password": af_conn.get("ssh_password"),
            "enabled": af.enabled if af else False,
        }
        for f in self._AIRFLOW_EXTRA_FIELDS:
            out[f] = extra.get(f)
        out["updated_at"] = af.updated_at if af else None
        return out

    def save_airflow(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        current = self._conn(db, "airflow")
        # 连接字段：仅在 data 中出现时更新；机密为 None 时保留原值（与旧实现一致）
        af_conn = dict(current)
        for f in ("endpoint", "username", "password", "ssh_password"):
            if f in data:
                v = data[f]
                if f in ("password", "ssh_password") and v is None:
                    continue
                af_conn[f] = v
        af_row = self._get_singleton(db, "airflow")
        if af_row:
            af_row.connection_json = _dumps(af_conn)
            af_row.enabled = data.get("enabled", af_row.enabled)
            # 保存连接≠连接可用：置未拨测，connected 只由拨测成功回写。
            af_row.deploy_status = "not_deployed"
        else:
            af_row = DependencyComponent(
                key="airflow", name="Airflow 调度", deploy_mode="external",
                deploy_status="not_deployed",
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
        _drop_probe_ledger(af_row)
        db.commit()
        return self.get_airflow(db)

    # ---- 拨测 ----
    def probe(
        self, db: Session, component_id: str, target: str | None = None
    ) -> ProbeResult:
        """拨测组件的连接。

        ``target`` 指定只测哪一条连接（如 airflow 的 ``api`` / ``ssh``），省略则全测。
        一个组件的几条连接互不相干——调度 API 通不通与 SSH 投递通不通是两件事，混在
        一次拨测里只会得到一个说不清哪儿断了的红叉，故结果**逐条记账**（存
        ``deploy_spec._probe``），行状态由各条的最新结果聚合而成。
        """
        row = db.get(DependencyComponent, component_id)
        if not row:
            return ProbeResult(False, "组件不存在")
        groups = connection_groups(row.key)
        if target:
            groups = [g for g in groups if g[0] == target]
            if not groups:
                return ProbeResult(
                    False,
                    f"组件 {row.key} 没有名为 {target} 的连接"
                    f"（可选：{'/'.join(g[0] for g in connection_groups(row.key))}）",
                )
        conn = _loads(row.connection_json)
        spec = _loads(row.deploy_spec_json)
        extra = dict(spec.get("extra", {}))
        ledger = dict(spec.get("_probe", {}))

        parts: list[dict[str, Any]] = []
        for gid, label, _fields in groups:
            fn = _PROBES.get((row.key, gid))
            if fn is None:
                return ProbeResult(False, f"组件 {row.key} 暂不支持拨测")
            r = fn(conn, extra)
            part = {
                "group": gid, "label": label, "ok": r.ok,
                "message": r.message, "latency_ms": r.latency_ms,
            }
            parts.append(part)
            ledger[gid] = {**part, "at": datetime.now(timezone.utc).isoformat()}

        spec["_probe"] = ledger
        row.deploy_spec_json = _dumps(spec)
        # 行状态看**全部**连接的最新记账：只测了一条就通过，不足以把整行判成已连接。
        all_groups = connection_groups(row.key)
        recorded = [ledger.get(g[0]) for g in all_groups]
        failed = [
            f"{g[1]}：{ledger[g[0]]['message']}"
            for g in all_groups
            if ledger.get(g[0]) and not ledger[g[0]]["ok"]
        ]
        if failed:
            row.deploy_status = "failed"
            row.deploy_error = "；".join(failed)[:500]
        elif all(recorded):
            row.deploy_status = "connected"
            row.deploy_error = None
        else:
            # 还有连接没测过：不算已连接，也别报错——如实停在「未拨测」。
            row.deploy_status = "not_deployed"
            row.deploy_error = None
        db.commit()

        ok = all(p["ok"] for p in parts)
        if len(parts) == 1:
            message = parts[0]["message"]
        elif ok:
            message = "全部连接拨测通过"
        else:
            message = "；".join(f"{p['label']}：{p['message']}" for p in parts if not p["ok"])
        latency = next((p["latency_ms"] for p in parts if p["latency_ms"] is not None), None)
        return ProbeResult(ok, message, latency, parts)

    def _probe_after_deploy(self, db: Session, component_id: str) -> ProbeResult:
        """部署后只拨测组件自身那条连接（见 primary_connection_group）。

        通过时把行状态提到 ``deployed``：其余连接还没测过，聚合规则不会给 ``connected``，
        但"装完了、服务自己是通的"不该显示成未部署——那会让前端把成功的安装报成失败。
        """
        row = db.get(DependencyComponent, component_id)
        if not row:
            return ProbeResult(False, "组件不存在")
        result = self.probe(db, component_id, target=primary_connection_group(row.key))
        db.refresh(row)
        if result.ok and row.deploy_status == "not_deployed":
            row.deploy_status = "deployed"
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
        row.deploy_log = None
        db.commit()
        # 部署日志：本次部署的逐命令/阶段记录，失败/成功后都落库供前端查看
        log: list[str] = [f"== {row.key} 部署开始（mode={mode}）=="]

        try:
            if mode == "external":
                log.append("external：直接拨测已填连接")
                result = self.probe(db, component_id)
                log.append(f"拨测：{'连接成功' if result.ok else result.message}")
                row.deploy_log = "\n".join(log)
                db.commit()
                return {"status": row.deploy_status, "ok": result.ok, "message": result.message}

            spec = _loads(row.deploy_spec_json)

            if mode == "bare_metal":
                # SSH 远程安装：开会话 → 派发组件配方 → 回收 connection → 落库 → 拨测。
                from app.services.install_recipes import run_install

                conn = run_install(row.key, spec, log=log)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                # 回写配方解析出的端口等运行期值（sync_runner 自动选端口），重装/卸载保持一致。
                # 对不改 spec 的既有配方是无害 no-op。
                row.deploy_spec_json = _dumps(spec)
                row.deploy_log = "\n".join(log)
                db.commit()
                result = self._probe_after_deploy(db, component_id)
                log.append(f"拨测：{'连接成功' if result.ok else result.message}")
                row.deploy_log = "\n".join(log)
                db.commit()
                msg = result.message if not result.ok else f"SSH 远程安装完成并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            if mode == "docker":
                conn = _deploy_docker(row.key, spec, log=log)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                row.deploy_log = "\n".join(log)
                db.commit()
                result = self._probe_after_deploy(db, component_id)
                log.append(f"拨测：{'连接成功' if result.ok else result.message}")
                row.deploy_log = "\n".join(log)
                db.commit()
                msg = result.message if not result.ok else f"Docker 部署完成并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            if mode == "k8s":
                conn = _deploy_k8s(row.key, spec, log=log)
                row.connection_json = _dumps(_validate_connection(row.key, conn))
                row.deploy_log = "\n".join(log)
                db.commit()
                result = self._probe_after_deploy(db, component_id)
                log.append(f"拨测：{'连接成功' if result.ok else result.message}")
                row.deploy_log = "\n".join(log)
                db.commit()
                msg = result.message if not result.ok else f"K8s 部署完成并连通（{result.latency_ms}ms）"
                return {"status": row.deploy_status, "ok": result.ok, "message": msg}

            raise ValueError(f"未知部署方式: {mode}")
        except Exception as exc:  # noqa: BLE001
            log.append(f"! {type(exc).__name__}: {exc}")
            row.deploy_status = "failed"
            row.deploy_error = f"{type(exc).__name__}: {exc}"[:500]
            row.deploy_log = "\n".join(log)
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
        elif mode == "bare_metal":
            # SSH 模式在远端装了软件，卸载须回到目标机停服务/清理（best-effort，失败可见）
            from app.services.install_recipes import run_teardown

            err = run_teardown(row.key, spec)
        # external 无需卸载（未在远端装东西）
        row.deploy_status = "not_deployed"
        row.deploy_error = err
        db.commit()
        return {"status": row.deploy_status, "message": err}

    # ---- 部署调度（同步 external / 后台 docker·k8s·bare_metal）----
    def start_deploy(self, db: Session, component_id: str) -> dict[str, Any]:
        """部署入口：external 同步拨测直接返回；其余模式（尤其 bare_metal SSH 安装可能
        持续数分钟）先置 deploying 落库并立即返回，实际部署交后台任务执行，前端轮询状态。

        返回 need_background=True 时，调用方（路由）须调度 run_deploy_detached。
        """
        row = db.get(DependencyComponent, component_id)
        if not row:
            raise ValueError("组件不存在")
        if row.deploy_mode == "external":
            result = self.deploy(db, component_id)
            return {**result, "need_background": False}
        # 后台模式：先占位 deploying，避免前端在任务起来前看到旧状态
        row.deploy_status = "deploying"
        row.deploy_error = None
        row.deploy_log = None
        db.commit()
        return {
            "status": "deploying",
            "ok": True,
            "message": "部署已在后台开始，请稍候刷新查看状态",
            "need_background": True,
        }

    def run_deploy_detached(self, component_id: str) -> None:
        """后台任务体：用独立 DB session 跑实际部署。deploy() 自身管状态/错误落库，
        这里只负责 session 生命周期与异常兜底（防止后台线程里未捕获异常吞掉状态）。"""
        with SessionLocal() as db:
            try:
                self.deploy(db, component_id)
            except Exception as exc:  # noqa: BLE001 - 后台任务不得静默失败
                row = db.get(DependencyComponent, component_id)
                if row is not None:
                    row.deploy_status = "failed"
                    row.deploy_error = f"{type(exc).__name__}: {exc}"[:500]
                    db.commit()


# --------------------------------------------------------------------- 拨测实现

import time  # noqa: E402
from app.services.common import make_http_client  # noqa: E402


def _probe_llm(conn: dict[str, Any], extra: dict[str, Any]) -> ProbeResult:
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


def _probe_airflow_api(conn: dict[str, Any], extra: dict[str, Any]) -> ProbeResult:
    """调度 API 两步拨测：/health 探通，再打带版本前缀的 REST 探鉴权。

    只测 ``/health`` 会给假绿灯——它在 2.x 默认匿名可读，而触发 DagRun 走的
    ``/api/{version}/*`` 可能因为没开 basic_auth 后端而 401。REST 版本由客户端
    自协商，拨测因此也不必知道实例是 2.x 还是 3.x。
    """
    from app.connectors.airflow import AirflowClient, AirflowError, explain_ping_failure

    endpoint = (conn.get("endpoint") or "").strip()
    if not endpoint:
        return ProbeResult(False, "缺少 endpoint")
    client = AirflowClient(
        endpoint, username=conn.get("username"), password=conn.get("password")
    )
    start = time.perf_counter()
    try:
        client.health()
    except AirflowError as exc:
        return ProbeResult(False, str(exc)[:300])
    try:
        client.ping_api()
    except AirflowError as exc:
        # /health 通、REST 不通：按 401/403（鉴权）与 404/405（路径）补充下一步。
        return ProbeResult(False, explain_ping_failure(client, exc)[:300])
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])
    else:
        return ProbeResult(True, "连接成功", int((time.perf_counter() - start) * 1000))
    finally:
        client.close()


def _probe_airflow_ssh(conn: dict[str, Any], extra: dict[str, Any]) -> ProbeResult:
    """DAG 投递拨测：真连一次 Airflow 主机，并确认 DAG 目录可写。

    与调度 API 拨测分开：这两条连接常常一通一断（API 走 8080 端口、投递走 22 端口，
    甚至不是同一台机器），合成一次拨测只会告诉用户"Airflow 有问题"却指不出是哪条。
    复用 preflight 的 ``probe_ssh_pipeline``，与提交前自检判的是同一件事。
    """
    from app.services.materialize_preflight import probe_ssh_pipeline

    host = (extra.get("ssh_host") or "").strip()
    dags_dir = (extra.get("dags_dir") or "").strip()
    if not host:
        return ProbeResult(False, "缺少 SSH 主机（DAG 产物没有地方可投）")
    if not dags_dir:
        return ProbeResult(False, "缺少 DAG 目录（Airflow 主机上的路径）")

    cfg = SimpleNamespace(  # probe_ssh_pipeline 只读这几个字段
        ssh_host=host,
        ssh_user=(extra.get("ssh_user") or "").strip(),
        ssh_port=int(extra.get("ssh_port") or 22),
        ssh_password=conn.get("ssh_password") or None,
        dags_dir=dags_dir,
    )
    start = time.perf_counter()
    ok, detail = probe_ssh_pipeline(cfg)
    return ProbeResult(ok, detail[:300], int((time.perf_counter() - start) * 1000) if ok else None)




def _probe_datahub(conn: dict[str, Any], extra: dict[str, Any]) -> ProbeResult:
    """拨测 DataHub：打运行时真正用到的 GraphQL 端点，并校验响应确实是 GMS。

    只 ``GET {gms_url}/config`` 看状态码会给假绿灯——若 ``gms_url`` 误填成前端 SPA
    端口（如 :9002），任意 GET 路径都被前端路由兜底成 ``200`` + HTML 首页，只看状态码
    的探测便误判"连接成功"，而真实取域走的 ``POST {gms_url}/api/graphql`` 却 404。
    这里改打真实的 GraphQL POST（与 ``connectors/datahub.py`` 同一路径/方法/鉴权），
    并要求响应是 GraphQL JSON（含 ``data``、无 ``errors``），从而把"端口/路径填错"
    与"token 无权限"都暴露成明确失败，而非假绿灯。与 Airflow 拨测同款思路。
    """
    import httpx

    base = (conn.get("gms_url") or "").strip().rstrip("/")
    if not base:
        return ProbeResult(False, "缺少 gms_url")
    url = f"{base}/api/graphql"
    headers = {"Content-Type": "application/json"}
    token = conn.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    start = time.perf_counter()
    try:
        with make_http_client() as client:
            resp = client.post(
                url,
                json={"query": "{ listDomains(input: {start: 0, count: 1}) { total } }"},
                headers=headers,
                timeout=8.0,
            )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])
    latency = int((time.perf_counter() - start) * 1000)

    if resp.status_code in (401, 403):
        return ProbeResult(False, f"鉴权失败 HTTP {resp.status_code}：请检查 token", latency)
    if resp.status_code >= 400:
        return ProbeResult(
            False,
            f"HTTP {resp.status_code}：{resp.text[:100]}"
            "（gms_url 是否填成了前端端口？应指向 GMS，通常 :8080）",
            latency,
        )
    # 必须解析为 GraphQL JSON——前端 SPA 兜底返回的是 HTML，会在此暴露。
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return ProbeResult(
            False,
            "响应不是 JSON（疑似前端 SPA 页面）：gms_url 应指向 GMS（通常 :8080）而非前端端口",
            latency,
        )
    if payload.get("errors"):
        return ProbeResult(False, f"GraphQL 错误：{str(payload['errors'])[:160]}", latency)
    if not isinstance(payload.get("data"), dict):
        return ProbeResult(False, "响应缺少 GraphQL data 字段（端点可能不是 DataHub GMS）", latency)
    return ProbeResult(True, "连接成功", latency)


# (组件 key, 连接分组 id) → 探针。分组见 CONNECTION_GROUPS；单连接组件用 "default"。
_PROBES: dict[tuple[str, str], Any] = {
    ("llm", "default"): _probe_llm,
    ("datahub", "default"): _probe_datahub,
    ("airflow", "api"): _probe_airflow_api,
    ("airflow", "ssh"): _probe_airflow_ssh,
}


# --------------------------------------------------------------------- Docker/K8s 部署

import subprocess  # noqa: E402


def _run(cmd: list[str], timeout: float = 120, log: list[str] | None = None) -> tuple[int, str, str]:
    """跑一条命令，返回 (returncode, stdout, stderr)。超时则杀。``log``：部署日志收集器。"""
    if log is not None:
        log.append("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if log is not None and proc.returncode != 0:
        log.append(f"! rc={proc.returncode}：{(proc.stderr or proc.stdout or '').strip()[-300:]}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _deploy_docker(key: str, spec: dict[str, Any], log: list[str] | None = None) -> dict[str, Any]:
    """docker compose up + docker compose port 回收映射端口 → 拼 connection。

    需 deploy_spec: compose_file（仓库内路径）、service（compose 服务名）、container_port。
    缺 compose_file 时回退到 docker/components/<key>.yml；都没有则报错。
    """
    compose_file = spec.get("compose_file") or str(_REPO_ROOT / "docker" / "components" / f"{key}.yml")
    service = spec.get("service") or key
    container_port = spec.get("container_port")
    if not container_port:
        # 各组件默认容器端口
        defaults = {"datahub": 8080, "airflow": 8080}
        container_port = defaults.get(key)
    if not container_port:
        raise ValueError("docker 部署需指定 container_port")
    rc, _, err = _run(["docker", "compose", "-f", compose_file, "up", "-d"], timeout=180, log=log)
    if rc != 0:
        raise RuntimeError(f"docker compose up 失败: {err or '未知错误'}（compose_file={compose_file}）")
    rc, out, err = _run(["docker", "compose", "-f", compose_file, "port", service, str(container_port)], log=log)
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
        "datahub": "gms_port", "airflow": "port",
        "llm": "port",
    }
    f = port_fields.get(key)
    if f:
        spec[f] = port


def _teardown_docker(key: str, spec: dict[str, Any]) -> str | None:
    compose_file = spec.get("compose_file") or str(_REPO_ROOT / "docker" / "components" / f"{key}.yml")
    rc, _, err = _run(["docker", "compose", "-f", compose_file, "down"], timeout=120)
    return None if rc == 0 else f"docker compose down 失败: {err}"


def _deploy_k8s(key: str, spec: dict[str, Any], log: list[str] | None = None) -> dict[str, Any]:
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
    rc, _, err = _run(["kubectl", "apply", "-n", namespace, "-f", path], timeout=180, log=log)
    if rc != 0:
        raise RuntimeError(f"kubectl apply 失败: {err}")
    # 取同名 Service 的端口
    rc, out, err = _run(
        ["kubectl", "get", "svc", key, "-n", namespace, "-o", "jsonpath={.spec.ports[0].nodePort}"],
        log=log,
    )
    port_str = out.strip() if rc == 0 else ""
    if not port_str or not port_str.isdigit():
        # 试 clusterIP:port
        rc, out, _ = _run(
            ["kubectl", "get", "svc", key, "-n", namespace,
             "-o", "jsonpath={.spec.clusterIP}:{.spec.ports[0].port}"],
            log=log,
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
