"""ontometa-sync-runner：常驻搬运服务（M14）。

**为什么是独立服务**：见 `MATERIALIZE_SYNC_STABILITY.md` §3.1。把「一次搬运要同时成立九件事」
压到三件的关键，是让 ontoMeta 在**点提交之前**能直接问执行侧「连不连得上源库」——只有一个
可被询问的常驻服务能回答，DockerOperator 那种一次性兄弟容器回答不了。

**刻意与 backend/app 解耦**：本包不 import ontoMeta 后端任何东西，自带版本化线格式
（``contract.py``）与自己的镜像。凭据只有一个归属地——runner 按 alias 从自己的 secrets 解析，
请求体里永远只有 alias（``secrets.py``）。
"""

from sync_runner.contract import CONTRACT_VERSION

__all__ = ["CONTRACT_VERSION"]
