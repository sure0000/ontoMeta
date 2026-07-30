"""本体一键物化编排：生成 DDL/ETL → 对目标数据源真正落库 → 回执。

本模块把已有的三块能力串成一次可执行的物化，不重造任何一块：

- **物化契约**（``services/materialization_contract``）：先 ``sync`` 保证契约存在/最新，
  弹窗覆盖的存储策略/表名作为 override 写回并钉住，使"生成"与"展示"同一事实源。
- **正向生成器**（``services/warehouse_generator``）：按目标引擎产出建表 DDL 与
  ODS→目标层的 ETL SQL；已按契约 ``materialized`` 过滤，本模块只再按用户勾选裁剪。
- **写侧执行器**（``services/data_app_executor.execute_write``）：把语句真正打到目标
  ``DataSource`` 的 DSN 上，单事务、失败回滚。

方言现实：生成的 DDL 是数仓方言（Hive/Doris/…），目标 DataSource 的 DSN 必须是对应
数仓引擎；本地 SQLite/DuckDB 无对应 adapter，不能承载完整 DDL（见 test 与文档）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.data_app import DataSource
from app.services import data_app_executor
from app.services.materialization_contract import MaterializationContractService
from app.services.warehouse_generator import WarehouseGenerator

_contract_service = MaterializationContractService()
_generator = WarehouseGenerator()


class MaterializationError(ValueError):
    """物化前置条件错误（目标源不存在 / 未配置连接串等），面向用户可读。"""


def _loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _bare_name(qualified: str) -> str:
    """``dim_erp.customer`` → ``customer``。generator 的 statements 以库.表为键。"""
    return qualified.split(".")[-1]


def _select(
    statements: dict[str, str], selected: set[str] | None
) -> list[tuple[str, str]]:
    """按用户勾选裁剪，保持 generator 的稳定顺序，返回 (qualified, sql) 列表。

    ``selected`` 为实体名集合；None 表示不裁剪（全选）。
    """
    items = list(statements.items())
    if selected is None:
        return items
    return [(q, s) for q, s in items if _bare_name(q) in selected]


def _run_phase(
    dsn: str, items: list[tuple[str, str]], mapping: dict[str, Any] | None
) -> dict[str, Any]:
    """执行一批语句并把回执的 per_statement 归位到 qualified name。"""
    receipt = data_app_executor.execute_write(
        dsn=dsn, statements=[sql for _, sql in items], mapping=mapping
    )
    for ps in receipt.get("per_statement", []):
        idx = ps.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            ps["target"] = items[idx][0]
    receipt["targets"] = [q for q, _ in items]
    return receipt


def _skipped_phase(total: int, reason: str) -> dict[str, Any]:
    return {
        "total": total,
        "executed": 0,
        "failed": 0,
        "error": None,
        "skipped": True,
        "skip_reason": reason,
        "per_statement": [],
        "targets": [],
    }


def run(
    db: Session,
    ontology_id: str,
    *,
    target_datasource_id: str,
    engine: str,
    database_prefix: str | None = None,
    selected_targets: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    sync_contracts: bool = True,
) -> dict[str, Any]:
    """物化一个本体到目标数据源，返回回执 dict。

    先建表（DDL），全部成功后再落数（ETL）——表不存在时装载没有意义，故 DDL 有失败
    即跳过 ETL，回执里显式标注，绝不静默。

    ``overrides``：``{contract_id: {字段: 值}}``，弹窗里人工改的存储策略/层/表名等。
    经 ``MaterializationContractService.update`` 写回并钉住，使生成读到的契约与展示
    一致（不另存一份配置）。
    """
    ds = db.get(DataSource, target_datasource_id)
    if ds is None:
        raise MaterializationError("目标数据源不存在")
    if not ds.dsn_secret_ref:
        raise MaterializationError(
            f"目标数据源「{ds.name}」未配置连接串（dsn），无法落库"
        )

    # 契约是生成器的输入事实源：先对齐，保证 materialized/层/分区等为最新，
    # 再应用人工覆盖（override 会钉住，后续机器推导不覆盖）。
    if sync_contracts:
        _contract_service.sync(db, ontology_id)
    for contract_id, patch in (overrides or {}).items():
        _contract_service.update(db, contract_id, patch)

    ddl = _generator.generate_ddl(
        db, ontology_id, engine, database_prefix=database_prefix
    )
    etl = _generator.generate_etl_sql(
        db, ontology_id, engine, database_prefix=database_prefix
    )

    selected = set(selected_targets) if selected_targets else None
    ddl_items = _select(ddl["statements"], selected)
    etl_items = _select(etl["statements"], selected)
    mapping = _loads(ds.mapping_json)

    ddl_receipt = _run_phase(ds.dsn_secret_ref, ddl_items, mapping)
    ddl_ok = ddl_receipt["failed"] == 0 and ddl_receipt["error"] is None
    if ddl_ok:
        etl_receipt = _run_phase(ds.dsn_secret_ref, etl_items, mapping)
    else:
        etl_receipt = _skipped_phase(len(etl_items), "建表未全部成功，跳过数据装载")

    ok = ddl_ok and etl_receipt["failed"] == 0 and etl_receipt.get("error") is None
    return {
        "ontology_id": ontology_id,
        "target_datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind},
        "engine": engine,
        "database_prefix": database_prefix,
        "tables": [q for q, _ in ddl_items],
        "ddl": ddl_receipt,
        "etl": etl_receipt,
        "warnings": ddl.get("warnings", []),
        "unsupported": (ddl.get("unsupported") or []) + (etl.get("unsupported") or []),
        "ok": ok,
    }
