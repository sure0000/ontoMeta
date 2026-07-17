#!/usr/bin/env python3
"""将「无 alembic_version 的遗留库」标记为当前 head（不改表结构）。

适用：B2 之前用 create_all + 手写 ALTER 建好的 SQLite/PG，且 schema 已与
当前 models 一致。执行前请备份数据库。

用法（在 backend/ 目录）：
  source .venv/bin/activate
  python scripts/alembic_stamp_legacy.py

若库已有 alembic_version，本脚本会跳过。
应用启动时（init_db）也会自动 stamp，通常无需手动执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证可从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from app.database import _alembic_config, engine  # noqa: E402


def main() -> int:
    from alembic import command

    tables = set(inspect(engine).get_table_names())
    if "alembic_version" in tables:
        print("alembic_version already present; nothing to do")
        return 0
    if "domain_contexts" not in tables:
        print("No business tables found. Use: alembic upgrade head")
        return 1

    cfg = _alembic_config()
    command.stamp(cfg, "head")
    print("Stamped legacy database to Alembic head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
