"""代码包扫描与血缘上报。

**扫的是野包**：客户给的 SQL 代码包没有格式约定——目录随意、方言混杂、里面混着
存储过程和空文件。所以这一层的产出不是"解析成功了"，而是三个能给人看的结论：
能补哪些边、影响哪些表、哪些孤岛还是孤岛。解析失败的文件必须逐个记下来，
藏起来会让人以为"扫完了就全了"。

**上报只写表级边**：DataHub 的 ``updateLineage`` GraphQL 只收表级（见 lineage_emitter
的同一条说明），关联键留在本地库里——它是给关系推断用的证据，不是 DataHub 的东西。
同一对表的多条键因此**合成一条 DataHub 边**发送，不重复发。

**preview / apply 分离**：扫描只落库不写 DataHub；写是单独一个动作，且逐条记录失败，
单条失败不中断其余（与 M7 回写、M11 物化血缘同构）。
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors import datahub as dh
from app.models import DomainContext, LineagePackage, LineagePackageEdge
from app.services import lineage_inventory
from app.services.lineage_inventory import DomainInventory
from app.services.settings_service import SettingsService
from app.services.sql_lineage_extractor import NO_LANDING_HINT, extract

logger = logging.getLogger("ontometa.lineage_package")

#: 单个代码包的大小上限。再大多半是误传了整个仓库快照。
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
#: 单个 .sql 的大小上限：超过这个的通常是 dump 而不是建模脚本。
MAX_SQL_BYTES = 4 * 1024 * 1024

REASON_TARGET_UNRESOLVED = "落点表未在 DataHub 找到"
REASON_SOURCE_UNRESOLVED = "上游表未在 DataHub 找到"
REASON_OUT_OF_DOMAIN = "不在本域"


@dataclass
class ApplyReceipt:
    """一次上报的回执。``failures`` 非空表示部分边没写进去。"""

    applied: int = 0
    resolved: int = 0
    failed: int = 0
    failures: list[dict] = field(default_factory=list)


def _archive_dir() -> Path:
    base = settings.lineage_package_dir or str(Path(__file__).resolve().parents[2] / "data" / "lineage_packages")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _decode(raw: bytes) -> str:
    """SQL 文件的编码没有约定，UTF-8 不行就试 GB18030，再不行按替换字符读。"""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_sql(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith("/") or "__macosx" in lowered:
        return False
    return lowered.endswith(".sql") and not Path(name).name.startswith(".")


def _members(filename: str, blob: bytes) -> list[tuple[str, bytes]]:
    """递归取出包里所有 .sql。zip / tar.gz / 单个 .sql 都收。"""
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return [
                (info.filename, archive.read(info))
                for info in archive.infolist()
                if not info.is_dir()
                and _is_sql(info.filename)
                and info.file_size <= MAX_SQL_BYTES
            ]

    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            found: list[tuple[str, bytes]] = []
            for member in archive.getmembers():
                if not member.isfile() or not _is_sql(member.name):
                    continue
                if member.size > MAX_SQL_BYTES:
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    found.append((member.name, handle.read()))
            return found
    except tarfile.TarError:
        pass

    if _is_sql(filename):
        return [(filename, blob)]
    raise ValueError("包里没有找到 .sql 文件（支持 .zip / .tar.gz / 单个 .sql）")


@dataclass
class _ScanOutcome:
    sql_files: int = 0
    directories: int = 0
    statements: int = 0
    parsed_files: int = 0
    failures: list[dict] = field(default_factory=list)
    #: (source_table, target_table, join_key, source_file)
    edges: list[tuple[str, str, str | None, str]] = field(default_factory=list)


def _scan_members(members: list[tuple[str, bytes]], dialect: str) -> _ScanOutcome:
    outcome = _ScanOutcome(sql_files=len(members))
    outcome.directories = len({str(Path(name).parent) for name, _ in members})
    seen: set[tuple[str, str, str | None]] = set()

    for name, raw in members:
        result = extract(_decode(raw), dialect=dialect)
        if result.error:
            outcome.failures.append(
                {"file": name, "reason": result.error, "kind": "parse_error"}
            )
            continue

        outcome.parsed_files += 1
        outcome.statements += result.statements
        if not result.lineages:
            # 不是解析失败，是这份文件里没有落点（纯查询、纯 UPDATE）。
            # 单独一类：混进"解析失败"会让「解析成功 + 失败」对不上文件总数。
            outcome.failures.append(
                {"file": name, "reason": NO_LANDING_HINT, "kind": "no_landing"}
            )
            continue

        for lineage in result.lineages:
            keys_by_source: dict[str, list[str]] = {}
            for key in lineage.join_keys:
                for table in (key.left_table, key.right_table):
                    if table in lineage.sources:
                        keys_by_source.setdefault(table, []).append(key.render())
            for source in lineage.sources:
                keys = keys_by_source.get(source) or [None]
                for key in keys:
                    signature = (source, lineage.target, key)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    outcome.edges.append((source, lineage.target, key, name))

    return outcome


def _classify(
    inventory: DomainInventory, source: str, target: str
) -> tuple[str, str | None, str | None, str | None]:
    """给一条边定 URN 与状态：(state, reason, source_urn, target_urn)。"""
    source_urn = inventory.resolve(source)
    target_urn = inventory.resolve(target)

    if target_urn is None:
        if not inventory.in_domain(target):
            return "skipped", f"{REASON_OUT_OF_DOMAIN}（{target}）", source_urn, None
        return "blocked", REASON_TARGET_UNRESOLVED, source_urn, None

    if source_urn is None:
        if not inventory.in_domain(source):
            return "skipped", f"{REASON_OUT_OF_DOMAIN}（{source}）", None, target_urn
        return "blocked", REASON_SOURCE_UNRESOLVED, None, target_urn

    return "ok", None, source_urn, target_urn


async def scan(
    db: Session,
    *,
    domain_id: str,
    filename: str,
    blob: bytes,
    dialect: str = "mysql",
) -> LineagePackage:
    """扫一个新上传的包：落盘归档 → 解析 → 对 URN → 落库。不写 DataHub。"""
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"代码包超过 {MAX_ARCHIVE_BYTES // 1024 // 1024} MB 上限")
    if db.get(DomainContext, domain_id) is None:
        raise ValueError("数据域不存在")

    members = _members(filename, blob)
    package = LineagePackage(
        domain_context_id=domain_id,
        name=filename,
        kind="scan",
        size_bytes=len(blob),
        dialect=dialect,
    )
    db.add(package)
    db.flush()

    archive_path = _archive_dir() / f"{package.id}-{Path(filename).name}"
    archive_path.write_bytes(blob)
    package.archive_path = str(archive_path)

    await _rescan_into(db, package, members)
    db.commit()
    db.refresh(package)
    return package


async def rescan(db: Session, package_id: str, *, dialect: str | None = None) -> LineagePackage:
    """用当前解析器重扫原包。归档丢了就明说，不假装扫过。"""
    package = db.get(LineagePackage, package_id)
    if package is None:
        raise ValueError("代码包不存在")
    if package.kind != "scan":
        raise ValueError("画布补录的记录不能重扫")
    if not package.archive_path or not Path(package.archive_path).exists():
        raise ValueError("原包归档已不在服务端，无法重扫；请重新上传")

    if dialect:
        package.dialect = dialect
    blob = Path(package.archive_path).read_bytes()
    members = _members(package.name, blob)

    # 已上报的边不能被重扫抹掉：DataHub 那边已经有了，本地记录得留着
    for edge in list(package.edges):
        if edge.applied_at is None:
            db.delete(edge)
    db.flush()

    await _rescan_into(db, package, members)
    db.commit()
    db.refresh(package)
    return package


async def _rescan_into(
    db: Session, package: LineagePackage, members: list[tuple[str, bytes]]
) -> None:
    outcome = _scan_members(members, package.dialect)
    inventory = await lineage_inventory.get_inventory(db, package.domain_context_id)

    applied = {
        (edge.source_table, edge.target_table, edge.join_key)
        for edge in package.edges
        if edge.applied_at is not None
    }

    for source, target, key, source_file in outcome.edges:
        if (source, target, key) in applied:
            continue
        state, reason, source_urn, target_urn = _classify(inventory, source, target)
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                source_table=source,
                target_table=target,
                join_key=key,
                source_file=source_file,
                source_urn=source_urn,
                target_urn=target_urn,
                state=state,
                reason=reason,
            )
        )

    package.sql_files = outcome.sql_files
    package.directories = outcome.directories
    package.statements = outcome.statements
    package.parsed_files = outcome.parsed_files
    package.failures_json = json.dumps(outcome.failures, ensure_ascii=False)
    package.scanned_at = datetime.now()
    db.flush()


def delete(db: Session, package_id: str) -> None:
    """删掉一条代码包记录与它的归档。

    **不会撤销已上报的血缘**：DataHub 那边的边独立存在，删本地记录只是不再留档。
    要撤边得去 DataHub（``updateLineage`` 的 edgesToRemove），本页不做——撤边比补边
    危险得多，得有人明确要求才该实现。
    """
    package = db.get(LineagePackage, package_id)
    if package is None:
        raise ValueError("代码包不存在")
    if package.archive_path:
        Path(package.archive_path).unlink(missing_ok=True)
    db.delete(package)
    db.commit()


async def apply(
    db: Session, package_id: str, *, targets: list[str] | None = None
) -> ApplyReceipt:
    """把包里选中的边写进 DataHub。

    同一对表的多条关联键合成一条 DataHub 边（那边只收表级）；逐条记录失败，
    单条失败不中断其余。
    """
    package = db.get(LineagePackage, package_id)
    if package is None:
        raise ValueError("代码包不存在")

    pending = [
        edge
        for edge in package.edges
        if edge.state == "ok"
        and edge.applied_at is None
        and edge.source_urn
        and edge.target_urn
        and (targets is None or edge.target_table in targets)
    ]
    if not pending:
        return ApplyReceipt()

    inventory = await lineage_inventory.get_inventory(db, package.domain_context_id)
    isolated_before = {table.urn for table in inventory.isolated_tables}

    receipt = await _write_edges(db, package.domain_context_id, pending, isolated_before)

    package.applied_edges += receipt.applied
    package.applied_resolved += receipt.resolved
    package.applied_at = datetime.now()
    remaining = [
        edge for edge in package.edges if edge.state == "ok" and edge.applied_at is None
    ]
    package.status = "applied" if not remaining else "partial"
    db.commit()
    lineage_inventory.invalidate(package.domain_context_id)
    return receipt


async def apply_manual(
    db: Session, *, domain_id: str, edges: list[dict], label: str | None = None
) -> ApplyReceipt:
    """把画布上连的边写进 DataHub，并作为一条 ``kind=manual`` 的记录留档。

    留档是为了回答"这个域的血缘是谁补的"——DataHub 那边只有边本身，没有来源；
    关联键更是只有本地存得住（GraphQL 只收表级边）。
    """
    if db.get(DomainContext, domain_id) is None:
        raise ValueError("数据域不存在")
    if not edges:
        return ApplyReceipt()

    inventory = await lineage_inventory.get_inventory(db, domain_id)
    isolated_before = {table.urn for table in inventory.isolated_tables}

    package = LineagePackage(
        domain_context_id=domain_id,
        name=label or f"画布补录 {datetime.now():%Y-%m-%d %H:%M}",
        kind="manual",
        dialect="manual",
        status="scanned",
        scanned_at=datetime.now(),
    )
    db.add(package)
    db.flush()

    rows: list[LineagePackageEdge] = []
    for item in edges:
        source = str(item.get("source_table") or "").strip()
        target = str(item.get("target_table") or "").strip()
        if not source or not target or source == target:
            continue
        keys = [str(key) for key in (item.get("join_keys") or []) if str(key).strip()]
        state, reason, source_urn, target_urn = _classify(inventory, source, target)
        for key in keys or [None]:
            row = LineagePackageEdge(
                package_id=package.id,
                source_table=source,
                target_table=target,
                join_key=key,
                source_file="画布",
                source_urn=source_urn,
                target_urn=target_urn,
                state=state,
                reason=reason,
            )
            db.add(row)
            rows.append(row)
    db.flush()

    writable = [row for row in rows if row.state == "ok" and row.source_urn and row.target_urn]
    receipt = await _write_edges(db, domain_id, writable, isolated_before)

    package.applied_edges = receipt.applied
    package.applied_resolved = receipt.resolved
    package.applied_at = datetime.now()
    package.status = "applied" if receipt.failed == 0 else "partial"
    db.commit()
    lineage_inventory.invalidate(domain_id)
    return receipt


async def _write_edges(
    db: Session,
    domain_id: str,
    edges: list[LineagePackageEdge],
    isolated_before: set[str],
) -> ApplyReceipt:
    """真正向 DataHub 写表级边。按 (上游 URN, 下游 URN) 去重后逐条发。"""
    receipt = ApplyReceipt()
    pairs: dict[tuple[str, str], list[LineagePackageEdge]] = {}
    for edge in edges:
        pairs.setdefault((edge.source_urn or "", edge.target_urn or ""), []).append(edge)

    connector = dh.DataHubConnector(SettingsService().get_datahub_runtime(db))
    written_targets: set[str] = set()
    try:
        for (source_urn, target_urn), group in pairs.items():
            try:
                ok = await dh.add_lineage_edge(connector, source_urn, target_urn)
            except dh.DataHubWriteError as exc:
                ok = False
                receipt.failures.append(
                    {"source": source_urn, "target": target_urn, "error": str(exc)}
                )
            except Exception as exc:  # noqa: BLE001 — 单条失败不中断其余
                ok = False
                receipt.failures.append(
                    {"source": source_urn, "target": target_urn, "error": str(exc)}
                )

            if not ok:
                receipt.failed += len(group)
                continue

            stamped = datetime.now()
            for edge in group:
                edge.applied_at = stamped
            receipt.applied += len(group)
            written_targets.add(target_urn)
    finally:
        await connector.aclose()

    receipt.resolved = len(written_targets & isolated_before)
    db.flush()
    logger.info(
        "域 %s 上报血缘：成功 %d 条 · 失败 %d 条 · %d 张表脱离孤岛",
        domain_id,
        receipt.applied,
        receipt.failed,
        receipt.resolved,
    )
    return receipt
