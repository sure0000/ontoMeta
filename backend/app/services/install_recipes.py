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
- ``REAL``：安装路径明确、依赖单一，实现为可跑的真配方（postgres/airflow/cube/
  seatunnel/sync_runner），平台差异由平台层收敛。
- ``BEST_EFFORT``：重组件（datahub/warehouse-doris/llm），装法随发行版/集群
  差异极大，这里给出「探测前置条件 → 缺失即明确报错」的骨架，不假装一键装好。
  真实落地需按目标环境补脚本；失败信息会回显到部署错误里，可见可改。
"""

from __future__ import annotations

import base64
import io
import secrets
import tarfile
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

# sync-runner 源码位置（仓库根）。
_REPO_ROOT = Path(__file__).resolve().parents[3]  # 同 dependency_service.py：backend/app/services → 仓库根
_SYNC_RUNNER_SRC = _REPO_ROOT / "sync_runner"
# 自动选端口的扫描范围。8098 是 runner 惯例端口优先试；8088 是 YARN RM 默认端口
# （docker/sync-runner/Dockerfile 实测必撞），扫描刻意避开。
_AUTO_PORT_CANDIDATES = [8098, *range(8100, 9000)]

# 跨平台解包脚本：base64 解码 + tar 提取，避免依赖 base64 -d / tar 的 CLI 差异。
# 由 put_file 推到目标机后以 ``{py} unpack.py <b64> <目标目录>`` 运行。
_UNPACK_PY = """\
import base64, io, sys, tarfile
with open(sys.argv[1], 'rb') as f:
    raw = base64.b64decode(f.read())
with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
    tf.extractall(sys.argv[2])
"""


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


def _pack_sync_runner_b64() -> str:
    """把仓库 sync_runner/ 打成 tar.gz 并 base64（内存态，单行文本，供 put_file 推送）。"""
    if not _SYNC_RUNNER_SRC.is_dir():
        raise SSHError(f"仓库内缺少 sync_runner 源码包：{_SYNC_RUNNER_SRC}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in sorted(_SYNC_RUNNER_SRC.rglob("*")):
            if "__pycache__" in f.parts or f.suffix == ".pyc" or not f.is_file():
                continue
            tf.add(f, arcname=f"sync_runner/{f.relative_to(_SYNC_RUNNER_SRC)}")
    return base64.b64encode(buf.getvalue()).decode()


def _psql_prefix(plat: Platform, spec: dict[str, Any]) -> str:
    """psql 连接前缀：Linux 用 sudo -u postgres 走本地 socket；macOS 的 brew 实例
    默认超管是登录用户，直接连 127.0.0.1 免 sudo。"""
    if plat.is_darwin:
        login = (spec.get("ssh_user") or "root").strip()
        port = int(spec.get("port") or 5432)
        return f"psql -h 127.0.0.1 -p {port} -U {login} -d postgres"
    return "sudo -u postgres psql"


def _psql_run(
    ssh: SSHSession,
    plat: Platform,
    spec: dict[str, Any],
    sql: str,
    *,
    params: dict[str, str] | None = None,
    tuples_only: bool = False,
    timeout: float = 120.0,
):
    """以超管身份跑一段 SQL（支持 \\gexec 等 meta-command）。

    SQL 正文写到远端临时文件再 ``-f`` 载入，正文完全不经 shell，杜绝嵌套引号问题；
    参数经 ``psql -v`` 传入并各自 shell 转义，SQL 内用 ``:'name'`` 引用（配合 format
    的 %I/%L）——既防注入，也不必在 Python 里手拼引号。返回 CommandResult。
    """
    params = params or {}
    remote = f"/tmp/ontometa_{secrets.token_hex(8)}.sql"
    ssh.put_file(sql + "\n", remote)
    try:
        var_args = " ".join(f"-v {k}={_sq(str(v))}" for k, v in params.items())
        flags = "-tA " if tuples_only else ""
        cmd = f"{_psql_prefix(plat, spec)} {flags}{var_args} -f {remote}"
        # darwin 以登录用户直连（免 sudo），linux 经 sudo -u postgres
        return ssh.sudo(cmd, timeout=timeout) if not plat.is_darwin else ssh.run(cmd, timeout=timeout)
    finally:
        ssh.run(f"rm -f {remote}")


def _psql_exec(
    ssh: SSHSession,
    plat: Platform,
    spec: dict[str, Any],
    sql: str,
    *,
    params: dict[str, str] | None = None,
    what: str = "执行 SQL",
    timeout: float = 120.0,
) -> None:
    """跑 SQL 并要求成功（非零退出即抛 SSHError，带 stderr 尾部）。"""
    _psql_run(ssh, plat, spec, sql, params=params, timeout=timeout).check(what)


# --------------------------------------------------------------------- REAL 配方


def _install_postgres(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    plat = detect_platform(ssh)
    if plat.is_windows:
        raise SSHError(
            "Windows 上 PostgreSQL 的自动安装需 EDB 安装器/winget 且服务注册要提权，"
            "本版不自动装：请手动装好 Postgres 后用 external 模式登记"
        )
    if plat.name == "linux":
        _require_debian(ssh)
        _require_sudo(ssh)
    port = int(spec.get("port") or 5432)
    user = (spec.get("db_username") or "ontometa").strip()
    database = (spec.get("database") or "ontometa").strip()
    pwd = spec.get("db_password") or secrets.token_urlsafe(16)

    if plat.is_darwin:
        # brew postgres：默认超管就是安装用户，服务由 brew services 托管（免 sudo）
        if not ssh.run("command -v psql").ok:
            if not ssh.run("command -v brew").ok:
                raise SSHError("macOS 目标机缺 psql 且没有 Homebrew：先装 brew 再重试")
            ssh.run("brew install postgresql@16", timeout=1200).check("brew 安装 postgresql")
            ssh.run("brew services start postgresql@16", timeout=120).check("启动 postgresql")
    else:
        ssh.sudo("DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout=300).check("apt update")
        ssh.sudo(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql", timeout=600
        ).check("安装 postgresql")
        ssh.sudo("systemctl enable --now postgresql", timeout=120).check("启动 postgresql")

    # 建角色 + 库（幂等：不存在才建，避免重复部署报错）。
    # 用 psql -v 传参而非字符串插值，既避开嵌套引号地狱，也防 SQL 注入（库名/口令含特殊字符）。
    role_sql = (
        "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'r', :'p') "
        "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'r')\\gexec"
    )
    _psql_exec(ssh, plat, spec, role_sql, params={"r": user, "p": pwd}, what="建角色")
    # 建库：createdb 幂等性差，先查后建
    exists = _psql_run(
        ssh,
        plat,
        spec,
        "SELECT 1 FROM pg_database WHERE datname = :'d'",
        params={"d": database},
        tuples_only=True,
    )
    if "1" not in exists.stdout:
        create_db_sql = "SELECT format('CREATE DATABASE %I OWNER %I', :'d', :'o')\\gexec"
        _psql_exec(ssh, plat, spec, create_db_sql, params={"d": database, "o": user}, what="建库")

    h = _host(spec)
    dsn = f"postgresql+psycopg://{user}:{pwd}@{h}:{port}/{database}"
    return {"sqlalchemy_url": dsn}


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


def _install_cube(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    plat = detect_platform(ssh)
    if plat.is_windows:
        raise SSHError(
            "Windows 上 Cube 的常驻服务需额外配置（sc.exe 提权 / NSSM），本版不自动注册："
            "请手动起 npx cubejs-server 后用 external 模式登记"
        )
    if plat.name == "linux":
        _require_debian(ssh)
        _require_sudo(ssh)
    port = int(spec.get("port") or 4000)
    api_secret = secrets.token_urlsafe(24)
    home = home_dir(ssh, plat, spec)
    cube_dir = plat.join(home, "cube")
    # 非交互 SSH 的 PATH 常缺 /usr/local/bin(brew)、nvm/brew 装的 node 等，
    # 直接 command -v node 会误判缺失。node/npm/npx/brew 一律走 login shell 拿用户 PATH。
    def _login(cmd: str) -> str:
        return f"bash -lc {shell_quote(cmd)}"

    # Node + cubejs-server
    if not ssh.run(_login("command -v node")).ok:
        if plat.is_darwin:
            if not ssh.run(_login("command -v brew")).ok:
                raise SSHError("macOS 目标机缺 node 且没有 Homebrew：先装 brew 或手动装 Node.js")
            ssh.run(_login("brew install node"), timeout=1200).check("brew 安装 node")
        else:
            ssh.sudo(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm", timeout=600
            ).check("安装 nodejs")
    if plat.name == "linux":
        ssh.sudo("npm install -g cubejs-server", timeout=600).check("安装 cubejs-server")
        prefix = ssh.sudo("npm prefix -g").check("解析 npm global prefix").stdout.strip()
    else:
        # darwin：brew 装的 node 归登录用户所有，全局 npm 无需 sudo
        ssh.run(_login("npm install -g cubejs-server"), timeout=600).check("安装 cubejs-server")
        prefix = ssh.run(_login("npm prefix -g")).check("解析 npm global prefix").stdout.strip()
    # npm 的 global prefix 不一定在 login PATH 里（本机实测 ~/.local/bin/npm 的 prefix
    # 指到 ~/.hermes/node/bin，而该目录不在 PATH）→ 用 npm prefix -g 拿到绝对路径，直接跑
    # 那个 bin，不靠 PATH 查 cubejs-server。bin 的 shebang 是 env node，仍需 login shell 里的 node。
    cube_bin = plat.join(prefix, "bin", "cubejs-server")
    if not ssh.run(f"test -x {shell_quote(cube_bin)}").ok:
        raise SSHError(f"cubejs-server 未装到预期位置：{cube_bin}（npm prefix -g={prefix}）")
    ssh.run(plat.mkdir_cmd(cube_dir))
    ssh.put_file(
        f"CUBEJS_API_SECRET={api_secret}\nCUBEJS_DEV_MODE=true\nPORT={port}\n",
        plat.sftp_path(plat.join(cube_dir, ".env")),
    )
    ssh.run(
        plat.background_cmd(_login(f"cd {cube_dir} && {shell_quote(cube_bin)}"), plat.join(cube_dir, "cube.log")),
        timeout=60,
    )
    # Cube Store 初始化要几秒，给 60×2=120s 余量。
    # 用 / 而非 /livez：后者把 schema/datasource 就绪也算进 health，部署阶段还没配 schema
    # 会一直 500，导致拨测失败；/ 只要 HTTP 服务在听就 200，符合「部署起没起来」语义。
    if not _wait_http(ssh, plat, f"http://127.0.0.1:{port}/", attempts=60):
        raise SSHError("Cube 启动超时：见目标机 cube/cube.log")
    return {
        "api_url": f"http://{_host(spec)}:{port}",
        "api_secret": api_secret,
        "preagg_refresh": "1 hour",
        "tenant_dimension": None,
        "timeout_seconds": 30,
    }


def _install_seatunnel(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    raise SSHError(
        "SeaTunnel 裸机安装需指定发行版本与 connector 插件集，无法在无版本约定下自动完成。"
        "请在目标机手动装好 SeaTunnel（含 REST）后用 external 模式登记，或补全本配方。"
    )


def _install_sync_runner(ssh: SSHSession, spec: dict[str, Any]) -> dict[str, Any]:
    """SSH 一键装 sync-runner：推源码包 → venv 装依赖 → 平台常驻服务 → 拨测回收。

    端口语义：spec 显式给了就用（占用即明确报错）；留空按 ``_AUTO_PORT_CANDIDATES``
    在目标机探测空闲端口，并把解析结果写回 ``spec["port"]``（deploy() 落库，重装/卸载一致）。
    服务按平台落地：linux systemd / darwin LaunchAgent / windows sc.exe。
    """
    plat = detect_platform(ssh)
    ensure_python(ssh, plat)
    home = home_dir(ssh, plat, spec)
    code_dir = plat.join(home, "sync-runner")
    venv = plat.join(home, "sync-runner-venv")
    venvpy = plat.venv_python(venv)
    secrets_dir = plat.join(code_dir, "secrets")

    # 端口探测要 python，必须在 ensure_python 之后
    port = _find_free_port(ssh, plat, spec)
    spec["port"] = port  # 写回，deploy() 落库供重装/卸载保持一致

    ssh.run(plat.mkdir_cmd(code_dir)).check("建 sync-runner 目录")
    ssh.put_file(_pack_sync_runner_b64() + "\n", plat.sftp_path(plat.join(code_dir, "sync_runner.b64")))
    ssh.put_file(_UNPACK_PY, plat.sftp_path(plat.join(code_dir, "unpack.py")))
    ssh.run(
        f"{plat.py} {plat.join(code_dir, 'unpack.py')} {plat.join(code_dir, 'sync_runner.b64')} {code_dir}"
    ).check("解包 sync_runner")
    ssh.run(plat.rm_cmd(plat.join(code_dir, "sync_runner.b64")))

    ssh.run(f"{plat.py} -m venv {venv}", timeout=120).check("建 venv")
    ssh.run(f"{venvpy} -m pip install --upgrade pip", timeout=300).check("升级 pip")
    ssh.run(
        f"{venvpy} -m pip install -r {plat.join(code_dir, 'sync_runner', 'requirements.txt')}", timeout=600
    ).check("pip 安装 sync-runner 依赖")

    ssh.run(plat.mkdir_cmd(secrets_dir))  # SYNC_SECRETS_DIR，服务用户可写
    token = secrets.token_urlsafe(24)
    start_service(
        ssh, plat, spec,
        ServiceSpec(
            unit_name="ontometa-sync-runner",
            label="com.ontometa.sync-runner",
            python_exe=venvpy,
            run_args=["-m", "uvicorn", "sync_runner.app:app", "--host", "0.0.0.0", "--port", str(port)],
            workdir=code_dir,
            env={"SYNC_RUNNER_TOKEN": token, "SYNC_SECRETS_DIR": secrets_dir},
            log_path=plat.join(code_dir, "sync-runner.log"),
        ),
    )

    # /healthz 匿名可读，wait 不需要 token；连接拨测（list_secrets）需要，故 token 必须回传
    if not _wait_http(ssh, plat, f"http://127.0.0.1:{port}/healthz", attempts=45):
        log_path = plat.join(code_dir, "sync-runner.log")
        tail = ssh.run(f"tail -n 40 {log_path} 2>/dev/null")
        detail = (tail.stdout or "").strip()
        raise SSHError(
            f"sync-runner 启动超时（{port}）："
            f"{service_check_hint(plat, 'ontometa-sync-runner', 'com.ontometa.sync-runner')}"
            + (f"\n--- {log_path} 末尾 ---\n{detail}" if detail else "")
        )
    return {"endpoint": f"http://{_host(spec)}:{port}", "token": token}


def _teardown_sync_runner(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    plat = detect_platform(ssh)
    return stop_service(ssh, plat, unit_name="ontometa-sync-runner", label="com.ontometa.sync-runner")


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


def _teardown_postgres(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    plat = detect_platform(ssh)
    if plat.is_darwin:
        r = ssh.run("brew services stop postgresql@16")
    else:
        r = ssh.sudo("systemctl stop postgresql")
    return None if r.ok else (r.stderr or "停止 postgresql 失败")[:300]


def _teardown_airflow(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    ssh.run("pkill -f 'airflow standalone' || true")
    return None


def _teardown_cube(ssh: SSHSession, spec: dict[str, Any]) -> str | None:
    ssh.run("pkill -f cubejs-server || true")
    return None


# --------------------------------------------------------------------- 注册表

INSTALL_RECIPES: dict[str, Recipe] = {
    "postgres": Recipe(REAL, _install_postgres, _teardown_postgres),
    "airflow": Recipe(REAL, _install_airflow, _teardown_airflow),
    "cube": Recipe(REAL, _install_cube, _teardown_cube),
    "seatunnel": Recipe(REAL, _install_seatunnel, _noop_teardown),
    "sync_runner": Recipe(REAL, _install_sync_runner, _teardown_sync_runner),
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
