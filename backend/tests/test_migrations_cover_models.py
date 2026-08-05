"""迁移完整性：``alembic upgrade head`` 建出的库必须覆盖全部模型表。

**为什么需要这条**：测试库是靠 ``Base.metadata.create_all`` 建的（见 conftest），所以少写一个
迁移测试也照样全绿——``governance_standard_records`` 就是这么漏掉的：模型有、测试有、迁移没有，
于是按 alembic 升级上来的真实环境里那张表压根不存在，``active_standard(db)`` 一查就
OperationalError，Data Agent 的 lint_against_standard 直接报「工具异常」。

这条用例在**独立的临时库**上真跑一遍 alembic，再把结果与模型元数据对账，堵住这个盲区。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

_BACKEND = Path(__file__).resolve().parent.parent


def test_alembic_head_creates_every_model_table(tmp_path):
    from app.database import Base
    import app.models  # noqa: F401 — 触发全部模型注册到 metadata

    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": url, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade head 失败：\n{result.stderr}"

    engine = sa.create_engine(url)
    try:
        actual = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = sorted(set(Base.metadata.tables) - actual)
    assert not missing, f"这些模型表没有对应迁移，真实环境升级后会缺表：{missing}"
