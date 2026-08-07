"""表单起草走 context+drafter 派生路径的回归测试。

背景：任务新建面板此前把表单原样当 spec 直填，缺 drafter 派生的必填字段（sync 的
source/target、transform 的结构化清洗规则、metric 对 business_logic_id 的解析），一律
过不了校验闸门或执行期报错。修复后统一走 context 路径，drafter 用显式选择器做确定性派生。

本文件锁住三件事：
1. transform 把表单多选给的规则码结构化成执行器要的 [{rule, description}]；
2. metric drafter 优先按 business_logic_id 选口径（表单下拉给的是 id，不是 name）；
3. sync drafter 让表单显式选的 mode/partition_key 覆盖契约默认。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agents.drafters.metric import MetricDrafter
from app.agents.drafters.sync import SyncDrafter
from app.agents.drafters.transform import TransformDrafter
from app.database import SessionLocal
from app.models import BusinessLogic, DomainContext, ObjectType, Ontology, OntologyStatus


# ---------- transform：清洗规则码 → 结构化 ----------


def test_transform_rules_from_context_structures_codes():
    """表单多选给规则码列表；结构化成 [{rule, description}]，描述取闭集词表。"""
    out = TransformDrafter._rules_from_context(["deduplicate", "trim"])
    assert out == [
        {"rule": "deduplicate", "description": "按主键去重"},
        {"rule": "trim", "description": "字符串首尾去空格"},
    ]


def test_transform_rules_from_context_preserves_dicts_and_drops_unknown():
    """已是 dict 的原样保留（补描述）；闭集外的码丢弃，不臆造规则。"""
    out = TransformDrafter._rules_from_context(
        [{"rule": "uppercase"}, "not_a_real_rule", "drop_null"]
    )
    assert out == [
        {"rule": "uppercase", "description": "统一转大写"},
        {"rule": "drop_null", "description": "过滤关键字段空值"},
    ]


def test_transform_rules_from_context_empty():
    assert TransformDrafter._rules_from_context(None) == []
    assert TransformDrafter._rules_from_context([]) == []


# ---------- metric：business_logic_id 优先 ----------


@pytest.fixture
def ontology_with_logics():
    """播种一个本体 + 两条业务逻辑，返回 (ontology_id, logic_a_id, logic_b_id)。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:ctx-{uuid4().hex[:8]}", name="ctx-domain"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(onto)
        db.flush()
        # 两条同前缀名，确保意图分词匹配无法区分——只有按 id 选才拿得准。
        a = BusinessLogic(
            ontology_id=onto.id, name="gmv", display_name="成交额",
            logic_type="metric", expression_summary="sum(amount)",
        )
        b = BusinessLogic(
            ontology_id=onto.id, name="gmv_refund", display_name="退款额",
            logic_type="metric", expression_summary="sum(refund)",
        )
        db.add_all([a, b])
        db.commit()
        return onto.id, a.id, b.id


def test_metric_select_logic_honors_business_logic_id(ontology_with_logics):
    """表单下拉给 business_logic_id：即便 intent 空，也精确命中该口径。"""
    ontology_id, _a_id, b_id = ontology_with_logics
    spec = MetricDrafter().draft(
        "", {"ontology_id": ontology_id, "business_logic_id": b_id}
    )
    assert spec["business_logic_id"] == b_id
    assert spec["metric_name"] == "gmv_refund"


def test_metric_business_logic_id_beats_intent(ontology_with_logics):
    """id 优先级高于意图匹配：intent 指向 A，但显式 id 指向 B → 选 B。"""
    ontology_id, _a_id, b_id = ontology_with_logics
    spec = MetricDrafter().draft(
        "成交额 gmv", {"ontology_id": ontology_id, "business_logic_id": b_id}
    )
    assert spec["metric_name"] == "gmv_refund"


# ---------- sync：mode / partition_key 覆盖 ----------


@pytest.fixture
def ontology_with_source_object():
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:sync-{uuid4().hex[:8]}", name="sync-domain"
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


def test_sync_honors_mode_and_partition_key_overrides(ontology_with_source_object):
    """表单显式选的装载方式/分区键进 spec（无契约时此前只会落默认 full/None）。"""
    ontology_id = ontology_with_source_object
    spec = SyncDrafter().draft(
        "",
        {
            "ontology_id": ontology_id,
            "object_type": "orders",
            "mode": "incremental",
            "partition_key": "created_at",
        },
    )
    assert spec["mode"] == "incremental"
    assert spec["partition_key"] == "created_at"
    # source/target 由 drafter 派生（表单不收）——正是此前直填缺的必填字段。
    assert spec["source"] == "erp.orders"
    assert spec["target"].endswith(".orders")
