"""血缘补录 API。

一句话口径：**这个域里有多少张表是孤岛**，以及把血缘补回 DataHub 的两种做法——
扫一个 SQL 代码包，或在画布上手工连。

写入一律 preview / apply 分离：扫描与画布编辑都只在本地落库，写 DataHub 是单独一个
动作（``/apply``），逐条记录失败。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DomainContext, LineagePackage, LineagePackageEdge
from app.services import lineage_inventory, lineage_package

logger = logging.getLogger("ontometa.api.lineage")

router = APIRouter(prefix="/lineage", tags=["lineage"])


# --------------------------------------------------------------------------- 出参


class TableRow(BaseModel):
    urn: str
    name: str
    platform: str | None = None
    upstream: int
    downstream: int
    isolated: bool


class ColumnRow(BaseModel):
    name: str
    data_type: str | None = None
    is_primary_key: bool = False


class DomainOverview(BaseModel):
    domain_id: str
    domain_name: str
    platform: str | None = None
    databases: list[str] = Field(default_factory=list)
    total: int
    with_lineage: int
    isolated: int


class EdgeRow(BaseModel):
    id: str
    source_table: str
    target_table: str
    join_key: str | None = None
    source_file: str
    state: str
    reason: str | None = None
    applied: bool = False


class GroupRow(BaseModel):
    """按落点分组——一个包上百条边，逐条列没人读得完。"""

    target: str
    isolated: bool
    files: list[str] = Field(default_factory=list)
    edges: list[EdgeRow] = Field(default_factory=list)


class PackageRow(BaseModel):
    id: str
    name: str
    kind: str
    dialect: str
    size_bytes: int
    uploaded_at: str | None = None
    sql_files: int
    directories: int
    statements: int
    parsed_files: int
    failures: list[dict] = Field(default_factory=list)
    #: 真正解析失败的文件数（parsed_files + failed_files == sql_files）。
    #: failures 里还有一类是"解析成功但没有落点"，那不算失败。
    failed_files: int = 0
    status: str
    applied_edges: int
    applied_resolved: int
    applied_at: str | None = None
    #: 扫描统计：可上报 / 待映射 / 跳过 / 落点数 / 其中孤岛落点数
    edges_ok: int = 0
    edges_blocked: int = 0
    edges_skipped: int = 0
    targets: int = 0
    isolated_targets: int = 0


class PackageDetail(PackageRow):
    groups: list[GroupRow] = Field(default_factory=list)


class ApplyReceiptOut(BaseModel):
    applied: int
    resolved: int
    failed: int
    failures: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------- 入参


class ManualEdgeIn(BaseModel):
    source_table: str
    target_table: str
    join_keys: list[str] = Field(default_factory=list)


class ManualApplyRequest(BaseModel):
    edges: list[ManualEdgeIn]
    label: str | None = None


class ApplyRequest(BaseModel):
    #: 只上报这些落点的边；不传＝该包全部可上报的边。
    targets: list[str] | None = None


# --------------------------------------------------------------------------- 组装


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _package_row(package: LineagePackage, isolated: set[str]) -> PackageRow:
    edges = list(package.edges)
    targets = {edge.target_table for edge in edges}
    failures = json.loads(package.failures_json or "[]")
    return PackageRow(
        id=package.id,
        name=package.name,
        kind=package.kind,
        dialect=package.dialect,
        size_bytes=package.size_bytes or 0,
        uploaded_at=_iso(package.uploaded_at),
        sql_files=package.sql_files or 0,
        directories=package.directories or 0,
        statements=package.statements or 0,
        parsed_files=package.parsed_files or 0,
        failures=failures,
        failed_files=sum(1 for item in failures if item.get("kind") == "parse_error"),
        status=package.status,
        applied_edges=package.applied_edges or 0,
        applied_resolved=package.applied_resolved or 0,
        applied_at=_iso(package.applied_at),
        edges_ok=sum(1 for edge in edges if edge.state == "ok"),
        edges_blocked=sum(1 for edge in edges if edge.state == "blocked"),
        edges_skipped=sum(1 for edge in edges if edge.state == "skipped"),
        targets=len(targets),
        isolated_targets=sum(1 for target in targets if target in isolated),
    )


def _package_detail(package: LineagePackage, isolated: set[str]) -> PackageDetail:
    grouped: dict[str, GroupRow] = {}
    for edge in package.edges:
        group = grouped.get(edge.target_table)
        if group is None:
            group = GroupRow(
                target=edge.target_table,
                isolated=edge.target_table in isolated,
            )
            grouped[edge.target_table] = group
        if edge.source_file not in group.files:
            group.files.append(edge.source_file)
        group.edges.append(
            EdgeRow(
                id=edge.id,
                source_table=edge.source_table,
                target_table=edge.target_table,
                join_key=edge.join_key,
                source_file=edge.source_file,
                state=edge.state,
                reason=edge.reason,
                applied=edge.applied_at is not None,
            )
        )

    base = _package_row(package, isolated)
    return PackageDetail(**base.model_dump(), groups=list(grouped.values()))


async def _isolated_names(db: Session, domain_id: str) -> set[str]:
    """孤岛表名。**DataHub 不通时返回空集而不是抛错**——代码包历史是本地数据，
    不该因为元数据侧不可达就整个查不了；孤岛标记缺失，概览端点会把真错误喊出来。"""
    try:
        inventory = await lineage_inventory.get_inventory(db, domain_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取域 %s 的孤岛清单失败，孤岛标记暂缺：%s", domain_id, exc)
        return set()
    return {table.name for table in inventory.isolated_tables}


# --------------------------------------------------------------------------- 家底


@router.get("/domains/{domain_id}/overview", response_model=DomainOverview)
async def get_overview(domain_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    """这个域有多少表、多少有血缘、多少是孤岛。"""
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="数据域不存在")
    try:
        inventory = await lineage_inventory.get_inventory(db, domain_id, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 — DataHub 不通要说清是它不通
        raise HTTPException(status_code=502, detail=f"读取 DataHub 元数据失败：{exc}") from exc

    platforms = [table.platform for table in inventory.tables if table.platform]
    return DomainOverview(
        domain_id=domain.id,
        domain_name=domain.name,
        platform=max(set(platforms), key=platforms.count) if platforms else None,
        databases=sorted(inventory.databases),
        total=inventory.total,
        with_lineage=inventory.with_lineage,
        isolated=len(inventory.isolated_tables),
    )


@router.get("/domains/{domain_id}/tables", response_model=list[TableRow])
async def list_tables(
    domain_id: str,
    only_isolated: bool = False,
    q: str = "",
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """表清单。画布从这里取表，扫描页拿它做对照。"""
    try:
        inventory = await lineage_inventory.get_inventory(db, domain_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取 DataHub 元数据失败：{exc}") from exc

    keyword = q.strip().lower()
    rows = [
        TableRow(
            urn=table.urn,
            name=table.name,
            platform=table.platform,
            upstream=table.upstream,
            downstream=table.downstream,
            isolated=table.isolated,
        )
        for table in inventory.tables
        if (not only_isolated or table.isolated) and (not keyword or keyword in table.name.lower())
    ]
    # 孤岛在前：补录要处理的就是它们
    rows.sort(key=lambda row: (not row.isolated, row.name.lower()))
    return rows[:limit]


@router.get("/domains/{domain_id}/columns", response_model=list[ColumnRow])
async def list_columns(domain_id: str, urn: str, db: Session = Depends(get_db)):
    """一张表的字段。画布上拖字段连线要用。

    **按表取**而不是随家底一起取：域里上千张表，为了画布上那三五张表把全域 schema
    拉一遍要几分钟（见 lineage_inventory 的说明）。
    """
    from app.connectors.datahub import DataHubConnector
    from app.services.settings_service import SettingsService

    connector = DataHubConnector(SettingsService().get_datahub_runtime(db))
    try:
        dataset = await connector.get_dataset_by_urn(urn)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取表字段失败：{exc}") from exc
    finally:
        await connector.aclose()

    return [
        ColumnRow(
            name=item.name,
            data_type=item.data_type,
            is_primary_key=item.is_primary_key,
        )
        for item in dataset.fields
    ]


@router.get("/domains/{domain_id}/uncovered-isolated", response_model=list[str])
async def list_uncovered_isolated(domain_id: str, db: Session = Depends(get_db)):
    """所有代码包都没提到的孤岛表——没有 SQL 可推，只能在画布上手工连。

    这是扫描页的第二个结论：扫完之后还剩什么。
    """
    isolated = await _isolated_names(db, domain_id)
    covered = {
        row[0]
        for row in db.execute(
            select(LineagePackageEdge.target_table)
            .join(LineagePackage, LineagePackage.id == LineagePackageEdge.package_id)
            .where(LineagePackage.domain_context_id == domain_id)
        ).all()
    }
    return sorted(isolated - covered)


# --------------------------------------------------------------------------- 代码包


@router.get("/domains/{domain_id}/packages", response_model=list[PackageRow])
async def list_packages(
    domain_id: str,
    kind: str = "scan",
    db: Session = Depends(get_db),
):
    """代码包历史。``kind=all`` 连画布补录的留档一起列。"""
    isolated = await _isolated_names(db, domain_id)
    stmt = (
        select(LineagePackage)
        .where(LineagePackage.domain_context_id == domain_id)
        .order_by(LineagePackage.uploaded_at.desc())
    )
    if kind != "all":
        stmt = stmt.where(LineagePackage.kind == kind)
    return [_package_row(package, isolated) for package in db.execute(stmt).scalars().all()]


@router.post("/domains/{domain_id}/packages", response_model=PackageDetail)
async def upload_package(
    domain_id: str,
    file: UploadFile = File(..., description="SQL 代码包（.zip / .tar.gz / 单个 .sql）"),
    dialect: str = Form("mysql"),
    db: Session = Depends(get_db),
):
    """上传并扫描一个代码包。只落库，不写 DataHub。"""
    blob = await file.read()
    try:
        package = await lineage_package.scan(
            db,
            domain_id=domain_id,
            filename=file.filename or "package.zip",
            blob=blob,
            dialect=dialect,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("代码包扫描失败")
        raise HTTPException(status_code=502, detail=f"扫描失败：{exc}") from exc

    return _package_detail(package, await _isolated_names(db, domain_id))


@router.get("/packages/{package_id}", response_model=PackageDetail)
async def get_package(package_id: str, db: Session = Depends(get_db)):
    package = db.get(LineagePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="代码包不存在")
    return _package_detail(package, await _isolated_names(db, package.domain_context_id))


@router.post("/packages/{package_id}/rescan", response_model=PackageDetail)
async def rescan_package(
    package_id: str, dialect: str | None = None, db: Session = Depends(get_db)
):
    """用当前解析器重扫原包。已上报的边保留，其余重算。"""
    try:
        package = await lineage_package.rescan(db, package_id, dialect=dialect)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _package_detail(package, await _isolated_names(db, package.domain_context_id))


@router.delete("/packages/{package_id}")
def delete_package(package_id: str, db: Session = Depends(get_db)):
    """删掉一条代码包记录。**不会撤销已写进 DataHub 的血缘边**。"""
    try:
        lineage_package.delete(db, package_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/packages/{package_id}/apply", response_model=ApplyReceiptOut)
async def apply_package(
    package_id: str, body: ApplyRequest, db: Session = Depends(get_db)
):
    """把选中的落点写进 DataHub。幂等，重复上报不会重复建边。"""
    try:
        receipt = await lineage_package.apply(db, package_id, targets=body.targets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApplyReceiptOut(**receipt.__dict__)


@router.post("/domains/{domain_id}/manual-apply", response_model=ApplyReceiptOut)
async def apply_manual(
    domain_id: str, body: ManualApplyRequest, db: Session = Depends(get_db)
):
    """把画布上连的边写进 DataHub，同时留一条 ``kind=manual`` 的档。"""
    try:
        receipt = await lineage_package.apply_manual(
            db,
            domain_id=domain_id,
            edges=[edge.model_dump() for edge in body.edges],
            label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApplyReceiptOut(**receipt.__dict__)
