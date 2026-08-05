"""语义检索（P1.5）：让「往来单位」能召回「客户」。

**为什么存在**：`search_*` 原本纯 ILIKE，中文同义词一个都命不中——
prompt 里那句「检索关键词优先用**中文**（本体以中文命名，英文词多半命不中）」
就是这个缺陷倒逼出来的用户级补丁。真正的同义词召回只能靠嵌入，
没有任何本地字符串技巧能把「往来单位」对到「客户」上。

**混合检索**，不是用向量取代 ILIKE：
  1. ILIKE 命中排前 —— 字面精确是强信号，不该被语义相似度冲掉；
  2. 向量召回补在后 —— 专治同义词与近义表达；
  3. 按 id 去重，保持原有条数上限。

**降级是常态而非异常**：未配置嵌入服务、索引未建、调用失败——一律退回纯 ILIKE，
并在工具结果里**明说召回可能不全**，让模型知道「没搜到」不等于「不存在」。

规模取舍见 ``build_index``：只索引对象与业务逻辑，不索引关系与字段。
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.models import (
    BusinessLogic,
    EntityStatus,
    ObjectType,
    Ontology,
    SemanticIndexEntry,
)

logger = logging.getLogger("ontometa.semantic_search")

_PUB = EntityStatus.PUBLISHED.value
KIND_OBJECT = "object_type"
KIND_LOGIC = "business_logic"

# 嵌入服务的批大小：一次请求塞太多容易超时/超长
_EMBED_BATCH = 64

#: 嵌入函数签名：一批文本 → 一批等长向量。测试注入确定性实现，生产走 LLM 服务。
Embedder = Callable[[Sequence[str]], list[list[float]]]


@dataclass(frozen=True)
class SemanticHit:
    kind: str
    entity_id: str
    score: float
    text: str


# --------------------------------------------------------------------------- 向量


def _normalize(vec: Sequence[float], dim: int | None) -> list[float]:
    """截断到 dim 并做 L2 归一化。

    归一化后余弦相似度退化成点积，检索时省掉每条向量的模长计算。
    截断是 Matryoshka 式的：现代嵌入模型前若干维已承载主要语义，
    截短能让纯 Python 的暴力检索快数倍，而召回质量基本无损。
    """
    v = list(vec[:dim]) if dim and dim > 0 else list(vec)
    norm = math.sqrt(sum(x * x for x in v))
    if norm <= 0:
        return v
    return [x / norm for x in v]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


# --------------------------------------------------------------------------- 嵌入服务


def default_embedder(db: Session) -> Embedder | None:
    """按已配置的 LLM 服务构造嵌入函数。未配置嵌入模型时返回 None（→ 纯 ILIKE）。"""
    model = (getattr(env_settings, "agent_embedding_model", "") or "").strip()
    if not model:
        return None

    from app.services.settings_service import SettingsService

    runtime = SettingsService().get_llm_runtime(db)
    if not runtime.api_key:
        return None

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=runtime.api_key, base_url=runtime.api_base_url)
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            chunk = list(texts[i : i + _EMBED_BATCH])
            resp = client.embeddings.create(model=model, input=chunk)
            out.extend([d.embedding for d in resp.data])
        return out

    return _embed


# --------------------------------------------------------------------------- 建索引


def entity_text(name: str, display_name: str, extra: str | None = None) -> str:
    """参与嵌入的文本。名称与显示名都进去——两者可能一中一英，各自贡献召回面。"""
    parts = [p.strip() for p in (display_name, name, extra or "") if p and p.strip()]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return " ｜ ".join(seen)


def _collect(db: Session, ontology_id: str) -> list[tuple[str, str, str]]:
    """待索引实体 → [(kind, entity_id, text)]。

    **只索引对象与业务逻辑**，不索引关系与字段：
    - 关系名是从两端派生的公式化名称（「订单归属客户」），语义检索收益低，
      而「这两个对象怎么连」这个真实需求已由 `find_join_path`（P1.2）覆盖；
    - 字段量级比对象大一个数量级，而 Agent 的路径是先定位对象、再 `get_object` 看字段。
    这样一个域的索引量是**百到千级**，纯 Python 暴力检索毫秒级完成，无需 pgvector。
    """
    rows: list[tuple[str, str, str]] = []
    for o in (
        db.query(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id, ObjectType.status == _PUB)
        .all()
    ):
        rows.append((KIND_OBJECT, o.id, entity_text(o.name, o.display_name, o.description)))
    for l in (
        db.query(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology_id, BusinessLogic.status == _PUB)
        .all()
    ):
        rows.append(
            (KIND_LOGIC, l.id, entity_text(l.name, l.display_name, l.expression_summary))
        )
    return rows


def build_index(
    db: Session, ontology_id: str, *, embedder: Embedder | None = None
) -> int:
    """（重）建一个本体的语义索引，返回写入条数。0 表示未建（无嵌入服务或无实体）。

    幂等：先删该本体的旧条目再写新的。发布时调用（见 `publish.py`）。
    """
    ontology = db.get(Ontology, ontology_id)
    if ontology is None:
        return 0
    embed = embedder or default_embedder(db)
    if embed is None:
        logger.info("未配置嵌入模型，跳过语义索引构建（检索退回 ILIKE）")
        return 0

    rows = _collect(db, ontology_id)
    db.query(SemanticIndexEntry).filter(
        SemanticIndexEntry.ontology_id == ontology_id
    ).delete(synchronize_session=False)
    if not rows:
        db.commit()
        return 0

    try:
        vectors = embed([t for _, _, t in rows])
    except Exception as exc:  # noqa: BLE001 — 建索引失败不得阻断发布
        logger.warning("语义索引构建失败，检索退回 ILIKE：%s", exc)
        db.commit()
        return 0
    if len(vectors) != len(rows):
        logger.warning(
            "嵌入返回条数不匹配（%d != %d），放弃本次索引", len(vectors), len(rows)
        )
        db.commit()
        return 0

    dim = int(getattr(env_settings, "agent_embedding_dim", 256) or 0)
    model = (getattr(env_settings, "agent_embedding_model", "") or "").strip()
    for (kind, entity_id, text), vec in zip(rows, vectors):
        norm = _normalize(vec, dim)
        db.add(
            SemanticIndexEntry(
                ontology_id=ontology_id,
                ontology_version=ontology.version,
                kind=kind,
                entity_id=entity_id,
                text=text,
                vector_json=json.dumps(norm),
                model=model,
                dim=len(norm),
            )
        )
    db.commit()
    reset_cache()
    return len(rows)


# --------------------------------------------------------------------------- 检索


class _IndexCache:
    """按 (ontology_id, version) 缓存解好的向量。重新发布必然换键，自动失效。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple, list[tuple[str, str, list[float], str]]] = {}

    def get(self, key: tuple):
        with self._lock:
            return self._data.get(key)

    def put(self, key: tuple, value) -> None:
        with self._lock:
            self._data[key] = value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CACHE = _IndexCache()


def reset_cache() -> None:
    _CACHE.clear()


def _load(db: Session, ontology_id: str, version: int):
    key = (ontology_id, version)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    entries = (
        db.query(SemanticIndexEntry)
        .filter(
            SemanticIndexEntry.ontology_id == ontology_id,
            SemanticIndexEntry.ontology_version == version,
        )
        .all()
    )
    loaded: list[tuple[str, str, list[float], str]] = []
    for e in entries:
        try:
            vec = json.loads(e.vector_json)
        except (TypeError, ValueError):
            continue
        if isinstance(vec, list) and vec:
            loaded.append((e.kind, e.entity_id, [float(x) for x in vec], e.text))
    _CACHE.put(key, loaded)
    return loaded


def search(
    db: Session,
    ontology: Ontology,
    query: str,
    *,
    kind: str,
    limit: int = 8,
    embedder: Embedder | None = None,
) -> list[SemanticHit]:
    """向量召回。索引不存在/无嵌入服务时返回空列表（调用方据此退回纯 ILIKE）。"""
    if not (query or "").strip():
        return []
    rows = [r for r in _load(db, ontology.id, ontology.version) if r[0] == kind]
    if not rows:
        return []
    embed = embedder or default_embedder(db)
    if embed is None:
        return []
    try:
        qvec = embed([query])[0]
    except Exception as exc:  # noqa: BLE001 — 检索失败不得拖垮问答
        logger.info("语义检索嵌入失败，退回 ILIKE：%s", exc)
        return []

    dim = int(getattr(env_settings, "agent_embedding_dim", 256) or 0)
    q = _normalize(qvec, dim)
    threshold = float(getattr(env_settings, "agent_embedding_min_score", 0.0) or 0.0)
    scored = [
        SemanticHit(kind=k, entity_id=eid, score=_dot(q, v), text=txt)
        for k, eid, v, txt in rows
    ]
    scored = [h for h in scored if h.score >= threshold]
    scored.sort(key=lambda h: -h.score)
    return scored[:limit]


def merge_hits(
    lexical_ids: Iterable[str], semantic: list[SemanticHit], limit: int
) -> list[str]:
    """混合排序：ILIKE 命中在前，向量召回补后，按 id 去重并截到 limit。

    **字面命中优先于语义相似**——用户打出的字面词是最强的意图信号，
    不该被一个分数更高的近义实体挤掉。
    """
    out: list[str] = []
    seen: set[str] = set()
    for eid in lexical_ids:
        if eid and eid not in seen:
            seen.add(eid)
            out.append(eid)
    for hit in semantic:
        if len(out) >= limit:
            break
        if hit.entity_id not in seen:
            seen.add(hit.entity_id)
            out.append(hit.entity_id)
    return out[:limit]


__all__ = [
    "Embedder",
    "SemanticHit",
    "KIND_OBJECT",
    "KIND_LOGIC",
    "build_index",
    "search",
    "merge_hits",
    "entity_text",
    "default_embedder",
    "reset_cache",
]
