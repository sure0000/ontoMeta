"""全量落地的 staging + 原子切换语句（M15，§3.3）。

各引擎切换语法不同，故落在 Dialect Adapter（与建表 DDL 同处），**不进 runner**。这里对每个
引擎做 golden：钉住生成的语句形状。⚠ 语句在真实 Doris/Hive/StarRocks/ClickHouse 上的
**原子性与代价**属部署侧验收（§8.3），本测试只保证「生成对了什么」，不代表「已在库里跑通」。
"""

from __future__ import annotations

from app.warehouse import get_adapter
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_RUN = "manual__2024-08-03T12:00:00+00:00"
# run_id 清洗后的后缀（非字母数字 → _）。staging/old 表名都带它，重跑/补数不撞表。
_SUF = "manual_2024_08_03T12_00_00_00_00"


def _table(database="dw"):
    return LogicalTable(
        name="dim_customer",
        database=database,
        columns=[LogicalColumn(name="id", data_type="bigint")],
    )


def test_run_id_is_sanitized_into_staging_name():
    stg = get_adapter("doris").staging_table_name(_table(), _RUN)
    assert stg == f"dim_customer__stg_{_SUF}"
    # 冒号/加号/减号都不能进表名
    for ch in (":", "+", "-"):
        assert ch not in stg


def test_doris_swap_is_single_atomic_replace():
    swap = get_adapter("doris").render_swap(_table(), _RUN)
    assert swap == [
        f'ALTER TABLE `dw`.`dim_customer` REPLACE WITH TABLE '
        f'`dw`.`dim_customer__stg_{_SUF}` PROPERTIES("swap" = "false");'
    ]
    # 单语句 = 原子；swap=false 表示替换后丢弃 staging（不把旧数据换到 staging 名下）
    assert len(swap) == 1 and 'swap" = "false' in swap[0]


def test_doris_create_staging_is_like():
    ddl = get_adapter("doris").render_create_staging(_table(), _RUN)
    assert ddl == (
        f"CREATE TABLE IF NOT EXISTS `dw`.`dim_customer__stg_{_SUF}` LIKE `dw`.`dim_customer`;"
    )


def test_hive_swap_is_insert_overwrite_then_drop():
    swap = get_adapter("hive").render_swap(_table(), _RUN)
    assert swap == [
        f"INSERT OVERWRITE TABLE `dw`.`dim_customer` SELECT * FROM `dw`.`dim_customer__stg_{_SUF}`;",
        f"DROP TABLE IF EXISTS `dw`.`dim_customer__stg_{_SUF}`;",
    ]


def test_starrocks_swap_is_swap_with_then_drop():
    swap = get_adapter("starrocks").render_swap(_table(), _RUN)
    assert swap == [
        f"ALTER TABLE `dw`.`dim_customer` SWAP WITH `dw`.`dim_customer__stg_{_SUF}`;",
        f"DROP TABLE IF EXISTS `dw`.`dim_customer__stg_{_SUF}`;",
    ]


def test_clickhouse_swap_is_exchange_tables_then_drop():
    a = get_adapter("clickhouse")
    assert a.render_swap(_table(), _RUN) == [
        f"EXCHANGE TABLES `dw`.`dim_customer` AND `dw`.`dim_customer__stg_{_SUF}`;",
        f"DROP TABLE IF EXISTS `dw`.`dim_customer__stg_{_SUF}`;",
    ]
    # ClickHouse 无 LIKE，用 AS 复制结构
    assert a.render_create_staging(_table(), _RUN) == (
        f"CREATE TABLE IF NOT EXISTS `dw`.`dim_customer__stg_{_SUF}` AS `dw`.`dim_customer`;"
    )


def test_base_default_swap_is_rename_two_step_with_window():
    """没有原生原子切换的引擎退到 rename 两步——有短暂窗口，非真正原子，这是下限。"""
    # doris 覆写了 render_swap；用它的 create_staging 走的是 base 默认（LIKE），
    # 而 base 默认 render_swap 的形状用一个未覆写的场景校验：直接调 base 逻辑。
    from app.warehouse.adapters.base import DialectAdapter

    class _Bare(DialectAdapter):
        name = "bare"

        def capabilities(self):  # pragma: no cover - 不涉及能力校验
            raise NotImplementedError

        def map_type(self, d, s):  # pragma: no cover
            raise NotImplementedError

        def render_create_table(self, table):  # pragma: no cover
            raise NotImplementedError

        def render_alter(self, before, after):  # pragma: no cover
            raise NotImplementedError

    swap = _Bare().render_swap(_table(), _RUN)
    assert swap == [
        f"DROP TABLE IF EXISTS `dw`.`dim_customer__old_{_SUF}`;",
        f"ALTER TABLE `dw`.`dim_customer` RENAME TO `dw`.`dim_customer__old_{_SUF}`;",
        f"ALTER TABLE `dw`.`dim_customer__stg_{_SUF}` RENAME TO `dw`.`dim_customer`;",
        f"DROP TABLE IF EXISTS `dw`.`dim_customer__old_{_SUF}`;",
    ]


def test_swap_without_database_omits_qualifier():
    swap = get_adapter("doris").render_swap(_table(database=None), _RUN)
    assert swap == [
        f'ALTER TABLE `dim_customer` REPLACE WITH TABLE '
        f'`dim_customer__stg_{_SUF}` PROPERTIES("swap" = "false");'
    ]
