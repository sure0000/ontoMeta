"""调度表达式的形态校验。

为什么要有这道闸：cron 会逐字写进生成的 DAG。写错的表达式让 Airflow **import 不了**
那条 DAG——而 import 失败在 ontoMeta 这边完全看不见（回执 ok、任务显示"已提交"），
表只是永远不更新。
"""

from __future__ import annotations

import pytest

from app.services.cron_spec import CronError, normalize_cron


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("0 2 * * *", "0 2 * * *"),
        ("  0   2 * * *  ", "0 2 * * *"),  # 多余空白归一
        ("0 */6 * * *", "0 */6 * * *"),
        ("*/15 * * * MON-FRI", "*/15 * * * MON-FRI"),
        ("0 3 1,15 * *", "0 3 1,15 * *"),
        ("@daily", "@daily"),
        ("@Daily", "@daily"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_valid_expressions(expr, expected):
    assert normalize_cron(expr) == expected


@pytest.mark.parametrize(
    "expr,fragment",
    [
        ("0 2 * *", "五段"),
        ("0 2 * * * *", "五段"),
        ("99 2 * * *", "分钟"),
        ("0 25 * * *", "小时"),
        ("0 2 32 * *", "日"),
        ("0 2 * 13 *", "月"),
        ("0 2 * * 9", "星期"),
        ("0 2 * * 5-1", "起点大于终点"),
        ("每天两点", "五段"),
        ("a b c d e", "分钟"),
        ("@nope", "调度预设"),
        ("0 2 * * */0", "步长"),
    ],
)
def test_invalid_expressions(expr, fragment):
    with pytest.raises(CronError) as exc:
        normalize_cron(expr)
    assert fragment in str(exc.value)


def test_gate_blocks_invalid_schedule():
    """闸门拦下非法 cron：不能等到 DAG 在 Airflow 侧 import 失败才发现。"""
    from app.agents.validation import is_blocking, validate_spec
    from app.database import SessionLocal

    with SessionLocal() as db:
        issues = validate_spec(
            db,
            kind="transform",
            spec={"target_table": "customer", "engine": "doris", "schedule": "0 99 * * *"},
            ontology_id=None,
        )
    schedule_issues = [i for i in issues if i.code == "schedule_invalid"]
    assert schedule_issues, "非法 cron 必须在闸门上报出来"
    assert is_blocking(schedule_issues[0]), "写错的调度会让 DAG 根本不存在，必须阻断"
    assert "小时" in schedule_issues[0].message


def test_gate_accepts_empty_and_valid_schedule():
    from app.agents.validation import validate_spec
    from app.database import SessionLocal

    with SessionLocal() as db:
        for schedule in ("", None, "0 2 * * *"):
            issues = validate_spec(
                db,
                kind="sync",
                spec={"object_type": "customer", "engine": "doris", "refresh_cron": schedule},
                ontology_id=None,
            )
            assert not [i for i in issues if i.code == "schedule_invalid"]


def test_gate_enforces_task_target_layer_boundaries():
    """目标层边界必须由后端执行，不能只依赖手动/Data Agent 下拉框。"""
    from app.agents.validation import validate_spec
    from app.database import SessionLocal

    with SessionLocal() as db:
        transform_issues = validate_spec(
            db,
            kind="transform",
            spec={"target_table": "customer", "engine": "doris", "target_layer": "ads"},
            ontology_id=None,
        )
        metric_issues = validate_spec(
            db,
            kind="metric",
            spec={"metric_name": "gmv", "engine": "doris", "target_layer": "dim"},
            ontology_id=None,
        )

    assert any(i.code == "transform_target_layer_forbidden" for i in transform_issues)
    assert any(i.code == "metric_target_layer_forbidden" for i in metric_issues)
