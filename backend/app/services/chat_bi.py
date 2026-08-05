"""智能问数（ChatBI）服务：基于已发布本体知识 + LLM 回答业务问题。

流程：
1. 根据数据域解析出已发布本体（无则返回引导性提示）。
2. 组装「本体知识包」：对象、字段、关系、业务逻辑。
3. 调用 LLM 生成结构化回答（JSON：answer / suggested_sql / referenced_*）。
4. Mock 模式下基于关键词匹配规则生成示例性回答，保证链路可体验。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import types
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, joinedload

import sqlparse

from app.config import settings as env_settings

from app.governance import active_standard, lint_against_standard
from app.models import (
    BusinessLogic,
    ChatBiConversation,
    ChatBiConversationTask,
    ChatBiDomainMemory,
    ChatBiMessage,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.common import log_change, make_async_http_client
from app.services.query import OntologyQueryService
from app.services.settings_service import SettingsService
from app.services.ontology_projection import build_projection
from app.services.sql_soundness import SqlRejection, prove_sql_sound
from app.services import agent_telemetry
from app.services.agent_grounding import FactLedger
from app.services.agent_telemetry import RunTelemetry
from app.services.answer_verifier import verify_answer
from app.services.domain_semantic_card import build_card
from app.services import chat_bi_external_tools as external_tools
from app.services.chat_bi_skills import SKILLS, Skill, skill_choices_text
from app.services.tool_result_compaction import compact_tool_result

logger = logging.getLogger("ontometa.chat_bi")

_AGG_KEYWORDS = ("多少", "总数", "总量", "合计", "汇总", "统计", "count", "sum", "avg", "平均")
_TIME_KEYWORDS = ("最近", "近", "今日", "昨天", "本月", "上月", "近 7 天", "近7天", "近 30 天", "近30天")
_FILTER_KEYWORDS = ("按", "where", "筛选", "条件", "等于", "大于", "小于")


# ---------------------------------------------------------------------------
# Data Agent（工具编排）运行常量 —— 均衡档（见 DATA_AGENT_REDESIGN.md §9.1）
# ---------------------------------------------------------------------------
# P4.4 预算分离：工具轮与自愈轮各自计数，不再共用一个全局上限。
# 原来 6 步一刀切，「检索 → 读细节 → 写 SQL → 被拒 → 改 → 重跑」正好撑满，
# 此后又加了 find_join_path / profile_values / compile_metric 三个工具，
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
    "materialize": "物化", "sync": "同步", "transform": "加工",
}

_PROPOSE_ACTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_action",
        "description": (
            "为新建一个数据任务（物化 materialize / 同步 sync / 加工 transform）产出**提案**"
            "（不执行、不写库）。提案由用户在前端点击后走「校验→看 dry-run 差异→人工确认→执行」"
            "的既有治理流程创建并运行。提案前先把「要对哪张/哪些表做什么」说清楚。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ACTION_KINDS),
                          "description": "物化 materialize / 同步 sync / 加工 transform"},
                "intent": {"type": "string",
                            "description": "自然语言任务意图，如「把客户主数据物化到数仓 dim_customer」"},
                "context": {"type": "object",
                             "description": '可选结构化上下文，如 {"target_table":"dim_customer"}；凭据只能传 *_ref/*_alias，勿传明文'},
            },
            "required": ["kind", "intent"],
        },
    },
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

# 基础工具集 = 12 检索/执行工具 + select_skill；技能激活后再并上其 extra 工具。
_BASE_TOOL_SCHEMAS: list[dict[str, Any]] = [*_AGENT_TOOL_SCHEMAS, _SELECT_SKILL_TOOL, _PROPOSE_PREFERENCE_TOOL]
_TOOL_BY_NAME: dict[str, dict[str, Any]] = {
    t["function"]["name"]: t
    for t in [
        *_BASE_TOOL_SCHEMAS,
        _RENDER_CHART_TOOL,
        _ANALYZE_RESULT_TOOL,
        _GET_LINEAGE_TOOL,
        _PROPOSE_DRAFT_TOOL,
        _LINT_TOOL,
        _PROPOSE_ACTION_TOOL,
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


@dataclass
class _ObjectSnapshot:
    id: str
    name: str
    display_name: str
    description: str | None
    properties: list[Property]


class ChatBiService:
    """以本体知识为上下文，调用 LLM 回答业务提问。"""

    def __init__(self) -> None:
        self.query_service = OntologyQueryService()
        self.settings_service = SettingsService()

    # ------------------------------------------------------------------ public

    async def ask(
        self,
        db: Session,
        *,
        domain_id: str,
        question: str,
        history: list[dict] | None = None,
        principal_role: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")

        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            return {
                "domain_id": domain_id,
                "domain_name": domain.name,
                "ontology_id": None,
                "answer": (
                    f"「{domain.name}」当前还没有已发布的本体。"
                    "请先在「本体建模」中完成草稿编辑并发布，"
                    "Data Agent 会基于已发布本体的对象、字段、关系与业务逻辑进行解读。"
                ),
                "suggested_sql": None,
                "referenced_objects": [],
                "referenced_logics": [],
                "used_mock": True,
            }

        snapshots = self._load_ontology_snapshot(db, ontology.id)
        relations = self._load_relations(db, ontology.id)
        logics = self._load_logics(db, ontology.id)

        # 关键词命中：用于 Mock 兜底的接地判定，也作为 Agent 的检索种子提示
        grounded_objects = self._match_objects(question, snapshots)
        grounded_logics = self._match_logics(question, logics)

        runtime = self.settings_service.get_llm_runtime(db)
        use_mock = not runtime.api_key

        # 名称 -> 实体 索引用全量本体：把 LLM/工具输出的名称/伪 id 归一到真实 id 供前端跳转
        resolver = _ReferenceResolver(
            objects=snapshots, relations=relations, logics=logics
        )

        if use_mock:
            # Mock 路径：保持原有关键词接地 —— 无命中直接拒绝，避免编造
            if not grounded_objects and not grounded_logics:
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )
            payload = self._mock_answer(
                question=question,
                snapshots=snapshots,
                relations=relations,
                logics=logics,
                matched_objects=grounded_objects,
                matched_logics=grounded_logics,
            )
            payload = resolver.resolve_payload(payload)
            payload = self._enforce_grounded_refs(
                payload,
                grounded_objects=grounded_objects,
                grounded_logics=grounded_logics,
                resolver=resolver,
            )
            if not payload.get("referenced_objects") and not payload.get("referenced_logics"):
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )
        else:
            # Agent 路径：LLM 自主多步调用工具检索/跑数；refs/sql/data_result 从工具轨迹收割。
            # 不再一次性灌全量本体 —— 上下文由分页工具按需拉取，结构上根治 413。
            tel = RunTelemetry()
            try:
                payload = await self._run_agent_loop(
                    db,
                    runtime=runtime,
                    domain=domain,
                    ontology=ontology,
                    question=question,
                    history=history or [],
                    resolver=resolver,
                    seed_objects=grounded_objects,
                    seed_logics=grounded_logics,
                    principal_role=principal_role,
                    telemetry=tel,
                    conversation_id=conversation_id,
                )
            except Exception as exc:
                logger.exception("ChatBI agent loop failed: %s", exc)
                # 不做 mock 降级：LLM 调用失败直接报错，避免用规则答案冒充真实回答。
                raise ValueError(
                    f"LLM 调用失败：{self._friendly_llm_error(exc)}"
                ) from exc
            payload = resolver.resolve_payload(payload)
            # Agent 接地判定：只要 Agent 真正命中过本体数据（检索有结果 / 读到对象逻辑 /
            # 概览 / 跑出数据）就视为接地，即便未产出具体引用（如“有哪些对象”这类概览问题）。
            # F4：校验失败（_unverified 非空）优先拒答，不被 referenced_* 兜底覆盖。
            unverified = payload.get("_unverified") or []
            # 拒答时保留已产生的工具轨迹，供用户查看「做了什么才拒答」——
            # 与 ask_stream() 的处置对齐（那边靠 _prev_steps 保留），此前非流式路径
            # 会被拒答 payload 整体覆盖而丢掉 steps，同一次问答两条路径回执不一致。
            _prev_steps = payload.get("steps") or []
            if unverified:
                tel.refuse("unverified")
                agent_telemetry.record(tel)
                refusal = self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                    reasons=unverified,
                )
                refusal["steps"] = _prev_steps
                return refusal
            grounded = bool(
                payload.pop("_grounded", False)
                or payload.get("referenced_objects")
                or payload.get("referenced_logics")
                or payload.get("data_result")
            )
            if not grounded:
                tel.refuse("ungrounded")
                agent_telemetry.record(tel)
                refusal = self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )
                refusal["steps"] = _prev_steps
                return refusal
            agent_telemetry.record(tel)

        if payload.get("suggested_sql"):
            payload["suggested_sql"] = _format_sql(payload["suggested_sql"])

        payload.update(
            {
                "domain_id": domain_id,
                "domain_name": domain.name,
                "ontology_id": ontology.id,
                "used_mock": use_mock,
            }
        )
        return payload

    async def ask_stream(
        self,
        db: Session,
        *,
        domain_id: str,
        question: str,
        history: list[dict] | None = None,
        principal_role: str | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """ask() 的流式版：yield step_start / step_done / token / done。

        done.payload 与 ask() 返回结构一致；透传工具步骤与逐字答案，供 SSE 端点包装。
        Mock / 无本体 / 未接地等无过程可流的情况直接产出终态 done。
        """
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")

        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            yield {
                "type": "done",
                "payload": {
                    "domain_id": domain_id,
                    "domain_name": domain.name,
                    "ontology_id": None,
                    "answer": (
                        f"「{domain.name}」当前还没有已发布的本体。"
                        "请先在「本体建模」中完成草稿编辑并发布，"
                        "Data Agent 会基于已发布本体的对象、字段、关系与业务逻辑进行解读。"
                    ),
                    "suggested_sql": None,
                    "referenced_objects": [],
                    "referenced_logics": [],
                    "used_mock": True,
                },
            }
            return

        snapshots = self._load_ontology_snapshot(db, ontology.id)
        relations = self._load_relations(db, ontology.id)
        logics = self._load_logics(db, ontology.id)
        grounded_objects = self._match_objects(question, snapshots)
        grounded_logics = self._match_logics(question, logics)
        runtime = self.settings_service.get_llm_runtime(db)
        use_mock = not runtime.api_key
        resolver = _ReferenceResolver(objects=snapshots, relations=relations, logics=logics)

        if use_mock:
            # Mock 无过程可流：直接产出终态 done（逻辑与 ask() 的 mock 分支一致）
            if not grounded_objects and not grounded_logics:
                yield {"type": "done", "payload": self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)}
                return
            payload = self._mock_answer(
                question=question, snapshots=snapshots, relations=relations, logics=logics,
                matched_objects=grounded_objects, matched_logics=grounded_logics)
            payload = resolver.resolve_payload(payload)
            payload = self._enforce_grounded_refs(
                payload, grounded_objects=grounded_objects, grounded_logics=grounded_logics, resolver=resolver)
            if not payload.get("referenced_objects") and not payload.get("referenced_logics"):
                yield {"type": "done", "payload": self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)}
                return
            if payload.get("suggested_sql"):
                payload["suggested_sql"] = _format_sql(payload["suggested_sql"])
            payload.update({"domain_id": domain_id, "domain_name": domain.name,
                            "ontology_id": ontology.id, "used_mock": True})
            yield {"type": "done", "payload": payload}
            return

        # Agent 流式路径：透传事件流，done 事件走与 ask() 相同的后处理
        payload: dict | None = None
        tel = RunTelemetry()
        try:
            async for ev in self._stream_agent_events(
                db, runtime=runtime, domain=domain, ontology=ontology,
                question=question, history=history or [],
                seed_objects=grounded_objects, seed_logics=grounded_logics,
                principal_role=principal_role, telemetry=tel,
                conversation_id=conversation_id,
            ):
                if ev["type"] == "done":
                    payload = ev["payload"]
                else:
                    yield ev  # step_start / step_done / token 透传
        except Exception as exc:  # noqa: BLE001
            logger.exception("ChatBI agent stream failed: %s", exc)
            # 不做 mock 降级：LLM 调用失败时只推送明确错误并结束，不用规则答案冒充。
            yield {"type": "error", "message": self._friendly_llm_error(exc)}
            return

        payload = payload or {"answer": "（模型未返回回答）", "steps": []}
        payload = resolver.resolve_payload(payload)
        unverified = payload.get("_unverified") or []
        # 拒答时仍保留 agent 已产生的工具步骤轨迹，供用户查看「做了什么才拒答」，
        # 避免拒答 payload 覆盖前端已流式出来的 steps。
        _prev_steps = payload.get("steps") or []
        if unverified:
            tel.refuse("unverified")
            payload = self._ungrounded_refusal(
                domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id,
                question=question, reasons=unverified)
            payload["steps"] = _prev_steps
        else:
            grounded = bool(
                payload.pop("_grounded", False)
                or payload.get("referenced_objects")
                or payload.get("referenced_logics")
                or payload.get("data_result")
            )
            if not grounded:
                tel.refuse("ungrounded")
                payload = self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)
                payload["steps"] = _prev_steps
        agent_telemetry.record(tel)
        if payload.get("suggested_sql"):
            payload["suggested_sql"] = _format_sql(payload["suggested_sql"])
        payload.update({"domain_id": domain_id, "domain_name": domain.name,
                        "ontology_id": ontology.id, "used_mock": use_mock})
        # 关键：只有在接地校验通过、确定不拒答后，才逐字流式吐出答案，
        # 避免"先流式给出内容、后又被拒答撤回"的观感。
        if not payload.get("grounding_refused"):
            async for ev in self._emit_answer_tokens(payload.get("answer") or ""):
                yield ev
        yield {"type": "done", "payload": payload}

    def suggest_questions(self, db: Session, domain_id: str) -> list[str]:
        """基于已发布本体生成若干示例提问，供前端首屏展示。"""
        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            return [
                "当前数据域有哪些业务对象？",
                "近 7 天的订单量趋势如何？",
                "请帮我梳理支付与退款之间的业务关系。",
            ]
        snapshots = self._load_ontology_snapshot(db, ontology.id)
        logics = self._load_logics(db, ontology.id)

        suggestions: list[str] = []
        if snapshots:
            primary = snapshots[0]
            suggestions.append(f"「{primary.display_name}」包含哪些关键字段？")
            if len(snapshots) > 1:
                other = snapshots[1]
                suggestions.append(
                    f"「{primary.display_name}」和「{other.display_name}」之间有什么关系？"
                )
            if any(p.semantic_type == "amount" or "amount" in p.name for p in primary.properties):
                suggestions.append(f"最近 30 天「{primary.display_name}」的金额合计是多少？")
            else:
                suggestions.append(f"最近 30 天「{primary.display_name}」的记录数有多少？")
        for logic in logics[:2]:
            suggestions.append(f"请解释业务逻辑「{logic.display_name}」的口径。")
        if not suggestions:
            suggestions = ["当前数据域有哪些业务对象？"]
        return suggestions[:5]

    # ---------------------------------------------------------------- conversation

    def list_conversations(
        self,
        db: Session,
        domain_id: str,
        query: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        q = db.query(ChatBiConversation).filter(
            ChatBiConversation.domain_id == domain_id
        )
        if not include_archived:
            q = q.filter(ChatBiConversation.is_archived == False)  # noqa: E712
        if query:
            q = q.filter(ChatBiConversation.title.ilike(f"%{query}%"))
        q = q.order_by(
            desc(ChatBiConversation.is_pinned),
            desc(ChatBiConversation.updated_at),
        )
        conversations = q.all()

        # Bulk-fetch message counts
        conv_ids = [c.id for c in conversations]
        counts: dict[str, int] = {}
        previews: dict[str, str | None] = {}
        if conv_ids:
            count_rows = (
                db.query(
                    ChatBiMessage.conversation_id,
                    func.count(ChatBiMessage.id),
                )
                .filter(ChatBiMessage.conversation_id.in_(conv_ids))
                .group_by(ChatBiMessage.conversation_id)
                .all()
            )
            counts = {row[0]: row[1] for row in count_rows}

            # Last message preview per conversation
            preview_rows = (
                db.query(
                    ChatBiMessage.conversation_id,
                    ChatBiMessage.content,
                )
                .filter(ChatBiMessage.conversation_id.in_(conv_ids))
                .order_by(ChatBiMessage.conversation_id, desc(ChatBiMessage.created_at))
                .all()
            )
            seen: set[str] = set()
            for row in preview_rows:
                if row[0] not in seen:
                    seen.add(row[0])
                    preview = row[1][:100] if row[1] else None
                    previews[row[0]] = preview

        return [
            {
                "id": c.id,
                "domain_id": c.domain_id,
                "title": c.title,
                "category": c.category,
                "is_pinned": c.is_pinned,
                "is_archived": c.is_archived,
                "message_count": counts.get(c.id, 0),
                "last_message_preview": previews.get(c.id),
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in conversations
        ]

    def create_conversation(
        self,
        db: Session,
        domain_id: str,
        title: str | None = None,
        category: str | None = None,
    ) -> dict:
        conv = ChatBiConversation(
            domain_id=domain_id,
            title=title or "新对话",
            category=category,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return {
            "id": conv.id,
            "domain_id": conv.domain_id,
            "title": conv.title,
            "category": conv.category,
            "is_pinned": conv.is_pinned,
            "is_archived": conv.is_archived,
            "message_count": 0,
            "last_message_preview": None,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    def get_conversation(
        self, db: Session, conversation_id: str
    ) -> ChatBiConversation | None:
        return db.get(ChatBiConversation, conversation_id)

    _UNSET = object()

    def update_conversation(
        self,
        db: Session,
        conversation_id: str,
        title: str | None | object = _UNSET,
        category: str | None | object = _UNSET,
        is_pinned: bool | object = _UNSET,
        is_archived: bool | object = _UNSET,
    ) -> dict:
        conv = db.get(ChatBiConversation, conversation_id)
        if not conv:
            raise ValueError("对话不存在")
        if title is not self._UNSET:
            conv.title = title
        if category is not self._UNSET:
            conv.category = category
        if is_pinned is not self._UNSET:
            conv.is_pinned = is_pinned
        if is_archived is not self._UNSET:
            conv.is_archived = is_archived
        log_change(db, "chat_bi_conversation", conversation_id, "rename")
        db.commit()
        db.refresh(conv)
        return {
            "id": conv.id,
            "domain_id": conv.domain_id,
            "title": conv.title,
            "category": conv.category,
            "is_pinned": conv.is_pinned,
            "is_archived": conv.is_archived,
            "message_count": 0,
            "last_message_preview": None,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    def delete_conversation(self, db: Session, conversation_id: str) -> None:
        conv = db.get(ChatBiConversation, conversation_id)
        if not conv:
            raise ValueError("对话不存在")
        log_change(db, "chat_bi_conversation", conversation_id, "delete")
        db.delete(conv)
        db.commit()

    # ---------------------------------------------------------------- categories

    def list_categories(self, db: Session, domain_id: str) -> list[dict]:
        rows = (
            db.query(
                ChatBiConversation.category,
                func.count(ChatBiConversation.id),
            )
            .filter(ChatBiConversation.domain_id == domain_id)
            .group_by(ChatBiConversation.category)
            .all()
        )
        result: list[dict] = []
        for row in rows:
            cat_name = row[0] or "__uncategorized__"
            result.append({"name": cat_name, "conversation_count": row[1]})
        # Sort: uncategorized first, then alphabetically
        result.sort(key=lambda x: ("" if x["name"] == "__uncategorized__" else x["name"]))
        return result

    def rename_category(
        self, db: Session, domain_id: str, old_name: str, new_name: str
    ) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("分类名称不能为空")
        convs = (
            db.query(ChatBiConversation)
            .filter(
                ChatBiConversation.domain_id == domain_id,
                ChatBiConversation.category == old_name,
            )
            .all()
        )
        for conv in convs:
            conv.category = new_name
        db.commit()

    def delete_category(self, db: Session, domain_id: str, name: str) -> None:
        convs = (
            db.query(ChatBiConversation)
            .filter(
                ChatBiConversation.domain_id == domain_id,
                ChatBiConversation.category == name,
            )
            .all()
        )
        for conv in convs:
            conv.category = None
        db.commit()

    def save_message(
        self,
        db: Session,
        conversation_id: str,
        role: str,
        content: str,
        payload: dict | None = None,
    ) -> ChatBiMessage:
        msg = ChatBiMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            payload=json.dumps(payload) if payload else None,
        )
        db.add(msg)
        conv = db.get(ChatBiConversation, conversation_id)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(msg)
        return msg

    def get_messages(
        self, db: Session, conversation_id: str
    ) -> list[dict]:
        rows = (
            db.query(ChatBiMessage)
            .filter(ChatBiMessage.conversation_id == conversation_id)
            .order_by(ChatBiMessage.created_at)
            .all()
        )
        return [
            {
                "id": m.id,
                "conversation_id": m.conversation_id,
                "role": m.role,
                "content": m.content,
                "payload": json.loads(m.payload) if m.payload else None,
                "created_at": m.created_at,
            }
            for m in rows
        ]

    # ------------------------------------------------------------------ data

    def link_conversation_task(
        self,
        db: Session,
        conversation_id: str,
        artifact_id: str,
        *,
        kind: str | None = None,
        intent: str | None = None,
    ) -> dict:
        """P1：记录「本会话催生了某数据任务（治理制品）」。幂等：同一 (会话,制品) 不重复。

        由前端在用户对任务提案点「去校验并执行」建出制品后调用。落这条关联后，该会话再问
        「那个任务好了吗」，get_task_status 无需用户重报 id 即可解析。
        """
        conv = db.get(ChatBiConversation, conversation_id)
        if not conv:
            raise ValueError("对话不存在")
        existing = (
            db.query(ChatBiConversationTask)
            .filter(
                ChatBiConversationTask.conversation_id == conversation_id,
                ChatBiConversationTask.artifact_id == artifact_id,
            )
            .first()
        )
        if existing:
            return {"id": existing.id, "artifact_id": artifact_id, "linked": True}
        row = ChatBiConversationTask(
            conversation_id=conversation_id,
            artifact_id=artifact_id,
            kind=kind,
            intent=intent,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "artifact_id": artifact_id, "linked": True}

    def list_conversation_task_ids(self, db: Session, conversation_id: str) -> list[str]:
        """本会话催生的数据任务 artifact_id 列表（最近在前）。"""
        rows = (
            db.query(ChatBiConversationTask.artifact_id)
            .filter(ChatBiConversationTask.conversation_id == conversation_id)
            .order_by(desc(ChatBiConversationTask.created_at))
            .all()
        )
        return [r[0] for r in rows]

    # ---- P3：跨会话记忆（按域沉淀高频使用，注入系统提示做软召回） ----

    def record_domain_memory(self, db: Session, domain_id: str, payload: dict) -> None:
        """把本次**已接地**回答命中的对象/口径按 (域, 实体) 累加使用度。

        best-effort：拒答/mock 不记；记忆失败绝不影响回答（吞异常并 rollback）。由 API 层在
        存完消息后调用（与 save_message 同类的会话侧持久化，ask() 本身保持只读）。
        """
        if payload.get("grounding_refused") or payload.get("used_mock"):
            return
        refs: list[tuple[str, str, str | None]] = []
        for o in payload.get("referenced_objects") or []:
            rid = o.get("id")
            if rid:
                refs.append(("object_type", rid, o.get("display_name") or o.get("name")))
        for lg in payload.get("referenced_logics") or []:
            rid = lg.get("id")
            if rid:
                refs.append(("business_logic", rid, lg.get("display_name") or lg.get("name")))
        if not refs:
            return
        try:
            now = datetime.now(timezone.utc)
            for ref_kind, ref_id, label in refs:
                row = (
                    db.query(ChatBiDomainMemory)
                    .filter_by(domain_id=domain_id, ref_kind=ref_kind, ref_id=ref_id)
                    .first()
                )
                if row:
                    row.hit_count += 1
                    row.last_used_at = now
                    if label:
                        row.label = label
                else:
                    db.add(ChatBiDomainMemory(
                        domain_id=domain_id, ref_kind=ref_kind, ref_id=ref_id,
                        label=label, hit_count=1, last_used_at=now,
                    ))
            db.commit()
        except Exception as exc:  # noqa: BLE001 — 记忆是增强，失败不该影响主流程
            db.rollback()
            logger.info("record_domain_memory failed: %s", exc)

    def build_domain_memory_card(self, db: Session, domain_id: str, *, limit: int = 8) -> str:
        """本域高频对象/口径 + 显式约定的**软提示**文本。空则返回空串。

        与静态语义卡互补：语义卡按结构重要性，本卡按真实使用度 + 用户立的约定（P3.1）。
        仅作提示、以检索为准——故标注「历史使用」，不作为权威。
        """
        try:
            rows = (
                db.query(ChatBiDomainMemory)
                .filter(ChatBiDomainMemory.domain_id == domain_id)
                .order_by(desc(ChatBiDomainMemory.hit_count), desc(ChatBiDomainMemory.last_used_at))
                .limit(60)
                .all()
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("build_domain_memory_card failed: %s", exc)
            return ""
        objs = [r.label for r in rows if r.ref_kind == "object_type" and r.label][:limit]
        logs = [r.label for r in rows if r.ref_kind == "business_logic" and r.label][:limit]
        prefs = [r.label for r in rows if r.ref_kind == "preference" and r.label][:limit]
        if not objs and not logs and not prefs:
            return ""
        out = ""
        parts: list[str] = []
        if objs:
            parts.append("常用对象：" + "、".join(objs))
        if logs:
            parts.append("常用口径：" + "、".join(logs))
        if parts:
            out += "\n\n【本域高频（历史使用，以检索为准）】" + "；".join(parts) + "。"
        if prefs:
            # 用户立的约定优先级更高：单独一段，作为回答口径/范围时的默认取向。
            out += "\n\n【本域约定（用户已确认，遵循）】" + "；".join(prefs) + "。"
        return out

    def record_domain_preference(self, db: Session, domain_id: str, text: str) -> dict:
        """P3.1：把用户确认的约定落库为本域记忆（ref_kind=preference）。幂等：同域同文不重复。

        由前端在用户对记忆提案点「记住」后调用。写入后经 build_domain_memory_card 注入系统提示。
        """
        text = (text or "").strip()[:255]
        if not text:
            raise ValueError("约定文本为空")
        if not db.get(DomainContext, domain_id):
            raise ValueError("数据域不存在")
        existing = (
            db.query(ChatBiDomainMemory)
            .filter(
                ChatBiDomainMemory.domain_id == domain_id,
                ChatBiDomainMemory.ref_kind == "preference",
                ChatBiDomainMemory.label == text,
            )
            .first()
        )
        if existing:
            return {"id": existing.id, "text": text, "remembered": True}
        row = ChatBiDomainMemory(
            domain_id=domain_id, ref_kind="preference",
            ref_id=str(uuid.uuid4()), label=text, hit_count=1,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "text": text, "remembered": True}

    def _load_ontology_snapshot(
        self, db: Session, ontology_id: str
    ) -> list[_ObjectSnapshot]:
        rows = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .order_by(ObjectType.display_name.asc())
            .all()
        )
        return [
            _ObjectSnapshot(
                id=o.id,
                name=o.name,
                display_name=o.display_name,
                description=o.description,
                properties=sorted(o.properties, key=lambda p: (not p.required, p.name)),
            )
            for o in rows
        ]

    def _load_relations(self, db: Session, ontology_id: str) -> list[RelationType]:
        return (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .order_by(RelationType.display_name.asc())
            .all()
        )

    def _load_logics(self, db: Session, ontology_id: str) -> list[BusinessLogic]:
        return (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .order_by(BusinessLogic.updated_at.desc())
            .all()
        )

    @staticmethod
    def _friendly_llm_error(exc: Exception) -> str:
        """把常见 LLM 失败翻译成可读提示（不降级，直接报错）。"""
        text = str(exc).lower()
        status = getattr(exc, "status_code", None)
        size_signals = (
            "413",
            "request entity too large",
            "context length",
            "maximum context",
            "context_length_exceeded",
            "too many tokens",
            "string too long",
            "payload too large",
        )
        if status in (413, 422) or any(sig in text for sig in size_signals):
            return (
                "LLM 上下文过大：本体知识超出模型/网关的请求上限。"
                "请缩小提问范围，或联系管理员放宽端点的 body/上下文限制。"
            )
        # 模型/通道不可用（如网关报 model_not_found / no available channel）
        if (
            status in (401, 403, 404)
            or "model_not_found" in text
            or "no available channel" in text
            or "invalid token" in text
            or "unauthorized" in text
        ):
            return (
                "LLM 服务不可用：当前模型或密钥/通道无效（网关返回不可用）。"
                "请到「设置 → 模型服务」检查并启用一个可用的 LLM 配置后重试。"
            )
        return f"LLM 调用失败：{str(exc)[:200]}"

    # -------------------------------------------------------------- agent loop

    def _resolve_domain_data_source(self, db: Session):
        """为 run_sql 选一个可执行数据源。

        DataSource 目前无数据域绑定（schema 层缺 domain 外键），故策略：
        唯一可用源直接用；多个可用源取最近更新的一个（P1 实用取舍）；无可用源返回 None
        （run_sql 据此优雅降级为「仅建议 SQL」）。
        """
        from app.models import DataSource

        usable = [
            s
            for s in db.query(DataSource).all()
            if (s.dsn_secret_ref or "").strip() and s.kind != "mock"
        ]
        if not usable:
            return None
        if len(usable) > 1:
            usable.sort(key=lambda s: (s.updated_at or s.created_at), reverse=True)
        return usable[0]

    # --- 工具结果 compact 化：只保留 LLM 需要的字段，压住回灌体积 ---

    @staticmethod
    def _compact_object_summary(o: Any) -> dict:
        d = o.model_dump(mode="json") if hasattr(o, "model_dump") else dict(o)
        return {
            k: d.get(k)
            for k in ("id", "name", "display_name", "description", "property_count", "table_role")
        }

    @staticmethod
    def _compact_relation(r: Any) -> dict:
        d = r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r)
        return {
            k: d.get(k)
            for k in (
                "id", "name", "display_name", "source_object_name",
                "target_object_name", "cardinality", "description",
            )
        }

    def _compact_object_detail(self, detail: Any) -> dict:
        d = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else dict(detail)
        props = [
            {
                k: p.get(k)
                for k in ("name", "display_name", "data_type", "semantic_type", "required", "description")
            }
            for p in (d.get("properties") or [])
        ]
        rels = [
            self._compact_relation(r)
            for r in (d.get("outgoing_relations") or []) + (d.get("incoming_relations") or [])
        ]
        logics = [
            {"id": l.get("id"), "display_name": l.get("display_name"), "expression_summary": l.get("expression_summary")}
            for l in (d.get("business_logics") or [])
        ]
        return {
            "id": d.get("id"),
            "name": d.get("name"),
            "display_name": d.get("display_name"),
            "description": d.get("description"),
            "properties": props,
            "relations": rels,
            "business_logics": logics,
        }

    @staticmethod
    def _compact_logic_summary(l: Any) -> dict:
        d = l.model_dump(mode="json") if hasattr(l, "model_dump") else dict(l)
        return {
            k: d.get(k)
            for k in ("id", "name", "display_name", "logic_type", "expression_summary", "description")
        }

    def _compact_logic_detail(self, detail: Any) -> dict:
        d = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else dict(detail)
        base = self._compact_logic_summary(detail)
        base["expression_draft"] = d.get("expression_draft")
        base["related_objects"] = [
            {"id": o.get("id"), "display_name": o.get("display_name")}
            for o in (d.get("related_object_types") or [])
        ]
        base["related_properties"] = [
            {"display_name": p.get("display_name"), "name": p.get("name")}
            for p in (d.get("related_properties") or [])
        ]
        return base

    @staticmethod
    def _may_run_sql(principal_role: str | None) -> bool:
        """当前主体是否够格让 Agent 代跑 SQL（P1.1）。

        权限必须约束到**工具粒度**：``/chat-bi/ask`` 端点兜底只要 editor，但它内部的
        run_sql 直打真实 DSN，而手动执行端点要 publisher——不在这里卡一道，工具化就把
        权限模型绕过去了。fail-closed：拿不到角色一律视为不够格。
        """
        from app.models.principal import role_satisfies

        return role_satisfies(principal_role, env_settings.agent_run_sql_min_role)

    def _dispatch_run_sql(
        self,
        db: Session,
        *,
        args: dict,
        ontology_id: str | None = None,
        principal_role: str | None = None,
    ) -> tuple[Any, str, bool]:
        from app.services import data_app_executor

        sql = str(args.get("sql") or "").strip()
        if not sql:
            return {"error": "缺少 sql"}, "run_sql 缺少 sql", True
        try:
            limit = int(args.get("limit") or _RUN_SQL_LIMIT)
        except (TypeError, ValueError):
            limit = _RUN_SQL_LIMIT
        limit = max(1, min(limit, _RUN_SQL_LIMIT))

        ok, reason = data_app_executor.is_read_only(sql)
        if not ok:
            return {"executed": False, "error": f"仅允许只读 SELECT：{reason}", "sql": sql}, "被只读校验拒绝", True

        # 权限不足时不解析数据源——降级为「仅建议 SQL」，而不是硬报错：
        # 检索类工具对低权角色仍合法，问答体验不掉，只是不代跑数。
        may_run = self._may_run_sql(principal_role)
        source = self._resolve_domain_data_source(db) if may_run else None
        mapping = _loads_payload(source.mapping_json) if source is not None else None

        # ★ SQL 语义证明（F3）：执行/建议前静态证明语义合法，不过则不放行。
        #   即便无可执行数据源（仅建议 SQL），也要证明——臆造字段/JOIN 与是否落库无关。
        rejection, proved = self._prove_sql_or_reject(db, sql, ontology_id, source, mapping)
        if rejection is not None:
            return rejection

        if not may_run:
            return (
                {
                    "executed": False,
                    "reason": (
                        f"当前角色无权让 Agent 执行 SQL（需 "
                        f"{env_settings.agent_run_sql_min_role} 及以上），仅能给出建议 SQL"
                    ),
                    "sql": sql,
                    "proved": proved,
                },
                "权限不足，仅建议 SQL",
                False,
            )
        if source is None:
            return (
                {"executed": False, "reason": "当前数据域未绑定可执行数据源，仅能给出建议 SQL",
                 "sql": sql, "proved": proved},
                "无可执行数据源",
                False,
            )
        try:
            columns, rows = data_app_executor.execute_sql(
                dsn=source.dsn_secret_ref,
                sql=sql,
                limit=limit,
                mapping=mapping or None,
                timeout_seconds=_SQL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            return {"executed": False, "error": str(exc)[:300], "sql": sql}, "SQL 执行失败", True
        return (
            {
                "executed": True,
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= limit,
                "proved": proved,
            },
            f"返回 {len(rows)} 行",
            False,
        )

    def _prove_sql_or_reject(
        self, db: Session, sql: str, ontology_id: str | None, source, mapping
    ) -> tuple[tuple[Any, str, bool] | None, dict]:
        """SQL 语义证明门（F3）。返回 (拒绝三元组或 None, 证书摘要)。

        受 ``settings.agent_soundness`` 开关：off=跳过；warn=只记日志不拦；on=拒绝执行。
        证明本身出错（解析器异常等）绝不误伤正常查询——降级为放行并记日志。

        **证书要带回去**：``SqlCertificate.tables/columns`` 是「这些表/列确实是已发布
        本体成员」的**证明结论**，不是模型的主张。不回传的话，模型写了一条被证明合法的
        SQL、再在正文里解释它引用的字段，会因账本里没有该字段而被 F4 判成幻觉——
        自己证过的东西反过来拒自己。
        """
        from app.config import settings as env_settings
        from app.services import data_app_executor

        mode = (getattr(env_settings, "agent_soundness", "on") or "on").lower()
        if mode == "off" or not ontology_id:
            return None, {}
        try:
            proj = build_projection(db, ontology_id, mapping)
            dialect = (
                data_app_executor.backend_of(source.dsn_secret_ref)
                if source is not None
                else None
            )
            verdict = prove_sql_sound(sql, proj, dialect=dialect)
        except Exception as exc:  # noqa: BLE001 — 证明器故障不得拖垮问答
            logger.warning("sql soundness prover error, allowing SQL: %s", exc)
            return None, {}
        if not isinstance(verdict, SqlRejection):
            return None, {
                "tables": list(verdict.tables),
                "columns": list(verdict.columns),
            }
        if mode == "warn":
            logger.info("[soundness=warn] 本应拒答 SQL：%s | %s", verdict.code, verdict.message)
            return None, {}
        # mode == "on"：拒绝执行。is_error=True → 不计入接地，避免用拒绝当命中。
        # hint（P1.4）必须随结果回灌：这条 dict 会 json 化进 role:tool，
        # 模型据此在下一步自修（补对候选字段、换合法对端、改安全聚合）。
        return (
            {"executed": False, "rejected": True, "sql": sql,
             "reason": verdict.message, "code": verdict.code,
             "hint": verdict.hint or {}},
            f"SQL 语义证明未通过：{verdict.code}",
            True,
        ), {}

    # -------------------------------------------------------------- F4 断言级可靠性

    @staticmethod
    def _ledger_register(
        ledger: FactLedger, tool_name: str, result: Any, is_error: bool
    ) -> None:
        """把一次工具调用的**真实返回**登记进事实账本。失败/错误返回不入账。"""
        if is_error:
            return
        try:
            if tool_name == "search_objects":
                for o in _search_items(result):
                    ledger.add_object_summary(o)
            elif tool_name == "get_object" and isinstance(result, dict):
                ledger.add_object_detail(result)
            elif tool_name == "search_relations":
                for r in _search_items(result):
                    ledger.add_relation(r)
            elif tool_name == "search_logics":
                for l in _search_items(result):
                    ledger.add_metric_summary(l)
            elif tool_name == "get_logic" and isinstance(result, dict):
                ledger.add_metric_summary(result)
            elif tool_name == "get_domain_overview" and isinstance(result, dict):
                for key in ("objects", "most_connected_objects"):
                    for o in result.get(key) or []:
                        if isinstance(o, dict):
                            ledger.add_object_summary(o)
                for r in result.get("relations") or []:
                    if isinstance(r, dict):
                        ledger.add_relation(r)
            elif tool_name == "find_join_path" and isinstance(result, dict):
                # 路径里的关系名与两端对象名都是工具真实返回的事实，
                # 答案引用「订单归属客户」这类关系名时不得被判幻觉。
                for p in result.get("paths") or []:
                    for hop in (p or {}).get("hops") or []:
                        ledger.add_relation({
                            "id": hop.get("relation"),
                            "name": hop.get("relation"),
                            "display_name": hop.get("relation_display"),
                            "source_object_name": hop.get("from"),
                            "target_object_name": hop.get("to"),
                        })
            elif tool_name == "compile_metric" and isinstance(result, dict):
                # 编译成功即证明了：口径名、涉及的对象、证书里的表/列都是本体成员。
                # 不入账的话，模型复述自己刚编出来的口径会被 F4 判成幻觉。
                labels = result.get("object_labels") or {}
                names = (
                    [result.get("logic")]
                    + list(result.get("objects") or [])
                    + list(labels.values())
                )
                cert = result.get("certificate") or {}
                names += list(cert.get("tables") or [])
                for col in cert.get("columns") or []:
                    names += [col, str(col).rsplit(".", 1)[-1]]
                for hop in result.get("join_hops") or []:
                    names += [hop.get("relation"), hop.get("relation_display")]
                ledger.add_context_name(*[str(n) for n in names if n])
            elif tool_name == "profile_values" and isinstance(result, dict):
                # 画像返回的是**库里真实存在的取值**，与 run_sql 的数据行同级可信：
                # 答案引用「状态取值有 Completed/Draft」时不得被判幻觉。
                ledger.add_cells(
                    [], [{"v": tv.get("value")} for tv in result.get("top_values") or []]
                )
                ledger.add_context_name(
                    *[str(tv.get("value")) for tv in result.get("top_values") or []
                      if tv.get("value") is not None]
                )
                for key in ("min_value", "max_value", "avg_value", "row_count", "distinct_count"):
                    if result.get(key) is not None:
                        ledger.add_cells([], [{"v": result[key]}])
            elif tool_name == "run_sql" and isinstance(result, dict):
                # SQL 语义证书里的表/列是**证明结论**（它们确实是已发布本体成员），
                # 与 get_object 返回的字段同等可信，故入账。否则模型解释自己那条
                # 已被证明合法的 SQL 时，会因账本没这些名字而被判幻觉。
                proved = result.get("proved") or {}
                names: list[str] = list(proved.get("tables") or [])
                for col in proved.get("columns") or []:
                    names.append(col)                       # "order.amount"
                    names.append(str(col).rsplit(".", 1)[-1])  # "amount"
                if names:
                    ledger.add_context_name(*names)
                if result.get("executed"):
                    ledger.add_cells(result.get("columns") or [], result.get("rows") or [])
            elif tool_name == "get_lineage" and isinstance(result, dict):
                # 血缘邻域里的对象名与关系名都是本体真实成员——答案解读「上游是客户、
                # 下游影响订单」时引用这些名字不得被 F4 判成幻觉。
                for n in result.get("nodes") or []:
                    if isinstance(n, dict):
                        ledger.add_context_name(
                            *[str(v) for v in (n.get("display_name"), n.get("label")) if v]
                        )
                for e in result.get("edges") or []:
                    if isinstance(e, dict) and e.get("label"):
                        ledger.add_context_name(str(e["label"]))
            elif tool_name == "propose_draft" and isinstance(result, dict):
                # 提案里的口径名是本轮工具产出的事实——答案复述「建议新建指标X」时
                # 引用 X 不得被 F4 判成幻觉（它是提案而非对已有实体的断言）。
                ledger.add_context_name(
                    *[str(v) for v in (result.get("display_name"), result.get("name")) if v]
                )
            elif tool_name == "propose_preference" and isinstance(result, dict):
                # 记忆提案的约定文本是本轮工具产出——答案复述该约定时不被 F4 判成幻觉。
                if result.get("text"):
                    ledger.add_context_name(str(result["text"]))
            elif tool_name == "analyze_result" and isinstance(result, dict):
                # P5：把统计画像的真实计算值（均值/分位/离群…）登记为可信事实，
                # 让答案复述「均值 X、离群 Y 个」时不被 F4 判成幻觉。
                a = result.get("analysis") or {}
                cells: list[dict] = []
                for rep in a.get("columns") or []:
                    if isinstance(rep, dict):
                        if rep.get("column"):
                            ledger.add_context_name(str(rep["column"]))
                        for k in ("count", "nulls", "min", "max", "mean", "p25",
                                  "median", "p75", "std", "outlier_count"):
                            if isinstance(rep.get(k), (int, float)) and not isinstance(rep.get(k), bool):
                                cells.append({"v": rep[k]})
                        for ov in rep.get("outliers") or []:
                            if isinstance(ov, (int, float)) and not isinstance(ov, bool):
                                cells.append({"v": ov})
                        tr = rep.get("trend") or {}
                        for k in ("first", "last", "change", "change_pct", "slope"):
                            if isinstance(tr.get(k), (int, float)) and not isinstance(tr.get(k), bool):
                                cells.append({"v": tr[k]})
                        for jp in rep.get("jumps") or []:
                            for k in ("from", "to", "delta"):
                                if isinstance(jp.get(k), (int, float)) and not isinstance(jp.get(k), bool):
                                    cells.append({"v": jp[k]})
                            if jp.get("at") is not None:
                                ledger.add_context_name(str(jp["at"]))
                if isinstance(a.get("total_outliers"), int):
                    cells.append({"v": a["total_outliers"]})
                if isinstance(a.get("total_jumps"), int):
                    cells.append({"v": a["total_jumps"]})
                if cells:
                    ledger.add_cells([], cells)
            elif isinstance(result, dict) and "data" in result:
                # P4：外部工具返回（{"data": ...}）是本轮工具的真实产出——把其中的字符串/数值
                # 登记为可信事实，让答案复述外部结果时不被 F4 判成幻觉。
                def _reg(v: Any, depth: int = 0) -> None:
                    if depth > 3:
                        return
                    if isinstance(v, dict):
                        for x in v.values():
                            _reg(x, depth + 1)
                    elif isinstance(v, list):
                        for x in v[:50]:
                            _reg(x, depth + 1)
                    elif isinstance(v, str) and v.strip():
                        ledger.add_context_name(v[:120])
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        ledger.add_cells([], [{"v": v}])
                _reg(result.get("data"))
        except Exception as exc:  # noqa: BLE001 — 登记失败不得拖垮问答
            logger.warning("fact ledger register failed for %s: %s", tool_name, exc)

    @staticmethod
    def _asks_number(question: str) -> bool:
        """问题是否在问具体数值/计量（决定数值断言是否要求 run_sql 凭证）。"""
        return any(k in (question or "") for k in _AGG_KEYWORDS)

    @staticmethod
    def _repair_instruction(unverified: list[str], ledger: FactLedger) -> str:
        """自愈指令：既指出**哪几句没凭证**，也给出**能说什么**。

        只说「你错了」模型多半会换个说法再错一次；把可证事实清单一并给它，
        它才知道边界在哪。这与 P1.4 给拒绝加修复信号是同一条思路——
        守卫要当教练，不能只当守门员。
        """
        provable = ledger.provable_names()
        lines = [
            "你上一条回答里有内容无法由本次检索到的事实证实，请**重写**这条回答。",
            "",
            "无法证实的部分：",
            *[f"  · {u}" for u in unverified[:8]],
            "",
        ]
        if provable:
            lines += [
                "本轮**已检索到、可以引用**的实体（只能用这些）：",
                "  " + "、".join(provable),
                "",
            ]
        lines += [
            "重写要求：删掉或改写无法证实的部分；不要引入任何新的名称或数字；",
            "确实无法回答的部分如实说明「当前本体中查不到」。只输出重写后的回答正文。",
        ]
        return "\n".join(lines)

    def _verify_answer(
        self, answer: str, ledger: FactLedger, question: str
    ) -> tuple[bool, list[str]]:
        """F4 校验入口。受 settings.agent_soundness 开关；off/warn 不拦，on 生效。

        返回 (ok, unverified)：ok=False 且 on 模式时，上层将拒答并展示 unverified。
        """
        from app.config import settings as env_settings

        mode = (getattr(env_settings, "agent_soundness", "on") or "on").lower()
        if mode == "off":
            return True, []
        # 数值严格模式：只要本轮真跑过数（有 cells）或问题在问量，就要求数值有凭证。
        strict = bool(ledger.cells) or self._asks_number(question)
        verdict = verify_answer(answer, ledger, strict_numbers=strict)
        if verdict.ok:
            return True, []
        if mode == "warn":
            logger.info("[soundness=warn] 答案含不可证断言：%s", "; ".join(verdict.unverified))
            return True, []  # warn 不拦
        return False, verdict.unverified

    @staticmethod
    def _search_envelope(
        items: list[dict], total: int, noun: str, facet_key: str | None = None
    ) -> tuple[Any, str, bool]:
        """检索类工具的统一返回。

        「样本 ≠ 全集」由**结构**保证，不靠 prompt 求模型自觉（P2.3）：
        - 未截断 → 键名是 ``items``（这就是全部命中）；
        - 截断   → 键名换成 ``sample``，并给 ``facets`` 描述剩下那些的构成。

        键名本身就在说明它是什么。原先无论如何都叫 ``items``，于是
        140 条里挑出的 8 条会被当作完整清单写进答案，只能靠一大段铁律去堵。
        """
        truncated = total > len(items)
        payload: dict[str, Any] = {"total_matched": total, "returned": len(items)}
        if not truncated:
            payload["items"] = items
            return payload, f"命中 {total} 个{noun}", False

        payload["truncated"] = True
        payload["sample"] = items
        payload["sample_note"] = (
            f"这是 {total} 个{noun}中的前 {len(items)} 个**样本**，不是完整清单；"
            "作答时须说明是示例并给出总数，不得由样本推断全集。"
        )
        if facet_key:
            facets = Counter(
                str(it.get(facet_key) or "unknown") for it in items if isinstance(it, dict)
            )
            if len(facets) > 1:
                payload["sample_facets"] = {facet_key: dict(facets)}
        return payload, f"命中 {total} 个{noun}，返回样本 {len(items)} 个", False

    def _dispatch_domain_overview(self, db: Session, *, ontology_id: str) -> tuple[Any, str, bool]:
        """已发布本体概览：总数 + 连通性统计 + **明确标注截断**的样本清单。

        只统计/列举【已发布】对象与关系；grouped_graph 混入未发布草稿，不能用于概览。
        连通性统计（有/无关系的对象数、关系最多的对象）是 “哪些本体/对象有关系”
        这类问题的**直接答案**，避免模型靠几条抽样去反推而说出
        “4113 条关系覆盖了 734 个对象” 这种错话。
        """
        qs = self.query_service
        obj_page = qs.list_object_types(
            db, ontology_id=ontology_id, published_only=True, limit=_OVERVIEW_LIST_LIMIT
        )
        rel_page = qs.list_relation_types(
            db, ontology_id=ontology_id, published_only=True, limit=_OVERVIEW_LIST_LIMIT
        )
        conn = self._relation_connectivity(db, ontology_id=ontology_id)
        # 必须带 id：FactLedger.add_object_summary 无 id 即丢弃，
        # 否则概览列出的对象名进不了账本，答案一引用就被判幻觉→误拒答。
        objects = [
            {"id": o.id, "display_name": o.display_name, "name": o.name, "table_role": o.table_role}
            for o in obj_page.items
        ]
        # 列出已发布关系（名称+两端），供概览/列举类问题接地：
        # 否则答案一提关系就因账本无凭证而被判不可证→误拒答。
        relations = [self._compact_relation(r) for r in rel_page.items]
        overview = {
            "published_object_count": obj_page.total,
            "published_relation_count": rel_page.total,
            "objects_with_relations": conn["with_relations"],
            "objects_without_relations": conn["without_relations"],
            "most_connected_objects": conn["top"],
            "objects_listed": len(objects),
            "objects_truncated": obj_page.total > len(objects),
            "objects": objects,
            "relations_listed": len(relations),
            "relations_truncated": rel_page.total > len(relations),
            "relations": relations,
            "note": (
                "仅统计并列举【已发布(published)】的业务对象与关系；未发布的建模草稿不计入。"
                f"objects/relations 两个清单最多各返回 {_OVERVIEW_LIST_LIMIT} 条：truncated=true 时"
                "它们只是样本，**不是全集**，作答时必须说明是示例并以 count 字段为准的总数为准，"
                "不得把样本当作完整清单，也不得由样本推断全集的性质。"
                "“哪些对象有关系/多少对象有关系”请直接用 objects_with_relations / "
                "objects_without_relations / most_connected_objects，不要自行推算。"
                "列举 most_connected_objects 时请用 display_label 作为对象名"
                "（重名对象已在其中带标识符消歧），不要改写它。"
            ),
        }
        summary = (
            f"{obj_page.total} 个已发布对象 / {rel_page.total} 个已发布关系"
            f"（{conn['with_relations']} 个对象有关系、{conn['without_relations']} 个无关系）"
        )
        return overview, summary, False

    @staticmethod
    def _relation_connectivity(db: Session, *, ontology_id: str) -> dict:
        """已发布关系的连通性：有/无关系的对象数，以及关系数最多的对象 Top N。

        度数按「作为源端或目标端出现的关系条数」计（同一关系对两端各计一次）。
        """
        obj_q = db.query(ObjectType.id, ObjectType.name, ObjectType.display_name).filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.status == EntityStatus.PUBLISHED.value,
        )
        objects = {oid: (nm, dn) for oid, nm, dn in obj_q.all()}

        rel_q = db.query(
            RelationType.source_object_type_id, RelationType.target_object_type_id
        ).filter(
            RelationType.ontology_id == ontology_id,
            RelationType.status == EntityStatus.PUBLISHED.value,
        )
        degree: dict[str, int] = {}
        for src, tgt in rel_q.all():
            for oid in (src, tgt):
                if oid in objects:
                    degree[oid] = degree.get(oid, 0) + 1

        top = [
            {
                "id": oid,
                "display_name": objects[oid][1],
                "name": objects[oid][0],
                "relation_count": cnt,
            }
            for oid, cnt in sorted(degree.items(), key=lambda kv: -kv[1])[:_OVERVIEW_TOP_CONNECTED]
        ]
        # 同一榜单里重名的对象（如 item 与 project 都叫「项目」）若只给显示名，
        # 读起来像同一个对象被列了两次；重名时补上标识符消歧。
        dup = {
            d for d in (t["display_name"] for t in top)
            if sum(1 for t in top if t["display_name"] == d) > 1
        }
        for t in top:
            t["display_label"] = (
                f"{t['display_name']}（{t['name']}）" if t["display_name"] in dup else t["display_name"]
            )
        return {
            "with_relations": len(degree),
            "without_relations": len(objects) - len(degree),
            "top": top,
        }

    def _dispatch_join_path(
        self, db: Session, *, ontology_id: str, args: dict
    ) -> tuple[Any, str, bool]:
        """P1.2：两对象之间的关联路径。语义层从「事后否决」转为「事前给答案」。

        找不到路径**不是错误**——「本体中这两个对象无从关联」本身就是一条可作答的
        事实（is_error=False），模型据此如实说明，而不是继续臆造 JOIN。
        """
        from app.services.semantic_navigator import describe_paths, find_join_path

        src = str(args.get("from_object") or "").strip()
        tgt = str(args.get("to_object") or "").strip()
        if not src or not tgt:
            return {"error": "需要 from_object 与 to_object"}, "缺少对象参数", True

        proj = build_projection(db, ontology_id, None)
        for token, label in ((src, "from_object"), (tgt, "to_object")):
            if proj.object_of(token) is None:
                return (
                    {"error": f"{label}「{token}」不是已发布业务对象"},
                    f"未知对象：{token}",
                    True,
                )

        paths = find_join_path(
            proj, src, tgt, measure_object=str(args.get("measure_object") or "") or None
        )
        result = {
            "from": src,
            "to": tgt,
            "found": len(paths),
            "paths": describe_paths(paths),
        }
        if not paths:
            result["note"] = (
                "本体中这两个对象之间没有可用的关联路径；不得自行构造 JOIN，"
                "请如实说明无法关联，或换用其它对象。"
            )
            return result, f"「{src}」与「{tgt}」无可用关联路径", False
        best = paths[0]
        summary = f"找到 {len(paths)} 条路径，最短 {best.hop_count} 跳"
        if best.fanout_risk:
            summary += "（有扇出风险）"
        return result, summary, False

    def _dispatch_compile_metric(self, db: Session, *, args: dict) -> tuple[Any, str, bool]:
        """P3：口径编译。模型从「照着口径文本重写 SQL」变成「选口径 + 选维度」。

        编译失败一律带 code + hint 回灌（同 P1.4 取向）：口径没形式化、维度不可关联、
        会扇出——每种都有明确下一步，比一句「编译失败」有用得多。
        """
        from app.services import data_app_executor
        from app.services.metric_compiler import MetricCompileError, compile_metric

        logic_id = str(args.get("logic_id") or "").strip()
        if not logic_id:
            return {"error": "需要 logic_id"}, "缺少 logic_id", True

        source = self._resolve_domain_data_source(db)
        dims = [str(d) for d in (args.get("dimensions") or []) if str(d).strip()]
        filters = [f for f in (args.get("filters") or []) if isinstance(f, dict)]
        try:
            compiled = compile_metric(
                db,
                logic_id,
                dimensions=dims,
                filters=filters,
                grain=str(args.get("grain") or "") or None,
                time_property=str(args.get("time_property") or "") or None,
                dialect=data_app_executor.backend_of(
                    source.dsn_secret_ref if source is not None else None
                ),
                mapping=_loads_payload(source.mapping_json) if source is not None else None,
            )
        except MetricCompileError as exc:
            return (
                {"compiled": False, "logic_id": logic_id, "code": exc.code,
                 "reason": exc.message, "hint": exc.hint},
                f"口径编译失败：{exc.code}",
                True,
            )
        result = compiled.to_dict()
        result["compiled"] = True
        dim_note = f"，{len(compiled.dimensions)} 个维度" if compiled.dimensions else ""
        return result, f"已编译口径「{compiled.logic_display_name}」{dim_note}", False

    def _dispatch_profile_values(
        self, db: Session, *, ontology_id: str, args: dict, principal_role: str | None
    ) -> tuple[Any, str, bool]:
        """P1.3：字段取值画像。堵住「谓词字面量猜错 → 返回 0 行 → 静默错答」。

        画像要读真实数据，与 run_sql 是同一类数据暴露，故用**同一道权限闸门**；
        权限不足或无数据源时优雅降级（available=False + 说明），不报错。
        """
        from app.services import data_app_executor
        from app.services.column_profiler import profile_property

        obj_token = str(args.get("object_id") or "").strip()
        prop_token = str(args.get("property") or "").strip()
        if not obj_token or not prop_token:
            return {"error": "需要 object_id 与 property"}, "缺少参数", True

        proj = build_projection(db, ontology_id, None)
        obj = proj.object_of(obj_token)
        if obj is None:
            # 模型常把 id 当 name 传（反之亦然），两种都认
            detail = self.query_service.get_object_type(db, obj_token)
            obj = proj.object_of(detail.name) if detail else None
        if obj is None:
            return {"error": f"对象「{obj_token}」不存在或未发布"}, "对象未命中", True
        prop = obj.resolve_property(prop_token)
        if prop is None:
            return (
                {
                    "error": f"字段「{prop_token}」不属于对象「{obj.display_name}」",
                    "available_columns": sorted(p.name for p in obj.props.values())[:20],
                },
                "字段未命中",
                True,
            )

        may_run = self._may_run_sql(principal_role)
        source = self._resolve_domain_data_source(db) if may_run else None
        if not may_run:
            return (
                {
                    "object": obj.name, "property": prop.name, "available": False,
                    "note": (
                        f"当前角色无权读取真实取值（需 "
                        f"{env_settings.agent_run_sql_min_role} 及以上）。"
                        "不得据此猜测字面量。"
                    ),
                },
                "权限不足，未画像",
                False,
            )

        mapping = _loads_payload(source.mapping_json) if source is not None else None
        dsn = source.dsn_secret_ref if source is not None else None
        profile = profile_property(
            proj, obj, prop,
            dsn=dsn,
            mapping=mapping,
            backend=data_app_executor.backend_of(dsn),
            scope_key=f"{ontology_id}|{getattr(source, 'id', '')}",
        )
        result = profile.to_dict()
        if not profile.available:
            return result, f"「{obj.display_name}.{prop.name}」未能画像", False
        if profile.top_values:
            summary = (
                f"「{prop.name}」{profile.distinct_count or len(profile.top_values)} 个取值，"
                f"返回前 {len(profile.top_values)} 个"
            )
        else:
            summary = f"「{prop.name}」区间 {profile.min_value} ~ {profile.max_value}"
        return result, summary, False

    async def _dispatch_locate_entities(
        self,
        db: Session,
        *,
        client,
        model: str,
        domain_id: str,
        ontology_id: str,
        args: dict,
        telemetry: RunTelemetry,
    ) -> tuple[Any, str, bool]:
        """P4.2：把检索外包给子 agent，只把结论带回主上下文。"""
        from app.services.retrieval_agent import locate_entities

        intent = str(args.get("intent") or "").strip()
        if not intent:
            return {"error": "需要 intent"}, "缺少 intent", True
        try:
            res = await locate_entities(
                db,
                client=client,
                model=model,
                intent=intent,
                domain_id=domain_id,
                ontology_id=ontology_id,
                dispatch=self._dispatch_agent_tool,
                tool_schemas=_AGENT_TOOL_SCHEMAS,
                to_thread=asyncio.to_thread,
            )
        except Exception as exc:  # noqa: BLE001 — 子 agent 故障不得拖垮主问答
            logger.warning("retrieval sub-agent failed: %s", exc)
            return (
                {"error": f"检索助手未能完成定位：{str(exc)[:200]}",
                 "fix": "请直接使用 search_objects / search_logics 检索。"},
                "检索助手失败",
                True,
            )

        telemetry.subagent(
            llm_calls=res.llm_calls, steps=res.steps, isolated_chars=res.isolated_chars
        )
        hit = len(res.objects) + len(res.logics)
        return (
            res.to_dict(),
            f"检索助手定位到 {hit} 个实体（{res.steps} 步在隔离上下文中完成）",
            False,
        )

    # ------------------------------------------------------------ P1.5 混合检索

    def _load_object_summaries(self, db: Session, ids: list[str]) -> list[dict]:
        objs = db.query(ObjectType).filter(ObjectType.id.in_(ids)).all()
        by_id = {o.id: o for o in objs}
        return [
            {
                "id": o.id, "name": o.name, "display_name": o.display_name,
                "description": o.description, "table_role": o.table_role,
            }
            for oid in ids
            if (o := by_id.get(oid)) is not None
        ]

    def _load_logic_summaries(self, db: Session, ids: list[str]) -> list[dict]:
        rows = db.query(BusinessLogic).filter(BusinessLogic.id.in_(ids)).all()
        by_id = {l.id: l for l in rows}
        return [
            {
                "id": l.id, "name": l.name, "display_name": l.display_name,
                "logic_type": l.logic_type, "expression_summary": l.expression_summary,
                "description": l.description,
            }
            for lid in ids
            if (l := by_id.get(lid)) is not None
        ]

    def _augment_semantic(
        self,
        db: Session,
        *,
        ontology_id: str,
        keyword: str | None,
        kind: str,
        items: list[dict],
        loader,
    ) -> tuple[list[dict], int]:
        """在 ILIKE 结果后补上向量召回（P1.5）。返回 (合并后条目, 新增条数)。

        字面命中**始终排前**——用户打出的词是最强意图信号，不该被一个分数更高的
        近义实体挤掉。向量只负责补 ILIKE 够不到的同义表达（往来单位 → 客户）。
        未配置嵌入服务/索引未建时返回原样，功能不受影响。
        """
        if not keyword or len(items) >= _SEARCH_LIMIT:
            return items, 0
        try:
            from app.services import semantic_search

            ontology = db.get(Ontology, ontology_id)
            if ontology is None:
                return items, 0
            hits = semantic_search.search(
                db, ontology, keyword, kind=kind, limit=_SEARCH_LIMIT
            )
            if not hits:
                return items, 0
            have = {i.get("id") for i in items}
            new_ids = [h.entity_id for h in hits if h.entity_id not in have][
                : _SEARCH_LIMIT - len(items)
            ]
            if not new_ids:
                return items, 0
            extra_items = loader(db, new_ids)
            for it in extra_items:
                # 标注来源：字面没命中、是语义近似召回的，模型据此措辞更准
                it["matched_by"] = "semantic"
            return items + extra_items, len(extra_items)
        except Exception as exc:  # noqa: BLE001 — 语义检索是增强，坏了不能拖垮检索
            logger.info("semantic augmentation skipped: %s", exc)
            return items, 0

    def _apply_select_skill(
        self, args: dict, messages: list[dict], base_system: str, governance_card: str = ""
    ) -> tuple["Skill | None", dict, str, bool]:
        """V3 S1：切换技能。就地把 messages[0] 重建为 base_system + 技能 overlay。

        返回 (激活的技能, 工具结果, 摘要, 是否错误)。未知技能名→不切换、回错误提示。
        重复选取以最后一次为准（overlay 不叠加，避免多次切换污染 system）。
        技能标了 attach_governance 时，把当前生效规约的约束卡并入 overlay（事前遵循）。
        """
        name = str(args.get("skill") or "").strip()
        skill = SKILLS.get(name)
        if skill is None:
            return (
                None,
                {"error": f"未知技能「{name}」", "available": list(SKILLS.keys())},
                f"未知技能「{name}」",
                True,
            )
        overlay = skill.prompt_overlay
        if skill.attach_governance and governance_card:
            overlay = f"{overlay}\n\n{governance_card}"
        messages[0]["content"] = f"{base_system}\n\n{overlay}"
        result = {
            "ok": True,
            "skill": skill.name,
            "display": skill.display,
            "tools_unlocked": list(skill.extra_tool_names),
        }
        return skill, result, f"已切换到「{skill.display}」技能", False

    def _dispatch_render_chart(
        self, args: dict, data_result: dict | None, charts: list[dict]
    ) -> tuple[dict, str, bool]:
        """V3 S1：把最近一次 run_sql 结果渲染成图表规格，追加到 charts。

        接地约束：必须已有执行结果，且 x/y 是结果表里的真实列名——不许对臆造列作图。
        规格随后由 answer_to_blocks 连同数据行投影成 chart 块。
        """
        if not data_result or not (data_result.get("rows") or []):
            return (
                {"error": "尚无查询结果，请先用 run_sql 取到数据再作图。"},
                "作图失败：无数据",
                True,
            )
        kind = str(args.get("kind") or "").strip()
        if kind not in ("bar", "line", "area"):
            return (
                {"error": f"不支持的图型「{kind}」", "available": ["bar", "line", "area"]},
                f"作图失败：图型「{kind}」不支持",
                True,
            )
        columns = data_result.get("columns") or []
        col_keys = {str(c.get("key") or c.get("title") or "") for c in columns}
        x = str(args.get("x") or "").strip()
        y = str(args.get("y") or "").strip()
        missing = [c for c in (x, y) if c not in col_keys]
        if missing:
            return (
                {
                    "error": f"列 {missing} 不在结果表里，x/y 必须照抄结果列名。",
                    "available_columns": sorted(col_keys),
                },
                "作图失败：列名不在结果中",
                True,
            )
        spec = {"kind": kind, "x": x, "y": y}
        title = str(args.get("title") or "").strip()
        if title:
            spec["title"] = title
        charts.append(spec)
        return {"ok": True, "chart": spec}, f"已生成{kind}图（x={x}, y={y}）", False

    @staticmethod
    def _dispatch_analyze_result(
        args: dict, data_result: dict | None, analyses: list[dict]
    ) -> tuple[dict, str, bool]:
        """P5：对最近一次 run_sql 结果做统计画像 + IQR 离群检测（纯 stdlib，不外呼）。

        让「有没有异常/分布如何」这类分析有**真实计算**支撑，而非模型对结果的口头臆测。
        接地约束：必须已有执行结果；只分析数值列。结果随后投影成 insight 块，且其统计数值
        登记进事实账本（见 _ledger_register），答案复述均值/离群时不被 F4 判成幻觉。
        """
        rows = (data_result or {}).get("rows") or []
        if not rows:
            return (
                {"error": "尚无查询结果，请先用 run_sql 取到数据再分析。"},
                "分析失败：无数据",
                True,
            )
        columns = [str(c.get("key") or c.get("title") or "") for c in (data_result.get("columns") or [])]
        if not columns:
            columns = list({k for r in rows if isinstance(r, dict) for k in r})
        want = [str(c).strip() for c in (args.get("columns") or []) if str(c).strip()]
        order_by = str(args.get("order_by") or "").strip()
        if order_by and order_by not in columns:
            order_by = ""  # 不在结果里就忽略（仍出统计，只是不算趋势）
        try:
            max_out = int(args.get("max_outliers") or 5)
        except (TypeError, ValueError):
            max_out = 5
        max_out = max(1, min(max_out, 20))

        def _num(v: Any) -> float | None:
            if isinstance(v, bool) or v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.replace(",", "").strip())
                except ValueError:
                    return None
            return None

        dict_rows = [r for r in rows if isinstance(r, dict)]
        # 趋势/突变需要**有序序列**：仅当给了 order_by 才排序并计算，避免对无序数据误判趋势。
        ordered_rows = dict_rows
        if order_by:
            def _okey(r: dict) -> tuple:
                v = r.get(order_by)
                nv = _num(v)
                return (0, nv) if nv is not None else (1, str(v))
            ordered_rows = sorted(dict_rows, key=_okey)

        def _trend_and_jumps(pairs: list[tuple[Any, float]]) -> tuple[dict | None, list[dict]]:
            ys = [v for _, v in pairs]
            n = len(ys)
            if n < 4:
                return None, []
            xs = list(range(n))
            mx = statistics.fmean(xs)
            my = statistics.fmean(ys)
            denom = sum((x - mx) ** 2 for x in xs)
            slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom) if denom else 0.0
            first, last = ys[0], ys[-1]
            change = last - first
            span = max(ys) - min(ys)
            eps = (span or abs(my) or 1.0) * 0.01
            direction = "up" if slope > eps else ("down" if slope < -eps else "flat")
            trend = {
                "direction": direction, "slope": round(slope, 4),
                "first": first, "last": last, "change": round(change, 4),
                "change_pct": round(change / abs(first) * 100, 2) if first else None,
            }
            deltas = [ys[i] - ys[i - 1] for i in range(1, n)]
            ad = [abs(d) for d in deltas]
            jumps: list[dict] = []
            if len(ad) >= 3:
                # 突变阈值用「3×典型步长（中位）」——中位对单个大突变稳健，不像 mean+kσ 会被
                # 突变自身抬高阈值而漏检（masking）。
                thr = max(eps, 3 * statistics.median(ad))
                for i, d in enumerate(deltas, start=1):
                    if abs(d) > thr:
                        jumps.append({"at": pairs[i][0], "from": pairs[i - 1][1],
                                      "to": pairs[i][1], "delta": round(d, 4)})
            jumps.sort(key=lambda j: -abs(j["delta"]))
            return trend, jumps[:max_out]

        col_reports: list[dict] = []
        total_outliers = 0
        total_jumps = 0
        for col in columns:
            if want and col not in want:
                continue
            if order_by and col == order_by:
                continue  # 排序维度本身不作为度量分析
            raw = [r.get(col) for r in dict_rows]
            vals = [x for x in (_num(v) for v in raw) if x is not None]
            nulls = len(raw) - len(vals)
            # 数值列判定：至少一半非空值可解析为数
            if not vals or len(vals) < max(1, len([v for v in raw if v is not None]) / 2):
                continue
            n = len(vals)
            rep: dict[str, Any] = {
                "column": col, "count": n, "nulls": nulls,
                "min": min(vals), "max": max(vals),
                "mean": round(statistics.fmean(vals), 4),
            }
            if n >= 2:
                q = statistics.quantiles(vals, n=4)  # [p25, p50, p75]
                p25, p50, p75 = q[0], q[1], q[2]
                iqr = p75 - p25
                rep.update({
                    "p25": round(p25, 4), "median": round(p50, 4), "p75": round(p75, 4),
                    "std": round(statistics.stdev(vals), 4),
                })
                lo, hi = p25 - 1.5 * iqr, p75 + 1.5 * iqr
                outliers = [v for v in vals if v < lo or v > hi]
                rep["outlier_count"] = len(outliers)
                rep["outliers"] = sorted(outliers, key=lambda v: -abs(v - rep["mean"]))[:max_out]
                total_outliers += len(outliers)
            if order_by:
                pairs = [(r.get(order_by), _num(r.get(col))) for r in ordered_rows]
                pairs = [(lbl, v) for lbl, v in pairs if v is not None]
                trend, jumps = _trend_and_jumps(pairs)
                if trend:
                    rep["trend"] = trend
                if jumps:
                    rep["jumps"] = jumps
                    total_jumps += len(jumps)
            col_reports.append(rep)

        if not col_reports:
            return (
                {"error": "结果里没有可分析的数值列。", "columns": columns},
                "分析失败：无数值列",
                True,
            )
        analysis = {
            "row_count": len(rows), "columns": col_reports,
            "total_outliers": total_outliers, "total_jumps": total_jumps,
        }
        if order_by:
            analysis["ordered_by"] = order_by
        _extra = f"、{total_jumps} 处突变" if order_by else ""
        analyses.append(analysis)
        return (
            {"analysis": analysis},
            f"已分析 {len(col_reports)} 个数值列，发现 {total_outliers} 个离群值{_extra}",
            False,
        )

    def _dispatch_get_lineage(
        self, db: Session, *, ontology_id: str, args: dict
    ) -> tuple[dict, str, bool]:
        """V3 S2：某对象的血缘/上下游邻域子图，包 `get_ontology_graph`（中心+depth BFS）。

        DataHub 表级血缘在 ingest 时已落成 `structure_type=derivation` 的关系边，故直接查
        已发布关系图即可，天然按域/发布范围收敛。center_id 须是已发布对象 id。
        """
        center_id = str(args.get("center_id") or "").strip()
        if not center_id:
            return (
                {"error": "需要 center_id（对象 id）", "hint": "先用 search_objects 定位对象拿到 id"},
                "血缘缺中心对象",
                True,
            )
        center = self.query_service.get_object_type(db, center_id)
        if not center or center.status != "published":
            return (
                {"error": "中心对象不存在或未发布", "hint": "先用 search_objects 定位已发布对象的 id"},
                "中心对象未命中",
                True,
            )
        try:
            depth = int(args.get("depth") or 1)
        except (TypeError, ValueError):
            depth = 1
        depth = max(1, min(depth, 3))
        graph = self.query_service.get_ontology_graph(
            db, ontology_id, center_id=center_id, depth=depth, published_only=True
        )
        payload = graph.model_dump() if hasattr(graph, "model_dump") else dict(graph)
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        result = {
            "center_id": center_id,
            "center_name": center.display_name,
            "depth": depth,
            "truncated": bool(payload.get("truncated")),
            "nodes": nodes,
            "edges": edges,
        }
        summary = f"「{center.display_name}」邻域：{len(nodes)} 对象 / {len(edges)} 关系"
        return result, summary, False

    _PROPOSE_LOGIC_TYPES = ("metric", "tag", "rule")
    _PROPOSE_TYPE_LABEL = {"metric": "指标", "tag": "标签", "rule": "规则"}

    def _dispatch_propose_draft(self, *, domain_id: str, args: dict) -> tuple[dict, str, bool]:
        """V3 S3：为新建口径定义产出**提案**（纯 spec，不写库）。

        Data Agent 只出提案；真正建库草稿由用户在前端点「去确认」后走 POST /api/business-logics。
        故本方法无任何 DB 写——ask() 保持只读（写侧仍走既有 draft→confirm→execute 治理骨架）。
        不替用户编表达式（口径是人定的）：提案只含名字、类型、自然语言说明。
        """
        display_name = str(args.get("display_name") or "").strip()
        if not display_name:
            return {"error": "需要 display_name（口径中文名）"}, "提案缺名称", True
        logic_type = str(args.get("logic_type") or "").strip()
        if logic_type not in self._PROPOSE_LOGIC_TYPES:
            return (
                {"error": f"logic_type 须为 metric/tag/rule，收到「{logic_type}」",
                 "available": list(self._PROPOSE_LOGIC_TYPES)},
                "提案类型非法",
                True,
            )
        name = str(args.get("name") or "").strip()
        if not name:
            # 无英文标识符则由中文名派生一个安全 slug（非 ascii 落空时给占位名，用户可在表单改）
            slug = re.sub(r"[^a-zA-Z0-9_]+", "_", display_name).strip("_").lower()
            name = slug or "new_logic"
        description = str(args.get("description") or "").strip()
        proposal = {
            "kind": "business_logic",
            "logic_type": logic_type,
            "display_name": display_name,
            "name": name,
            "description": description,
            # 前端「去确认」按钮原样 POST /api/business-logics 的载荷（BusinessLogicCreate）。
            "create_payload": {
                "domain_id": domain_id,
                "name": name,
                "display_name": display_name,
                "logic_type": logic_type,
                "description": description or None,
                "expression_summary": description or None,
            },
        }
        label = self._PROPOSE_TYPE_LABEL[logic_type]
        return proposal, f"提案：新建{label}「{display_name}」", False

    def _dispatch_lint(self, db: Session, *, args: dict) -> tuple[dict, str, bool]:
        """规约自检：用当前生效规约 lint 一份规格，返回违规项+可照做的 fix（只读，不写库）。

        「事前遵循」的自检工具——agent 提含物理表名的规格前调，据 fix 自改而非等治理闸门打回。
        口径提案无物理表名时返回空（合规）。
        """
        spec = args.get("spec")
        if not isinstance(spec, dict):
            return {"error": "需要 spec 对象"}, "规约自检失败：无 spec", True
        kind = str(args.get("kind") or "").strip()
        violations = lint_against_standard(kind, spec, db)
        return (
            {"violations": violations, "compliant": not violations},
            f"规约自检：{len(violations)} 项待修" if violations else "规约自检：合规",
            False,
        )

    @staticmethod
    def _dispatch_propose_preference(*, domain_id: str, args: dict) -> tuple[dict, str, bool]:
        """P3.1：为「跨会话应记住的约定」产出提案（纯 spec，不写库）。

        与 propose_draft/propose_action 同构——ask() 保持只读：真正落库由用户在前端点「记住」
        触发 POST /chat-bi/domain-memory/preferences。守住「agent 只提案、写在人点击」不变量。
        """
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "需要 text（要记住的约定）"}, "记忆提案为空", True
        proposal = {"kind": "preference", "text": text[:255], "domain_id": domain_id}
        return proposal, f"提案：记住约定「{text[:24]}」", False

    def _dispatch_propose_action(
        self, *, ontology_id: str, domain_id: str, args: dict
    ) -> tuple[dict, str, bool]:
        """P0：为新建数据任务（物化/同步/加工）产出**提案**（纯 spec，不写库、不执行）。

        与 propose_draft 同构：ask() 保持只读——真正的 draft（写一条 GovernanceArtifact）
        由用户在前端点「去校验并执行」后触发，落在 publisher 门控 + 人工确认（含 dry-run 差异）
        之后。本方法只组装前端按钮原样回传给 POST /api/agents/draft 的载荷。
        """
        kind = str(args.get("kind") or "").strip()
        if kind not in _ACTION_KINDS:
            return (
                {"error": f"kind 须为 {'/'.join(_ACTION_KINDS)}，收到「{kind}」",
                 "available": list(_ACTION_KINDS)},
                "提案类型非法",
                True,
            )
        intent = str(args.get("intent") or "").strip()
        if not intent:
            return {"error": "需要 intent（任务意图）"}, "提案缺意图", True
        context = args.get("context")
        if not isinstance(context, dict):
            context = {}
        proposal = {
            "kind": kind,
            "intent": intent,
            "context": context,
            "ontology_id": ontology_id,
            # 前端「去校验并执行」按钮原样 POST /api/agents/draft 的载荷（ArtifactDraftRequest）。
            "draft_payload": {
                "kind": kind,
                "intent": intent,
                "context": context,
                "ontology_id": ontology_id,
            },
        }
        label = _ACTION_KIND_LABEL[kind]
        return proposal, f"提案：新建{label}任务「{intent[:24]}」", False

    @staticmethod
    def _dispatch_update_plan(args: dict) -> tuple[dict, str, bool]:
        """P2：产出/更新一份多步分析计划（纯 echo，不写库、不接地）。

        计划是「打算怎么拆解」的可见路线图，与实时执行轨迹（steps）互补。整份计划每次整体
        覆盖（同 TodoWrite）。计划文本不进 answer，不参与接地校验，故不会因步骤标题触发拒答。
        """
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return {"error": "需要 steps（非空步骤数组）"}, "计划为空", True
        steps: list[dict] = []
        for item in raw_steps[:_PLAN_MAX_STEPS]:
            if isinstance(item, str):
                title, status = item.strip(), "pending"
            elif isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                status = str(item.get("status") or "pending").strip()
            else:
                continue
            if not title:
                continue
            if status not in _PLAN_STATUSES:
                status = "pending"
            steps.append({"title": title[:120], "status": status})
        if not steps:
            return {"error": "steps 里没有有效步骤（需 title）"}, "计划无有效步骤", True
        note = str(args.get("note") or "").strip()[:200]
        plan = {"steps": steps, "note": note}
        done = sum(1 for s in steps if s["status"] == "done")
        return {"plan": plan}, f"计划 {len(steps)} 步（完成 {done}）", False

    def _live_task_state(self, db: Session, artifact: Any) -> dict | None:
        """尽力回读一个物化任务的 Airflow 实时态（多批 DagRun 聚合）。**从不抛异常**。

        制品状态在 execute() 提交 DAG 后即置 succeeded，但 DAG 在 Airflow 里可能还在跑——
        故实时权威是 Airflow。复用 warehouse 的批次解析 + 状态聚合，读不到就返回 None（退制品态）。
        """
        try:
            from app.api.warehouse import _aggregate_state, _receipt_batches  # noqa: PLC0415
            from app.connectors.airflow import AirflowClient, AirflowError, is_terminal  # noqa: PLC0415

            batches = _receipt_batches(db, artifact.id)
            if not batches:
                return None
            rt = self.settings_service.get_airflow_runtime(db)
            client = AirflowClient(
                rt.endpoint, username=rt.username, password=rt.password,
                token=rt.token, api_version=rt.api_version,
            )
            try:
                states: list = []
                run_url = None
                for b in batches:
                    bid, brun = b.get("dag_id"), b.get("dag_run_id")
                    if not bid or not brun:
                        states.append(b.get("state") or "failed")
                        continue
                    try:
                        run = client.get_dag_run(bid, brun)
                        states.append(run.get("state"))
                        run_url = run_url or client.run_url(bid, brun)
                    except AirflowError:
                        states.append(None)
            finally:
                client.close()
            agg = _aggregate_state(states)
            if not agg:
                return None
            return {"live_state": agg, "terminal": is_terminal(agg), "run_url": run_url}
        except Exception as exc:  # noqa: BLE001 — 实时态是增强，读不到退回制品态，绝不炸问答
            logger.info("live task state unavailable for %s: %s", getattr(artifact, "id", "?"), exc)
            return None

    def _dispatch_get_task_status(
        self, db: Session, *, ontology_id: str, args: dict, conversation_id: str | None = None
    ) -> tuple[dict, str, bool]:
        """回读数据任务（治理制品）状态与回执摘要。纯 DB 读。

        不引入 Airflow 实时轮询（那条留给现有 UI 状态端点）：读 GovernanceArtifact.status
        与 execution_receipt_json 已能回答「跑完没/成没成功」。

        P1 跨轮任务记忆：未指定 artifact_id 时，**优先**返回本会话催生的任务
        （conversation_id ↔ artifact 关联），用户不必重报 id 即可问「那个任务好了吗」；
        本会话无关联任务时回落到「本体最近任务」（P0 行为）。
        """
        from app.api.deps import agent_pipeline  # 延迟导入避免与 deps 循环

        def _summarize_receipt(raw: str | None) -> str | None:
            if not raw:
                return None
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return None
            if not isinstance(data, dict):
                return None
            if data.get("error"):
                return f"错误：{str(data['error'])[:80]}"
            keys = ("rows", "row_count", "dag_run_id", "dag_id", "table", "target_table", "message")
            picked = {k: data[k] for k in keys if k in data}
            return json.dumps(picked, ensure_ascii=False)[:160] if picked else None

        def _project(a) -> dict:
            return {
                "id": a.id,
                "kind": a.kind,
                "name": a.name,
                "status": a.status,
                "is_high_risk": a.is_high_risk,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                "receipt_summary": _summarize_receipt(a.execution_receipt_json),
            }

        artifact_id = str(args.get("artifact_id") or "").strip()
        if artifact_id:
            a = agent_pipeline.get(db, artifact_id)
            if a is None or (a.ontology_id and a.ontology_id != ontology_id):
                return {"error": "任务不存在或不属于当前数据域"}, "任务未命中", True
            task = _project(a)
            # 实时权威在 Airflow：单任务查询时尽力回读一次 DagRun 实时态（best-effort，失败退制品态）。
            live = self._live_task_state(db, a)
            if live:
                task.update(live)
            state_label = (live or {}).get("live_state") or a.status
            return {"tasks": [task]}, f"任务「{a.name}」：{state_label}", False

        try:
            limit = int(args.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))

        # P1：优先本会话催生的任务（跨轮记忆）
        if conversation_id:
            linked_ids = self.list_conversation_task_ids(db, conversation_id)
            linked = [a for aid in linked_ids if (a := agent_pipeline.get(db, aid)) is not None]
            if linked:
                tasks = [_project(a) for a in linked[:limit]]
                return (
                    {"tasks": tasks, "total": len(linked), "scope": "conversation"},
                    f"本会话的 {len(tasks)} 个数据任务",
                    False,
                )

        kind = str(args.get("kind") or "").strip() or None
        if kind and kind not in _ACTION_KINDS:
            kind = None
        rows = agent_pipeline.list_artifacts(db, ontology_id=ontology_id, kind=kind)
        tasks = [_project(a) for a in rows[:limit]]
        return (
            {"tasks": tasks, "total": len(rows), "scope": "ontology"},
            f"最近 {len(tasks)} 个数据任务" if tasks else "暂无数据任务",
            False,
        )

    def _dispatch_agent_tool(
        self,
        db: Session,
        *,
        domain_id: str,
        ontology_id: str,
        name: str,
        args: dict,
        principal_role: str | None = None,
    ) -> tuple[Any, str, bool]:
        """执行一个工具调用，返回 (结果对象, 人类可读摘要, 是否错误)。同步，供 to_thread 调用。"""
        qs = self.query_service
        try:
            if name == "search_objects":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_object_types(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_object_summary(o) for o in page.items]
                items, extra = self._augment_semantic(
                    db, ontology_id=ontology_id, keyword=kw, kind="object_type",
                    items=items, loader=self._load_object_summaries,
                )
                return self._search_envelope(
                    items, page.total + extra, "对象", facet_key="table_role"
                )
            if name == "get_object":
                detail = qs.get_object_type(db, str(args.get("object_id") or ""))
                if not detail or detail.status != "published":
                    return {"error": "对象不存在或未发布"}, "对象未命中", True
                return self._compact_object_detail(detail), f"对象「{detail.display_name}」{len(detail.properties)} 字段", False
            if name == "search_relations":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_relation_types(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_relation(r) for r in page.items]
                return self._search_envelope(items, page.total, "关系", facet_key="cardinality")
            if name == "search_logics":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_business_logics(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_logic_summary(l) for l in page.items]
                items, extra = self._augment_semantic(
                    db, ontology_id=ontology_id, keyword=kw, kind="business_logic",
                    items=items, loader=self._load_logic_summaries,
                )
                return self._search_envelope(
                    items, page.total + extra, "口径", facet_key="logic_type"
                )
            if name == "get_logic":
                detail = qs.get_business_logic(db, str(args.get("logic_id") or ""))
                if not detail or detail.status != "published":
                    return {"error": "业务逻辑不存在或未发布"}, "口径未命中", True
                return self._compact_logic_detail(detail), f"口径「{detail.display_name}」", False
            if name == "get_domain_overview":
                return self._dispatch_domain_overview(db, ontology_id=ontology_id)
            if name == "find_join_path":
                return self._dispatch_join_path(db, ontology_id=ontology_id, args=args)
            if name == "profile_values":
                return self._dispatch_profile_values(
                    db, ontology_id=ontology_id, args=args, principal_role=principal_role
                )
            if name == "compile_metric":
                return self._dispatch_compile_metric(db, args=args)
            if name == "get_lineage":
                return self._dispatch_get_lineage(db, ontology_id=ontology_id, args=args)
            if name == "propose_draft":
                return self._dispatch_propose_draft(domain_id=domain_id, args=args)
            if name == "lint_against_standard":
                return self._dispatch_lint(db, args=args)
            if name == "propose_action":
                return self._dispatch_propose_action(
                    ontology_id=ontology_id, domain_id=domain_id, args=args
                )
            if name == "propose_preference":
                return self._dispatch_propose_preference(domain_id=domain_id, args=args)
            if name == "update_plan":
                return self._dispatch_update_plan(args)
            # get_task_status 在 agent 循环里特判（需注入 conversation_id 做跨轮解析），不走此处。
            if name == "ask_clarification":
                q = str(args.get("question") or "").strip()
                if not q:
                    return {"error": "需要 question"}, "澄清问题为空", True
                opts = [str(o) for o in (args.get("options") or []) if str(o).strip()]
                return (
                    {"clarification": True, "question": q, "options": opts,
                     "reason": str(args.get("reason") or "")},
                    f"向用户澄清：{q}",
                    False,
                )
            if name == "run_sql":
                return self._dispatch_run_sql(
                    db, args=args, ontology_id=ontology_id, principal_role=principal_role
                )
            return {"error": f"未知工具：{name}"}, "未知工具", True
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent tool %s failed: %s", name, exc)
            return {"error": str(exc)[:300]}, f"工具异常：{type(exc).__name__}", True

    # 有些模型不走 OpenAI 原生 function-calling，而把工具调用以 DSML/XML 文本写进正文：
    #   <｜｜DSML｜｜invoke name="search_relations">
    #     <｜｜DSML｜｜parameter name="keyword" string="true">分组</｜｜DSML｜｜parameter>
    #   </｜｜DSML｜｜invoke>
    # 仅锚定 invoke/parameter 关键字，兼容全角竖线等任意前缀。
    _TEXT_INVOKE_RE = re.compile(
        r'invoke\s+name="([^"]+)"(.*?)</[^<>]*?invoke[^<>]*?>', re.S | re.I
    )
    _TEXT_PARAM_RE = re.compile(
        r'parameter\s+name="([^"]+)"[^<>]*?>(.*?)</[^<>]*?parameter[^<>]*?>', re.S | re.I
    )

    @classmethod
    def _extract_text_tool_calls(cls, content: str | None) -> list:
        """从正文解析非原生（DSML/XML 文本）的工具调用，返回与原生 tool_call 同形的对象。

        兼容部分模型（如部署不完善的 GLM/DeepSeek 网关）把工具调用写到 content 而非
        tool_calls 字段的情形，避免把一堆标记当答案输出。
        """
        if not content or "invoke" not in content.lower():
            return []
        calls: list = []
        for i, m in enumerate(cls._TEXT_INVOKE_RE.finditer(content)):
            name = (m.group(1) or "").strip()
            if not name:
                continue
            args: dict = {}
            for pm in cls._TEXT_PARAM_RE.finditer(m.group(2) or ""):
                args[(pm.group(1) or "").strip()] = (pm.group(2) or "").strip()
            calls.append(
                types.SimpleNamespace(
                    id=f"txt_{i}",
                    type="function",
                    function=types.SimpleNamespace(
                        name=name, arguments=json.dumps(args, ensure_ascii=False)
                    ),
                )
            )
        return calls

    @staticmethod
    def _strip_tool_markup(text: str | None) -> str:
        """去除正文里泄漏的工具调用标记（DSML/tool_calls/invoke/parameter），避免当答案展示。"""
        if not text:
            return text or ""
        t = re.sub(r"<[^<>]*?tool_calls[^<>]*?>.*?</[^<>]*?tool_calls[^<>]*?>", "", text, flags=re.S | re.I)
        t = re.sub(r"<[^<>]*?invoke\b.*?</[^<>]*?invoke[^<>]*?>", "", t, flags=re.S | re.I)
        # 未闭合/残留：从孤立的 DSML/tool 起始标签到结尾全部舍弃
        t = re.sub(r"<[^<>]*?(?:DSML|tool_calls|invoke|parameter)\b.*$", "", t, flags=re.S | re.I)
        t = re.sub(r"</?[^<>]*?DSML[^<>]*?>", "", t, flags=re.I)
        return t.strip()

    @staticmethod
    def _classify_intent(question: str) -> str:
        """意图分类：``"analytical"`` / ``"structural"`` / ``"general"``。

        以「需要精准回答的意图」正向定义，规则优先、确定性、零额外 LLM 调用：
        - 命中取数标记 → analytical（要真数据，需接地；赢平局，绝不误伤真实取数）；
        - 否则命中结构标记 → structural（要具体本体元数据，需接地）；
        - 否则 → **general**（默认；打招呼/问能力/产品用法等，自由作答、不要求接地）。

        注意默认落到 general 而非 analytical：拒答是少数精准场景的例外，不是常态。
        取数工具可用性（sql_allowed）另按 `intent != structural` 判，仍对 general 放开取数。
        """
        q = (question or "").lower()
        if any(m in q for m in _ANALYTICAL_MARKERS):
            return "analytical"
        if any(m in q for m in _STRUCTURAL_MARKERS):
            return "structural"
        return "general"

    @staticmethod
    def _extract_sql_from_text(text: str | None) -> str | None:
        """从答案正文里抽取 ```sql 围栏块（兜底：模型未走 run_sql 时不丢 SQL）。"""
        if not text:
            return None
        m = re.search(r"```sql\s+(.*?)```", text, re.S | re.I)
        if not m:
            m = re.search(r"```\s*(SELECT\b.*?)```", text, re.S | re.I)
        return m.group(1).strip() if m else None

    @staticmethod
    def _steps_to_caliber(
        steps: list[dict],
        referenced_objects: list[dict],
        referenced_logics: list[dict],
        compiled: list[dict] | None = None,
    ) -> list[dict]:
        """口径拆解卡——**只保留编译器的口径展开契约**。

        每项来自 P3 口径编译器的 ``caliber_trace``：由本体确定性生成、已自证的
        「口径如何展开成这条查询」。此前还按 steps 反推「对象口径 / 逻辑口径 / 数据查询」
        三类卡——那是事后猜测（「调了 get_object，那大概是个对象口径」），且「N 字段」是
        无效计数、对象 chip 又与展开轨迹里的「关联」及底部引用重复。已移除；命中的对象/口径
        改为在块投影层（``chat_bi_blocks._caliber_hits``）汇成一行去重的「命中本体」。

        ``steps`` / ``referenced_*`` 形参保留（调用点不变），但不再参与卡片构建。
        """
        items: list[dict] = []
        for c in compiled or []:
            items.append({
                "label": f"口径展开 · {c.get('logic') or ''}".strip(" ·"),
                "description": "\n".join(c.get("caliber_trace") or []),
                "references": (
                    [{"kind": "business_logic", "id": c["logic_id"],
                      "display_name": c.get("logic")}]
                    if c.get("logic_id") else []
                ),
            })
        return items

    async def _stream_final_answer(
        self, client: AsyncOpenAI, model: str, messages: list[dict], *, nudge: str | None = None
    ) -> AsyncIterator[str]:
        """最终作答轮：不带工具、stream=True 逐 token 产出（真·逐字流式）。"""
        msgs = messages if nudge is None else messages + [{"role": "user", "content": nudge}]
        stream = await client.chat.completions.create(model=model, messages=msgs, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta

    @staticmethod
    async def _emit_answer_tokens(text: str) -> AsyncIterator[dict]:
        """将已生成并通过接地校验的答案按小片逐字流式产出。

        只有确定不会被拒答后才调用，从而避免“先给内容再撤回”的观感；
        因答案已在服务端生成完毕，此处仅模拟打字机节奏。
        """
        if not text:
            return
        # 依长度自适应步长，长答案加大步长以控制总时长
        step = 1 if len(text) <= 200 else (2 if len(text) <= 600 else 4)
        for i in range(0, len(text), step):
            yield {"type": "token", "delta": text[i : i + step]}
            await asyncio.sleep(0.012)

    async def _stream_agent_events(
        self,
        db: Session,
        *,
        runtime: Any,
        domain: DomainContext,
        ontology: Ontology,
        question: str,
        history: list[dict],
        seed_objects: list[_ObjectSnapshot],
        seed_logics: list[BusinessLogic],
        principal_role: str | None = None,
        telemetry: RunTelemetry | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """多步工具编排的事件流：yield step_start / step_done / token / done。

        流式与非流式共用此核心；`_run_agent_loop` 只是消费到 done 后聚合返回。
        接地判定 / 引用归一 / SQL 美化等收尾在 ask()/ask_stream() 的 done 后处理里做。
        """
        client = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.api_base_url,
            timeout=env_settings.llm_timeout_seconds,
            http_client=make_async_http_client(),
        )

        seed_lines: list[str] = []
        if seed_objects:
            seed_lines.append(
                "· 业务对象：" + "、".join(f"{o.display_name}({o.name})" for o in seed_objects[:5])
            )
        if seed_logics:
            seed_lines.append(
                "· 业务逻辑：" + "、".join(f"{l.display_name}({l.name})" for l in seed_logics[:5])
            )
        # 候选作为 system 弱提示，并明确其性质，避免模型把它当成"用户提到的对象"而答非所问
        seed_note = ""
        if seed_lines:
            seed_note = (
                "\n\n【系统预匹配候选】以下是系统按关键词猜测的可能相关实体，"
                "仅作为检索起点参考，**可能与本次问题无关**；若无关请忽略，"
                "**切勿声称用户提到了这些对象**、也不要在回答里节外生枝地回应它们：\n"
                + "\n".join(seed_lines)
            )

        # P2.1：域语义卡常驻 system——模型开口前就知道域的骨架，
        # 不必先花一步调 get_domain_overview，也不必靠 prompt 铁律约束概览类问题。
        card = None
        try:
            card = build_card(db, ontology, domain.name)
            card_text = "\n\n" + card.render()
        except Exception as exc:  # noqa: BLE001 — 卡是增强，算不出就退回原样
            logger.info("domain semantic card unavailable: %s", exc)
            card_text = f"\n\n当前数据域：{domain.name}"

        # P3：跨会话记忆软提示——本域历史高频对象/口径，帮复现问题少绕检索、少重复澄清。
        try:
            memory_text = self.build_domain_memory_card(db, domain.id)
        except Exception as exc:  # noqa: BLE001 — 记忆是增强，算不出退空
            logger.info("domain memory card unavailable: %s", exc)
            memory_text = ""

        messages: list[dict] = [
            {"role": "system", "content": f"{_AGENT_SYSTEM_PROMPT}{card_text}{memory_text}{seed_note}"}
        ]
        for item in history[-6:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": str(content)})
        messages.append({"role": "user", "content": question})

        steps: list[dict] = []
        referenced_objects: list[dict] = []
        referenced_logics: list[dict] = []
        seen_obj: set[str] = set()
        seen_logic: set[str] = set()
        data_result: dict | None = None
        last_sql: str | None = None
        compiled_metrics: list[dict] = []  # P3：口径编译轨迹（权威口径卡的来源）
        compiled_sql: str | None = None
        clarification: dict | None = None  # P4.1：待用户澄清的缺口
        grounded_hit = False
        answer = ""
        ledger = FactLedger()  # F4：断言级凭证账本（只登记工具真实返回的事实）
        # 植入当前数据域名作为可信上下文，允许答案引用「数据域/本体名」而不被误判为幻觉
        ledger.add_context_name(domain.name)
        # 工具名是内部机制、非业务实体：若模型在正文提到工具名（如 get_domain_overview），
        # 不得被 answer_verifier 当成本体幻觉实体而拒答。
        ledger.add_context_name(
            *[t["function"]["name"] for t in _AGENT_TOOL_SCHEMAS],
            *_ALL_AGENT_TOOL_NAMES,
        )
        # 语义卡上的名字是**服务端从已发布本体算出来的**，与 get_domain_overview 同源同可信。
        # 不入账的话，模型引用卡上看到的核心对象/指标名会被 F4 当成幻觉——
        # 我们把事实塞进它的上下文，又因为它用了而拒答，说不过去。
        if card is not None:
            ledger.add_context_name(
                *[n for n, _ in card.core_objects],
                *[n for n, _ in card.clusters],
                *card.metrics,
            )

        tel = telemetry if telemetry is not None else RunTelemetry()

        # 意图门控（架构强制，非提示词）：仅**结构性**问题（问对象有哪些属性/字段/关系等）
        # 收窄取数工具，避免答非所问地追加取数 SQL。analytical 与 general 都保留取数能力
        # （对 general 放开是 fail-open：万一是未加标记的真实取数问题，工具仍在，不误伤）。
        intent = self._classify_intent(question)
        sql_allowed = intent != "structural"

        # V3 S1：技能层运行态。base_system 是不含技能 overlay 的系统提示基线，
        # 选中技能后就地重建 messages[0] = base_system + overlay（重复选取以最后一次为准）。
        base_system = messages[0]["content"]
        active_skill: Skill | None = None
        # P4：本域启用的外部工具（配置驱动，curated + 数量封顶）——注入工具集、按名分发。
        try:
            external_schemas = external_tools.tool_schemas_for_domain(db, domain.id)
            external_names = {s["function"]["name"] for s in external_schemas}
        except Exception as exc:  # noqa: BLE001 — 外部工具是增强，取不到就当没有
            logger.info("external tools unavailable: %s", exc)
            external_schemas, external_names = [], set()

        def _compose_tools(skill: "Skill | None") -> list[dict]:
            return [*_tools_for_skill(skill, sql_allowed=sql_allowed), *external_schemas]

        active_tools = _compose_tools(active_skill)
        # 规约意识：备好当前生效规约的约束卡；选中带 attach_governance 的技能（建数）时并入 overlay。
        try:
            governance_card = active_standard(db).compile_prompt_card()
        except Exception as exc:  # noqa: BLE001 — 规约取不到只是少一段增强，不该炸循环
            logger.info("governance card unavailable: %s", exc)
            governance_card = ""
        charts: list[dict] = []  # render_chart 产出的图表规格（挂到 data_result 上渲染）
        analyses: list[dict] = []  # analyze_result 产出的统计画像/离群（供 insight 块）
        lineage: dict | None = None  # get_lineage 产出的血缘邻域子图（供 lineage 块渲染）
        draft_proposals: list[dict] = []  # propose_draft 产出的建数提案（供 draft_proposal 块）
        preference_proposals: list[dict] = []  # propose_preference 产出的记忆提案（供 preference_proposal 块）
        action_proposals: list[dict] = []  # propose_action 产出的数据任务提案（供 action_proposal 块）
        task_statuses: list[dict] = []  # get_task_status 产出的任务状态（供 task_status 块）
        plan: dict | None = None  # update_plan 产出的多步分析计划（供 plan 块；整体覆盖）

        for _ in range(_AGENT_MAX_STEPS):
            tel.llm_call()
            resp = await client.chat.completions.create(
                model=runtime.model,
                messages=messages,
                tools=active_tools,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                # 部分模型不走原生 function-calling，而把工具调用以 DSML/XML 文本写进正文：
                # 解析并当作工具调用执行，避免把一堆标记当答案输出。
                tool_calls = self._extract_text_tool_calls(msg.content or "")
            if not tool_calls:
                # 没有工具调用 = 这一轮的 content 就是最终答案，**直接用**（P4.5）。
                # 原实现把它丢掉、再发一次全上下文请求去"流式"重新生成一遍：
                # 那次的 token 同样被 buffer 起来（没有透传给前端），最终仍由
                # `_emit_answer_tokens` 假打字机吐出——等于每问一次白付一整轮
                # prefill + 生成，换来一个模拟的流式效果。
                answer = self._strip_tool_markup(msg.content or "")
                if not answer:
                    # 少数模型会返回「空 content + 无 tool_calls」，此时才补一次显式收尾轮
                    tel.llm_call()
                    async for tok in self._stream_final_answer(client, runtime.model, messages):
                        answer += tok
                    answer = self._strip_tool_markup(answer)
                break

            # 存入历史的 content 去掉工具标记，避免污染上下文并被当作 thought 展示。
            clean_content = self._strip_tool_markup(msg.content or "")
            messages.append(
                {
                    "role": "assistant",
                    "content": clean_content,
                    "tool_calls": [
                        {
                            "id": t.id,
                            "type": "function",
                            "function": {"name": t.function.name, "arguments": t.function.arguments},
                        }
                        for t in tool_calls
                    ],
                }
            )
            # 工具间的模型自述：作为 thought 伪步穿插进轨迹（贴近 Claude Code 的"先说再做"），
            # 既实时流给前端、也落进 steps 持久化；_steps_to_caliber 因 tool 不匹配自动忽略它。
            thought = clean_content.strip()
            if thought:
                t_idx = len(steps)
                steps.append({"index": t_idx, "kind": "thought", "tool": "", "text": thought})
                yield {"type": "thought", "index": t_idx, "text": thought}
            for t in tool_calls:
                tool_name = t.function.name
                try:
                    call_args = json.loads(t.function.arguments or "{}")
                    if not isinstance(call_args, dict):
                        call_args = {}
                except (json.JSONDecodeError, TypeError):
                    call_args = {}

                idx = len(steps)
                yield {"type": "step_start", "index": idx, "tool": tool_name, "arguments": call_args}

                if (not sql_allowed) and tool_name in _SQL_TOOL_NAMES:
                    # 意图门控硬拒：结构性 turn 里，就算模型（或正文 tool-call 绕过）发出取数
                    # 工具，也在执行层拦下——这是真正的强制，覆盖非原生模型的正文工具调用路径。
                    result = {"error": "结构性问题无需取数，已跳过取数工具", "code": "sql_not_allowed"}
                    summary, is_error = "结构性问题：已跳过取数工具", True
                elif tool_name == "select_skill":
                    # V3 S1：切换技能——叠 prompt overlay + 解锁额外工具。只解锁不收窄。
                    active_skill, result, summary, is_error = self._apply_select_skill(
                        call_args, messages, base_system, governance_card
                    )
                    # 升级阀：模型显式选 query（取数）技能=有意识的取数请求 → 放开取数能力。
                    # 只挡「无意识漂移」，给规则误判留一条可恢复路径。
                    if active_skill is not None and active_skill.name == "query":
                        sql_allowed = True
                    active_tools = _compose_tools(active_skill)
                elif tool_name == "render_chart":
                    # V3 S1：把最近的 run_sql 结果渲染成图表；x/y 须为真实结果列（接地）。
                    result, summary, is_error = self._dispatch_render_chart(
                        call_args, data_result, charts
                    )
                elif tool_name == "analyze_result":
                    # P5：对最近的 run_sql 结果做统计画像+离群检测（真实计算，接地）。
                    result, summary, is_error = self._dispatch_analyze_result(
                        call_args, data_result, analyses
                    )
                elif tool_name == "locate_entities":
                    # P4.2：子 agent 在**隔离上下文**里跑检索循环，
                    # 只有结论回到主上下文；试错过程一个字符都不进来。
                    result, summary, is_error = await self._dispatch_locate_entities(
                        db, client=client, model=runtime.model,
                        domain_id=domain.id, ontology_id=ontology.id,
                        args=call_args, telemetry=tel,
                    )
                elif tool_name == "get_task_status":
                    # P1：跨轮任务记忆——注入本会话 id，未指定 artifact_id 时优先解析本会话催生的任务。
                    result, summary, is_error = await asyncio.to_thread(
                        self._dispatch_get_task_status,
                        db,
                        ontology_id=ontology.id,
                        args=call_args,
                        conversation_id=conversation_id,
                    )
                elif tool_name in external_names:
                    # P4：配置驱动的外部工具——通用 HTTP executor 取回，结果封顶；从不抛异常进循环。
                    result, summary, is_error = await asyncio.to_thread(
                        external_tools.call_external_tool,
                        db,
                        tool_name=tool_name,
                        domain_id=domain.id,
                        args=call_args,
                    )
                else:
                    result, summary, is_error = await asyncio.to_thread(
                        self._dispatch_agent_tool,
                        db,
                        domain_id=domain.id,
                        ontology_id=ontology.id,
                        name=tool_name,
                        args=call_args,
                        principal_role=principal_role,
                    )
                tel.tool(tool_name, is_error=is_error)
                if isinstance(result, dict):
                    if result.get("clarification") and not is_error:
                        # P4.1：模型判定缺口只能由用户补齐 → 本轮到此为止，把问题抛回去。
                        # 这**不是拒答**：拒答是「答不了」，澄清是「先确认再答」，
                        # 两者对用户的下一步指引完全不同，也不该混进拒答率里。
                        clarification = {
                            "question": result["question"],
                            "options": result.get("options") or [],
                            "reason": result.get("reason") or "",
                        }
                        steps.append({
                            "index": idx, "tool": tool_name, "arguments": call_args,
                            "status": "succeeded", "summary": summary,
                        })
                        yield {"type": "step_done", "index": idx,
                               "status": "succeeded", "summary": summary}
                        tel.clarification()
                        break
                    if result.get("code"):
                        tel.rejection(str(result["code"]))
                    if tool_name == "run_sql":
                        tel.run_sql_outcome(
                            "executed" if result.get("executed")
                            else ("rejected" if result.get("rejected") else "suggest_only")
                        )

                # 从工具轨迹收割结构化产物
                if not is_error and (
                    (tool_name.startswith("search_") and _search_items(result))
                    or (tool_name in (
                        "get_object", "get_logic", "get_domain_overview",
                        "find_join_path", "profile_values", "compile_metric", "get_lineage",
                        "propose_draft", "propose_action", "get_task_status",
                        "propose_preference",
                    ))
                    or (tool_name == "run_sql" and isinstance(result, dict) and (result.get("executed") or result.get("sql")))
                    # P5：结果分析成功=基于真实数据的统计，算接地。
                    or (tool_name == "analyze_result" and isinstance(result, dict) and result.get("analysis"))
                    # P4：外部工具成功=拿到真实数据，算接地（否则纯外部工具答案会被误判未接地拒答）。
                    or (tool_name in external_names)
                ):
                    grounded_hit = True
                if isinstance(result, dict):
                    if tool_name == "get_object" and result.get("id") and result["id"] not in seen_obj:
                        seen_obj.add(result["id"])
                        referenced_objects.append(
                            {"id": result["id"], "name": result.get("name"), "display_name": result.get("display_name")}
                        )
                    elif tool_name == "get_logic" and result.get("id") and result["id"] not in seen_logic:
                        seen_logic.add(result["id"])
                        referenced_logics.append(
                            {"id": result["id"], "name": result.get("name"), "display_name": result.get("display_name")}
                        )
                    elif tool_name == "compile_metric" and result.get("compiled"):
                        compiled_metrics.append({
                            "logic_id": result.get("logic_id"),
                            "logic": result.get("logic"),
                            "caliber_trace": result.get("caliber_trace") or [],
                        })
                        # 模型可能只编译不执行（无数据源时很常见）——SQL 不能丢
                        compiled_sql = result.get("sql") or compiled_sql
                    elif tool_name == "run_sql":
                        if result.get("sql"):
                            last_sql = result["sql"]
                        if result.get("executed"):
                            data_result = {
                                "columns": result.get("columns") or [],
                                "rows": result.get("rows") or [],
                                "truncated": bool(result.get("truncated")),
                            }
                    elif tool_name == "get_lineage" and result.get("nodes"):
                        lineage = result
                    elif tool_name == "propose_draft" and result.get("create_payload"):
                        draft_proposals.append(result)
                    elif tool_name == "propose_preference" and result.get("text"):
                        preference_proposals.append(result)
                    elif tool_name == "propose_action" and result.get("draft_payload"):
                        action_proposals.append(result)
                    elif tool_name == "get_task_status" and "tasks" in result:
                        task_statuses.append(result)
                    elif tool_name == "update_plan" and result.get("plan"):
                        plan = result["plan"]  # 整体覆盖，末次为准

                # F4：把工具真实返回的结构化事实登记进账本（LLM 说的不入账）
                self._ledger_register(ledger, tool_name, result, is_error)

                step_status = "failed" if is_error else "succeeded"
                steps.append(
                    {"index": idx, "tool": tool_name, "arguments": call_args, "status": step_status, "summary": summary}
                )
                yield {"type": "step_done", "index": idx, "status": step_status, "summary": summary}

                # P2.2：超预算时按语义降级，不按字符砍——回灌的永远是合法 JSON
                result_text, _compacted = compact_tool_result(result, _TOOL_RESULT_MAX_CHARS)
                messages.append({"role": "tool", "tool_call_id": t.id, "content": result_text})

            if clarification is not None:
                break  # 澄清请求：跳出整个 agent 循环，不再作答
        else:
            # 步数耗尽仍未收敛：强制不带工具收尾（同样仅缓冲，稍后校验通过再流式）
            tel.llm_call()
            async for tok in self._stream_final_answer(
                client,
                runtime.model,
                messages,
                nudge="请基于以上工具结果直接给出最终回答，不要再调用工具。",
            ):
                answer += tok
            answer = self._strip_tool_markup(answer)

        if clarification is not None:
            # 澄清出口：不生成答案、不跑接地校验（没有断言可校验），直接把问题抛给用户。
            # 答案正文用澄清问题本身，前端无需改动即可显示；结构化字段供 UI 渲染选项。
            body = clarification["question"]
            if clarification["options"]:
                body += "\n\n" + "\n".join(f"- {o}" for o in clarification["options"])
            yield {
                "type": "done",
                "payload": {
                    "answer": body,
                    "clarification": clarification,
                    "suggested_sql": None,
                    "caliber_decomposition": [],
                    "referenced_objects": referenced_objects,
                    "referenced_logics": referenced_logics,
                    "steps": steps,
                    "data_result": None,
                    "_grounded": True,   # 反问不是拒答，不该被接地判定拦下
                    "_unverified": [],
                },
            }
            return

        # SQL 收割优先级：run_sql 实际提交的 > 口径编译产物 > 正文围栏块兜底。
        # 编译产物排在正文之前——它是本体确定性生成且已自证的，比模型正文里写的可信。
        # 结构性 turn（sql_allowed=False）不收割：正文里的示例 SQL 不得被提升成取数块，
        # 否则又是「答非所问地追加取数」。取数工具本已被门控拦下，这里是最后一道闸。
        if sql_allowed and not last_sql:
            last_sql = compiled_sql or self._extract_sql_from_text(answer)

        # F4：断言级可靠性校验。答案里出现账本外的具名实体/未证实数值 → 判不可靠。
        # 受 settings.agent_soundness 开关：off 跳过；warn 仅记录不拦；on 生效。
        verify_ok, unverified = self._verify_answer(answer, ledger, question)

        # P4.3 自愈回环：校验不过时先给模型**一次**重写机会，而不是直接拒答。
        # 此前 unverified 是终局判决——模型连「哪句没凭证」都收不到，
        # 而它往往只是多写了一个未检索的字段名，删掉即可成立。拒答的代价是整轮白跑。
        for _ in range(_AGENT_REPAIR_ATTEMPTS if not verify_ok else 0):
            tel.repair()
            yield {"type": "repair", "reasons": list(unverified)}
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": self._repair_instruction(unverified, ledger)})
            tel.llm_call()
            repaired = ""
            async for tok in self._stream_final_answer(client, runtime.model, messages):
                repaired += tok
            repaired = self._strip_tool_markup(repaired)
            if not repaired:
                break
            answer = repaired
            verify_ok, unverified = self._verify_answer(answer, ledger, question)
            if verify_ok:
                tel.repair_succeeded()
                break

        # 接地判定：只有需要精准回答的意图（analytical/structural）才要求命中本体工具。
        # 其余默认 general（打招呼/问能力/产品 how-to/一般解释）——与具体业务数据无关，
        # 直接作答即可，豁免 grounded_hit。拒答是少数精准场景的例外，不是常态。
        # 但仍**保留** verify_ok（F4）：万一 general 混进真本体问题、模型据此编造具名实体/数值，
        # 断言校验照样拦下。所以 general 只放开「没查本体」，不放开「乱说本体」。
        grounded = (grounded_hit or intent == "general") and verify_ok

        yield {
            "type": "done",
            "payload": {
                "answer": answer or "（模型未返回回答）",
                "suggested_sql": last_sql,
                "caliber_decomposition": self._steps_to_caliber(
                    steps, referenced_objects, referenced_logics, compiled=compiled_metrics
                ),
                "referenced_objects": referenced_objects,
                "referenced_logics": referenced_logics,
                "steps": steps,
                "data_result": data_result,
                "charts": charts,
                "analyses": analyses,
                "lineage": lineage,
                "draft_proposals": draft_proposals,
                "preference_proposals": preference_proposals,
                "action_proposals": action_proposals,
                "task_statuses": task_statuses,
                "plan": plan,
                "skill": active_skill.name if active_skill else None,
                "_grounded": grounded,
                "_unverified": unverified,
            },
        }

    async def _run_agent_loop(
        self,
        db: Session,
        *,
        runtime: Any,
        domain: DomainContext,
        ontology: Ontology,
        question: str,
        history: list[dict],
        resolver: "_ReferenceResolver",
        seed_objects: list[_ObjectSnapshot],
        seed_logics: list[BusinessLogic],
        principal_role: str | None = None,
        telemetry: RunTelemetry | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        """非流式包装：消费事件流、聚合 done.payload 返回（供 ask() agent 路径复用）。"""
        payload: dict | None = None
        async for ev in self._stream_agent_events(
            db,
            runtime=runtime,
            domain=domain,
            ontology=ontology,
            question=question,
            history=history,
            seed_objects=seed_objects,
            seed_logics=seed_logics,
            principal_role=principal_role,
            telemetry=telemetry,
            conversation_id=conversation_id,
        ):
            if ev["type"] == "done":
                payload = ev["payload"]
        return payload or {
            "answer": "（模型未返回回答）",
            "suggested_sql": None,
            "caliber_decomposition": [],
            "referenced_objects": [],
            "referenced_logics": [],
            "steps": [],
            "data_result": None,
            "_grounded": False,
        }

    # ------------------------------------------------------------------ mock

    def _mock_answer(
        self,
        *,
        question: str,
        snapshots: list[_ObjectSnapshot],
        relations: list[RelationType],
        logics: list[BusinessLogic],
        matched_objects: list[_ObjectSnapshot] | None = None,
        matched_logics: list[BusinessLogic] | None = None,
    ) -> dict:
        q_lower = question.lower()

        matched_objects = matched_objects if matched_objects is not None else self._match_objects(
            question, snapshots
        )
        if not matched_objects:
            # 调用方应已拦截无命中；此处兜底为空回答，避免回退到 snapshots[:1]
            return {
                "answer": "当前问题未能命中已发布本体中的对象或业务逻辑，无法给出基于本体的解读。",
                "suggested_sql": None,
                "caliber_decomposition": [],
                "referenced_objects": [],
                "referenced_logics": [],
            }
        primary = matched_objects[0]

        if matched_logics is None:
            matched_logics = self._match_logics(question, logics)
        matched_logics = matched_logics[:2]

        is_aggregation = any(k in q_lower for k in _AGG_KEYWORDS) or any(
            k in question for k in _AGG_KEYWORDS
        )
        time_window = self._detect_time_window(question)

        amount_prop = next(
            (p for p in primary.properties if "amount" in p.name or p.semantic_type == "amount"),
            None,
        )
        date_prop = next(
            (p for p in primary.properties if p.semantic_type == "date" or "date" in p.name),
            None,
        )

        # ---- answer text
        lines: list[str] = []
        lines.append(f"基于「{primary.display_name}」本体解读你的问题：")
        lines.append("")
        lines.append("**口径解读**")
        bullet_bits: list[str] = []
        bullet_bits.append(f"主对象：{primary.display_name}（`{primary.name}`）")
        if amount_prop:
            bullet_bits.append(f"度量字段：{amount_prop.display_name}（`{amount_prop.name}`）")
        if date_prop:
            bullet_bits.append(f"时间字段：{date_prop.display_name}（`{date_prop.name}`）")
        if time_window:
            bullet_bits.append(f"时间范围：{time_window}")
        if is_aggregation:
            bullet_bits.append("聚合方式：求和 / 计数")
        for b in bullet_bits:
            lines.append(f"- {b}")

        if matched_logics:
            lines.append("")
            lines.append("**关联业务逻辑**")
            for logic in matched_logics:
                summary = f" — {logic.expression_summary}" if logic.expression_summary else ""
                lines.append(f"- {logic.display_name}（`{logic.name}`）{summary}")
        elif logics:
            lines.append("")
            lines.append(
                f"> 当前本体共有 {len(logics)} 条业务逻辑，但未在问题中匹配到关键词，"
                "可在「业务逻辑」页确认口径。"
            )

        # ---- suggested SQL
        suggested_sql = self._build_mock_sql(
            primary=primary,
            amount_prop=amount_prop,
            date_prop=date_prop,
            is_aggregation=is_aggregation,
            time_window=time_window,
        )
        if suggested_sql:
            lines.append("")
            lines.append("**建议查询（基于本体语义，需映射到物理表后执行）**")
            lines.append("```sql")
            lines.append(suggested_sql)
            lines.append("```")

        lines.append("")
        lines.append(
            "_当前为 Mock 模式回答（未配置真实 LLM），可在「设置 → LLM 服务」中接入模型获得更智能的解读。_"
        )

        # ---- caliber decomposition
        caliber = self._build_mock_caliber(
            primary=primary,
            amount_prop=amount_prop,
            date_prop=date_prop,
            is_aggregation=is_aggregation,
            time_window=time_window,
            matched_objects=matched_objects,
            matched_logics=matched_logics,
        )

        return {
            "answer": "\n".join(lines),
            "suggested_sql": suggested_sql,
            "caliber_decomposition": caliber,
            "referenced_objects": [
                {
                    "id": o.id,
                    "name": o.name,
                    "display_name": o.display_name,
                }
                for o in matched_objects
            ],
            "referenced_logics": [
                {
                    "id": logic.id,
                    "name": logic.name,
                    "display_name": logic.display_name,
                }
                for logic in matched_logics
            ],
        }

    def _match_objects(
        self, question: str, snapshots: list[_ObjectSnapshot]
    ) -> list[_ObjectSnapshot]:
        tokens = self._tokens(question)
        if not tokens or not snapshots:
            return []
        scored: list[tuple[int, _ObjectSnapshot]] = []
        for o in snapshots:
            blob = f"{o.name} {o.display_name} {o.description or ''}".lower()
            score = sum(1 for t in tokens if t and t in blob)
            if score > 0:
                scored.append((score, o))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [o for _, o in scored[:3]]

    def _match_logics(
        self, question: str, logics: list[BusinessLogic]
    ) -> list[BusinessLogic]:
        tokens = self._tokens(question)
        if not tokens or not logics:
            return []
        matched = [
            logic
            for logic in logics
            if any(
                token
                and token
                in (logic.name + logic.display_name + (logic.description or "")).lower()
                for token in tokens
            )
        ]
        return matched[:3]

    @staticmethod
    def _ungrounded_refusal(
        *,
        domain_id: str,
        domain_name: str,
        ontology_id: str,
        question: str,
        reasons: list[str] | None = None,
    ) -> dict:
        q = (question or "").strip()
        preview = q if len(q) <= 80 else q[:80] + "…"
        if reasons:
            # F4：校验不通——拒答并逐条说明「哪句不可证」，而非笼统「无法回答」。
            bullet = "\n".join(f"  · {r}" for r in reasons)
            answer = (
                f"为避免给出不准确信息，未能基于「{domain_name}」已发布本体可靠回答该问题"
                + (f"（{preview}）" if preview else "")
                + "：以下结论无法由本体证实：\n"
                + bullet
                + "\n请换用本体中已有实体提问，或先补充/发布相关建模。"
            )
        else:
            answer = (
                f"无法基于「{domain_name}」已发布本体回答该问题"
                + (f"（{preview}）" if preview else "")
                + "：未检索到匹配的对象类型或业务逻辑。"
                "请换用本体中已有实体的名称提问，或先补充/发布相关建模。"
            )
        return {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "ontology_id": ontology_id,
            "answer": answer,
            "suggested_sql": None,
            "caliber_decomposition": [],
            "referenced_objects": [],
            "referenced_logics": [],
            "used_mock": True,
            "grounding_refused": True,
        }

    @staticmethod
    def _enforce_grounded_refs(
        payload: dict,
        *,
        grounded_objects: list[_ObjectSnapshot],
        grounded_logics: list[BusinessLogic],
        resolver: "_ReferenceResolver",
    ) -> dict:
        """过滤无法落地的引用；若 LLM 未给引用则回填检索命中。"""
        cleaned_objs = [
            ref
            for ref in payload.get("referenced_objects") or []
            if isinstance(ref, dict) and ref.get("id") in resolver.obj_by_id
        ]
        if not cleaned_objs and grounded_objects:
            cleaned_objs = [
                {"id": o.id, "name": o.name, "display_name": o.display_name}
                for o in grounded_objects
            ]

        cleaned_logics = [
            ref
            for ref in payload.get("referenced_logics") or []
            if isinstance(ref, dict) and ref.get("id") in resolver.logic_by_id
        ]
        if not cleaned_logics and grounded_logics:
            cleaned_logics = [
                {
                    "id": logic.id,
                    "name": logic.name,
                    "display_name": logic.display_name,
                }
                for logic in grounded_logics
            ]

        payload["referenced_objects"] = cleaned_objs
        payload["referenced_logics"] = cleaned_logics
        return payload

    @staticmethod
    def _tokens(text: str) -> list[str]:
        # 简单中英文分词：英文按非字母数字切，中文按字符切。
        if not text:
            return []
        text = text.lower()
        alpha = re.findall(r"[a-z_][a-z0-9_]+", text)
        cjk = re.findall(r"[\u4e00-\u9fa5]", text)
        return alpha + cjk

    @staticmethod
    def _detect_time_window(question: str) -> str | None:
        if "近 7 天" in question or "近7天" in question or "最近一周" in question or "近一周" in question:
            return "近 7 天"
        if "近 30 天" in question or "近30天" in question or "最近一个月" in question or "近一个月" in question:
            return "近 30 天"
        if "今日" in question or "今天" in question:
            return "今日"
        if "本月" in question:
            return "本月"
        if "上月" in question:
            return "上月"
        if "最近" in question or "近" in question:
            return "近 7 天"
        return None

    @staticmethod
    def _build_mock_sql(
        *,
        primary: _ObjectSnapshot,
        amount_prop: Property | None,
        date_prop: Property | None,
        is_aggregation: bool,
        time_window: str | None,
    ) -> str | None:
        if not primary:
            return None
        select_parts: list[str] = []
        group_parts: list[str] = []
        where_parts: list[str] = []

        if date_prop:
            group_parts.append(date_prop.name)

        if is_aggregation:
            if amount_prop:
                select_parts.append(f"SUM({amount_prop.name}) AS total_{amount_prop.name}")
            select_parts.append(f"COUNT(*) AS record_count")
        else:
            select_parts.append(f"{primary.name}_id")
            for p in primary.properties[:4]:
                if p.name and p.name != f"{primary.name}_id":
                    select_parts.append(p.name)

        if date_prop and time_window:
            where_parts.append(f"{date_prop.name} >= DATE_SUB(CURDATE(), INTERVAL _N DAY)")
        # 替换占位 _N
        days_map = {
            "近 7 天": "7",
            "近 30 天": "30",
            "今日": "0",
            "本月": "0",
            "上月": "30",
        }
        days = days_map.get(time_window or "", "7") if time_window else None

        select_clause = ", ".join(select_parts) if select_parts else "*"
        sql_lines = [f"SELECT {select_clause}", f"FROM {primary.name}"]
        if group_parts and is_aggregation:
            sql_lines.append(f"GROUP BY {', '.join(group_parts)}")
        if where_parts:
            clause = "; ".join(where_parts)
            if days is not None:
                clause = clause.replace("_N", days)
            sql_lines.append(f"WHERE {clause}")
        sql_lines.append("LIMIT 100;")
        return "\n".join(sql_lines)

    def _build_mock_caliber(
        self,
        *,
        primary: _ObjectSnapshot,
        amount_prop: Property | None,
        date_prop: Property | None,
        is_aggregation: bool,
        time_window: str | None,
        matched_objects: list[_ObjectSnapshot],
        matched_logics: list[BusinessLogic],
    ) -> list[dict]:
        items: list[dict] = []

        items.append(
            {
                "label": "主对象",
                "description": f"查询主体为「{primary.display_name}」",
                "references": [
                    {
                        "kind": "object_type",
                        "id": primary.id,
                        "name": primary.name,
                        "display_name": primary.display_name,
                    }
                ],
            }
        )

        if amount_prop:
            items.append(
                {
                    "label": "度量字段",
                    "description": f"对「{amount_prop.display_name}」进行聚合",
                    "references": [
                        {
                            "kind": "property",
                            "id": amount_prop.id,
                            "name": amount_prop.name,
                            "display_name": amount_prop.display_name,
                        }
                    ],
                }
            )

        if date_prop:
            items.append(
                {
                    "label": "时间维度",
                    "description": f"按「{date_prop.display_name}」筛选时间范围",
                    "references": [
                        {
                            "kind": "property",
                            "id": date_prop.id,
                            "name": date_prop.name,
                            "display_name": date_prop.display_name,
                        }
                    ],
                }
            )

        if time_window:
            items.append(
                {
                    "label": "时间范围",
                    "description": f"统计窗口：{time_window}",
                    "references": [],
                }
            )

        if is_aggregation:
            items.append(
                {
                    "label": "聚合方式",
                    "description": "按主对象记录求和 / 计数",
                    "references": [],
                }
            )

        if matched_logics:
            items.append(
                {
                    "label": "关联业务逻辑",
                    "description": "回答依据以下业务逻辑口径",
                    "references": [
                        {
                            "kind": "business_logic",
                            "id": logic.id,
                            "name": logic.name,
                            "display_name": logic.display_name,
                        }
                        for logic in matched_logics
                    ],
                }
            )

        # 关联对象（除主对象外的命中对象）
        extra_objects = [o for o in matched_objects if o.id != primary.id]
        if extra_objects:
            items.append(
                {
                    "label": "关联对象",
                    "description": "问题中提到的其它业务对象",
                    "references": [
                        {
                            "kind": "object_type",
                            "id": o.id,
                            "name": o.name,
                            "display_name": o.display_name,
                        }
                        for o in extra_objects
                    ],
                }
            )

        return items



    # ---------- M4：把 suggested_sql 真正执行掉 ----------

    def execute_message_sql(
        self, db: Session, message_id: str, *, data_source_id: str, limit: int = 100
    ) -> dict:
        """执行某条回答的 ``suggested_sql``。

        在本体驱动的数仓里，这一步的准确性是架构保证的而非提示词保证的：
        物理表由本体生成，表名/列名与本体标识符天然一致，
        ``_LLM`` 那句「必须严格使用本体标识符」从祈使句变成了物理事实。

        安全：复用 ``data_app_executor`` 既有的只读校验与强制 LIMIT。
        注：执行权限应限制为 publisher 角色，但 RBAC 四层角色尚未产品化
        （见 README「安全模型（阶段性）」），当前仍由共享 Admin Token 兜底。
        """
        from app.models import DataSource
        from app.services import data_app_executor

        message = db.get(ChatBiMessage, message_id)
        if message is None:
            raise ValueError("消息不存在")
        payload = _loads_payload(message.payload)
        sql = (payload.get("suggested_sql") or "").strip()
        if not sql:
            raise ValueError("该消息没有可执行的 SQL")

        source = db.get(DataSource, data_source_id)
        if source is None:
            raise ValueError("数据源不存在")
        dsn = (source.dsn_secret_ref or "").strip()
        if not dsn or source.kind == "mock":
            raise ValueError("该数据源未配置连接串，无法执行")

        mapping = _loads_payload(source.mapping_json)
        columns, rows = data_app_executor.execute_sql(
            dsn=dsn, sql=sql, limit=limit, mapping=mapping or None
        )
        return {
            "message_id": message_id,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }


class _ReferenceResolver:
    """将 LLM/Mock 输出中的 name/display_name 解析为真实实体 id，供前端跳转。

    LLM 经常返回伪造的 id（如 "payment"、""），因此一律以本体快照为准：
    优先按 name/display_name 命中真实实体后覆写 id；命中失败时保留原 id。
    """

    def __init__(
        self,
        *,
        objects: list[_ObjectSnapshot],
        relations: list[RelationType],
        logics: list[BusinessLogic],
    ) -> None:
        self.obj_by_key: dict[str, _ObjectSnapshot] = {}
        self.obj_by_id: dict[str, _ObjectSnapshot] = {}
        for o in objects:
            self.obj_by_id[o.id] = o
            for key in (o.name, o.display_name, o.name.lower(), o.display_name.lower()):
                if key:
                    self.obj_by_key.setdefault(key, o)
        self.logic_by_key: dict[str, BusinessLogic] = {}
        self.logic_by_id: dict[str, BusinessLogic] = {}
        for logic in logics:
            self.logic_by_id[logic.id] = logic
            for key in (logic.name, logic.display_name, logic.name.lower(), logic.display_name.lower()):
                if key:
                    self.logic_by_key.setdefault(key, logic)
        self.rel_by_key: dict[str, RelationType] = {}
        for rel in relations:
            for key in (rel.name, rel.display_name, rel.name.lower(), rel.display_name.lower()):
                if key:
                    self.rel_by_key.setdefault(key, rel)
        # property: (object_id, property_name) -> Property
        self.prop_by_obj_and_name: dict[tuple[str, str], Property] = {}
        self.prop_by_name: dict[str, Property] = {}
        for o in objects:
            for p in o.properties:
                self.prop_by_obj_and_name.setdefault((o.id, p.name.lower()), p)
                self.prop_by_name.setdefault(p.name.lower(), p)
                self.prop_by_name.setdefault(p.display_name.lower(), p)

    def resolve_payload(self, payload: dict) -> dict:
        payload["referenced_objects"] = [
            r
            for r in (
                self._resolve_obj(ref) for ref in payload.get("referenced_objects") or []
            )
            if r and r.get("id") in self.obj_by_id
        ]
        payload["referenced_logics"] = [
            r
            for r in (
                self._resolve_logic(ref)
                for ref in payload.get("referenced_logics") or []
            )
            if r and r.get("id") in self.logic_by_id
        ]
        payload["caliber_decomposition"] = [
            self._resolve_caliber_item(item)
            for item in payload.get("caliber_decomposition") or []
        ]
        return payload

    def _resolve_obj(self, ref: dict) -> dict:
        ref = dict(ref)
        snap = self._find(self.obj_by_key, ref)
        if snap:
            ref["id"] = snap.id
            ref.setdefault("name", snap.name)
            ref.setdefault("display_name", snap.display_name)
        return ref

    def _resolve_logic(self, ref: dict) -> dict:
        ref = dict(ref)
        logic = self._find(self.logic_by_key, ref)
        if logic:
            ref["id"] = logic.id
            ref.setdefault("name", logic.name)
            ref.setdefault("display_name", logic.display_name)
        return ref

    def _resolve_caliber_item(self, item: dict) -> dict:
        item = dict(item)
        refs = item.get("references") or []
        resolved: list[dict] = []
        for r in refs:
            r = dict(r)
            kind = r.get("kind") or "object_type"
            if kind == "object_type":
                resolved.append(self._resolve_obj(r))
            elif kind == "business_logic":
                resolved.append(self._resolve_logic(r))
            elif kind == "relation_type":
                rel = self._find(self.rel_by_key, r)
                if rel:
                    r["id"] = rel.id
                    r.setdefault("name", rel.name)
                    r.setdefault("display_name", rel.display_name)
                resolved.append(r)
            elif kind == "property":
                prop = self._find(self.prop_by_name, r)
                if prop:
                    r["id"] = prop.id
                    r.setdefault("name", prop.name)
                    r.setdefault("display_name", prop.display_name)
                resolved.append(r)
            else:
                resolved.append(r)
        item["references"] = resolved
        return item

    @staticmethod
    def _find(index: dict, ref: dict):
        if not ref:
            return None
        for key in (ref.get("name"), ref.get("display_name"), ref.get("id")):
            if not key:
                continue
            hit = index.get(key) or index.get(str(key).lower())
            if hit:
                return hit
        return None


def _loads_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
