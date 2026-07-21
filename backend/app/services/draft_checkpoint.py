"""草稿分块生成的按块检查点存储。

分块只对「对象命名增强」这一步生效：每个子块成功后把该块的命名结果(overrides
字典)落库；执行中途失败后重试可复用已完成子块、跳过重复 LLM 调用以节省 token。
检查点按数据域 + 块内容哈希寻址。

草稿的结构(对象/属性/关系)始终由证据确定性组装，不依赖检查点，故检查点只需
缓存 LLM 的命名结果(JSON 字典)。

存储方法为同步实现(SQLite/PG 皆快)，与代码库中「在 async 函数里做同步 DB
操作」的既有约定一致(参见 draft_task_service._update_task_progress)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def chunk_key(payload: str) -> str:
    """由块的规范化 payload 文本生成稳定内容哈希，作为检查点寻址键。

    内容敏感：证据一旦变化(字段/描述/关系变动)，键随之变化，旧检查点自然失效。
    """
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class DraftCheckpointStore:
    """按 (domain_context_id, chunk_key) 寻址的检查点存储(值为 JSON 字典)。"""

    def __init__(self, domain_context_id: str) -> None:
        self.domain_context_id = domain_context_id

    def load(self, key: str) -> dict[str, Any] | None:
        from app.database import SessionLocal
        from app.models import DraftChunkCheckpoint

        with SessionLocal() as db:
            row = (
                db.query(DraftChunkCheckpoint)
                .filter(
                    DraftChunkCheckpoint.domain_context_id == self.domain_context_id,
                    DraftChunkCheckpoint.chunk_key == key,
                )
                .first()
            )
            if row is None:
                return None
            return json.loads(row.output_json)

    def save(self, key: str, value: dict[str, Any]) -> None:
        from app.database import SessionLocal
        from app.models import DraftChunkCheckpoint

        payload = json.dumps(value, ensure_ascii=False)
        with SessionLocal() as db:
            row = (
                db.query(DraftChunkCheckpoint)
                .filter(
                    DraftChunkCheckpoint.domain_context_id == self.domain_context_id,
                    DraftChunkCheckpoint.chunk_key == key,
                )
                .first()
            )
            if row is None:
                db.add(
                    DraftChunkCheckpoint(
                        domain_context_id=self.domain_context_id,
                        chunk_key=key,
                        output_json=payload,
                    )
                )
            else:
                row.output_json = payload
            db.commit()

    def clear(self) -> None:
        from app.database import SessionLocal
        from app.models import DraftChunkCheckpoint

        with SessionLocal() as db:
            db.query(DraftChunkCheckpoint).filter(
                DraftChunkCheckpoint.domain_context_id == self.domain_context_id
            ).delete()
            db.commit()
