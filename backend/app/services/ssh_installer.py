"""SSH 远程执行层（物理机模式 SSH 安装的底座）。

物理机模式的部署不再是「登记一台已跑起来的服务」，而是「填目标机 IP + SSH 账密/
私钥，ontoMeta 远程安装、启动、回收连接并拨测」。本模块把 paramiko 封装成一个薄薄的
会话对象：连接（密码 / 私钥两种认证）、跑命令（带超时、收 rc/stdout/stderr）、传文件
（把安装脚本推上去）。安装配方（install_recipes.py）在此之上编排每组件的安装步骤。

设计要点：
- **不缓存凭据**：SSHSession 只在一次部署的生命周期内持有连接参数，用完即关。
- **私钥优先**：auth_method=key 时用私钥（支持 passphrase）；否则用密码。
- **异常收敛**：所有底层 paramiko 异常包成 SSHError，上层只需 catch 一种。
"""

from __future__ import annotations

import io
import socket
from dataclasses import dataclass
from types import TracebackType
from typing import Any


class SSHError(RuntimeError):
    """SSH 连接 / 执行 / 传输失败的统一异常。"""


@dataclass
class CommandResult:
    """一条远程命令的执行结果。"""

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def check(self, what: str = "命令") -> "CommandResult":
        """非零退出即抛错，错误信息带上 stderr 尾部，便于回显给用户定位。"""
        if not self.ok:
            tail = (self.stderr or self.stdout or "").strip()[-400:]
            raise SSHError(f"{what}失败（rc={self.rc}）：{tail}")
        return self


class SSHSession:
    """一次 SSH 会话：连接目标机、跑命令、传文件。用作上下文管理器自动关闭。

    参数取自 deploy_spec 的通用 SSH 字段：
      ssh_host / ssh_port / ssh_user / auth_method(password|key)
      ssh_password / ssh_private_key / ssh_key_passphrase
    """

    def __init__(self, spec: dict[str, Any], log: list[str] | None = None):
        """``log``：部署日志收集器（可选）。每次执行命令/传文件都会追加一行，
        部署失败后 frontend 据此展示安装过程（见 dependency_service.deploy）。"""
        self._log = log
        self._host = (spec.get("ssh_host") or "").strip()
        if not self._host:
            raise SSHError("缺少 ssh_host（目标机 IP/域名）")
        self._port = int(spec.get("ssh_port") or 22)
        self._user = (spec.get("ssh_user") or "").strip()
        if not self._user:
            raise SSHError("缺少 ssh_user（SSH 登录账号）")
        self._auth_method = (spec.get("auth_method") or "password").strip()
        self._password = spec.get("ssh_password") or None
        self._private_key = spec.get("ssh_private_key") or None
        self._key_passphrase = spec.get("ssh_key_passphrase") or None
        self._client: Any = None

    def _log_line(self, line: str) -> None:
        if self._log is not None:
            self._log.append(line)

    def note(self, msg: str) -> None:
        """往部署日志写一条说明/告警（不执行命令）。无 log 收集器时 no-op。
        用于把「能装但有隐患」这类软提示暴露到前端安装过程，而不打断部署。"""
        self._log_line(msg)

    # ---- 连接生命周期 ----
    def connect(self, timeout: float = 15.0) -> "SSHSession":
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确报错
            raise SSHError(
                "缺少 paramiko 依赖，无法执行 SSH 安装：pip install paramiko"
            ) from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._user,
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        try:
            if self._auth_method == "key":
                if not self._private_key:
                    raise SSHError("auth_method=key 但未提供 ssh_private_key")
                kwargs["pkey"] = self._load_private_key(self._private_key, self._key_passphrase)
            else:
                if not self._password:
                    raise SSHError("auth_method=password 但未提供 ssh_password")
                kwargs["password"] = self._password
            client.connect(**kwargs)
        except SSHError:
            raise
        except socket.timeout as exc:
            raise SSHError(f"SSH 连接超时：{self._host}:{self._port}") from exc
        except Exception as exc:  # noqa: BLE001 - paramiko 抛多种异常，统一收敛
            raise SSHError(f"SSH 连接失败：{type(exc).__name__}: {exc}") from exc
        self._client = client
        self._log_line(f"ssh {self._user}@{self._host}:{self._port} 已连接")
        return self

    @staticmethod
    def _load_private_key(material: str, passphrase: str | None) -> Any:
        """按常见密钥类型逐一尝试解析 PEM 私钥文本。"""
        import paramiko

        errors: list[str] = []
        for key_cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
            paramiko.DSSKey,
        ):
            try:
                return key_cls.from_private_key(
                    io.StringIO(material), password=passphrase or None
                )
            except Exception as exc:  # noqa: BLE001 - 类型不匹配就换下一种
                errors.append(f"{key_cls.__name__}: {exc}")
        raise SSHError("私钥解析失败（尝试 Ed25519/ECDSA/RSA/DSS 均不成）：" + "; ".join(errors))

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "SSHSession":
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---- 执行 ----
    def run(self, cmd: str, timeout: float = 120.0) -> CommandResult:
        """跑一条命令，收 rc/stdout/stderr。不抛非零错误——调用方用 .check() 决定。"""
        if self._client is None:
            raise SSHError("SSH 会话未连接")
        self._log_line(f"$ {cmd}")
        try:
            _stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        except socket.timeout as exc:
            raise SSHError(f"命令执行超时（{timeout}s）：{cmd[:80]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SSHError(f"命令执行失败：{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            self._log_line(f"! rc={rc}：{(err or out or '').strip()[-300:]}")
        return CommandResult(rc=rc, stdout=out, stderr=err)

    def sudo(self, cmd: str, timeout: float = 120.0) -> CommandResult:
        """以 root 跑命令：非 root 登录时用 sudo -n（免密 sudo，失败即明确报错）。"""
        if self._user == "root":
            return self.run(cmd, timeout=timeout)
        # -n：不交互要密码，没配免密 sudo 就直接失败，避免挂起
        return self.run(f"sudo -n bash -c {_shell_quote(cmd)}", timeout=timeout)

    def put_file(self, content: str, remote_path: str) -> None:
        """把文本内容写到远端路径（用于推安装脚本 / 配置文件）。"""
        if self._client is None:
            raise SSHError("SSH 会话未连接")
        self._log_line(f"push {remote_path}")
        try:
            sftp = self._client.open_sftp()
            try:
                with sftp.open(remote_path, "w") as fh:
                    fh.write(content)
            finally:
                sftp.close()
        except Exception as exc:  # noqa: BLE001
            raise SSHError(f"写远端文件失败 {remote_path}：{type(exc).__name__}: {exc}") from exc


def _shell_quote(s: str) -> str:
    """单引号包裹 + 转义，供 sudo bash -c 安全传递复合命令。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"
