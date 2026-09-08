"""依赖组件登记服务：LLM / DataHub / Airflow 的连接信息 + 拨测。

ontoMeta **不部署任何依赖**，一律连接已经跑着的服务。所以这里只有「怎么连」和
「上次拨测通不通」，没有部署方式、部署参数、部署日志。组件也是**固定**的那几样
（见 ``COMPONENT_CATALOG``），不由用户增删——``ensure_components`` 兜底把缺的行补齐，
面板只负责填连接。上层功能经 ``get_*_runtime`` 投影消费。

ERPNext 等外部源库不在此纳管——它们是外部数据源，走 ``DataSource``；目标数仓走
「数据源」标签页；ontoMeta 自身数据库属于 bootstrap 配置，都不是"依赖组件"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DependencyComponent

# --------------------------------------------------------------------- 组件目录

# 固定组件目录：key → 展示名，顺序即面板里的显示顺序。每 key 至多一行（单例）。
COMPONENT_CATALOG: dict[str, str] = {
    "llm": "LLM / 嵌入服务",
    "datahub": "DataHub（GMS + 前端）",
    "airflow": "Airflow 调度",
    "superset": "Superset BI",
}

# 借本表存运行期配置、但不是"外部服务"的 key（当前只有 mcp）：它们不在
# COMPONENT_CATALOG 里，因此既不出现在面板上、也不被 ensure_components 补齐。
# MCP 是 ontoMeta 自己的服务，由「Agent 接入」页管理。
INTERNAL_KEYS = {"mcp"}

# 补齐新行时默认**不启用**的组件：Airflow 一启用，物化/搬运就会真的往它上面投 DAG，
# 而刚补出来的行只有占位地址。要等人填完连接、拨测通过再自己打开。
# Superset 同理：刚补出来的行是 localhost:8088 占位，启用着只会让建图工具去连一个
# 不存在的地址、报一句网络错；关着则能明说"组件未启用，请先在设置页配置并拨测"。
DEFAULT_DISABLED_KEYS = {"airflow", "superset"}

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
    #   · DAG 投递（SSH）：主机/端口/用户/目录在 settings.extra，唯独密码落在这里——
    #     extra 脱敏时原样透传（_SETTINGS_PRESERVE_KEYS），机密搁那儿会明文回显，且
    #     「留空=保持不变」失效；落进本 schema 才有 _mask_connection/_merge_connection 兜着。
    "airflow": [
        ("endpoint", "str", False, True, "http://localhost:8081"),
        ("username", "str", False, False, None),
        ("password", "str", True, False, None),
        ("ssh_password", "str", True, False, None),
    ],
    # Superset：只连不部署，ontoMeta 只往里推数据集/图表/看板，不建 database。
    #   · database_id 指 Superset 里那条指向数仓（Doris）的 database 连接。**不推导**：
    #     由 Superset 侧建好并自测通过，ontoMeta 只认这个 id——否则就得把数仓凭据
    #     再复制一份推给 Superset，那是另一个人管的另一套密钥。
    #   · public_base_url 是用户浏览器可达的地址。后端从哪儿访问 Superset 与用户从哪儿
    #     访问它是两回事（内网地址 vs 对外域名），嵌入与跳转链接只能用后者，故单独配。
    #     留空则回落到 base_url。
    "superset": [
        ("base_url", "str", False, True, "http://localhost:8088"),
        ("username", "str", False, True, None),
        ("password", "str", True, True, None),
        ("database_id", "int", False, False, None),
        ("public_base_url", "str", False, False, None),
    ],
    # MCP 不是外部服务，是 ontoMeta 自己的一层：这里只是借本表存它的运行期开关
    # （见 INTERNAL_KEYS），配置界面在「Agent 接入」页，不进基础设施面板。
    "mcp": [
        ("mcp_http_enabled", "bool", False, False, False),
        ("mcp_http_allow_anonymous", "bool", False, False, False),
        ("mcp_default_role", "str", False, False, "reader"),
        ("mcp_rate_limit_per_minute", "int", False, False, 120),
        ("mcp_execute_sql_rate_limit_per_minute", "int", False, False, 30),
        # 代执行授权闸：开着时，MCP 的 confirm_task/execute_task 只认已被人工
        # 逐条标记为可代执行的任务。默认开——角色是长期许可，"这条现在可以自动跑"
        # 是另一个决定。
        ("mcp_require_execution_approval", "bool", False, False, True),
        # 只允许本机 stdio + 真实 Principal 使用宿主交互确认断言；远程 HTTP 永不接受。
        ("mcp_allow_stdio_interactive_approval", "bool", False, False, False),
        # Skill 安装目录（后端主机上的绝对路径）。记住上次装到哪，免得每次重填一遍
        # Agent 的 skills 目录；只是默认值，每次安装仍以请求里的目录为准。
        ("mcp_skill_install_dir", "str", False, False, ""),
        # 控制台对外地址：交互表单链接要拼成用户点得开的地址。后端听什么地址与用户从哪
        # 访问它是两回事，故只取配置、不推导（见 use-saved-connection-info 那条法则）。
        ("mcp_console_base_url", "str", False, False, ""),
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


# 连接状态：**只由拨测写入**。unknown = 还没测过——保存连接不等于连接可用，
# 那种"填完地址就显示已连接"的假绿灯正是这套状态要避免的。
CONN_STATUSES = ["unknown", "connected", "failed"]


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
    """连接信息回显：secret 字段下发明文原值（供前端 Input.Password 预填+眼睛切换显隐），
    同时保留 *_set/*_hint 向前兼容；非 secret 原样。"""
    out: dict[str, Any] = {}
    for name, _typ, secret, _req, _default in CONNECTION_SCHEMAS.get(key, []):
        val = conn.get(name)
        if secret:
            out[name] = val  # 明文回显：前端预填进 Input.Password，眼睛图标控制显隐
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

    ``require=False``：跳过必填校验——``ensure_components`` 补出来的空行还没人填过，
    此刻不该以「必填」拦下（拦下就没有那一行可填了）。
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
        if val is not None and typ == "bool" and not isinstance(val, bool):
            if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes", "on"}:
                val = True
            elif isinstance(val, str) and val.strip().lower() in {"false", "0", "no", "off"}:
                val = False
            else:
                raise ValueError(f"连接字段 {name} 须为布尔值")
        cleaned[name] = val
    return cleaned


# settings_json 里认得的键。其余键为安全起见不外泄（历史上部署流程往里写过东西）。
#   · extra：组件附加参数（现只有 Airflow 编排/Flink 参数）
#   · _probe：逐条连接的拨测记账
_SETTINGS_PRESERVE_KEYS = {"extra", "_probe"}


def _mask_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """组件附加配置回显：只带出认得的键。

    这里没有机密——Airflow 的 SSH 密码在 CONNECTION_SCHEMAS 里（extra 不脱敏，
    机密搁那儿会明文回显且「留空=保持不变」失效）。
    """
    return {k: v for k, v in settings.items() if k in _SETTINGS_PRESERVE_KEYS}


def _drop_probe_ledger(row: DependencyComponent) -> None:
    """把逐条拨测记账清掉：配置变了，上次的结论就不再是这份配置的结论。

    只退行状态、不清记账的话，前端那几个逐条 ✓/✗ 会拿着旧配置的结果继续显示——
    地址改对了却仍挂着红叉，是「保存即绿灯」那种假绿灯的镜像版假红灯。
    """
    settings = _loads(row.settings_json)
    if settings.pop("_probe", None) is not None:
        row.settings_json = _dumps(settings)


def _merge_settings(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """编辑态合并附加配置：incoming 里出现的顶层键覆盖，其余（如 _probe）保留。"""
    return {**current, **{k: v for k, v in incoming.items() if k in _SETTINGS_PRESERVE_KEYS}}


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
    """固定组件的连接登记：schema 自描述 + 补齐固定行 + 编辑 + 拨测分派。"""

    # ---- schema 自描述（供前端表单生成）----
    def schema(self) -> dict[str, Any]:
        def field_list(fields: list[ConnectionField]) -> list[dict[str, Any]]:
            return [
                {"name": n, "type": t, "secret": s, "required": r, "default": d}
                for n, t, s, r, d in fields
            ]

        return {
            "components": [
                {"key": k, "label": label} for k, label in COMPONENT_CATALOG.items()
            ],
            "connection_schemas": {
                k: field_list(CONNECTION_SCHEMAS[k]) for k in COMPONENT_CATALOG
            },
            # 连接分组：一个组件可能握着几条互不相干的连接，前端据此分节渲染并逐条拨测。
            "connection_groups": {
                k: [
                    {"id": gid, "label": label, "fields": list(fields)}
                    for gid, label, fields in connection_groups(k)
                ]
                for k in COMPONENT_CATALOG
            },
            "connection_statuses": CONN_STATUSES,
        }

    # ---- 查询 ----
    def ensure_components(self, db: Session) -> None:
        """把固定组件缺的行补齐（幂等）。

        组件不由用户增删，面板要能直接显示"还没配的 DataHub"——没有这一步，新库里
        表是空的，面板就只能显示"暂无组件"，而用户根本没有"新增"这个动作可做。
        """
        existing = set(db.execute(select(DependencyComponent.key)).scalars().all())
        created = False
        for key, label in COMPONENT_CATALOG.items():
            if key in existing:
                continue
            db.add(
                DependencyComponent(
                    key=key,
                    name=label,
                    connection_status="unknown",
                    connection_json=_dumps(
                        _validate_connection(key, {}, require=False)
                    ),
                    enabled=key not in DEFAULT_DISABLED_KEYS,
                    is_default=True,
                )
            )
            created = True
        if created:
            db.commit()

    def list_components(self, db: Session) -> list[DependencyComponent]:
        """列出基础设施组件：只认目录里的 key（INTERNAL_KEYS 因此天然不在内），按目录顺序。"""
        rows = db.execute(select(DependencyComponent)).scalars().all()
        order = list(COMPONENT_CATALOG)
        return sorted(
            (r for r in rows if r.key in COMPONENT_CATALOG),
            key=lambda r: (order.index(r.key), not r.is_default, r.created_at),
        )

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
            "settings": _mask_settings(_loads(row.settings_json)),
            "connection_status": row.connection_status,
            "connection_error": row.connection_error,
            "connection": _mask_connection(row.key, _loads(row.connection_json)),
            "enabled": row.enabled,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # ---- 编辑（组件固定，只能改不能增删）----
    def update_component(
        self, db: Session, component_id: str, data: dict[str, Any]
    ) -> DependencyComponent | None:
        row = db.get(DependencyComponent, component_id)
        if not row:
            return None
        if "name" in data:
            row.name = data["name"]
        # 连接参数分居两处（连接信息 + settings.extra 里的 SSH 主机/目录），
        # 任一处变动都让上次拨测记账失效。
        touched_config = "settings" in data or "connection" in data
        if "settings" in data:
            row.settings_json = _dumps(
                _merge_settings(_loads(row.settings_json), data["settings"] or {})
            )
        if "enabled" in data:
            row.enabled = data["enabled"]
        # 连接信息：按 schema 校验；secret 字段留空(None)表示保留原值
        if "connection" in data:
            conn = self._merge_connection(row, data["connection"])
            row.connection_json = _dumps(_validate_connection(row.key, conn))
            # 保存连接≠连接可用：连接信息变了就把状态退回未拨测，避免"填完地址就显示已连接"
            # 的假绿灯。真正的 connected 只由拨测成功回写（见 probe）。
            row.connection_status = "unknown"
            row.connection_error = None
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

    # ---- 投影读/写（供 SettingsService 委托，保持既有返回结构）----

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
            row.connection_status = "unknown"
        else:
            row = DependencyComponent(
                key=key,
                name=name,
                connection_status="unknown",
                connection_json=_dumps(validated),
                enabled=enabled,
                is_default=True,
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

    # -- Superset（只连不部署：ontoMeta 只往里推数据集/图表/看板）--
    def get_superset(self, db: Session) -> dict[str, Any]:
        row = self._get_singleton(db, "superset")
        if not row:
            return {}
        c = _loads(row.connection_json)
        c["enabled"] = row.enabled
        c["updated_at"] = row.updated_at
        return c

    # -- MCP 运行期配置 --
    def get_mcp(self, db: Session) -> dict[str, Any]:
        row = self._get_singleton(db, "mcp")
        if not row:
            return {}
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    def save_mcp(self, db: Session, data: dict[str, Any]) -> dict[str, Any]:
        current = self._conn(db, "mcp")
        merged = dict(current)
        for field_name, _typ, _secret, _required, _default in CONNECTION_SCHEMAS["mcp"]:
            if field_name in data:
                merged[field_name] = data[field_name]
        row = self._upsert_singleton(db, "mcp", "MCP 服务", merged)
        c = _loads(row.connection_json)
        c["updated_at"] = row.updated_at
        return c

    # -- LLM --
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
        """当前在用的 LLM。

        组件固定成一行，但历史库里可能残留多行（早先支持多实例），故仍按
        默认行优先取一条，而不是断言只有一条。
        """
        row = db.execute(
            select(DependencyComponent)
            .where(
                DependencyComponent.key == "llm",
                DependencyComponent.enabled.is_(True),
            )
            .order_by(DependencyComponent.is_default.desc(), DependencyComponent.updated_at.desc())
        ).scalars().first()
        return self._llm_view(row) if row else None

    def update_llm(self, db: Session, service_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        row = db.get(DependencyComponent, service_id)
        if not row or row.key != "llm":
            return None
        if "name" in data:
            row.name = data["name"]
        if "enabled" in data:
            row.enabled = data["enabled"]
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

    # -- 旧表迁移（幂等）：把既有 DatahubSetting/LlmServiceConfig 搬进注册表 --
    def migrate_from_legacy(self, db: Session) -> None:
        from app.models import AirflowSetting, DatahubSetting, LlmServiceConfig

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
                    key="llm", name=svc.name, connection_status="unknown",
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
                af_row.settings_json = _dumps({"extra": {
                    "dags_dir": af.dags_dir, "max_tasks_per_dag": af.max_tasks_per_dag,
                    "max_active_tasks_per_dag": af.max_active_tasks_per_dag,
                    "dag_parse_timeout": af.dag_parse_timeout,
                    "staging_swap": af.staging_swap,
                }})
                db.commit()

    # -- Airflow（连接 + 编排参数 extra，投影为单一 dict）--
    _AIRFLOW_EXTRA_FIELDS = (
        "dags_dir",
        "max_tasks_per_dag", "max_active_tasks_per_dag",
        "dag_parse_timeout", "staging_swap",
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
        "flink_checkpoint_dir", "flink_rest_endpoint",
    )

    def get_airflow(self, db: Session) -> dict[str, Any]:
        af = self._get_singleton(db, "airflow")
        af_conn = _loads(af.connection_json) if af else {}
        extra = (_loads(af.settings_json) if af else {}).get("extra", {})
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
            af_row.connection_status = "unknown"
        else:
            af_row = DependencyComponent(
                key="airflow", name="Airflow 调度",
                connection_status="unknown", is_default=True,
                connection_json=_dumps(af_conn), enabled=data.get("enabled", True),
            )
            db.add(af_row)
        # 编排参数落 extra（仅更新 data 中出现的字段）
        settings = _loads(af_row.settings_json)
        extra = dict(settings.get("extra", {}))
        for f in self._AIRFLOW_EXTRA_FIELDS:
            if f in data:
                extra[f] = data[f]
        settings["extra"] = extra
        af_row.settings_json = _dumps(settings)
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
        ``settings._probe``），行状态由各条的最新结果聚合而成。
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
        settings = _loads(row.settings_json)
        extra = dict(settings.get("extra", {}))
        ledger = dict(settings.get("_probe", {}))

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
            ledger[gid] = {**part, "at": datetime.now(UTC).isoformat()}

        settings["_probe"] = ledger
        row.settings_json = _dumps(settings)
        # 行状态看**全部**连接的最新记账：只测了一条就通过，不足以把整行判成已连接。
        all_groups = connection_groups(row.key)
        recorded = [ledger.get(g[0]) for g in all_groups]
        failed = [
            f"{g[1]}：{ledger[g[0]]['message']}"
            for g in all_groups
            if ledger.get(g[0]) and not ledger[g[0]]["ok"]
        ]
        if failed:
            row.connection_status = "failed"
            row.connection_error = "；".join(failed)[:500]
        elif all(recorded):
            row.connection_status = "connected"
            row.connection_error = None
        else:
            # 还有连接没测过：不算已连接，也别报错——如实停在「未拨测」。
            row.connection_status = "unknown"
            row.connection_error = None
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


def _probe_superset(conn: dict[str, Any], extra: dict[str, Any]) -> ProbeResult:
    """拨测 Superset：登录拿 JWT，再用它打一次真实 REST，最后确认 database_id 存在。

    三步都不能省，每一步对应一种"填了等于没填"：
    * 只看 ``/login`` 的状态码 → 反代/静态页会回 200 + HTML，假绿灯（与 DataHub 同款坑）；
    * 登录成功但带版本前缀的 API 用不了（反代吞了 Authorization 头、账号没有任何角色）；
    * ``database_id`` 填错 → 要到 Agent 真去建数据集时才炸，那时错误在另一个进程里。
      没填则只提示，不算失败：先把连接配通、回头再补 id 是合理的次序。
    """
    from app.connectors.superset import SupersetClient, SupersetError

    base = (conn.get("base_url") or "").strip()
    if not base:
        return ProbeResult(False, "缺少 base_url")
    start = time.perf_counter()
    client = SupersetClient(
        base,
        username=conn.get("username"),
        password=conn.get("password"),
        timeout=10.0,
    )
    try:
        try:
            client.ping()
        except SupersetError as exc:
            return ProbeResult(False, str(exc)[:300])
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(False, f"{type(exc).__name__}: {exc}"[:300])
        latency = int((time.perf_counter() - start) * 1000)

        database_id = conn.get("database_id")
        if database_id in (None, ""):
            return ProbeResult(
                True,
                "连接成功（未填 database_id：建数据集前需在此填上 Superset 里指向数仓的 database）",
                latency,
            )
        try:
            client.get_database(int(database_id))
        except SupersetError as exc:
            return ProbeResult(
                False, f"database_id={database_id} 读不到：{exc}"[:300], latency
            )
        except (TypeError, ValueError):
            return ProbeResult(False, f"database_id 不是整数：{database_id!r}", latency)
        return ProbeResult(True, "连接成功", latency)
    finally:
        client.close()


# (组件 key, 连接分组 id) → 探针。分组见 CONNECTION_GROUPS；单连接组件用 "default"。
_PROBES: dict[tuple[str, str], Any] = {
    ("llm", "default"): _probe_llm,
    ("datahub", "default"): _probe_datahub,
    ("airflow", "api"): _probe_airflow_api,
    ("airflow", "ssh"): _probe_airflow_ssh,
    ("superset", "default"): _probe_superset,
}
