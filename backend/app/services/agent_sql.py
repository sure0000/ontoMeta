"""Agent 代跑只读 SQL 的**唯一**闸门链（中性位置，无对话依赖）。

任何 Agent（尤其是 MCP 客户端）要读真实数据，走的都得是这一条：

    只读校验 → SQL 语义证明(F3) → 割接闸 → 就绪闸(必要时对账重判) → 落点映射 → 执行

**为什么必须是同一条**：此前 MCP 的 ``execute_sql`` 只做了第一步（只读校验）就直连 Doris。
两套校验一旦分叉，宽的那套就是实际的安全边界——一个通用 agent 于是可以写一条引用了本体
里根本不存在的表/列的 SQL，或者去查一张同步还没跑完的表，拿到 0 行、再用权威口吻报出来。
更隐蔽的是**落点映射**：本体对象名到物理表名必须由服务端统一解析，客户端不能按命名
规则自行拼表名。

出参使用统一的三元组 ``(payload, summary, is_error)``：
``is_error=True`` 表示这次调用本身不成立（缺参/被拒/执行报错），不计入接地；
被闸门挡下但**结论有效**的（未就绪、无执行目标、权限不足）是 ``is_error=False`` 的
「只给建议 SQL」——那是一条真事实，不是错误。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.models import Ontology, OntologyStatus
from app.services.ontology_projection import OntologyProjection, build_projection
from app.services.sql_soundness import SqlRejection, prove_sql_sound

logger = logging.getLogger(__name__)

#: 代跑 SQL 的默认返回行上限。两侧同一份，别各写各的。
RUN_SQL_LIMIT = 100
#: 代跑 SQL 的默认语句超时（秒）。
SQL_TIMEOUT_SECONDS = 15


def published_ontology_ids(db: Session) -> list[str]:
    """全部已发布本体的 id。

    没有会话作用域的调用方（MCP）用它当默认在场集合——与「不选域的对话＝全域通盘」
    是同一条约定，不另立一套。
    """
    from app.models import DomainContext

    rows = (
        db.query(Ontology.id)
        .join(DomainContext, DomainContext.id == Ontology.domain_context_id)
        .filter(Ontology.status == OntologyStatus.PUBLISHED.value)
        # 按数据域名排序：与 `_resolve_scope`（不选域＝全域通盘）同一个顺序，
        # 免得同一个问题在两个入口拿到不同的锚点本体。
        .order_by(DomainContext.name.asc())
        .all()
    )
    return [row[0] for row in rows]


def may_run_sql(principal_role: str | None) -> bool:
    """当前主体是否够格让 Agent 代跑 SQL。

    权限必须约束到**工具粒度**：代跑 SQL 直打真实 DSN，不能让客户端绕过角色闸门。
    fail-closed：拿不到角色一律视为不够格。
    """
    from app.models.principal import role_satisfies

    return role_satisfies(principal_role, env_settings.agent_run_sql_min_role)


def build_merged_projection(
    db: Session, ontology_ids: list[str], mapping: dict | None
) -> OntologyProjection:
    """合并多个本体的投影为一个虚拟投影，供跨域 SQL 语义证明。

    表/列名跨域冲突时后者覆盖前者（同名极罕见；以检索为准，证明仅放行已知表列）。
    """
    objects: dict[str, Any] = {}
    relations_by_pair: dict[frozenset[str], list[Any]] = {}
    mapping_tables: dict[str, str] = {}
    mapping_columns: dict[str, str] = {}
    for oid in ontology_ids:
        proj = build_projection(db, oid, mapping)
        objects.update(proj.objects)
        for k, v in proj.relations_by_pair.items():
            relations_by_pair.setdefault(k, []).extend(v)
        mapping_tables.update(proj.mapping_tables)
        mapping_columns.update(proj.mapping_columns)
    return OntologyProjection(
        objects=objects,
        relations_by_pair=relations_by_pair,
        mapping_tables=mapping_tables,
        mapping_columns=mapping_columns,
    )


def _references_no_table(sql: str) -> bool:
    """这条 SQL 是否一张表都没引用（解析不了时返回 False——交给证明器去拒）。"""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="doris")
    except Exception:  # noqa: BLE001
        return False
    if tree is None:
        return False
    return not any(table.name for table in tree.find_all(exp.Table))


def prove_or_reject(
    db: Session, sql: str, ontology_ids: list[str] | None, source: Any, mapping: dict | None
) -> tuple[tuple[Any, str, bool] | None, dict]:
    """SQL 语义证明门（F3）。返回 (拒绝三元组或 None, 证书摘要)。

    多域：合并所有涉及本体的投影后统一证明，跨域 SQL（JOIN 不同域的表）一次过。
    受 ``settings.agent_soundness`` 开关：off=跳过；warn=只记日志不拦；on=拒绝执行。
    证明本身出错时，若存在可执行 Doris 则 fail-closed；没有执行目标时仍可给建议 SQL。

    **证书要带回去**：``SqlCertificate.tables/columns`` 是「这些表/列确实是已发布本体
    成员」的**证明结论**，不是模型的主张。不回传的话，模型写了一条被证明合法的 SQL、
    再在正文里解释它引用的字段，会因账本里没有该字段而被 F4 判成幻觉——自己证过的
    东西反过来拒自己。
    """
    from app.services import data_app_executor

    mode = (getattr(env_settings, "agent_soundness", "on") or "on").lower()
    if mode == "off" or not ontology_ids:
        return None, {}
    if _references_no_table(sql):
        # 一条不引用任何表的查询（`SELECT 1`、`SELECT NOW()`）没有「这张表/这个列属于本体
        # 吗」这个命题可证——它读不到任何数据。证明器对它会因为无法给常量列定归属而
        # 保守拒绝，那不是在拦风险，只是把连通性探测也一并拦掉了。
        return None, {}
    try:
        proj = build_merged_projection(db, ontology_ids, mapping)
        dialect = (
            "doris"
            if source is not None and getattr(source, "kind", None) == "doris"
            else (
                data_app_executor.backend_of(source.dsn_secret_ref)
                if source is not None else None
            )
        )
        verdict = prove_sql_sound(sql, proj, dialect=dialect)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sql soundness prover error: %s", exc)
        if source is not None:
            return (
                {
                    "executed": False,
                    "rejected": True,
                    "sql": sql,
                    "reason": "SQL 语义证明器不可用，Doris 查询已 fail-closed",
                    "code": "soundness_unavailable",
                },
                "SQL 语义证明不可用",
                True,
            ), {}
        return None, {}
    if not isinstance(verdict, SqlRejection):
        return None, {"tables": list(verdict.tables), "columns": list(verdict.columns)}
    if mode == "warn":
        logger.info("[soundness=warn] 本应拒答 SQL：%s | %s", verdict.code, verdict.message)
        return None, {}
    # mode == "on"：拒绝执行。is_error=True → 不计入接地，避免用拒绝当命中。
    # hint 必须随结果回灌：调用方据此自修（补对候选字段、换合法对端、改安全聚合）。
    return (
        {"executed": False, "rejected": True, "sql": sql,
         "reason": verdict.message, "code": verdict.code,
         "hint": _enrich_hint(db, verdict, ontology_ids)},
        f"SQL 语义证明未通过：{verdict.code}",
        True,
    ), {}


def _enrich_hint(db: Session, verdict: SqlRejection, ontology_ids: list[str]) -> dict:
    """「表名不对应任何已发布业务对象」时，说清它是**不存在**还是**没发布**。

    两者的下一步完全不同：不存在要换个表名，没发布要走复核发布。原来的拒绝信号把它们
    说成同一句话，调用方于是要靠 resolve_subject / query_objects / get_landing 三四次
    额外调用才能自己拼出真相——真机上就这么发生过一次。
    """
    hint = dict(verdict.hint or {})
    if verdict.code != "unknown_table":
        return hint
    table = (verdict.detail or {}).get("table")
    if not table:
        return hint

    from app.models import EntityStatus, ObjectType

    rows = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id.in_(ontology_ids),
            ObjectType.name == str(table),
            ObjectType.status != EntityStatus.PUBLISHED.value,
        )
        .all()
    )
    if rows:
        hint["unpublished_match"] = [
            {"object_id": row.id, "name": row.name, "status": row.status}
            for row in rows
        ]
        hint["fix"] = (
            f"对象「{table}」在本体里是存在的，但状态是 "
            f"{'/'.join(sorted({row.status for row in rows}))}，还没发布——受治理的取数只认已发布"
            "对象。这不是「表不存在」，别换个表名重试；要么走复核发布，要么如实说明取不到。"
        )
    return hint


def run_agent_sql(
    db: Session,
    *,
    sql: str,
    ontology_ids: list[str] | None,
    limit: int | None = None,
    timeout_seconds: int | None = None,
    principal_role: str | None = None,
    require_role: bool = True,
    resolve_source: Callable[[Session], Any] | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """跑完整条闸门链并（在允许时）执行。

    ``require_role=False`` 表示调用方已经在自己那一层做过等价的角色门控——MCP 的
    ``execute_sql`` 用 ``required_role = settings.agent_run_sql_min_role`` 在服务器层
    统一强制，到这里再要一次 principal_role 只会把已经通过的调用重新拒掉。

    ``resolve_source`` 允许调用方替换「默认 Doris 是哪一个」的解析（测试会替身掉它，
    好把用例钉在一个确定的数据源上而不是全库里随手捞到的那一个）。
    """
    from app.services import data_app_executor
    from app.services.data_app import resolve_domain_data_source

    sql = (sql or "").strip()
    if not sql:
        return {"error": "缺少 sql"}, "run_sql 缺少 sql", True

    try:
        limit = int(limit or RUN_SQL_LIMIT)
    except (TypeError, ValueError):
        limit = RUN_SQL_LIMIT
    limit = max(1, min(limit, RUN_SQL_LIMIT))
    timeout_seconds = int(timeout_seconds or SQL_TIMEOUT_SECONDS)

    ok, reason = data_app_executor.is_read_only(sql)
    if not ok:
        return (
            {"executed": False, "error": f"仅允许只读 SELECT：{reason}", "sql": sql},
            "被只读校验拒绝",
            True,
        )

    # 权限不足时不解析数据源——降级为「仅建议 SQL」，而不是硬报错：
    # 检索类工具对低权角色仍合法，问答体验不掉，只是不代跑数。
    may_run = may_run_sql(principal_role) if require_role else True
    resolver = resolve_source or resolve_domain_data_source
    source = resolver(db) if may_run else None
    # DataSource.mapping_json 是历史元数据，不得据以授权查询。对象级映射只在语义证明
    # 认出了每一个被引用的对象之后，从当前 Deployment / Projection 现取。
    mapping: dict | None = None

    # ★ SQL 语义证明（F3）：执行/建议前静态证明语义合法，不过则不放行。
    #   即便无可执行数据源（仅建议 SQL），也要证明——臆造字段/JOIN 与是否落库无关。
    rejection, proved = prove_or_reject(db, sql, ontology_ids, source, mapping)
    if rejection is not None:
        return rejection

    object_names: list[str] = []
    try:
        from app.services.query_routing import (
            projection_mapping,
            readiness_error,
            referenced_table_names,
        )

        # 不引用任何表的查询（`SELECT 1`）没有对象要过就绪闸——`referenced_table_names`
        # 对它会抛「未引用任何本体对象」，那句话对连通性探测是误报。
        if not _references_no_table(sql):
            object_names = list(proved.get("tables") or referenced_table_names(sql))
        if source is not None:
            from app.services.warehouse_migration import cutover_error

            migration_error = cutover_error(db, ontology_ids or [])
            if migration_error:
                return (
                    {"executed": False, "reason": migration_error, "sql": sql, "proved": proved},
                    "尚未审批切流",
                    False,
                )
            ready_error = readiness_error(
                db,
                datasource=source,
                ontology_ids=ontology_ids,
                object_names=object_names,
            )
            if ready_error:
                # 未就绪的结论可能只是陈旧：制品状态靠「有人读」才推进，一次早已
                # success 的同步能把表锁在不可查上好几个小时。先对一次账再重判。
                from app.services.query_readiness import (
                    readiness_detail,
                    reconcile_blocking_runs,
                )

                if reconcile_blocking_runs(
                    db, ontology_ids=ontology_ids, object_names=object_names
                ):
                    ready_error = readiness_error(
                        db,
                        datasource=source,
                        ontology_ids=ontology_ids,
                        object_names=object_names,
                    )
                if ready_error:
                    detail = readiness_detail(
                        db, ontology_ids=ontology_ids, object_names=object_names
                    )
                    return (
                        {
                            "executed": False,
                            "reason": (
                                f"{ready_error}（{detail}）" if detail else ready_error
                            ),
                            "sql": sql,
                            "proved": proved,
                        },
                        "Doris Projection 未就绪",
                        False,
                    )
            mapping = projection_mapping(
                db,
                datasource=source,
                ontology_ids=ontology_ids or [],
                object_names=object_names,
            )
    except ValueError as exc:
        return (
            {"executed": False, "reason": str(exc), "sql": sql, "proved": proved},
            "Doris Projection 覆盖校验失败",
            False,
        )

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
            {
                "executed": False,
                "reason": "当前未配置可执行的默认 Doris 数仓，仅能给出建议 SQL",
                "sql": sql,
                "proved": proved,
            },
            "无可执行数据源",
            False,
        )
    try:
        columns, rows = data_app_executor.execute_sql(
            dsn=source.dsn_secret_ref,
            sql=sql,
            limit=limit,
            mapping=mapping or None,
            timeout_seconds=timeout_seconds,
            dialect="doris",
        )
    except Exception as exc:  # noqa: BLE001
        return (
            {"executed": False, "error": str(exc)[:300], "sql": sql},
            "SQL 执行失败",
            True,
        )

    truncated = len(rows) >= limit
    result: dict[str, Any] = {
        "executed": True,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "proved": proved,
        "datasource": source.name,
    }
    if source.purpose == "warehouse":
        from app.services.query_routing import target_receipt

        result["query_target"] = target_receipt(
            db,
            datasource=source,
            ontology_ids=ontology_ids,
            object_names=object_names,
        )
    # 截断 + 无 ORDER BY → 这是一份「无序样本」（执行层虽已自动补 ORDER BY 1 保证可复现，
    # 但首列排序无业务含义），明确告知不是全集、非按业务键取样，避免把两次样例读成矛盾。
    if truncated and not re.search(r"\border\s+by\b", sql, flags=re.IGNORECASE):
        result["sample_note"] = (
            f"已截断到前 {limit} 行且原 SQL 未指定排序：这是一份无业务序的样本、非全集。"
            "若需稳定/可复现的明细，请按主键或时间键显式 ORDER BY 后重取。"
        )
    return (
        result,
        f"返回 {len(rows)} 行" + ("（无序样本）" if result.get("sample_note") else ""),
        False,
    )
