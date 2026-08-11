"""物理机 SSH 安装配方（在 ssh_installer.SSHSession 与 install_platforms 之上编排）。

每个组件一条 Recipe：
- ``install(ssh, spec) -> connection``：在目标机上装好并启动服务，回收出一个满足
  ``CONNECTION_SCHEMAS[key]`` 的 connection dict（deploy() 会据此落库并拨测）。
- ``teardown(ssh, spec)``：尽力停服/卸载（best-effort，失败只回错误串不抛）。

**平台支持**（见 install_platforms.py，OS 自动探测）：
- linux：Debian/Ubuntu 系（apt）+ systemd + 免密 sudo。
- darwin：macOS。python3/brew + 用户级 LaunchAgent，**免 sudo**。
- windows：python venv 可装；常驻服务走 sc.exe，SSH 会话默认无提权 token 时给出可照做的手动命令。

**可靠性分级（诚实标注）**——裸机安装 9 种异构服务本质是 9 套运维工程，且绝大多数
无法在本仓库环境实测。故按 tier 区分：
- ``REAL``：安装路径明确、依赖单一，实现为可跑的真配方（airflow/seatunnel），
  平台差异由平台层收敛。
- ``BEST_EFFORT``：重组件（datahub/warehouse-doris/llm），装法随发行版/集群
  差异极大，这里给出「探测前置条件 → 缺失即明确报错」的骨架，不假装一键装好。
  真实落地需按目标环境补脚本；失败信息会回显到部署错误里，可见可改。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.install_platforms import (
    Platform,
    ServiceSpec,
    detect_platform,
    ensure_python,
    home_dir,
    require_debian,
    service_check_hint,
    shell_quote,
    start_service,
    stop_service,
)
from app.services.ssh_installer import CommandResult, SSHError, SSHSession

# 既有调用点继续用 _sq / _require_debian 的名字（实现已上收到平台层）
_sq = shell_quote
_require_debian = require_debian

__all__ = ["CommandResult", "SSHError", "SSHSession", "INSTALL_RECIPES", "run_install", "run_teardown"]

REAL = "real"
BEST_EFFORT = "best_effort"

# 自动选端口的扫描范围。8098 是 runner 惯例端口优先试；8088 是 YARN RM 默认端口
# （docker/sync-runner/Dockerfile 实测必撞），扫描刻意避开。
_AUTO_PORT_CANDIDATES = [8098, *range(8100, 9000)]


@dataclass
class Recipe:
    tier: str
    install: Callable[[SSHSession, dict[str, Any]], dict[str, Any]]
    teardown: Callable[[SSHSession, dict[str, Any]], str | None]


# --------------------------------------------------------------------- 公共工具


def _host(spec: dict[str, Any]) -> str:
    return (spec.get("ssh_host") or "").strip()


def _require_sudo(ssh: SSHSession) -> None:
    """确认非 root 登录时有免密 sudo，避免安装中途挂起。仅 Linux 分支需要。"""
    if not ssh.sudo("true").ok:
        raise SSHError("需要免密 sudo（visudo 加 NOPASSWD），或用 root 登录")


def _wait_http(ssh: SSHSession, plat: Platform, url: str, attempts: int = 30, interval: float = 2.0) -> bool:
    """在目标机本地轮询 URL 直到起活。走 python urllib（绕代理）——curl 在 Windows 不保证有，
    python 由 ensure_python 保证。"""
    expr = (
        "import urllib.request,sys; "
        "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        f"sys.exit(0 if o.open('{url}', timeout=3).status < 400 else 1)"
    )
    for _ in range(attempts):
        if ssh.run(plat.py_run(expr)).ok:
            return True
        time.sleep(interval)
    return False


def _port_free(ssh: SSHSession, plat: Platform, port: int) -> bool:
    """目标机上该端口当前是否可 bind（>=1024 无需 root）。"""
    expr = f"import socket; s=socket.socket(); s.bind(('0.0.0.0', {port})); s.close()"
    return ssh.run(plat.py_run(expr)).ok


def _find_free_port(ssh: SSHSession, plat: Platform, spec: dict[str, Any]) -> int:
    """端口解析：spec 给了就用（校验空闲，占用即明确报错）；留空自动探测。通用，供各配方复用。"""
    given = spec.get("port")
    if given is not None and str(given).strip() != "":
        port = int(given)
        if not (1 <= port <= 65535):
            raise SSHError(f"端口 {port} 非法：须在 1-65535")
        if not _port_free(ssh, plat, port):
            raise SSHError(
                f"端口 {port} 已被占用：改填其他端口，或清空让系统自动选择；"
                "如上次部署未卸载，请先卸载再重装"
            )
        return port
    for port in _AUTO_PORT_CANDIDATES:
        if _port_free(ssh, plat, port):
            return port
    raise SSHError("未找到可用端口：8098 与 8100-8999 均被占用")


# --------------------------------------------------------------------- REAL 配方


def _install_airflow(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    plat = detect_platform(ssh)
    if plat.is_windows:
        raise SSHError(
            "Airflow 上游不支持 Windows（需要 POSIX 环境）。"
            "请用 external 模式登记 Linux/macOS 上的 Airflow 实例"
        )
    if plat.name == "linux":
        _require_sudo(ssh)
    ensure_python(ssh, plat)  # linux: apt 装 python3；darwin: 已有则用、缺则 brew
    port = int(spec.get("port") or 8081)
    admin_user = (spec.get("admin_username") or "admin").strip()
    admin_pwd = spec.get("admin_password") or secrets.token_urlsafe(12)

    home = home_dir(ssh, plat, spec)
    venv = plat.join(home, "airflow-venv")
    venvpy = plat.venv_python(venv)
    af_home = plat.join(home, "airflow")
    rcfile = plat.join(home, ".zshrc") if plat.is_darwin else plat.join(home, ".bashrc")

    # 独立 venv 装 airflow，避免污染系统 python；standalone 会初始化 db + 建 admin + 起服务
    ssh.run(f"{plat.py} -m venv {venv}", timeout=120).check("建 venv")
    ssh.run(
        f"{venvpy} -m pip install --upgrade pip 'apache-airflow==2.9.3'", timeout=1200
    ).check("pip 安装 airflow")

    ssh.run(f"{plat.mkdir_cmd(af_home)} && echo 'export AIRFLOW_HOME={af_home}' >> {rcfile}")
    ssh.run(
        plat.background_cmd(
            f"AIRFLOW_HOME={af_home} AIRFLOW__WEBSERVER__WEB_SERVER_PORT={port} "
            f"{venvpy} -m airflow standalone",
            plat.join(af_home, "standalone.log"),
        ),
        timeout=60,
    )
    # standalone 起 db+web 要一会儿；轮询本地端口
    if not _wait_http(ssh, plat, f"http://127.0.0.1:{port}/health", attempts=45):
        raise SSHError("Airflow 启动超时：见目标机 airflow/standalone.log")

    # 覆写 admin 密码为我们持有的值（standalone 自动生成的密码在 standalone_admin_password.txt）
    ssh.run(
        f"AIRFLOW_HOME={af_home} {venvpy} -m airflow users create "
        f"--username {admin_user} --password {_sq(admin_pwd)} --firstname a --lastname b "
        f"--role Admin --email admin@example.com || true",
        timeout=120,
    )
    return {
        "endpoint": f"http://{_host(spec)}:{port}",
        "username": admin_user,
        "password": admin_pwd,
        "token": None,
        "api_version": "v1",
    }


def _install_seatunnel(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    raise SSHError(
        "SeaTunnel 裸机安装需指定发行版本与 connector 插件集，无法在无版本约定下自动完成。"
        "请在目标机手动装好 SeaTunnel（含 REST）后用 external 模式登记，或补全本配方。"
    )


# --------------------------------------------------------------------- BEST_EFFORT


def _best_effort_unsupported(name: str) -> Callable[[SSHSession, dict[str, Any]], dict[str, Any]]:
    def _install(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
        raise SSHError(
            f"{name} 的裸机安装随发行版/集群差异极大，未提供一键自动化。"
            f"请在目标机装好后用 external 模式登记连接，或按目标环境补全本配方。"
        )

    return _install


def _noop_teardown(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    return None


def _teardown_airflow(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    ssh.run("pkill -f 'airflow standalone' || true")
    return None


# --------------------------------------------------------------------- 注册表

INSTALL_RECIPES: dict[str, Recipe] = {
    "airflow": Recipe(REAL, _install_airflow, _teardown_airflow),
    "seatunnel": Recipe(REAL, _install_seatunnel, _noop_teardown),
    "datahub": Recipe(BEST_EFFORT, _best_effort_unsupported("DataHub"), _noop_teardown),
    "warehouse": Recipe(BEST_EFFORT, _best_effort_unsupported("目标数仓（Doris/Hive/…）"), _noop_teardown),
    "llm": Recipe(BEST_EFFORT, _best_effort_unsupported("LLM 推理服务"), _noop_teardown),
}


def run_install(key: str, spec: dict[str, Any], log: list[str] | None = None) -> dict[str, Any]:
    """开 SSH 会话 → 派发到组件配方 → 回收 connection。异常统一为 SSHError/ValueError。

    ``log``：部署日志收集器（list），透传给 SSHSession 记录每条命令与结果。
    """
    recipe = INSTALL_RECIPES.get(key)
    if recipe is None:
        raise ValueError(f"组件 {key} 暂不支持 SSH 安装")
    with SSHSession(spec, log=log) as ssh:
        return recipe.install(ssh, spec)


def run_teardown(key: str, spec: dict[str, Any], log: list[str] | None = None) -> str | None:
    recipe = INSTALL_RECIPES.get(key)
    if recipe is None:
        return None
    try:
        with SSHSession(spec, log=log) as ssh:
            return recipe.teardown(ssh, spec)
    except SSHError as exc:
        return str(exc)[:300]
