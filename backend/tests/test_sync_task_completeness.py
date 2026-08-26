"""同步任务的「填得完、跑得起来」——从实际数据工作出发的两条硬缺口。

1. **调度频率**：入仓作业跑一次不叫管道。此前 Spec 里根本没有 ``refresh_cron`` 这个键，
   执行器读到的恒为 None，产出的 DAG 一律 ``schedule=None``（只能人点）。想让同步定时
   跑，只能绕到物化弹窗里逐实体改契约——没人找得到。
2. **增量 / CDC 的策略参数**：装载方式给了三个选项，表单却只到那里为止。选「增量同步」
   的人提交时被打回「必须配置 primary_keys / incremental_column / initial_watermark」，
   而表单里没有这三个格子——三选一里两个是死路。
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.agents.drafters.sync import SyncDrafter
from app.agents.executors.sync import SyncExecutor
from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, Property
from app.services.chat_bi import ChatBiService

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,erp.public.{t},PROD)"


@pytest.fixture
def sync_domain() -> dict:
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:sc-{tag}", name=f"sc-{tag}")
        db.add(domain)
        db.flush()
        onto = Ontology(domain_context_id=domain.id, status="draft", version=0)
        db.add(onto)
        db.flush()
        obj = ObjectType(
            ontology_id=onto.id, name="sale_order", display_name="销售订单",
            table_role="business_object", source_ref=_URN.format(t="sale_order"),
        )
        db.add(obj)
        db.flush()
        db.add_all([
            Property(object_type_id=obj.id, name="sale_order_id", display_name="订单ID",
                     data_type="bigint", semantic_type="identifier"),
            Property(object_type_id=obj.id, name="updated_at", display_name="更新时间",
                     data_type="timestamp", semantic_type="datetime"),
            Property(object_type_id=obj.id, name="amount", display_name="金额",
                     data_type="decimal", semantic_type="amount"),
        ])
        db.commit()
        return {"ontology_id": onto.id, "object": "sale_order"}


# ---------- ① 调度频率 ----------


def test_sync_spec_carries_schedule(sync_domain):
    """表单填的调度频率必须进 Spec：不进就等于那个格子是装饰。"""
    spec = SyncDrafter().draft(
        "每天同步销售订单",
        {
            "ontology_id": sync_domain["ontology_id"],
            "object_type": "sale_order",
            "refresh_cron": "0 2 * * *",
        },
    )
    assert spec["refresh_cron"] == "0 2 * * *"


def test_sync_spec_schedule_defaults_to_manual(sync_domain):
    """留空 = 仅手动触发；不硬塞一个默认周期（人没要求的定时不该自己长出来）。"""
    spec = SyncDrafter().draft(
        "同步销售订单",
        {"ontology_id": sync_domain["ontology_id"], "object_type": "sale_order"},
    )
    assert spec["refresh_cron"] is None


def test_sync_executor_passes_schedule_to_runner(monkeypatch):
    """``run_sync(refresh_cron=…)`` 才真的作用到产出的 DAG 上（一个 cron 一个 DAG）。"""
    from app.services import materialization_runner

    mock_run = MagicMock(return_value={"dag_id": "d", "dag_run_id": "r", "ok": True})
    monkeypatch.setattr(materialization_runner, "run_sync", mock_run)

    SyncExecutor().execute(
        {
            "ontology_id": "onto-x",
            "object_type": "Customer",
            "source": "erp.customers",
            "target_datasource_id": "some-non-warehouse-ds",
            "engine": "doris",
            "mode": "full",
            "refresh_cron": "0 3 * * *",
        },
        {},
    )
    assert mock_run.call_args.kwargs["refresh_cron"] == "0 3 * * *"


def test_sync_dry_run_shows_schedule():
    """「确认执行方案」那一环要看得见这条同步是定时跑还是只跑一次。"""
    diff = SyncExecutor().dry_run(
        {"object_type": "Customer", "mode": "full", "refresh_cron": "0 2 * * *"}, {}
    )
    assert diff["refresh_cron"] == "0 2 * * *"


def test_sync_form_asks_for_schedule(sync_domain):
    fields = _sync_form_fields(sync_domain["ontology_id"])
    assert fields["refresh_cron"]["type"] == "cron"
    assert fields["refresh_cron"]["confirmation_node"] == "data"


# ---------- ② 增量 / CDC 的策略参数 ----------


def _sync_form_fields(ontology_id: str) -> dict:
    with SessionLocal() as db:
        result, _s, is_error = ChatBiService()._dispatch_request_form(
            db,
            ontology_id=ontology_id,
            args={"title": "同步参数", "task_kind": "sync", "intent": "同步 sale_order"},
        )
    assert is_error is False
    return {f["name"]: f for f in result["form"]["fields"]}


def test_sync_form_covers_every_load_strategy(sync_domain):
    """三种装载方式各自要的参数都得有格子可填，否则那个选项是死路。"""
    fields = _sync_form_fields(sync_domain["ontology_id"])
    for key in ("primary_keys", "incremental_column", "initial_watermark", "sequence_column",
                "delete_policy"):
        assert key in fields, f"选了增量/CDC 却没有 {key} 的输入格"

    # 每个策略字段都声明了在哪种装载方式下出现：全量同步的人不该看到六个填不着的格子。
    assert fields["incremental_column"]["visible_when"] == {"field": "mode", "in": ["incremental"]}
    assert fields["initial_watermark"]["visible_when"] == {"field": "mode", "in": ["incremental"]}
    assert fields["sequence_column"]["visible_when"] == {"field": "mode", "in": ["cdc"]}
    assert fields["delete_policy"]["visible_when"] == {"field": "mode", "in": ["cdc"]}
    assert fields["primary_keys"]["visible_when"] == {
        "field": "mode", "in": ["incremental", "cdc"]
    }


def test_sync_form_scopes_column_candidates_to_the_chosen_object(sync_domain):
    """主键/增量字段/sequence 列的候选必须是**所选那张表**的列，且随对象实时取。"""
    fields = _sync_form_fields(sync_domain["ontology_id"])
    for key in ("primary_keys", "incremental_column", "sequence_column"):
        assert fields[key]["options_from"] == "object_properties"
        assert fields[key]["depends_on"] == "object_type"
        # 静态候选不该摊在表单里：几百对象的本体全摊开是几 MB 的消息负载。
        assert not fields[key].get("options")


def test_sync_context_errors_match_the_form_fields(sync_domain):
    """提交闸门要的键，与表单给的格子必须一一对上。"""
    from app.services.chat_bi_tool_schemas import _sync_context_errors

    fields = _sync_form_fields(sync_domain["ontology_id"])
    with SessionLocal() as db:
        incremental = _sync_context_errors(db, {"mode": "incremental"})
        cdc = _sync_context_errors(db, {"mode": "cdc"})
    for message in incremental + cdc:
        named = [key for key in fields if key in message]
        assert named, f"闸门报「{message}」，但表单里没有对应字段"


def test_cdc_checkpoint_follows_the_settings_default(sync_domain, monkeypatch):
    """设置页配了全局 checkpoint 目录就跟随，不逼每条 CDC 任务重填一遍。

    见 DEVELOPMENT_PRINCIPLES P1「全局配置 ≠ 唯一取值」：设置页那份是默认值。
    """
    from app.api.deps import settings_service
    from app.services.chat_bi_tool_schemas import _sync_context_errors

    base = settings_service.get_airflow_runtime

    def _with_dir(db, *a, **kw):
        rt = base(db, *a, **kw)
        rt.flink_checkpoint_dir = "hdfs:///flink/ck"
        return rt

    monkeypatch.setattr(settings_service, "get_airflow_runtime", _with_dir)
    with SessionLocal() as db:
        errors = _sync_context_errors(db, {"mode": "cdc", "sequence_column": "updated_at",
                                           "delete_policy": "ignore"})
        assert not [e for e in errors if "checkpoint" in e]
        # 表单也不该再问一遍。
        fields = {
            f["name"]
            for f in ChatBiService()._sync_strategy_fields(db)
        }
        assert "flink_checkpoint_dir" not in fields


def test_cdc_without_any_checkpoint_dir_is_still_blocked(sync_domain, monkeypatch):
    """两处都没有仍要拦：没有读位点持久化，CDC 一重启就从头重搬。"""
    from app.api.deps import settings_service
    from app.services.chat_bi_tool_schemas import _sync_context_errors

    base = settings_service.get_airflow_runtime

    def _without_dir(db, *a, **kw):
        rt = base(db, *a, **kw)
        rt.flink_checkpoint_dir = ""
        return rt

    monkeypatch.setattr(settings_service, "get_airflow_runtime", _without_dir)
    with SessionLocal() as db:
        errors = _sync_context_errors(db, {"mode": "cdc", "sequence_column": "updated_at",
                                           "delete_policy": "ignore"})
    assert [e for e in errors if "checkpoint" in e]


# ---------- ③ 字段候选按对象收窄 ----------


def test_property_options_scope_to_one_object(sync_domain, client, admin_headers):
    """全本体混列会让人从几百张表的字段里选到一个根本不在目标表上的列。"""
    onto_id = sync_domain["ontology_id"]
    with SessionLocal() as db:
        other = ObjectType(
            ontology_id=onto_id, name="supplier", display_name="供应商",
            table_role="business_object", source_ref=_URN.format(t="supplier"),
        )
        db.add(other)
        db.flush()
        db.add(Property(object_type_id=other.id, name="supplier_code",
                        display_name="供应商编码", data_type="varchar"))
        db.commit()

    everything = client.get(
        f"/api/ontologies/{onto_id}/properties", headers=admin_headers
    ).json()
    assert {p["name"] for p in everything} >= {"sale_order_id", "supplier_code"}

    scoped = client.get(
        f"/api/ontologies/{onto_id}/properties?object_type=sale_order", headers=admin_headers
    ).json()
    names = {p["name"] for p in scoped}
    assert names == {"sale_order_id", "updated_at", "amount"}
    assert "supplier_code" not in names


def test_identity_column_is_flagged_only_when_conventional(sync_domain, client, admin_headers):
    """``<对象>_id`` 命中约定 → 可作主键默认值；只是"名字里有 id"的不算。

    与本体投影同一份判据（primary_key_is_confident）：猜错主键会让重跑插出重复行，
    故有把握才预选，没把握就让人自己指。
    """
    onto_id = sync_domain["ontology_id"]
    scoped = client.get(
        f"/api/ontologies/{onto_id}/properties?object_type=sale_order", headers=admin_headers
    ).json()
    by_name = {p["name"]: p for p in scoped}
    assert by_name["sale_order_id"]["is_identity"] is True
    assert by_name["updated_at"]["is_identity"] is False
    # 类型也回传：界面据此提示哪几列像时间列（增量字段通常取更新时间）。
    assert by_name["updated_at"]["semantic_type"] == "datetime"


def test_identity_flag_absent_without_the_convention(client, admin_headers):
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:nc-{tag}", name=f"nc-{tag}")
        db.add(domain)
        db.flush()
        onto = Ontology(domain_context_id=domain.id, status="draft", version=0)
        db.add(onto)
        db.flush()
        obj = ObjectType(ontology_id=onto.id, name="bank", display_name="银行",
                         table_role="business_object")
        db.add(obj)
        db.flush()
        db.add(Property(object_type_id=obj.id, name="custom_external_id",
                        display_name="外部ID", data_type="varchar",
                        semantic_type="identifier"))
        db.commit()
        onto_id = onto.id

    scoped = client.get(
        f"/api/ontologies/{onto_id}/properties?object_type=bank", headers=admin_headers
    ).json()
    assert all(p["is_identity"] is False for p in scoped)


# ---------- ④ 列表 API 必须带上「能不能同步」这个事实 ----------


def test_object_list_reports_real_source_provenance(sync_domain, client, admin_headers):
    """``source_provenance`` 是派生属性，读模型按关键字构造时必须显式带上。

    漏掉它 → 恒取默认值 ``"none"`` → 前端把每个对象都判成「无源表，不可同步」，
    同步向导里的对象全部置灰、一个都选不了，还配上一句「该本体下的对象都没有物理源表」
    的错误解释。字段存在却永远不填，比没有这个字段更糟。
    """
    onto_id = sync_domain["ontology_id"]
    items = client.get(
        f"/api/object-types?ontology_id={onto_id}&published_only=false",
        headers=admin_headers,
    ).json()["items"]
    by_name = {o["name"]: o for o in items}
    assert by_name["sale_order"]["source_provenance"] == "datahub"

    with SessionLocal() as db:
        db.add(ObjectType(
            ontology_id=onto_id, name="manual_plan", display_name="人工计划",
            table_role="business_object", source_ref="manual:doris:manual_plan",
        ))
        db.add(ObjectType(
            ontology_id=onto_id, name="no_source", display_name="无来源",
            table_role="business_object", source_ref=None,
        ))
        db.commit()

    items = client.get(
        f"/api/object-types?ontology_id={onto_id}&published_only=false",
        headers=admin_headers,
    ).json()["items"]
    by_name = {o["name"]: o for o in items}
    assert by_name["manual_plan"]["source_provenance"] == "manual"
    assert by_name["no_source"]["source_provenance"] == "none"
