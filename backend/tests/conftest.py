"""测试环境：在导入应用前固定 env，使用独立 SQLite 文件。"""

from __future__ import annotations

import os
from pathlib import Path

# ---- 必须在 import app.* 之前设置 ----
_TEST_DB = Path(__file__).resolve().parent / "_test_ontometa.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_TEST_DB) + suffix) if suffix else _TEST_DB
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
os.environ["ONTOMETA_ADMIN_TOKEN"] = "test-admin-token"
os.environ["DEBUG"] = "true"
# 避免本地 .env 中的真实 DataHub/LLM 干扰：不配 api_key → 服务走确定性/报错路径，不发真实调用。
os.environ.pop("OPENAI_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

# 与 app 启动一致地建表：确保**不经 app 启动、直连 SessionLocal** 的用例也有全部表
# （env 已在上方固定，此处 import app.* 安全）。app 启动的 init_db 里 create_all 幂等，无冲突。
from app.database import Base, engine
import app.models  # noqa: F401 —— 注册所有模型到 Base.metadata
Base.metadata.create_all(bind=engine)

ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="session")
def client():
    from app.services.settings_service import SettingsService

    SettingsService._defaults_initialized = False

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    SettingsService._defaults_initialized = False


@pytest.fixture
def admin_headers():
    return dict(ADMIN_HEADERS)
