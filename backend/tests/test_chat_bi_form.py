"""request_form 工具的 dispatch 校验单测（P6 交互表单）。

盯住 `_dispatch_request_form` 的归一与不变式：
1. 缺 title / 空 fields → 错误态（返回 is_error=True，不产出表单）；
2. 非法字段（缺 name/label、type 非法、name 重复）被丢弃，全丢光 → 错误态；
3. 选项类字段无候选项 → 退化为文本输入（避免出一个点不动的空下拉）；
4. 合法表单归一出干净 spec（title/fields[+intent/submit_label]），是**终态出口**产物。
"""

from __future__ import annotations

from app.services.chat_bi import ChatBiService
from app.services.chat_bi_blocks import answer_to_blocks
from tests.fixtures.golden_questions import ToolTurn
from tests.test_chat_bi_golden import _StubCompletions, _seed_golden_domain
from tests.test_chat_bi_intent_gate import _ask, _make_service

def _dispatch(args: dict):
    """无 task_kind 的通用表单不碰库，给个占位 session 即可（模板分支不会走到）。"""
    from app.database import SessionLocal

    with SessionLocal() as db:
        return ChatBiService()._dispatch_request_form(db, ontology_id="onto-x", args=args)


def test_missing_title_is_error():
    result, _summary, is_error = _dispatch({"fields": [{"name": "a", "label": "A", "type": "text"}]})
    assert is_error is True
    assert "title" in result["error"]


def test_empty_fields_is_error():
    result, _summary, is_error = _dispatch({"title": "t", "fields": []})
    assert is_error is True
    assert "form" not in result


def test_all_invalid_fields_is_error():
    """字段全非法（缺 label / type 非法）→ 无有效字段，报错而不产出空表单。"""
    result, _summary, is_error = _dispatch(
        {"title": "t", "fields": [{"name": "a", "type": "text"}, {"name": "b", "label": "B", "type": "bogus"}]}
    )
    assert is_error is True


def test_valid_form_normalizes_spec():
    result, summary, is_error = _dispatch(
        {
            "title": "确认取数需求",
            "intent": "需要指标与时间范围",
            "submit_label": "开始取数",
            "fields": [
                {"name": "metric", "label": "指标", "type": "select",
                 "options": ["GMV", "客单价"], "required": True},
                {"name": "range", "label": "时间范围", "type": "text", "placeholder": "如近30天"},
            ],
        }
    )
    assert is_error is False
    form = result["form"]
    assert form["title"] == "确认取数需求"
    assert form["intent"] == "需要指标与时间范围"
    assert form["submit_label"] == "开始取数"
    assert [f["name"] for f in form["fields"]] == ["metric", "range"]
    assert form["fields"][0]["required"] is True
    # 纯字符串候选归一为 {label, value}（label == value），老写法仍然合法。
    assert form["fields"][0]["options"] == [
        {"label": "GMV", "value": "GMV"}, {"label": "客单价", "value": "客单价"}
    ]
    assert form["fields"][1]["placeholder"] == "如近30天"
    assert "2 项" in summary


def test_select_without_options_degrades_to_text():
    """select/radio 无候选项 → 退化 text；multiselect 无候选项 → 退化 textarea。"""
    result, _summary, is_error = _dispatch(
        {
            "title": "t",
            "fields": [
                {"name": "a", "label": "A", "type": "select"},
                {"name": "b", "label": "B", "type": "multiselect", "options": []},
                {"name": "c", "label": "C", "type": "radio", "options": ["x"]},
            ],
        }
    )
    assert is_error is False
    types = {f["name"]: f["type"] for f in result["form"]["fields"]}
    assert types == {"a": "text", "b": "textarea", "c": "radio"}
    assert "options" not in result["form"]["fields"][0]  # 退化后不带空 options


def test_duplicate_field_names_deduped():
    result, _summary, is_error = _dispatch(
        {
            "title": "t",
            "fields": [
                {"name": "a", "label": "首个", "type": "text"},
                {"name": "a", "label": "重复", "type": "number"},
            ],
        }
    )
    assert is_error is False
    fields = result["form"]["fields"]
    assert len(fields) == 1
    assert fields[0]["label"] == "首个"  # 首次为准


def test_request_form_ends_turn_and_projects_form_block():
    """端到端：模型调 request_form → 本轮到此为止，终态 payload 带 form_request，投影出 form 块。

    这是交互表单的**终态出口**回归——与澄清同构（break 出循环、不作答、不接地校验），
    钉住「表单请求 = 先收集再答，而非拒答」这条链路。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    script = [
        ToolTurn([("request_form", {
            "title": "确认取数需求",
            "intent": "需要指标与时间范围",
            "fields": [
                {"name": "metric", "label": "指标", "type": "select", "options": ["GMV"]},
                {"name": "range", "label": "时间范围", "type": "text"},
            ],
        })]),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    payload = _ask(service, domain_id, "帮我取个数")

    assert payload.get("form_request"), "终态 payload 应携带 form_request"
    assert payload["form_request"]["title"] == "确认取数需求"
    assert payload.get("clarification") is None
    assert payload.get("grounding_refused") is not True  # 表单不是拒答
    # blocks 由 API 层投影；service.ask 不含 blocks，这里就地投影验证。
    types = [b["type"] for b in answer_to_blocks(payload)]
    assert "form" in types
    assert "markdown" not in types



# ---------------- P2：建数任务表单走服务端模板 ----------------


def _materialize_form(
    datasource_id: str = "",
    extra: list | None = None,
    prefill: dict | None = None,
    intent: str = "",
):
    """建一个可写数据源 + 对齐契约，产出物化表单模板。返回 (form, cleanup 已完成)。"""
    from app.database import SessionLocal
    from app.models import DataSource
    from app.services.materialization_contract import MaterializationContractService

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id="ds-tpl", name="默认 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok",
            dsn_secret_ref="ref://tpl",
        ))
        db.commit()
        try:
            MaterializationContractService().sync(db, onto_id)
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db, ontology_id=onto_id,
                args={"title": "物化参数", "task_kind": "materialize",
                      "target_datasource_id": datasource_id,
                      **({"intent": intent} if intent else {}),
                      **({"fields": extra} if extra else {}),
                      **({"prefill": prefill} if prefill else {})},
            )
        finally:
            db.query(DataSource).filter(DataSource.id == "ds-tpl").delete()
            if old_defaults:
                db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                    {DataSource.is_default_warehouse: True}, synchronize_session=False
                )
            db.commit()
    assert is_error is False, result
    return result["form"]


def test_materialize_template_only_asks_effective_fields():
    """物化只建结构：表单只收执行器真正消费的范围、默认 Doris 和目标库。"""
    form = _materialize_form()
    names = [f["name"] for f in form["fields"]]
    assert names == [
        "task_requirement", "selected_targets", "target_datasource_id", "target_database",
    ]
    by_name = {f["name"]: f for f in form["fields"]}
    assert by_name["target_datasource_id"]["type"] == "select"
    # 目录会如实包含其它未设为默认的 Doris（disabled）；当前唯一可执行默认项必须正确。
    selected = next(
        option
        for option in by_name["target_datasource_id"]["options"]
        if option["value"] == "ds-tpl"
    )
    assert selected == {
        "label": "默认 Doris（默认 Doris）",
        "value": "ds-tpl",
        "disabled": False,
    }
    assert by_name["target_datasource_id"]["default"] == "ds-tpl"
    assert by_name["selected_targets"]["type"] == "multiselect"
    # 物化不搬数据，这些属于同步或旧版无效字段，不能再出现。
    assert not {"target_table", "load_strategy", "partition_key", "refresh_cron"} & set(names)
    assert [s["node"] for s in form["confirmation_steps"]] == [
        "requirement", "ontology", "data", "plan", "execute", "result"
    ]
    assert [s["phase"] for s in form["confirmation_steps"]] == [
        "form", "form", "form", "artifact", "artifact", "artifact"
    ]


def test_materialize_ignores_obsolete_model_fields():
    """模型不能通过 extra fields 把已移除的无效物化字段偷偷加回来。"""
    form = _materialize_form(extra=[
        {"name": "target_table", "label": "目标表", "type": "text"},
        {"name": "load_strategy", "label": "装载方式", "type": "radio", "options": ["full"]},
        {"name": "partition_key", "label": "分区键", "type": "text"},
        {"name": "refresh_cron", "label": "调度", "type": "cron"},
    ])
    names = {f["name"] for f in form["fields"]}
    assert not {"target_table", "load_strategy", "partition_key", "refresh_cron"} & names


def test_task_template_appends_supported_model_fields_without_overriding():
    """额外有效字段可追加；骨架同名字段仍不能覆盖。"""
    form = _materialize_form(extra=[
        {"name": "target_database", "label": "模型想改的库", "type": "text"},
        {"name": "comment", "label": "备注", "type": "textarea"},
    ])
    by_name = {f["name"]: f for f in form["fields"]}
    assert by_name["target_database"]["label"] == "目标数据库"
    assert form["fields"][-1]["name"] == "comment"


def test_transform_template_offers_cleansing_vocabulary():
    """加工表单把清洗规则做成多选——规则是闭集，让用户在词表里选而不是自由描述。"""
    from app.database import SessionLocal

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        result, _s, is_error = ChatBiService()._dispatch_request_form(
            db, ontology_id=onto_id,
            args={"title": "加工参数", "task_kind": "transform"},
        )
    assert is_error is False
    by_name = {f["name"]: f for f in result["form"]["fields"]}
    assert by_name["target_table"]["type"] == "select"  # 不再靠 select_by_intent 猜
    assert by_name["cleansing_rules"]["type"] == "multiselect"
    # 规则码进 value（Drafter 认的就是它），中文说明进 label
    assert any("去重" in o["label"] for o in by_name["cleansing_rules"]["options"])
    assert all(o["value"] for o in by_name["cleansing_rules"]["options"])


def test_transform_template_uses_default_doris_dropdown():
    """加工目标不能手填任意 DataSource；默认 Doris 唯一合法并自动选中。"""
    from uuid import uuid4

    from app.database import SessionLocal
    from app.models import DataSource

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    target_id = f"doris-transform-{uuid4().hex[:8]}"
    with SessionLocal() as db:
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id=target_id, name="分析 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref="ref://doris",
        ))
        db.commit()
        result, _s, is_error = ChatBiService()._dispatch_request_form(
            db, ontology_id=onto_id,
            args={"title": "加工订单", "task_kind": "transform", "intent": "清洗订单"},
        )
        db.query(DataSource).filter(DataSource.id == target_id).delete()
        if old_defaults:
            db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                {DataSource.is_default_warehouse: True}, synchronize_session=False
            )
        db.commit()
    assert is_error is False
    by_name = {f["name"]: f for f in result["form"]["fields"]}
    assert by_name["target_datasource_id"]["type"] == "select"
    assert by_name["target_datasource_id"]["default"] == target_id
    assert by_name["target_layer"]["default"] == "dim"
    assert result["form"]["confirmation_id"]


def test_metric_template_selects_formal_logic_not_object():
    """聚合任务选择已形式化业务口径，不复用对象模板。"""
    from uuid import uuid4

    from app.database import SessionLocal
    from app.models import DataSource

    _domain_id, onto_id, aliases = _seed_golden_domain()
    target_id = f"doris-metric-{uuid4().hex[:8]}"
    with SessionLocal() as db:
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id=target_id, name="指标 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref="ref://doris",
        ))
        db.commit()
        result, _s, is_error = ChatBiService()._dispatch_request_form(
            db, ontology_id=onto_id,
            args={"title": "聚合订单总额", "task_kind": "metric", "intent": "计算订单总额"},
        )
        db.query(DataSource).filter(DataSource.id == target_id).delete()
        if old_defaults:
            db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                {DataSource.is_default_warehouse: True}, synchronize_session=False
            )
        db.commit()
    assert is_error is False
    by_name = {f["name"]: f for f in result["form"]["fields"]}
    assert "business_logic_id" in by_name and "object_type" not in by_name
    assert by_name["business_logic_id"]["type"] == "select"
    assert by_name["business_logic_id"]["default"] == aliases["@order_total"]
    assert by_name["target_datasource_id"]["default"] == target_id
    assert by_name["target_layer"]["options"] == [{"label": "应用层 ADS", "value": "ads"}]


def test_sync_template_recommends_ontology_and_keeps_all_objects_searchable():
    """同步本体按意图默认推荐，但完整候选仍可搜索和修改。

    工具目录为控制 LLM 上下文只返回前 30 条；人操作的表单不能沿用该截断，否则搜索框
    永远搜不到排在后面的对象。
    """
    from uuid import uuid4

    from app.database import SessionLocal
    from app.models import DataSource, ObjectType

    _domain_id, onto_id, aliases = _seed_golden_domain()
    pg_id = f"ds-form-pg-{uuid4().hex[:8]}"
    mysql_id = f"ds-form-mysql-{uuid4().hex[:8]}"
    with SessionLocal() as db:
        order = db.get(ObjectType, aliases["@order"])
        order.source_ref = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,erp.public.order,PROD)"
        )
        db.add(ObjectType(
            ontology_id=onto_id,
            name="sale_order",
            display_name="销售订单",
            description="销售订单主表",
            source_ref=(
                "urn:li:dataset:(urn:li:dataPlatform:postgres,"
                "erp.public.sale_order,PROD)"
            ),
            status=order.status,
        ))
        # 超过工具目录的 30 条，钉住表单不截断以及后排对象可以被前端本地搜索。
        for i in range(35):
            db.add(ObjectType(
                ontology_id=onto_id,
                name=f"sync_object_{i:02d}",
                display_name=f"同步对象{i:02d}",
                source_ref=(
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
                    f"erp.public.sync_object_{i:02d},PROD)"
                ),
                status=order.status,
            ))
        db.add_all([
            DataSource(
                id=pg_id, name="ERP PostgreSQL", kind="postgres",
                purpose="business_source", enabled=True, status="ok",
                dsn_secret_ref="postgresql://reader@db/erp",
            ),
            DataSource(
                id=mysql_id, name="其它 MySQL", kind="mysql",
                purpose="business_source", enabled=True, status="ok",
                dsn_secret_ref="mysql://reader@db/other",
            ),
        ])
        db.commit()

        result, _s, is_error = ChatBiService()._dispatch_request_form(
            db,
            ontology_id=onto_id,
            args={
                "title": "同步参数",
                "task_kind": "sync",
                "intent": "同步 sale_order 到数仓",
            },
        )
        db.query(DataSource).filter(DataSource.id.in_([pg_id, mysql_id])).delete(
            synchronize_session=False
        )
        db.commit()

    assert is_error is False
    field = {f["name"]: f for f in result["form"]["fields"]}["object_type"]
    assert field["label"] == "确认同步本体"
    assert field["placeholder"] == "搜索中文名或技术名"
    assert field["default"] == "sale_order"
    assert len(field["options"]) == 37
    sale = next(o for o in field["options"] if o["value"] == "sale_order")
    assert "销售订单（sale_order）" in sale["label"]
    assert "erp.public.sale_order" in sale["label"]
    assert "ods_golden_" in sale["label"] and sale["label"].endswith("_sale_order")
    assert any(o["value"] == "sync_object_34" for o in field["options"])
    # 闭环向导：六环一次给全，人从第一步就看得见还剩几环。前三环在表单里收集
    # （phase=form），后三环等制品 dry-run 出来后在任务详情里确认（phase=artifact）——
    # 故这里不能提前记 plan。
    assert result["form"]["confirmation_id"]
    assert [s["node"] for s in result["form"]["confirmation_steps"]] == [
        "requirement", "ontology", "data", "plan", "execute", "result"
    ]
    assert [
        s["node"] for s in result["form"]["confirmation_steps"] if s["phase"] == "form"
    ] == ["requirement", "ontology", "data"]
    by_name = {f["name"]: f for f in result["form"]["fields"]}
    assert by_name["object_type"]["confirmation_node"] == "ontology"
    source_field = by_name["source_datasource_id"]
    assert source_field["confirmation_node"] == "data"
    assert source_field["depends_on"] == "object_type"
    assert source_field["default"] == pg_id
    assert source_field["options_by_value"]["sale_order"] == [
        {"label": "ERP PostgreSQL（postgres）", "value": pg_id}
    ]
    assert all(o["value"] != mysql_id for o in source_field["options_by_value"]["sale_order"])
    assert by_name["target_datasource_id"]["confirmation_node"] == "data"
    # 落点不给选：同步恒写 ODS 库，表单里不该再出现「ODS 数据库」这类分层/落库选项。
    assert "target_ods_database" not in by_name
    assert "database_prefix" not in by_name
    assert by_name["mode"]["default"] == "full"


def test_sync_intent_cannot_be_misrouted_to_materialize_form():
    """模型把同步误标为 materialize 时，服务端仍应返回源对象同步表单。"""
    from app.database import SessionLocal
    from app.models import ObjectType

    _domain_id, onto_id, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        order = db.get(ObjectType, aliases["@order"])
        old_source_ref = order.source_ref
        order.source_ref = (
            "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tabSales Order,PROD)"
        )
        db.commit()
        try:
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db,
                ontology_id=onto_id,
                args={
                    "title": "同步订单到数仓",
                    "task_kind": "materialize",
                    "intent": "将订单源表数据全量同步到 Doris 数仓",
                },
            )
        finally:
            order.source_ref = old_source_ref
            db.commit()

    assert is_error is False
    form = result["form"]
    assert form["task_kind"] == "sync"
    by_name = {field["name"]: field for field in form["fields"]}
    assert "object_type" in by_name
    assert "selected_targets" not in by_name
    assert by_name["object_type"]["default"] == "order"


def test_materialize_before_sync_collapses_into_the_sync_form():
    """「先物化再同步」只出同步表单——同步自己会幂等建出 ODS 表，物化那一步是多余的。

    此前文本同时出现「物化」和「同步」时保持原值，于是用户为了同步一张表，先被要求
    确认一次「物化范围」，而那份范围默认整本体几百个实体。
    """
    from app.database import SessionLocal
    from app.models import ObjectType

    _domain_id, onto_id, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        order = db.get(ObjectType, aliases["@order"])
        old_source_ref = order.source_ref
        order.source_ref = (
            "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tabSales Order,PROD)"
        )
        db.commit()
        try:
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db,
                ontology_id=onto_id,
                args={
                    "title": "任务1/2：物化「订单」到数仓",
                    "task_kind": "materialize",
                    "intent": "先把订单物化到数仓，再把订单同步进 ODS",
                },
            )
        finally:
            order.source_ref = old_source_ref
            db.commit()

    assert is_error is False
    form = result["form"]
    assert form["task_kind"] == "sync"
    assert "selected_targets" not in {field["name"] for field in form["fields"]}
    assert [s["title"] for s in form["confirmation_steps"] if s["node"] == "ontology"] == [
        "确认同步本体"
    ]


def test_materialize_step_collapses_when_only_the_user_said_sync():
    """模型给子任务起的标题只写「物化 X」，用户原话里的「再同步」同样算数。

    这是实测里真实发生的形状：模型把整句需求拆成「任务1/2：物化…」，标题与 intent
    都不含「同步」，判据只看工具入参就永远看不到那一步是在为同步做准备。
    """
    from app.database import SessionLocal
    from app.models import ObjectType

    _domain_id, onto_id, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        order = db.get(ObjectType, aliases["@order"])
        old_source_ref = order.source_ref
        order.source_ref = (
            "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tabSales Order,PROD)"
        )
        db.commit()
        try:
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db,
                ontology_id=onto_id,
                args={
                    "title": "任务1/2：物化「订单」到数仓",
                    "task_kind": "materialize",
                    "intent": "将订单(order)物化到数仓 Doris",
                },
                question="先把订单物化到数仓，再把订单同步进 ODS",
            )
        finally:
            order.source_ref = old_source_ref
            db.commit()

    assert is_error is False
    form = result["form"]
    assert form["task_kind"] == "sync"
    # 改判要说出来，标题里的「物化」也得改口——否则人看到的是一张写着「物化…」的同步表单。
    assert "CREATE TABLE IF NOT EXISTS" in form["notice"]
    assert "物化" not in form["title"]
    assert "物化" not in form.get("intent", "")


def test_materialize_stays_for_objects_without_a_physical_source():
    """没有源表的对象不能改判成同步——那种表只能靠物化建出来。

    改判的判据是证据（对象有没有物理源表），不是「文本里出现了同步两个字」。
    """
    from app.database import SessionLocal
    from app.models import DataSource, ObjectType
    from app.services.materialization_contract import MaterializationContractService

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        # 整个本体都没有物理源表 = 纯人工建模，同步无从谈起。
        db.query(ObjectType).filter(ObjectType.ontology_id == onto_id).update(
            {ObjectType.source_ref: None}, synchronize_session=False
        )
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id="ds-manual", name="默认 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref="ref://m",
        ))
        db.commit()
        try:
            MaterializationContractService().sync(db, onto_id)
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db,
                ontology_id=onto_id,
                args={
                    "title": "物化订单",
                    "task_kind": "materialize",
                    "intent": "把订单物化到数仓",
                },
                question="先把订单物化出来，再同步进 ODS",
            )
        finally:
            db.query(DataSource).filter(DataSource.id == "ds-manual").delete()
            if old_defaults:
                db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                    {DataSource.is_default_warehouse: True}, synchronize_session=False
                )
            db.commit()

    assert is_error is False
    assert result["form"]["task_kind"] == "materialize"


def test_materialize_scope_defaults_to_the_entity_the_intent_names():
    """需求点名了实体，物化范围就默认成它——不是「全部契约实体（几百项）」。

    默认全选 + 一路确认 = 人以为在物化一张表、实际建了整本体几百张。
    """
    form = _materialize_form(intent="将客户分组(customer_group)物化到数仓")
    scope = {field["name"]: field for field in form["fields"]}["selected_targets"]
    assert scope["default"] != ["__all__"]
    assert len(scope["default"]) == 1
    assert any(
        option["value"] == scope["default"][0] and option["value"] != "__all__"
        for option in scope["options"]
    )


def test_search_objects_uses_selected_ontology_and_matches_physical_table_name():
    """无 domain_ids 时仍按 ontology_id 搜；中文、技术名和 source_ref 都可命中。"""
    from uuid import uuid4

    from app.database import SessionLocal
    from app.models import EntityStatus, ObjectType

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    suffix = uuid4().hex[:8]
    object_name = f"code_list_{suffix}"
    with SessionLocal() as db:
        obj = ObjectType(
            ontology_id=onto_id,
            name=object_name,
            display_name=f"代码表目录{suffix}",
            source_ref=(
                "urn:li:dataset:(urn:li:dataPlatform:mysql,"
                f"erp.tabCode List {suffix},PROD)"
            ),
            status=EntityStatus.PUBLISHED.value,
        )
        db.add(obj)
        db.commit()

        service = ChatBiService()
        for keyword in (object_name, f"代码表目录{suffix}", f"tabCode List {suffix}"):
            result, _summary, is_error = service._dispatch_agent_tool(
                db,
                domain_ids=[],
                ontology_ids=[onto_id],
                name="search_objects",
                args={"keyword": keyword},
            )
            assert is_error is False
            hits = result.get("items") or result.get("sample") or []
            assert any(item["name"] == object_name for item in hits), keyword

        all_result, _summary, is_error = service._dispatch_agent_tool(
            db,
            domain_ids=[],
            ontology_ids=[onto_id],
            name="search_objects",
            args={"keyword": "*"},
        )
        assert is_error is False
        assert all_result["total_matched"] > 0


def test_task_kind_form_needs_no_model_fields():
    """带 task_kind 时 fields 可以完全不给——骨架本身就够了。"""
    form = _materialize_form()
    assert len(form["fields"]) == 4


def test_materialize_database_is_real_dropdown_when_catalog_available():
    """默认 Doris 可列库时，目标数据库使用真实候选下拉，不让用户猜。"""
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form()
    database = {f["name"]: f for f in form["fields"]}["target_database"]
    assert database["type"] == "select"
    assert database["options"] == [
        {"label": "dw", "value": "dw"}, {"label": "ods", "value": "ods"}
    ]


def test_materialize_prefill_uses_real_database_candidate():
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form(prefill={"target_database": "dw"})
    database = {f["name"]: f for f in form["fields"]}["target_database"]
    assert database["default"] == "dw"


def test_materialize_prefill_drops_unknown_database():
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form(prefill={"target_database": "not_exists"})
    database = {f["name"]: f for f in form["fields"]}["target_database"]
    assert "default" not in database


def test_scalar_target_database_reaches_the_spec():
    """人说的「物化到 dw 库」要真的生效。

    执行侧只认「层 → 库名」，而对话（和物化弹窗）说的是一个不分层的目标库。此前表单收上来
    的 target_database 在 Drafter 里被静默丢掉，表落回默认库——选了等于没选。
    """
    from app.agents.drafters.materialize import MaterializeDrafter

    spec = MaterializeDrafter().draft(
        "物化到数仓",
        {"ontology_id": "onto-1", "target_datasource_id": "ds-1", "target_database": "dw"},
    )
    assert spec["database_overrides"] == {"dim": "dw", "dwd": "dw", "dws": "dw", "ads": "dw"}
    # 逐层显式给的优先，不被标量覆盖
    spec2 = MaterializeDrafter().draft(
        "物化到数仓",
        {"ontology_id": "onto-1", "target_datasource_id": "ds-1",
         "target_database": "dw", "database_overrides": {"ads": "app"}},
    )
    assert spec2["database_overrides"]["ads"] == "app"
    assert spec2["database_overrides"]["dim"] == "dw"
