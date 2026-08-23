"""Data Agent 工具 schema + 编排常量 + 工具集构建（V4 O5：从 chat_bi.py 拆出）。

**为什么拆**：`chat_bi.py` 一度 5300+ 行，工具 schema（~980 行）与推理循环挤在一个文件里，
改一个工具描述要在巨文件里翻半天。这些 schema 是**纯声明**——不依赖 `ChatBiService`，
也不依赖任何运行态。把它们连同「工具集构建」这组纯函数抽出来，`chat_bi.py` 只 `import *`
再全量 re-export，对外符号（`_AGENT_TOOL_SCHEMAS` / `_TOOL_BY_NAME` / `_tools_for_skill` …）
**逐字节不变**，测试与其它模块的 import 契约不动。纯结构重构、零行为变化。

内容：运行常量 · 系统提示 · 全部工具 schema · 工具注册表 · 工具集构建/检索小工具。
不含：`ChatBiService`（推理循环）、`_ObjectSnapshot`、`_ReferenceResolver`——那些有运行态或强耦合。
"""

from __future__ import annotations

from typing import Any

import sqlparse
from sqlalchemy.orm import Session

from app.services.chat_bi_skills import SKILLS, Skill, skill_choices_text
from app.services.metric_compiler import COMPARE_OPS, LOGIC_TYPES, METRIC_OPS


# 预算只会更紧——真被拒一次就没有余量修正了。
_AGENT_MAX_STEPS = 8            # 工具轮上限，超出强制收尾作答
_AGENT_REPAIR_ATTEMPTS = 1      # 自愈重写次数上限（独立预算，不占工具轮）
_RUN_SQL_LIMIT = 100           # run_sql 默认返回行上限
_TOOL_RESULT_MAX_CHARS = 8000  # 单个工具结果回灌前的截断阈值
_SQL_TIMEOUT_SECONDS = 15      # run_sql 语句超时（execute_sql 既有能力）
_SEARCH_LIMIT = 8              # 检索类工具默认返回条数
_OVERVIEW_LIST_LIMIT = 100     # 概览里 objects/relations 样本清单各自的条数上限
_OVERVIEW_TOP_CONNECTED = 10   # 概览里「关系最多的对象」Top N

# 提示词只保留**无法由构造保证**的部分。
#
# 随 P1–P3 迁走的那些（原 1b/1c/一半的 1/4b）已由架构承担：
#   · 域骨架   → 域语义卡常驻 system（P2.1），不必再要求「概览题先调 get_domain_overview」
#   · 样本≠全集 → 截断时键名即为 `sample` 且带 sample_note/facets（P2.3），
#                 不必再用一整段铁律去堵「把 8 条当成全部」
#   · 结果完整性 → 语义降级压缩保证回灌永远是合法 JSON（P2.2）
#   · 不得编造   → FactLedger + answer_verifier 断言级核验（F4）当场拒答，
#                 比反复叮嘱「宁可少答不可编造」有效得多
# 保留下来的都是**工具选择策略**——这类「该先调谁」的判断，守卫拦得住结果却教不会顺序。
_AGENT_SYSTEM_PROMPT = (
    "你是企业数据助手，请用中文简洁回答。"
    "使用当前已发布本体和工具结果获取信息。"
    "数据查询使用默认 Doris。元数据问题使用本体工具。"
    "数据任务使用任务工具和确认表单。"
    "请清楚标注建议、草稿、查询结果和任务状态。"
)

# Prompt 被上游网关误判时使用的单次精简重试版本。它不包含动态本体卡、历史记忆或任务细节。
_MINIMAL_AGENT_SYSTEM_PROMPT = (
    "你是企业数据助手，请用中文完成用户请求。优先使用工具取得当前信息。"
    "数据查询使用默认 Doris。数据任务先读取选项，再生成确认表单；表单提交后生成任务提案。"
    "请区分建议、草稿和实际运行状态。"
)

# OpenAI 原生 function-calling 工具（自建 GLM 实测支持）。
# 检索类直呼 OntologyQueryService（带 q 关键词 + limit），避免 MCP 目录“仅按域全返”导致的巨结果。
_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_objects",
            "description": (
                "按关键词检索当前数据域的已发布业务对象。返回 "
                "{total_matched, returned, truncated, items}："
                "total_matched 是真实命中总数，items 只是前几条；truncated=true 表示还有更多未返回。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（对象名/含义）"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_object",
            "description": "获取单个业务对象的完整定义：字段（name/显示名/类型/语义）、进出关系、绑定的业务逻辑。",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string", "description": "业务对象 id（来自 search_objects）"},
                },
                "required": ["object_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_relations",
            "description": (
                "按关键词检索业务关系（两个对象之间的关联）。返回 "
                "{total_matched, returned, truncated, items}："
                "total_matched 是真实命中总数，items 只是前几条；truncated=true 表示还有更多未返回，"
                "此时不得把 items 当作全部关系。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logics",
            "description": (
                "按关键词检索业务逻辑/指标口径（如 GMV、活跃客户）。返回 "
                "{total_matched, returned, truncated, items}："
                "total_matched 是真实命中总数，items 只是前几条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logic",
            "description": "获取单个业务逻辑/指标的完整口径：表达式、绑定的对象与字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "logic_id": {"type": "string", "description": "业务逻辑 id（来自 search_logics）"},
                },
                "required": ["logic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_domain_overview",
            "description": (
                "获取当前数据域【已发布本体】的概览：对象/关系总数、"
                "有关系与无关系的对象数（objects_with_relations / objects_without_relations）、"
                "关系最多的对象（most_connected_objects），以及**可能被截断的**对象与关系样本清单"
                "（objects_truncated / relations_truncated 标示是否截断）。"
                "回答“有哪些对象/本体”“哪些对象有关系”这类概览问题时首选此工具。"
                "**仅含已发布内容，不含未发布草稿**。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "locate_entities",
            "description": (
                "把「在本体里找出与问题相关的对象/口径」这件事**整体外包**给检索助手，"
                "只拿回一份标识符清单。当你不确定该用什么关键词、或域很大需要来回试几次时用它——"
                "试错过程不会占用本对话上下文。"
                "拿到清单后再对具体标识符调 get_object / get_logic 取细节。"
                "若你已经知道要找什么，直接用 search_* 更快，不必绕这一道。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "要定位什么（用自然语言描述，可直接转述用户问题）",
                    },
                },
                "required": ["intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_join_path",
            "description": (
                "查两个业务对象之间**如何关联**：返回已声明的关系路径（可多跳）、"
                "每段的 ON 条件、基数链、扇出风险与安全聚合建议。"
                "写涉及两个及以上对象的 SQL **之前必须先调用它**——"
                "JOIN 条件只能来自这里，自行猜测外键字段会被语义证明拒绝。"
                "返回空列表表示本体中这两个对象无从关联。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_object": {"type": "string", "description": "起点对象标识符（name）"},
                    "to_object": {"type": "string", "description": "终点对象标识符（name）"},
                    "measure_object": {
                        "type": "string",
                        "description": "被聚合度量所在对象的 name（判扇出用，默认取起点）",
                    },
                },
                "required": ["from_object", "to_object"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compile_metric",
            "description": (
                "把一条**已发布业务逻辑**按给定维度/过滤/时间粒度编译成 SQL，并返回口径展开轨迹。"
                "支持三类：**指标**（GMV/客单价…→ 聚合查询）、"
                "**标签**（客户分层/订单分级…→ 各分桶取值的分布）、"
                "**规则**（金额必须为正…→ 统计**违规**行数）。"
                "**凡是问已有指标/标签/规则的问题，一律用它，不要自己写 SQL**——"
                "口径以本体为准，自己重写会算出与其它系统不一致的数。"
                "返回的 sql 可直接交给 run_sql 执行；结果里的 logic_type 告诉你它是哪一类。"
                "编译失败会给出原因与修复信号（如维度不可关联、口径尚未形式化）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "logic_id": {
                        "type": "string",
                        "description": "业务逻辑 id（来自 search_logics，指标/标签/规则均可）",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "拆分维度，写成 对象.字段（如 customer.region）或本对象字段名",
                    },
                    "filters": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "追加过滤，每项 {property, op, value|values}；"
                            "op ∈ =,!=,>,>=,<,<=,like,in。"
                            "**字面量必须来自 profile_values 的真实取值**，不得凭空猜。"
                        ),
                    },
                    "grain": {
                        "type": "string",
                        "description": "时间粒度：day/week/month/quarter/year",
                    },
                    "time_property": {
                        "type": "string",
                        "description": "时间粒度作用的时间字段（有多个时间字段时必填）",
                    },
                },
                "required": ["logic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_values",
            "description": (
                "查某个字段**实际存着什么值**：类别/标识字段给 TopN 取值及频次与去重数，"
                "度量字段给最小/最大/均值，时间字段给时间区间；另有空值率。"
                "**写带字面量的 WHERE 之前必须先调用它**——"
                "本体只定义字段存在，不保证你猜的枚举值（如「已完成」）真的在库里；"
                "猜错的字面量会让查询返回 0 行而不报错，答案就错得看不出来。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string", "description": "业务对象 id 或标识符(name)"},
                    "property": {"type": "string", "description": "字段标识符（本体属性 name）"},
                },
                "required": ["object_id", "property"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": (
                "当问题存在**必须由用户澄清才能继续**的缺口时调用它反问，而不是挑一个可能错的解释硬答。"
                "适用：问的指标在本体里有多个候选、时间字段有多个不知按哪个、"
                "口径里的分类值不明确、问题范围过宽无法确定主对象。"
                "**不适用**：本体里查不到相关内容（那应如实说明无法回答）、"
                "或你只是没检索够（那应继续调检索工具）。"
                "调用后本轮结束并把问题抛给用户，不要再输出别的内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "向用户提出的澄清问题（中文，一句话）"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "供用户选择的候选项（**必须来自工具返回的真实实体**）",
                    },
                    "reason": {"type": "string", "description": "为什么需要澄清（一句话）"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "在当前已发布本体对应的默认 Doris 数仓上执行**只读 SELECT**，返回列与真实数据行。"
                "表名/字段名必须使用本体标识符。Doris 未配置或投影未就绪时 fail-closed，"
                "只返回建议 SQL，不切换到业务源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句"},
                    "limit": {"type": "integer", "description": f"返回行上限，默认 {_RUN_SQL_LIMIT}"},

                },
                "required": ["sql"],
            },
        },
    },
]


# --- V3 S1：技能层工具 ---------------------------------------------------------
# select_skill 是基础工具（永远可用）；render_chart 由 query 技能解锁（只解锁不收窄）。
_SELECT_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "select_skill",
        "description": (
            "当问题明确属于某类数据任务时，先选对应技能，获得该任务类型的专门策略与能力。"
            "可选技能：" + skill_choices_text() + "。不确定就不必选，按通用方式作答即可。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": list(SKILLS.keys()), "description": "要切换到的技能名"},
            },
            "required": ["skill"],
        },
    },
}

_RENDER_CHART_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "render_chart",
        "description": (
            "把最近一次 run_sql 的结果渲染成图表（供前端展示）。仅在已取到数据且适合可视化时用；"
            "x/y 必须照抄结果表里的真实列名，否则会被拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["bar", "line", "area"],
                         "description": "图型：类别对比用 bar；时间序列用 line 或 area"},
                "x": {"type": "string", "description": "X 轴列名（维度/时间），须为结果表列"},
                "y": {"type": "string", "description": "Y 轴列名（度量），须为结果表列"},
                "title": {"type": "string", "description": "可选：图表标题"},
            },
            "required": ["kind", "x", "y"],
        },
    },
}

_ANALYZE_RESULT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "analyze_result",
        "description": (
            "对最近一次 run_sql 的结果做统计画像 + 离群检测（均值/分位/标准差 + IQR 异常值）。"
            "回答「有没有异常/分布怎样/哪些离群」时用它拿到**真实计算**，别口头臆测。仅分析数值列。"
            "给 order_by（如时间/月份列）还会算**趋势方向 + 突变点**（回答「趋势如何/哪里突变」）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"},
                             "description": "可选：只分析这些结果列（缺省=全部数值列）"},
                "order_by": {"type": "string",
                              "description": "可选：按此列（时间/序号）排序后算趋势与突变；须为结果列"},
                "max_outliers": {"type": "integer", "description": "每列最多返回的离群/突变样例数，默认 5"},
            },
            "required": [],
        },
    },
}

_READ_RESULT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_result",        "description": (
            "分页读取某次 run_sql 的**完整结果行**（大结果表默认只回样例行，全量存在离场 store）。"
            "仅当样例不够回答、确需更多行时用；handle 用 run_sql 返回的 result_handle。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "run_sql 返回的 result_handle（如 rs_1）"},
                "offset": {"type": "integer", "description": "起始行号，从 0 起，默认 0"},
                "limit": {"type": "integer", "description": "本次取多少行，默认 20，上限 100"},
            },
            "required": ["handle"],
        },
    },
}

_SCOUT_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "scout_query",
        "description": (
            "把「取数前的探路」**整体外包**给探路助手：它在隔离上下文里定位实体、摸取值分布、"
            "找 join 路径、编口径，只拿回一条**候选只读 SQL** + 依据要点。当取数需跨多个对象、"
            "或要先 profile 好几个字段才能写对时用它——探路过程不占本对话上下文。"
            "拿到候选 SQL 后由你用 run_sql 提交执行（仍过只读校验与语义证明）。"
            "若取数很直接（单对象、无需探值），直接写 SQL 更快，不必绕这一道。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "要取什么数（用自然语言描述，可直接转述用户问题）",
                },
            },
            "required": ["intent"],
        },
    },
}

_GET_LINEAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_lineage",
        "description": (
            "查看某业务对象的血缘/上下游邻域子图（中心对象 + depth 跳关系）。"
            "center_id 用 search_objects/get_object 拿到的对象 id。"
            "structure_type=derivation 的边是数据加工血缘，其它是业务关系（外键/引用等）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "center_id": {"type": "string",
                              "description": "中心业务对象的 id（来自 search_objects/get_object）"},
                "depth": {"type": "integer", "description": "邻域跳数，默认 1，最多 3"},
            },
            "required": ["center_id"],
        },
    },
}

_PROPOSE_DRAFT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_draft",
        "description": (
            "为新建一个口径定义（指标 metric / 标签 tag / 规则 rule）产出**提案**（不写库）。"
            "只给中文名、类型与口径自然语言说明；**不要编具体表达式**——口径由人在确认后补全。"
            "提案由用户在前端点击确认后才创建为草稿口径。建前应先 search_logics 查重。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "description": "口径中文名，如「复购率」"},
                "logic_type": {"type": "string", "enum": ["metric", "tag", "rule"],
                               "description": "指标 metric / 标签 tag / 规则 rule"},
                "name": {"type": "string",
                         "description": "英文标识符（snake_case，如 repurchase_rate）；缺省则由中文名派生"},
                "description": {"type": "string", "description": "口径的自然语言说明/意图"},
            },
            "required": ["display_name", "logic_type"],
        },
    },
}

# ---- 口径形式化：让模型出**可编译**的表达式 ----
# propose_draft 只出名字与自然语言说明，表达式留给人在编辑器里补——那一跳是这条链上最容易
# 断的地方（提案确认完，口径停在「有名字没表达式」的草稿态）。现在可以让模型直接出表达式，
# 因为守卫不是提示词而是**编译器**：产出的 AST 当场过 compile_candidate（与已发布口径同一条
# 编译+自证路径），编不出来就把编译器的错误与候选字段还给模型让它改；编得出来，人看到的
# 也是真 SQL 与口径展开轨迹，而不是一段自然语言承诺。
_PROPOSE_EXPRESSION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_expression",
        "description": (
            "为一条口径产出**带表达式**的提案：你给字段与表达式体，服务端解析成本体引用、"
            "当场编译成 SQL 并做语义证明，编不过会把原因和可用字段返给你改。\n"
            "**字段必须来自本体**：先用 search_objects/get_object 确认对象名与字段名，别猜。\n"
            "编过之后人看到的是真 SQL；确认与落库仍由用户点击，你不写库。\n"
            "只想给个名字、表达式交给人写，用 propose_draft。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "description": "口径中文名，如「大额订单」"},
                "logic_type": {"type": "string", "enum": list(LOGIC_TYPES),
                               "description": "指标 metric / 标签 tag / 规则 rule"},
                "fields": {
                    "type": "array",
                    "description": (
                        "表达式用到的本体字段。别名由你起（表达式体里用它引用），"
                        "object/property 必须是本体里的**技术名**（如 order / amount），不是中文显示名"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "alias": {"type": "string", "description": "别名，如 amt"},
                            "object": {"type": "string", "description": "对象技术名，如 order"},
                            "property": {"type": "string",
                                          "description": "字段技术名，如 amount；只引用对象本身时可省"},
                        },
                        "required": ["alias", "object"],
                    },
                },
                "body": {
                    "type": "object",
                    "description": (
                        "表达式体，按类型三选一，引用一律写 {\"ref\":\"别名\"}：\n"
                        "· metric：{\"operation\":\"" + "|".join(METRIC_OPS) + "\","
                        "\"args\":[{\"ref\":\"amt\"}],\"group_by\":[{\"ref\":\"st\"}],\"filter\":<条件|null>}"
                        "（SUM/AVG 只能作用于语义类型为 measure 的字段）\n"
                        "· tag：{\"cases\":[{\"when\":<条件>,\"then\":{\"value\":\"大额\"}},"
                        "{\"when\":null,\"then\":{\"value\":\"普通\"}}]}"
                        "（when=null 即 else 分支；**每个分支都要给标签值**，否则编不过）\n"
                        "· rule：{\"condition\":<应当成立的条件>,\"message\":\"违规说明\"}"
                        "（规则统计的是**不满足**该条件的行数）\n"
                        "条件形如 {\"left\":{\"ref\":\"amt\"},\"op\":\"" + "|".join(COMPARE_OPS)
                        + "\",\"right\":{\"value\":1000}}，"
                        "可嵌套 {\"op\":\"and|or\",\"conditions\":[…]}"
                    ),
                },
                "name": {"type": "string",
                          "description": "英文标识符（snake_case）；缺省由中文名派生"},
                "summary": {"type": "string", "description": "口径的一句话说明（给人看）"},
                "logic_id": {
                    "type": "string",
                    "description": (
                        "可选：为**已存在**的口径补全表达式时给它的 id"
                        "（search_logics 能查到、但还没形式化的那些）；不给即新建"
                    ),
                },
            },
            "required": ["display_name", "logic_type", "fields", "body"],
        },
    },
}

_LINT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lint_against_standard",
        "description": (
            "用当前数据治理规约自检一份建数/建表规格（提案前调、据返回的 fix 自行修正）。"
            "主要查物理表名等命名规约；返回违规项列表，空列表=合规。口径提案无物理表名时返回空。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "制品类型，如 transform / materialize / metric（可空）",
                },
                "spec": {
                    "type": "object",
                    "description": '待自检的规格，如 {"target_table": "dim_customer"}',
                },
            },
            "required": ["spec"],
        },
    },
}

# 数据任务类型白名单——agent 只在「物化/同步/加工」车道出提案；metric 归 propose_draft、
# cluster（基建）不开，避免与口径提案重叠混淆。
_ACTION_KINDS: tuple[str, ...] = ("materialize", "sync", "transform", "metric")
_ACTION_KIND_LABEL: dict[str, str] = {
    "materialize": "物化", "sync": "同步", "transform": "加工", "metric": "聚合",
}

# 当前已落地的 Doris 执行能力。
_PIPELINE_KINDS: tuple[str, ...] = ("materialize", "sync", "transform", "metric")
_PIPELINE_MAX_STEPS: int = 8

_PROPOSE_ACTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_action",
        "description": (
            "根据已填写的任务确认表单生成任务提案。"
            "context 包含表单返回的 task_confirmation_id 和字段值。"
            "materialize 建本体结构；sync 写入 ODS；transform 加工 ODS 数据；metric 生成 ADS 结果。"
            "返回的是提案，后续状态由任务流水线更新。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "物化 / 同步 / Doris 加工 / Doris metric-tag-rule"},
                "intent": {"type": "string",
                            "description": "自然语言任务意图，如「把客户主数据物化到数仓 dim_customer」"},
                "context": {"type": "object",
                             "description": (
                                 "结构化上下文；必须包含本次 request_form 回填的 task_confirmation_id。"
                                 "其余键按任务类型使用服务端表单回填值，禁止自造 id/库名/对象/口径。"
                                 "凭据不得进入 context；连接只传 DataSource id。"
                             )},
            },
            "required": ["kind", "intent"],
        },
    },
}

_PROPOSE_PIPELINE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_pipeline",
        "description": (
            "当用户要的是**前后相继的多个任务**（如「物化到数仓，然后清洗，再按口径聚合」）时，"
            "产出一条**任务链提案**（不执行、不写库）。链上每一步仍是一条独立任务，各自走"
            "「校验→dry-run→人工确认→执行」；链负责的是记住下一步、并把上游已定下的目标数据源/"
            "默认 Doris/本体版本接到下游，用户不必逐步重报。\n"
            "**只有一个任务时用 propose_action**，别为单步套一条链。\n"
            "当前标准链是 materialize(Doris 建结构) → sync(业务源经 Flink 写 ODS) → "
            "transform(Doris ODS→DIM/DWD/DWS) → metric(Doris ADS)。metric 必须引用已发布且形式化的口径。"
            "sync 的 source_datasource_id、ODS 库、主键/水位/CDC 策略"
            "不能从 materialize 继承，必须显式给；ODS 表名由后端固定生成，"
            "target_datasource_id 固定继承默认 Doris。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "这条链叫什么，一句话（如「客户主数据入仓链」）"},
                "intent": {"type": "string", "description": "整条链要达成什么，一句话"},
                "steps": {
                    "type": "array",
                    "description": f"有序步骤（2-{_PIPELINE_MAX_STEPS} 步），按执行先后排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string", "enum": list(_PIPELINE_KINDS),
                                "description": "当前可执行：materialize / sync / transform / metric",
                            },
                            "intent": {"type": "string", "description": "这一步做什么，一句话"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "血缘依赖：这一步依赖的上游步序（从 0 起）。"
                                "默认空 = 依赖上一步；如「清洗依赖同步」则第 1 步 depends_on=[0]。"
                            },
                            "context": {
                                "type": "object",
                                "description": (
                                    "这一步的结构化上下文，键与 propose_action 同；"
                                    "上游已给过的落点（数据源/库/引擎）不必重复"
                                ),
                            },
                        },
                        "required": ["kind", "intent"],
                    },
                },
            },
            "required": ["name", "steps"],
        },
    },
}

# ---- P1：建数任务的可选项目录 ----
# 建数表单此前长不出下拉框，根因是模型没有任何工具能读到物理侧的候选（数据源/库/契约/
# 装载方式/调度）——request_form 的「options 必须来自真实实体」于是永远无法满足，只能退化
# 成文本输入。本工具就是那份缺失的目录：与物化弹窗（MaterializeModal）读同一批服务，
# 保证「对话里选到的」和「弹窗里选到的」是同一套事实。

# 调度频率预置项。与前端 CronPicker 同域：那里是任意 cron 的下拉编辑器，这里给几个常用
# 值让模型直接摆进表单；用户要别的频率仍可自填合法 cron 表达式。
_CRON_PRESETS: tuple[dict[str, str], ...] = (
    {"expr": "0 2 * * *", "label": "每天 02:00"},
    {"expr": "0 */6 * * *", "label": "每 6 小时"},
    {"expr": "0 * * * *", "label": "每小时"},
    {"expr": "0 3 * * 1", "label": "每周一 03:00"},
    {"expr": "0 4 1 * *", "label": "每月 1 日 04:00"},
    {"expr": "", "label": "不定时（仅手动触发）"},
)

# Flink source→Doris ODS 的三种接入语义。
_LOAD_STRATEGIES: tuple[dict[str, str], ...] = (
    {"value": "full", "label": "全量覆盖",
     "hint": "Flink batch 写 ODS staging，质量检查后 Doris atomic replace；失败不影响正式表"},
    {"value": "incremental", "label": "增量同步",
     "hint": "按 incremental_column + 成功水位做有界 JDBC batch，Doris Unique Key UPSERT"},
    {"value": "cdc", "label": "CDC 变更捕获",
     "hint": "Flink CDC detached 长期作业；必须配置主键、sequence、checkpoint 与 DELETE 策略"},
)

# 候选清单的回灌上限。物化契约会有几百条（一个 734 对象的域即如此），整份倒进上下文
# 既挤爆预算也没人读——按 search_* 的既有约定给 {total, returned, truncated, items}。
_TASK_OPTIONS_LIMIT: int = 30

_GET_TASK_OPTIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_task_options",
        "description": (
            "读取数据任务可用选项。"
            "materialize 返回 Doris、数据库和物化范围；sync 返回本体、业务源和 ODS 信息；"
            "transform 返回可加工对象和规则；metric 返回形式化业务口径。"
            "读取后使用 request_form 生成确认表单。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "要建的任务类型：materialize / sync / transform"},
                "target_datasource_id": {
                    "type": "string",
                    "description": "兼容参数；物化实际固定使用服务端唯一可执行默认 Doris，不能用它覆盖",
                },
                "keyword": {"type": "string",
                             "description": "按实体名/显示名过滤候选，候选很多时用它收窄"},
            },
            "required": ["kind"],
        },
    },
}

# 由服务端注入的 context 键——当前会话的本体是确定的，模型不必（也无从）给。
_AUTO_ACTION_CONTEXT_KEYS: frozenset[str] = frozenset({"ontology_id"})

_ACTION_CONTEXT_HINT = (
    "这些是起草该任务必须先定下、且无法从本体推导的选项。用 request_form 把它们做成一张表单"
    "让用户选（候选项用本结果里给出的真实值），拿到回填后再重新 propose_action。不要自己编 id。"
)


def _sync_context_errors(
    db: Session, context: dict[str, Any], *, ontology_id: str | None = None
) -> list[str]:
    """Deterministic mirror of the Doris ODS sync prompt contract."""
    from app.models import DataSource, ObjectType

    errors: list[str] = []
    source = db.get(DataSource, context.get("source_datasource_id"))
    target = db.get(DataSource, context.get("target_datasource_id"))
    if source is not None and (source.purpose != "business_source" or not source.enabled):
        errors.append("source_datasource_id 必须是启用的 business_source")
    if source is not None and ontology_id and context.get("object_type"):
        from app.services.source_datasource import source_datasource_candidates

        obj = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.name == str(context["object_type"]),
            )
            .first()
        )
        if obj is not None:
            allowed = {candidate.id for candidate in source_datasource_candidates(db, obj)}
            if source.id not in allowed:
                errors.append(
                    "source_datasource_id 与所选本体的 source_ref 平台/库/表来源不匹配"
                )
    if target is not None and not (
        target.purpose == "warehouse" and target.kind == "doris"
        and target.is_default_warehouse and target.enabled
        and bool((target.dsn_secret_ref or "").strip())
    ):
        errors.append("target_datasource_id 必须是启用、已配置连接的默认 Doris")
    if context.get("target_ods_database") and not str(context["target_ods_database"]).startswith("ods"):
        errors.append("target_ods_database 必须以 ods 开头")
    mode = str(context.get("mode") or "full")
    if mode in {"incremental", "cdc"} and not context.get("primary_keys"):
        errors.append(f"{mode} 必须配置 primary_keys")
    if mode == "incremental":
        for key in ("incremental_column", "initial_watermark"):
            if context.get(key) in (None, ""):
                errors.append(f"incremental 必须配置 {key}")
    if mode == "cdc":
        for key in ("sequence_column", "delete_policy", "flink_checkpoint_dir"):
            if context.get(key) in (None, ""):
                errors.append(f"CDC 必须配置 {key}")
    return errors


def _missing_action_context(kind: str, context: dict[str, Any]) -> list[str]:
    """该类任务起草前还缺哪些 context 键。

    判据取 Drafter 自己声明的 ``required_context``（其 ``require_context`` 的同一份字面值），
    不在这里另抄一份——否则两处迟早分叉。注意**不能**改用规约的
    ``required_metadata.per_artifact``：那约束的是 Spec 字段（如 sync 的 source/target），
    由 Drafter 从本体推导，不是调用方要给的 context 键。
    """
    # 局部导入：app.agents 在导入期注册 Drafter/Executor（连带拉起 materialization_runner），
    # 读侧模块不该为一次校验把整条写侧流水线拽进导入图。
    from app.agents import registry

    try:
        drafter = registry.get_drafter(kind)
    except registry.UnregisteredKindError:
        return []
    required = list(drafter.required_context)
    if kind in {"materialize", "transform", "metric"}:
        required.append("target_datasource_id")
    if kind == "materialize":
        required.append("target_database")
    if kind == "metric":
        required.append("business_logic_id")
    if kind == "sync":
        required.extend((
            "source_datasource_id",
            "target_datasource_id",
            "target_ods_database",
        ))
    return [
        key
        for key in dict.fromkeys(required)
        if key not in _AUTO_ACTION_CONTEXT_KEYS and not context.get(key)
    ]


def _action_context_candidates(db: Session, missing: list[str]) -> dict[str, Any]:
    """缺失键的真实候选值——只说「缺 target_datasource_id」模型和用户都无从下手。

    只返回选项本身（id/名称/类型/连通状态），凭据不出现（DSN 存的本就是 ``dsn_secret_ref``）。
    """
    from app.models import DataSource

    rows = db.query(DataSource).order_by(DataSource.name).limit(50).all()
    out: dict[str, Any] = {}
    if "source_datasource_id" in missing:
        out["source_datasource_id_options"] = [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
            for s in rows if s.purpose == "business_source" and s.enabled
        ]
    if "target_datasource_id" in missing:
        out["target_datasource_id_options"] = [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
            for s in rows
            if s.purpose == "warehouse" and s.kind == "doris"
            and s.is_default_warehouse and s.enabled
        ]
    return out


_GET_TASK_STATUS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_task_status",
        "description": (
            "回读数据任务（治理制品）的执行状态与回执摘要——回答「某任务跑到哪了/成没成功」。"
            "给 artifact_id 查单个；否则按 kind/limit 列最近若干。只读，不触发执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "指定任务（制品）id；缺省则列最近的"},
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "按类型过滤（可空）"},
                "limit": {"type": "integer", "description": "列表条数上限，默认 5"},
            },
            "required": [],
        },
    },
}

# 计划步骤状态（P2 显式规划）
_PLAN_STATUSES: frozenset[str] = frozenset({"pending", "active", "done"})
_PLAN_MAX_STEPS: int = 12

_UPDATE_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "为开放式/多步分析列一份**计划**（2-5 步为宜），让用户看到你打算怎么拆解这个问题。"
            "整份计划**每次调用整体覆盖**（同 TodoWrite）；进展时可再调一次把某步 status 改为 "
            "active/done，但不必每步都回来更新（执行轨迹已实时展示进度）。单值/单步问题不必规划。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "计划步骤（有序）；每步一句话标题",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "该步要做什么，一句话"},
                            "status": {"type": "string", "enum": list(_PLAN_STATUSES),
                                        "description": "pending/active/done，缺省 pending"},
                        },
                        "required": ["title"],
                    },
                },
                "note": {"type": "string", "description": "对整体思路的一句可选说明"},
            },
            "required": ["steps"],
        },
    },
}

_PROPOSE_PREFERENCE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_preference",
        "description": (
            "当用户明确表达一个**应跨会话记住的口径/范围约定**（如「以后成交额都按含税口径」"
            "「华东含上海」）时，产出一份**记忆提案**（不写库）。由用户在前端点「记住」后才落库为"
            "本域约定，后续作为软提示注入。仅在用户确实在立约定时用，普通提问别调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记住的约定，一句话（如「成交额默认含税口径」）"},
            },
            "required": ["text"],
        },
    },
}

# P6 交互表单：需一次补齐多个结构化参数才能继续时，Agent 生成一张可填写表单收集上下文，
# 比逐条 ask_clarification 追问高效。与 clarification 同为终态出口（本轮结束等用户回填）。
_FORM_FIELD_TYPES: tuple[str, ...] = (
    "text", "textarea", "number", "select", "multiselect", "radio", "boolean", "date",
    # autocomplete = 带候选建议的文本框（候选是建议不是闭集，如分区键：物理表上可能有
    # 本体没建模的列）；cron = 与 CronPicker 同一个调度选择器（任意合法 cron，非预置项）。
    "autocomplete", "cron",
)
_FORM_MAX_FIELDS: int = 10
# 建表单时最多探几个数据源的库列表：每个源一次真实连接，全探会把发表单这一步拖成秒级。
_FORM_DATASOURCE_PROBE_LIMIT: int = 8
# 「数据源 → 库」合并候选的条数上限：一个源上几百个库时下拉本身就没法用了。
_FORM_LOCATION_LIMIT: int = 200


def _normalize_form_options(raw: Any) -> list[dict[str, str]]:
    """候选项归一为 ``{label, value}``（可带 disabled）。

    **显示什么和回填什么是两件事**：带 id 的候选（数据源、对象）此前只能写成「名称｜id」，
    那串 id 就直接糊在下拉里给人看。模型给纯字符串时 label = value，行为不变。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"label": text, "value": text})
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") if item.get("value") is not None else "").strip()
        label = str(item.get("label") or value).strip()
        if not value:
            value = label
        if not label:
            continue
        option: dict[str, Any] = {"label": label[:120], "value": value[:255]}
        if item.get("disabled"):
            option["disabled"] = True
        out.append(option)
    return out


def _match_option(field: dict, value: Any) -> Any | None:
    """把一个预填值对到该字段的真实候选上；对不上返回 None。

    对不上就**丢掉**：一个听错的库名若原样落进 default，用户看到的是一张「系统已经替我
    确认过」的表单，而它是错的——错得比空着更贵。
    """
    options = field.get("options") or []
    if not options:
        # 无候选的字段（text/number/cron/…）自由取值，原样采用。
        return value
    text = str(value).strip()
    if not text:
        return None
    if field.get("type") == "autocomplete":
        # 候选是**建议**不是闭集：分区键可以是本体没建模的物理列，对不上也照填。
        for option in options:
            if text in (option["value"], option["label"]):
                return option["value"]
        return text
    for option in options:
        if option.get("disabled"):
            continue  # 选不了的候选不能被预填绕过（如执行侧不支持的装载方式）
        if text == option["value"] or text == option["label"]:
            return option["value"]
    lowered = text.lower()
    for option in options:
        if option.get("disabled"):
            continue
        if lowered in (option["value"].lower(), option["label"].lower()):
            return option["value"]
    # 退到「唯一子串命中」：用户说「落到 dw 库」，候选是「仓库（hive） → dw」。
    hits = [
        o for o in options
        if not o.get("disabled") and (lowered in o["label"].lower() or lowered in o["value"].lower())
    ]
    return hits[0]["value"] if len(hits) == 1 else None


def _apply_prefill(fields: list[dict], raw: Any) -> list[str]:
    """把模型读到的「用户已经说过的取值」核对后填成默认值。返回命中的字段名。"""
    if not isinstance(raw, dict):
        return []
    by_name = {f["name"]: f for f in fields}
    hit: list[str] = []
    for name, value in raw.items():
        field = by_name.get(str(name).strip())
        if field is None or value is None or value == "":
            continue
        if field["type"] == "multiselect":
            values = value if isinstance(value, list) else [value]
            matched = [m for m in (_match_option(field, v) for v in values) if m is not None]
            if matched:
                field["default"] = matched
                hit.append(field["name"])
            continue
        if isinstance(value, list):
            continue
        matched = _match_option(field, value)
        if matched is not None:
            field["default"] = matched
            hit.append(field["name"])
    return hit


_REQUEST_FORM_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_form",
        "description": (
            "生成可填写表单。"
            "数据任务提供 task_kind 和 intent，服务端生成需求、本体或口径、数据参数三个确认步骤。"
            "fields 可用于普通分析表单；任务表单可留空。"
            "prefill 可填写用户已经给出的值。"
            "表单提交后将 task_confirmation_id 和字段值用于 propose_action。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "表单标题：这一步在收集什么（一句话）"},
                "task_kind": {
                    "type": "string", "enum": list(_ACTION_KINDS),
                    "description": "建数任务填 materialize/sync/transform/metric；服务端生成完整确认向导",
                },
                "target_datasource_id": {
                    "type": "string",
                    "description": "task_kind=materialize 且已定下数据源时给，服务端会据此把「目标库」也列成下拉",
                },
                "intent": {"type": "string", "description": "可选：为什么需要这些信息（一句话辅助说明）"},
                "submit_label": {"type": "string", "description": "可选：提交按钮文案，缺省「提交」"},
                "prefill": {
                    "type": "object",
                    "description": (
                        "可选：{字段名: 取值}，把用户在对话里**已经说过**的填成默认值。"
                        "取值可以是候选的 label 或 value（也接受能唯一命中的片段，如库名 dw）；"
                        "服务端核对不上的会被丢掉，故不必怕填错，但也别拿它编造用户没说过的东西"
                    ),
                },
                "fields": {
                    "type": "array",
                    "description": f"表单字段（1-{_FORM_MAX_FIELDS} 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                      "description": "字段标识（英文 snake_case，回填时作键）"},
                            "label": {"type": "string", "description": "字段中文标签"},
                            "type": {"type": "string", "enum": list(_FORM_FIELD_TYPES),
                                      "description": ("控件类型：text 单行/textarea 多行/number 数字/"
                                                      "select 单选下拉/multiselect 多选/radio 单选按钮/"
                                                      "boolean 开关/date 日期/autocomplete 带建议的文本框/"
                                                      "cron 调度选择器")},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "给人看的名称"},
                                        "value": {"type": "string",
                                                   "description": "回填给你的取值（id 类放这里，界面上不显示）"},
                                    },
                                    "required": ["label", "value"],
                                },
                                "description": (
                                    "select/radio/multiselect/autocomplete 的候选项（须来自真实实体）。"
                                    "**label 是给人看的、value 是回给你的**：带 id 的候选把 id 放 value，"
                                    "别把 id 塞进 label 糊在下拉里。纯字符串也接受（label = value）"
                                ),
                            },
                            "required": {"type": "boolean", "description": "是否必填，缺省 false"},
                            "placeholder": {"type": "string", "description": "可选：占位提示"},
                            "help": {"type": "string", "description": "可选：字段说明"},
                            "default": {"description": "可选：默认值（字符串/数字/布尔/数组）"},
                        },
                        "required": ["name", "label", "type"],
                    },
                },
            },
            "required": ["title", "fields"],
        },
    },
}

# ---- 数据应用车道：把本轮口径做成面板/看板 ----
# 此前这条路只有前端按钮（ChatBiReferences 的动作条 → /chat-bi/generate-widget|generate-app），
# agent 手里只有 render_chart（对话内画图、不落库），于是它能画给你看却交付不了东西。
# 这两个工具把那两个端点接进工具循环，**不换机制**：仍是「agent 只出提案、写在用户点击」。
#
# 口径从哪来：面板的绑定由服务端**复用本轮回答的 caliber_decomposition + referenced_objects**
# 重建（generate_widget_from_chat 的既有保证：带了载荷就不重调 LLM）。所以模型不需要、也不
# 应该自己写 SQL 或列字段——它只决定「叫什么、用哪种图」。

# 面板可视化类型。**只列渲染器真支持的三种**：DashboardGrid.renderBody 里 bar / kpi 各有
# 渲染器，其余一律回退表格。多列一个 line/pie 只会让模型提出一个渲染不出来的面板。
_PANEL_VIZ_TYPES: tuple[str, ...] = ("bar", "kpi", "table")

_PROPOSE_PANEL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_panel",
        "description": (
            "把**本轮回答的口径**做成一个数据面板的**提案**（不写库）。面板=一个数据逻辑，"
            "用户点确认后才真正生成，并由用户选择加入哪个看板（可新建）。\n"
            "**前置条件：本轮必须先对主对象调过 get_object**——面板要绑定一个主对象，"
            "只 search_objects 或只 run_sql 都凑不出来，缺了会被当场判错。\n"
            "面板绑定由服务端复用本轮回答重建，你**不用也不该**自己写 SQL 或列字段，只需给标题与图型。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "面板标题（中文，简短），如「各渠道订单量」"},
                "viz_type": {
                    "type": "string", "enum": list(_PANEL_VIZ_TYPES),
                    "description": "bar=柱状（有分组维度时用）/ kpi=指标卡（单值）/ table=表格（明细）",
                },
            },
            "required": ["title", "viz_type"],
        },
    },
}

_PROPOSE_DASHBOARD_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_dashboard",
        "description": (
            "为**新建一个看板**产出提案（不写库）：以本轮口径生成首个面板并放进新看板。"
            "用户点确认后才创建，随后可在看板编辑器里继续加面板。\n"
            "**要往已有看板里加一块**用 propose_panel，别为此新建看板。"
            "同样要求本轮真取到数或编译过口径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "看板名称（中文，简短），如「渠道销售看板」"},
                "panel_title": {"type": "string", "description": "首个面板的标题；缺省用看板名"},
                "viz_type": {
                    "type": "string", "enum": list(_PANEL_VIZ_TYPES),
                    "description": "首个面板的图型，同 propose_panel；缺省 bar",
                },
            },
            "required": ["name"],
        },
    },
}

# ---- 接数据车道：连数据源 + 生成本体草稿 ----
# 这条路此前整段在 UI 里（设置页配 DataHub / 数据源页建连接 / 工作区点「生成草稿」），
# 对话只能从「已经接好的数据」开始。三个工具补上前半段，仍是提案制。
#
# **诚实边界**：ontoMeta 不触发 DataHub 自身的采集（connectors/datahub.py 没有 ingestion API），
# 域与 dataset 是 DataHub 那边爬好后同步过来的。所以这里没有「一键把某个库爬进来」的工具，
# 只有「登记一个可查询的数据源」与「让 LLM 从已同步的元数据生成本体草稿」。

# 可提案的数据源类型。与 data_app._HOST_DSN_KINDS / _FILE_DSN_KINDS 对齐——超出这些的
# kind 建出来也拼不出 DSN。
_DATASOURCE_KINDS: tuple[str, ...] = (
    "mysql", "postgres", "starrocks", "doris", "hive", "clickhouse", "sqlite", "duckdb",
)

# 草稿生成范围 → workspace 的三个端点（generate-draft / -objects / -relations）。
_DRAFT_SCOPES: tuple[str, ...] = ("draft", "objects", "relations")

_LIST_ONBOARDING_TARGETS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_onboarding_targets",
        "description": (
            "读取接数据时**可选什么**：DataHub 配没配、已同步的数据域（各自草稿/发布状态与对象数）、"
            "已登记的数据源（id/名称/类型/catalog/连通状态）。\n"
            "**接数据开工第一步就调它**：propose_datasource 要靠它避免建重复的源，"
            "propose_ontology_draft 的 domain_id 必须是这里返回的真实域 id，不得自己编。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_PROPOSE_DATASOURCE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_datasource",
        "description": (
            "为**新登记一个数据源连接**产出提案（不写库）。mysql/postgres 等是 business_source，"
            "只供 Flink 同步读取；Doris 是唯一 warehouse，Data Agent 只查询默认 Doris。\n"
            "**绝不要在参数里写主机地址以外的凭据**：用户名/密码/DSN 一律由用户在确认卡里自己填，"
            "你给的是名称、类型、catalog 这类非机密骨架。带凭据的字段会被丢弃。\n"
            "先调 list_onboarding_targets 查重——同名或同 catalog 的源已存在就别重复建。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "数据源显示名，如「ERP 生产库（只读）」"},
                "kind": {"type": "string", "enum": list(_DATASOURCE_KINDS),
                          "description": "数据源类型"},
                "catalog_name": {
                    "type": "string",
                    "description": (
                        "可选的外部 catalog 元数据，仅用于元数据登记；不参与 Data Agent 查询路由"
                    ),
                },
                "note": {"type": "string", "description": "可选：给用户看的一句话说明（这个源是干什么的）"},
            },
            "required": ["name", "kind"],
        },
    },
}

_PROPOSE_ONTOLOGY_DRAFT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_ontology_draft",
        "description": (
            "为某个数据域**生成本体草稿**产出提案（不写库、不启动生成）。用户点确认后才真正启动"
            "LLM 草稿生成，产出的对象/关系仍需在工作区人工确认、再发布。\n"
            "domain_id 必须来自 list_onboarding_targets。scope：draft=对象+关系全量（首次用它）、"
            "objects=只补业务对象、relations=只补业务关系（需已有含对象的草稿）。\n"
            "该域已有发布本体时要提醒用户：重跑会产生新草稿并进入合并流程，不是原地覆盖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain_id": {"type": "string", "description": "数据域 id（来自 list_onboarding_targets）"},
                "scope": {"type": "string", "enum": list(_DRAFT_SCOPES),
                           "description": "draft=全量 / objects=只对象 / relations=只关系；缺省 draft"},
                "reason": {"type": "string", "description": "可选：为什么现在要生成（给用户看的一句话）"},
            },
            "required": ["domain_id"],
        },
    },
}

# ========== 建模工单工具 ==========

_CREATE_MODELING_CASE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_modeling_case",
        "description": (
            "创建建模工单，记录用户的分析/建模需求。\n"
            "当用户明确表达要做一个完整的分析、报表、数据应用，或需要持续跟进的建模任务时使用。\n"
            "创建后会自动进入需求确认阶段，需要进一步澄清业务目标、分析粒度、主体对象等关键信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "工单标题，简明描述建模目标（如：销售延期分析、客户标签体系、渠道绩效看板）",
                },
                "business_goal": {
                    "type": "string",
                    "description": "业务目标，用户希望通过这次建模解决什么问题",
                },
                "primary_domain_id": {
                    "type": "string",
                    "description": "主数据域 ID（可选，来自 get_domain_overview 或 list_onboarding_targets）",
                },
                "domain_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "涉及的数据域列表（可选）",
                },
            },
            "required": ["title", "business_goal"],
        },
    },
}

_UPDATE_REQUIREMENT_SPEC_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_requirement_spec",
        "description": (
            "更新建模工单的需求规格。\n"
            "在需求确认阶段，根据对话逐步完善关键信息：业务目标、分析粒度、主体对象、"
            "时间范围、度量需求、维度需求、交付形式等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "工单 ID"},
                "business_goal": {"type": "string", "description": "业务目标（可选，补充或更新）"},
                "analysis_scope": {"type": "string", "description": "分析范围（可选，如：全国、华东区、线上渠道）"},
                "primary_subject": {"type": "string", "description": "主体对象（如：订单、客户、商品）"},
                "grain": {"type": "string", "description": "分析粒度（如：每笔订单、每个客户每天、每个SKU每月）"},
                "time_range": {"type": "string", "description": "时间范围（如：近一年、2023年、最近30天）"},
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                    "description": "度量列表（可选，如：[{name: '销售额', description: '订单金额汇总'}]）",
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "维度列表（可选，如：['地区', '渠道', '类目']）",
                },
                "deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "交付形式（可选，如：['看板', '定期报表', '标签']）",
                },
            },
            "required": ["case_id"],
        },
    },
}

_CONFIRM_REQUIREMENT_SPEC_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "confirm_requirement_spec",
        "description": (
            "确认需求规格，推进工单到下一阶段（本体确认）。\n"
            "仅当关键信息（业务目标、主体对象、分析粒度）已明确且用户表示确认时调用。\n"
            "确认后需求规格将被锁定，后续修改需要新版本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "工单 ID"},
            },
            "required": ["case_id"],
        },
    },
}

_GET_MODELING_CASE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_modeling_case",
        "description": (
            "查询当前会话关联的建模工单详情。\n"
            "返回工单状态、当前阶段、已确认的规格、待确认的草稿等信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

_PROPOSE_DIMENSIONAL_MODEL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_dimensional_model",
        "description": (
            "提议一个维度模型（星型/雪花模型）设计方案。\n"
            "在已确认本体和数据后，基于业务过程和粒度，设计事实表和维度表。\n"
            "事实表包含度量和维度键；维度表包含代理键、自然键、属性和 SCD 策略。\n"
            "用户确认后，可以编译为物化契约并生成 DDL/ETL。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modeling_case_id": {
                    "type": "string",
                    "description": "关联的建模工单 ID（可选）",
                },
                "domain_id": {
                    "type": "string",
                    "description": "数据域 ID",
                },
                "ontology_id": {
                    "type": "string",
                    "description": "本体 ID",
                },
                "name": {
                    "type": "string",
                    "description": "模型名称（如：order_star_model）",
                },
                "display_name": {
                    "type": "string",
                    "description": "显示名称（如：订单分析星型模型）",
                },
                "business_process": {
                    "type": "string",
                    "description": "业务过程描述（如：客户在线下单购买商品）",
                },
                "grain": {
                    "type": "string",
                    "description": "粒度声明（如：每笔订单的每个商品明细行）",
                },
                "fact_tables": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "事实表名（如：fct_order_line）"},
                            "display_name": {"type": "string", "description": "显示名称"},
                            "source_object_id": {"type": "string", "description": "来源本体对象 ID"},
                            "measures": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "field": {"type": "string"},
                                        "additive_type": {
                                            "type": "string",
                                            "enum": ["additive", "semi_additive", "non_additive"],
                                            "description": "可加性：additive=完全可加, semi_additive=半可加, non_additive=不可加",
                                        },
                                    },
                                },
                            },
                            "dimension_keys": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "维度键列表（如：['customer_key', 'product_key', 'order_date_key']）",
                            },
                            "degenerate_dimensions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "退化维度（如：['order_id', 'line_number']）",
                            },
                        },
                    },
                    "description": "事实表设计列表",
                },
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "维度表名（如：dim_customer）"},
                            "display_name": {"type": "string"},
                            "source_object_id": {"type": "string", "description": "来源本体对象 ID（可选）"},
                            "natural_key": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "自然键（如：['customer_code']）",
                            },
                            "surrogate_key": {"type": "string", "description": "代理键（如：customer_key）"},
                            "scd_type": {
                                "type": "string",
                                "enum": ["none", "scd1", "scd2"],
                                "description": "缓慢变化维度类型",
                            },
                            "scd_config": {
                                "type": "object",
                                "properties": {
                                    "effective_date": {"type": "string"},
                                    "expiration_date": {"type": "string"},
                                    "current_flag": {"type": "string"},
                                },
                                "description": "SCD2 配置（仅当 scd_type=scd2 时需要）",
                            },
                            "attributes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "field": {"type": "string"},
                                    },
                                },
                                "description": "维度属性列表",
                            },
                        },
                    },
                    "description": "维度设计列表",
                },
                "conformed_dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dimension_name": {"type": "string"},
                            "shared_across_facts": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "description": {"type": "string"},
                        },
                    },
                    "description": "一致性维度（可选）",
                },
                "model_type": {
                    "type": "string",
                    "enum": ["star", "snowflake", "constellation"],
                    "description": "模型类型，默认 star",
                },
                "description": {
                    "type": "string",
                    "description": "模型描述（可选）",
                },
            },
            "required": [
                "domain_id",
                "ontology_id",
                "name",
                "display_name",
                "business_process",
                "grain",
                "fact_tables",
                "dimensions",
            ],
        },
    },
}

_PROPOSE_LOGIC_BATCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_logic_batch",
        "description": (
            "批量生成一组指标、标签或规则的形式化表达式。\n"
            "适用于基于同一业务主体或维度模型生成多个相关口径的场景。\n"
            "每个口径都会经过独立的编译验证，共享粒度、对象和时间定义。\n"
            "用户可以一次确认整批口径，系统会分别落成独立可治理制品。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modeling_case_id": {
                    "type": "string",
                    "description": "关联的建模工单 ID（可选）",
                },
                "domain_id": {
                    "type": "string",
                    "description": "数据域 ID",
                },
                "ontology_id": {
                    "type": "string",
                    "description": "本体 ID",
                },
                "dimensional_model_id": {
                    "type": "string",
                    "description": "关联的维度模型 ID（可选，如果基于维度模型生成）",
                },
                "business_subject": {
                    "type": "string",
                    "description": "业务主体描述（如：订单分析、客户画像）",
                },
                "shared_context": {
                    "type": "object",
                    "properties": {
                        "grain": {"type": "string", "description": "共享粒度"},
                        "primary_object_id": {"type": "string", "description": "主要对象 ID"},
                        "time_dimension": {"type": "string", "description": "时间维度字段"},
                        "default_time_range": {"type": "string", "description": "默认时间范围"},
                    },
                    "description": "共享上下文：粒度、对象、时间定义等",
                },
                "logics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "口径名称（英文标识）"},
                            "display_name": {"type": "string", "description": "显示名称（中文）"},
                            "logic_type": {
                                "type": "string",
                                "enum": ["metric", "tag", "rule"],
                                "description": "口径类型：metric=指标, tag=标签, rule=规则",
                            },
                            "summary": {"type": "string", "description": "业务含义说明"},
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "引用的字段列表（用于编译验证）",
                            },
                            "body": {
                                "type": "object",
                                "description": "表达式结构（AST），参考 propose_expression 的 body 结构",
                            },
                        },
                        "required": ["name", "display_name", "logic_type", "fields", "body"],
                    },
                    "description": "批量口径列表，每个包含名称、类型和表达式",
                },
                "deduplication": {
                    "type": "boolean",
                    "description": "是否自动查重（默认 true）",
                },
            },
            "required": [
                "ontology_id",
                "business_subject",
                "logics",
            ],
        },
    },
}

# 基础工具集 = 12 检索/执行工具 + select_skill + 记忆提案 + 交互表单；技能激活后再并上其 extra 工具。
# read_result 不入基础集（V4 O3 渐进披露）：只在首次 run_sql 取到结果后动态解锁。
_BASE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    *_AGENT_TOOL_SCHEMAS, _SELECT_SKILL_TOOL, _PROPOSE_PREFERENCE_TOOL, _REQUEST_FORM_TOOL,
]
_TOOL_BY_NAME: dict[str, dict[str, Any]] = {
    t["function"]["name"]: t
    for t in [
        *_BASE_TOOL_SCHEMAS,
        _READ_RESULT_TOOL,
        _RENDER_CHART_TOOL,
        _ANALYZE_RESULT_TOOL,
        _SCOUT_QUERY_TOOL,
        _GET_LINEAGE_TOOL,
        _PROPOSE_DRAFT_TOOL,
        _PROPOSE_EXPRESSION_TOOL,
        _LINT_TOOL,
        _PROPOSE_ACTION_TOOL,
        _PROPOSE_PIPELINE_TOOL,
        _GET_TASK_OPTIONS_TOOL,
        _GET_TASK_STATUS_TOOL,
        _UPDATE_PLAN_TOOL,
        _PROPOSE_PANEL_TOOL,
        _PROPOSE_DASHBOARD_TOOL,
        _LIST_ONBOARDING_TARGETS_TOOL,
        _PROPOSE_DATASOURCE_TOOL,
        _PROPOSE_ONTOLOGY_DRAFT_TOOL,
        _CREATE_MODELING_CASE_TOOL,
        _UPDATE_REQUIREMENT_SPEC_TOOL,
        _CONFIRM_REQUIREMENT_SPEC_TOOL,
        _GET_MODELING_CASE_TOOL,
        _PROPOSE_DIMENSIONAL_MODEL_TOOL,
        _PROPOSE_LOGIC_BATCH_TOOL,
    ]
}
# 所有可能出现的工具名——供 FactLedger 登记为可信上下文（工具名非业务实体）。
_ALL_AGENT_TOOL_NAMES: list[str] = list(_TOOL_BY_NAME.keys())

# 取数（写 SQL / 产出可执行 SQL）工具——结构性问题下按意图门控从工具集移除并在 dispatch 硬拒。
_SQL_TOOL_NAMES: frozenset[str] = frozenset({"run_sql", "compile_metric"})

# 意图分类关键词（规则优先、确定性；无额外 LLM 调用）。
# 结构性=纯元数据/结构问题（对象有哪些属性/字段/关系、口径定义），应基于本体元数据直接作答；
# 取数=需要实际查库出数。**analytical 赢平局**（fail-open）：宁可多给取数能力，绝不误伤真实取数。
# 注意：取数标记只收「聚合动词/量词」（多少/统计/求和…），**不收营收名词**（金额/总额/总数）——
# 后者是指标名/字段名的组成部分（如指标「订单总额」），会把「…的定义」这类结构问题误判成取数。
_ANALYTICAL_MARKERS: tuple[str, ...] = (
    "多少", "数量", "几个", "趋势", "环比", "同比", "对比", "占比",
    "比例", "排名", "排行", "top", "明细", "统计", "近", "分布", "增长",
    "均值", "平均", "合计", "汇总", "求和", "计数",
    # 时间窗——问「某时段的业务表现」即在要真数据（即便没写聚合动词）。
    # 与结构问题正交（结构问对象/字段/口径定义，不带时段），加进来不误伤结构分类。
    "今年", "去年", "本年", "本月", "上月", "当月", "本周", "上周",
    "本季", "季度", "年度", "今日", "当日", "昨天",
)
_STRUCTURAL_MARKERS: tuple[str, ...] = (
    "有哪些", "哪些属性", "哪些字段", "包含哪些", "由哪些", "组成", "构成",
    "属性", "字段", "结构", "定义", "关系", "关联", "主键", "外键",
    "数据类型", "schema", "元数据", "是什么意思", "怎么理解",
)
# 接地判定的适用范围由「需要精准回答的意图」正向定义，而非反向枚举「哪些不必拒答」：
# 只有 analytical（要真数据）和 structural（要具体本体元数据）**才要求接地**——答业务事实
# 却没查过就该拦。其余一切（打招呼、问能力、产品用法/how-to、一般解释）默认 general，
# 自由作答、不要求接地，因此**无需维护一张一般问题白名单**（默认即豁免）。
# 即便 general 里混进真本体问题，F4 断言校验仍拦得住捏造的具名实体/数值，是最后一道网。


def _tools_for_skill(
    skill: "Skill | None", *, sql_allowed: bool = True
) -> list[dict[str, Any]]:
    """当前可用工具集：基础工具 + 激活技能解锁的额外工具（只增不减）。

    ``sql_allowed=False``（结构性问题）时**收窄**——从工具集移除取数工具
    （run_sql / compile_metric），让模型在 API 层就无法调用它们。这是「只解锁不收窄」
    的意图门控例外：默认 True 保持旧行为，仅结构性 turn 显式收窄。
    """
    base = _BASE_TOOL_SCHEMAS if skill is None else [
        *_BASE_TOOL_SCHEMAS,
        *[_TOOL_BY_NAME[n] for n in skill.extra_tool_names if n in _TOOL_BY_NAME],
    ]
    if sql_allowed:
        return base
    return [t for t in base if t["function"]["name"] not in _SQL_TOOL_NAMES]


def _search_items(result: Any) -> list[dict]:
    """取检索类工具结果里的条目列表。

    兼容三种形态：完整信封 ``{items:[…]}``、截断信封 ``{sample:[…]}``（P2.3）、
    以及历史的裸列表。
    """
    if isinstance(result, dict):
        result = result.get("items") or result.get("sample")
    if not isinstance(result, list):
        return []
    return [x for x in result if isinstance(x, dict)]


def _format_sql(sql: str | None) -> str | None:
    """使用 sqlparse 美化 SQL；失败时原样返回。"""
    if not sql or not sql.strip():
        return sql
    try:
        formatted = sqlparse.format(
            sql,
            reindent=True,
            keyword_case="upper",
            strip_comments=False,
            use_space_around_operators=True,
        )
        return formatted.rstrip()
    except Exception:
        return sql



__all__ = [
    '_AGENT_MAX_STEPS',
    '_AGENT_REPAIR_ATTEMPTS',
    '_RUN_SQL_LIMIT',
    '_TOOL_RESULT_MAX_CHARS',
    '_SQL_TIMEOUT_SECONDS',
    '_SEARCH_LIMIT',
    '_OVERVIEW_LIST_LIMIT',
    '_OVERVIEW_TOP_CONNECTED',
    '_AGENT_SYSTEM_PROMPT',
    '_MINIMAL_AGENT_SYSTEM_PROMPT',
    '_AGENT_TOOL_SCHEMAS',
    '_SELECT_SKILL_TOOL',
    '_RENDER_CHART_TOOL',
    '_ANALYZE_RESULT_TOOL',
    '_READ_RESULT_TOOL',
    '_SCOUT_QUERY_TOOL',
    '_GET_LINEAGE_TOOL',
    '_PROPOSE_DRAFT_TOOL',
    '_PROPOSE_EXPRESSION_TOOL',
    '_LINT_TOOL',
    '_ACTION_KINDS',
    '_ACTION_KIND_LABEL',
    '_PIPELINE_KINDS',
    '_PIPELINE_MAX_STEPS',
    '_PROPOSE_ACTION_TOOL',
    '_PROPOSE_PIPELINE_TOOL',
    '_CRON_PRESETS',
    '_LOAD_STRATEGIES',
    '_TASK_OPTIONS_LIMIT',
    '_GET_TASK_OPTIONS_TOOL',
    '_AUTO_ACTION_CONTEXT_KEYS',
    '_ACTION_CONTEXT_HINT',
    '_missing_action_context',
    '_sync_context_errors',
    '_action_context_candidates',
    '_GET_TASK_STATUS_TOOL',
    '_PLAN_STATUSES',
    '_PLAN_MAX_STEPS',
    '_UPDATE_PLAN_TOOL',
    '_PROPOSE_PREFERENCE_TOOL',
    '_FORM_FIELD_TYPES',
    '_FORM_MAX_FIELDS',
    '_FORM_DATASOURCE_PROBE_LIMIT',
    '_FORM_LOCATION_LIMIT',
    '_normalize_form_options',
    '_match_option',
    '_apply_prefill',
    '_REQUEST_FORM_TOOL',
    '_PANEL_VIZ_TYPES',
    '_PROPOSE_PANEL_TOOL',
    '_PROPOSE_DASHBOARD_TOOL',
    '_DATASOURCE_KINDS',
    '_DRAFT_SCOPES',
    '_LIST_ONBOARDING_TARGETS_TOOL',
    '_PROPOSE_DATASOURCE_TOOL',
    '_PROPOSE_ONTOLOGY_DRAFT_TOOL',
    '_CREATE_MODELING_CASE_TOOL',
    '_UPDATE_REQUIREMENT_SPEC_TOOL',
    '_CONFIRM_REQUIREMENT_SPEC_TOOL',
    '_GET_MODELING_CASE_TOOL',
    '_BASE_TOOL_SCHEMAS',
    '_TOOL_BY_NAME',
    '_ALL_AGENT_TOOL_NAMES',
    '_SQL_TOOL_NAMES',
    '_ANALYTICAL_MARKERS',
    '_STRUCTURAL_MARKERS',
    '_tools_for_skill',
    '_search_items',
    '_format_sql',
]
