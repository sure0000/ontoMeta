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
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import (
    DomainContext,
    LineagePackage,
    LineagePackageEdge,
    LineageTableMapping,
    RelationCandidateFamily,
    RelationInferenceTask,
)
from app.services import (
    datahub_bundle_cache,
    key_family,
    lineage_inventory,
    lineage_package,
    observed_joins,
    relation_inference,
)

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
    #: 上下游皆空的表数（与 isolated 同口径，保留旧字段兼容）。
    no_lineage: int
    #: 同时没有 DataHub 血缘和本地关联证据的表数。
    no_any_relation: int


class EdgeRow(BaseModel):
    id: str
    #: lineage（数据流动，会上报 DataHub）/ relation（DDL 外键，只进本体证据）
    kind: str = "lineage"
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
    #: 扫描统计：可上报 / 待映射 / 跳过 / 落点数 / 其中孤岛落点数。
    #: 只统计**血缘边**——关联边（DDL 外键）不上报，见 relations。
    edges_ok: int = 0
    edges_blocked: int = 0
    edges_skipped: int = 0
    #: DDL 里声明的外键条数。不上报 DataHub，只作为关联证据进本体。
    relations: int = 0
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
    # 血缘边与关联边分开统计：``edges_ok`` 是「待上报」的口径，关联边永远不上报，
    # 混进去会让人以为还有一批没投出去。
    edges = [edge for edge in package.edges if edge.kind == "lineage"]
    relations = [edge for edge in package.edges if edge.kind == "relation"]
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
        relations=len(relations),
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
                kind=edge.kind,
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


def _relation_table_names(
    db: Session, domain_id: str, inventory: lineage_inventory.DomainInventory
) -> set[str]:
    """返回本域已有关系证据涉及的 DataHub 表名。

    关联证据来自代码包 JOIN、DDL 外键和已确认键族；它们的表名可能带库名前缀，
    因而只在 inventory 中裸表名唯一时归一。无法唯一对齐的证据不参与概览计数，
    避免把跨库同名表错误地标成“有关系”。
    """
    by_bare: dict[str, list[str]] = {}
    for table in inventory.tables:
        bare = observed_joins.bare_table(table.name).lower()
        by_bare.setdefault(bare, []).append(table.name)

    names: set[str] = set()
    try:
        joins = observed_joins.load_relation_evidence(db, domain_id)
    except Exception as exc:  # noqa: BLE001 - 关系证据是概览加分项，不能阻塞主指标
        logger.warning("读取域 %s 的关系证据失败，概览暂不计关系：%s", domain_id, exc)
        return names

    for join in joins:
        for endpoint in (join.left_table, join.right_table):
            hits = by_bare.get(observed_joins.bare_table(endpoint).lower(), [])
            if len(hits) == 1:
                names.add(hits[0])
    return names


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
    relation_names = _relation_table_names(db, domain_id, inventory)
    no_lineage = len(inventory.isolated_tables)
    no_any_relation = sum(
        1
        for table in inventory.tables
        if table.isolated and table.name not in relation_names
    )
    return DomainOverview(
        domain_id=domain.id,
        domain_name=domain.name,
        platform=max(set(platforms), key=platforms.count) if platforms else None,
        databases=sorted(inventory.databases),
        total=inventory.total,
        with_lineage=inventory.with_lineage,
        isolated=no_lineage,
        no_lineage=no_lineage,
        no_any_relation=no_any_relation,
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


# --------------------------------------------------------------------------- 表名映射


class TableMappingRow(BaseModel):
    id: str
    sql_table: str
    target_urn: str
    target_table: str
    created_by: str | None = None
    created_at: str | None = None


class TableMappingRequest(BaseModel):
    #: 代码包里写的表名（大小写随意，存的时候归一到小写）
    sql_table: str
    #: 它其实指向哪张 DataHub 表
    target_urn: str
    operator: str | None = None


class TableMappingReceipt(BaseModel):
    mapping: TableMappingRow
    #: 记下这条映射后当场修复了多少条 blocked 边。
    repaired: int = 0


def _mapping_row(mapping: LineageTableMapping) -> TableMappingRow:
    return TableMappingRow(
        id=mapping.id,
        sql_table=mapping.sql_table,
        target_urn=mapping.target_urn,
        target_table=mapping.target_table,
        created_by=mapping.created_by,
        created_at=_iso(mapping.created_at),
    )


@router.get("/domains/{domain_id}/table-mappings", response_model=list[TableMappingRow])
def list_table_mappings(domain_id: str, db: Session = Depends(get_db)):
    """该域的人工表名映射。重扫时自动套用。"""
    return [_mapping_row(m) for m in lineage_package.list_mappings(db, domain_id)]


@router.post(
    "/domains/{domain_id}/table-mappings", response_model=TableMappingReceipt
)
async def save_table_mapping(
    domain_id: str, body: TableMappingRequest, db: Session = Depends(get_db)
):
    """记一条「这张表其实是那张」，并**当场修复**该域已有的 blocked 边。

    此前 blocked 边的唯一出路是重扫，而重扫用的是同一套自动解析、结果一样。
    """
    try:
        repaired = await lineage_package.save_mapping(
            db,
            domain_id=domain_id,
            sql_table=body.sql_table,
            target_urn=body.target_urn,
            operator=body.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mapping = next(
        m
        for m in lineage_package.list_mappings(db, domain_id)
        if m.sql_table == body.sql_table.strip().strip("`\"'").lower()
    )
    return TableMappingReceipt(mapping=_mapping_row(mapping), repaired=repaired)


@router.delete("/table-mappings/{mapping_id}")
def delete_table_mapping(mapping_id: str, db: Session = Depends(get_db)):
    """删掉一条映射。已经修好的边不会被打回 blocked——要改判请重扫。"""
    try:
        lineage_package.delete_mapping(db, mapping_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


# --------------------------------------------------------------------------- 键族


class KeyMemberRow(BaseModel):
    table: str
    column: str
    distinct: int
    rows: int
    distinct_ratio: float
    near_unique: bool


class KeyFamilyRow(BaseModel):
    id: str
    value_shape: str
    tables: int
    columns: int
    column_names: list[str]
    sample_values: list[str]
    #: 近唯一成员 = 该实体在本域的候选主表。空 = 这个实体没有主数据表。
    anchors: list[str] = Field(default_factory=list)
    members: list[KeyMemberRow] = Field(default_factory=list)


class KeyFamilyReport(BaseModel):
    domain_id: str
    tables: int
    fields: int
    candidate_columns: int
    #: 各闸门剔掉了多少列——不摊开就没法回答「我的键为什么不见了」。
    dropped: dict[str, int] = Field(default_factory=dict)
    families: list[KeyFamilyRow] = Field(default_factory=list)
    cached: bool = False


@router.get("/domains/{domain_id}/key-families", response_model=KeyFamilyReport)
async def get_key_families(
    domain_id: str, refresh: bool = False, db: Session = Depends(get_db)
):
    """按**值形状**把域内字段聚成「键族」——跨表复用的实体键候选。

    只做机械判定（闸门 + 聚族 + 基数），**不判断族是不是真实体键**：``N<6>``/``310115``
    是行政区划码而 ``A<2>N<8>``/``RY00000183`` 是人员编号，这层要看业务语义，
    留给后续的 LLM 判定与人工确认。

    首次要等 DataHub 的分钟级抓取，之后在缓存 TTL 内秒回；``refresh=true`` 强制重抓。
    """
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="数据域不存在")

    try:
        bundle, cached = await datahub_bundle_cache.fetch(db, domain, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 — DataHub 不通要说清是它不通
        raise HTTPException(status_code=502, detail=f"读取 DataHub 元数据失败：{exc}") from exc

    families, stats = key_family.build_key_families(bundle.datasets)
    return KeyFamilyReport(
        domain_id=domain_id,
        tables=len(bundle.datasets),
        fields=sum(len(ds.fields) for ds in bundle.datasets),
        candidate_columns=sum(len(f.members) for f in families),
        dropped=stats.as_dict(),
        cached=cached,
        families=[
            KeyFamilyRow(
                id=family.id,
                value_shape=family.value_shape,
                tables=len(family.tables),
                columns=len(family.members),
                column_names=list(family.column_names),
                sample_values=list(family.sample_values),
                anchors=[member.ref for member in family.anchors],
                members=[
                    KeyMemberRow(
                        table=member.table,
                        column=member.column,
                        distinct=member.distinct,
                        rows=member.rows,
                        distinct_ratio=round(member.distinct_ratio, 4),
                        near_unique=member.near_unique,
                    )
                    for member in family.members
                ],
            )
            for family in families
        ],
    )


# --------------------------------------------------------------------------- 关系候选


class CandidatePairRow(BaseModel):
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    #: 按 distinct/rows 算出来的，不是模型给的。
    cardinality: str
    structure_type: str


class RelationCandidateRow(BaseModel):
    id: str
    family_id: str
    value_shape: str
    sample_values: list[str] = Field(default_factory=list)
    table_count: int
    column_count: int
    #: entity_key / dimension_code / not_a_key
    verdict: str
    entity_name: str | None = None
    key_name: str | None = None
    predicate: str | None = None
    confidence: float
    #: 判据原文——复核界面要展示的就是这句。
    reason: str | None = None
    state: str
    decided_by: str | None = None
    decided_at: str | None = None
    members: list[dict] = Field(default_factory=list)
    #: 展开后的两两关系条数（真要看明细走 ?with_pairs=true）。
    pair_count: int = 0
    pairs: list[CandidatePairRow] = Field(default_factory=list)


class RelationApplyRequest(BaseModel):
    #: 不传＝写回该域全部已确认键族；传入时只处理指定候选。
    candidate_ids: list[str] | None = None


class RelationApplyReceipt(BaseModel):
    attempted: int
    applied: int
    failed: int
    candidates_applied: int
    failures: list[dict[str, str]] = Field(default_factory=list)


class InferenceTaskRow(BaseModel):
    id: str
    domain_id: str
    run_id: str | None = None
    #: queued / running / succeeded / failed
    status: str
    progress: int
    message: str | None = None
    error_summary: str | None = None
    summary: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DecideRequest(BaseModel):
    #: confirmed / rejected
    state: str
    operator: str | None = None


def _candidate_row(
    row: RelationCandidateFamily, *, with_pairs: bool = False
) -> RelationCandidateRow:
    pairs = relation_inference.expand_pairs(row)
    return RelationCandidateRow(
        id=row.id,
        family_id=row.family_id,
        value_shape=row.value_shape,
        sample_values=json.loads(row.sample_values_json or "[]"),
        table_count=row.table_count or 0,
        column_count=row.column_count or 0,
        verdict=row.verdict,
        entity_name=row.entity_name,
        key_name=row.key_name,
        predicate=row.predicate,
        confidence=row.confidence or 0.0,
        reason=row.reason,
        state=row.state,
        decided_by=row.decided_by,
        decided_at=_iso(row.decided_at),
        members=json.loads(row.members_json or "[]"),
        pair_count=len(pairs),
        pairs=[CandidatePairRow(**pair) for pair in pairs] if with_pairs else [],
    )


def _task_row(task: RelationInferenceTask) -> InferenceTaskRow:
    return InferenceTaskRow(
        id=task.id,
        domain_id=task.domain_context_id,
        run_id=task.run_id,
        status=task.status,
        progress=task.progress or 0,
        message=task.message,
        error_summary=task.error_summary,
        summary=json.loads(task.summary_json) if task.summary_json else None,
        created_at=_iso(task.created_at),
        updated_at=_iso(task.updated_at),
    )


@router.post("/domains/{domain_id}/infer-relations", response_model=InferenceTaskRow)
def infer_relations(
    domain_id: str, refresh: bool = False, db: Session = Depends(get_db)
):
    """起一次智能关系补充：聚族 → LLM 判定 → 落候选。**只写本地库，不碰 DataHub。**

    **异步**：LLM 判定实测数百秒（整域一次长生成），同步等会被前端与反向代理先掐断。
    这里立刻返回任务，调用方轮询 ``GET /lineage/inference-tasks/{id}``。

    同一个域同时只允许一个推断在跑；已经人工表过态的族保留原表态，只刷新机器判定部分。
    """
    try:
        task = relation_inference.start_inference(db, domain_id, refresh=refresh)
    except relation_inference.InferenceAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _task_row(task)


@router.get("/inference-tasks/{task_id}", response_model=InferenceTaskRow)
def get_inference_task(task_id: str, db: Session = Depends(get_db)):
    """轮询推断进度。``status=failed`` 时 ``error_summary`` 里是真实原因。"""
    task = relation_inference.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_row(task)


@router.get(
    "/domains/{domain_id}/inference-task", response_model=InferenceTaskRow | None
)
def get_latest_inference_task(domain_id: str, db: Session = Depends(get_db)):
    """该域最近一次推断任务——页面一进来靠它判断「是不是还在跑」。没有则返回 null。"""
    task = relation_inference.latest_task(db, domain_id)
    return _task_row(task) if task else None


@router.get(
    "/domains/{domain_id}/relation-candidates",
    response_model=list[RelationCandidateRow],
)
def list_relation_candidates(
    domain_id: str,
    verdict: str | None = None,
    state: str | None = None,
    with_pairs: bool = False,
    db: Session = Depends(get_db),
):
    """列该域的关系候选（按置信度降序）。``with_pairs=true`` 才展开两两关系明细。"""
    rows = relation_inference.list_candidates(
        db, domain_id, verdict=verdict, state=state
    )
    return [_candidate_row(row, with_pairs=with_pairs) for row in rows]


@router.post(
    "/relation-candidates/{candidate_id}/decide", response_model=RelationCandidateRow
)
def decide_relation_candidate(
    candidate_id: str, body: DecideRequest, db: Session = Depends(get_db)
):
    """人工表态：整族接受或整族否决。"""
    try:
        row = relation_inference.decide(
            db, candidate_id, state=body.state, operator=body.operator
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _candidate_row(row, with_pairs=True)


@router.post(
    "/domains/{domain_id}/relations/apply", response_model=RelationApplyReceipt
)
async def apply_relation_candidates(
    domain_id: str, body: RelationApplyRequest, db: Session = Depends(get_db)
):
    """将已确认键族写入 DataHub schemaMetadata.foreignKeys。"""
    try:
        receipt = await relation_inference.apply_confirmed_to_datahub(
            db, domain_id, candidate_ids=body.candidate_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RelationApplyReceipt(**receipt)


# --------------------------------------------------------------------------- 代码包


@router.get("/domains/{domain_id}/packages", response_model=list[PackageRow])
async def list_packages(
    domain_id: str,
    kind: str = "scan",
    include_inventory: bool = True,
    db: Session = Depends(get_db),
):
    """代码包历史。``kind=all`` 连画布补录的留档一起列。"""
    # The history is local data and should be usable while the DataHub
    # inventory is still loading.  Existing callers keep the accurate default;
    # the workbench opts into the fast local-only phase explicitly.
    isolated = await _isolated_names(db, domain_id) if include_inventory else set()
    stmt = (
        select(LineagePackage)
        .where(LineagePackage.domain_context_id == domain_id)
        .order_by(LineagePackage.uploaded_at.desc())
    )
    if kind != "all":
        stmt = stmt.where(LineagePackage.kind == kind)
    # ``_package_row`` needs edge statistics.  Eager-load all packages in one
    # relationship query to avoid one SELECT per package (the page lists the
    # whole history on first paint).
    stmt = stmt.options(selectinload(LineagePackage.edges))
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
async def get_package(
    package_id: str,
    include_inventory: bool = True,
    db: Session = Depends(get_db),
):
    package = db.get(LineagePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="代码包不存在")
    isolated = (
        await _isolated_names(db, package.domain_context_id)
        if include_inventory
        else set()
    )
    return _package_detail(package, isolated)


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
