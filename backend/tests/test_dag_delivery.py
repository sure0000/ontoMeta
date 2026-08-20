"""DAG 投递器测试：SshDelivery（唯一投递通道）。

**替身传输而非真 sshd**：投递器把所有子进程调用收在 ``_run`` 一处，测试覆盖它就能
断言"发了哪些命令、按什么顺序"，且零环境依赖。此前的 git 投递器直接 subprocess.run，
导致 preflight 的 git 管道检查至今零覆盖——那正是要避免的形状。

部分用例让替身把 rsync/ssh **真的作用在本地临时目录**上（模拟远端文件系统），
这样落位、覆盖、原子切换这些行为是被真实验证的，不是对着 mock 断言。
"""

import json
import os
import subprocess
import tempfile

import pytest

from app.services.dag_delivery import (
    DagDeliveryError,
    SshDelivery,
    get_delivery,
)


class _RecordingSsh(SshDelivery):
    """替身：记下每条命令；rsync/ssh 真作用在本地目录（远端 = 本地某路径）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.commands: list[list[str]] = []

    def _run(self, cmd):
        self.commands.append(list(cmd))
        if cmd and cmd[0] == "rsync" or (len(cmd) > 2 and cmd[2] == "rsync"):
            src, dst = cmd[-2], cmd[-1]
            real = dst.split(":", 1)[1]
            os.makedirs(real, exist_ok=True)
            subprocess.run(["rsync", "-a", src, real], check=True, capture_output=True)
        else:
            subprocess.run(["bash", "-c", cmd[-1]], check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def ssh_scripts(self) -> list[str]:
        """远端跑过的 shell 命令（按顺序）。"""
        return [c[-1] for c in self.commands if "ssh" in c[0] or c[0] == "sshpass"]


def _deliver(delivery, dags_dir, **over):
    kwargs = dict(
        dags_dir=dags_dir,
        jobs_dir=os.path.join(dags_dir, "jobs"),
        dag_filename="my_dag.py",
        dag_source="# generated\n",
        spec_filename="my_dag.json",
        spec={"dag_id": "my_dag", "tasks": []},
        job_files={"t1.sql": "SELECT 1;", "cfg.json": {"a": 1}},
        lib_files={"sql-runner-abc123def456.jar": b"\xca\xfe\xba\xbe"},
    )
    kwargs.update(over)
    return delivery.deliver(**kwargs)


# ---------- 工厂 ----------


def test_get_delivery_returns_ssh():
    assert isinstance(get_delivery("airflow-host"), SshDelivery)


def test_get_delivery_rejects_empty_host():
    """没有主机就没有投递目标——早报错，别等 dag_exists 轮询超时才发现。"""
    with pytest.raises(DagDeliveryError, match="缺少主机地址"):
        get_delivery("")


def test_target_includes_user_when_given():
    assert get_delivery("h", user="deploy").target == "deploy@h"
    assert get_delivery("h").target == "h"


# ---------- 落位 ----------


def test_delivery_writes_all_artifacts_to_remote_layout():
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        d = _RecordingSsh(host="localhost")
        result = _deliver(d, dags)

        assert os.path.isfile(os.path.join(dags, "my_dag.py"))
        assert os.path.isfile(os.path.join(dags, "my_dag.json"))
        assert os.path.isfile(os.path.join(dags, "jobs", "t1.sql"))
        # jar 落**共享**的 _lib/，与制品目录平级（跨制品复用同一份）
        assert os.path.isfile(
            os.path.join(tmp, "ontometa", "_lib", "sql-runner-abc123def456.jar")
        )

        assert open(os.path.join(dags, "my_dag.py")).read() == "# generated\n"
        assert open(os.path.join(dags, "jobs", "t1.sql")).read() == "SELECT 1;"
        with open(os.path.join(dags, "jobs", "cfg.json")) as f:
            assert json.load(f)["a"] == 1
        with open(os.path.join(dags, "my_dag.json")) as f:
            assert json.load(f)["dag_id"] == "my_dag"


def test_files_written_are_remote_paths():
    """回执给的必须是远端路径——ontoMeta 与 Airflow 不同机时本地视角的路径是错的。

    此前调用方读的是不存在的 ``result.written``，恒为 None/{}，物化的「产物路径」
    面板因此永远空白。
    """
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        result = _deliver(_RecordingSsh(host="af-host", user="deploy"), dags)

        assert result.files_written["dag"] == os.path.join(dags, "my_dag.py")
        assert result.files_written["spec"] == os.path.join(dags, "my_dag.json")
        assert result.files_written["sql_dir"] == os.path.join(dags, "jobs")
        assert result.files_written["lib_dir"].endswith("_lib")
        # 前端契约是 Record<string, string>，列表会被渲染成逗号拼接串
        assert all(isinstance(v, str) for v in result.files_written.values())
        assert "deploy@af-host" in result.note


def test_jar_is_content_addressed_and_shared_across_artifacts():
    """两个制品用同一个 jar：只存一份，且都落在共享的 _lib/。"""
    with tempfile.TemporaryDirectory() as tmp:
        jar = {"sql-runner-deadbeef1234.jar": b"JAR"}
        for art in ("artA", "artB"):
            _deliver(
                _RecordingSsh(host="h"),
                os.path.join(tmp, "ontometa", art),
                lib_files=jar,
            )
        lib = os.path.join(tmp, "ontometa", "_lib")
        assert os.listdir(lib) == ["sql-runner-deadbeef1234.jar"]


# ---------- 原子性 ----------


def test_dag_file_lands_last():
    """.py 必须最后落位：Airflow 的 scheduler 周期扫 dags 目录，先看到 DAG 文件
    再看到 spec 就会解析出一个读不到边车 JSON 的 DAG。"""
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        d = _RecordingSsh(host="h")
        _deliver(d, dags)

        rsync_dests = [c[-1] for c in d.commands if c[0] == "rsync"]
        # 最后一次传输就是 DAG 文件所在目录
        assert rsync_dests[-1].endswith(dags)
        # 且它不在 staging 里——staging 已经切换完了
        assert ".staging" not in rsync_dests[-1]


def test_bundle_goes_through_staging_then_switches():
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        d = _RecordingSsh(host="h")
        _deliver(d, dags)

        scripts = " ".join(d.ssh_scripts)
        assert ".staging" in scripts  # 先进暂存
        assert "cp -rf" in scripts and "rm -rf" in scripts  # 再切换并清理


def test_staging_dir_is_cleaned_up():
    """.staging 就在 Airflow 扫描的 dags 目录里，留着是垃圾。"""
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        _deliver(_RecordingSsh(host="h"), dags)
        assert not os.path.exists(os.path.join(dags, ".staging"))


def test_redelivery_overwrites_previous_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art123")
        _deliver(_RecordingSsh(host="h"), dags)
        _deliver(
            _RecordingSsh(host="h"),
            dags,
            dag_source="# v2\n",
            job_files={"t1.sql": "SELECT 2;"},
        )
        assert open(os.path.join(dags, "my_dag.py")).read() == "# v2\n"
        assert open(os.path.join(dags, "jobs", "t1.sql")).read() == "SELECT 2;"
        assert not os.path.exists(os.path.join(dags, ".staging"))


# ---------- 无 SQL / 无 jar（链 DAG 形态）----------


def test_chain_dag_shape_without_jobs_or_lib():
    """链 DAG 只有骨架：它调度别的 DAG，不自己跑 Flink。"""
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "chain1")
        result = _deliver(_RecordingSsh(host="h"), dags, job_files={}, lib_files=None)

        assert os.path.isfile(os.path.join(dags, "my_dag.py"))
        assert sorted(result.files_written) == ["dag", "spec"]


# ---------- 认证 ----------


def test_key_auth_passes_identity_options():
    opts = get_delivery("h", key_path="/keys/id_ed25519")._ssh_opts()
    assert "-i" in opts and "/keys/id_ed25519" in opts
    assert "IdentitiesOnly=yes" in opts


def test_custom_port_is_passed():
    assert get_delivery("h", port=2222)._ssh_opts()[:2] == ["-p", "2222"]


def test_password_auth_requires_sshpass(monkeypatch):
    """ssh 不从 stdin 读密码。缺 sshpass 时明确报错，而不是静默退回免密尝试——
    后者的表现是一句指不到原因的 Permission denied。"""
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(DagDeliveryError, match="sshpass"):
        get_delivery("h", password="pw")._auth_prefix()


def test_password_auth_uses_sshpass_when_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/sshpass")
    assert get_delivery("h", password="pw")._auth_prefix() == ["sshpass", "-p", "pw"]


def test_key_path_takes_precedence_over_password(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)  # 没装 sshpass 也不该报错
    assert get_delivery("h", key_path="/k", password="pw")._auth_prefix() == []


# ---------- 错误映射 ----------


def test_command_failure_becomes_readable_error():
    class Failing(SshDelivery):
        def _run(self, cmd):
            raise subprocess.CalledProcessError(255, cmd, stderr="Permission denied")

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(DagDeliveryError, match="Permission denied"):
            _deliver(Failing(host="h"), os.path.join(tmp, "d"))


def test_timeout_becomes_readable_error():
    class Hanging(SshDelivery):
        def _run(self, cmd):
            raise subprocess.TimeoutExpired(cmd, 120)

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(DagDeliveryError, match="超时"):
            _deliver(Hanging(host="h"), os.path.join(tmp, "d"))


def test_local_staging_is_cleaned_up_even_on_failure():
    """ontoMeta 侧不留产物——失败路径也不能漏临时目录。"""
    import glob

    class Failing(SshDelivery):
        def _run(self, cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ontometa-*")))
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(DagDeliveryError):
            _deliver(Failing(host="h"), os.path.join(tmp, "d"))
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "ontometa-*"))) == before


# ---------- 路径处理 ----------


def test_remote_paths_use_posix_separators():
    """远端通常是 Linux，而 ontoMeta 可能跑在别的 OS 上。"""
    d = _RecordingSsh(host="h")
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "ontometa", "art1")
        result = _deliver(d, dags)
    assert "\\" not in result.files_written["dag"]


def test_paths_with_spaces_are_quoted():
    with tempfile.TemporaryDirectory() as tmp:
        dags = os.path.join(tmp, "air flow", "ontometa", "art1")
        d = _RecordingSsh(host="h")
        _deliver(d, dags)  # 不抛即说明远端 shell 没被空格拆断
        assert os.path.isfile(os.path.join(dags, "my_dag.py"))
