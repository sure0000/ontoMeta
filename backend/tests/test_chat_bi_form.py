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
):
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
                      **({"fields": extra} if extra else {}),
                      **({"prefill": prefill} if prefill else {})},
            )
        finally:
            # 带 dsn 的源会被 run_sql 的数据源解析选走，串到别的用例
            db.query(DataSource).filter(DataSource.id == "ds-tpl").delete()
            db.commit()
    assert is_error is False, result
    return result["form"]


def test_materialize_template_asks_every_required_param():
    """物化表单的必问字段由服务端出——模型漏问装载方式/分区键这件事不该再可能发生。

    这里列不出 ds-tpl 上的库（测试库没有真连接），故走「数据源下拉 + 库名手填」的降级分支。
    """
    form = _materialize_form()
    names = [f["name"] for f in form["fields"]]
    assert names == [
        "target_datasource_id", "target_database", "target_table",
        "load_strategy", "partition_key", "refresh_cron",
    ]
    by_name = {f["name"]: f for f in form["fields"]}
    # id 放 value、名称放 label：那串 id 不该糊在下拉里给人看，但下一轮模型仍要认得是哪条
    assert by_name["target_datasource_id"]["options"] == [
        {"label": "仓库（hive）", "value": "ds-tpl"}
    ]
    # 唯一可写数据源直接定为默认值，省一次追问
    assert by_name["target_datasource_id"]["default"] == "ds-tpl"
    assert by_name["target_database"]["type"] == "text"  # 列不出库 → 手填
    assert by_name["load_strategy"]["options"]  # 装载方式恒有候选
    # 调度频率是 cron 选择器（与业务对象详情里的「定时策略」同一个控件），不是几个预置项
    assert by_name["refresh_cron"]["type"] == "cron"
    assert "options" not in by_name["refresh_cron"]


def test_materialize_offers_all_three_load_strategies_with_unsupported_disabled():
    """装载方式**三种都摆出来**，执行侧不支持的置灰并说明原因。

    此前不支持的被直接过滤掉，界面上只剩「全量覆盖」一项——看着像是系统只会全量，而真正
    的原因（这个目标引擎在执行侧只声明了全量）一个字都没说。与 MaterializeModal 同口径。
    """
    from unittest.mock import patch

    with patch("app.services.sync_tool_resolver.engine_modes",
               return_value=(["full"], "取自 sync-runner 声明的 hive 能力。")):
        form = _materialize_form()
    strategy = {f["name"]: f for f in form["fields"]}["load_strategy"]
    assert [o["value"] for o in strategy["options"]] == ["full", "incremental", "cdc"]
    assert strategy["options"][0].get("disabled") is not True
    assert all(o["disabled"] is True for o in strategy["options"][1:])
    assert "不支持" in strategy["options"][1]["label"]
    assert strategy["default"] == "full"  # 默认落在**支持**的那一档


def test_materialize_partition_key_offers_business_attributes():
    """分区键从业务属性里选：它必须是这些表上真实存在的列，不该靠手填印象里的字段名。"""
    form = _materialize_form()
    pk = {f["name"]: f for f in form["fields"]}["partition_key"]
    # autocomplete 而不是 select：候选是建议不是闭集，物理表上可能有本体没建模的分区列
    assert pk["type"] == "autocomplete"
    assert pk["options"], "本体里有属性时分区键必须有候选"
    assert all(o["value"] and o["label"] for o in pk["options"])
    assert any("覆盖" in o["label"] for o in pk["options"])  # 覆盖几个实体要如实标出


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
    # 规则码进 value（Drafter 认的就是它），中文说明进 label
    assert any("去重" in o["label"] for o in by_name["cleansing_rules"]["options"])
    assert all(o["value"] for o in by_name["cleansing_rules"]["options"])


def test_task_kind_form_needs_no_model_fields():
    """带 task_kind 时 fields 可以完全不给——骨架本身就够了，不该再报「表单无字段」。"""
    form = _materialize_form()
    assert len(form["fields"]) == 6


def test_target_datasource_and_database_are_one_choice():
    """能列出库时，「数据源」与「目标库」合并成一次选择。

    两者在物化弹窗里本来就是联动的（先选源、再从这个源上列出的库里挑）。表单一次性提交、
    没有联动，两个独立下拉就会让人选出「A 源 + B 源上的库」这种根本不存在的组合。
    """
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form()
    names = [f["name"] for f in form["fields"]]
    assert "target_location" in names
    assert "target_datasource_id" not in names and "target_database" not in names
    loc = {f["name"]: f for f in form["fields"]}["target_location"]
    assert [o["label"] for o in loc["options"]] == ["仓库（hive） → dw", "仓库（hive） → ods"]
    # value 自解释成「键=值」：回填是纯文本，模型不必再猜哪一段是数据源 id
    assert loc["options"][0]["value"] == "target_datasource_id=ds-tpl,target_database=dw"


def test_prefill_fills_what_user_already_said():
    """用户已经说过的取值预填进表单，不再原样问一遍（片段也认，如库名 dw）。"""
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form(prefill={
            "target_location": "dw",          # 唯一子串命中「仓库（hive） → dw」
            "load_strategy": "全量覆盖",       # 按 label 命中
            "refresh_cron": "0 2 * * *",      # 无候选的字段原样采用
            "target_table": "dim_customer",
        })
    by_name = {f["name"]: f for f in form["fields"]}
    assert by_name["target_location"]["default"] == "target_datasource_id=ds-tpl,target_database=dw"
    assert by_name["load_strategy"]["default"] == "full"
    assert by_name["refresh_cron"]["default"] == "0 2 * * *"
    assert by_name["target_table"]["default"] == "dim_customer"


def test_prefill_drops_values_that_match_no_real_candidate():
    """核不上真实候选的预填被丢掉。

    一个听错的库名若原样落进 default，用户看到的是一张「系统已经替我确认过」的表单，
    而它是错的——错得比空着更贵。
    """
    from unittest.mock import patch

    with patch("app.services.data_app.DataAppService.list_databases", return_value=["dw", "ods"]):
        form = _materialize_form(prefill={"target_location": "根本没有这个库"})
    assert "default" not in {f["name"]: f for f in form["fields"]}["target_location"]


def test_prefill_cannot_select_a_disabled_option():
    """执行侧不支持的装载方式摆出来但选不了——预填也不能把它绕过去。"""
    from unittest.mock import patch

    with patch("app.services.sync_tool_resolver.engine_modes",
               return_value=(["full"], "取自 sync-runner 声明的 hive 能力。")):
        form = _materialize_form(prefill={"load_strategy": "增量追加"})
    strategy = {f["name"]: f for f in form["fields"]}["load_strategy"]
    assert strategy["default"] == "full"  # 骨架默认值保持，未被预填改成 incremental


def test_prefill_accepts_free_value_for_open_candidate_field():
    """autocomplete 的候选是**建议不是闭集**：本体没建模的物理列也照填，不该被丢掉。"""
    form = _materialize_form(prefill={"partition_key": "dt_physical_only"})
    assert {f["name"]: f for f in form["fields"]}["partition_key"]["default"] == "dt_physical_only"


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
