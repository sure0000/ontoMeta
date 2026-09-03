"""把 SQLite 库里的业务数据整体搬进 Postgres。

背景：`c4dd444 pg迁移` 只把 alembic 脚本改成兼容 Postgres，没有搬数据——切过去之后
库里只有表结构和几行配置，界面上每个页面都是空状态。本脚本补上搬数据这一步。

**两侧都用同一份 SQLAlchemy metadata 读写**，这不是图省事：SQLite 把 bool 存成 0/1、
把 datetime 存成字符串、把 JSON 列存成文本，直接 `INSERT` 到 Postgres 会全线类型报错。
走 Core 的话 SQLite 方言读出来就是 Python 的 `bool` / `datetime` / `dict`，
Postgres 方言再按目标列类型写回去，46 个布尔列、106 个时间列、5 个 JSON 列一个都不用手工转。

**配置表默认不搬**。运行期配置的权威源是目标库自己（见 docs/DEVELOPMENT_PRINCIPLES.md
的「配置只走设置页」），目标库里那几行是切过去之后在设置页配的，往往比源库新
（比如 Airflow 的 dags_dir 指向当前这台机器）。要连配置一起覆盖就加
``--include-settings``——它**只**覆盖配置表那几张，别的表一个不碰。

用法::

    python -m scripts.migrate_sqlite_to_postgres --dry-run      # 只看会搬什么
    python -m scripts.migrate_sqlite_to_postgres                # 搬业务数据
    python -m scripts.migrate_sqlite_to_postgres --include-settings   # 只覆盖配置表
    python -m scripts.migrate_sqlite_to_postgres --truncate-target    # 清空目标库重搬（危险）

``--truncate-target`` 是**全局**的：它会清掉目标库里所有非空的表再从源库重搬，
搬完之后在目标库上做过的任何修改（板块回填、人工判定、新建的对象）全部丢失。
只在「目标库要整体重置成源库的样子」时才用它——补配置用 ``--include-settings`` 就够了。

整个迁移在**一个事务**里完成：中途任何一行插不进去就整体回滚，不会留下半个库。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

from sqlalchemy import String, Table, create_engine, func, insert, select
from sqlalchemy.engine import Engine

from app.config import settings
from app.database import Base

import app.models  # noqa: F401  触发全部模型注册，metadata 才是完整的

#: 运行期配置表：权威源是目标库自己，默认不覆盖。
SETTINGS_TABLES = frozenset(
    {
        "dependency_components",
        "airflow_settings",
        "datahub_settings",
        "llm_service_configs",
        "draft_generation_settings",
        "doris_warehouse_configs",
    }
)

#: alembic 的版本表由迁移自己管，搬过去只会让两边打架。
SKIP_TABLES = frozenset({"alembic_version"})

#: 一次 executemany 的行数。properties 有 4 万行，一次性攒完再发既慢又占内存。
BATCH = 1000


def count_rows(engine: Engine, table: Table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def single_column_fks(table: Table) -> list[tuple]:
    """该表的单列外键：[(本列, 父列)]。复合外键本仓没有，遇到就跳过并告警。"""
    out = []
    for fk in table.foreign_key_constraints:
        cols = list(fk.columns)
        refs = [element.column for element in fk.elements]
        if len(cols) != 1:
            print(f"  ! {table.name} 有复合外键 {[c.name for c in cols]}，孤儿检查跳过")
            continue
        out.append((cols[0], refs[0]))
    return out


def bounded_string_columns(table: Table) -> list:
    """该表带长度上限的字符串列。SQLite 不管 ``VARCHAR(n)``，Postgres 会拒收超长值。"""
    return [c for c in table.columns if isinstance(c.type, String) and c.type.length]


def survey_overlong(source: Engine, tables: list[Table]) -> dict[str, tuple[int, int, int]]:
    """源库里超出列长上限的值，按 `表.列` 计 (行数, 上限, 最长)。"""
    out: dict[str, tuple[int, int, int]] = {}
    with source.connect() as conn:
        for table in tables:
            for col in bounded_string_columns(table):
                lim = col.type.length
                n = conn.execute(
                    select(func.count()).select_from(table).where(func.length(col) > lim)
                ).scalar_one()
                if n:
                    mx = conn.execute(
                        select(func.max(func.length(col))).select_from(table)
                    ).scalar_one()
                    out[f"{table.name}.{col.name}"] = (n, lim, mx)
    return out


def survey_orphans(source: Engine, tables: list[Table]) -> dict[str, int]:
    """源库里指向已不存在父行的行数，按 `子表.列` 计。

    SQLite 默认不开外键约束，删父行不会带走子行，源库因此攒了一堆悬空引用
    （实测 properties 有 16214 行挂在早就没了的 object_type 上）。Postgres 会拒收，
    所以搬之前先把账算清楚——这不是"迁移出错"，是源库本来就脏。
    """
    orphans: dict[str, int] = {}
    with source.connect() as conn:
        for table in tables:
            for col, ref in single_column_fks(table):
                n = conn.execute(
                    select(func.count())
                    .select_from(table)
                    .where(col.isnot(None))
                    .where(~col.in_(select(ref)))
                ).scalar_one()
                if n:
                    orphans[f"{table.name}.{col.name}"] = n
    return orphans


def read_batches(engine: Engine, table: Table) -> Iterator[list[dict]]:
    """按主键顺序分批读源表。顺序确定，失败重跑读到的是同一批。"""
    order = list(table.primary_key.columns) or list(table.columns)[:1]
    with engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(select(table).order_by(*order))
        while chunk := result.fetchmany(BATCH):
            yield [dict(row._mapping) for row in chunk]


def plan(
    source: Engine, target: Engine, *, include_settings: bool, truncate_target: bool
) -> list[tuple]:
    """给出每张表的 (表, 源行数, 目标行数, 动作)。动作决定后面写不写。"""
    rows = []
    for table in Base.metadata.sorted_tables:
        if table.name in SKIP_TABLES:
            continue
        n_src = count_rows(source, table)
        n_dst = count_rows(target, table)
        is_settings = table.name in SETTINGS_TABLES
        if n_src == 0:
            action = "empty"
        elif n_dst == 0:
            # 目标是空的，没什么好护的——配置表也一样，搬过去总比让人对着空设置页强。
            action = "copy"
        elif is_settings and include_settings:
            # --include-settings 只覆盖这几张表，不牵连别的表（别的表要重置得用
            # --truncate-target，那是另一回事）。
            action = "overwrite"
        elif is_settings:
            # 配置表在**目标库已经配过**时让路：那几行是切库之后在设置页填的，
            # 通常比源库新（比如 dags_dir 指向当前这台机器）。
            action = "skip-settings"
        elif truncate_target:
            action = "overwrite"
        else:
            action = "target-not-empty"
        rows.append((table, n_src, n_dst, action))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="ontometa.db", help="源 SQLite 文件（默认 ontometa.db）")
    parser.add_argument("--target", default=None, help="目标库 URL（默认取 settings.database_url）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument(
        "--include-settings",
        action="store_true",
        help="只覆盖运行期配置表（dependency_components 等），别的表不碰",
    )
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="【危险】清空目标库所有非空表再整体重搬：目标库上做过的修改全部丢失",
    )
    args = parser.parse_args()

    target_url = args.target or settings.database_url
    if not target_url.startswith("postgresql"):
        print(f"目标不是 Postgres：{target_url}\n（要搬去别处请显式传 --target）")
        return 1

    source = create_engine(f"sqlite:///{args.source}")
    target = create_engine(target_url)

    # 两边 alembic 版本必须一致，否则列对不上，报错还是静默丢字段都不好收拾。
    with source.connect() as sc, target.connect() as tc:
        from sqlalchemy import text

        src_ver = sc.execute(text("select version_num from alembic_version")).scalar()
        dst_ver = tc.execute(text("select version_num from alembic_version")).scalar()
    if src_ver != dst_ver:
        print(f"alembic 版本不一致：源 {src_ver} / 目标 {dst_ver}")
        print("先把落后的那边 `alembic upgrade head` 之后再搬。")
        return 1

    rows = plan(
        source,
        target,
        include_settings=args.include_settings,
        truncate_target=args.truncate_target,
    )

    print(f"源  : {args.source}")
    print(f"目标: {target_url.split('@')[-1]}")
    print(f"alembic: {src_ver}\n")
    print(f"{'表':<40s} {'源':>7s} {'目标':>6s}  动作")
    blocked = []
    total = 0
    for table, n_src, n_dst, action in rows:
        if action == "empty":
            continue
        note = {
            "copy": "搬",
            "overwrite": "清空后重搬",
            "skip-settings": "跳过（配置表，目标库为准）",
            "target-not-empty": "跳过（目标表非空）",
        }[action]
        print(f"{table.name:<40s} {n_src:>7d} {n_dst:>6d}  {note}")
        if action in ("copy", "overwrite"):
            total += n_src
        if action == "target-not-empty":
            blocked.append(table.name)
    print(f"\n合计待搬 {total} 行")

    if blocked:
        print(f"\n目标表非空、已跳过：{', '.join(blocked)}")
        print("这些表在目标库里已经有数据了。要整体重置成源库的样子得加 --truncate-target，")
        print("但那会丢掉目标库上做过的一切修改（板块回填、人工判定、新建对象）——先想清楚。")

    to_copy = [t for t, _s, _d, a in rows if a in ("copy", "overwrite")]
    orphans = survey_orphans(source, to_copy)
    if orphans:
        print(f"\n源库悬空引用 {sum(orphans.values())} 行（父行已不存在，搬不过去，将丢弃）：")
        for key, n in sorted(orphans.items(), key=lambda kv: -kv[1]):
            print(f"  {key:<50s} {n:>6d}")
        print("  ↑ SQLite 默认不开外键约束，删父行不带走子行，源库因此攒下这些悬空行。")

    overlong = survey_overlong(source, to_copy)
    if overlong:
        n_total = sum(v[0] for v in overlong.values())
        print(f"\n源库超长值 {n_total} 处（Postgres 会按列长拒收，将截断）：")
        for key, (n, lim, mx) in sorted(overlong.items(), key=lambda kv: -kv[1][0]):
            print(f"  {key:<50s} {n:>6d} 行  上限 {lim} / 最长 {mx}")
        print("  ↑ SQLite 不校验 VARCHAR(n)，源库因此存进了超出声明长度的值。")

    if args.dry_run:
        print("\n（dry-run，未写库）")
        return 0

    copied: dict[str, int] = {}
    dropped: dict[str, int] = {}
    truncated: dict[str, int] = {}
    # 一个事务包住全部写入：FK 顺序按 sorted_tables 走，中途出错整体回滚。
    with target.begin() as conn:
        # 反序清空：先删子表再删父表，否则外键挡住。只清 overwrite 的那些表。
        for table, _n_src, n_dst, action in reversed(rows):
            if action == "overwrite" and n_dst:
                conn.execute(table.delete())
        for table, n_src, _n_dst, action in rows:
            if action not in ("copy", "overwrite"):
                continue
            # 父表此刻已经写完（sorted_tables 顺序），直接从**目标库**读父键集合：
            # 这样既盖住刚搬进来的行，也盖住目标库本来就有的（比如没搬的配置表）。
            parents = {
                col.name: (
                    col,
                    {k for (k,) in conn.execute(select(ref).distinct()) if k is not None},
                )
                for col, ref in single_column_fks(table)
            }
            bounded = bounded_string_columns(table)
            written = 0
            for batch in read_batches(source, table):
                keep = []
                for row in batch:
                    for col in bounded:
                        value = row.get(col.name)
                        if isinstance(value, str) and len(value) > col.type.length:
                            row[col.name] = value[: col.type.length]
                            key = f"{table.name}.{col.name}"
                            truncated[key] = truncated.get(key, 0) + 1
                    bad = next(
                        (
                            name
                            for name, (_col, keys) in parents.items()
                            if row.get(name) is not None and row[name] not in keys
                        ),
                        None,
                    )
                    if bad:
                        # 父行不存在：这行在源库里就已经是悬空的，搬过去只会被 Postgres 拒收。
                        dropped[f"{table.name}.{bad}"] = dropped.get(f"{table.name}.{bad}", 0) + 1
                        continue
                    keep.append(row)
                if keep:
                    conn.execute(insert(table), keep)
                    written += len(keep)
                if n_src > 5000:
                    print(f"  {table.name}: {written}/{n_src}", end="\r", flush=True)
            if written:
                copied[table.name] = written
                print(f"  {table.name:<38s} {written:>7d} 行已写入")

    print(f"\n已写入 {sum(copied.values())} 行 / {len(copied)} 张表")
    if dropped:
        print(f"丢弃悬空行 {sum(dropped.values())} 行（父行在源库里就已不存在）：")
        for key, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"  {key:<50s} {n:>6d}")
    if truncated:
        print(f"截断超长值 {sum(truncated.values())} 处（超出列声明长度）：")
        for key, n in sorted(truncated.items(), key=lambda kv: -kv[1]):
            print(f"  {key:<50s} {n:>6d}")

    # 校验：逐表比对行数，别只信写入计数。差额必须正好等于本表丢弃的悬空行数，
    # 对不上说明还有别的东西在丢数据。
    bad = []
    for table, n_src, _n_dst, action in rows:
        if table.name not in copied:
            continue
        n_now = count_rows(target, table)
        expected = n_src - sum(n for k, n in dropped.items() if k.startswith(f"{table.name}."))
        if n_now != expected:
            bad.append(f"{table.name}: 源 {n_src} / 应为 {expected} / 目标 {n_now}")
    if bad:
        print("校验不一致：\n  " + "\n  ".join(bad))
        return 2
    print("校验：逐表行数 = 源行数 − 悬空行数，全部对得上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
