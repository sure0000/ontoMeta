"""L1：Flink SQL 血缘解析单元测试。

钉住：FROM/JOIN 源表解析、INSERT INTO 目标表解析、逻辑名→物理名（WITH table-name）
映射、多源保序去重、解析失败返回空（不抛错）、与 build_flink_sql_dag 的 inlets/outlets
注入联动。
"""

from __future__ import annotations

import ast

from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    FlinkSubmitConfig,
    build_flink_sql_dag,
)
from app.services.flink_sql_lineage import parse_flink_sql_lineage


def _cfg(tmp_path, **over) -> FlinkSubmitConfig:
    # runner_jar 是 ontoMeta 侧真实路径（随包分发）：用临时假 jar，指向
    # /opt/... 的占位路径现在会直接被 jar 读取报错。
    from tests.support.delivery import make_runner_jar

    base = dict(runner_jar=make_runner_jar(tmp_path))
    base.update(over)
    return FlinkSubmitConfig(**base)


# --------------------------------------------------------------------------- 解析器


def test_parse_simple_insert_select():
    """INSERT INTO ... SELECT ... FROM ... 的标准形态。"""
    sql = """SET 'execution.runtime-mode' = 'batch';
CREATE TABLE `src_brand` (id INT) WITH ('connector'='jdbc', 'table-name'='ods.brand');
CREATE TABLE `brand` (id INT) WITH ('connector'='jdbc', 'table-name'='dwd.brand');
INSERT INTO `brand` SELECT * FROM `src_brand`;
"""
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["ods.brand"]
    assert lg.target_table == "dwd.brand"


def test_parse_join_multiple_sources():
    """JOIN 多源表：保序去重。"""
    sql = """CREATE TABLE `a` (id INT) WITH ('table-name'='ods.a');
CREATE TABLE `b` (id INT) WITH ('table-name'='ods.b');
CREATE TABLE `out` (id INT) WITH ('table-name'='dws.out');
INSERT INTO `out`
SELECT a.id FROM `a` JOIN `b` ON a.id = b.id;
"""
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["ods.a", "ods.b"]
    assert lg.target_table == "dws.out"


def test_parse_mysql_table_name_is_bare():
    """MySQL 平台 table-name 是裸表名（URL 带库）。"""
    sql = """CREATE TABLE `src_customer` (id INT) WITH ('connector'='jdbc', 'table-name'='customer');
CREATE TABLE `customer` (id INT) WITH ('connector'='jdbc', 'table-name'='dwd.customer');
INSERT INTO `customer` SELECT * FROM `src_customer`;
"""
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["customer"]
    assert lg.target_table == "dwd.customer"


def test_parse_ctas_style():
    """CTAS（CREATE TABLE AS SELECT）形态：目标从 CREATE 拿，源从 SELECT 的 FROM 拿。"""
    sql = """CREATE TABLE `agg` (k STRING, v INT) WITH ('table-name'='ads.agg');
INSERT INTO `agg`
SELECT k, COUNT(*) FROM `src_event` GROUP BY k;
"""
    # src_event 未声明 CREATE TABLE → 逻辑名原样保留
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["src_event"]
    assert lg.target_table == "ads.agg"


def test_parse_empty_sql_returns_empty():
    lg = parse_flink_sql_lineage("")
    assert lg.source_tables == []
    assert lg.target_table is None


def test_parse_unrecognized_sql_does_not_raise():
    """识别不到（无 CREATE/INSERT）→ 返回空，不抛错（血缘是增强）。"""
    lg = parse_flink_sql_lineage("SET 'x' = 'y';")
    assert lg.source_tables == []
    assert lg.target_table is None


def test_parse_no_table_name_falls_back_to_logical():
    """WITH 里没有 table-name（逻辑名直用）→ 源/目标回退逻辑名。"""
    sql = """CREATE TABLE `src_t` (id INT) WITH ('connector'='jdbc');
CREATE TABLE `t` (id INT) WITH ('connector'='jdbc');
INSERT INTO `t` SELECT * FROM `src_t`;
"""
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["src_t"]
    assert lg.target_table == "t"


def test_parse_deduplicates_sources_preserving_order():
    sql = """CREATE TABLE `s` (id INT) WITH ('table-name'='ods.s');
CREATE TABLE `s2` (id INT) WITH ('table-name'='ods.s2');
CREATE TABLE `o` (id INT) WITH ('table-name'='ads.o');
INSERT INTO `o` SELECT * FROM `s` JOIN `s2` ON 1=1 JOIN `s` ON 1=1;
"""
    lg = parse_flink_sql_lineage(sql)
    assert lg.source_tables == ["ods.s", "ods.s2"]


# --------------------------------------------------------------------------- DAG 注入


def test_inlets_outlets_land_in_task_spec(tmp_path):
    """FlinkSqlTask 的 source_urns/target_urn 进 task_spec 的 inlets/outlets。"""
    b = build_flink_sql_dag(
        ontology_id="artifact-abc",
        engine="hive",
        tasks=[
            FlinkSqlTask(
                task_id="clean",
                sql="INSERT INTO t SELECT 1;",
                source_urns=("urn:li:dataset:(urn:li:dataPlatform:hive,ods.brand,PROD)",),
                target_urn="urn:li:dataset:(urn:li:dataPlatform:hive,dwd.brand,PROD)",
            )
        ],
        ddl_statements={"dwd.brand": "CREATE TABLE t (x int)"},
        config=_cfg(tmp_path),
    )
    task = b.spec["tasks"][0]
    assert task["inlets"] == ["urn:li:dataset:(urn:li:dataPlatform:hive,ods.brand,PROD)"]
    assert task["outlets"] == ["urn:li:dataset:(urn:li:dataPlatform:hive,dwd.brand,PROD)"]


def test_empty_urns_yield_empty_inlets_outlets(tmp_path):
    b = build_flink_sql_dag(
        ontology_id="artifact-abc",
        engine="hive",
        tasks=[FlinkSqlTask(task_id="mv", sql="INSERT INTO t SELECT 1;")],
        ddl_statements={},
        config=_cfg(tmp_path),
    )
    task = b.spec["tasks"][0]
    assert task["inlets"] == []
    assert task["outlets"] == []


def test_dag_source_wires_inlets_outlets_and_is_valid_python(tmp_path):
    b = build_flink_sql_dag(
        ontology_id="artifact-abc",
        engine="hive",
        tasks=[
            FlinkSqlTask(
                task_id="clean",
                sql="INSERT INTO t SELECT 1;",
                source_urns=("urn:li:dataset:(urn:li:dataPlatform:hive,ods.brand,PROD)",),
                target_urn="urn:li:dataset:(urn:li:dataPlatform:hive,dwd.brand,PROD)",
            )
        ],
        ddl_statements={},
        config=_cfg(tmp_path),
    )
    ast.parse(b.dag_source)  # 合法 Python
    assert "inlets=task_spec.get(\"inlets\", [])" in b.dag_source
    assert "outlets=task_spec.get(\"outlets\", [])" in b.dag_source
