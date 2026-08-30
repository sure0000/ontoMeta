"""任务级 Flink 执行参数：每个任务自己填，留空跟随设置页。

钉住四件事：
1. ``normalize`` 的闭集/边界/注入校验（这些值会拼进 flink run 的命令行）；
2. 优先级——任务级覆盖 > 设置页默认，且"没填"必须真的等于跟随设置页（不是落个空值）；
3. 参数真的到达产物：DAG 里的 ``flink run`` 命令带的是**这个任务**的并行度/队列；
4. drafter 把表单填的参数收进 Spec，校验闸门在确认前拦下非法值。
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agents.drafters.sync import SyncDrafter
from app.agents.validation import validate_spec
from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus
from app.services import flink_params


def _airflow(tmp_path, **over):
    """设置页 Flink 默认值，供参数优先级单元测试使用。"""
    defaults = dict(
        flink_sql_runner_jar=str(tmp_path / "flink-sql-runner.jar"),
        flink_sql_runner_class="com.ontometa.flink.SqlRunner",
        flink_bin="flink",
        flink_deploy_target="yarn-per-job",
        flink_parallelism=1,
        flink_yarn_queue="",
        flink_checkpoint_dir="",
    )
    defaults.update(over)
    return MagicMock(**defaults)


# ---------- normalize：闭集与边界 ----------


def test_blank_values_are_dropped_not_stored():
    """没填 = 跟随设置页。空串/None/空列表都不落键——落个空值会覆盖掉默认。"""
    assert flink_params.normalize(
        {"flink_parallelism": None, "flink_yarn_queue": "  ", "flink_extra_args": []}
    ) == {}
    assert flink_params.normalize(None) == {}
    assert flink_params.normalize({"target_table": "customer"}) == {}


def test_filled_values_are_coerced():
    out = flink_params.normalize(
        {
            "flink_parallelism": "8",  # 表单可能给字符串
            "flink_yarn_queue": " etl ",
            "flink_deploy_target": "yarn-session",
            "flink_checkpoint_dir": "hdfs://ns/ckpt",
            "flink_extra_args": ["-Dtaskmanager.memory.process.size=2g"],
        }
    )
    assert out == {
        "flink_parallelism": 8,
        "flink_yarn_queue": "etl",
        "flink_deploy_target": "yarn-session",
        "flink_checkpoint_dir": "hdfs://ns/ckpt",
        "flink_extra_args": ("-Dtaskmanager.memory.process.size=2g",),
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"flink_parallelism": 0},
        {"flink_parallelism": 999},
        {"flink_parallelism": "many"},
        {"flink_deploy_target": "kubernetes-application"},  # 闭集外
        {"flink_yarn_queue": "etl queue"},  # 带空格 → 命令行会被切成两个参数
        {"flink_checkpoint_dir": "/var/ckpt"},  # 缺 scheme
    ],
)
def test_invalid_values_are_rejected(raw):
    with pytest.raises(flink_params.FlinkParamError):
        flink_params.normalize(raw)


@pytest.mark.parametrize(
    "arg",
    [
        "-Dkey=val; rm -rf /",  # 命令注入
        "-Dkey=$(whoami)",
        "-Dkey=`id`",
        "not-a-flag",
        "-Dkey=a b",
    ],
)
def test_extra_args_reject_shell_metacharacters(arg):
    """extra_args 直接进 BashOperator 的 bash_command（纯 join），形态必须闭集。"""
    with pytest.raises(flink_params.FlinkParamError):
        flink_params.normalize({"flink_extra_args": [arg]})


def test_extra_args_accept_string_form():
    """对话/表单可能给一整串；按空白切开后逐个校验。"""
    assert flink_params.normalize(
        {"flink_extra_args": "-Da=1 -Db=2"}
    )["flink_extra_args"] == ("-Da=1", "-Db=2")


# ---------- 优先级：任务级 > 设置页 ----------


def test_from_spec_prefers_spec_over_context():
    params = flink_params.from_spec(
        {"flink_parallelism": 4}, {"flink_parallelism": 16, "flink_yarn_queue": "etl"}
    )
    assert params == {"flink_parallelism": 4, "flink_yarn_queue": "etl"}


def test_resolve_config_merges_defaults_with_overrides(tmp_path):
    airflow = _airflow(tmp_path, flink_parallelism=2, flink_yarn_queue="global")
    cfg = flink_params.resolve_config(
        airflow, {"flink_parallelism": 16, "flink_deploy_target": "local"}
    )
    assert cfg.parallelism == 16  # 任务级覆盖
    assert cfg.deploy_target == "local"  # 任务级覆盖
    assert cfg.yarn_queue == "global"  # 没填 → 跟随设置页
    assert cfg.flink_bin == "flink"  # 部署事实，永远来自设置页


def test_resolve_config_queue_fallback_only_when_nothing_configured(tmp_path):
    airflow = _airflow(tmp_path, flink_yarn_queue="")
    assert flink_params.resolve_config(airflow, None, queue_fallback="default").yarn_queue == "default"
    assert flink_params.resolve_config(airflow, {"flink_yarn_queue": "etl"}).yarn_queue == "etl"


def test_resolve_checkpoint_dir_prefers_task(tmp_path):
    airflow = _airflow(tmp_path, flink_checkpoint_dir="file:///global/ckpt")
    assert flink_params.resolve_checkpoint_dir(airflow, None) == "file:///global/ckpt"
    assert (
        flink_params.resolve_checkpoint_dir(
            airflow, {"flink_checkpoint_dir": "hdfs://ns/task"}
        )
        == "hdfs://ns/task"
    )


# ---------- drafter 收下 + 闸门拦住 ----------


@pytest.fixture
def ontology_with_source_object():
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:fp-{uuid4().hex[:8]}", name="fp-domain"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(onto)
        db.flush()
        db.add(
            ObjectType(
                ontology_id=onto.id, name="orders", display_name="订单",
                table_role="business_object",
                source_ref="urn:li:dataset:(urn:li:dataPlatform:mysql,erp.orders,PROD)",
            )
        )
        db.commit()
        return onto.id


def test_sync_drafter_carries_form_flink_params(ontology_with_source_object):
    """表单填的 Flink 参数进 Spec；没填的不落键（留空 = 跟随设置页）。"""
    spec = SyncDrafter().draft(
        "",
        {
            "ontology_id": ontology_with_source_object,
            "object_type": "orders",
            "flink_parallelism": 12,
            "flink_yarn_queue": "etl",
        },
    )
    assert spec["flink_parallelism"] == 12
    assert spec["flink_yarn_queue"] == "etl"
    assert "flink_checkpoint_dir" not in spec


def test_validation_gate_blocks_invalid_flink_params(ontology_with_source_object):
    """非法参数在**人工确认之前**被拦下，而不是到 DagRun 里变成一条 bash 报错。"""
    with SessionLocal() as db:
        issues = validate_spec(
            db,
            kind="sync",
            spec={
                "object_type": "orders",
                "target_datasource_id": "ds-1",
                "flink_yarn_queue": "etl; rm -rf /",
            },
            ontology_id=ontology_with_source_object,
        )
    assert any(i.code == "flink_param_invalid" for i in issues)
