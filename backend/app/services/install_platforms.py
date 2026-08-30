"""SSH 安装的平台抽象层：目标机 OS 识别 + 每平台的服务/环境管理。

**为什么单独一层**：同一套配方要跑在 Debian（apt+systemd）、macOS（brew+launchd，用户级免
sudo）、Windows（winget/python + sc.exe，通常要提权）上。配方只写业务步骤，平台差异全部
收进这里：OS 探测、python 解释器名、venv 路径、目录/删除命令、后台常驻服务的注册与启停。

**诚实分级**（沿用 install_recipes 的 tier 哲学）：
- linux：apt + systemd，REAL。
- darwin：brew/已有 python3 + 用户级 LaunchAgent（~/Library/LaunchAgents，免 sudo），REAL。
- windows：代码/venv 可装；常驻服务走 ``sc.exe``，但 OpenSSH 会话默认无管理员提权 token，
  注册大概率失败——此时抛出的 SSHError 带可照做的手动命令（best-effort，不假装一键装好）。

所有配方都要求目标机能联网拉包；python3/python 由 ``ensure_python`` 保证，端口与健康探测
统一走 python（curl 在 Windows 不保证有）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.ssh_installer import SSHError, SSHSession


def shell_quote(s: str) -> str:
    """单引号包裹（供 POSIX shell 安全传参）。Windows 走双引号，见 Platform.py_run。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


@dataclass(frozen=True)
class Platform:
    """目标机平台。``py`` 是系统 python 解释器命令（python3 / python）。"""

    name: str  # "linux" | "darwin" | "windows"
    py: str

    @property
    def is_darwin(self) -> bool:
        return self.name == "darwin"

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"

    def join(self, *parts: str) -> str:
        """命令里的路径分隔：POSIX `/`，Windows `\\`。"""
        return "/".join(parts) if not self.is_windows else "\\".join(parts)

    def sftp_path(self, path: str) -> str:
        """put_file 用路径：Windows OpenSSH 的 sftp-server 认 POSIX 风格（正斜杠）。"""
        return path.replace("\\", "/")

    def venv_python(self, venv: str) -> str:
        """venv 里的 python 解释器绝对路径（布局差异：bin/ vs Scripts\\）。"""
        return (
            self.join(venv, "Scripts", "python.exe")
            if self.is_windows
            else self.join(venv, "bin", "python")
        )

    def py_run(self, expr: str) -> str:
        """``python -c <expr>`` 的跨平台拼法。expr 内部只用单引号：
        POSIX 用单引号包裹（shell_quote 转义），Windows 用双引号（cmd 不解释单引号）。"""
        if self.is_windows:
            return f'{self.py} -c "{expr}"'
        return f"{self.py} -c {shell_quote(expr)}"

    def mkdir_cmd(self, path: str) -> str:
        """建目录（含父级）。cmd 的 mkdir 自动建全路径，POSIX 用 -p。"""
        return f'mkdir -p "{path}"' if not self.is_windows else f'mkdir "{path}"'

    def rm_cmd(self, path: str) -> str:
        return f'rm -f "{path}"' if not self.is_windows else f'del /q "{path}"'

    def background_cmd(self, cmd: str, log: str) -> str:
        """后台起进程并落日志。Windows 的 start /b 在会话断开时会随会话终止——因此
        Windows 常驻服务只走 sc.exe（见 start_service），此方法仅作非服务场景兜底。"""
        if self.is_windows:
            return f'start /b cmd /c "{cmd} > {log} 2>&1"'
        return f"nohup {cmd} > {log} 2>&1 &"


def detect_platform(ssh: SSHSession) -> Platform:
    """探测目标机 OS。uname 优先（Darwin/Linux）；uname 不可用（Windows cmd）退 ver。"""
    r = ssh.run("uname -s")
    if r.ok and r.stdout.strip():
        name = r.stdout.strip().lower()
        if name == "darwin":
            return Platform("darwin", "python3")
        if name:  # 其余（Linux 等）按 POSIX 处理，apt 由 linux 分支的 require_debian 把关
            return Platform("linux", "python3")
    r = ssh.run("ver")
    if r.ok and "windows" in r.stdout.lower():
        return Platform("windows", "python")
    raise SSHError(
        "无法识别目标机操作系统（uname / ver 均不可用）。"
        "请用 external 模式登记已装好的服务，或检查目标机 SSH 登录 shell。"
    )


def require_debian(ssh: SSHSession) -> None:
    """确认目标机有 apt（Linux 分支只支持 Debian/Ubuntu 系）。"""
    if not ssh.run("command -v apt-get").ok:
        raise SSHError(
            "目标机未检出 apt-get：本版 SSH 安装的 Linux 仅支持 Debian/Ubuntu 系；"
            "macOS/Windows 目标机请用对应平台分支（自动识别），其他 Linux 发行版请用 external 模式登记"
        )


def _warn_if_py39(ssh: SSHSession, plat: Platform) -> None:
    """探测 python3 版本；<3.10 只写一条软告警到部署日志（不硬卡）。

    macOS 自带 python3 常是 Xcode CLT 的 3.9，现代依赖的 PEP 604 联合注解可能无法
    解析。这里仅提醒运维尽量使用 3.10+，让「能装但有隐患」可见而非事后天书。
    """
    r = ssh.run(f'{plat.py} -c "import sys; print(sys.version_info[0], sys.version_info[1])"')
    if not r.ok:
        return
    try:
        major, minor = (int(x) for x in r.stdout.split()[:2])
    except (ValueError, IndexError):
        return
    if (major, minor) < (3, 10):
        ssh.note(
            f"注意：目标机 {plat.py} 版本为 {major}.{minor}（<3.10）。"
            "建议 brew install python@3.12 或改用 3.10+ 以免依赖解析失败。"
        )


def ensure_python(ssh: SSHSession, plat: Platform) -> None:
    """确保目标机有可用 python（解释器名随平台），否则按平台装/明确报错。"""
    if plat.is_windows:
        if not ssh.run("python --version").ok:
            raise SSHError(
                "Windows 目标机未检出 python：请安装 python.org 的 Python "
                "（安装时勾选 Add python.exe to PATH）后重试，或改用 external 模式"
            )
        return
    if plat.is_darwin:
        if ssh.run("command -v python3").ok:
            _warn_if_py39(ssh, plat)
            return
        if not ssh.run("command -v brew").ok:
            raise SSHError(
                "macOS 目标机缺 python3 且没有 Homebrew：请先装 Xcode Command Line Tools "
                "（xcode-select --install）或 brew，再重试部署"
            )
        ssh.run("brew install python", timeout=1800).check("brew 安装 python")
        return
    # linux：apt 装 python3（require_debian 兜底非 Debian 发行版）
    require_debian(ssh)
    ssh.sudo("DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout=300).check("apt update")
    ssh.sudo(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv",
        timeout=600,
    ).check("安装 python3-pip/python3-venv")


@dataclass
class ServiceSpec:
    """一个常驻服务的完整描述，由 start_service 按平台落地。"""

    unit_name: str            # systemd / sc.exe 服务名
    label: str                # launchd label
    python_exe: str           # venv python 绝对路径
    run_args: list[str]       # python 之后的参数，如 ["-m", "uvicorn", "…", "--port", "8098"]
    workdir: str
    env: dict[str, str]
    log_path: str


def start_service(ssh: SSHSession, plat: Platform, spec: dict[str, Any], svc: ServiceSpec) -> None:
    """按平台注册并启动常驻服务。失败即抛 SSHError（Windows 附手动命令）。"""
    if plat.is_windows:
        _start_service_windows(ssh, plat, spec, svc)
    elif plat.is_darwin:
        _start_service_launchd(ssh, plat, spec, svc)
    else:
        _start_service_systemd(ssh, plat, spec, svc)


def stop_service(
    ssh: SSHSession, plat: Platform, *, unit_name: str, label: str
) -> str | None:
    """尽力停服/卸载（best-effort：失败只回错误串不抛，与 teardown 契约一致）。"""
    if plat.is_windows:
        r = ssh.run(f"sc.exe stop {unit_name}")
        r2 = ssh.run(f"sc.exe delete {unit_name}")
        if r.ok and r2.ok:
            return None
        return (r2.stderr or r.stderr or "停服失败")[:300]
    if plat.is_darwin:
        # 与 _start_service_launchd 对齐用 user 域；bootout 是现代 launchctl，
        # 老系统/失败退 remove。都幂等，服务不存在不算错。
        r = ssh.run(f"launchctl bootout user/$(id -u)/{label} 2>/dev/null || launchctl remove {label}")
        return None if r.ok else (r.stderr or "停服失败")[:300]
    r = ssh.sudo(f"systemctl disable --now {unit_name}.service")
    return None if r.ok else (r.stderr or "停服失败")[:300]


def service_check_hint(plat: Platform, unit_name: str, label: str) -> str:
    """启动超时等错误里「去哪看」的提示。"""
    if plat.is_windows:
        return f"见目标机 sc query {unit_name}"
    if plat.is_darwin:
        return f"见目标机 launchctl list | grep {label}"
    return f"见目标机 systemctl status {unit_name}"


# --------------------------------------------------------------------- 各平台服务落地


def _start_service_systemd(ssh: SSHSession, plat: Platform, spec: dict[str, Any], svc: ServiceSpec) -> None:
    user = (spec.get("ssh_user") or "root").strip()
    env_path = f"/etc/ontometa/{svc.unit_name}.env"
    unit = (
        "[Unit]\n"
        f"Description=ontometa-{svc.unit_name}\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"User={user}\nWorkingDirectory={svc.workdir}\n"
        f"EnvironmentFile={env_path}\n"
        f"ExecStart={svc.python_exe} {' '.join(svc.run_args)}\n"
        "Restart=on-failure\nRestartSec=3\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    env = "".join(f"{k}={v}\n" for k, v in svc.env.items())
    ssh.put_file(unit, f"/tmp/{svc.unit_name}.service")
    ssh.put_file(env, f"/tmp/{svc.unit_name}.env")
    ssh.sudo("mkdir -p /etc/ontometa")
    ssh.sudo(f"install -o root -g root -m 644 /tmp/{svc.unit_name}.service /etc/systemd/system/").check("装 systemd unit")
    ssh.sudo(f"install -o root -g root -m 600 /tmp/{svc.unit_name}.env {env_path}").check("装环境文件")
    ssh.sudo("systemctl daemon-reload").check("daemon-reload")
    ssh.sudo(f"systemctl enable {svc.unit_name}.service").check("enable 服务")
    # 关键：必须 restart，不能只 enable --now。重装时单元已在运行，--now 的 start 对 active
    # 单元是 no-op，不会重启进程 → 新写入 EnvironmentFile 的 token 没被读进去，runner 仍持旧
    # token，/healthz 匿名放行能过，但带新 token 的 list_secrets 命中 require_token → 401。
    ssh.sudo(f"systemctl restart {svc.unit_name}.service", timeout=120).check("启动服务")


def _start_service_launchd(ssh: SSHSession, plat: Platform, spec: dict[str, Any], svc: ServiceSpec) -> None:
    """用户级 LaunchAgent：~/Library/LaunchAgents，免 sudo（brew 的 node/python 同理）。"""
    home = home_dir(ssh, plat, spec)
    agents_dir = plat.join(home, "Library", "LaunchAgents")
    plist_path = plat.join(agents_dir, f"{svc.label}.plist")
    args = [svc.python_exe, *svc.run_args]
    env_xml = "".join(
        f"    <key>{k}</key>\n    <string>{v}</string>\n" for k, v in svc.env.items()
    )
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{svc.label}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        + "".join(f"    <string>{a}</string>\n" for a in args)
        + "  </array>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{svc.workdir}</string>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n"
        + env_xml
        + "  </dict>\n"
        f"  <key>StandardOutPath</key>\n  <string>{svc.log_path}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{svc.log_path}</string>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        "</dict>\n</plist>\n"
    )
    ssh.run(plat.mkdir_cmd(agents_dir)).check("建 LaunchAgents 目录")
    ssh.put_file(plist, plat.sftp_path(plist_path))
    # 关键：走 per-user 域 user/<uid>，不用 gui/<uid>。
    # SSH 登录没有 Aqua(GUI)会话，因此使用对无头上下文有效的 user 域。
    domain = "user/$(id -u)"
    # 先摘掉旧的再装新的（幂等）。
    ssh.run(f"launchctl bootout {domain}/{svc.label} 2>/dev/null || true")
    ssh.run(f"launchctl bootstrap {domain} {plist_path}").check("bootstrap LaunchAgent")
    # enable 清掉可能残留的 disabled 记录；kickstart 立即拉起，不等 RunAtLoad 的时机
    # （best-effort：RunAtLoad 已能起活，kickstart 只是把「起不起来」尽早暴露到日志）
    ssh.run(f"launchctl enable {domain}/{svc.label} 2>/dev/null || true")
    ssh.run(f"launchctl kickstart {domain}/{svc.label} 2>/dev/null || true")


def _start_service_windows(ssh: SSHSession, plat: Platform, spec: dict[str, Any], svc: ServiceSpec) -> None:
    """sc.exe 注册：写包装 cmd（set env + cd + python -m uvicorn）→ sc create → sc start。

    OpenSSH 会话默认无管理员提权 token，sc.exe create 大概率「拒绝访问」——此时给出
    可照做的手动命令，不假装装好。
    """
    wrapper = plat.join(svc.workdir, f"run-{svc.unit_name}.cmd")
    lines = ["@echo off"]
    lines += [f"set {k}={v}" for k, v in svc.env.items()]
    lines += [f'cd /d "{svc.workdir}"', f'"{svc.python_exe}" {" ".join(svc.run_args)}']
    ssh.run(plat.mkdir_cmd(svc.workdir))
    ssh.put_file("\r\n".join(lines) + "\r\n", plat.sftp_path(wrapper))
    # sc.exe 的 binPath 引号约定：外层一对 + 内层转义一对，路径含空格也能吃
    bin_path = f'"cmd.exe" /c "{wrapper}"'
    sc_create = f'sc.exe create {svc.unit_name} binPath= "\\"{bin_path}\\"" start= auto'
    r = ssh.run(sc_create)
    if not r.ok:
        raise SSHError(
            f"Windows 服务注册失败（rc={r.rc}）：{(r.stderr or r.stdout or '')[:200]}。"
            "SSH 会话默认无管理员提权。请提权后在目标机手动执行：\n"
            f"  {sc_create}\n"
            f"  sc.exe start {svc.unit_name}"
        )
    ssh.run(f"sc.exe start {svc.unit_name}").check("启动服务")


# --------------------------------------------------------------------- 家目录


def home_dir(ssh: SSHSession, plat: Platform, spec: dict[str, Any]) -> str:
    """目标机登录用户的家目录（绝对路径）。SFTP 与 systemd/launchd 都不展开 ~。"""
    if plat.is_windows:
        r = ssh.run("echo %USERPROFILE%")
        return r.stdout.strip() if r.ok and r.stdout.strip() else "C:\\Users\\" + (
            (spec.get("ssh_user") or "").strip() or "Administrator"
        )
    r = ssh.run("echo $HOME")
    if r.ok and r.stdout.strip():
        return r.stdout.strip()
    user = (spec.get("ssh_user") or "root").strip()
    # macOS 没有 getent；id -P 的第六字段是家目录，两平台通用性优于 dscl
    r = ssh.run(
        f"id -P {shell_quote(user)} | cut -d: -f6"
        if plat.is_darwin
        else f"getent passwd {shell_quote(user)} | cut -d: -f6"
    )
    return (r.stdout.strip() if r.ok else "") or f"/home/{user}"
