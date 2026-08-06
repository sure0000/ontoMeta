"""运行轨迹落地（V4 O6.1）：对齐 pi 的 JSONL session 范式，而非新增 DB 表。

**为什么用 JSONL 而不是建表**：agent_telemetry 明写「刻意不落库」以免留 schema 债；
但生产 eval / 回放 / skill_misroute 又需要**持久**轨迹。pi 的做法（docs/session-format.md）
正是把每次运行落成 JSONL——一行一条，可回放、可分支、易摘除。本模块照搬：每问追加一行 JSON 到
``settings.agent_trace_dir``。改造收尾后删目录即可，零 schema 债、零迁移。

**默认关闭**（``agent_trace_enabled=False``）：开启才写文件；任何写失败都吞掉，绝不拖垮问答。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger("ontometa.agent_trace")


def _trace_path() -> Path:
    base = Path(settings.agent_trace_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    base.mkdir(parents=True, exist_ok=True)
    # 按天分文件，便于滚动清理
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return base / f"agent-trace-{day}.jsonl"


def write_trace(record: dict[str, Any]) -> None:
    """追加一行运行轨迹。开关关闭或写失败都静默返回——轨迹是观测增强，不是主链路。"""
    if not settings.agent_trace_enabled:
        return
    try:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _trace_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 —— 落轨迹失败绝不能影响回答
        logger.info("agent trace write skipped: %s", exc)


__all__ = ["write_trace"]
