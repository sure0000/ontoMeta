"""P1-3：Flink SQL 计算 DAG 构建（Flink on YARN，BashOperator）单元测试。

钉住：flink run 命令形态（--file + -Dyarn.application.queue + extra_args）、
.sql 在 spec.tasks[].sql、依赖串联、batch/streaming（-d）、建表 DDL 去分号、
DAG 源码是合法 Python、幂等、staging swap。

统一执行架构（F/G 后）接口：build_flink_sql_dag(ontology_id, engine, tasks,
ddl_statements, config, ...)；DagBundle 是纯数据（dag_id/dag_source/spec）；
SQL 从 spec.tasks[].sql 读，运行期 sql_dir 走 xcom；swap 在 spec.swaps。
"""

from __future__ import annotations

import ast

from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    FlinkSubmitConfig,
    build_flink_sql_dag,
    flink_dag_id_for,
)


_JAR_PATH: str | None = None


def _fake_jar() -> str:
    """懒创建的假 SqlRunner jar。

    runner_jar 现在是 ontoMeta 侧真实路径（生成期读字节算 sha256 做内容寻址），
    指向 /opt 的占位路径会直接 FileNotFoundError。内容无所谓，sha 只决定文件名。
    """
    global _JAR_PATH
    if _JAR_PATH is None:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".jar")
        import os

        os.write(fd, b"fake-sql-runner")
        os.close(fd)
        _JAR_PATH = path
    return _JAR_PATH


def _cfg(**over) -> FlinkSubmitConfig:
    base = dict(runner_jar=_fake_jar())
    base.update(over)
    return FlinkSubmitConfig(**base)


def _build(**over):
    kwargs = dict(
        ontology_id="artifact-abc-123",
        engine="hive",
        tasks=[FlinkSqlTask(task_id="clean_customer", sql="INSERT INTO t SELECT 1;")],
        ddl_statements={"dim.customer": "CREATE TABLE t (x int)"},
        warehouse_conn_id="warehouse_hive",
        config=_cfg(),
    )
    kwargs.update(over)
    return build_flink_sql_dag(**kwargs)


def test_dag_id_is_stable_and_flink_scoped():
    assert flink_dag_id_for("artifact-abc-123").startswith("ontometa_flink_")
    assert flink_dag_id_for("x", "b0").endswith("__b0")


def test_sql_lands_in_task_spec():
    b = _build()
    task = b.spec["tasks"][0]
    assert task["task_id"] == "clean_customer"
    assert isinstance(task["sql"], str)
    assert "INSERT INTO t" in task["sql"]
    # .sql 文件名稳定，运行期由 xcom 的 sql_dir 拼绝对路径
    assert task["sql_file"] == "clean_customer.sql"


def test_command_submits_to_yarn_via_flink_run():
    b = _build()
    cmd = b.spec["tasks"][0]["command"]
    assert cmd.startswith("flink run -t yarn-per-job -p 1")
    # jar 随包分发：命令里是 xcom 的 lib_dir + 内容寻址文件名（sha12），
    # 不再是 Airflow 机器上的预置绝对路径。
    assert "-c com.ontometa.flink.SqlRunner" in cmd
    assert "lib_dir'] }}/sql-runner-" in cmd
    assert "sql-runner-" + b.spec["runner"]["sha256"][:12] in cmd
    # SqlRunner 只认 --file；路径是运行期 xcom 解析的 sql_dir + 文件名
    assert "--file " in cmd
    assert "xcom_pull" in cmd and cmd.endswith(".sql")


def test_streaming_task_submits_detached():
    b = _build(
        tasks=[FlinkSqlTask(task_id="rt", sql="INSERT INTO t SELECT 1;", detached=True)]
    )
    assert "-d" in b.spec["tasks"][0]["command"].split()


def test_batch_task_is_not_detached():
    b = _build()
    assert "-d" not in b.spec["tasks"][0]["command"].split()


def test_yarn_queue_and_extra_args_are_passed():
    b = _build(config=_cfg(yarn_queue="etl", extra_args=("-Dkey=val",)))
    cmd = b.spec["tasks"][0]["command"]
    assert "-Dyarn.application.queue=etl" in cmd
    assert "-Dkey=val" in cmd


def test_warehouse_ddl_is_split_into_single_statements():
    b = _build(ddl_statements={"ads_gmv": "CREATE TABLE ads_gmv (gmv decimal);"})
    assert b.spec["warehouse_ddl"] == ["CREATE TABLE ads_gmv (gmv decimal)"]


def test_credentials_only_via_env_jinja_not_in_sql_file():
    b = _build(
        tasks=[
            FlinkSqlTask(
                task_id="t1",
                sql="CREATE TABLE s (...) WITH ('url'='${ERP_URL}');",
                env={"ERP_URL": "{{ conn.erp.host }}"},
            )
        ]
    )
    # 凭据表达式在 endpoint_env，不在 sql 产物里
    assert b.spec["tasks"][0]["endpoint_env"] == {"ERP_URL": "{{ conn.erp.host }}"}
    assert "conn.erp.host" not in b.spec["tasks"][0]["sql"]


def test_dag_source_is_valid_python():
    b = _build(
        tasks=[
            FlinkSqlTask(task_id="a", sql="INSERT INTO t SELECT 1;"),
            FlinkSqlTask(task_id="b", sql="INSERT INTO u SELECT 2;"),
        ],
        ddl_statements={"t": "CREATE TABLE t (x int);"},
    )
    ast.parse(b.dag_source)  # 不抛即合法


def test_empty_tasks_and_ddl_yield_empty_dag():
    """空输入产出空 DAG（不抛）——空批的防御在上层 materialization_runner 跳过，
    build 层只忠实编排，职责分离。"""
    b = _build(tasks=[], ddl_statements={})
    assert b.spec["tasks"] == []
    assert b.spec["warehouse_ddl"] == []
    ast.parse(b.dag_source)  # 空 DAG 仍是合法 Python


def test_build_is_idempotent():
    a = _build()
    b = _build()
    assert a.dag_source == b.dag_source
    assert a.spec == b.spec


# ---------- staging / swap（统一执行 B2）----------


def test_swap_statements_land_in_spec_swaps():
    """全量搬运的 staging→正式表切换语句进 spec.swaps（逐条去分号）。"""
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv_customer", sql="INSERT INTO stg SELECT 1;")],
        swaps={"mv_customer": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    assert b.spec["swaps"]["mv_customer"] == ["ALTER TABLE dim REPLACE WITH TABLE stg"]


def test_no_swap_yields_no_swap_entry():
    b = _build(tasks=[FlinkSqlTask(task_id="mv", sql="INSERT INTO t SELECT 1;")])
    assert b.spec["swaps"].get("mv") in (None, [])


def test_swap_operator_wired_downstream_in_dag_source():
    """有 swap 的任务，DAG 源码里挂一个下游 swap_<task> SQLExecuteQueryOperator。"""
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv_customer", sql="INSERT INTO stg SELECT 1;")],
        swaps={"mv_customer": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    ast.parse(b.dag_source)  # 合法
    assert "swap_" in b.dag_source
    assert "SQLExecuteQueryOperator" in b.dag_source


def test_staging_dag_source_is_valid_python():
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv", sql="INSERT INTO stg SELECT 1;")],
        ddl_statements={"dim": "CREATE TABLE dim (x int);", "stg": "CREATE TABLE stg LIKE dim;"},
        swaps={"mv": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    ast.parse(b.dag_source)


# ---------- dag_id 的 base：制品，不是本体 ----------


def test_dag_id_base_defaults_to_ontology_but_can_be_overridden():
    """同一本体上并存的物化/同步两条制品必须落在不同 dag_id 上，否则互相覆盖投递。"""
    same_ontology = dict(ontology_id="onto-1", dag_id_suffix="manual")
    a = _build(**same_ontology, dag_id_base="artifact-materialize")
    b = _build(**same_ontology, dag_id_base="artifact-sync")
    assert a.dag_id != b.dag_id
    # 不传 base 时退回本体 id（旧行为，也是 runner 缺 artifact_id 时的回退）
    assert _build(**same_ontology).dag_id == flink_dag_id_for("onto-1", "manual")


# ---------- DAG 模板的运行时结构（用替身 Airflow 真跑一遍模板）----------


class _FakeTask:
    """替身算子：只记 task_id 与 >> 出来的边。"""

    def __init__(self, task_id, **kw):
        self.task_id = task_id
        self.kwargs = kw
        _REGISTRY["tasks"][task_id] = self

    def __rshift__(self, other):
        _REGISTRY["edges"].add((self.task_id, other.task_id))
        return other


_REGISTRY: dict = {"tasks": {}, "edges": set()}


def _exec_dag_source(bundle, tmp_path, monkeypatch):
    """把生成的 DAG 源码在替身 Airflow 下真的执行一遍，返回 {tasks, edges}。

    模板里的建表守卫、依赖串联都是**运行期** Python（DAG 文件里的 if / >>），
    只对源码做字符串断言钉不住它们；本机没装 airflow，故注入替身模块。
    """
    import json
    import sys
    import types

    _REGISTRY["tasks"], _REGISTRY["edges"] = {}, set()

    class _FakeDag:
        def __init__(self, **kw):
            self.kwargs = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    _mod("airflow", DAG=_FakeDag)
    _mod("airflow.operators")
    _mod("airflow.operators.python", PythonOperator=_FakeTask)
    _mod("airflow.operators.bash", BashOperator=_FakeTask)
    _mod("airflow.operators.empty", EmptyOperator=_FakeTask)
    _mod("airflow.providers")
    _mod("airflow.providers.common")
    _mod("airflow.providers.common.sql")
    _mod("airflow.providers.common.sql.operators")
    _mod(
        "airflow.providers.common.sql.operators.sql",
        SQLExecuteQueryOperator=_FakeTask,
    )
    _mod("airflow.utils")
    _mod("airflow.utils.task_group", TaskGroup=object)

    (tmp_path / bundle.spec_filename).write_text(
        json.dumps(bundle.spec), encoding="utf-8"
    )
    dag_file = tmp_path / bundle.dag_filename
    dag_file.write_text(bundle.dag_source, encoding="utf-8")
    exec(  # noqa: S102 —— 被测对象就是这段生成的源码
        compile(bundle.dag_source, str(dag_file), "exec"),
        {"__file__": str(dag_file), "__name__": "_generated_dag"},
    )
    # 透出算子 kwargs：字符串断言钉不住 append_env 这类"会静默丢环境"的坑，
    # 必须从真执行后的算子对象上读。
    return {
        "tasks": set(_REGISTRY["tasks"]),
        "edges": set(_REGISTRY["edges"]),
        "kwargs": {tid: t.kwargs for tid, t in _REGISTRY["tasks"].items()},
    }


def test_move_task_keeps_worker_env_via_append_env(tmp_path, monkeypatch):
    """append_env=True 是跨机部署的命门：Airflow 的 env 默认**替换**整个环境，
    丢了它 flink 命令（PATH）、HADOOP_CONF_DIR、FLINK_HOME 全没，worker 上
    `flink: command not found`。旧模板有、重写时丢过——回归专测。"""
    b = _build()
    dag = _exec_dag_source(b, tmp_path, monkeypatch)
    move = dag["kwargs"]["clean_customer"]
    assert move["append_env"] is True


def test_jar_path_is_live_xcom_expression(tmp_path, monkeypatch):
    """jar 路径必须走 read_spec 的 xcom（lib_dir + 内容寻址文件名），不能是生成期
    绝对路径——ontoMeta 与 Airflow 不同机时，生成期根本不知道远端挂载点。"""
    b = _build()
    dag = _exec_dag_source(b, tmp_path, monkeypatch)
    # 真执行 DAG 后，read_spec 的 python_callable 在本地 tmp 目录上算出的值
    read_spec = dag["kwargs"]["read_spec"]["python_callable"]()
    assert read_spec["lib_dir"].endswith("_lib")
    jar_name = b.spec["runner"]["jar"]
    assert jar_name == f"sql-runner-{b.spec['runner']['sha256'][:12]}.jar"
    cmd = dag["kwargs"]["clean_customer"]["bash_command"]
    assert "{{ ti.xcom_pull(task_ids='read_spec')['lib_dir'] }}/" + jar_name in cmd


def test_no_ddl_yields_no_create_tables_task(tmp_path, monkeypatch):
    """一条 DDL 都没有时不建 create_tables——否则得到一个 sql=[] 的空 SQL 算子。

    只搬数据的同步 DAG（全增量、或关了 staging）正是这种形态。
    """
    b = _build(ddl_statements={})
    dag = _exec_dag_source(b, tmp_path, monkeypatch)
    assert "create_tables" not in dag["tasks"]
    assert "clean_customer" in dag["tasks"]


def test_create_tables_present_when_ddl_given(tmp_path, monkeypatch):
    b = _build()
    dag = _exec_dag_source(b, tmp_path, monkeypatch)
    assert "create_tables" in dag["tasks"]
    assert ("create_tables", "clean_customer") in dag["edges"]


def test_embedded_connections_are_ensured_before_sql_and_flink(tmp_path, monkeypatch):
    connections = [{
        "conn_id": "managed_mysql",
        "conn_type": "mysql",
        "host": "db",
        "login": "root",
        "password": "secret",
        "schema": "erp",
        "port": 3306,
        "extra": {},
    }]
    bundle = _build(connections=connections)
    dag = _exec_dag_source(bundle, tmp_path, monkeypatch)

    assert bundle.spec["connections"] == connections
    assert "ensure_connections" in dag["tasks"]
    assert ("read_spec", "ensure_connections") in dag["edges"]
    assert ("ensure_connections", "create_tables") in dag["edges"]
    assert ("ensure_connections", "clean_customer") in dag["edges"]
    xcom_value = dag["kwargs"]["read_spec"]["python_callable"]()
    assert "connections" not in xcom_value["spec"]
    assert "secret" not in str(xcom_value)


def test_move_task_always_waits_for_read_spec(tmp_path, monkeypatch):
    """搬运任务的 sql 路径来自 read_spec 的 XCom，必须挂在它下游。

    此前只有 ``create_tables >> move_task`` 这一条边间接保证了顺序；没有建表任务的
    DAG（只搬数据）里搬运任务会与 read_spec 并发调度，xcom_pull 拿到 None，
    命令拼成 ``None/xxx.sql``。
    """
    dag = _exec_dag_source(_build(ddl_statements={}), tmp_path, monkeypatch)
    assert ("read_spec", "clean_customer") in dag["edges"]


def test_generated_dag_is_executable_not_merely_parseable(tmp_path, monkeypatch):
    """回归：模板曾放在 f-string 里靠手工加倍花括号，数错了就产出「能 parse、一 import
    就炸」的文件。

    ``default_args = {{…}}`` 渲染成了 ``{…}`` 外面再套一层 ``{}``——一个装着 dict 的
    set，import 即 ``TypeError: unhashable type: 'dict'``；``f"swap_{{task_id}}"`` 渲染成
    字面量 ``swap_{task_id}``，多个 swap 于是撞成同一个 task_id。``ast.parse`` 对两者
    都是绿的，所以这里必须**执行**模板，并核对 swap 任务名真的按 task_id 展开。
    """
    b = _build(
        tasks=[
            FlinkSqlTask(task_id="mv_a", sql="INSERT INTO a SELECT 1;"),
            FlinkSqlTask(task_id="mv_b", sql="INSERT INTO b SELECT 2;"),
        ],
        swaps={"mv_a": ["ALTER TABLE a REPLACE WITH stg_a"],
               "mv_b": ["ALTER TABLE b REPLACE WITH stg_b"]},
    )
    dag = _exec_dag_source(b, tmp_path, monkeypatch)
    assert {"swap_mv_a", "swap_mv_b"} <= dag["tasks"]
    assert "swap_{task_id}" not in dag["tasks"]
    assert ("mv_a", "swap_mv_a") in dag["edges"]
