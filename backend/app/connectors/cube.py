"""CubeConnector：把 Cube（开源语义层）作为外部成熟组件外挂。

设计与 DataHubConnector 一致：ontoMeta 只做「中枢」——

1. `generate_model()`：把已发布本体（对象/属性/业务逻辑）翻译成 Cube data model
   （cube=对象、dimension=属性、measure=业务逻辑口径），供运维部署到 Cube 服务。
2. `query()`：把数据集口径绑定翻译成 Cube 查询（measures/dimensions/filters/
   timeDimensions），调用 Cube Load API 执行，返回统一的 columns/rows。
3. `build_token()`：用 HS256 生成 Cube 需要的 JWT（含 securityContext 行级权限）。

无外部依赖即可运行：`use_mock_cube=true`（默认）或未配置密钥时返回确定性 Mock。
真实 Cube 环境只需设置 `CUBE_API_URL` / `CUBE_API_SECRET` 并置 `USE_MOCK_CUBE=false`。

Cube 的 Refresh Worker（预聚合定时刷新）由 Cube 侧承担，ontoMeta 不做进程内调度。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("ontometa.cube")

_AGG_MAP = {
    "sum": "sum",
    "count": "count",
    "avg": "avg",
    "max": "max",
    "min": "min",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def cube_name(object_name: str) -> str:
    """本体对象名 → Cube cube 名（大驼峰，Cube 约定）。"""
    parts = re.split(r"[^0-9a-zA-Z]+", object_name or "")
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Cube"


class CubeExecutionError(Exception):
    """CubeConnector 对外可读错误。"""


class CubeConnector:
    """Cube 语义层连接器（外挂执行/预聚合）。"""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_secret: str | None = None,
        use_mock: bool | None = None,
    ) -> None:
        self.api_url = (api_url or settings.cube_api_url or "").rstrip("/")
        self.api_secret = api_secret if api_secret is not None else settings.cube_api_secret
        self.use_mock = (
            settings.use_mock_cube if use_mock is None else use_mock
        ) or not self.api_secret

    # ------------------------------------------------------------- model gen

    def generate_model(
        self,
        *,
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """生成 Cube data model（JSON 形态，可落盘为 model/*.yml 或 *.js）。

        objects: [{name, display_name, sql_table?, properties:[{name,display_name,data_type,semantic_type}],
                   measures:[{name,display_name,agg,sql?}]}]
        """
        cubes: list[dict[str, Any]] = []
        for obj in objects:
            cname = cube_name(obj["name"])
            dimensions: dict[str, Any] = {}
            for p in obj.get("properties", []):
                dimensions[p["name"]] = {
                    "sql": p["name"],
                    "type": self._dim_type(p.get("data_type"), p.get("semantic_type")),
                    "title": p.get("display_name") or p["name"],
                }
            measures: dict[str, Any] = {
                "count": {"type": "count", "title": "记录数"},
            }
            # 为数值属性自动生成聚合度量（与 build_query 的 <name>_<agg> 命名对齐）
            for p in obj.get("properties", []):
                if self._dim_type(p.get("data_type"), p.get("semantic_type")) == "number":
                    for agg in ("sum", "avg", "max", "min"):
                        measures[f"{p['name']}_{agg}"] = {
                            "type": agg,
                            "sql": p["name"],
                            "title": f"{p.get('display_name') or p['name']} {agg.upper()}",
                        }
            for m in obj.get("measures", []):
                measures[m["name"]] = {
                    "type": _AGG_MAP.get((m.get("agg") or "sum").lower(), "sum"),
                    "sql": m.get("sql") or m["name"],
                    "title": m.get("display_name") or m["name"],
                }
            cubes.append(
                {
                    "name": cname,
                    "sql_table": obj.get("sql_table") or obj["name"],
                    "title": obj.get("display_name") or obj["name"],
                    "dimensions": dimensions,
                    "measures": measures,
                }
            )
        return {"cubes": cubes}

    @staticmethod
    def _dim_type(data_type: str | None, semantic_type: str | None) -> str:
        dt = (data_type or "").lower()
        st = (semantic_type or "").lower()
        if st in {"date", "datetime", "time"} or "date" in dt or "time" in dt:
            return "time"
        if st in {"amount", "number", "count"} or dt in {"int", "integer", "bigint", "decimal", "float", "double", "number"}:
            return "number"
        if dt in {"bool", "boolean"}:
            return "boolean"
        return "string"

    # --------------------------------------------------------- binding→query

    def build_query(
        self,
        *,
        object_name: str,
        measures: list[dict[str, Any]],
        dimensions: list[dict[str, Any]],
        filters: list[dict[str, Any]] | None = None,
        time_range: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """把数据集绑定翻成 Cube 查询 JSON（成员名形如 Cube.member）。"""
        cname = cube_name(object_name)
        q: dict[str, Any] = {"limit": limit}
        q_measures: list[str] = []
        for m in measures:
            ref = m.get("ref") or {}
            member = ref.get("name")
            if not member:
                continue
            if ref.get("kind") == "business_logic":
                q_measures.append(f"{cname}.{member}")
            else:
                agg = _AGG_MAP.get((m.get("agg") or "sum").lower(), "sum")
                q_measures.append(f"{cname}.{member}_{agg}")
        if not q_measures:
            q_measures = [f"{cname}.count"]
        q["measures"] = q_measures

        q_dims: list[str] = []
        for d in dimensions:
            if d.get("name"):
                q_dims.append(f"{cname}.{d['name']}")
        if q_dims:
            q["dimensions"] = q_dims

        q_filters: list[dict[str, Any]] = []
        _op_map = {"eq": "equals", "ne": "notEquals", "gt": "gt", "lt": "lt", "ge": "gte", "le": "lte", "like": "contains"}
        for f in filters or []:
            ref = f.get("ref") or {}
            if not ref.get("name"):
                continue
            q_filters.append(
                {
                    "member": f"{cname}.{ref['name']}",
                    "operator": _op_map.get(str(f.get("op") or "eq").lower(), "equals"),
                    "values": [str(f.get("value"))],
                }
            )
        if q_filters:
            q["filters"] = q_filters

        if time_range and (time_range.get("ref") or {}).get("name"):
            tname = time_range["ref"]["name"]
            date_range = self._window_to_range(time_range.get("window"))
            td: dict[str, Any] = {"dimension": f"{cname}.{tname}"}
            if date_range:
                td["dateRange"] = date_range
            q["timeDimensions"] = [td]
        return q

    @staticmethod
    def _window_to_range(window: str | None) -> str | None:
        return {
            "last_7d": "last 7 days",
            "last_30d": "last 30 days",
            "last_90d": "last 90 days",
            "today": "today",
            "this_month": "this month",
        }.get(window or "")

    # ------------------------------------------------------------------ exec

    def query(self, cube_query: dict[str, Any], *, security_context: dict | None = None) -> tuple[list[dict], list[dict]]:
        """执行 Cube 查询，返回 (columns, rows)。Mock 模式下返回确定性示例数据。"""
        if self.use_mock:
            return self._mock_query(cube_query)

        token = self.build_token(security_context or {})
        url = f"{self.api_url}/cubejs-api/v1/load"
        try:
            with httpx.Client(trust_env=False, timeout=settings.cube_timeout_seconds) as client:
                resp = client.post(
                    url,
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    json={"query": cube_query},
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cube query failed: %s", exc)
            raise CubeExecutionError(f"Cube 查询失败：{exc}") from exc

        data = payload.get("data") or []
        annotation = payload.get("annotation") or {}
        members = {**annotation.get("measures", {}), **annotation.get("dimensions", {}), **annotation.get("timeDimensions", {})}
        # 列顺序：dimensions 优先，measures 其后
        keys: list[str] = list((cube_query.get("dimensions") or [])) + list(cube_query.get("measures") or [])
        columns = [
            {"key": k, "title": (members.get(k) or {}).get("shortTitle") or k.split(".")[-1]}
            for k in keys
        ]
        rows = [{k: row.get(k) for k in keys} for row in data]
        return columns, rows

    def build_token(self, security_context: dict | None = None) -> str:
        """生成 Cube 需要的 HS256 JWT（无 PyJWT 依赖，手工签名）。"""
        if not self.api_secret:
            raise CubeExecutionError("未配置 CUBE_API_SECRET，无法生成访问令牌")
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        if security_context:
            payload["securityContext"] = security_context
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segments).encode("ascii")
        signature = hmac.new(self.api_secret.encode(), signing_input, hashlib.sha256).digest()
        segments.append(_b64url(signature))
        return ".".join(segments)

    def _mock_query(self, cube_query: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        dims = cube_query.get("dimensions") or []
        measures = cube_query.get("measures") or []
        columns = [{"key": k, "title": k.split(".")[-1]} for k in [*dims, *measures]]
        if not columns:
            columns = [{"key": "value", "title": "value"}]
        n = 6
        rows: list[dict[str, Any]] = []
        for i in range(n):
            row: dict[str, Any] = {}
            for k in dims:
                row[k] = f"{k.split('.')[-1]}-{chr(65 + (i % 6))}"
            for k in measures:
                seed = int(hashlib.md5(f"{k}:{i}".encode()).hexdigest(), 16)
                row[k] = seed % 10000
            if not dims and not measures:
                row["value"] = i
            rows.append(row)
        return columns, rows
