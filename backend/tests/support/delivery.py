"""测试用的投递器替身：把 SSH 传输落到本地临时目录。

SshDelivery 把子进程调用收在 ``_run`` 一处，替身覆盖它就能让 rsync/ssh 真的作用在
本地路径上——"远端"于是变成一个可断言的目录。这样落盘断言仍是真的（不是对 mock
断言），又不需要真开一个 sshd。

用在两类地方：
- 需要验证产物真的落位的用例（materialization_runner）
- 只需要投递别炸的用例（MagicMock 的 deliver() 什么都不写，落盘断言会变成空转）
"""

from __future__ import annotations

import os
import subprocess

from app.services.dag_delivery import SshDelivery


class LocalTransportDelivery(SshDelivery):
    """把 rsync/ssh 作用在本地文件系统上的 SshDelivery（"远端"= 本地路径）。"""

    def __init__(self, **kw):
        kw.setdefault("host", "test-airflow-host")
        super().__init__(**kw)

    def _run(self, cmd):
        if cmd and cmd[0] == "rsync":
            src, dst = cmd[-2], cmd[-1]
            real = dst.split(":", 1)[1]
            os.makedirs(real, exist_ok=True)
            subprocess.run(["rsync", "-a", src, real], check=True, capture_output=True)
        else:
            subprocess.run(["bash", "-c", cmd[-1]], check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def make_runner_jar(tmp_path, content: bytes = b"\xca\xfe\xba\xbeSQLRUNNER") -> str:
    """造一个假的 SqlRunner jar。

    jar 现在会被**读取并算 sha256**（内容寻址后随包投递），所以配置里的路径必须
    真实存在——此前那些指向 /opt/flink/runner.jar 的 fixture 会直接报文件不存在。
    """
    jar = tmp_path / "sql-runner.jar"
    jar.write_bytes(content)
    return str(jar)
