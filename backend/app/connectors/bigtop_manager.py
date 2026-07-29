"""Apache Bigtop Manager 1.1.0 下发客户端（遗留4）。

BM 负责把大数据栈部署到主机；ontoMeta 只在**发布者显式确认**后，把「集群拓扑规格」
翻成 BM 的 command 请求并提交，随后由 BM Agent 完成真实 SSH 与安装——**ontoMeta 不自行 SSH**。

接口按 ``apache/bigtop-manager`` 的 ``release-1.1.0`` 源码核实：

- **鉴权三步握手**（controller/LoginController、service/impl/LoginServiceImpl）：
  ``GET /api/salt?username`` → ``GET /api/nonce?username`` → ``POST /api/login``；
  登录成功返回 JWT，之后所有请求带**自定义头 ``Token: <jwt>``**（非 ``Authorization: Bearer``）。
- **登录口令**为 ``PBKDF2-HMAC-SHA256(rawPassword, salt, 600000, 32B)`` 的小写 hex
  （utils/Pbkdf2Utils、ui/src/utils/pbkdf2.ts）。
- **部署走单一 command 端点** ``POST /api/command``（controller/CommandController，
  body ``CommandReq``）；``commandLevel=cluster`` 建集群、``=service`` 装服务。
- **进度轮询** ``GET /api/clusters/{clusterId}/jobs/{jobId}``（controller/JobController），
  看 ``state`` / ``progress``。

⚠ **凭据边界**：BM 的 API **不支持「凭据引用」**——建集群必须内联 SSH 明文口令/私钥，
登录也需管理口令。故这些密钥**只从运行时 ``context`` 取**（发布者在下发时提供），
**绝不进 Spec / LLM 上下文**：Spec 里仍只有 ``credential_ref``，明文在 dispatch 边界由
context 注入并直接转发给 BM，ontoMeta 不落盘、不分发密钥。

⚠ **两处未在真实 BM 实例核实**：(1) 响应体是否套 ``{code,message,data}`` 信封——此处
用 ``_unwrap`` 兼容套壳与裸值两种；(2) ``ClusterCommandReq.type`` 等少量字段的取值语义。
**首次真实下发必须在非生产集群验证。**
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

# Pbkdf2Utils.java：iterations=600000，keySize=256bit=32B，HmacSHA256，输出小写 hex。
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_KEY_BYTES = 32
# Pbkdf2Utils.generateSalt 的默认盐：salt = PBKDF2(username, DEFAULT_SALT)。
_DEFAULT_SALT = "k9@2Uw3*Y@Ccno9c"


class BigtopManagerError(RuntimeError):
    """BM 下发失败。保留操作名与原始异常，便于定位与重放。"""

    def __init__(self, operation: str, cause: Exception):
        self.operation = operation
        self.cause = cause
        super().__init__(f"BM 下发失败（{operation}）：{cause}")


def derive_login_password(raw_password: str, salt: str) -> str:
    """按 BM 的 PBKDF2 参数派生登录口令（小写 hex）。"""
    dk = hashlib.pbkdf2_hmac(
        "sha256", raw_password.encode(), salt.encode(), _PBKDF2_ITERATIONS, _PBKDF2_KEY_BYTES
    )
    return dk.hex()


def default_salt_for(username: str) -> str:
    """BM 的默认盐推导——可离线算出，省去 ``/api/salt`` 往返（但规范流程仍走该端点）。"""
    dk = hashlib.pbkdf2_hmac(
        "sha256", username.encode(), _DEFAULT_SALT.encode(), _PBKDF2_ITERATIONS, _PBKDF2_KEY_BYTES
    )
    return dk.hex()


def _unwrap(body: Any) -> Any:
    """兼容 BM 响应信封：套 ``{code,message,data}`` 时取 ``data``，裸值则原样返回。

    响应信封结构未在真实实例核实，故两种都容忍，不硬编码某一种而在错版本上炸。
    """
    if isinstance(body, dict) and "data" in body and set(body) <= {"code", "message", "data", "success"}:
        return body["data"]
    return body


def build_host_req(hostnames: list[str], ssh: dict[str, Any]) -> dict[str, Any]:
    """拓扑主机 + 运行时 SSH 凭据 → BM 的 HostReq（req/HostReq.java）。

    ``ssh`` 来自 context（发布者提供），非 Spec；只有此处才出现明文。
    """
    req: dict[str, Any] = {
        "hostnames": list(hostnames),
        "sshUser": ssh.get("sshUser", "root"),
        "sshPort": int(ssh.get("sshPort", 22)),
        # HostAuthTypeEnum：1=PASSWORD 2=KEY 3=NO_AUTH。
        "authType": int(ssh.get("authType", 1)),
        "agentDir": ssh.get("agentDir", "/opt"),
        "grpcPort": int(ssh.get("grpcPort", 8835)),
    }
    for key in ("sshPassword", "sshKeyString", "sshKeyFilename", "sshKeyPassword"):
        if ssh.get(key):
            req[key] = ssh[key]
    return req


def build_cluster_command(
    *,
    name: str,
    display_name: str,
    hostnames: list[str],
    ssh: dict[str, Any],
    cluster_type: int = 1,
    user_group: str = "hadoop",
    root_dir: str = "/opt",
    desc: str = "",
) -> dict[str, Any]:
    """建集群的 CommandReq（command=add, commandLevel=cluster）。"""
    return {
        "command": "add",
        "commandLevel": "cluster",
        "clusterCommand": {
            "name": name,
            "displayName": display_name,
            "desc": desc,
            "type": cluster_type,
            "userGroup": user_group,
            "rootDir": root_dir,
            "hosts": [build_host_req(hostnames, ssh)],
        },
    }


def build_service_command(
    cluster_id: int, services: list[str], layout: dict[str, str]
) -> dict[str, Any]:
    """装服务的 CommandReq 骨架（command=add, commandLevel=service）。

    ``componentHosts`` 由 layout 推导，但 ``configs``（ServiceConfigReq）需按部署实际值填写，
    此处不臆造——留空由发布者补全后再提交。
    """
    return {
        "command": "add",
        "commandLevel": "service",
        "clusterId": cluster_id,
        "serviceCommands": [
            {
                "serviceName": svc,
                "installed": False,
                "componentHosts": [],  # 由 layout 展开，见 note
                "configs": [],  # 需部署实际值，发布者补全
                "_layout": layout.get(svc, "all"),
            }
            for svc in services
        ],
    }


class BigtopManagerClient:
    """最小 BM 下发客户端。``client`` 可注入（测试用 httpx.MockTransport）。"""

    def __init__(self, endpoint: str, *, client: httpx.AsyncClient | None = None):
        self.base = endpoint.rstrip("/")
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._token: str | None = None

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            # BM 用自定义头 Token（非 Authorization: Bearer），见 AuthInterceptor.java。
            headers["Token"] = self._token
        return headers

    async def login(self, username: str, password: str) -> str:
        """三步握手 → JWT。password 为**原始口令**，本方法内部派生。"""
        try:
            salt_resp = await self._client.get(
                f"{self.base}/api/salt", params={"username": username}
            )
            salt_resp.raise_for_status()
            salt = str(_unwrap(salt_resp.json()))
            nonce_resp = await self._client.get(
                f"{self.base}/api/nonce", params={"username": username}
            )
            nonce_resp.raise_for_status()
            nonce = str(_unwrap(nonce_resp.json()))
            login_resp = await self._client.post(
                f"{self.base}/api/login",
                json={
                    "username": username,
                    "password": derive_login_password(password, salt),
                    "nonce": nonce,
                },
                headers={"Content-Type": "application/json"},
            )
            login_resp.raise_for_status()
            token = _unwrap(login_resp.json())["token"]
        except Exception as exc:  # noqa: BLE001 —— 统一包成 BigtopManagerError
            raise BigtopManagerError("login", exc) from exc
        self._token = str(token)
        return self._token

    async def submit_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/command → CommandVO {id, name, state}；id 即 Job id。"""
        try:
            resp = await self._client.post(
                f"{self.base}/api/command", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            return _unwrap(resp.json())
        except Exception as exc:  # noqa: BLE001
            raise BigtopManagerError("submit_command", exc) from exc

    async def get_job(self, cluster_id: int, job_id: int) -> dict[str, Any]:
        """轮询作业进度 → JobVO {state, progress, stages}。"""
        try:
            resp = await self._client.get(
                f"{self.base}/api/clusters/{cluster_id}/jobs/{job_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return _unwrap(resp.json())
        except Exception as exc:  # noqa: BLE001
            raise BigtopManagerError("get_job", exc) from exc
