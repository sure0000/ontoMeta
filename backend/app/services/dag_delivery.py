"""DAG 产物投递器：把生成的 DAG 文件交到 Airflow 可见的位置。

**唯一投递方式（SshDelivery）**：ontoMeta 在本地临时目录组装好整个制品包，用 rsync
经 SSH 推到 Airflow 主机，再在远端原子切换到最终路径。ontoMeta、Airflow、Flink/YARN
分处三台机器是常态，"写本地文件系统"在那种拓扑下只会把产物写进一台没人看的机器。

**为什么只留一条通道**：开发环境跑的就是生产路径。此前 local / git-sync 两条分支里，
local 在跨机时会"成功"但产物根本不到位（要等 dag_exists 轮询超时才暴露），git-sync 则
要求一整套 git 基础设施 + Airflow 侧 sidecar。想在本机验证就把 ssh_host 指向 localhost。

**制品包布局**（远端）::

    <dags_dir>/ontometa/
      _lib/sql-runner-<sha12>.jar     ← 内容寻址，跨 artifact 共享
      <artifact_id>/
        <dag_id>.py
        <dag_id>.json                 ← spec（边车 JSON）
        jobs/*.sql

jar 走 ``lib_files`` 而非 ``job_files``：前者是二进制、按内容寻址、多个制品共用一份；
后者是文本、每个制品独有。合并成一个参数就得在写入时按扩展名猜编码方式。

**原子性**：Airflow 的 scheduler 周期扫 dags 目录，会读到传了一半的 .py。故先 rsync 到
``<dags_dir>/.staging/<artifact_id>/``，校验无误后 ``mv`` 到最终路径（同文件系统 rename
是原子的），且 .py 最后落位——spec 与 SQL 都就位了，DAG 文件才出现在 Airflow 眼前。

投递器只管「把文件交出去」，不管「生成什么文件」（DagBundle 负责）或「如何触发运行」
（AirflowClient 负责）。运行期配置由 AirflowRuntimeConfig 携带。
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class DagDeliveryError(RuntimeError):
    """投递失败（连不上主机/目录不可写/传输中断等），面向用户可读。"""


@dataclass
class DeliveryResult:
    """投递结果：写入的文件清单与可选的补充信息。

    ``files_written`` 的值一律是**远端绝对路径**——用户拿它去 Airflow 主机上找文件。
    值只放字符串（不放列表）：前端契约是 ``Record<string, string>``，多个同类文件
    拼成一个逗号分隔串在 UI 上没法看，故 SQL 用 ``sql_dir`` 给目录而非逐个列出。
    """

    files_written: dict[str, str] = field(default_factory=dict)
    note: str | None = None  # 可选补充说明（如 user@host:path）


class DagDelivery(ABC):
    """DAG 产物投递器抽象：把 DagBundle 的内容交到 Airflow 可见的位置。"""

    @abstractmethod
    def deliver(
        self,
        *,
        dags_dir: str,
        jobs_dir: str,
        dag_filename: str,
        dag_source: str,
        spec_filename: str,
        spec: dict,
        job_files: dict[str, dict | str],
        lib_files: dict[str, bytes] | None = None,
    ) -> DeliveryResult:
        """投递一个 DAG 包的全部文件。

        Args:
            dags_dir: DAG 文件与 spec 的目标目录（**远端路径**）
            jobs_dir: SQL/作业配置的目标目录（**远端路径**），通常是 dags_dir/jobs
            dag_filename: DAG 文件名（如 ``ontometa_flink_abc123.py``）
            dag_source: DAG 源码
            spec_filename: 边车 spec 文件名（如 ``ontometa_flink_abc123.json``）
            spec: spec 内容（dict，序列化为 JSON）
            job_files: ``{文件名: 内容}``，内容为 dict 时序列化为 JSON，为 str 时原样写
            lib_files: ``{文件名: 字节}``，投到 ``<dags_dir>/ontometa/_lib/``。
                内容寻址（文件名含 sha），已存在同名文件即同内容，rsync 会自动跳过。

        Returns:
            DeliveryResult（files_written 是远端绝对路径）

        Raises:
            DagDeliveryError: 投递失败
        """
        raise NotImplementedError


class SshDelivery(DagDelivery):
    """SSH 投递器：rsync 推到 Airflow 主机，远端原子切换。

    **认证**：优先私钥（``key_path``），否则密码（需要 ``sshpass``——ssh 二进制不从
    stdin 读密码）。缺 sshpass 时明确报错而非静默降级成免密尝试，否则用户看到的会是
    一句指不到原因的 "Permission denied"。

    **传输 seam**：所有子进程调用收在 ``_run`` 一处。测试据此注入替身，既能验路径拼接
    与命令序列，又不必真开一个 sshd。
    """

    def __init__(
        self,
        *,
        host: str,
        user: str | None = None,
        port: int = 22,
        key_path: str | None = None,
        password: str | None = None,
        timeout: float = 120.0,
        strict_host_key_checking: bool = False,
    ):
        """
        Args:
            host: Airflow 主机
            user: SSH 用户名，不设则用 ssh 自身的默认（~/.ssh/config 或当前用户）
            port: SSH 端口
            key_path: 私钥路径（ontoMeta 侧可读）
            password: SSH 密码（key_path 为空时使用，需要 sshpass）
            timeout: 单条命令的超时（秒）
            strict_host_key_checking: 关掉时首次连接不会因 known_hosts 缺条目而卡住。
                默认关——投递目标是运维自己配的内网主机，且卡住的表现是超时而非报错。
        """
        if not host:
            raise DagDeliveryError("SSH 投递缺少主机地址（设置页 → Airflow → SSH 主机）")
        self.host = host
        self.user = user or None
        self.port = port or 22
        self.key_path = key_path or None
        self.password = password or None
        self.timeout = timeout
        self.strict_host_key_checking = strict_host_key_checking

    # ---- 传输原语（测试注入点）----

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """跑一条子进程命令。**测试注入点**：替身覆盖这里就能接管全部传输。

        只负责执行；错误翻译在 ``_exec``——两者分开，替身抛的 CalledProcessError
        才同样会被翻成面向用户的消息（否则调用方收到的是一句 Python 异常原文）。
        """
        return subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=self.timeout
        )

    def _exec(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """执行并把子进程异常翻成面向用户的 DagDeliveryError。"""
        try:
            return self._run(cmd)
        except subprocess.TimeoutExpired as exc:
            raise DagDeliveryError(
                f"SSH 操作超时（{self.timeout:.0f}s）：{self.target}。"
                "请确认主机可达、网络通畅。"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip() or "无输出"
            raise DagDeliveryError(
                f"SSH 投递失败（returncode={exc.returncode}）：{self.target}\n{detail}"
            ) from exc

    @property
    def target(self) -> str:
        """``user@host`` 或 ``host``，用于 rsync 目标与错误消息。"""
        return f"{self.user}@{self.host}" if self.user else self.host

    def _auth_prefix(self) -> list[str]:
        """密码认证时的 sshpass 前缀；密钥认证返回空。"""
        if self.key_path or not self.password:
            return []
        if not shutil.which("sshpass"):
            raise DagDeliveryError(
                "SSH 密码认证需要 sshpass，但未安装（ssh 不从标准输入读密码）。"
                "请安装 sshpass（macOS: brew install sshpass），"
                "或改用密钥认证（设置页 → Airflow → SSH 私钥路径）。"
            )
        return ["sshpass", "-p", self.password]

    def _ssh_opts(self) -> list[str]:
        """ssh 的公共选项（端口/私钥/主机校验），rsync 经 -e 复用同一套。"""
        opts = ["-p", str(self.port), "-o", "BatchMode=" + ("no" if self.password and not self.key_path else "yes")]
        if self.key_path:
            opts += ["-i", self.key_path, "-o", "IdentitiesOnly=yes"]
        if not self.strict_host_key_checking:
            opts += ["-o", "StrictHostKeyChecking=no"]
        return opts

    def _ssh(self, remote_cmd: str) -> subprocess.CompletedProcess:
        """在远端跑一条 shell 命令。"""
        return self._exec(self._auth_prefix() + ["ssh"] + self._ssh_opts() + [self.target, remote_cmd])

    def _rsync(self, local_dir: str, remote_dir: str) -> subprocess.CompletedProcess:
        """把本地目录内容同步到远端目录（增量：内容相同的文件自动跳过）。"""
        # rsync 的 -e 要一个字符串形式的 ssh 命令；端口/私钥选项都塞这里。
        ssh_cmd = " ".join(["ssh"] + self._ssh_opts())
        return self._exec(
            self._auth_prefix()
            + [
                "rsync",
                "-a",  # 保留权限/时间戳，递归
                "-e",
                ssh_cmd,
                # 末尾斜杠：同步目录**内容**而非目录本身
                local_dir.rstrip("/") + "/",
                f"{self.target}:{remote_dir}",
            ]
        )

    # ---- 投递 ----

    def deliver(
        self,
        *,
        dags_dir: str,
        jobs_dir: str,
        dag_filename: str,
        dag_source: str,
        spec_filename: str,
        spec: dict,
        job_files: dict[str, dict | str],
        lib_files: dict[str, bytes] | None = None,
    ) -> DeliveryResult:
        # 远端路径一律 posixpath：Airflow 主机通常是 Linux，而 ontoMeta 可能跑在
        # macOS/Windows 上，用 os.path 会在 Windows 上拼出反斜杠。
        remote_dags = _as_posix(dags_dir)
        remote_jobs = _as_posix(jobs_dir)
        lib_files = lib_files or {}

        staging_root = posixpath.join(remote_dags, ".staging")
        # staging 名带 DAG id：同一 dags_dir 下多个制品并发投递时互不干扰。
        staging_dir = posixpath.join(staging_root, spec_filename.rsplit(".", 1)[0])

        local_root = tempfile.mkdtemp(prefix="ontometa-bundle-")
        try:
            # 1. 本地组装完整包
            self._stage_locally(
                local_root=local_root,
                spec_filename=spec_filename,
                spec=spec,
                job_files=job_files,
                remote_dags=remote_dags,
                remote_jobs=remote_jobs,
            )

            # 2. 远端建 staging（-p：父目录一并建）
            self._ssh(f"mkdir -p {_q(staging_dir)}")

            # 3. rsync 包体（不含 DAG 文件——它最后单独落位）
            self._rsync(local_root, staging_dir)

            # 4. jar 投到共享的 _lib/。内容寻址：文件名含 sha，rsync 判定同名即同内容
            #    自动跳过，不会为每个制品重复传一份。
            lib_written = self._deliver_lib(remote_dags, lib_files)

            # 5. 原子切换：staging 内容 mv 到最终目录
            self._ssh(
                f"mkdir -p {_q(remote_dags)} {_q(remote_jobs)} && "
                # /. 尾缀让 cp 复制目录内容而非目录本身；-f 覆盖同名旧产物
                f"cp -rf {_q(staging_dir)}/. {_q(remote_dags)}/ && "
                f"rm -rf {_q(staging_dir)}; "
                # 顺手收掉空的 .staging——它就在 Airflow 扫描的 dags 目录里，
                # 留着是垃圾。非空说明有并发投递在用，rmdir 会失败，忽略即可。
                f"rmdir {_q(staging_root)} 2>/dev/null || true"
            )

            # 6. DAG 文件最后落位——此刻 spec 与 SQL 都已就位，Airflow 一看到 .py
            #    就能完整解析，不会撞上半个包。
            dag_final = posixpath.join(remote_dags, dag_filename)
            self._write_remote_text(dag_final, dag_source)
        finally:
            shutil.rmtree(local_root, ignore_errors=True)

        written = {
            "dag": dag_final,
            "spec": posixpath.join(remote_dags, spec_filename),
        }
        if job_files:
            written["sql_dir"] = remote_jobs
        written.update(lib_written)
        return DeliveryResult(
            files_written=written,
            note=f"已投递到 {self.target}:{remote_dags}",
        )

    def _stage_locally(
        self,
        *,
        local_root: str,
        spec_filename: str,
        spec: dict,
        job_files: dict[str, dict | str],
        remote_dags: str,
        remote_jobs: str,
    ) -> None:
        """在本地临时目录里按远端布局摆好文件（DAG 文件除外）。"""
        with open(os.path.join(local_root, spec_filename), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, indent=2, sort_keys=True)

        if not job_files:
            return
        # jobs_dir 通常是 dags_dir/jobs；若配成别处，本地就按那个相对位置摆。
        rel = posixpath.relpath(remote_jobs, remote_dags)
        jobs_local = os.path.join(local_root, *rel.split("/")) if rel != "." else local_root
        os.makedirs(jobs_local, exist_ok=True)
        for name, conf in sorted(job_files.items()):
            with open(os.path.join(jobs_local, name), "w", encoding="utf-8") as fh:
                if isinstance(conf, str):
                    fh.write(conf)
                else:
                    json.dump(conf, fh, ensure_ascii=False, indent=2, sort_keys=True)

    def _deliver_lib(self, remote_dags: str, lib_files: dict[str, bytes]) -> dict[str, str]:
        """把 jar 之类的二进制投到 ontometa/_lib/。

        ``_lib`` 与各 artifact 目录平级（都在 ``<dags_dir>/ontometa/`` 下），故从
        remote_dags（= ``…/ontometa/<artifact_id>``）往上一级取。前缀下划线不会与
        artifact_id（十六进制哈希）撞名，且 Airflow 的 DagBag 只认 .py/.zip，
        目录里躺个 .jar 不会被当 DAG 解析。
        """
        if not lib_files:
            return {}
        lib_dir = posixpath.join(posixpath.dirname(remote_dags), "_lib")
        local_lib = tempfile.mkdtemp(prefix="ontometa-lib-")
        try:
            for name, blob in sorted(lib_files.items()):
                with open(os.path.join(local_lib, name), "wb") as fh:
                    fh.write(blob)
            self._ssh(f"mkdir -p {_q(lib_dir)}")
            self._rsync(local_lib, lib_dir)
        finally:
            shutil.rmtree(local_lib, ignore_errors=True)
        return {"lib_dir": lib_dir}

    def _write_remote_text(self, remote_path: str, content: str) -> None:
        """把一段文本写到远端文件（走 rsync 单文件，避免 shell 转义把源码改坏）。"""
        local_dir = tempfile.mkdtemp(prefix="ontometa-dag-")
        try:
            name = posixpath.basename(remote_path)
            with open(os.path.join(local_dir, name), "w", encoding="utf-8") as fh:
                fh.write(content)
            self._rsync(local_dir, posixpath.dirname(remote_path))
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)


def _as_posix(path: str) -> str:
    """本地路径字符串 → 远端 posix 路径（Windows 上的反斜杠转成斜杠）。"""
    return path.replace("\\", "/")


def _q(path: str) -> str:
    """远端 shell 的路径转义（目录名可能含空格）。"""
    return "'" + path.replace("'", "'\\''") + "'"


def get_delivery(
    host: str,
    *,
    user: str | None = None,
    port: int = 22,
    key_path: str | None = None,
    password: str | None = None,
) -> DagDelivery:
    """构造投递器。

    只有 SSH 一条通道——想在本机验证就把 host 指向 localhost，跑的仍是生产路径。

    Args:
        host: Airflow 主机
        user: SSH 用户名
        port: SSH 端口
        key_path: 私钥路径（优先）
        password: SSH 密码（key_path 为空时用，需要 sshpass）

    Returns:
        DagDelivery 实例

    Raises:
        DagDeliveryError: 缺少主机地址
    """
    return SshDelivery(
        host=host, user=user, port=port, key_path=key_path, password=password
    )
