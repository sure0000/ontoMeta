"""DAG 投递器测试：LocalFsDelivery 与 GitSyncDelivery。"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services.dag_delivery import (
    DagDeliveryError,
    GitSyncDelivery,
    LocalFsDelivery,
    get_delivery,
)


def test_get_delivery_returns_local_by_default():
    delivery = get_delivery("local")
    assert isinstance(delivery, LocalFsDelivery)


def test_get_delivery_recognizes_git_variants():
    for method in ("git", "git-sync", "gitsync", "GIT", "Git-Sync"):
        delivery = get_delivery(method)
        assert isinstance(delivery, GitSyncDelivery)


def test_local_delivery_writes_files_to_disk():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = os.path.join(tmpdir, "dags")
        jobs_dir = os.path.join(tmpdir, "jobs")

        delivery = LocalFsDelivery()
        result = delivery.deliver(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            dag_filename="test_dag.py",
            dag_source="# dag source\n",
            spec_filename="test_dag.json",
            spec={"dag_id": "test_dag", "tasks": []},
            job_files={
                "job1.json": {"source": "erp", "target": "warehouse"},
                "script.sql": "SELECT 1;",
            },
        )

        # 验证文件都落盘了
        assert os.path.isfile(os.path.join(dags_dir, "test_dag.py"))
        assert os.path.isfile(os.path.join(dags_dir, "test_dag.json"))
        assert os.path.isfile(os.path.join(jobs_dir, "job1.json"))
        assert os.path.isfile(os.path.join(jobs_dir, "script.sql"))

        # 验证内容正确
        with open(os.path.join(dags_dir, "test_dag.py")) as f:
            assert f.read() == "# dag source\n"

        with open(os.path.join(jobs_dir, "script.sql")) as f:
            assert f.read() == "SELECT 1;"

        with open(os.path.join(jobs_dir, "job1.json")) as f:
            data = json.load(f)
            assert data["source"] == "erp"

        # 返回值包含已写入的文件路径
        assert "dag" in result.files_written
        assert "spec" in result.files_written
        assert "jobs_dir" in result.files_written


def test_local_delivery_creates_directories_if_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = os.path.join(tmpdir, "nested", "dags")
        jobs_dir = os.path.join(tmpdir, "nested", "jobs")

        delivery = LocalFsDelivery()
        delivery.deliver(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            dag_filename="dag.py",
            dag_source="",
            spec_filename="dag.json",
            spec={},
            job_files={},
        )

        assert os.path.isdir(dags_dir)
        assert os.path.isdir(jobs_dir)


def test_git_sync_delivery_commits_and_pushes():
    """端到端测试：设一个 bare remote + 一个工作副本，投递后验证 push 到 remote。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        remote_dir = os.path.join(tmpdir, "remote.git")
        work_dir = os.path.join(tmpdir, "work")

        # 建一个 bare remote
        subprocess.run(
            ["git", "init", "--bare", remote_dir, "-b", "main"],
            check=True,
            capture_output=True,
        )

        # clone 一个工作副本
        subprocess.run(
            ["git", "clone", remote_dir, work_dir],
            check=True,
            capture_output=True,
        )

        # 配置 git 用户（commit 需要）
        subprocess.run(
            ["git", "config", "user.email", "test@test"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )

        # 初始 commit 让 main 分支存在
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )

        # 投递一个 DAG
        dags_dir = os.path.join(work_dir, "dags")
        jobs_dir = os.path.join(work_dir, "jobs")

        delivery = GitSyncDelivery(
            remote_name="origin", branch="main", commit_author="CI", commit_email="ci@ci"
        )
        result = delivery.deliver(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            dag_filename="my_dag.py",
            dag_source="# generated\n",
            spec_filename="my_dag.json",
            spec={"dag_id": "my_dag"},
            job_files={"job.json": {"a": 1}},
        )

        # 验证已推送（note 里有 commit hash）
        assert "已推送到 origin/main" in result.note
        assert "commit" in result.note

        # 验证文件确实到了 remote（clone 一个新副本检查）
        verify_dir = os.path.join(tmpdir, "verify")
        subprocess.run(
            ["git", "clone", remote_dir, verify_dir],
            check=True,
            capture_output=True,
        )
        assert os.path.isfile(os.path.join(verify_dir, "dags", "my_dag.py"))
        assert os.path.isfile(os.path.join(verify_dir, "dags", "my_dag.json"))
        assert os.path.isfile(os.path.join(verify_dir, "jobs", "job.json"))


def test_git_sync_delivery_is_idempotent():
    """重复投递相同内容不产生新 commit。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        remote_dir = os.path.join(tmpdir, "remote.git")
        work_dir = os.path.join(tmpdir, "work")

        subprocess.run(
            ["git", "init", "--bare", remote_dir, "-b", "main"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", remote_dir, work_dir], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=work_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=work_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=work_dir, check=True, capture_output=True
        )

        delivery = GitSyncDelivery(remote_name="origin", branch="main")
        kwargs = dict(
            dags_dir=os.path.join(work_dir, "dags"),
            jobs_dir=os.path.join(work_dir, "jobs"),
            dag_filename="dag.py",
            dag_source="# v1\n",
            spec_filename="dag.json",
            spec={"v": 1},
            job_files={},
        )

        result1 = delivery.deliver(**kwargs)
        commit1 = result1.note.split("commit ")[1].split(")")[0]

        # 再投递完全相同的内容
        result2 = delivery.deliver(**kwargs)
        commit2 = result2.note.split("commit ")[1].split(")")[0]

        # commit hash 相同 = 没有新 commit
        assert commit1 == commit2


def test_git_sync_fails_if_not_a_repo_and_auto_init_false():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = os.path.join(tmpdir, "dags")

        delivery = GitSyncDelivery(auto_init=False)
        with pytest.raises(DagDeliveryError, match="不在 git 仓库中"):
            delivery.deliver(
                dags_dir=dags_dir,
                jobs_dir=os.path.join(tmpdir, "jobs"),
                dag_filename="dag.py",
                dag_source="",
                spec_filename="dag.json",
                spec={},
                job_files={},
            )


def test_git_sync_auto_init_creates_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = os.path.join(tmpdir, "dags")
        os.makedirs(dags_dir)

        # auto_init=True 但没有 remote，会 init 但 push 时失败（这个测试只验证 init）
        delivery = GitSyncDelivery(auto_init=True)

        # 因为没有 remote，push 会失败；我们只验证 init 这一步
        with pytest.raises(DagDeliveryError, match="Git 操作失败"):
            delivery.deliver(
                dags_dir=dags_dir,
                jobs_dir=os.path.join(tmpdir, "jobs"),
                dag_filename="dag.py",
                dag_source="",
                spec_filename="dag.json",
                spec={},
                job_files={},
            )

        # 验证确实 init 了
        assert (Path(dags_dir) / ".git").is_dir()


def test_local_delivery_handles_symlinks_correctly():
    """在 macOS 上 /tmp 实际是 /private/tmp 的符号链接，验证路径解析正确。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建一个符号链接目录
        real_dir = os.path.join(tmpdir, "real")
        link_dir = os.path.join(tmpdir, "link")
        os.makedirs(real_dir)
        os.symlink(real_dir, link_dir)

        dags_dir = os.path.join(link_dir, "dags")
        jobs_dir = os.path.join(link_dir, "jobs")

        delivery = LocalFsDelivery()
        result = delivery.deliver(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            dag_filename="dag.py",
            dag_source="# test\n",
            spec_filename="dag.json",
            spec={},
            job_files={},
        )

        # 验证文件确实落盘了（通过符号链接也能读到）
        assert os.path.isfile(os.path.join(dags_dir, "dag.py"))
        assert os.path.isfile(os.path.join(real_dir, "dags", "dag.py"))
