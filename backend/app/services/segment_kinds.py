"""板块种类：业务地图的划分必须是**全覆盖分区**——每个对象恰好属于一个板块。

只有三类，判定顺序就是一句话：**是不是业务对象或业务关系表？是就一定落在某个业务
板块下；不是就落系统表。** 中间地带一个都不留——曾经的「待归类业务对象」是个隐式
垃圾桶（实测 erpnext 547 个对象压在里面），既进不了业务地图，也没人知道该拿它怎么办。

====================  ==========================================  ============
kind                  是什么                                       怎么变少
====================  ==========================================  ============
business              聚类得出的业务模块                            —
shared                公共主数据（枢纽对象，处处被引用）              —
system                不是业务对象/关系表的一切：框架管道表、数据库
                      自带 schema，以及归不进任何业务模块的表         人工移板块 / 收窄摄取范围
====================  ==========================================  ============

``shared`` 也是业务板块的一种（枢纽对象都是业务对象），只是刻意不并进任何单个模块
——并进去会把大半张图粘成一块。

只有 ``business`` 的名字来自 LLM；另两类名字固定，不进命名流程
（它们不是业务子域，硬给业务名反而误导）。
"""

from __future__ import annotations

SEGMENT_KIND_BUSINESS = "business"
SEGMENT_KIND_SHARED = "shared"
SEGMENT_KIND_SYSTEM = "system"

#: 非业务模块板块在目录里的固定顺序（业务板块永远排在它们之前）
FALLBACK_KIND_ORDER = (
    SEGMENT_KIND_SHARED,
    SEGMENT_KIND_SYSTEM,
)

#: 已废弃的板块种类。存量库里还躺着这两种板块，回填脚本按它们找旧板块并搬空。
#: 判定链路一律不认这两个值——留常量只为迁移与兼容读。
LEGACY_SEGMENT_KIND_PENDING = "pending"
LEGACY_SEGMENT_KIND_TECHNICAL = "technical"
LEGACY_KINDS = (LEGACY_SEGMENT_KIND_PENDING, LEGACY_SEGMENT_KIND_TECHNICAL)

#: 会被归进业务板块的角色。别的角色说明它压根不是业务数据，归宿是「系统表」。
#: 判定只在这里写一次，落位、回填、队列共用。
BUSINESS_ROLES = frozenset({"business_object", "bridge"})

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
    SEGMENT_KIND_SYSTEM: {
        "name": "__system_tables__",
        "display_name": "系统表",
        "description": (
            "不是业务对象、也不是业务关系表的一切：框架管道表（表单配置、队列、"
            "路由历史等）、数据库自带 schema（mysql / information_schema 等），"
            "以及判成了业务对象却归不进任何业务模块的表。"
            "分错的可以直接移动到对应的业务板块。"
        ),
    },
}


def is_business_role(table_role: str | None) -> bool:
    """这个角色的对象该不该落在业务板块下。"""
    return (table_role or "") in BUSINESS_ROLES


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
