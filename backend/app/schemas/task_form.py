"""建数/收参表单的传输结构（中性位置，无对话依赖）。

同一张表单有两个入口——Web 任务面板和 MCP 的交互式流程。表单契约放在这里，
使通用 Agent 不依赖某个产品内置的对话实现。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskFormOption(BaseModel):
    """表单候选项：**显示什么**（label）与**回填什么**（value）分开。

    此前两者是同一个字符串，于是需要带 id 的候选（数据源、对象）只能写成「名称｜id」，
    那串 id 就直接糊在下拉里给人看。分开之后：界面只显示 label，id 留在 value 里，
    提交时随回填文本带回给模型。

    ``disabled`` 用于「摆出来但选不了」的候选——执行侧不支持的装载方式必须**看得见**
    （否则用户以为系统只会全量），但不能真被选中（与 MaterializeModal 的置灰同口径）。
    """

    label: str
    value: str
    disabled: bool = False


class TaskFormField(BaseModel):
    """交互表单的单个字段（P6）。``type`` 决定前端用哪种控件渲染。

    ``options`` 仅对 select/multiselect/radio/autocomplete 有意义，且**必须来自工具返回
    的真实实体**（与 clarification.options 同一约束）；没有候选项时应退化为 text/number
    让用户自填。为兼容纯字符串候选，字符串会被归一为 ``label == value`` 的候选项。
    """

    name: str  # 字段标识（回填时作键）
    label: str  # 中文标签
    # text/textarea/number/select/multiselect/radio/boolean/date/autocomplete/cron
    type: str
    options: list[TaskFormOption] = Field(default_factory=list)
    required: bool = False
    placeholder: str | None = None
    help: str | None = None
    default: Any | None = None
    # 建数确认向导中的所属环节：requirement / ontology / data。通用表单留空。
    confirmation_node: str | None = None
    # 级联候选：depends_on 字段当前值 → options_by_value[value]。
    depends_on: str | None = None
    options_by_value: dict[str, list[TaskFormOption]] = Field(default_factory=dict)
    # 候选实时取：``object_properties`` = 取 depends_on 那个对象的字段清单。
    # 静态摊开几百个对象的字段是几 MB 的消息负载，故这类候选按需拉。
    options_from: str | None = None
    # 条件可见：``{"field": "mode", "in": ["incremental", "cdc"]}``。不满足时前端既不
    # 渲染、也不校验、更不把值提交上来——避免「先选了 CDC 填了 sequence 列，又改回全量」
    # 时把一个不该生效的参数留在 Spec 里（它会真的进建表语句）。
    visible_when: dict[str, Any] | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, v: Any) -> Any:
        """字符串候选项归一为 {label, value}——老的纯字符串写法仍然合法。"""
        if not isinstance(v, list):
            return v
        return [{"label": o, "value": o} if isinstance(o, str) else o for o in v]


class TaskFormResponse(BaseModel):
    """Agent 动态生成的**可填写表单**（P6）：一次向用户收集多个结构化参数。

    与 clarification 一样是**终态出口**——本轮到此为止，等用户在前端填完提交后作为
    新一轮问题带回（结构化回填文本进 history，Agent 据此继续）。表单只描述「要收集什么」，
    不携带业务结论，故不入接地账本、不参与拒答判定。适用于取数（指标+时间+维度）、
    建数任务（目标表+更新策略+调度）等需一次补齐多参数的场景。
    """

    title: str
    intent: str = ""
    #: 服务端改判/合并了这次请求时给人的一句解释（如「同步自带建表，已省掉物化那一步」）。
    #: 空 = 没有可说的。改判不能只在后台发生——人得知道自己拿到的为什么是这张表单。
    notice: str = ""
    submit_label: str = "提交"
    fields: list[TaskFormField] = Field(default_factory=list)
    task_kind: str | None = None
    ontology_id: str | None = None
    confirmation_id: str | None = None
