"""凭据解析：alias → 连接串。

**凭据只有一个归属地**（§3.1）：runner 从自己的 secrets 后端按 alias 解析，ontoMeta 的
DAG/spec/请求体里始终只有 alias。这样 ``POST /probe`` 才有意义，也顺带消掉了「Airflow
Connection 不存在导致渲染期全部任务爆炸」的失败模式 #6。

两种来源，**按此优先级**：

1. 环境变量 ``SYNC_CONN_<ALIAS>_{URL,USER,PASSWORD,…}``（``<ALIAS>`` 为别名大写、非字母数字转 _）；
2. secrets 目录 ``$SYNC_SECRETS_DIR/<alias>.json``：``{"url":…, "user":…, "password":…}``。

环境变量优先，且**目录里的同名别名不允许覆盖它**（写入时直接拒绝，见 :func:`save`）：
环境变量是部署方在容器启动时钉死的，UI 上一次静默覆盖会让人对着一个不生效的值排查。

目录这一份可由 ontoMeta 的设置页经 runner 的 secrets 接口写入——**凭据仍只有一个归属地**：
值落在 runner 自己的存储里，ontoMeta 只是代填，不留副本、不回读明文。

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
    """URL 里没带账号密码时，用另给的补上；URL 已带的以 URL 为准。

    **无主机的 URL（sqlite 这类文件库）不合并**：往里塞账号密码会得到
    ``sqlite://:pw@/path`` 这种 SQLAlchemy 直接拒绝解析的形式，而错误文本
    （"Invalid SQLite URL"）指向 URL 本身，完全看不出是被另一项配置搞坏的。
    """
    if not url.host:
        return url
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


def resolve_options(alias: str) -> dict[str, str]:
    """别名 → 该连接的**全部**配置项（小写键）。

    ``resolve()`` 只给 SQLAlchemy URL，够 native 档用；seatunnel 档还需要一些没法从 URL
    推出来的东西——Hive 的 ``metastore_uri``、私有部署要覆盖的 ``jdbc_url``。这些同样按
    别名走同一套 secrets 来源，不另开配置口子：

    - 环境变量 ``SYNC_CONN_<ALIAS>_<KEY>``（``<KEY>`` 转小写作键）；
    - ``$SYNC_SECRETS_DIR/<alias>.json`` 里的所有键。

    找不到该别名返回空 dict——调用方按需判断，缺哪一项报哪一项，比在这里统一报错好定位。
    """
    token = _alias_token(alias)
    options: dict[str, str] = {}

    base = os.environ.get("SYNC_SECRETS_DIR")
    if base:
        path = os.path.join(base, f"{alias}.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                options.update({str(k).lower(): str(v) for k, v in json.load(fh).items()})

    # 环境变量优先于文件：部署时临时覆盖一项不必改挂载的文件。
    prefix = f"SYNC_CONN_{token}_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            options[key[len(prefix):].lower()] = value
    return options


# ---------- secrets 目录的读写（供 runner 的 /secrets 接口用） ----------

# 值里这些键当作机密：列出时只报「已设」，绝不回明文。
SECRET_KEYS = frozenset({"password", "secret", "token"})

# 环境变量 SYNC_CONN_<别名>_<字段> 里认得的字段名，**按长度倒序**匹配
# （METASTORE_URI 必须先于 URI 命中，否则别名会被切多一截）。
_ENV_FIELD_SUFFIXES = tuple(
    sorted(
        ("URL", "USER", "PASSWORD", "TOKEN", "SECRET", "METASTORE_URI", "JDBC_URL", "HADOOP_USER"),
        key=len,
        reverse=True,
    )
)


class SecretStoreUnavailable(RuntimeError):
    """没配 SYNC_SECRETS_DIR，写不了。错误文本给出该怎么配。"""

    def __init__(self) -> None:
        super().__init__(
            "runner 未配置 secrets 存储：设环境变量 SYNC_SECRETS_DIR 指向一个可写目录"
            "（容器部署时挂成卷，否则重启即丢）。"
        )


class SecretIsEnvManaged(RuntimeError):
    """该别名由环境变量提供，不接受写入。

    静默让文件覆盖环境变量会更糟：环境变量优先级更高，UI 上「保存成功」而实际生效的
    还是旧值，排查时无从下手。
    """

    def __init__(self, alias: str):
        self.alias = alias
        super().__init__(
            f"别名「{alias}」由环境变量提供（SYNC_CONN_{_alias_token(alias)}_*），"
            "不能从接口改；要改请改部署的环境变量，或先移除它再用接口管理。"
        )


def _store_dir() -> str:
    base = os.environ.get("SYNC_SECRETS_DIR")
    if not base:
        raise SecretStoreUnavailable()
    return base


def _store_path(alias: str) -> str:
    return os.path.join(_store_dir(), f"{alias}.json")


def is_env_managed(alias: str) -> bool:
    return bool(os.environ.get(f"SYNC_CONN_{_alias_token(alias)}_URL")) or any(
        k.startswith(f"SYNC_CONN_{_alias_token(alias)}_") for k in os.environ
    )


def save(alias: str, values: dict[str, str]) -> None:
    """写入/覆盖一个别名的连接配置。空值的键会被删掉（便于「清掉某一项」）。"""
    if is_env_managed(alias):
        raise SecretIsEnvManaged(alias)
    path = _store_path(alias)
    os.makedirs(_store_dir(), exist_ok=True)
    current: dict[str, str] = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
    for key, value in values.items():
        key = key.strip().lower()
        if not key:
            continue
        if value is None or value == "":
            current.pop(key, None)
        else:
            current[key] = value
    # 先写临时文件再改名：写到一半崩溃不会留下半个 JSON 让解析炸掉。
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(current, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)  # 明文落盘，至少不让同机其他用户读到
    os.replace(tmp, path)


def delete(alias: str) -> bool:
    """删掉一个别名的配置。返回是否真的删了（不存在不算错）。"""
    if is_env_managed(alias):
        raise SecretIsEnvManaged(alias)
    try:
        os.remove(_store_path(alias))
        return True
    except FileNotFoundError:
        return False


def describe() -> list[dict]:
    """列出已配的别名：**只报哪些键已设，不回任何值**。

    机密键（password 等）连长度都不给；非机密键（url/metastore_uri）回明文，
    因为排查「连到哪去了」时看不到地址等于没法查，而这些本就不是秘密。
    """
    out: dict[str, dict] = {}

    base = os.environ.get("SYNC_SECRETS_DIR")
    if base and os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json"):
                continue
            alias = name[: -len(".json")]
            try:
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            out[alias] = {"alias": alias, "source": "store", "values": _redact(data)}

    # 环境变量提供的别名也要列出来，否则 UI 上「没有这个别名」与「有但改不了」分不清。
    prefix = "SYNC_CONN_"
    env_aliases: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        # **按已知字段后缀切，不能按最后一个下划线切**：别名和字段名里都可能有下划线，
        # 按最后一个下划线切会把 SYNC_CONN_ONTOMETA_DS_HIVE_DW_METASTORE_URI 拆成
        # 别名 ontometa_ds_hive_dw_metastore + 字段 uri——别名是错的，UI 上对不上号。
        # 取最长匹配：METASTORE_URI 要先于 URI 被匹配到。
        field = next(
            (f for f in _ENV_FIELD_SUFFIXES if rest.endswith(f"_{f}")), None
        )
        if field is None:
            continue
        alias = rest[: -len(field) - 1]
        if alias:
            env_aliases.setdefault(alias.lower(), {})[field.lower()] = value
    for alias, data in env_aliases.items():
        out[alias] = {"alias": alias, "source": "env", "values": _redact(data)}

    return [out[a] for a in sorted(out)]


def _redact(data: dict) -> dict[str, str]:
    return {key: _redact_value(key, value) for key, value in sorted(data.items())}


def _redact_value(key: str, value) -> str:
    """一个值该怎么回给调用方。

    键名是机密的（password/secret/token）→ 只报「已设置」。
    其余回明文**但要先摘掉 URL 里内嵌的密码**：连接串写成
    ``mysql+pymysql://root:pw@host/db`` 是最常见的写法，只看键名的话这一整串会被当作
    「非机密」原样回出去，密码就跟着漏进 UI 了。
    """
    if key.lower() in SECRET_KEYS:
        return "<已设置>"
    text = str(value)
    if "://" not in text:
        return text
    try:
        return make_url(text).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — 解析不了的串宁可整体隐掉，也不赌它没带密码
        return "<已设置>"
