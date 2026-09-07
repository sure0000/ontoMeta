"""DataHub 域元数据（原始 bundle）的落盘缓存。

**与 ``draft_evidence_cache`` 的分工**：那个缓存的是**组装后**的 ``EvidenceBundle``（已套源画像、
已改成候选对象名、已裁成 LLM 证据包）。键族聚类要的是**原始**的 ``DatasetInput``——物理表名、
物理列名、样例值、distinct、rowCount，一个都不能少，所以必须单独缓存一份原料。

**为什么非缓存不可**：``fetch_domain_bundle`` 为每张表拉 schema + profile + 样例值，
ERP 域曾达 ~7 分钟（见 ``draft_evidence_cache`` 与 ``lineage_inventory`` 的同款说明）。
「智能补充关系」是人点一下就要出结果的交互，不能每次等几分钟。

失效口径与 evidence 缓存一致：**TTL + fingerprint（datahub 域 id）双重失效**，
任何缓存异常（读写失败、JSON 损坏）一律降级为 miss——缓存只做加速，绝不成为新的失败源。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.config import settings
from app.schemas import DataHubDomainBundle

logger = logging.getLogger("ontometa.datahub_bundle_cache")


def _cache_dir() -> Path:
    path = Path(settings.datahub_bundle_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file(domain_id: str) -> Path:
    # domain_id 是受控 UUID，直接作文件名安全；仍做一次基本清洗以防意外。
    safe = "".join(c for c in domain_id if c.isalnum() or c in "-_")
    return _cache_dir() / f"{safe}.json"


def load(domain_id: str, fingerprint: str) -> DataHubDomainBundle | None:
    """命中且未过期、fingerprint 一致时返回 bundle，否则 None（含禁用/异常）。"""
    ttl = settings.datahub_bundle_cache_ttl_seconds
    if ttl <= 0:
        return None
    try:
        path = _cache_file(domain_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint:
            return None
        age = time.time() - float(payload.get("cached_at", 0))
        if age > ttl:
            return None
        bundle = DataHubDomainBundle.model_validate(payload["bundle"])
        logger.info(
            "datahub bundle 缓存命中 domain_id=%s age=%.0fs datasets=%d",
            domain_id,
            age,
            len(bundle.datasets),
        )
        return bundle
    except Exception:
        logger.warning(
            "datahub bundle 缓存读取失败 domain_id=%s（降级为 miss）",
            domain_id,
            exc_info=True,
        )
        return None


def save(domain_id: str, fingerprint: str, bundle: DataHubDomainBundle) -> None:
    """写入缓存；失败只告警不抛（缓存是加速项，不影响主流程）。"""
    if settings.datahub_bundle_cache_ttl_seconds <= 0:
        return
    try:
        payload = {
            "cached_at": time.time(),
            "fingerprint": fingerprint,
            "bundle": bundle.model_dump(mode="json"),
        }
        path = _cache_file(domain_id)
        # 原子写：先写临时文件再 rename，避免并发读到半截 JSON。
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        logger.info(
            "datahub bundle 已缓存 domain_id=%s datasets=%d",
            domain_id,
            len(bundle.datasets),
        )
    except Exception:
        logger.warning(
            "datahub bundle 缓存写入失败 domain_id=%s", domain_id, exc_info=True
        )


def clear(domain_id: str) -> None:
    """删除某域的 bundle 缓存（源域重绑或要求强制重抓时调用）。"""
    try:
        _cache_file(domain_id).unlink(missing_ok=True)
    except Exception:
        logger.warning(
            "datahub bundle 缓存清除失败 domain_id=%s", domain_id, exc_info=True
        )


async def fetch(db, domain, *, refresh: bool = False) -> tuple[DataHubDomainBundle, bool]:
    """取该域的 bundle，返回 ``(bundle, 是否命中缓存)``。

    ``refresh=True`` 强制重抓并回填。这是「二次点击秒回」的落点：第一次几分钟，
    之后在 TTL 内都是读一个 JSON 文件。
    """
    from app.connectors.datahub import DataHubConnector
    from app.services.settings_service import SettingsService

    fingerprint = domain.datahub_domain_id or "none"
    if not refresh:
        cached = load(domain.id, fingerprint)
        if cached is not None:
            return cached, True

    connector = DataHubConnector(SettingsService().get_datahub_runtime(db))
    try:
        bundle = await connector.fetch_domain_bundle(
            domain.datahub_domain_id, include_logic_evidences=False
        )
    finally:
        await connector.aclose()
    save(domain.id, fingerprint, bundle)
    return bundle, False
