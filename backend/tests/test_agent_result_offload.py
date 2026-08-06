"""V4 S1 单测：大结果离场存储（O2）——store 分页 + run_sql 投影 + 遥测。"""

from __future__ import annotations

from app.services.agent_result_store import RunResultStore, project_run_sql_for_model
from app.services import agent_telemetry
from app.services.agent_telemetry import RunTelemetry


def _big_result(n: int = 100) -> dict:
    return {
        "executed": True,
        "sql": "SELECT a FROM t",
        "columns": [{"key": "a", "title": "A"}],
        "rows": [{"a": i} for i in range(n)],
        "row_count": n,
        "truncated": True,
        "proved": {"tables": ["t"], "columns": ["t.a"]},
    }


def test_projection_replaces_rows_with_sample_and_handle():
    store = RunResultStore()
    ref = project_run_sql_for_model(_big_result(100), store, sample_rows=5)
    # 回给模型的引用里没有全量 rows，只有样例
    assert "rows" not in ref
    assert len(ref["sample_rows"]) == 5
    assert ref["result_handle"] == "rs_1"
    assert ref["rows_omitted"] == 95
    assert "read_result" in ref["result_note"]
    # 证书等其它字段保留
    assert ref["proved"]["tables"] == ["t"]
    assert ref["row_count"] == 100


def test_store_paging_and_full_fidelity():
    store = RunResultStore()
    project_run_sql_for_model(_big_result(50), store, sample_rows=5)
    page = store.page("rs_1", offset=20, limit=10)
    assert page["returned"] == 10
    assert page["rows"][0] == {"a": 20}
    assert page["total"] == 50
    assert page["has_more"] is True
    # 末页
    last = store.page("rs_1", offset=45, limit=10)
    assert last["returned"] == 5
    assert last["has_more"] is False


def test_unknown_handle_returns_error_not_raise():
    store = RunResultStore()
    page = store.page("rs_999", offset=0, limit=10)
    assert "error" in page


def test_small_result_all_in_sample():
    store = RunResultStore()
    ref = project_run_sql_for_model(_big_result(3), store, sample_rows=5)
    assert len(ref["sample_rows"]) == 3
    assert "rows_omitted" not in ref
    assert "read_result" not in ref["result_note"]


def test_non_executed_result_passthrough():
    store = RunResultStore()
    res = {"executed": False, "sql": "SELECT 1", "reason": "无数据源"}
    ref = project_run_sql_for_model(res, store, sample_rows=5)
    assert ref is res  # 原样返回，不离场


def test_telemetry_offload_snapshot():
    agent_telemetry.reset()
    run = RunTelemetry()
    run.offload(7000)
    run.offload(3000)
    agent_telemetry.record(run)
    snap = agent_telemetry.snapshot()
    assert snap["offloaded_chars"] == 10000
    assert snap["offload_count"] == 2
    agent_telemetry.reset()
