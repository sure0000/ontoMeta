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
    assert form["fields"][0]["options"] == ["GMV", "客单价"]
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


def _materialize_form(datasource_id: str = "", extra: list | None = None):
    """建一个可写数据源 + 对齐契约，产出物化表单模板。返回 (form, cleanup 已完成)。"""
    from app.database import SessionLocal
    from app.models import DataSource
    from app.services.materialization_contract import MaterializationContractService

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        db.add(DataSource(id="ds-tpl", name="仓库", kind="hive", status="ok",
                          dsn_secret_ref="ref://tpl"))
        db.commit()
        try:
            MaterializationContractService().sync(db, onto_id)
            result, _summary, is_error = ChatBiService()._dispatch_request_form(
                db, ontology_id=onto_id,
                args={"title": "物化参数", "task_kind": "materialize",
                      "target_datasource_id": datasource_id,
                      **({"fields": extra} if extra else {})},
            )
        finally:
            # 带 dsn 的源会被 run_sql 的数据源解析选走，串到别的用例
            db.query(DataSource).filter(DataSource.id == "ds-tpl").delete()
            db.commit()
    assert is_error is False, result
    return result["form"]


def test_materialize_template_asks_every_required_param():
    """物化表单的必问字段由服务端出——模型漏问装载方式/分区键这件事不该再可能发生。"""
    form = _materialize_form()
    names = [f["name"] for f in form["fields"]]
    assert names == [
        "target_datasource_id", "target_database", "target_table",
        "load_strategy", "partition_key", "refresh_cron",
    ]
    by_name = {f["name"]: f for f in form["fields"]}
    # 数据源选项带 id：表单回填是纯文本、无后端会话态，只回名字下一轮还得再猜是哪条
    assert by_name["target_datasource_id"]["options"] == ["仓库｜ds-tpl"]
    # 唯一可写数据源直接定为默认值，省一次追问
    assert by_name["target_datasource_id"]["default"] == "仓库｜ds-tpl"
    assert by_name["load_strategy"]["options"]  # 装载方式恒有候选
    assert any("每天 02:00" in o for o in by_name["refresh_cron"]["options"])


def test_task_template_appends_model_fields_without_overriding():
    """模型另给的字段追加在骨架之后；与骨架同名的被丢掉（骨架不被覆盖）。"""
    form = _materialize_form(extra=[
        {"name": "target_table", "label": "模型想改的表名", "type": "text"},
        {"name": "comment", "label": "备注", "type": "textarea"},
    ])
    by_name = {f["name"]: f for f in form["fields"]}
    assert by_name["target_table"]["label"] == "目标表名"  # 骨架为准
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
    assert any("去重" in o for o in by_name["cleansing_rules"]["options"])


def test_task_kind_form_needs_no_model_fields():
    """带 task_kind 时 fields 可以完全不给——骨架本身就够了，不该再报「表单无字段」。"""
    form = _materialize_form()
    assert len(form["fields"]) == 6
