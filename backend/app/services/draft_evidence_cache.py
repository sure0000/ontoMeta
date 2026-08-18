"""预生成 evidence 证据包的跨进程磁盘缓存。

DataHub 抓取是预生成的分钟级瓶颈（ERP 域曾达 ~7 分钟），而草稿 worker 是独立子
进程 → 进程内内存缓存对「失败后续跑」无效，必须落盘。缓存以 ``domain_context_id``
为键，带 **TTL** 与 **fingerprint**（datahub 域 id）双重失效：过期或源域绑定变更即
视为 miss，回退实时抓取，绝不喂过期证据。``EvidenceBundle`` 是 Pydantic 模型，直接
JSON 序列化/反序列化。

任何缓存异常（读写失败、JSON 损坏）都降级为 miss——缓存只做加速，绝不成为新的失败源。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import settings
from app.schemas import EvidenceBundle

logger = logging.getLogger(__name__)


def _cache_dir() -> Path:
    path = Path(settings.draft_evidence_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file(domain_id: str) -> Path:
    # domain_id 是受控 UUID，直接作文件名安全；仍做一次基本清洗以防意外。
    safe = "".join(c for c in domain_id if c.isalnum() or c in "-_")
    return _cache_dir() / f"{safe}.json"


def load(domain_id: str, fingerprint: str) -> EvidenceBundle | None:
    """命中且未过期、fingerprint 一致时返回 evidence，否则 None（含禁用/异常）。"""
    ttl = settings.draft_evidence_cache_ttl_seconds
    if ttl <= 0:
        return None
    try:
        path = _cache_file(domain_id)
        if not path.exists():
            return None
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint:
            return None
        age = time.time() - float(payload.get("cached_at", 0))
        if age > ttl:
            return None
        evidence = EvidenceBundle.model_validate(payload["evidence"])
        logger.info(
            "draft evidence cache hit domain_id=%s age=%.0fs objects=%d",
            domain_id,
            age,
            len(evidence.object_types),
        )
        return evidence
    except Exception:
        logger.warning(
            "draft evidence cache load failed domain_id=%s (降级为 miss)",
            domain_id,
            exc_info=True,
        )
        return None


def save(domain_id: str, fingerprint: str, evidence: EvidenceBundle) -> None:
    """写入缓存；失败只告警不抛（缓存是加速项，不影响主流程）。"""
    if settings.draft_evidence_cache_ttl_seconds <= 0:
        return
    try:
        import json

        payload = {
            "cached_at": time.time(),
            "fingerprint": fingerprint,
            "evidence": evidence.model_dump(mode="json"),
        }
        path = _cache_file(domain_id)
        # 原子写：先写临时文件再 rename，避免续跑读到半截 JSON。
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        logger.info(
            "draft evidence cached domain_id=%s objects=%d",
            domain_id,
            len(evidence.object_types),
        )
    except Exception:
        logger.warning(
            "draft evidence cache save failed domain_id=%s", domain_id, exc_info=True
        )


def clear(domain_id: str) -> None:
    """删除某域的 evidence 缓存（源域重绑或强制全新生成时调用）。"""
    try:
        _cache_file(domain_id).unlink(missing_ok=True)
    except Exception:
        logger.warning(
            "draft evidence cache clear failed domain_id=%s", domain_id, exc_info=True
        )
