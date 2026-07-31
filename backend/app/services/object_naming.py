"""对象标识名去碰撞。

同一本体内，两张**不同**的源表可能被命名管线（LLM 去技术前后缀，或确定性
回退）压成同一个 ``name``——例如 Frappe 文档表 ``tabProcess Period Closing
Voucher`` 与 ``tabPeriod Closing Voucher`` 都被压成 ``period_closing_voucher``。
这不是「同一对象被复制」，而是两个不同对象撞了标识名，发布期会被
``validate_ontology`` 判为「对象标识重复」。

修法：撞名组的每个成员改用**源表名的 snake**（天然唯一且可读），非撞名的
名字原样保留。纯函数、不依赖 ORM，便于生成端（A）、合并端（B）与存量脚本复用。
"""

from __future__ import annotations

import re
from collections import defaultdict

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")
# urn:li:dataset:(urn:li:dataPlatform:<platform>,<schema>.<table>,<env>)
# 取括号内以逗号分隔的中间字段（库表路径），末段为表名。
_URN_TABLE = re.compile(r"\(.*?,(?P<path>.+),[^,()]+\)\s*$")


def _snake(value: str) -> str:
    return _NON_ALNUM.sub("_", value).strip("_").lower()


def table_name_from_ref(source_ref: str | None) -> str:
    """从 source_ref（DataHub dataset urn 或裸表名）解析出源表名的 snake 标识。

    - urn 形态取括号内库表路径的末段（最后一个 ``.`` 之后）。
    - Frappe 文档表命名 ``tab<DocType>``：去掉 ``tab`` 前缀（仅当其后紧跟大写字母，
      避免误伤 ``tabular`` / ``table_*`` 等真实表名）。
    - 非 urn / 解析失败时对整串 snake，保证确定性、非空即可用于消歧。
    """
    if not source_ref:
        return ""
    match = _URN_TABLE.search(source_ref)
    path = match.group("path") if match else source_ref
    table = path.rsplit(".", 1)[-1].strip()
    if table.startswith("tab") and len(table) > 3 and table[3].isupper():
        table = table[3:]
    return _snake(table)


def dedupe_object_names(
    entries: list[tuple[str, str, str | None]],
) -> dict[str, str]:
    """把 (key, name, source_ref) 列表去碰撞，返回 ``key -> 唯一 name``。

    ``entries`` 需按稳定顺序传入（生成/入库顺序）。返回对**每个** key 都给出映射：
    非碰撞名原样返回；碰撞组的每个成员改用 :func:`table_name_from_ref` 得到的源表
    名 snake，仍撞则追加数字后缀。先占用所有非碰撞名，确保碰撞组消歧不会反过来
    撞上无辜的单例名。
    """
    groups: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for key, name, ref in entries:
        groups[name].append((key, name, ref))

    result: dict[str, str] = {}
    used: set[str] = set()
    # 第一遍：占用所有唯一（非碰撞）名。
    for name, members in groups.items():
        if len(members) == 1:
            used.add(name)
            result[members[0][0]] = name
    # 第二遍：碰撞组逐成员消歧。
    for name, members in groups.items():
        if len(members) == 1:
            continue
        for key, _name, ref in members:
            base = table_name_from_ref(ref) or name
            final = base
            suffix = 2
            while final in used:
                final = f"{base}_{suffix}"
                suffix += 1
            result[key] = final
            used.add(final)
    return result
