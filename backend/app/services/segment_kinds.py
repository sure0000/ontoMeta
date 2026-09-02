"""板块种类：业务地图的划分必须是**全覆盖分区**——每个对象恰好属于一个板块。

背景（实测 erpnext 本体 1035 对象，890 个未接入，86%）：
「未接入」曾经是个隐式垃圾桶，把四种完全不同的情况混在一起，谁也不知道该怎么办。
拆开之后每一类都有明确的处置方式：

====================  ==========================================  ============
kind                  是什么                                       怎么变少
====================  ==========================================  ============
business              聚类得出的业务模块                            —
shared                公共主数据（枢纽对象，处处被引用）              —
pending               判为业务对象/桥表，但连不成簇（零边或单点）      补关系推断
technical             框架管道表（判为 technical，不参与业务聚类）     人工复核角色
system                数据库自带 schema，压根不是业务数据             收窄摄取范围
====================  ==========================================  ============

只有 ``business`` 的名字来自 LLM；其余四类名字固定，不进命名流程
（它们不是业务子域，硬给业务名反而误导）。
"""

from __future__ import annotations

SEGMENT_KIND_BUSINESS = "business"
SEGMENT_KIND_SHARED = "shared"
SEGMENT_KIND_PENDING = "pending"
SEGMENT_KIND_TECHNICAL = "technical"
SEGMENT_KIND_SYSTEM = "system"

#: 非业务板块在目录里的固定顺序（业务板块永远排在它们之前）
FALLBACK_KIND_ORDER = (
    SEGMENT_KIND_SHARED,
    SEGMENT_KIND_PENDING,
    SEGMENT_KIND_TECHNICAL,
    SEGMENT_KIND_SYSTEM,
)

#: 兜底板块的固定身份。name 用双下划线包起来，与 LLM 生成的业务板块标识名不可能撞。
FALLBACK_SEGMENT_META: dict[str, dict[str, str]] = {
    SEGMENT_KIND_SHARED: {
        "name": "__shared_master_data__",
        "display_name": "公共主数据",
        "description": (
            "被多个业务模块共同引用的枢纽对象（公司、客户、商品这类）。"
            "它们刻意不并入任何单个模块——并进去会把大半张图粘成一块。"
        ),
    },
    SEGMENT_KIND_PENDING: {
        "name": "__pending_objects__",
        "display_name": "待归类业务对象",
        "description": (
            "判定为业务对象或桥表，但在关系图上连不成簇：要么一条关系都没有，"
            "要么只跟自己成一个单点簇。补齐关系推断后它们会自动归入业务模块。"
        ),
    },
    SEGMENT_KIND_TECHNICAL: {
        "name": "__technical_tables__",
        "display_name": "技术表",
        "description": (
            "判定为技术表的框架管道表（表单配置、队列、路由历史等），不参与业务聚类。"
            "判定多为机器给出、仍待复核，复核改判后会自动归入业务模块。"
        ),
    },
    SEGMENT_KIND_SYSTEM: {
        "name": "__system_tables__",
        "display_name": "系统表",
        "description": (
            "来自数据库自带 schema（mysql / information_schema / performance_schema / sys 等），"
            "不是业务数据。根治办法是收窄摄取范围，让它们一开始就不进本体。"
        ),
    },
}

#: 数据库自带 schema：这些库里的表不是业务数据。小写比较。
SYSTEM_SCHEMAS = frozenset(
    {
        # MySQL / MariaDB
        "mysql",
        "sys",
        "information_schema",
        "performance_schema",
        # PostgreSQL
        "pg_catalog",
        "pg_toast",
        "pg_temp_1",
        # SQL Server
        "sys",
        "guest",
        # Oracle
        "sysaux",
    }
)


def schema_of_source_ref(source_ref: str | None) -> str | None:
    """从 DataHub dataset URN 里取出 schema 名。

    URN 形如 ``urn:li:dataset:(urn:li:dataPlatform:mysql,mysql.column_stats,PROD)``，
    中间那段是 ``<schema>.<table>``（也可能是 ``<catalog>.<schema>.<table>``）。
    取不出来返回 ``None``——拿不准就不当系统表，宁可漏判不可误杀业务对象。
    """
    if not source_ref:
        return None
    start = source_ref.find("(")
    end = source_ref.rfind(")")
    body = source_ref[start + 1 : end] if start != -1 and end > start else source_ref
    parts = [p.strip() for p in body.split(",")]
    # 平台段在前、环境段在后，中间那段才是数据集路径
    dataset_path = parts[1] if len(parts) >= 2 else parts[0]
    segments = [s for s in dataset_path.split(".") if s]
    if len(segments) < 2:
        return None
    # 倒数第二段是 schema（无论前面有没有 catalog）
    return segments[-2].lower()


def is_system_table(source_ref: str | None) -> bool:
    """该对象是否来自数据库自带 schema。"""
    schema = schema_of_source_ref(source_ref)
    return schema is not None and schema in SYSTEM_SCHEMAS
