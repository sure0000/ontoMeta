"""P3 口径编译器：expression_json → 确定性 SQL。

**这是「语义层」与「接了数据库的通用 SQL agent」的分水岭**：没有编译器，模型读一段
文字口径然后凭理解重写 SQL，口径就在那一步丢了；有了编译器，SQL 由本体生成，
同一个指标在问数 / 数据应用 / 物化三处必然一致。

本文件锁三件事：
1. 口径**忠实**编译（聚合算子、自带过滤、自带分组一个都不能丢）；
2. 编译产物**必过语义证明器**——这是「编译器与证明器永不打架」的架构不变式；
3. 编译不了的情况**明确报错并带修复信号**，绝不降级成「猜一个 SQL」。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.metric_compiler import MetricCompileError, compile_metric
from app.services.ontology_projection import build_projection
from app.services.sql_soundness import SqlCertificate, SqlRejection, prove_sql_sound

PUB = EntityStatus.PUBLISHED.value


def _seed() -> dict:
    """order —N:1→ customer；order —N:N→ tag。

    order   : amount[measure] status[categorical] order_date[temporal] customer_id[identifier]
    customer: id[identifier] region[categorical]
    tag     : id[identifier] label[textual]
    """
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mc-{uniq}", name=f"口径域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()

        order = ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                           table_role="business_object", status=PUB)
        customer = ObjectType(ontology_id=onto.id, name="customer", display_name="客户",
                              table_role="business_object", status=PUB)
        tag = ObjectType(ontology_id=onto.id, name="tag", display_name="标签",
                         table_role="business_object", status=PUB)
        db.add_all([order, customer, tag])
        db.flush()

        props = {
            ("order", "amount"): ("金额", "measure", "decimal"),
            ("order", "status"): ("状态", "categorical", "varchar"),
            ("order", "order_date"): ("下单日期", "temporal", "date"),
            ("order", "customer_id"): ("客户ID", "identifier", "bigint"),
            ("customer", "id"): ("ID", "identifier", "bigint"),
            ("customer", "region"): ("区域", "categorical", "varchar"),
            ("tag", "id"): ("ID", "identifier", "bigint"),
            ("tag", "label"): ("标签名", "textual", "varchar"),
        }
        obj_by_name = {"order": order, "customer": customer, "tag": tag}
        ids: dict[str, str] = {}
        for (obj_name, prop_name), (disp, sem, dt) in props.items():
            p = Property(
                object_type_id=obj_by_name[obj_name].id, name=prop_name,
                display_name=disp, semantic_type=sem, data_type=dt, status=PUB,
            )
            db.add(p)
            db.flush()
            ids[f"{obj_name}.{prop_name}"] = p.id

        db.add_all([
            RelationType(
                ontology_id=onto.id, name="order_of_customer", display_name="订单归属客户",
                source_object_type_id=order.id, target_object_type_id=customer.id,
                cardinality="many_to_one", structure_type="foreign_key", status=PUB,
            ),
            RelationType(
                ontology_id=onto.id, name="order_has_tag", display_name="订单打标",
                source_object_type_id=order.id, target_object_type_id=tag.id,
                cardinality="many_to_many", structure_type="bridge_table", status=PUB,
            ),
        ])
        db.commit()
        return {
            "domain_id": domain.id, "onto_id": onto.id,
            "order_id": order.id, "customer_id": customer.id, "tag_id": tag.id,
            "props": ids,
        }


def _ref(rid: str, obj_id: str, obj_name: str, prop_id: str | None, prop_name: str | None) -> dict:
    return {
        "ref_id": rid, "object_type_id": obj_id, "object_name": obj_name,
        "object_display_name": obj_name, "property_id": prop_id,
        "property_name": prop_name, "property_display_name": prop_name,
    }


def _make_logic(env: dict, name: str, ast: dict | None, *, summary: str = "") -> str:
    with SessionLocal() as db:
        logic = BusinessLogic(
            ontology_id=env["onto_id"], name=name, display_name=name,
            logic_type="metric", expression_summary=summary,
            expression_json=json.dumps(ast, ensure_ascii=False) if ast else None,
            status=PUB,
        )
        db.add(logic)
        db.commit()
        return logic.id


def _sum_amount_ast(env: dict, *, with_filter=False, with_group=False) -> dict:
    refs = [_ref("r1", env["order_id"], "order", env["props"]["order.amount"], "amount")]
    body: dict = {"operation": "sum", "args": [{"ref": "r1"}], "filter": None,
                  "group_by": [], "window": None}
    if with_filter:
        refs.append(_ref("r2", env["order_id"], "order", env["props"]["order.status"], "status"))
        body["filter"] = {"left": {"ref": "r2"}, "op": "!=", "right": {"value": "Cancelled"}}
    if with_group:
        refs.append(_ref("r3", env["order_id"], "order", env["props"]["order.status"], "status"))
        body["group_by"] = [{"ref": "r3"}]
    return {"type": "metric", "description": "订单金额求和", "refs": refs, "body": body}


def _compile(logic_id: str, **kw):
    with SessionLocal() as db:
        return compile_metric(db, logic_id, **kw)


# ---------------------------------------------------------------- 忠实编译


def test_simple_sum_compiles(client):
    env = _seed()
    lid = _make_logic(env, "order_total", _sum_amount_ast(env), summary="订单金额求和")
    c = _compile(lid)

    assert 'SUM("order"."amount")' in c.sql
    assert '"order"' in c.sql
    assert c.base_object == "order"
    assert c.certificate["aggregations"] == ["sum(order.amount)"]
    # 口径轨迹是一等交付物：原始口径与聚合都要在里面
    assert any("订单金额求和" in t for t in c.caliber_trace)
    assert any("SUM(order.amount)" in t for t in c.caliber_trace)


def test_own_filter_is_not_dropped(client):
    """**口径一致性的核心**：口径自带的过滤条件不能在编译中丢失。"""
    env = _seed()
    lid = _make_logic(env, "valid_total", _sum_amount_ast(env, with_filter=True))
    c = _compile(lid)

    assert "WHERE" in c.sql and "'Cancelled'" in c.sql
    assert any("Cancelled" in t and "过滤" in t for t in c.caliber_trace)


def test_own_group_by_is_kept(client):
    env = _seed()
    lid = _make_logic(env, "total_by_status", _sum_amount_ast(env, with_group=True))
    c = _compile(lid)

    assert "GROUP BY" in c.sql
    assert c.dimensions == ["order.status"]


def test_count_without_measure_uses_count_star(client):
    env = _seed()
    ast = {
        "type": "metric", "description": "订单笔数",
        "refs": [_ref("r1", env["order_id"], "order", None, None)],
        "body": {"operation": "count", "args": [], "filter": None, "group_by": [], "window": None},
    }
    lid = _make_logic(env, "order_count", ast)
    c = _compile(lid)
    assert "COUNT(*)" in c.sql
    assert c.base_object == "order"


def test_distinct_count(client):
    env = _seed()
    ast = {
        "type": "metric", "description": "下单客户数",
        "refs": [_ref("r1", env["order_id"], "order", env["props"]["order.customer_id"], "customer_id")],
        "body": {"operation": "distinct_count", "args": [{"ref": "r1"}], "filter": None,
                 "group_by": [], "window": None},
    }
    lid = _make_logic(env, "buyer_count", ast)
    assert "COUNT(DISTINCT" in _compile(lid).sql


# ---------------------------------------------------------------- 跨对象维度


def test_cross_object_dimension_joins_via_navigator(client):
    """按「客户.区域」拆分订单金额：JOIN 必须来自语义导航器，不是编译器自己拼的。"""
    env = _seed()
    lid = _make_logic(env, "total_by_region", _sum_amount_ast(env))
    c = _compile(lid, dimensions=["customer.region"])

    assert "JOIN" in c.sql
    assert '"order"."customer_id" = "customer"."id"' in c.sql
    assert c.objects == ["customer", "order"]
    assert c.join_hops and c.join_hops[0]["relation"] == "order_of_customer"
    assert any("订单归属客户" in t for t in c.caliber_trace)
    # N:1 不放大订单行 → 不应有扇出告警
    assert c.fanout_note is None


def test_fanout_dimension_is_refused_with_safe_aggs(client):
    """按 N:N 的标签拆分金额会重复计数——必须拒绝并给出安全聚合建议。"""
    env = _seed()
    lid = _make_logic(env, "total_by_tag", _sum_amount_ast(env))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid, dimensions=["tag.label"])

    assert ei.value.code in ("fanout_risk", "illegal_group_by", "unjoinable")
    if ei.value.code == "fanout_risk":
        assert ei.value.hint.get("safe_aggs")


def test_unjoinable_dimension_refused(client):
    """维度对象与主对象无通路时，宁可编译失败也不臆造关联。"""
    env = _seed()
    # 让 tag 与 order 之间没有任何关系：新建一个孤立对象作维度
    with SessionLocal() as db:
        lonely = ObjectType(ontology_id=env["onto_id"], name="lonely", display_name="孤岛",
                            table_role="business_object", status=PUB)
        db.add(lonely)
        db.flush()
        db.add(Property(object_type_id=lonely.id, name="code", display_name="编码",
                        semantic_type="categorical", data_type="varchar", status=PUB))
        db.commit()

    lid = _make_logic(env, "total_lonely", _sum_amount_ast(env))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid, dimensions=["lonely.code"])
    assert ei.value.code == "unjoinable"


# ---------------------------------------------------------------- 时间粒度


def test_grain_truncates_the_time_field(client):
    env = _seed()
    lid = _make_logic(env, "monthly_total", _sum_amount_ast(env))
    c = _compile(lid, grain="month")

    assert "DATE_TRUNC" in c.sql.upper()
    assert c.grain == "month"
    assert any("时间粒度" in t for t in c.caliber_trace)


def test_ambiguous_time_property_asks_instead_of_guessing(client):
    """有两个时间字段时不许挑一个——要求调用方指定。"""
    env = _seed()
    with SessionLocal() as db:
        db.add(Property(object_type_id=env["order_id"], name="paid_at", display_name="支付时间",
                        semantic_type="temporal", data_type="date", status=PUB))
        db.commit()

    lid = _make_logic(env, "amb_total", _sum_amount_ast(env))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid, grain="month")
    assert ei.value.code == "ambiguous_time_property"
    assert len(ei.value.hint["candidates"]) == 2


def test_non_temporal_time_property_refused(client):
    env = _seed()
    lid = _make_logic(env, "bad_grain", _sum_amount_ast(env))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid, grain="month", time_property="order.status")
    assert ei.value.code == "bad_time_property"


# ---------------------------------------------------------------- 调用方过滤


def test_caller_filter_is_parameterised_not_concatenated(client):
    """字面量走 sqlglot 构造：注入串只会变成**一个普通字符串常量**。

    判据不能是「SQL 文本里没有 DROP TABLE」——转义后的字面量当然含有这些字符。
    真正要证的是：整条 SQL 仍**只解析出一条 SELECT**，且注入串原样落在一个
    Literal 节点里（没有逃逸成语法结构）。
    """
    import sqlglot
    from sqlglot import exp

    env = _seed()
    lid = _make_logic(env, "inject_total", _sum_amount_ast(env))
    evil = "x'; DROP TABLE customer; --"
    c = _compile(lid, filters=[{"property": "order.status", "op": "=", "value": evil}])

    statements = [s for s in sqlglot.parse(c.sql) if s is not None]
    assert len(statements) == 1 and isinstance(statements[0], exp.Select)
    literals = [
        node.this for node in statements[0].find_all(exp.Literal) if node.is_string
    ]
    assert evil in literals, literals            # 原样成为一个字符串常量
    assert not list(statements[0].find_all(exp.Drop))
    assert isinstance(prove_sql_sound(c.sql, _proj(env)), SqlCertificate)


def test_caller_filter_in_operator(client):
    env = _seed()
    lid = _make_logic(env, "in_total", _sum_amount_ast(env))
    c = _compile(
        lid, filters=[{"property": "order.status", "op": "in", "values": ["Completed", "Draft"]}]
    )
    assert " IN (" in c.sql and "'Completed'" in c.sql and "'Draft'" in c.sql


def test_caller_filter_missing_value_refused(client):
    env = _seed()
    lid = _make_logic(env, "novalue_total", _sum_amount_ast(env))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid, filters=[{"property": "order.status", "op": "="}])
    assert ei.value.code == "incomplete_filter"


# ---------------------------------------------------------------- 拒绝与信号


def test_logic_without_expression_json_refused(client):
    """只有文字口径、未形式化 → 明确报错，不许「照着摘要猜一条 SQL」。"""
    env = _seed()
    lid = _make_logic(env, "text_only", None, summary="按业务口径统计的活跃客户")
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "no_expression"
    assert "活跃客户" in ei.value.hint["expression_summary"]


def test_unresolved_property_refused(client):
    """口径引用的字段已被下线：宁可整条失败，也不能悄悄换个字段算出一个数。"""
    env = _seed()
    ast = _sum_amount_ast(env)
    ast["refs"][0]["property_name"] = "gone_field"
    lid = _make_logic(env, "stale_total", ast)
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "unresolved_property"
    assert "amount" in ei.value.hint["available_columns"]


def test_illegal_aggregation_refused(client):
    env = _seed()
    ast = _sum_amount_ast(env)
    ast["refs"][0]["property_name"] = "status"      # 对分类字段 SUM
    ast["refs"][0]["property_id"] = env["props"]["order.status"]
    lid = _make_logic(env, "sum_status", ast)
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "illegal_aggregation"


def test_unknown_logic_type_refused(client):
    env = _seed()
    ast = _sum_amount_ast(env)
    ast["type"] = "workflow"          # metric/tag/rule 之外的类型
    lid = _make_logic(env, "a_workflow", ast)
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "unsupported_logic_type"
    assert ei.value.hint["supported"] == ["metric", "tag", "rule"]


# ---------------------------------------------------------------- tag / rule（P3.5）


def _tag_ast(env: dict, *, labelled: bool = True) -> dict:
    """标签：金额 >= 1000 记「高价值」，否则「普通」。"""
    hi = {"value": "高价值"} if labelled else {"value": None}
    lo = {"value": "普通"} if labelled else {"value": None}
    return {
        "type": "tag",
        "description": "订单价值分层",
        "refs": [_ref("r1", env["order_id"], "order", env["props"]["order.amount"], "amount")],
        "body": {
            "cases": [
                {"when": {"left": {"ref": "r1"}, "op": ">=", "right": {"value": 1000}},
                 "then": hi},
                {"when": None, "then": lo},
            ]
        },
    }


def _rule_ast(env: dict) -> dict:
    """规则：订单金额必须 > 0。"""
    return {
        "type": "rule",
        "description": "金额必须为正",
        "refs": [_ref("r1", env["order_id"], "order", env["props"]["order.amount"], "amount")],
        "body": {
            "condition": {"left": {"ref": "r1"}, "op": ">", "right": {"value": 0}},
            "message": "订单金额必须大于 0",
        },
    }


def test_tag_compiles_to_bucket_distribution(client):
    """标签编译成**分布查询**：各标签取值各多少行。"""
    env = _seed()
    lid = _make_logic(env, "order_tier", _tag_ast(env))
    c = _compile(lid)

    assert c.logic_type == "tag"
    assert "CASE" in c.sql.upper()
    assert "'高价值'" in c.sql and "'普通'" in c.sql
    assert "GROUP BY" in c.sql and "COUNT(*)" in c.sql
    assert any("标签分桶" in t for t in c.caliber_trace)


def test_tag_without_labels_refused(client):
    """各分支都没有标签值 → 编出来只是一列 NULL，不如明确报错。"""
    env = _seed()
    lid = _make_logic(env, "blank_tier", _tag_ast(env, labelled=False))
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "incomplete_tag"


def test_tag_with_extra_dimension(client):
    """标签可与维度交叉：各状态下各分层各多少。"""
    env = _seed()
    lid = _make_logic(env, "tier_by_status", _tag_ast(env))
    c = _compile(lid, dimensions=["order.status"])
    assert "CASE" in c.sql.upper() and '"order"."status"' in c.sql
    assert c.dimensions == ["order.status"]


def test_rule_counts_violations_not_conformers(client):
    """规则统计的是**不满足**条件的行——规则本身成立没有信息量，违规才有。"""
    env = _seed()
    lid = _make_logic(env, "amount_positive", _rule_ast(env))
    c = _compile(lid)

    assert c.logic_type == "rule"
    assert "NOT" in c.sql.upper()
    assert "COUNT(*)" in c.sql and "violations" in c.sql
    assert any("不满足" in t for t in c.caliber_trace)
    assert any("订单金额必须大于 0" in t for t in c.caliber_trace)


def test_rule_without_condition_refused(client):
    env = _seed()
    ast = _rule_ast(env)
    ast["body"]["condition"] = None
    lid = _make_logic(env, "empty_rule", ast)
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "bad_expression"


def test_rule_violations_by_dimension(client):
    env = _seed()
    lid = _make_logic(env, "violations_by_status", _rule_ast(env))
    c = _compile(lid, dimensions=["order.status"])
    assert "GROUP BY" in c.sql and "NOT" in c.sql.upper()


def test_tag_over_measure_is_not_illegal_group_by(client):
    """**架构不变式**：按度量**分桶**是合法的，按度量**原值**分组才是口径错误。

    证明器原先递归收集 GROUP BY 里的所有列，会把 `GROUP BY CASE WHEN amount…`
    里的 amount 当成分组键而拒掉——整类标签口径都编不出来。
    """
    env = _seed()
    lid = _make_logic(env, "tier_invariant", _tag_ast(env))
    c = _compile(lid)   # 编译内部已自证；这里再独立证一次
    assert isinstance(prove_sql_sound(c.sql, _proj(env)), SqlCertificate)

    # 对照：按度量原值分组仍须被拒
    r = prove_sql_sound(
        'SELECT "order"."amount", COUNT(*) FROM "order" GROUP BY "order"."amount"',
        _proj(env),
    )
    assert isinstance(r, SqlRejection) and r.code == "illegal_group_by"


def test_unpublished_logic_refused(client):
    env = _seed()
    lid = _make_logic(env, "draft_metric", _sum_amount_ast(env))
    with SessionLocal() as db:
        db.get(BusinessLogic, lid).status = "edited"
        db.commit()
    with pytest.raises(MetricCompileError) as ei:
        _compile(lid)
    assert ei.value.code == "logic_not_found"


# ---------------------------------------------------------------- 架构不变式


def _proj(env: dict):
    with SessionLocal() as db:
        return build_projection(db, env["onto_id"], None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"dimensions": ["order.status"]},
        {"dimensions": ["customer.region"]},
        {"grain": "month"},
        {"dimensions": ["customer.region"], "grain": "day"},
        {"filters": [{"property": "order.status", "op": "=", "value": "Completed"}]},
    ],
)
def test_every_compiled_output_is_certified(client, kwargs):
    """**架构不变式**：编译器产出的每一条 SQL 都必须过证明器。

    自证失败只有两种可能——编译器有 bug，或这条口径本就无法安全表达成查询。
    两者都必须当场暴露，而不是让 Agent 拿着一条会被守卫拒掉的 SQL 去撞。
    """
    env = _seed()
    lid = _make_logic(env, "inv_total", _sum_amount_ast(env, with_filter=True))
    c = _compile(lid, **kwargs)

    # 编译器自己已经证过一遍（certificate 非空即为证），这里再独立证一次
    assert c.certificate["tables"]
    verdict = prove_sql_sound(c.sql, _proj(env))
    assert isinstance(verdict, SqlCertificate), getattr(verdict, "message", verdict)


def test_reserved_word_object_is_quoted(client):
    """``order`` 是保留字：编译产物必须带方言引号，否则一执行就语法错。"""
    env = _seed()
    lid = _make_logic(env, "quoted_total", _sum_amount_ast(env))
    for dialect in ("sqlite", "postgres", "mysql"):
        sql = _compile(lid, dialect=dialect).sql
        assert "order" in sql
        assert 'FROM "order"' in sql or "FROM `order`" in sql, sql
