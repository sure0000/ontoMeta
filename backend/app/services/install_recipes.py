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
- ``REAL``：安装路径明确、依赖单一，实现为可跑的真配方（airflow），
  平台差异由平台层收敛。
- ``BEST_EFFORT``：重组件（datahub/llm），装法随发行版/集群
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
    service_check_hint,
    shell_quote,
    start_service,
    stop_service,
)
from app.services.ssh_installer import CommandResult, SSHError, SSHSession

__all__ = ["CommandResult", "SSHError", "SSHSession", "INSTALL_RECIPES", "run_install", "run_teardown"]

REAL = "real"
BEST_EFFORT = "best_effort"

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
        f"--username {admin_user} --password {shell_quote(admin_pwd)} --firstname a --lastname b "
        f"--role Admin --email admin@example.com || true",
        timeout=120,
    )
    return {
        "endpoint": f"http://{_host(spec)}:{port}",
        "username": admin_user,
        "password": admin_pwd,
    }


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
    "datahub": Recipe(BEST_EFFORT, _best_effort_unsupported("DataHub"), _noop_teardown),
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
