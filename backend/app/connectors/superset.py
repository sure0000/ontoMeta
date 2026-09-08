"""Superset REST 客户端：登记数据集、建图表/看板、签发嵌入用 guest token。

**ontoMeta 不做 BI 呈现**：图表怎么画、怎么存、怎么分享全归 Superset，本模块只把
ontoMeta 已经治理好的东西（落点物理表 + 本体语义 + 编译好的图表规格）推过去，
再把 id 与 url 拿回来登记。

几个跟别的连接器不一样、但都是被 Superset 逼出来的地方：

* **鉴权是两段**。``POST /api/v1/security/login`` 拿 JWT 之后，写接口还要 CSRF
  令牌 + 同一条会话的 cookie。因此客户端持一个带 cookie jar 的 ``httpx.Client``，
  并在首次写之前取一次 ``/api/v1/security/csrf_token/``。只带 Bearer 去 POST 会
  得到一个说不清原因的 400。
* **``Referer`` 要带**。Flask-WTF 的 CSRF 校验在 https 下会核对 Referer，缺了就 400。
* **列表过滤用 rison**，不是普通 query string。这里只拼最小几种（等值/包含），
  没有引入 rison 库——多一个依赖换三行字符串不划算。

凭据只从设置页读（见 ``SettingsService.get_superset_runtime``），不入任何产物。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ontometa.connectors.superset")


class SupersetError(RuntimeError):
    """Superset 侧的可读错误。带上操作名，便于回执里说清是哪一步失败。"""

    def __init__(self, operation: str, cause: Exception | str):
        self.operation = operation
        super().__init__(f"Superset {operation} 失败：{cause}")


def _rison_str(value: str) -> str:
    """rison 字符串字面量：单引号包裹，内部单引号用 ``!'`` 转义。"""
    return "'" + str(value).replace("!", "!!").replace("'", "!'") + "'"


def rison_filters(
    filters: list[tuple[str, str, Any]],
    *,
    page_size: int = 100,
    columns: list[str] | None = None,
) -> str:
    """把 ``[(列, 运算符, 值)]`` 拼成 Superset 列表接口要的 ``q=`` rison。

    运算符用 Superset 的写法：``eq`` 等值、``ct`` 包含。值是数字就不加引号。
    """
    parts = []
    for col, opr, value in filters:
        rendered = str(value) if isinstance(value, int | float) and not isinstance(value, bool) else _rison_str(str(value))
        parts.append(f"(col:{col},opr:{opr},value:{rendered})")
    chunks = [f"filters:!({','.join(parts)})"] if parts else []
    if columns:
        chunks.append(f"columns:!({','.join(columns)})")
    chunks.append(f"page_size:{page_size}")
    return f"({','.join(chunks)})"


class SupersetClient:
    """最小 Superset 客户端。``client`` 可注入（测试用 ``httpx.MockTransport``）。"""

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._username = username
        self._password = password
        # trust_env=False：与其它连接器一致，绝不走开发机的 HTTP(S)_PROXY / ALL_PROXY
        # （ALL_PROXY=socks5://… 时 httpx 会因缺 socksio 直接抛 ImportError）。
        # cookie jar 必须留着：CSRF 令牌与会话 cookie 是一对，分开就校验不过。
        self._client = client or httpx.Client(trust_env=False, timeout=timeout)
        self._token: str | None = None
        self._csrf: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupersetClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 鉴权 ----------

    def login(self) -> str:
        """拿 JWT。已登录则直接复用——一次调用链里不该反复登录。"""
        if self._token:
            return self._token
        if not self.base_url:
            raise SupersetError("login", "缺少 base_url")
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v1/security/login",
                json={
                    "username": self._username,
                    "password": self._password,
                    "provider": "db",
                    "refresh": True,
                },
            )
        except httpx.HTTPError as exc:
            raise SupersetError("login", exc) from exc
        if resp.status_code in (401, 403):
            raise SupersetError("login", f"鉴权失败 HTTP {resp.status_code}：请检查账号密码")
        if resp.status_code >= 400:
            raise SupersetError("login", f"HTTP {resp.status_code} {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            # 地址填成了 Superset 前面的反代/静态页时最典型的形态：200 + HTML。
            raise SupersetError(
                "login", "响应不是 JSON（base_url 可能指向反向代理或静态页，而非 Superset）"
            ) from exc
        token = payload.get("access_token")
        if not token:
            raise SupersetError("login", "响应缺少 access_token（该端点可能不是 Superset）")
        self._token = str(token)
        return self._token

    def _csrf_token(self) -> str | None:
        """取 CSRF 令牌。部分部署关掉了 WTF_CSRF，取不到就算了，不该因此拦住写操作。"""
        if self._csrf:
            return self._csrf
        try:
            resp = self._client.get(
                f"{self.base_url}/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {self.login()}"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            self._csrf = resp.json().get("result")
        except ValueError:
            return None
        return self._csrf

    def _headers(self, *, write: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.login()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if write:
            csrf = self._csrf_token()
            if csrf:
                headers["X-CSRFToken"] = csrf
                # Flask-WTF 在 https 下核对 Referer；缺了会得到一个没信息的 400。
                headers["Referer"] = self.base_url
        return headers

    # ---------- 请求 ----------

    def _request(self, method: str, path: str, operation: str, **kwargs: Any) -> dict:
        write = method.upper() not in ("GET", "HEAD")
        url = f"{self.base_url}{path}"
        try:
            resp = self._client.request(method, url, headers=self._headers(write=write), **kwargs)
        except httpx.HTTPError as exc:
            raise SupersetError(operation, exc) from exc
        # JWT 过期：重登一次再试。长会话里这是常态，不该让调用方自己处理。
        if resp.status_code == 401 and self._token:
            self._token = None
            self._csrf = None
            try:
                resp = self._client.request(
                    method, url, headers=self._headers(write=write), **kwargs
                )
            except httpx.HTTPError as exc:
                raise SupersetError(operation, exc) from exc
        if resp.status_code >= 400:
            # 原样带出 Superset 的错误体：它的 message 通常已经说清了哪个字段不对。
            raise SupersetError(operation, f"HTTP {resp.status_code} {resp.text[:300]}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise SupersetError(operation, f"响应不是 JSON：{resp.text[:200]}") from exc

    # ---------- 探活 ----------

    def ping(self) -> dict:
        """两步探活：先登录（鉴权），再打一个真实的 REST GET（路径 + 权限）。

        只看登录会给假绿灯——登录成功但 token 在带版本前缀的 API 上用不了（反代吞了
        Authorization 头、账号没有任何角色）都属于「配了等于没配」，要在这里暴露。
        """
        self.login()
        return self._request("GET", "/api/v1/me/", "ping")

    # ---------- 数据库连接（只读；ontoMeta 不建 database）----------

    def get_database(self, database_id: int) -> dict:
        """读一条 database。用于校验设置页填的 ``database_id`` 真的存在。"""
        return self._request("GET", f"/api/v1/database/{int(database_id)}", "get_database")

    # ---------- 数据集 ----------

    def find_dataset(self, database_id: int, schema: str | None, table_name: str) -> dict | None:
        """按 库 + schema + 表名 找已存在的数据集；找不到回 ``None``。

        建数据集要幂等，靠的就是先查这一步——Superset 允许同名重复建，
        重复建出来的数据集会让"这张图到底连的哪个"变成一件说不清的事。
        """
        filters: list[tuple[str, str, Any]] = [
            ("table_name", "eq", table_name),
            ("database", "rel_o_m", int(database_id)),
        ]
        if schema:
            filters.append(("schema", "eq", schema))
        payload = self._request(
            "GET",
            "/api/v1/dataset/",
            "find_dataset",
            params={"q": rison_filters(filters, page_size=100)},
        )
        for item in payload.get("result", []) or []:
            if str(item.get("table_name")) == str(table_name):
                return item
        return None

    def create_dataset(self, database_id: int, schema: str | None, table_name: str) -> dict:
        body: dict[str, Any] = {"database": int(database_id), "table_name": table_name}
        if schema:
            body["schema"] = schema
        return self._request("POST", "/api/v1/dataset/", "create_dataset", json=body)

    def get_dataset(self, dataset_id: int) -> dict:
        return self._request("GET", f"/api/v1/dataset/{int(dataset_id)}", "get_dataset")

    def update_dataset(self, dataset_id: int, body: dict[str, Any]) -> dict:
        return self._request(
            "PUT", f"/api/v1/dataset/{int(dataset_id)}", "update_dataset", json=body
        )

    def refresh_dataset(self, dataset_id: int) -> dict:
        """让 Superset 重新从库里同步列。新建的数据集不刷就可能没有列。"""
        return self._request(
            "PUT", f"/api/v1/dataset/{int(dataset_id)}/refresh", "refresh_dataset"
        )

    def list_datasets(self, keyword: str | None = None, *, page_size: int = 100) -> list[dict]:
        filters: list[tuple[str, str, Any]] = []
        if keyword:
            filters.append(("table_name", "ct", keyword))
        payload = self._request(
            "GET",
            "/api/v1/dataset/",
            "list_datasets",
            params={"q": rison_filters(filters, page_size=page_size)},
        )
        return list(payload.get("result", []) or [])

    # ---------- 图表 ----------

    def create_chart(self, body: dict[str, Any]) -> dict:
        return self._request("POST", "/api/v1/chart/", "create_chart", json=body)

    def update_chart(self, chart_id: int, body: dict[str, Any]) -> dict:
        return self._request("PUT", f"/api/v1/chart/{int(chart_id)}", "update_chart", json=body)

    def get_chart(self, chart_id: int) -> dict:
        return self._request("GET", f"/api/v1/chart/{int(chart_id)}", "get_chart")

    def list_charts(self, *, page_size: int = 100) -> list[dict]:
        payload = self._request(
            "GET",
            "/api/v1/chart/",
            "list_charts",
            params={"q": rison_filters([], page_size=page_size)},
        )
        return list(payload.get("result", []) or [])

    # ---------- 看板 ----------

    def create_dashboard(self, body: dict[str, Any]) -> dict:
        return self._request("POST", "/api/v1/dashboard/", "create_dashboard", json=body)

    def update_dashboard(self, dashboard_id: int, body: dict[str, Any]) -> dict:
        return self._request(
            "PUT", f"/api/v1/dashboard/{int(dashboard_id)}", "update_dashboard", json=body
        )

    def get_dashboard(self, dashboard_id: int) -> dict:
        return self._request("GET", f"/api/v1/dashboard/{int(dashboard_id)}", "get_dashboard")

    def list_dashboards(self, *, page_size: int = 100) -> list[dict]:
        payload = self._request(
            "GET",
            "/api/v1/dashboard/",
            "list_dashboards",
            params={"q": rison_filters([], page_size=page_size)},
        )
        return list(payload.get("result", []) or [])

    def enable_embedded(self, dashboard_id: int, allowed_domains: list[str]) -> str:
        """开启嵌入并拿到 embedded uuid（``embedDashboard`` 的 ``id`` 就是它）。

        需要 Superset 侧打开 ``EMBEDDED_SUPERSET`` 特性开关，否则这里 404。
        ``allowed_domains`` 留空表示不限来源——生产上应当明确填 ontoMeta 前端的源。
        """
        payload = self._request(
            "POST",
            f"/api/v1/dashboard/{int(dashboard_id)}/embedded",
            "enable_embedded",
            json={"allowed_domains": allowed_domains},
        )
        uuid = (payload.get("result") or {}).get("uuid")
        if not uuid:
            raise SupersetError("enable_embedded", "响应缺少 uuid")
        return str(uuid)

    def guest_token(self, resources: list[dict[str, Any]], user: dict[str, Any]) -> str:
        payload = self._request(
            "POST",
            "/api/v1/security/guest_token/",
            "guest_token",
            json={"user": user, "resources": resources, "rls": []},
        )
        token = payload.get("token")
        if not token:
            raise SupersetError("guest_token", "响应缺少 token")
        return str(token)
