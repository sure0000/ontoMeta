"""Doris ODS 物理表的确定性命名。

同步目标表不是用户或 Agent 可配置项。唯一规则：

    ods_{数据域}_{原始表名}

其中数据域来自本体所属 ``DomainContext.name``，原始表名来自对象 ``source_ref`` 所指向
物理表的最后一段。所有写侧入口必须调用本模块，避免 Drafter、接入契约和 Projection 各自
拼出不同名字。
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy.orm import Session

from app.models import DomainContext, ObjectType, Ontology
from app.services.source_ref import source_table_of


_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


class OdsNamingError(ValueError):
    """无法从本体事实确定 ODS 物理表名。"""


def _identifier_part(value: str) -> str:
    """把名称归一为小写 snake_case 标识符片段。"""
    ascii_value = unicodedata.normalize("NFKD", value or "").encode(
        "ascii", "ignore"
    ).decode("ascii")
    snake = _CAMEL_BOUNDARY.sub("_", ascii_value)
    return _NON_IDENTIFIER.sub("_", snake.lower()).strip("_")


def _domain_code(domain: DomainContext) -> str:
    """取可用于物理表名的数据域代码。

    优先使用人可读的域名；域名完全不含 ASCII 时，退到 DataHub 域标识末段。仍无法形成
    标识符时明确报错，而不是生成一个不可追溯的随机名字。
    """
    code = _identifier_part(domain.name)
    if not code:
        code = _identifier_part((domain.datahub_domain_id or "").rsplit(":", 1)[-1])
    if not code:
        raise OdsNamingError(f"数据域「{domain.name}」无法生成 ODS 命名代码")
    # snake_case 标识符不能以数字开头；加 d_ 后仍可稳定追溯到域标识。
    return f"d_{code}" if code[0].isdigit() else code


def target_ods_table_name(db: Session, ontology_id: str, object_type: ObjectType) -> str:
    """按 ``ods_{数据域}_{原始表名}`` 返回对象的固定 ODS 表名。"""
    ontology = db.get(Ontology, ontology_id)
    if ontology is None:
        raise OdsNamingError("本体不存在，无法生成 ODS 表名")
    if object_type.ontology_id != ontology.id:
        raise OdsNamingError("对象不属于当前本体，无法生成 ODS 表名")
    domain = db.get(DomainContext, ontology.domain_context_id)
    if domain is None:
        raise OdsNamingError("本体未关联数据域，无法生成 ODS 表名")
    source_table = source_table_of(object_type.source_ref)
    if not source_table:
        raise OdsNamingError(f"对象「{object_type.name}」没有可定位的原始表")
    original_table = _identifier_part(source_table.rsplit(".", 1)[-1])
    if not original_table:
        raise OdsNamingError(f"对象「{object_type.name}」的原始表名无法用于 ODS 命名")
    return f"ods_{_domain_code(domain)}_{original_table}"


__all__ = ["OdsNamingError", "target_ods_table_name"]
