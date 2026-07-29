"""引擎能力矩阵（Capability Matrix）。

这是「完整适配层」与「写几个 if-else」的分界线：每个 Adapter 必须**显式声明**
自己支持什么，渲染前据此校验——**本体定义了但目标引擎表达不了的东西必须报错，
绝不静默降级**。

``verified=False`` 表示该引擎的能力条目尚未逐项核实，会产生 warning 级缺口，
提醒实施前验证；这样「没核实」本身也是机器可见的，而不是藏在注释里。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.warehouse.logical_schema import LogicalTable


class ConstraintSupport(str, enum.Enum):
    """约束的表达方式。

    区分 ENFORCED 与 DECLARATIVE 很重要：Hive 没有真外键，但可以把外键写进
    TBLPROPERTIES 与列注释——那是**有记录的声明**，不是静默丢失。
    """

    ENFORCED = "enforced"
    DECLARATIVE = "declarative"
    NONE = "none"


class GapSeverity(str, enum.Enum):
    ERROR = "error"  # 结构性丢失，拒绝渲染
    WARNING = "warning"  # 有损但可用，必须显式呈现


@dataclass(frozen=True)
class CapabilityGap:
    feature: str
    detail: str
    severity: GapSeverity = GapSeverity.ERROR

    @property
    def is_error(self) -> bool:
        return self.severity is GapSeverity.ERROR


@dataclass(frozen=True)
class Capabilities:
    engine: str
    primary_key: ConstraintSupport
    foreign_key: ConstraintSupport
    # none / unique / aggregate / duplicate —— OLAP 引擎的主键模型差异很大
    primary_key_model: str = "none"
    supports_table_comment: bool = True
    supports_column_comment: bool = True
    supports_partition: bool = True
    supports_bucketing: bool = False
    supports_scd2_merge: bool = False
    supports_alter_add_column: bool = True
    supports_alter_drop_column: bool = False
    supports_alter_rename_column: bool = False
    max_identifier_length: int = 128
    # 该能力矩阵是否已逐项核实。False → 产生 warning 缺口。
    verified: bool = False


class CapabilityError(RuntimeError):
    """目标引擎无法表达本体所要求的结构。"""

    def __init__(self, engine: str, gaps: list[CapabilityGap]):
        self.engine = engine
        self.gaps = gaps
        detail = "；".join(f"{g.feature}: {g.detail}" for g in gaps)
        super().__init__(f"引擎 {engine} 无法表达该表结构 → {detail}")


def check_table(table: LogicalTable, caps: Capabilities) -> list[CapabilityGap]:
    """校验一张逻辑表能否被目标引擎完整表达。

    error 级会阻断渲染；warning 级必须被上层呈现给人，不得吞掉。
    """
    gaps: list[CapabilityGap] = []

    if table.scd_type == "scd2" and not caps.supports_scd2_merge:
        gaps.append(
            CapabilityGap(
                "scd2",
                f"物化契约要求 SCD2 拉链，但 {caps.engine} 当前配置不支持 MERGE",
            )
        )

    if table.partition_key and not caps.supports_partition:
        gaps.append(
            CapabilityGap(
                "partition",
                f"物化契约指定分区键 {table.partition_key}，但 {caps.engine} 不支持分区",
            )
        )

    if table.partition_key and table.column(table.partition_key) is None:
        gaps.append(
            CapabilityGap(
                "partition",
                f"分区键 {table.partition_key} 不在列清单中",
            )
        )

    if table.primary_key and caps.primary_key is ConstraintSupport.NONE:
        gaps.append(
            CapabilityGap(
                "primary_key",
                f"本体声明了主键，但 {caps.engine} 无法表达",
                GapSeverity.WARNING,
            )
        )

    if table.foreign_keys and caps.foreign_key is ConstraintSupport.NONE:
        gaps.append(
            CapabilityGap(
                "foreign_key",
                f"本体声明了 {len(table.foreign_keys)} 个外键，但 {caps.engine} 无法表达（连声明式也不支持）",
                GapSeverity.WARNING,
            )
        )

    if table.comment and not caps.supports_table_comment:
        gaps.append(
            CapabilityGap(
                "table_comment",
                f"{caps.engine} 不支持表注释，本体的业务语义无法落到物理层",
                GapSeverity.WARNING,
            )
        )

    if not caps.supports_column_comment and any(c.comment for c in table.columns):
        gaps.append(
            CapabilityGap(
                "column_comment",
                f"{caps.engine} 不支持列注释，字段业务含义无法落到物理层",
                GapSeverity.WARNING,
            )
        )

    over_long = [
        n
        for n in (table.name, *(c.name for c in table.columns))
        if len(n) > caps.max_identifier_length
    ]
    if over_long:
        gaps.append(
            CapabilityGap(
                "identifier_length",
                f"标识符超出 {caps.engine} 上限 {caps.max_identifier_length}：{', '.join(over_long)}",
            )
        )

    if not caps.verified:
        gaps.append(
            CapabilityGap(
                "unverified_capabilities",
                f"{caps.engine} 的能力矩阵尚未逐项核实，需实施前验证",
                GapSeverity.WARNING,
            )
        )

    return gaps
