"""L3：血缘驱动的任务调度（lineage_scheduler）单元测试。

钉住：URN 匹配推导依赖（A 产出 B 消费 → A>>B）、多产出者、环检测、
compile_lineage_dag 把依赖编进 DAG、describe_lineage 的摘要结构、
build_flink_sql_dag 的 task_dependencies 落进 spec 与 DAG 源码。
"""

from __future__ import annotations

import ast

import pytest

from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    FlinkSubmitConfig,
    build_flink_sql_dag,
)
from app.services.lineage_scheduler import (
    LineageSchedulerError,
    ScheduledTask,
    compile_lineage_dag,
    derive_dependencies,
    describe_lineage,
)

_ODS_BRAND = "urn:li:dataset:(urn:li:dataPlatform:hive,ods.brand,PROD)"
_DWD_BRAND = "urn:li:dataset:(urn:li:dataPlatform:hive,dwd.brand,PROD)"
_ADS_BRAND = "urn:li:dataset:(urn:li:dataPlatform:hive,ads.brand,PROD)"
_ERP = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tab_brand,PROD)"


def _cfg(tmp_path, **over) -> FlinkSubmitConfig:
    # runner_jar 是 ontoMeta 侧真实路径（随包分发）：用临时假 jar，指向
    # /opt/... 的占位路径现在会直接被 jar 读取报错。
    from tests.support.delivery import make_runner_jar

    base = dict(runner_jar=make_runner_jar(tmp_path))
    base.update(over)
    return FlinkSubmitConfig(**base)


def _st(task_id: str, *, src=(), tgt="", **kw) -> ScheduledTask:
    return ScheduledTask(
        task=FlinkSqlTask(
            task_id=task_id,
            sql=f"INSERT INTO t SELECT 1;",
            source_urns=tuple(src),
            target_urn=tgt,
        ),
        **kw,
    )


# --------------------------------------------------------------------------- 依赖推导


def test_derive_chain_from_urns():
    """A 产出 dwd.brand，B 消费 dwd.brand → A >> B。"""
    a = _st("sync_brand", tgt=_DWD_BRAND)
    b = _st("clean_brand", src=[_DWD_BRAND], tgt=_ADS_BRAND)
    edges = derive_dependencies([a, b])
    assert [(e[0].task_id, e[1].task_id) for e in edges] == [("sync_brand", "clean_brand")]


def test_derive_two_step_chain():
    """ERP → ods.brand → dwd.brand → ads.brand 全链。"""
    s = _st("sync", src=[_ERP], tgt=_ODS_BRAND)
    t = _st("clean", src=[_ODS_BRAND], tgt=_DWD_BRAND)
    m = _st("agg", src=[_DWD_BRAND], tgt=_ADS_BRAND)
    edges = derive_dependencies([s, t, m])
    pairs = {(e[0].task_id, e[1].task_id) for e in edges}
    assert pairs == {("sync", "clean"), ("clean", "agg")}


def test_derive_fan_out():
    """一张表被两个下游消费 → 两个下游都依赖它。"""
    a = _st("produce", tgt=_DWD_BRAND)
    b = _st("consume1", src=[_DWD_BRAND])
    c = _st("consume2", src=[_DWD_BRAND])
    edges = derive_dependencies([a, b, c])
    pairs = {(e[0].task_id, e[1].task_id) for e in edges}
    assert pairs == {("produce", "consume1"), ("produce", "consume2")}


def test_derive_no_dependency_when_urn_unrelated():
    a = _st("a", tgt=_DWD_BRAND)
    b = _st("b", src=[_ERP])  # 消费源表，与 a 无关
    edges = derive_dependencies([a, b])
    assert edges == []


def test_derive_multiple_producers_both_edges():
    """同一张表被两个任务产出 → 下游同时依赖两者（都成功才跑）。"""
    p1 = _st("p1", tgt=_DWD_BRAND)
    p2 = _st("p2", tgt=_DWD_BRAND)
    c = _st("c", src=[_DWD_BRAND])
    edges = derive_dependencies([p1, p2, c])
    pairs = {(e[0].task_id, e[1].task_id) for e in edges}
    assert pairs == {("p1", "c"), ("p2", "c")}


def test_derive_explicit_upstream_urns():
    """显式 upstream_urns（跨提交依赖，查 DataHub 得）也参与匹配。"""
    upstream = _st("external", tgt="urn:li:dataset:(urn:li:dataPlatform:hive,ext.t,PROD)")
    b = ScheduledTask(
        task=FlinkSqlTask(task_id="b", sql="INSERT INTO t SELECT 1;",
                          source_urns=(), target_urn=_DWD_BRAND),
        upstream_urns=("urn:li:dataset:(urn:li:dataPlatform:hive,ext.t,PROD)",),
    )
    edges = derive_dependencies([upstream, b])
    assert [(e[0].task_id, e[1].task_id) for e in edges] == [("external", "b")]


def test_derive_cycle_raises():
    """A 产出 B 消费、B 产出 A 消费 → 环，抛错。"""
    a = _st("a", src=[_DWD_BRAND], tgt=_ODS_BRAND)
    b = _st("b", src=[_ODS_BRAND], tgt=_DWD_BRAND)
    with pytest.raises(LineageSchedulerError, match="循环依赖"):
        derive_dependencies([a, b])


def test_derive_duplicate_task_ids_raises():
    with pytest.raises(LineageSchedulerError, match="task_id 重复"):
        derive_dependencies([_st("x"), _st("x")])


# --------------------------------------------------------------------------- DAG 编译


def test_compile_lineage_dag_wires_dependencies_into_spec(tmp_path):
    a = _st("sync_brand", tgt=_DWD_BRAND)
    b = _st("clean_brand", src=[_DWD_BRAND], tgt=_ADS_BRAND)
    bundle = compile_lineage_dag(
        ontology_id="artifact-1",
        engine="hive",
        tasks=[a, b],
        config=_cfg(tmp_path),
        warehouse_conn_id="warehouse_hive",
    )
    assert bundle.spec["task_dependencies"] == [("sync_brand", "clean_brand")]
    ast.parse(bundle.dag_source)  # 合法 Python


def test_compile_lineage_dag_dag_source_has_dependency_wiring(tmp_path):
    a = _st("sync_brand", tgt=_DWD_BRAND)
    b = _st("clean_brand", src=[_DWD_BRAND], tgt=_ADS_BRAND)
    bundle = compile_lineage_dag(
        ontology_id="artifact-1",
        engine="hive",
        tasks=[a, b],
        config=_cfg(tmp_path),
        warehouse_conn_id="warehouse_hive",
    )
    # 模板按 spec.task_dependencies 循环渲染依赖，不写死具体任务名
    assert 'for up_id, down_id in _SPEC.get("task_dependencies", [])' in bundle.dag_source
    assert 'move_tasks[up_id] >> move_tasks[down_id]' in bundle.dag_source
    # spec 里是真实的 (上游, 下游) 对
    assert bundle.spec["task_dependencies"] == [("sync_brand", "clean_brand")]


def test_compile_lineage_dag_no_deps_yields_empty(tmp_path):
    a = _st("a", tgt=_DWD_BRAND)
    b = _st("b", tgt=_ADS_BRAND)  # 无依赖
    bundle = compile_lineage_dag(
        ontology_id="artifact-1",
        engine="hive",
        tasks=[a, b],
        config=_cfg(tmp_path),
        warehouse_conn_id="warehouse_hive",
    )
    assert bundle.spec["task_dependencies"] == []


def test_build_flink_sql_dag_rejects_unknown_dependency(tmp_path):
    """task_dependencies 引用了不在 tasks 里的任务 → 抛错。"""
    a = _st("a", tgt=_DWD_BRAND)
    ghost = _st("ghost", tgt=_ADS_BRAND)
    with pytest.raises(ValueError, match="不在 tasks 里"):
        build_flink_sql_dag(
            ontology_id="x",
            engine="hive",
            tasks=[a.task],
            ddl_statements={},
            config=_cfg(tmp_path),
            task_dependencies=[(a.task, ghost.task)],
        )


# --------------------------------------------------------------------------- 摘要


def test_describe_lineage_summary():
    a = _st("sync_brand", label="品牌同步", tgt=_DWD_BRAND)
    b = _st("clean_brand", label="品牌清洗", src=[_DWD_BRAND], tgt=_ADS_BRAND)
    summary = describe_lineage([a, b])
    assert len(summary["tasks"]) == 2
    assert summary["tasks"][0]["label"] == "品牌同步"
    assert summary["dependencies"] == [
        {"upstream": "sync_brand", "downstream": "clean_brand"}
    ]


def test_reconstructed_task_without_flink_task_derives_deps():
    """L4 重建场景：task=None、只有 URN 字段的 ScheduledTask 也能推导依赖。"""
    a = ScheduledTask(
        task_id="sync_brand", label="品牌同步",
        source_urns=(), target_urn=_DWD_BRAND,
    )
    b = ScheduledTask(
        task_id="clean_brand", label="品牌清洗",
        source_urns=(_DWD_BRAND,), target_urn=_ADS_BRAND,
    )
    summary = describe_lineage([a, b])
    assert summary["dependencies"] == [
        {"upstream": "sync_brand", "downstream": "clean_brand"}
    ]


def test_reconstructed_task_cycle_raises_gracefully():
    """重建任务的环：describe_lineage 内部推导会抛，但 _conversation_lineage 已包 try。"""
    a = ScheduledTask(task_id="a", source_urns=(_DWD_BRAND,), target_urn=_ODS_BRAND)
    b = ScheduledTask(task_id="b", source_urns=(_ODS_BRAND,), target_urn=_DWD_BRAND)
    with pytest.raises(LineageSchedulerError):
        describe_lineage([a, b])
