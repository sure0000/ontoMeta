"""规约 linter：把规约条款作用到具体载体上，产出带**可照做修法**的违规项。

一处定义、三处复用：
- ``lint_spec``          —— Spec 层（agent 自检 / Validation Gate 调，见 agents/validation）
- ``lint_logical_table`` —— 生成期物理表（warehouse_generator 在真实物理名上校验）
- ``lint_against_standard`` —— agent 工具入口（返回可 JSON 化的违规+修法）

每个 ``Violation`` 带 ``fix``：延续 materialize_preflight「每项失败都给下一步」的哲学——
让 agent 能自己改对，而不是只被拒。severity 由规约条款决定，本模块不自持 error/warning。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.governance.standard import GovernanceStandard, Severity, active_standard
from app.warehouse.logical_schema import LogicalTable

# snake_case 标识符：小写字母打头，含小写/数字/下划线。规约唯一的命名正则住这里。
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Violation:
    code: str
    severity: Severity
    message: str
    fix: str  # 可照做的下一步
    entity_type: str = "artifact"
    entity_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _snake_hint(ident: str) -> str:
    """把一个非 snake 标识符转成建议名，仅供 fix 提示（不改数据）。"""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", ident)  # camelCase → camel_Case
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).lower().strip("_")
    return s or "table_name"


def _lint_identifier(
    ident: str, display: str, standard: GovernanceStandard
) -> list[Violation]:
    """校验一个物理标识符（表/列名段）的命名。"""
    out: list[Violation] = []
    naming = standard.naming

    snake = standard.rule("naming_snake_case")
    if snake and not _SNAKE_RE.match(ident):
        out.append(
            Violation(
                code=snake.code,
                severity=snake.severity,
                message=f"{display} 不符合 {naming.identifier_case} 约定",
                fix=f"改为 snake_case（小写+下划线），如 {_snake_hint(ident)}",
                entity_type="table",
                entity_name=display,
            )
        )

    reserved = standard.rule("naming_reserved_word")
    if reserved and ident.lower() in {w.lower() for w in naming.reserved_words}:
        out.append(
            Violation(
                code=reserved.code,
                severity=reserved.severity,
                message=f"{display} 使用了 SQL 保留字 {ident}",
                fix="换一个非保留字表名，或加层前缀（如 dim_/dwd_）",
                entity_type="table",
                entity_name=display,
            )
        )
    return out


def lint_spec(
    kind: str, spec: dict, standard: GovernanceStandard
) -> list[Violation]:
    """Spec 层可查的规约条款（G2：命名）。

    多数条款（落层、comment/owner/pk、类型）要到物理表才有载体，归 lint_logical_table；
    这里只查 Spec 里已成形的物理标识符（如 ``target_table``）。kind 暂未细分，留作扩展位。
    """
    target = spec.get("target_table")
    if not isinstance(target, str) or not target.strip():
        return []
    ident = target.strip().split(".")[-1]  # 取「库.表」的表名段
    return _lint_identifier(ident, target.strip(), standard)


def lint_logical_table(
    table: LogicalTable, standard: GovernanceStandard
) -> list[Violation]:
    """生成期物理表的规约条款：命名（真实物理名）+ 元数据 advisory。"""
    out = _lint_identifier(table.name, table.qualified_name, standard)
    req = standard.required_metadata

    if req.table_comment_required:
        rule = standard.rule("table_comment_missing")
        if rule and not (table.comment and table.comment.strip()):
            out.append(
                Violation(
                    code=rule.code,
                    severity=rule.severity,
                    message=f"{table.qualified_name} 缺 comment",
                    fix="由本体 display_name/description 反补表注释",
                    entity_type="table",
                    entity_name=table.qualified_name,
                )
            )

    if req.primary_key_required:
        rule = standard.rule("primary_key_missing")
        has_pk = any(c.kind == "primary_key" for c in table.constraints)
        if rule and not has_pk:
            out.append(
                Violation(
                    code=rule.code,
                    severity=rule.severity,
                    message=f"{table.qualified_name} 无主键",
                    fix="在本体上声明可识别的身份属性，或带理由豁免主键",
                    entity_type="table",
                    entity_name=table.qualified_name,
                )
            )
    return out


def lint_against_standard(kind: str, spec: dict, db=None) -> list[dict]:
    """agent 工具入口：对一个 Spec 自检，返回可 JSON 化的违规+修法。

    建数 skill（V3 S3）把它做成 ``lint_against_standard`` 工具——让 agent 提 draft_proposal
    **前**自己 lint、自己改，而非等 Validation Gate 打回（引导降轮次，闸门仍是兜底）。
    """
    return [v.to_dict() for v in lint_spec(kind, spec, active_standard(db))]
