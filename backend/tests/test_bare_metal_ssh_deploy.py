"""物理机 SSH 安装改造的回归测试。

覆盖四条改造关键路径（不触真实 SSH/网络，paramiko 与 SSHSession 全 mock）：
1. schema 自描述：bare_metal 字段已换成 SSH 接入参数，且不再暴露 token/api_version。
2. deploy_spec 回显：SSH 密码/私钥明文回显供前端预填+显隐切换，*_set/*_hint 保留兼容。
3. 编辑态 secret-merge：机密留空 = 保留原值。
4. SSHSession 认证分派（密码 / 私钥 / 缺凭据报错）+ recipe 派发 + start_deploy 同步/后台切分。
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from app.database import SessionLocal
from app.services import dependency_service as ds
from app.services.dependency_service import DependencyComponentService
from app.services.ssh_installer import CommandResult, SSHError, SSHSession


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def svc():
    return DependencyComponentService()


# --------------------------------------------------------------------- schema

def test_schema_bare_metal_has_ssh_fields_not_service_params(svc):
    """airflow 物理机字段应是 SSH 接入参数，且不含 token/api_version（那是 external 才填的）。"""
    schema = svc.schema()
    fields = {f["name"] for f in schema["bare_metal_params"]["airflow"]}
    # SSH 接入参数在
    assert {"ssh_host", "ssh_user", "auth_method", "ssh_password", "ssh_private_key"} <= fields
    # 服务自身 API 参数不在（由安装流程回收，不再要人填）
    assert "token" not in fields
    assert "api_version" not in fields


def test_schema_roundtrips_through_response_model(svc):
    """schema() 的返回必须能被 DependencySchemaOut 完整接住（防再次漏字段白屏）。"""
    from app.schemas import DependencySchemaOut

    out = DependencySchemaOut(**svc.schema())
    assert "airflow" in out.bare_metal_params
    assert out.docker_params  # 两个曾漏声明的字段都在


def test_private_key_field_is_text_type(svc):
    fields = {f["name"]: f for f in svc.schema()["bare_metal_params"]["airflow"]}
    assert fields["ssh_private_key"]["type"] == "text"
    assert fields["ssh_private_key"]["secret"] is True


# --------------------------------------------------------------------- 脱敏

def test_mask_deploy_spec_echoes_ssh_secrets_plaintext():
    """机密字段（SSH 密码/私钥）现在明文回显供前端预填+显隐切换，*_set/*_hint 保留兼容。"""
    spec = {
        "ssh_host": "192.168.1.10",
        "ssh_user": "root",
        "ssh_password": "supersecret",
        "ssh_private_key": "-----BEGIN KEY-----\nabcdefXYZ",
    }
    masked = ds._mask_deploy_spec("airflow", "bare_metal", spec)
    # 非机密原样
    assert masked["ssh_host"] == "192.168.1.10"
    # 密码：明文回显 + set 标志 + 尾4提示都在
    assert masked["ssh_password"] == "supersecret"
    assert masked["ssh_password_set"] is True
    assert masked["ssh_password_hint"] == "*******cret"  # mask_secret: len-4 个 * + 尾4
    # 私钥：text 型，明文回显 + set 标志；hint 仍固定 ****（尾4无识别价值）
    assert masked["ssh_private_key"] == "-----BEGIN KEY-----\nabcdefXYZ"
    assert masked["ssh_private_key_set"] is True
    assert masked["ssh_private_key_hint"] == "****"


def test_mask_deploy_spec_unset_secret_returns_none():
    masked = ds._mask_deploy_spec("airflow", "bare_metal", {"ssh_host": "h"})
    assert masked["ssh_password_set"] is False
    assert masked["ssh_password_hint"] is None
    assert masked["ssh_password"] is None  # 未设也不发明文


def test_mask_connection_echoes_secret_plaintext():
    """连接信息里的 secret 字段（如 LLM api_key）现在明文回显，*_set/*_hint 保留兼容。"""
    masked = ds._mask_connection("llm", {"api_base_url": "https://x", "api_key": "sk-abc123"})
    # 非机密原样
    assert masked["api_base_url"] == "https://x"
    # 机密明文回显 + set/hint 兼容
    assert masked["api_key"] == "sk-abc123"
    assert masked["api_key_set"] is True
    assert masked["api_key_hint"] == "*****c123"  # mask_secret("sk-abc123") = len9-4=5 个 * + 尾4


# --------------------------------------------------------------------- merge

def test_merge_deploy_spec_blank_secret_preserves_old():
    current = {"ssh_password": "old-pw", "ssh_host": "h1"}
    incoming = {"ssh_password": "", "ssh_host": "h2"}  # 密码留空、host 改
    merged = ds._merge_deploy_spec("airflow", "bare_metal", current, incoming)
    assert merged["ssh_password"] == "old-pw"  # 保留
    assert merged["ssh_host"] == "h2"  # 覆盖


def test_merge_deploy_spec_new_secret_overwrites():
    merged = ds._merge_deploy_spec(
        "airflow", "bare_metal", {"ssh_password": "old"}, {"ssh_password": "new"}
    )
    assert merged["ssh_password"] == "new"


def test_merge_ignores_mask_echo_fields():
    """脱敏回显字段（*_set/*_hint）不得回写进真实 spec。"""
    merged = ds._merge_deploy_spec(
        "airflow", "bare_metal", {"ssh_password": "old"},
        {"ssh_password_set": True, "ssh_password_hint": "****"},
    )
    assert "ssh_password_set" not in merged
    assert merged["ssh_password"] == "old"


# --------------------------------------------------------------------- SSH 会话

def test_ssh_session_requires_host():
    with pytest.raises(SSHError, match="ssh_host"):
        SSHSession({"ssh_user": "root"})


def test_ssh_session_password_auth_connects():
    fake_client = MagicMock()
    fake_paramiko = MagicMock()
    fake_paramiko.SSHClient.return_value = fake_client
    with patch.dict("sys.modules", {"paramiko": fake_paramiko}):
        sess = SSHSession(
            {"ssh_host": "h", "ssh_user": "root", "auth_method": "password", "ssh_password": "pw"}
        )
        sess.connect()
    kwargs = fake_client.connect.call_args.kwargs
    assert kwargs["password"] == "pw"
    assert "pkey" not in kwargs


def test_ssh_session_password_auth_missing_password_errors():
    fake_paramiko = MagicMock()
    fake_paramiko.SSHClient.return_value = MagicMock()
    with patch.dict("sys.modules", {"paramiko": fake_paramiko}):
        sess = SSHSession({"ssh_host": "h", "ssh_user": "root", "auth_method": "password"})
        with pytest.raises(SSHError, match="ssh_password"):
            sess.connect()


def test_ssh_session_key_auth_missing_key_errors():
    fake_paramiko = MagicMock()
    fake_paramiko.SSHClient.return_value = MagicMock()
    with patch.dict("sys.modules", {"paramiko": fake_paramiko}):
        sess = SSHSession({"ssh_host": "h", "ssh_user": "root", "auth_method": "key"})
        with pytest.raises(SSHError, match="ssh_private_key"):
            sess.connect()


def test_ssh_run_collects_rc_stdout_stderr():
    fake_client = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b"hello"
    stdout.channel.recv_exit_status.return_value = 0
    stderr = MagicMock()
    stderr.read.return_value = b""
    fake_client.exec_command.return_value = (MagicMock(), stdout, stderr)
    sess = SSHSession({"ssh_host": "h", "ssh_user": "root", "ssh_password": "pw"})
    sess._client = fake_client
    res = sess.run("echo hello")
    assert res.rc == 0 and res.stdout == "hello" and res.ok


# --------------------------------------------------------------------- recipe 派发

def test_run_install_unknown_key_raises():
    from app.services.install_recipes import run_install

    with pytest.raises(ValueError, match="不支持"):
        run_install("nope", {"ssh_host": "h", "ssh_user": "root", "ssh_password": "p"})


def test_best_effort_component_raises_clear_error():
    """重组件（datahub）配方在缺前置条件时应明确报错，而不是假装装好。"""
    from app.services.install_recipes import INSTALL_RECIPES, BEST_EFFORT

    recipe = INSTALL_RECIPES["datahub"]
    assert recipe.tier == BEST_EFFORT
    with pytest.raises(SSHError):
        recipe.install(MagicMock(), {"ssh_host": "h"})


def test_run_install_dispatches_to_recipe():
    """run_install 应开 SSH 会话并调对应配方，回收其 connection。"""
    import app.services.install_recipes as ir

    sentinel = {"sqlalchemy_url": "postgresql+psycopg://u:p@h:5432/d"}
    fake_recipe = ir.Recipe(ir.REAL, lambda ssh, spec: sentinel, ir._noop_teardown)
    fake_session = MagicMock()
    fake_session.__enter__.return_value = fake_session
    with patch.dict(ir.INSTALL_RECIPES, {"airflow": fake_recipe}), \
         patch.object(ir, "SSHSession", return_value=fake_session):
        conn = ir.run_install("airflow", {"ssh_host": "h", "ssh_user": "root", "ssh_password": "p"})
    assert conn == sentinel


# --------------------------------------------------------------------- 平台识别

def test_detect_platform_linux():
    from app.services.install_platforms import detect_platform

    ssh = MagicMock()
    ssh.run.return_value = CommandResult(0, "Linux", "")
    plat = detect_platform(ssh)
    assert plat.name == "linux" and plat.py == "python3"


def test_detect_platform_darwin():
    from app.services.install_platforms import detect_platform

    ssh = MagicMock()
    ssh.run.return_value = CommandResult(0, "Darwin", "")
    plat = detect_platform(ssh)
    assert plat.is_darwin and plat.py == "python3"


def test_detect_platform_windows_via_ver():
    """uname 不可用（cmd shell）→ 退 ver 识别 Windows。"""
    from app.services.install_platforms import detect_platform

    ssh = MagicMock()
    ssh.run.side_effect = [
        CommandResult(1, "", "not recognized"),
        CommandResult(0, "Microsoft Windows [Version 10.0.22631]", ""),
    ]
    plat = detect_platform(ssh)
    assert plat.is_windows and plat.py == "python"


def test_detect_platform_unknown_raises():
    from app.services.install_platforms import detect_platform

    ssh = MagicMock()
    ssh.run.side_effect = [CommandResult(1, "", ""), CommandResult(1, "", "")]
    with pytest.raises(SSHError, match="无法识别"):
        detect_platform(ssh)


# --------------------------------------------------------------------- 平台分支（airflow / 工具）

def _simple_spec(**extra):
    """通用测试 spec，用于需要基本 SSH 连接信息的测试。"""
    return {
        "ssh_host": "1.2.3.4", "ssh_user": "ubuntu",
        "auth_method": "password", "ssh_password": "pw",
        **extra,
    }


def _linux_run(plat_name: str = "Linux", home: str = "/home/ubuntu", busy: set[int] | None = None):
    """生成按命令分派的 ssh.run：uname/家目录/端口探测按需应答，其余成功。"""
    busy = busy or set()

    def fake_run(cmd, timeout=120.0):
        if cmd == "uname -s":
            return CommandResult(0, plat_name, "")
        if cmd == "echo $HOME":
            return CommandResult(0, home, "")
        if (" -c " in cmd and "bind" in cmd) or (" -c " in cmd and "urllib" in cmd):
            import re
            # 端口探测：python3 -c 'import socket; …s.bind(('0.0.0.0', 8098))…'（shell 引号转义后仍是 bind((…, PORT)) 结构）
            m = re.search(r"bind\(\(.*?, (\d+)\)\)", cmd)
            if m:
                return CommandResult(1, "", "") if int(m.group(1)) in busy else CommandResult(0, "", "")
            return CommandResult(0, "", "")  # 健康探测直接成功
        return CommandResult(0, "", "")
    return fake_run


def test_install_airflow_macos_no_apt():
    """airflow 在 macOS：venv + standalone（纯 Python），无 apt、无 sudo。"""
    import app.services.install_recipes as ir

    ssh = MagicMock()
    ssh.run.side_effect = _linux_run(plat_name="Darwin", home="/Users/u")
    conn = ir._install_airflow(
        ssh, {"ssh_host": "1.2.3.4", "ssh_user": "u", "port": 8081, "admin_password": "pw"}
    )
    cmds = [c.args[0] for c in ssh.run.call_args_list]
    assert not any("apt" in c for c in cmds)
    assert ssh.sudo.call_count == 0
    assert any("apache-airflow==2.9.3" in c for c in cmds)
    assert any("airflow standalone" in c for c in cmds)
    assert conn["endpoint"] == "http://1.2.3.4:8081"


def test_install_airflow_windows_raises_upstream():
    """Airflow 上游不支持 Windows → 明确报错，指向 external。"""
    import app.services.install_recipes as ir

    ssh = MagicMock()
    ssh.run.side_effect = [
        CommandResult(1, "", "not recognized"),
        CommandResult(0, "Microsoft Windows [Version 10.0]", ""),
    ]
    with pytest.raises(SSHError, match="不支持 Windows"):
        ir._install_airflow(ssh, _simple_spec())


def test_wait_http_uses_python_probe():
    """健康探测走 python urllib（绕代理），不再依赖 curl。"""
    from app.services.install_platforms import Platform
    from app.services.install_recipes import _wait_http

    ssh = MagicMock()
    ssh.run.side_effect = [CommandResult(1, "", "down"), CommandResult(0, "", "")]
    ok = _wait_http(ssh, Platform("linux", "python3"), "http://127.0.0.1:8099/healthz", attempts=2, interval=0)
    assert ok is True
    probes = [c.args[0] for c in ssh.run.call_args_list]
    assert all("urllib" in p and "ProxyHandler" in p for p in probes)
    assert not any("curl" in p for p in probes)


def test_home_dir_windows_uses_userprofile():
    from app.services.install_platforms import Platform, home_dir

    ssh = MagicMock()
    ssh.run.return_value = CommandResult(0, "C:\\Users\\dev", "")
    assert home_dir(ssh, Platform("windows", "python"), {}) == "C:\\Users\\dev"
    ssh.run.assert_called_once_with("echo %USERPROFILE%")




def test_deploy_bare_metal_failure_records_deploy_log(svc, db):
    """部署失败 → deploy_log 落库（含失败行），前端可据此定位。"""
    row = _insert_bare_metal_row(
        db, "airflow",
        {"ssh_host": "1.2.3.4", "ssh_user": "ubuntu", "ssh_password": "pw"},
    )

    def boom(key, spec, log=None):
        log.append("$ python3 -c 'probe'")
        raise SSHError("安装失败：某命令 rc=127")

    with patch("app.services.install_recipes.run_install", side_effect=boom):
        result = svc.deploy(db, row.id)
    assert result["ok"] is False
    db.refresh(row)
    assert "== airflow 部署开始" in (row.deploy_log or "")
    assert "$ python3 -c 'probe'" in (row.deploy_log or "")
    assert "安装失败" in (row.deploy_log or "")
    svc.delete_component(db, row.id)


# --------------------------------------------------------------------- start_deploy

# 用多实例组件 llm（而非单例）：会话级共享 DB 里其他用例已建过单例组件，
# 单例 create 会以「已存在」失败。多实例每次 create 都成功，cleanup 只删本用例自己的行，
# 不影响别的测试。
def test_start_deploy_external_is_synchronous(svc, db):
    row = svc.create_component(db, {
        "key": "llm", "deploy_mode": "external",
        "connection": {"api_base_url": "http://localhost:11434", "model": "m"},
    })
    with patch.object(svc, "deploy", return_value={"status": "connected", "ok": True, "message": "ok"}) as m:
        result = svc.start_deploy(db, row.id)
    assert result["need_background"] is False
    m.assert_called_once()
    svc.delete_component(db, row.id)


# bare_metal 用例不能走 create_component：多实例组件（llm）现已收窄为 external-only，
# 会被白名单拒；单例组件（airflow 等）又会撞会话级共享 DB 里别的用例建的行。故直接插模型行，
# 绕开单例守卫与白名单——这两条不是本用例要测的，本用例测的是 deploy 机制本身。
def _insert_bare_metal_row(db, key: str, spec: dict) -> ds.DependencyComponent:
    row = ds.DependencyComponent(
        key=key,
        name=f"test-{key}",
        deploy_mode="bare_metal",
        deploy_spec_json=ds._dumps(spec),
        deploy_status="not_deployed",
        connection_json=ds._dumps({}),
        enabled=True,
        is_default=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_start_deploy_bare_metal_backgrounds_and_sets_deploying(svc, db):
    row = _insert_bare_metal_row(
        db, "airflow", {"ssh_host": "h", "ssh_user": "root", "ssh_password": "pw"}
    )
    result = svc.start_deploy(db, row.id)
    assert result["need_background"] is True
    assert result["status"] == "deploying"
    db.refresh(row)
    assert row.deploy_status == "deploying"  # 已占位，前端轮询可见
    svc.delete_component(db, row.id)


def test_deploy_bare_metal_recovers_connection_and_probes(svc, db):
    """端到端（mock SSH+probe）：bare_metal deploy 应回收 connection 落库并拨测。"""
    row = _insert_bare_metal_row(
        db, "airflow",
        {"ssh_host": "1.2.3.4", "ssh_user": "root", "ssh_password": "pw", "port": 8081},
    )
    conn = {"endpoint": "http://1.2.3.4:8081", "username": "admin", "password": "pw"}
    with patch("app.services.install_recipes.run_install", return_value=conn), \
         patch.object(svc, "probe", return_value=ds.ProbeResult(True, "连接成功", 12)):
        result = svc.deploy(db, row.id)
    assert result["ok"] is True
    db.refresh(row)
    stored = ds._loads(row.connection_json)
    assert stored["endpoint"] == "http://1.2.3.4:8081"
    svc.delete_component(db, row.id)


# --------------------------------------------------------------------- 部署方式白名单

def test_allowed_deploy_modes_restricts_external_only_components():
    """datahub / llm 只允许 external；其余组件全支持。"""
    for k in ("datahub", "llm"):
        assert ds.allowed_deploy_modes(k) == ["external"]
    for k in ("airflow",):
        assert set(ds.allowed_deploy_modes(k)) == set(ds.DEPLOY_MODES)


def test_create_rejects_disallowed_mode(svc, db):
    """external-only 组件用 bare_metal 创建应被后端拦下（前端收窄之外的兜底）。"""
    # 用 llm（external-only 且多实例）：跳过单例卫兵，直达白名单校验；datahub 是单例，
    # 会话级共享库里常已被别的用例建过，先撞「单例已存在」而非白名单，故不用它。
    with pytest.raises(ValueError, match="不支持部署方式"):
        svc.create_component(db, {
            "key": "llm", "deploy_mode": "bare_metal",
            "deploy_spec": {"ssh_host": "h", "ssh_user": "root", "ssh_password": "p"},
        })


def test_schema_exposes_component_deploy_modes():
    """schema() 必须带 component_deploy_modes，供前端收窄模式选择器。"""
    s = svc_schema()
    assert s["component_deploy_modes"]["llm"] == ["external"]
    assert set(s["component_deploy_modes"]["airflow"]) == set(ds.DEPLOY_MODES)


def svc_schema():
    return DependencyComponentService().schema()
