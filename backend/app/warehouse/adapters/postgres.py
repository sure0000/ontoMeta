"""PostgreSQL Adapter。

Postgres 是标准 SQL 引擎（非 OLAP 数仓），与已有的 Hive/Doris 等有几处关键差异，
正是引擎逻辑必须下沉到 Adapter 的原因：

1. **ANSI 双引号引用标识符**——不是 Hive/Doris 的反引号。但只覆写 ``quote_identifier``
   **不足以**让基类的 staging/swap 成立：postgres 的建表复制是 ``(LIKE 原表 INCLUDING ALL)``
   而非 MySQL/Hive 的 ``LIKE 原表``，改名也只接受**裸名**（``ALTER TABLE a.b RENAME TO c``，
   新名不带库前缀，跨 schema 改名不支持）。两者都已在此覆写——这条差异是真实跑一次
   物化才暴露的：此前 M15 的 staging 只有 golden 断言、从未接到 DAG 运行时。
2. **真主键 / 真外键**——postgres 能强制约束（``ENFORCED``），建表内联 ``PRIMARY KEY(...)``
   与 ``FOREIGN KEY(...) REFERENCES ...``，不像 Hive 只能写进 TBLPROPERTIES 声明式记录。
3. **无表模型 / 无分桶**——没有 Doris 的 Unique/Duplicate Key 概念，也没有 ``DISTRIBUTED BY``。
4. **TIMESTAMP 原生**——不像 Doris 要降级成 DATETIME。

分区：PoC 阶段**不建 postgres 原生声明式分区**（``PARTITION BY RANGE`` 要预建子分区，
本轮不铺开），分区键仅作**普通列**保留在表里。但能力矩阵仍声明 ``supports_partition=True``
——因为 postgres **能**承载分区键（作为可查询的普通列），声明 False 会让任何带分区键的
表在 ``guard()`` 时被判 error 而无法物化到 postgres，那是错的。差别只在"不建原生分区
子表"，属实现深度而非能力缺失。

能力矩阵按 PostgreSQL 16 文档写，但**未在真实实例逐项核实**，故 ``verified=False``：
按项目约定它会产生 warning 级缺口，提醒实施前验证（与 iceberg 等未核实引擎一致）。
"""

from __future__ import annotations

from app.warehouse.adapters.base import DialectAdapter, base_type
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="postgres",
    # postgres 强制主键/外键（真约束，非声明式）。
    primary_key=ConstraintSupport.ENFORCED,
    foreign_key=ConstraintSupport.ENFORCED,
    # 无 OLAP Key 模型概念。
    primary_key_model="none",
    supports_table_comment=True,  # COMMENT ON TABLE
    supports_column_comment=True,  # COMMENT ON COLUMN
    # 承载分区键（作普通列）；PoC 不建原生子分区，故 render 时分区键仅留在列清单。
    supports_partition=True,
    supports_bucketing=False,
    # postgres 无 MERGE 的 SCD2 语义保证（15 起有 MERGE，但 PoC 不承诺 SCD2）。
    supports_scd2_merge=False,
    supports_alter_add_column=True,
    supports_alter_drop_column=True,
    supports_alter_rename_column=True,
    # postgres 标识符硬上限 63 字节。
    max_identifier_length=63,
    verified=False,
)


def _q(name: str) -> str:
    """ANSI 双引号引用。内部双引号按 SQL 规则翻倍转义。"""
    return '"' + name.replace('"', '""') + '"'


def _c(text: str) -> str:
    """postgres 字符串字面量：单引号包裹，内部单引号翻倍。"""
    return "'" + text.replace("'", "''") + "'"


class PostgresAdapter(DialectAdapter):
    name = "postgres"

    def capabilities(self) -> Capabilities:
        return _CAPS

    def quote_identifier(self, name: str) -> str:
        return _q(name)

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → postgres 类型。判定顺序与其他 Adapter 一致（语义优先于物理类型）。

        postgres 有原生 TIMESTAMP（不像 Doris 要降级 DATETIME）；无长度元数据的泛字符串
        落 TEXT——postgres 的 TEXT 与 VARCHAR 无性能差异，且不必臆造一个长度上限。
        """
        # 去参数再判：INTEGER(11) / DECIMAL(21, 9) 这类原样类型精确查表命中不了，
        # 会被整体误判成文本列（见 base.base_type）。
        dt = base_type(data_type)
        st = (semantic_type or "").lower()
        if st == "date" or dt == "date":
            return "DATE"
        if st in {"datetime", "time"} or "time" in dt or "date" in dt:
            return "TIMESTAMP"
        if st == "amount" or dt in {"decimal", "numeric", "money"}:
            return "NUMERIC(18,4)"
        if st in {"number", "count"} or dt in {"int", "integer", "smallint"}:
            return "INTEGER"
        if dt in {"bigint", "long"}:
            return "BIGINT"
        if dt in {"float", "double", "real"}:
            return "DOUBLE PRECISION"
        if st == "flag" or dt in {"bool", "boolean"}:
            return "BOOLEAN"
        return "TEXT"

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_q(table.database)}.{_q(table.name)}"
        return _q(table.name)

    def _render_column(self, col: LogicalColumn) -> str:
        line = f"  {_q(col.name)} {self.map_type(col.data_type, col.semantic_type)}"
        if not col.nullable:
            line += " NOT NULL"
        return line

    def render_create_table(self, table: LogicalTable) -> str:
        self.guard(table)
        # 分层落库在 postgres 上是 **schema**（`"dim"."account"`），不是 database——一个连接
        # 跨不了库。而 schema 不会随建表自动出现：不先建它，整批 DDL 会齐刷刷报
        # `schema "dim" does not exist`。其它引擎（hive/doris）的 database 由目标仓预建或
        # 建表时自动带出，故这一句只属于本 Adapter。
        stmts: list[str] = []
        if table.database:
            stmts.append(f"CREATE SCHEMA IF NOT EXISTS {_q(table.database)};")

        # postgres 分区键留在列清单里（PoC 不建原生分区，仅作普通列）。
        lines = [f"CREATE TABLE IF NOT EXISTS {self._qualified(table)} ("]
        body = [self._render_column(c) for c in table.columns]

        pk = table.primary_key
        if pk and pk.columns:
            body.append(f"  PRIMARY KEY ({', '.join(_q(c) for c in pk.columns)})")
        # 外键**不内联**：见 render_foreign_keys——内联会让建表依赖被引用表已存在，
        # 从而「只物化一部分」和「本体里有环」两种情况都建不出来。

        lines.append(",\n".join(body))
        lines.append(");")
        stmts.append("\n".join(lines))

        # 表 / 列注释：postgres 走独立的 COMMENT ON 语句（不能内联进 CREATE）。
        if table.comment:
            stmts.append(
                f"COMMENT ON TABLE {self._qualified(table)} IS {_c(table.comment)};"
            )
        for col in table.columns:
            if col.comment:
                stmts.append(
                    f"COMMENT ON COLUMN {self._qualified(table)}.{_q(col.name)} "
                    f"IS {_c(col.comment)};"
                )
        return "\n".join(stmts)

    def render_foreign_keys(self, table: LogicalTable) -> list[str]:
        """建表之后再补外键，一条约束一句 ALTER。

        **幂等**：postgres 没有 ``ADD CONSTRAINT IF NOT EXISTS``，故套一层 DO 块先查
        ``pg_constraint``。物化会重跑，第二次直接报 42710（约束已存在）会让整个
        create/constraints 任务红掉，而它其实什么都不必做。

        约束名由「表名 + 列名」派生并截到 63 字节（postgres 标识符上限），保证同一张表
        重跑得到同一个名字——否则每跑一次就多出一条同义约束。
        """
        self.guard(table)
        out: list[str] = []
        qualified = self._qualified(table)
        for fk in table.foreign_keys:
            if not fk.columns or not fk.ref_table:
                continue
            cols = ", ".join(_q(c) for c in fk.columns)
            ref_cols = (
                f" ({', '.join(_q(c) for c in fk.ref_columns)})" if fk.ref_columns else ""
            )
            # ref_table 形如 "库.表" 或 "表"：逐段引用，不整体当一个标识符。
            ref_tbl = ".".join(_q(p) for p in fk.ref_table.split("."))
            name = f"fk_{table.name}_{'_'.join(fk.columns)}"[:63]
            out.append(
                "DO $$ BEGIN\n"
                f"  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN\n"
                f"    ALTER TABLE {qualified} ADD CONSTRAINT {_q(name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {ref_tbl}{ref_cols};\n"
                "  END IF;\n"
                "END $$;"
            )
        return out

    def render_load(self, target: str, select_body: str, *, overwrite: bool) -> str:
        """postgres 没有 ``INSERT OVERWRITE``：覆盖装载 = ``TRUNCATE`` + ``INSERT``。

        两句而不是「DELETE 再插」：TRUNCATE 不逐行写 WAL，整表替换的语义也更贴切。
        postgres 的 DDL/DML 都在事务里，两句放同一事务即为原子（DAG 逐句执行时
        与其他引擎同为短暂窗口，且真正的兜底是 staging+swap，见 render_swap）。

        追加装载也去掉 ``TABLE`` 关键字——那是 Hive 的写法，标准 SQL 里是语法错误。
        """
        insert = f"INSERT INTO {target}\n{select_body};"
        return f"TRUNCATE TABLE {target};\n{insert}" if overwrite else insert

    def render_create_staging(self, table: LogicalTable, run_id: str) -> str:
        """postgres 的建表复制是 ``(LIKE 原表 INCLUDING ALL)``，不是 MySQL/Hive 的 ``LIKE 原表``。

        ``INCLUDING ALL`` 连默认值/约束/索引一起复制——staging 搬完要改名顶替正式表，
        少复制什么，切换之后正式表就少什么。
        """
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return f"CREATE TABLE IF NOT EXISTS {stg} (LIKE {orig} INCLUDING ALL);"

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """rename 两步切换。**新名必须是裸名**：postgres 的 ``RENAME TO`` 不接受库前缀
        （``ALTER TABLE a.b RENAME TO a.c`` 直接语法报错），改名也不能跨 schema。

        postgres 的 DDL 在事务里，故这几句放进同一事务即为真原子；DAG 逐句执行时
        与基类同为「短暂窗口」，不比它更差。
        """
        run = self._sanitize_run(run_id)
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        old_qualified = self._qual(table.database, f"{table.name}__old_{run}")
        return [
            f"DROP TABLE IF EXISTS {old_qualified};",
            f"ALTER TABLE {orig} RENAME TO {_q(f'{table.name}__old_{run}')};",
            f"ALTER TABLE {stg} RENAME TO {_q(table.name)};",
            f"DROP TABLE IF EXISTS {old_qualified};",
        ]

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER。postgres 支持逐列增/删/改类型。"""
        self.guard(after)
        stmts: list[str] = []
        qualified = self._qualified(after)
        before_names = {c.name for c in before.columns}
        after_names = {c.name for c in after.columns}

        for col in after.columns:
            if col.name not in before_names:
                coldef = self._render_column(col).strip()
                stmts.append(f"ALTER TABLE {qualified} ADD COLUMN {coldef};")
        for name in sorted(before_names - after_names):
            stmts.append(f"ALTER TABLE {qualified} DROP COLUMN {_q(name)};")
        for col in after.columns:
            old = before.column(col.name)
            if old is None:
                continue
            new_type = self.map_type(col.data_type, col.semantic_type)
            old_type = self.map_type(old.data_type, old.semantic_type)
            if new_type != old_type:
                stmts.append(
                    f"ALTER TABLE {qualified} ALTER COLUMN {_q(col.name)} "
                    f"TYPE {new_type};"
                )
            if (col.comment or "") != (old.comment or ""):
                comment = _c(col.comment) if col.comment else "NULL"
                stmts.append(
                    f"COMMENT ON COLUMN {qualified}.{_q(col.name)} IS {comment};"
                )
        return stmts
