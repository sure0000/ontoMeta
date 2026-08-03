"""凭据解析：alias → 连接串。

**凭据只有一个归属地**（§3.1）：runner 从自己的 secrets 后端按 alias 解析，ontoMeta 的
DAG/spec/请求体里始终只有 alias。这样 ``POST /probe`` 才有意义，也顺带消掉了「Airflow
Connection 不存在导致渲染期全部任务爆炸」的失败模式 #6。

两种来源，按序：
1. 环境变量 ``SYNC_CONN_<ALIAS>_{URL,USER,PASSWORD}``（``<ALIAS>`` 为别名大写、非字母数字转 _）；
2. 挂载的 secrets 目录 ``$SYNC_SECRETS_DIR/<alias>.json``：``{"url":…, "user":…, "password":…}``。

``URL`` 是完整的 SQLAlchemy URL（如 ``mysql+pymysql://host:3306/erp``）。若 URL 里没带
账号密码而 USER/PASSWORD 另给了，则合并进去——两种写法都支持，不强求部署方二选一。
"""

from __future__ import annotations

import json
import os

from sqlalchemy.engine import URL, make_url


class SecretNotFound(RuntimeError):
    """该 alias 在 runner 侧没配 secret。错误文本给出该设哪个环境变量，便于照做。"""

    def __init__(self, alias: str):
        self.alias = alias
        token = _alias_token(alias)
        super().__init__(
            f"别名「{alias}」在 runner 侧没有配置连接：设环境变量 "
            f"SYNC_CONN_{token}_URL（可选 _USER/_PASSWORD），或在 $SYNC_SECRETS_DIR 放 "
            f"{alias}.json。"
        )


def _alias_token(alias: str) -> str:
    """``erp_readonly`` → ``ERP_READONLY``。与 ontoMeta 侧 ``_alias_token`` 同规则。"""
    return "".join(c if c.isalnum() else "_" for c in (alias or "")).strip("_").upper()


def _merge_credentials(url: URL, user: str | None, password: str | None) -> URL:
    """URL 里没带账号密码时，用另给的补上；URL 已带的以 URL 为准。"""
    if user and not url.username:
        url = url.set(username=user)
    if password and not url.password:
        url = url.set(password=password)
    return url


def _from_env(alias: str) -> URL | None:
    token = _alias_token(alias)
    raw = os.environ.get(f"SYNC_CONN_{token}_URL")
    if not raw:
        return None
    url = make_url(raw)
    return _merge_credentials(
        url,
        os.environ.get(f"SYNC_CONN_{token}_USER"),
        os.environ.get(f"SYNC_CONN_{token}_PASSWORD"),
    )


def _from_dir(alias: str) -> URL | None:
    base = os.environ.get("SYNC_SECRETS_DIR")
    if not base:
        return None
    path = os.path.join(base, f"{alias}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("url")
    if not raw:
        return None
    return _merge_credentials(make_url(raw), data.get("user"), data.get("password"))


def resolve(alias: str) -> URL:
    """别名 → SQLAlchemy URL。找不到即 :class:`SecretNotFound`（不返回 None，调用方不用判空）。"""
    url = _from_env(alias) or _from_dir(alias)
    if url is None:
        raise SecretNotFound(alias)
    return url
