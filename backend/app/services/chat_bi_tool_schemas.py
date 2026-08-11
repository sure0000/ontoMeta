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
    "你是企业数据问答助手（Data Agent），基于**已发布本体**回答业务问题。\n"
    "可多步调用工具检索本体并执行只读 SQL，像分析师一样先查清口径再作答。\n\n"
    "若问题明确属于某类任务（了解域结构 / 取数分析），可先调 select_skill 选对应技能，"
    "它会给出该类任务的专门策略与能力；不确定则直接按下面的通用策略作答。\n\n"
    "工具选择：\n"
    "1. 先 search_* 定位实体，再 get_object / get_logic 取细节。"
    "**若关键词要来回试几次**（域大、说法不确定），改用 locate_entities 整体外包，"
    "试错过程不占本对话上下文。\n"
    "2. 【JOIN】SQL 涉及两个及以上对象时先调 **find_join_path**，ON 条件照抄它给的；"
    "它返回空即表示本体中两者无从关联，如实说明。有扇出风险时改用它建议的安全聚合。\n"
    "3. 【字面量】WHERE 要写具体值时先调 **profile_values** 看真实取值并照抄；"
    "猜错的字面量不会报错，只会返回 0 行，让你得出「无数据」这个错误结论。\n"
    "4. 【口径】问题涉及已有指标/标签/规则时先 search_logics，再用 **compile_metric** 编译出 SQL，"
    "不要自己照着口径说明重写——口径以本体为准，重写会算出与其它系统不一致的数。"
    "编译结果的 caliber_trace 即权威口径展开；失败时按 hint 修正。\n"
    "5. 写了查询 SQL 就必须经 **run_sql** 提交（只读，仅 SELECT）；"
    "返回「无可执行数据源」时作为建议 SQL 给出并说明未实际执行。\n"
    "6. 【缺口反问】只有用户能补齐的歧义（多个候选指标/时间字段、口径值不明确），"
    "调 **ask_clarification** 反问，不要挑一个可能错的解释硬答；"
    "但「本体里查不到」应如实说明，「你还没查够」应继续检索——都不属于反问。\n"
    "6b.【多参数收集】若需用户**一次补齐多个**结构化参数（取数的指标+时间范围+分组维度、"
    "建数任务的目标数据源+目标库+更新策略+调度）才能继续，调 **request_form** 生成可填写表单"
    "一并收集，比逐条追问高效；候选项须来自真实实体——**先用工具把候选查出来再发表单**"
    "（建数任务用 get_task_options），别因为没查就把本该是下拉的字段退化成文本框。"
    "用户在对话里**已经说过**的取值走 prefill 预填进表单，别原样再问一遍。"
    "仅单个歧义仍用 ask_clarification。\n"
    "7. 【结构性问题】问对象有哪些属性/字段/关系、口径定义这类元数据问题，"
    "直接用 get_object/get_logic 的本体元数据作答，**不要取数、不要在正文写取数 SQL**；"
    "此时取数工具可能不可用。用户确实要据此查数时，再 select_skill('query') 切到取数。\n\n"
    "作答：中文 Markdown，先口径解读再结论；有数据用表格。"
    "用对象/关系的**显示名**称呼它们，不要在正文提及工具名。"
    "本体中确实没有的，如实说明无法回答。"
    "但对『你能做什么/怎么用/打招呼』这类与具体业务数据无关的一般性问题，"
    "直接友好作答、简介你的能力即可，无需检索本体、也不受上述『查不到就拒答』约束。"
)

# OpenAI 原生 function-calling 工具（自建 GLM 实测支持）。
# 检索类直呼 OntologyQueryService（带 q 关键词 + limit），避免 MCP 目录“仅按域全返”导致的巨结果。
_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_catalogs",
            "description": (
                "列出当前数据域可查询的数据目录（catalog）。取数前先看这里拿合法 target 值："
                "warehouse（数仓投影，默认）或各源库 catalog 名（如 erp/crm，实时源数据）。"
                "run_sql 的 target 参数只收这里返回的名字，别自己编。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
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
                "在当前数据域的物理数据源上执行**只读 SELECT**，返回列与真实数据行。"
                "表名/字段名必须使用本体标识符。若无可执行数据源会返回提示，此时改为给出建议 SQL。"
                "默认查数仓投影（warehouse）；确需查源库时才显式传 target（如 \"erp\"），"
                "不传时绝不直连源库。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句"},
                    "limit": {"type": "integer", "description": f"返回行上限，默认 {_RUN_SQL_LIMIT}"},
                    "target": {
                        "type": "string",
                        "description": (
                            "目标目录：warehouse（默认，查数仓投影）；或显式源库 catalog 名"
                            "（如 erp/crm，查源系统实时数据）。默认 warehouse，不传不查源库。"
                        ),
                    },
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
_ACTION_KINDS: tuple[str, ...] = ("materialize", "sync", "transform")
_ACTION_KIND_LABEL: dict[str, str] = {
    "materialize": "物化", "sync": "同步", "transform": "加工", "metric": "聚合",
}

# 任务链上可以出现的环节。比 _ACTION_KINDS 多一个 metric——「物化完清洗、清洗完聚合」里的
# 聚合就是它（按已定义的业务逻辑口径产聚合 SQL）。单发提案不开 metric 是怕与口径提案
# （propose_draft 产的是**口径定义**）混淆；在链里它的位置明确，不存在这个歧义。
# cluster（基建）不进链：它与数据加工不是一条流水线上的事。
_PIPELINE_KINDS: tuple[str, ...] = ("materialize", "sync", "transform", "metric")
_PIPELINE_MAX_STEPS: int = 8

_PROPOSE_ACTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_action",
        "description": (
            "为新建一个数据任务（物化 materialize / 同步 sync / 加工 transform）产出**提案**"
            "（不执行、不写库）。提案由用户在前端点击后走「校验→看 dry-run 差异→人工确认→执行」"
            "的既有治理流程创建并运行。提案前先把「要对哪张/哪些表做什么」说清楚。\n"
            "**物化必须在 context 里给 target_datasource_id**（落到哪个数据源，本体推导不出来）。"
            "不知道选哪个就先调 request_form 让用户选，别自己编 id；缺了会被当场判错并给出真实候选。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "物化 materialize / 同步 sync / 加工 transform"},
                "intent": {"type": "string",
                            "description": "自然语言任务意图，如「把客户主数据物化到数仓 dim_customer」"},
                "context": {"type": "object",
                             "description": (
                                 "结构化上下文，取值一律来自 get_task_options 的真实候选。物化常用："
                                 "target_datasource_id（必填）、target_database（目标库，一个库通吃各层；"
                                 '要逐层分库才用 database_overrides={"层":"库名"}）、'
                                 'table_overrides={"契约id":"表名"}、refresh_cron（整批调度）、'
                                 'selected_targets=["实体名"]（只物化其中几个）、'
                                 'overrides={"契约id":{"partition_key":"dt","load_strategy":"incremental"}}'
                                 "（逐实体的分区键/装载方式/调度）。凭据只能传 *_ref/*_alias，勿传明文"
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
            "目标库/引擎接到下游，用户不必逐步重报。\n"
            "**只有一个任务时用 propose_action**，别为单步套一条链。\n"
            "**上游给过的选项下游不用再给**：target_datasource_id / target_database / engine 沿链"
            "继承。第一步是 materialize 时它仍必须自己给 target_datasource_id（不知道就先 "
            "request_form 问）。metric（聚合）要求口径已在「业务逻辑」里定义好，否则起草时会报错。"
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
                                "description": "物化 materialize / 同步 sync / 清洗加工 transform / 聚合 metric",
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

# 装载方式的中文标签与**实际语义**。语义不能省：CDC 在物化里其实按全量跑，模型若照字面
# 理解会给用户一个错误的承诺（与 MaterializeModal 的 STRATEGY_HINT 是同一句话）。
_LOAD_STRATEGIES: tuple[dict[str, str], ...] = (
    {"value": "full", "label": "全量覆盖",
     "hint": "INSERT OVERWRITE：重写整表/分区"},
    {"value": "incremental", "label": "增量追加",
     "hint": "INSERT INTO：按分区键追加，水位由调度器注入；未配分区键会退化为无谓词追加"},
    {"value": "cdc", "label": "CDC 变更捕获",
     "hint": "物化内不承载变更捕获，本次按全量覆盖执行；要 CDC 请改用同步作业"},
)

# 候选清单的回灌上限。物化契约会有几百条（一个 734 对象的域即如此），整份倒进上下文
# 既挤爆预算也没人读——按 search_* 的既有约定给 {total, returned, truncated, items}。
_TASK_OPTIONS_LIMIT: int = 30

_GET_TASK_OPTIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_task_options",
        "description": (
            "读取新建数据任务时**可选什么**：物化的目标数据源/目标库/待物化实体（含各自的"
            "契约 id、分层、分区键、装载方式、现有调度）/装载方式/调度频率预置；同步与加工的"
            "候选对象。\n"
            "**建数任务开工第一步就调它**：propose_action 的 context 与 request_form 的 "
            "options 都必须用这里返回的真实值，不得自己编数据源 id、库名或表名。"
            "只读，不建任何东西。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "要建的任务类型：materialize / sync / transform"},
                "target_datasource_id": {
                    "type": "string",
                    "description": "物化专用：给了才会去这个数据源上列库（databases），并按其类型定引擎",
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
    return [
        key
        for key in drafter.required_context
        if key not in _AUTO_ACTION_CONTEXT_KEYS and not context.get(key)
    ]


def _action_context_candidates(db: Session, missing: list[str]) -> dict[str, Any]:
    """缺失键的真实候选值——只说「缺 target_datasource_id」模型和用户都无从下手。

    只返回选项本身（id/名称/类型/连通状态），凭据不出现（DSN 存的本就是 ``dsn_secret_ref``）。
    """
    if "target_datasource_id" not in missing:
        return {}
    from app.models import DataSource

    rows = db.query(DataSource).order_by(DataSource.name).limit(50).all()
    return {
        "target_datasource_id_options": [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status} for s in rows
        ]
    }


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
            "当需要用户**一次补齐多个结构化参数**才能继续时，生成一张**可填写表单**收集上下文，"
            "而不是来回追问多轮。适用：取数需明确「指标+时间范围+分组维度」、"
            "建数任务需明确「目标表+更新策略+调度」等。"
            "select/radio/multiselect 的 options **必须来自工具返回的真实实体**；"
            "无从预置就用 text/number 让用户自填。调用后本轮结束、等用户填完提交再继续。"
            "只需单个澄清用 ask_clarification；无需用户补参数别乱发表单。\n"
            "**建数任务（物化/同步/加工）传 task_kind**：服务端会按该类型补齐必问字段并填入真实"
            "候选（物化=目标数据源与库/表名/装载方式/分区键/调度频率），你只需给 title；"
            "fields 可留空，给了则作为额外字段追加。这样不会漏问参数。\n"
            "**用户已经说过的用 prefill 预填**（如「物化到 dw 库、每天凌晨跑」）：服务端会拿真实"
            "候选核对，对得上就填成默认值、对不上就丢掉。别把已经问到的答案再问一遍。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "表单标题：这一步在收集什么（一句话）"},
                "task_kind": {
                    "type": "string", "enum": list(_ACTION_KINDS),
                    "description": "建数任务表单填这个（materialize/sync/transform），服务端据此补齐必问字段与真实候选",
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
        _LINT_TOOL,
        _PROPOSE_ACTION_TOOL,
        _PROPOSE_PIPELINE_TOOL,
        _GET_TASK_OPTIONS_TOOL,
        _GET_TASK_STATUS_TOOL,
        _UPDATE_PLAN_TOOL,
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
    '_AGENT_TOOL_SCHEMAS',
    '_SELECT_SKILL_TOOL',
    '_RENDER_CHART_TOOL',
    '_ANALYZE_RESULT_TOOL',
    '_READ_RESULT_TOOL',
    '_SCOUT_QUERY_TOOL',
    '_GET_LINEAGE_TOOL',
    '_PROPOSE_DRAFT_TOOL',
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
