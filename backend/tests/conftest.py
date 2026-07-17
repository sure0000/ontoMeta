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
os.environ["USE_MOCK_DATAHUB"] = "true"
os.environ["USE_MOCK_LLM"] = "true"
os.environ["DEBUG"] = "true"
# 避免本地 .env 中的真实 DataHub/LLM 干扰（env 已优先，此处显式清空可选）
os.environ.pop("OPENAI_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

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
