"""维度模型编译：把设计变成物化契约。

这一整块此前是「有端点、有模型、有校验器，唯独没有实现」——``compile_model`` 返回
「模型已编译（占位实现）」，只改状态不产任何东西；``dimensional_model_validator``
写给 ``DimensionalModelSpecV2``，而落库的是另一套字段名，一条规则也跑不到，
是个零调用方的死模块。

补齐之后要钉住的是：编译产出**既有的** MaterializationContract（不另立一套制品）、
四类特殊情况各自留痕不静默跳过、以及不覆盖人工钉住的字段。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import DomainContext, MaterializationContract, ObjectType, Ontology
from app.models.dimensional_model import DimensionalModel
from app.services.dimensional_model import DimensionalModelService

SERVICE = DimensionalModelService()


@pytest.fixture
def scaffold(db):
    """一个域 + 本体 + 三个对象（客户维、日期维用不上对象、订单事实）。"""
    token = uuid4().hex[:12]
    domain = DomainContext(
        id=f"dm-domain-{token}",
        datahub_domain_id=f"urn:li:domain:{token}",
        name="销售",
    )
    ontology = Ontology(
        id=f"dm-onto-{token}",
        domain_context_id=domain.id,
        status="published",
        version=1,
    )
    customer = ObjectType(
        id=f"dm-obj-cust-{token}",
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        status="published",
    )
    order_line = ObjectType(
        id=f"dm-obj-line-{token}",
        ontology_id=ontology.id,
        name="order_line",
        display_name="订单明细",
        status="published",
    )
    db.add_all([domain, ontology, customer, order_line])
    db.commit()

    created: list[DimensionalModel] = []

    def _model(**overrides) -> DimensionalModel:
        model = DimensionalModel(
            id=f"dm-{uuid4().hex[:12]}",
            domain_id=domain.id,
            ontology_id=ontology.id,
            name=overrides.pop("name", "订单分析星型模型"),
            display_name="订单分析",
            business_process="客户下单",
            grain=overrides.pop("grain", "每笔订单明细行"),
            fact_tables=overrides.pop("fact_tables", []),
            dimensions=overrides.pop("dimensions", []),
            conformed_dimensions=overrides.pop("conformed_dimensions", []),
            status=overrides.pop("status", "confirmed"),
            **overrides,
        )
        db.add(model)
        db.commit()
        created.append(model)
        return model

    yield {
        "ontology_id": ontology.id,
        "customer_id": customer.id,
        "order_line_id": order_line.id,
        "model": _model,
    }

    for model in created:
        db.delete(model)
    db.query(MaterializationContract).filter(
        MaterializationContract.ontology_id == ontology.id
    ).delete(synchronize_session=False)
    db.delete(customer)
    db.delete(order_line)
    db.delete(ontology)
    db.delete(domain)
    db.commit()


def _contract(db, ontology_id: str, object_id: str) -> MaterializationContract | None:
    return (
        db.query(MaterializationContract)
        .filter(
            MaterializationContract.ontology_id == ontology_id,
            MaterializationContract.target_id == object_id,
        )
        .first()
    )


def test_compile_writes_real_materialization_contracts(db, scaffold):
    """编译产出的是既有的物化契约，不是一堆 status=pending 的占位记录。"""
    model = scaffold["model"](
        dimensions=[{
            "name": "dim_customer",
            "source_object_id": scaffold["customer_id"],
            "surrogate_key": "customer_key",
            "natural_key": ["customer_code"],
            "scd_type": "scd1",
            "attributes": [{"name": "customer_name", "field": "name"}],
        }],
        fact_tables=[{
            "name": "fct_order_line",
            "source_object_id": scaffold["order_line_id"],
            "grain": "每笔订单明细行",
            "measures": [{"name": "amount", "field": "amount", "additive_type": "additive"}],
            "dimension_keys": ["customer_key"],
        }],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 2
    assert not result["unsupported"]

    dim_contract = _contract(db, scaffold["ontology_id"], scaffold["customer_id"])
    assert dim_contract is not None
    assert dim_contract.target_layer == "dim"
    assert dim_contract.scd_type == "scd1"
    assert dim_contract.materialized is True

    fact_contract = _contract(db, scaffold["ontology_id"], scaffold["order_line_id"])
    assert fact_contract is not None
    assert fact_contract.target_layer == "dwd"
    # 事实表不做缓慢变化：历史由粒度本身承载
    assert fact_contract.scd_type == "none"


def test_scd2_dimension_is_incremental_not_full(db, scaffold):
    """SCD2 必须增量装载。

    全量重载会把历史版本冲掉，而留住历史正是 SCD2 的**全部意义**——
    编译成 full 等于生成了一个每晚自毁的缓慢变化维。
    """
    model = scaffold["model"](
        dimensions=[{
            "name": "dim_customer",
            "source_object_id": scaffold["customer_id"],
            "surrogate_key": "customer_key",
            "scd_type": "scd2",
            "scd_config": {
                "effective_date": "valid_from",
                "expiration_date": "valid_to",
                "current_flag": "is_current",
            },
        }],
    )

    SERVICE.compile_model(db, model.id)

    contract = _contract(db, scaffold["ontology_id"], scaffold["customer_id"])
    assert contract.scd_type == "scd2"
    assert contract.load_strategy == "incremental"
    assert contract.partition_key == "valid_from"


def test_degenerate_dimensions_get_no_contract_but_are_reported(db, scaffold):
    """退化维度是事实表上的一列，没有独立维表——不生成契约，但必须留痕。

    静默跳过的话，「说好的 order_id 维度怎么没编译出来」无从对账。
    """
    model = scaffold["model"](
        fact_tables=[{
            "name": "fct_order_line",
            "source_object_id": scaffold["order_line_id"],
            "grain": "每笔订单明细行",
            "measures": [{"name": "amount", "additive_type": "additive"}],
            "degenerate_dimensions": ["order_id", "line_number"],
        }],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 1  # 只有事实表本身
    degenerate = [c for c in result["compiled_contracts"] if c["type"] == "degenerate_dimension"]
    assert {d["name"] for d in degenerate} == {"order_id", "line_number"}
    assert all(d["status"] == "inline" for d in degenerate)


def test_role_playing_dimension_reuses_the_base_table(db, scaffold):
    """order_date / ship_date 指向同一张 dim_date，物理上只有一张表。

    为角色再生成一份契约就会和基础维度抢同一个本体对象，两份契约互相覆盖。
    """
    model = scaffold["model"](
        dimensions=[
            {
                "name": "dim_date",
                "source_object_id": scaffold["customer_id"],  # 借用一个真实对象即可
                "surrogate_key": "date_key",
                "scd_type": "none",
            },
            {
                "name": "order_date",
                "role_playing": {"base_dimension": "dim_date", "role": "order_date"},
            },
        ],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 1
    alias = [c for c in result["compiled_contracts"] if c.get("status") == "alias"]
    assert len(alias) == 1
    assert alias[0]["aliases"] == "dim_date"


def test_generated_dimension_without_an_object_is_unsupported(db, scaffold):
    """日期维这类生成维没有本体对象可挂契约（契约主键就是本体实体）。

    报为 unsupported，而不是假装编译成功。
    """
    model = scaffold["model"](
        dimensions=[{
            "name": "dim_date",
            "source_object_id": None,
            "surrogate_key": "date_key",
            "is_date_dimension": True,
            "scd_type": "none",
        }],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 0
    assert len(result["unsupported"]) == 1
    assert "未绑定本体对象" in result["unsupported"][0]["reason"]


def test_scd_type3_is_reported_not_silently_downgraded(db, scaffold):
    """契约只表达得了 none/scd1/scd2。

    type3 是列内多版本，没有对应落层语义——降级成 scd1 会让人以为历史留住了，
    实际上旧值被就地覆盖。宁可报不支持。
    """
    model = scaffold["model"](
        dimensions=[{
            "name": "dim_customer",
            "source_object_id": scaffold["customer_id"],
            "surrogate_key": "customer_key",
            "scd_type": "scd3",
        }],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 0
    assert "type3" in result["unsupported"][0]["reason"]
    assert _contract(db, scaffold["ontology_id"], scaffold["customer_id"]) is None


def test_two_dimensions_on_one_object_is_a_reported_conflict(db, scaffold):
    """契约按本体实体唯一：两个维度指向同一对象是设计里的真冲突。

    后者覆盖前者会让「同一张表两套 SCD 策略」悄悄变成最后写的那套。
    """
    model = scaffold["model"](
        dimensions=[
            {"name": "dim_a", "source_object_id": scaffold["customer_id"], "scd_type": "scd1"},
            {"name": "dim_b", "source_object_id": scaffold["customer_id"], "scd_type": "scd2"},
        ],
    )

    result = SERVICE.compile_model(db, model.id)

    assert result["contracts_written"] == 1
    assert any("已被" in u["reason"] for u in result["unsupported"])


def test_compile_does_not_overwrite_human_pinned_fields(db, scaffold):
    """编译是机器行为，不该冲掉人在契约页上按过的决定。

    与 MaterializationContractService.sync 同一套三方合并语义。
    """
    import json

    from app.models.warehouse import TargetKind

    existing = MaterializationContract(
        ontology_id=scaffold["ontology_id"],
        target_kind=TargetKind.OBJECT_TYPE.value,
        target_id=scaffold["customer_id"],
        target_layer="ads",                       # 人工改过并钉住
        load_strategy="full",
        scd_type="none",
        materialized=True,
        overridden_fields=json.dumps(["target_layer"]),
    )
    db.add(existing)
    db.commit()

    model = scaffold["model"](
        dimensions=[{
            "name": "dim_customer",
            "source_object_id": scaffold["customer_id"],
            "scd_type": "scd2",
            "scd_config": {"effective_date": "valid_from"},
        }],
    )

    result = SERVICE.compile_model(db, model.id)

    db.refresh(existing)
    assert existing.target_layer == "ads", "被钉住的层不该被编译覆盖"
    assert existing.scd_type == "scd2", "没钉住的字段照常更新"
    entry = next(c for c in result["compiled_contracts"] if c.get("contract_id") == existing.id)
    assert entry["pinned_fields_kept"] == ["target_layer"]


def test_only_confirmed_models_compile(db, scaffold):
    model = scaffold["model"](status="draft")
    with pytest.raises(ValueError, match="已确认"):
        SERVICE.compile_model(db, model.id)


def test_validation_runs_the_real_validator(db, scaffold):
    """校验改为委托 dimensional_model_validator，不再在服务层手写一份薄的。

    这条同时证明适配层是通的：校验器读的是规格词汇，落库的是存储词汇。
    """
    model = scaffold["model"](
        status="draft",
        fact_tables=[
            {
                "name": "fct_dup",
                "source_object_id": scaffold["order_line_id"],
                "measures": [],  # 应报 warning
            },
            {
                "name": "fct_dup",  # 重名 → 校验器报 error
                "source_object_id": scaffold["customer_id"],
                "measures": [{"name": "amount", "additive_type": "additive"}],
            },
        ],
    )

    result = SERVICE.validate_model(db, model.id)

    messages = [i["message"] for i in result["issues"]]
    assert any("名称重复" in m for m in messages), result["issues"]
    assert any("没有定义度量" in m for m in messages), result["issues"]
    assert result["error_count"] >= 1
    # 有 error 就退回 draft：让「已校验」始终意味着「可以确认了」
    assert result["status"] == "draft"


def test_malformed_model_reports_issues_instead_of_500(db, scaffold):
    """存的 JSON 连规格都组不成时，要报成 issue，不能让 500 漏出去。

    事实表缺粒度这类必填项在 schema 层就被 Pydantic 拦下——那**也是**一种校验失败，
    该告诉用户哪一格没填，而不是回一句「服务端内部错误」。
    """
    model = scaffold["model"](
        status="draft",
        grain="",  # 模型级也没有粒度可继承
        fact_tables=[{"name": "fct_no_grain", "source_object_id": scaffold["order_line_id"]}],
    )

    result = SERVICE.validate_model(db, model.id)

    assert result["status"] == "draft"
    assert result["error_count"] >= 1
    assert any(i["category"] == "schema" for i in result["issues"]), result["issues"]
