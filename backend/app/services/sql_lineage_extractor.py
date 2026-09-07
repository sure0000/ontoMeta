"""从 SQL 文本推两样东西：**血缘**（上游表 → 落点表）与**关联**（等值谓词、DDL 外键）。

两者语义不同，分开产出、分开消费：血缘是数据流动（PROV-O 的 wasDerivedFrom），
关联是业务事实（外键）。一个只有建表语句的包**不产生任何血缘**（没有数据流动），
但可以产出满满一把外键关联——这正是 DDL 包过去产出为 0 的原因。


**为什么是通用解析而不是复用 flink_sql_lineage**：后者只认本项目生成器产出的那一种
形态（正则匹配固定模板），客户的代码包是野生的——目录随意、方言混杂、写法各异。
这里用 sqlglot 走 AST，认的是语法不是模板。

三条口径，都是为了**不把推断当事实**（本仓踩过：推断当事实落成硬约束，表装不下自己
的源数据）：

1. **只有落点明确的语句才产血缘**：INSERT INTO / CREATE TABLE AS / CREATE VIEW。
   裸 SELECT、UPDATE 没有落点，不猜——它们只是"没有可推的落点"，不是解析失败。
2. **关联键解析不出来就不给**：列引用要能经别名（或 CTE 的投影）落到真实的表.列，
   否则这条键直接丢。宁可只给表级边，也不给一条编出来的键——键会被下游当 FK 证据用。
3. **CTE 不是表**：CTE 名不进上游表清单，它内部引用的真实表才进；引用 CTE 别名的
   关联键往里追一层投影（``p.party AS customer`` → ``customer`` 即 ``tabPayment Entry.party``），
   追不到就丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError

#: 解析失败时给人看的原因。野生代码包里这几类一定有，摊开讲比藏起来有用。
STORED_PROCEDURE_HINT = "存储过程语法（DELIMITER）超出解析范围"
EMPTY_FILE_HINT = "文件没有可解析的语句"
NO_LANDING_HINT = "没有可推的落点（无 INSERT / CREATE TABLE AS / CREATE VIEW）"


@dataclass(frozen=True)
class JoinKey:
    """一对关联键：``left_table.left_column = right_table.right_column``。"""

    left_table: str
    left_column: str
    right_table: str
    right_column: str

    def render(self) -> str:
        return (
            f"{self.left_table}.{self.left_column} = {self.right_table}.{self.right_column}"
        )


@dataclass(frozen=True)
class StatementLineage:
    """一条语句推出的血缘：一个落点 + 若干上游 + 若干关联键。"""

    target: str
    sources: tuple[str, ...]
    join_keys: tuple[JoinKey, ...]


@dataclass(frozen=True)
class DeclaredForeignKey:
    """DDL 里**声明**的外键：``table.column → target_table.target_column``。

    **这是关联关系，不是血缘**：``FOREIGN KEY (customer_id) REFERENCES customers(id)``
    说的是「订单引用客户」，不是「客户的数据加工成了订单」。所以它不产出血缘边，
    只产出关联证据（见 relation_structure 的 foreign_key vs derivation）。

    证据强度是全场最高的一档——库自己声明的参照完整性，比等值谓词（可能只是一次性
    报表拼接）和命名推断都硬。
    """

    table: str
    column: str
    target_table: str
    target_column: str

    def render(self) -> str:
        return (
            f"{self.table}.{self.column} = {self.target_table}.{self.target_column}"
        )


@dataclass
class SqlParseResult:
    """一个 .sql 文件的解析结果。``error`` 非空表示整份文件没解析成功。"""

    statements: int = 0
    lineages: list[StatementLineage] = field(default_factory=list)
    #: DDL 里声明的外键。与 ``lineages`` 完全独立——一个只有建表语句的包，
    #: ``lineages`` 必然为空（没有数据流动），但外键可以满满一把。
    foreign_keys: list[DeclaredForeignKey] = field(default_factory=list)
    error: str | None = None


def _table_name(table: exp.Table) -> str:
    """``catalog.db.table``，缺哪段跳哪段。反引号/引号由 sqlglot 剥掉。"""
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts)


def _target_table(statement: exp.Expression) -> exp.Table | None:
    """取语句的落点表。没有落点的语句（SELECT/UPDATE/DELETE）返回 None。"""
    if isinstance(statement, exp.Insert):
        node = statement.this
    elif isinstance(statement, exp.Create):
        if (statement.kind or "").upper() not in {"TABLE", "VIEW"}:
            return None
        node = statement.this
    else:
        return None

    if isinstance(node, exp.Schema):  # INSERT INTO t (col, col) 的列清单形态
        node = node.this
    return node if isinstance(node, exp.Table) else None


def _cte_index(statement: exp.Expression) -> dict[str, dict[str, tuple[str, str]]]:
    """CTE 名 → {投影别名: (真实表, 真实列)}。

    只追一层：``WITH pay AS (SELECT p.party AS customer FROM tabPayment Entry p)``
    能让 ``pay.customer`` 落到 ``tabPayment Entry.party``。CTE 套 CTE 不追——追不到
    就丢键，不猜。
    """
    index: dict[str, dict[str, tuple[str, str]]] = {}
    for cte in statement.find_all(exp.CTE):
        inner = cte.this
        if not isinstance(inner, exp.Expression):
            continue
        inner_aliases = _alias_map(inner, cte_names=set())
        projections: dict[str, tuple[str, str]] = {}
        for projection in getattr(inner, "expressions", []) or []:
            column = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(column, exp.Column):
                continue
            source = inner_aliases.get((column.table or "").lower())
            if source is None:
                # 无表限定的列：内部只有一张表时才敢认
                tables = {_table_name(t) for t in inner.find_all(exp.Table)}
                if len(tables) != 1:
                    continue
                source = next(iter(tables))
            projections[projection.alias_or_name.lower()] = (source, column.name)
        index[cte.alias.lower()] = projections
    return index


def _alias_map(statement: exp.Expression, cte_names: set[str]) -> dict[str, str]:
    """别名/表名 → 真实表名。CTE 名不算表，排除掉。"""
    mapping: dict[str, str] = {}
    for table in statement.find_all(exp.Table):
        name = _table_name(table)
        if not name or name.lower() in cte_names:
            continue
        mapping[table.name.lower()] = name
        alias = table.alias_or_name
        if alias:
            mapping[alias.lower()] = name
    return mapping


def _resolve_column(
    column: exp.Column,
    alias_map: dict[str, str],
    cte_map: dict[str, dict[str, tuple[str, str]]],
) -> tuple[str, str] | None:
    """列引用 → (真实表, 真实列)。解析不出来返回 None——丢掉，不猜。"""
    qualifier = (column.table or "").lower()
    if not qualifier:
        return None
    if qualifier in alias_map:
        return alias_map[qualifier], column.name
    projections = cte_map.get(qualifier)
    if projections:
        return projections.get(column.name.lower())
    return None


def _join_keys(
    statement: exp.Expression,
    alias_map: dict[str, str],
    cte_map: dict[str, dict[str, tuple[str, str]]],
) -> tuple[JoinKey, ...]:
    """从所有等值条件里挑出「两端属于不同表」的那些。

    不区分 JOIN ON 与 WHERE：老 SQL 用逗号连表、条件写在 WHERE 里，那也是关联键。
    ``p.docstatus = 1`` 这类一端是常量的自然不满足两端都是列。
    """
    keys: list[JoinKey] = []
    seen: set[tuple[str, str, str, str]] = set()
    for equality in statement.find_all(exp.EQ):
        left, right = equality.this, equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_ref = _resolve_column(left, alias_map, cte_map)
        right_ref = _resolve_column(right, alias_map, cte_map)
        if left_ref is None or right_ref is None:
            continue
        if left_ref[0] == right_ref[0]:
            continue
        signature = (*left_ref, *right_ref)
        if signature in seen:
            continue
        seen.add(signature)
        keys.append(
            JoinKey(
                left_table=left_ref[0],
                left_column=left_ref[1],
                right_table=right_ref[0],
                right_column=right_ref[1],
            )
        )
    return tuple(keys)


def _owning_table(statement: exp.Expression) -> str | None:
    """``CREATE TABLE`` / ``ALTER TABLE`` 语句管的是哪张表。"""
    node = statement.this
    if isinstance(node, exp.Schema):  # CREATE TABLE t (...) 的列清单形态
        node = node.this
    return _table_name(node) if isinstance(node, exp.Table) else None


def _reference_target(reference: exp.Expression | None) -> tuple[str, list[str]] | None:
    """``REFERENCES tbl(col, col)`` → ``(表名, [列名])``。读不出来返回 None。"""
    if reference is None:
        return None
    inner = reference.this
    if isinstance(inner, exp.Schema):
        table = inner.this
        columns = [c.name for c in inner.expressions if c.name]
        if isinstance(table, exp.Table):
            return _table_name(table), columns
        return None
    if isinstance(inner, exp.Table):
        return _table_name(inner), []
    return None


def _declared_foreign_keys(statement: exp.Expression) -> list[DeclaredForeignKey]:
    """扫 DDL 里声明的外键。三种写法都要认，少认一种就是整包漏掉。

    1. 表级约束：``CONSTRAINT fk FOREIGN KEY (a) REFERENCES t(b)`` → ``exp.ForeignKey``
    2. ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`` → 同样是 ``exp.ForeignKey``，
       只是挂在 ``exp.Alter`` 下，owning table 取 ``statement.this``
    3. 列级内联：``order_id INT REFERENCES orders(id)`` → **没有** ForeignKey 节点，
       只有 ``exp.ColumnDef`` 里的 ``exp.Reference``

    目标列缺省（``REFERENCES t`` 不写列）时按位置对不上，直接丢——不猜主键叫什么。
    """
    table = _owning_table(statement)
    if not table:
        return []

    found: list[DeclaredForeignKey] = []

    for fk in statement.find_all(exp.ForeignKey):
        source_columns = [c.name for c in fk.expressions if c.name]
        target = _reference_target(fk.args.get("reference"))
        if not source_columns or target is None:
            continue
        target_table, target_columns = target
        if not target_table or len(target_columns) != len(source_columns):
            continue
        for source_column, target_column in zip(
            source_columns, target_columns, strict=True
        ):
            found.append(
                DeclaredForeignKey(table, source_column, target_table, target_column)
            )

    for column_def in statement.find_all(exp.ColumnDef):
        # 列级内联 REFERENCES：owning column 就是这个 ColumnDef 自己。
        for reference in column_def.find_all(exp.Reference):
            target = _reference_target(reference)
            if target is None or not column_def.name:
                continue
            target_table, target_columns = target
            if not target_table or len(target_columns) != 1:
                continue
            found.append(
                DeclaredForeignKey(
                    table, column_def.name, target_table, target_columns[0]
                )
            )

    return [fk for fk in found if fk.table != fk.target_table]


def _statement_lineage(statement: exp.Expression) -> StatementLineage | None:
    target_node = _target_table(statement)
    if target_node is None:
        return None
    target = _table_name(target_node)
    if not target:
        return None

    cte_map = _cte_index(statement)
    cte_names = set(cte_map)
    alias_map = _alias_map(statement, cte_names)

    # **只从查询体里取上游**，不是从整条语句里扫表。
    # 扫整条语句会把约束子句里的表也当成上游：``CREATE TABLE orders (...
    # FOREIGN KEY (customer_id) REFERENCES customers(id))`` 会推出一条
    # 「customers 加工至 orders」的血缘——那是假的，外键说的是「订单引用客户」。
    # 这条边一旦上报进 DataHub，下游会把它判成 derivation、命名成「派生出」。
    body = statement.args.get("expression")
    if body is None:
        return None

    sources: list[str] = []
    for table in body.find_all(exp.Table):
        name = _table_name(table)
        if not name or name.lower() in cte_names or name == target:
            continue
        if name not in sources:
            sources.append(name)

    if not sources:
        return None

    return StatementLineage(
        target=target,
        sources=tuple(sources),
        join_keys=_join_keys(statement, alias_map, cte_map),
    )


def extract(sql: str, dialect: str = "mysql") -> SqlParseResult:
    """解析一份 SQL 文本（可含多条语句），返回其中所有有落点的血缘。"""
    text = (sql or "").strip()
    if not text:
        return SqlParseResult(error=EMPTY_FILE_HINT)

    # DELIMITER 是客户端指令不是 SQL，sqlglot 报的错看不出这回事，先自己认出来
    for line in text.splitlines():
        if line.strip().upper().startswith("DELIMITER"):
            return SqlParseResult(error=STORED_PROCEDURE_HINT)

    try:
        statements = [item for item in sqlglot.parse(text, read=dialect) if item is not None]
    except (ParseError, TokenError) as exc:
        return SqlParseResult(error=f"解析失败：{_first_line(exc)}")
    except SqlglotError as exc:  # 方言不符等
        return SqlParseResult(error=f"解析失败：{_first_line(exc)}")
    except RecursionError:
        return SqlParseResult(error="语句嵌套过深，解析放弃")

    if not statements:
        return SqlParseResult(error=EMPTY_FILE_HINT)

    result = SqlParseResult(statements=len(statements))
    seen_fks: set[DeclaredForeignKey] = set()
    for statement in statements:
        lineage = _statement_lineage(statement)
        if lineage is not None:
            result.lineages.append(lineage)
        for fk in _declared_foreign_keys(statement):
            if fk not in seen_fks:
                seen_fks.add(fk)
                result.foreign_keys.append(fk)
    return result


def _first_line(exc: Exception) -> str:
    message = str(exc).strip().splitlines()
    return message[0][:200] if message else exc.__class__.__name__
