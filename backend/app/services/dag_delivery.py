"""DAG 产物投递器：把生成的 DAG 文件交到 Airflow 可见的位置。

**默认投递方式（LocalFsDelivery）**：直接写本地文件系统。适用于 Airflow 与 ontoMeta
同机部署、或通过共享网络卷（NFS/SMB）共享 dags 目录的场景——零额外配置，当前行为不变。

**Git-sync 投递方式（GitSyncDelivery）**：落盘后自动 commit + push 到远程 git 仓库，
Airflow 侧用 git-sync sidecar 拉取。适用于跨机部署且有 git 基础设施的场景。产物进 git
天然实现「可 diff、可 review、可回滚」，与方案 A 的治理诉求合二为一。

投递器只管「把文件交出去」，不管「生成什么文件」（DagBundle 负责）或「如何触发运行」
（AirflowClient 负责）。各投递器的运行期配置由 AirflowRuntimeConfig 携带，按 delivery_method
选择实现类。
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class DagDeliveryError(RuntimeError):
    """投递失败（写盘/git 推送失败等），面向用户可读。"""


@dataclass
class DeliveryResult:
    """投递结果：写入/推送的文件清单与可选的补充信息。"""

    files_written: dict[str, str]  # {用途: 绝对路径}
    note: str | None = None  # 可选补充说明（如 git commit hash）


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
    ) -> DeliveryResult:
        """投递一个 DAG 包的全部文件。

        Args:
            dags_dir: DAG 文件与 spec 的目标目录
            jobs_dir: 作业配置/SQL 脚本的目标目录
            dag_filename: DAG 文件名（如 ``ontometa_materialize_abc123.py``）
            dag_source: DAG 源码
            spec_filename: 边车 spec 文件名（如 ``ontometa_materialize_abc123.json``）
            spec: spec 内容（dict，需序列化为 JSON）
            job_files: 作业配置/SQL 文件 ``{文件名: 内容}``，内容为 dict 时序列化为 JSON，
                       为 str 时原样写入（Flink SQL 脚本）

        Returns:
            DeliveryResult 包含已写入的文件清单与可选的补充信息

        Raises:
            DagDeliveryError: 投递失败时抛出，错误信息面向用户可读
        """


class LocalFsDelivery(DagDelivery):
    """默认投递器：直接写本地文件系统。

    与 DagBundle.write() 原逻辑完全一致——当前行为不变，零额外配置。
    """

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
    ) -> DeliveryResult:
        import json

        try:
            os.makedirs(dags_dir, exist_ok=True)
            os.makedirs(jobs_dir, exist_ok=True)
        except OSError as exc:
            raise DagDeliveryError(f"创建目录失败：{exc}") from exc

        written: dict[str, str] = {}

        try:
            dag_path = os.path.join(dags_dir, dag_filename)
            with open(dag_path, "w", encoding="utf-8") as fh:
                fh.write(dag_source)
            written["dag"] = dag_path

            spec_path = os.path.join(dags_dir, spec_filename)
            with open(spec_path, "w", encoding="utf-8") as fh:
                json.dump(spec, fh, ensure_ascii=False, indent=2, sort_keys=True)
            written["spec"] = spec_path

            for name, conf in sorted(job_files.items()):
                job_path = os.path.join(jobs_dir, name)
                with open(job_path, "w", encoding="utf-8") as fh:
                    if isinstance(conf, str):
                        fh.write(conf)
                    else:
                        json.dump(conf, fh, ensure_ascii=False, indent=2, sort_keys=True)

            written["jobs_dir"] = jobs_dir
        except OSError as exc:
            raise DagDeliveryError(f"写入文件失败：{exc}") from exc

        return DeliveryResult(files_written=written)


class GitSyncDelivery(DagDelivery):
    """Git-sync 投递器：落盘后自动 commit + push 到远程 git 仓库。

    Airflow 侧需配置 git-sync sidecar 或使用内置 DAG bundle 机制拉取该仓库。
    产物进 git 实现了「可 diff、可 review、可回滚」，完全契合方案 A 的治理诉求。

    **前置条件**：
    - dags_dir/jobs_dir 所在路径已是一个 git 仓库（或在初始化时自动 init）
    - 配置了 git remote（通常命名为 origin）
    - 当前用户/进程有权限推送到该 remote（SSH key / HTTPS token / 已登录）

    **工作流**：
    1. 写文件到本地工作区（与 LocalFsDelivery 完全相同）
    2. git add 这些文件
    3. git commit（消息包含 DAG id、时间戳）
    4. git push 到远程（失败不回滚本地 commit，便于人工介入）

    **关于并发**：同一仓库多次物化并发提交时，push 可能因「远程已更新」冲突。
    这是 git-sync 的特性而非 bug——每次物化独立 commit，失败时人工 pull + push 即可；
    或者在调用侧加锁（见 materialization_runner 的 _DAG_GIT_LOCK）。
    """

    def __init__(
        self,
        *,
        remote_name: str = "origin",
        branch: str = "main",
        auto_init: bool = True,
        commit_author: str | None = None,
        commit_email: str | None = None,
    ):
        """
        Args:
            remote_name: git remote 名称，默认 origin
            branch: 推送的分支，默认 main
            auto_init: dags_dir 不是 git 仓库时是否自动 git init + 添加 remote（需配置 remote_url）
            commit_author: commit 的 author name，不设则用 git 全局配置
            commit_email: commit 的 author email，不设则用 git 全局配置
        """
        self.remote_name = remote_name
        self.branch = branch
        self.auto_init = auto_init
        self.commit_author = commit_author
        self.commit_email = commit_email

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
    ) -> DeliveryResult:
        # 先写文件（复用 LocalFsDelivery 的逻辑）
        local_delivery = LocalFsDelivery()
        result = local_delivery.deliver(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            dag_filename=dag_filename,
            dag_source=dag_source,
            spec_filename=spec_filename,
            spec=spec,
            job_files=job_files,
        )

        # 判断 dags_dir 是否在 git 仓库中
        repo_root = self._find_git_root(dags_dir)
        if not repo_root:
            if not self.auto_init:
                raise DagDeliveryError(
                    f"{dags_dir} 不在 git 仓库中，且 auto_init=False。"
                    "请先在该目录执行 `git init` 并配置 remote，或启用 auto_init。"
                )
            repo_root = self._init_git_repo(dags_dir)

        # git add + commit + push
        try:
            self._git_add_and_commit(
                repo_root=repo_root,
                dags_dir=dags_dir,
                jobs_dir=jobs_dir,
                dag_filename=dag_filename,
                spec_filename=spec_filename,
                job_files=job_files,
            )
            commit_hash = self._git_push(repo_root)
        except subprocess.CalledProcessError as exc:
            # git 命令失败时给出可读错误（stderr 通常已说清原因）
            raise DagDeliveryError(
                f"Git 操作失败（returncode={exc.returncode}）：\n{exc.stderr or exc.stdout or '无输出'}"
            ) from exc

        result.note = f"已推送到 {self.remote_name}/{self.branch}（commit {commit_hash[:8]}）"
        return result

    def _find_git_root(self, path: str) -> str | None:
        """往上找 .git 目录，找到返回仓库根，找不到返回 None。"""
        current = Path(path).resolve()
        for parent in [current] + list(current.parents):
            if (parent / ".git").is_dir():
                return str(parent)
        return None

    def _init_git_repo(self, path: str) -> str:
        """在 path 初始化 git 仓库。auto_init=True 时调用。"""
        try:
            subprocess.run(
                ["git", "init"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise DagDeliveryError(
                f"git init 失败：{exc.stderr or exc.stdout}"
            ) from exc
        return path

    def _git_add_and_commit(
        self,
        *,
        repo_root: str,
        dags_dir: str,
        jobs_dir: str,
        dag_filename: str,
        spec_filename: str,
        job_files: dict[str, dict | str],
    ) -> None:
        """git add 这批文件 + commit。"""
        import datetime

        # 计算相对于 repo_root 的路径（git add 需要相对路径）。
        # repo_root 来自 _find_git_root 的 Path.resolve()（已解析符号链接，如 macOS 的
        # /tmp → /private/tmp）；dags_dir/jobs_dir 若不同样 resolve，relpath 会算出一条
        # 逃出仓库的 ../.. 路径，git add 直接 fatal: outside repository。两侧同口径解析。
        dags_rel = os.path.relpath(str(Path(dags_dir).resolve()), repo_root)
        jobs_rel = os.path.relpath(str(Path(jobs_dir).resolve()), repo_root)

        files_to_add = [
            os.path.join(dags_rel, dag_filename),
            os.path.join(dags_rel, spec_filename),
        ]
        files_to_add += [os.path.join(jobs_rel, name) for name in job_files]

        # git add
        subprocess.run(
            ["git", "add"] + files_to_add,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

        # 检查是否有东西可 commit（避免 nothing to commit 报错）
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            # 没有变更（重复提交相同内容），跳过 commit
            return

        # 构造 commit 消息：包含 DAG id 与时间戳，便于审计
        dag_id = spec_filename.replace(".json", "")
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        commit_msg = f"ontoMeta DAG 物化：{dag_id}\n\n自动生成于 {timestamp}"

        # git commit（可选配置 author）
        cmd = ["git", "commit", "-m", commit_msg]
        env = os.environ.copy()
        if self.commit_author:
            env["GIT_AUTHOR_NAME"] = self.commit_author
            env["GIT_COMMITTER_NAME"] = self.commit_author
        if self.commit_email:
            env["GIT_AUTHOR_EMAIL"] = self.commit_email
            env["GIT_COMMITTER_EMAIL"] = self.commit_email

        subprocess.run(
            cmd,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def _git_push(self, repo_root: str) -> str:
        """git push 到远程，返回推送后的 commit hash。

        push 失败时抛异常，不回滚本地 commit——冲突时人工 pull + 重新 push 更安全。
        """
        subprocess.run(
            ["git", "push", self.remote_name, self.branch],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

        # 读当前 commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


def get_delivery(
    method: str,
    git_remote: str = "origin",
    git_branch: str = "main",
    git_auto_init: bool = False,
    git_author: str | None = None,
    git_email: str | None = None,
) -> DagDelivery:
    """根据 method 返回对应的投递器实例。

    Args:
        method: 投递方式，``local``（默认）或 ``git``
        git_remote: git-sync 的 remote 名称
        git_branch: git-sync 的分支
        git_auto_init: git-sync 模式下，目录不是仓库时是否自动 init
        git_author: git commit author name
        git_email: git commit author email

    Returns:
        DagDelivery 实例
    """
    method = (method or "local").strip().lower()
    if method in ("git", "git-sync", "gitsync"):
        return GitSyncDelivery(
            remote_name=git_remote,
            branch=git_branch,
            auto_init=git_auto_init,
            commit_author=git_author,
            commit_email=git_email,
        )
    # 默认走本地文件系统（当前行为不变）
    return LocalFsDelivery()
