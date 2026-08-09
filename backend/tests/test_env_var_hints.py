"""错误提示里写的环境变量名，必须真的是 Settings 认的名字。

**为什么值得一条测试**：preflight 和各处报错的全部价值就在那句「可照做的下一步」。
提示里写 `SYNC_CHANNEL=docker`，而 `Settings` 的字段叫 `sync_channel`
（`model_config` 只设了 `env_file`，没有 `env_prefix`，环境变量名就是字段名），
照着做完一点效果没有——人会以为自己配错了别的地方，比直接报错更耗时间。

这类错字肉眼审不出来（名字看起来完全合理），但一条测试就能钉死。
"""

from __future__ import annotations

import pathlib
import re

from app.config import Settings

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
# 长得像「我们自己的环境变量」的 token。前缀限定这三个，避免把 SQL 关键字之类扫进来。
_TOKEN = re.compile(r"\b(?:ONTOMETA|SYNC|AIRFLOW)_[A-Z0-9_]{2,}\b")

# 不是 ontoMeta 的 Settings 字段，但出现在提示里是对的——每条都要写清归谁。
_FOREIGN = {
    # Airflow 自己的配置项（双下划线是它的层级分隔符），提示里教用户改 Airflow。
    "AIRFLOW__API__AUTH_BACKENDS",
    "AIRFLOW__WEBSERVER__WEB_SERVER_PORT",
    "AIRFLOW_HOME",
    # runner 侧的环境变量，由 sync_runner/ 定义，不在后端 Settings 里。
    "SYNC_CONN_",
    "SYNC_SECRETS_DIR",
    "SYNC_RUNNER_TOKEN",
}


def _known_env_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_var_hints_match_real_settings_fields():
    known = _known_env_names()
    bad: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in _TOKEN.findall(line):
                if token in known or token in _FOREIGN:
                    continue
                bad.append(f"{path.relative_to(_APP.parent)}:{lineno}  {token}")

    assert not bad, (
        "这些提示里的环境变量名不存在（Settings 无此字段，也不在外部变量白名单里），"
        "用户照做无效：\n  " + "\n  ".join(bad)
    )


def test_the_three_sync_knobs_have_no_ontometa_prefix():
    """钉住这次踩到的具体三个：它们的字段名没有 ontometa_ 前缀，环境变量名也就没有。

    单独列出来，是因为同目录下确实有 `ontometa_dag_parse_timeout` 这类**带**前缀的字段，
    两种命名并存，光看别处很容易顺手写错。
    """
    known = _known_env_names()
    for name in ("SYNC_CHANNEL", "SYNC_RUNNER_ENDPOINT", "SYNC_TOOL_IMAGES"):
        assert name in known
        assert f"ONTOMETA_{name}" not in known
