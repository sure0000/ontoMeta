"""代码包里**真实执行过的 JOIN**——把它接进本体证据。

**为什么单独有这个模块**：``sql_lineage_extractor`` 从客户代码包里解析出的关联键
（``a.x = b.y``）落在 ``LineagePackageEdge.join_key``，``lineage_package`` 的模块注释写着
「它是给关系推断用的证据」——但全仓查下来，这一列的读者只有 API 出参和 MCP 工具，
``evidence_builder`` / ``draft_generator`` / ``object_classifier`` **一个都没读过**。
系统里置信度最高的关联证据（被真实 SQL 执行过，不是推的）采集完就烂在库里。

本模块就是那条缺失的消费路径：读出来、解析成结构、交给 ``EvidenceBuilder``。

**证据强度**：观察到的 JOIN 比命名约定推断（源画像，0.6）强，但比数据库自己声明的外键
（0.8）弱——SQL 里 JOIN 得上不等于存在参照完整性约束，也可能是一次性的报表拼接。
故取 :data:`OBSERVED_JOIN_CONFIDENCE` = 0.75。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LineagePackage, LineagePackageEdge

logger = logging.getLogger("ontometa.observed_joins")

#: 观察到的 JOIN 作为外键证据的置信度：高于源画像命名推断(0.6)、低于声明式外键(0.8)。
OBSERVED_JOIN_CONFIDENCE = 0.75


#: 人工确认的智能关系补充候选，作为外键证据的置信度。
#: 与观察到的 JOIN 同档：一个是机器见过它被执行，一个是人看着判据点了确认。
CONFIRMED_INFERENCE_CONFIDENCE = 0.75

#: 代码包 DDL 里**声明**的外键。全场最高一档，与 DataHub 声明式外键同价——
#: 库自己写下的参照完整性约束，比「见过它被 JOIN 过」硬。
DDL_FOREIGN_KEY_CONFIDENCE = 0.8

ORIGIN_OBSERVED_JOIN = "observed_join"
ORIGIN_CONFIRMED_INFERENCE = "confirmed_inference"
ORIGIN_DDL_FOREIGN_KEY = "ddl_foreign_key"


@dataclass(frozen=True)
class ObservedJoin:
    """一条「这两列会关联」的证据：``left_table.left_column = right_table.right_column``。

    表名保持来源里的原样（可能带库名前缀），由消费方按 DataHub 里的表名去匹配——
    对不上就丢，不猜（与 ``lineage_inventory.resolve`` 同一条口径）。

    ``origin`` 必须一路带到关系描述里：代码包里真实跑过的 JOIN 与人工确认的智能推断
    是两种不同强度的证据，混成一句话说就等于抹掉了来源——复核的人分不清「机器见过」
    和「机器猜的、我点了确认」。
    """

    left_table: str
    left_column: str
    right_table: str
    right_column: str
    source_file: str
    origin: str = ORIGIN_OBSERVED_JOIN
    confidence: float = OBSERVED_JOIN_CONFIDENCE
    #: 方向是否已经确定（左=引用端，右=被引用端）。
    #: 等值谓词是无向的（``a.x = b.y`` 与反过来一个意思），方向要按区分度猜；
    #: **DDL 外键是有向的**——``REFERENCES`` 白纸黑字写着谁引用谁，不该再拿统计去二猜。
    #: 空表、profiling 缺失时统计会给出相反的答案，那是纯粹的倒退。
    directed: bool = False


def bare_table(table: str) -> str:
    """``db.table`` → ``table``。DataHub 里的表名带不带库名不固定，两种都要能对上。"""
    return table.rsplit(".", 1)[-1] if "." in table else table


def parse_join_key(text: str | None) -> tuple[str, str, str, str] | None:
    """``a.b.c = d.e`` → ``(a.b, c, d, e)``。解析不出来返回 None——丢掉，不猜。

    表名本身可能带库名（``erp_db.orders.customer_id``），所以是**按最后一个点**切列名，
    不是按第一个点切表名。
    """
    if not text or "=" not in text:
        return None
    left, _, right = text.partition("=")
    parsed: list[str] = []
    for side in (left, right):
        ref = side.strip().strip("`\"'")
        if "." not in ref:
            return None
        table, _, column = ref.rpartition(".")
        table, column = table.strip(), column.strip()
        if not table or not column:
            return None
        parsed.extend((table, column))
    left_table, left_column, right_table, right_column = parsed
    if left_table == right_table:
        # 同表两列相等是过滤条件，不是关联。
        return None
    return left_table, left_column, right_table, right_column


def load_for_domain(db: Session, domain_id: str) -> list[ObservedJoin]:
    """读该域所有代码包里解析出的关联键，去重后返回。

    **不按 ``state`` 过滤**：``blocked``/``skipped`` 说的是「表名没对上 DataHub URN」，
    而这里是按**表名**去匹配 bundle 的，对不对得上 URN 由消费方判断，在这里预先筛掉
    只会白丢证据。
    """
    rows = db.execute(
        select(
            LineagePackageEdge.join_key,
            LineagePackageEdge.source_file,
            LineagePackageEdge.kind,
        )
        .join(LineagePackage, LineagePackage.id == LineagePackageEdge.package_id)
        .where(
            LineagePackage.domain_context_id == domain_id,
            LineagePackageEdge.join_key.is_not(None),
        )
    ).all()

    seen: set[tuple[str, str, str, str]] = set()
    joins: list[ObservedJoin] = []
    # DDL 外键先落座：同一对列既被 JOIN 过又有声明式外键时，留证据更硬的那条。
    for join_key, source_file, kind in sorted(rows, key=lambda r: r[2] != "relation"):
        parsed = parse_join_key(join_key)
        if parsed is None:
            continue
        # 无向去重：``a.x = b.y`` 与 ``b.y = a.x`` 是同一条证据。
        signature = min(parsed, parsed[2:] + parsed[:2])
        if signature in seen:
            continue
        seen.add(signature)
        declared = kind == "relation"
        joins.append(
            ObservedJoin(
                *parsed,
                source_file=source_file or "代码包",
                origin=ORIGIN_DDL_FOREIGN_KEY if declared else ORIGIN_OBSERVED_JOIN,
                confidence=(
                    DDL_FOREIGN_KEY_CONFIDENCE
                    if declared
                    else OBSERVED_JOIN_CONFIDENCE
                ),
                directed=declared,
            )
        )
    return joins


def load_relation_evidence(db: Session, domain_id: str) -> list[ObservedJoin]:
    """本域全部「这两列会关联」的证据：代码包里的 JOIN **加上**人工确认的智能推断候选。

    这是 ``evidence_builder`` 该拿的那份——两个来源的 origin 不同，描述与置信度分开渲染。
    局部 import 是为了避开 ``relation_inference`` → ``observed_joins`` 的反向依赖。
    """
    from app.services import relation_inference

    return load_for_domain(db, domain_id) + relation_inference.confirmed_joins(
        db, domain_id
    )


def load(domain_id: str) -> list[ObservedJoin]:
    """:func:`load_relation_evidence` 的自开会话版本，供拿不到 session 的调用点
    （草稿生成子进程）使用。

    **读不出来就当没有**：关联证据是加分项，不该让证据组装因为它挂掉。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return load_relation_evidence(db, domain_id)
    except Exception:
        logger.warning("读取域 %s 的关联证据失败（当作没有）", domain_id, exc_info=True)
        return []
    finally:
        db.close()


def evidence_fingerprint(domain_id: str, datahub_domain_id: str | None) -> str:
    """evidence 磁盘缓存的 fingerprint：datahub 域 id + 本域关联键摘要。

    两个写入点（草稿生成、未建模清单）必须用同一个函数算，否则各写各的 fingerprint，
    缓存永远互不命中。
    """
    from app.database import SessionLocal

    base = datahub_domain_id or "none"
    db = SessionLocal()
    try:
        return f"{base}:{digest(db, domain_id)}"
    except Exception:
        logger.warning(
            "计算域 %s 的关联键摘要失败（退回仅按 datahub 域 id）", domain_id, exc_info=True
        )
        return f"{base}:nojoin"
    finally:
        db.close()


def digest(db: Session, domain_id: str) -> str:
    """该域关联证据集合的短摘要，用于 evidence 缓存的 fingerprint。

    **不加这一段，新证据不会生效**：evidence 缓存原先只按 datahub 域 id 作 fingerprint，
    扫完新包（或确认了一批推断候选）后重新生成草稿会直接命中旧缓存，新证据永远进不去。
    摘要覆盖两个来源，所以「确认一个键族」同样会让缓存失效。
    """
    joins = load_relation_evidence(db, domain_id)
    if not joins:
        return "nojoin"
    payload = "|".join(
        sorted(
            f"{j.origin}:{j.left_table}.{j.left_column}={j.right_table}.{j.right_column}"
            for j in joins
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
