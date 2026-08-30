"""错误提示里写的环境变量名，必须真的是 Settings 认的名字。

**为什么值得一条测试**：preflight 和各处报错的全部价值就在那句「可照做的下一步」。
提示里的 ontoMeta 环境变量必须与 `Settings` 字段保持一致。
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
    "AIRFLOW__CORE__DAGS_FOLDER",
    "AIRFLOW_HOME",
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
