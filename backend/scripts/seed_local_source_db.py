"""按本体的 source_ref 在本机建一份可搬运的源库（开发/联调用）。

背景：本体记的源是 ``urn:li:dataset:(...,mariadb,_3214abce8e7be3d7.tabBrand,PROD)``——
那是采集时的真实 ERP 库。本机没有它时，物化/同步/清洗三类任务的搬运一定失败在
``NoSuchTable``（源别名连得上、库里却一张表都没有），四类任务谁也跑不出非零行。

本脚本照着本体自己的元数据把那份源建出来：库名/表名取自 ``ObjectType.source_ref``，
列名取自 ``Property.source_field_ref``（URN 的 ``#字段`` 段），列类型直接用
``Property.data_type``——这些值本就是从 MariaDB 采回来的原生类型（``VARCHAR(140)``、
``DECIMAL(21, 9)``），不必再映射一次。

**造的是形状，不是业务数据**：值按类型合成，只保证类型合法、``modified`` 单调递增
（增量水位有东西可回读）、并按需插入重复行（去重规则有东西可去）。

不建主键：真实 ERP 源在 DataHub 里就没有 PK/FK（见 real-datahub-shape），照着造才有
意义——目标表的主键由本体反补，那是物化侧的事。

用法::

    cd backend && source .venv/bin/activate
    # 看要建什么，不落库
    python -m scripts.seed_local_source_db --url mysql+pymysql://root@127.0.0.1:3306
    # 只建任务用到的那几张
    python -m scripts.seed_local_source_db --url ... --entities brand,account --apply
    # 全建（734 张表）
    python -m scripts.seed_local_source_db --url ... --all --apply
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.connectors.datahub import _extract_dataset_name
from app.database import SessionLocal
from app.models import ObjectType, Ontology, Property

# 缺省只建这几张：现有 sync/transform/materialize 任务用到的对象。全量建库用 --all。
DEFAULT_ENTITIES = (
    "brand,account,account_category,accounting_period,activity_cost,bank,country,"
    "address,item_variant_attribute,asset_repair_purchase_invoice"
)


def _field_name(source_field_ref: str | None, fallback: str) -> str:
    """``urn:...:(...)#modified`` → ``modified``。取不到就用本体属性名（同名回退）。"""
    if source_field_ref and "#" in source_field_ref:
        tail = source_field_ref.rsplit("#", 1)[-1].strip()
        if tail:
            return tail
    return fallback


def _split_dataset(name: str) -> tuple[str | None, str]:
    """``_3214abce8e7be3d7.tabBrand`` → (库, 表)。无库前缀则库为 None。"""
    if "." in name:
        db, table = name.split(".", 1)
        return db, table
    return None, name


def _sample_value(data_type: str, row: int, base_time: datetime, semantic: str | None = None) -> object:
    """按列类型合成一个值。够用即可——这里造的是形状，不是业务数据。

    ``semantic`` 也要看：目标列的类型由本体的**语义类型**决定（``flag`` → BOOLEAN），
    源列却常是 TEXT。只按源的物理类型造值，会造出一列搬不进目标表的数据。
    """
    t = (data_type or "").upper()
    # 语义类型决定目标列类型（flag→BOOLEAN、datetime→TIMESTAMP、amount→DECIMAL），
    # 故文本源列也得按语义造值——否则造出来的是一列搬不进目标表的数据。
    # 本体里这类「语义与物理矛盾」的属性有 495 个（见 scripts/backfill_semantic_types.py）。
    st = (semantic or "").lower()
    if st == "flag":
        return str(row % 2)
    if st == "datetime" and not t.startswith(("DATETIME", "TIMESTAMP", "DATE", "TIME")):
        return (base_time + timedelta(minutes=row)).isoformat(sep=" ")
    if st == "amount" and not t.startswith(("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT")):
        return str(round(random.uniform(1, 10000), 2))
    if t.startswith(("DATETIME", "TIMESTAMP")):
        # 单调递增：增量搬运回读 max(modified) 当水位，值不递增就永远搬不动。
        return base_time + timedelta(minutes=row)
    if t.startswith("DATE"):
        return (base_time + timedelta(days=row)).date()
    if t.startswith("TIME"):
        return (base_time + timedelta(seconds=row)).time()
    if t.startswith(("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT")):
        return round(random.uniform(1, 10000), 2)
    if t.startswith(("TINYINT", "SMALLINT", "INT", "BIGINT")):
        return row % 2 if t.startswith("TINYINT") else row
    return f"v{row}"


def _table_ddl(db_name: str, table: str, columns: list[tuple[str, str, str | None]]) -> str:
    cols = ",\n  ".join(f"`{name}` {dtype}" for name, dtype, _ in columns)
    return (
        f"CREATE TABLE IF NOT EXISTS `{db_name}`.`{table}` (\n  {cols}\n) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def _collect(db, ontology_id: str, entities: set[str] | None) -> dict:
    """本体 → {(源库, 源表): [(列名, 类型)]}，按 source_ref/source_field_ref 还原。"""
    objects = (
        db.query(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    )
    props_by_obj: dict[str, list[Property]] = defaultdict(list)
    for prop in (
        db.query(Property)
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    ):
        props_by_obj[prop.object_type_id].append(prop)

    tables: dict[tuple[str | None, str], list[tuple[str, str, str | None]]] = {}
    for obj in objects:
        if not obj.source_ref:
            continue
        if entities is not None and obj.name not in entities:
            continue
        source_db, source_table = _split_dataset(_extract_dataset_name(obj.source_ref))
        columns: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()
        for prop in props_by_obj.get(obj.id, ()):
            name = _field_name(prop.source_field_ref, prop.name)
            if name in seen:
                continue
            seen.add(name)
            columns.append((name, prop.data_type or "VARCHAR(255)", prop.semantic_type))
        if columns:
            tables[(source_db, source_table)] = columns
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="源库连接串（不带库名），如 mysql+pymysql://root@127.0.0.1:3306")
    parser.add_argument("--ontology-id", help="缺省取最新一个有 source_ref 的本体")
    parser.add_argument("--entities", default=DEFAULT_ENTITIES, help="逗号分隔的对象技术名")
    parser.add_argument("--all", action="store_true", help="建本体里全部有 source_ref 的对象")
    parser.add_argument("--rows", type=int, default=20, help="每表插入行数")
    parser.add_argument("--dup-rows", type=int, default=2, help="额外插入的重复行数（供去重规则验证）")
    parser.add_argument("--apply", action="store_true", help="真的建库建表；缺省只打印")
    args = parser.parse_args()

    entities = None if args.all else {e.strip() for e in args.entities.split(",") if e.strip()}

    with SessionLocal() as db:
        ontology_id = args.ontology_id
        if not ontology_id:
            row = (
                db.query(ObjectType.ontology_id)
                .filter(ObjectType.source_ref.isnot(None))
                .join(Ontology, Ontology.id == ObjectType.ontology_id)
                .order_by(Ontology.created_at.desc())
                .first()
            )
            if row is None:
                raise SystemExit("库里没有带 source_ref 的本体，无从还原源库")
            ontology_id = row[0]
        tables = _collect(db, ontology_id, entities)

    if not tables:
        raise SystemExit("没有匹配到任何对象（检查 --entities / --ontology-id）")

    databases = {db_name for db_name, _ in tables if db_name}
    total_cols = sum(len(c) for c in tables.values())
    print(f"本体 {ontology_id}")
    print(f"将建 {len(databases)} 个库、{len(tables)} 张表、{total_cols} 个列")
    for (db_name, table), columns in sorted(tables.items(), key=lambda kv: str(kv[0])):
        print(f"  {db_name}.{table}（{len(columns)} 列）")
    if not args.apply:
        print("\n（dry-run；加 --apply 才真的建）")
        return

    engine = create_engine(make_url(args.url))
    base_time = datetime(2024, 1, 1)
    created = rows_inserted = 0
    with engine.begin() as conn:
        for db_name in databases:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARSET utf8mb4"))
        for (db_name, table), columns in tables.items():
            if not db_name:
                continue
            conn.execute(text(_table_ddl(db_name, table, columns)))
            created += 1
            existing = conn.execute(
                text(f"SELECT COUNT(*) FROM `{db_name}`.`{table}`")
            ).scalar()
            if existing:
                continue  # 幂等：已有数据就不再灌，反复跑不会翻倍
            col_names = ", ".join(f"`{c}`" for c, _, _ in columns)
            placeholders = ", ".join(f":{i}" for i, _ in enumerate(columns))
            insert = text(
                f"INSERT INTO `{db_name}`.`{table}` ({col_names}) VALUES ({placeholders})"
            )
            payload = [
                {
                    str(i): _sample_value(dtype, r, base_time, semantic)
                    for i, (_, dtype, semantic) in enumerate(columns)
                }
                for r in range(args.rows)
            ]
            # 重复行：整行照抄前几行——去重规则要有东西可去，否则「已去重」验不出真假。
            payload += [dict(payload[r]) for r in range(min(args.dup_rows, len(payload)))]
            conn.execute(insert, payload)
            rows_inserted += len(payload)
    print(f"\n已建 {created} 张表，插入 {rows_inserted} 行（其中每表 {args.dup_rows} 行是重复行）")
    print("下一步：把 runner 的源别名指到这个库（口令别落进终端历史，用变量代入）")
    # 口令不回显：连接串在这里只以脱敏形式出现。
    safe = make_url(args.url).render_as_string(hide_password=True)
    print(f"  库：{safe}/{sorted(databases)[0]}")
    print("  curl -X PUT $RUNNER/secrets/erp_readonly -H 'Content-Type: application/json' \\")
    print('       -d "{\\"url\\": \\"$SOURCE_URL\\"}"')


if __name__ == "__main__":
    main()
