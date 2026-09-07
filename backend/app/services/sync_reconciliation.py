"""Verify a completed sync against its Doris ODS target.

**为什么不能只数目标表**：Airflow 成功只证明编排跑完了，目标表有几行才是结论。但
「目标表 0 行」有两种完全不同的含义——源本来就是空表，或者这次根本没搬动任何数据——
只数目标表分辨不了，于是两者都会显示绿色的「落数验证通过」。同理，搬了 500 行中的
50 行（部分装载）也一样是绿的。

所以全量模式下**两边都数**，用源表行数当预期值：
  源 == 目标 且 > 0   → 通过
  源 == 目标 且 == 0   → 通过，但标成「源本来就是空的」，界面不给绿勾
  源 != 目标          → 不通过，报出两个数字
  源数不到（无权限/连不上）→ 通过，但注明未能比对

增量/CDC 模式不比对：目标表跨多次运行累积，目标 > 源是正常的，拿全量计数去比只会
制造假失败。这类任务仍然会因为 0 行而被标注。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import DataSource, IngestionContract
from app.services.data_app_executor import ExecutionError, backend_of, execute_sql
from app.services.ingestion_contract import mirror_contract_to_projection
from app.warehouse import get_adapter, quote_table_ref

logger = logging.getLogger("ontometa.sync_reconciliation")

#: 只有全量装载能拿「源表总行数」当预期值。
_COMPARABLE_MODES = frozenset({"full", "", "overwrite"})


def _count_rows(dsn: str, table_ref: str, *, dialect: str | None) -> int | None:
    """数一张表的行数。数不到返回 None（连不上、无权限、表不存在都算数不到）。"""
    quoted = quote_table_ref(dialect, table_ref)
    _columns, rows = execute_sql(
        dsn=dsn,
        sql=f"SELECT COUNT(*) AS row_count FROM {quoted}",
        limit=1,
        timeout_seconds=30,
        dialect=dialect,
    )
    return int((rows[0] if rows else {}).get("row_count") or 0)


def _count_source_rows(db: Session, contract: IngestionContract) -> tuple[int | None, str | None]:
    """源表行数，以及数不到时的原因。数不到不是失败——只是没法比对。"""
    datasource = db.get(DataSource, contract.source_datasource_id)
    dsn = (datasource.dsn_secret_ref or "").strip() if datasource else ""
    if not dsn:
        return None, "源数据源不存在或未配置连接"
    try:
        return _count_rows(dsn, contract.source_physical_table, dialect=backend_of(dsn)), None
    except Exception as exc:  # noqa: BLE001 —— 数不到源不该把已经搬成的同步判失败
        logger.info(
            "无法统计源表 %s 的行数，本次跳过行数比对：%s",
            contract.source_physical_table,
            exc,
        )
        return None, str(exc)[:200]


def reconcile_sync_receipt(
    db: Session,
    *,
    receipt: dict[str, Any],
    airflow_state: str | None,
) -> dict[str, Any] | None:
    """Update the ingestion contract and return Doris verification evidence.

    Airflow 成功只证明编排跑完了。全量模式下再拿源表行数当预期值比对（模块 docstring
    列了四种判定）；比对不上即判失败，比对得上但结果需要提醒的（0 行、没能比对、增量
    跳过比对）在 ``caveat`` 里说明——调用方据此决定要不要给纯绿勾。
    """
    contract_id = str(receipt.get("ingestion_contract_id") or "").strip()
    if not contract_id:
        return {
            "status": "failed",
            "verified": False,
            "error": "同步回执缺少接入契约，无法验证 Doris 落数结果",
        }
    contract = db.get(IngestionContract, contract_id)
    if contract is None:
        return {
            "status": "failed",
            "verified": False,
            "error": "同步回执引用的接入契约不存在，无法验证 Doris 落数结果",
        }

    state = (airflow_state or "").lower()
    target = f"{contract.target_ods_database}.{contract.target_ods_table}"
    if state in {"running", "queued", "scheduled"}:
        contract.status = "running"
        mirror_contract_to_projection(db, contract)
        db.commit()
        return {
            "status": "running",
            "verified": False,
            "target_table": target,
        }
    if state in {"failed", "upstream_failed"}:
        contract.status = "failed"
        mirror_contract_to_projection(db, contract)
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": "Airflow/Flink 同步任务执行失败",
        }
    if state != "success":
        return None

    datasource = db.get(DataSource, contract.doris_datasource_id)
    target_dsn = (datasource.dsn_secret_ref or "").strip() if datasource else ""
    if not target_dsn:
        contract.status = "failed"
        mirror_contract_to_projection(db, contract)
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": "目标 Doris 数据源不存在或未配置连接，无法验证落数结果",
        }

    quoted = get_adapter("doris").quote_table_ref(target)
    try:
        _columns, rows = execute_sql(
            dsn=target_dsn,
            sql=f"SELECT COUNT(*) AS row_count FROM {quoted}",
            limit=1,
            timeout_seconds=30,
            dialect="doris",
        )
        row_count = int((rows[0] if rows else {}).get("row_count") or 0)
    except (ExecutionError, TypeError, ValueError) as exc:
        contract.status = "failed"
        mirror_contract_to_projection(db, contract)
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": f"Doris 目标表验证失败：{exc}",
        }

    # ---- 与源表行数比对 ----
    mode = (contract.mode or "").strip().lower()
    comparable = mode in _COMPARABLE_MODES
    source_count: int | None = None
    compare_note: str | None = None
    if comparable:
        source_count, compare_note = _count_source_rows(db, contract)

    if not comparable:
        comparison = "skipped_incremental"
    elif source_count is None:
        comparison = "unavailable"
    elif source_count == row_count:
        comparison = "match"
    else:
        comparison = "mismatch"

    now = datetime.now(UTC).replace(tzinfo=None)
    evidence: dict[str, Any] = {
        "target_table": target,
        "row_count": row_count,
        "source_table": contract.source_physical_table,
        "source_row_count": source_count,
        "comparison": comparison,
        "mode": mode or "full",
        "empty": row_count == 0,
        "verified_at": now.isoformat(),
    }
    if compare_note:
        evidence["comparison_note"] = compare_note

    # 行数对不上 = 没搬完。这不是「验证不了」，是明确的失败：目标表现在装着一份
    # 不完整的数据，而下游（Projection / Agent）会把它当成这个对象的全量。
    if comparison == "mismatch":
        contract.status = "failed"
        mirror_contract_to_projection(db, contract)
        db.commit()
        return {
            **evidence,
            "status": "failed",
            "verified": False,
            "error": (
                f"落数行数与源表不一致：源 {source_count} 行，目标 {row_count} 行。"
                "目标表当前是一份不完整的数据，请查 Flink 作业日志后重跑。"
            ),
        }

    contract.status = "ready"
    contract.last_success_at = now
    # 落数验证通过才算真成功：同步目标就是没有独立加工步骤的源对象的 serving 表。
    # 共同镜像函数会创建/补齐当前版本的 Deployment/Projection，再让各读侧共用这一结论。
    mirror_contract_to_projection(db, contract)
    db.commit()

    # 通过，但有话要说的两种情况——界面据此不给纯绿勾（见 AgentsPanel 的回执面板）：
    #   0 行：即便源也是 0，「这张表里没有数据」本身就是使用者必须知道的事；
    #   数不到源：这次没有比对过，绿勾的含金量比平时低，得说出来。
    caveat: str | None = None
    if row_count == 0:
        caveat = (
            "源表也是 0 行，目标表建好了但没有数据"
            if comparison == "match"
            else "目标表 0 行，且未能与源表核对——无法确认是源本来就空，还是这次没搬动数据"
        )
    elif comparison == "unavailable":
        caveat = f"未能统计源表行数，本次没有做行数核对（{compare_note or '原因未知'}）"
    elif comparison == "skipped_incremental":
        caveat = "增量/CDC 模式按设计不做全量行数核对（目标表跨多次运行累积）"

    return {
        **evidence,
        "status": "verified",
        "verified": True,
        "caveat": caveat,
    }


__all__ = ["reconcile_sync_receipt"]
