# Alembic 迁移说明（ontoMeta Batch B2）

## 常用命令（在 backend/ 目录，已激活 venv）

```bash
alembic upgrade head          # 应用到最新
alembic current               # 当前版本
alembic history               # 历史
alembic revision --autogenerate -m "描述"   # 根据 models 生成新迁移（需审阅）
```

应用启动时会自动执行 `upgrade head`；遗留库（有表无 `alembic_version`）会自动 `stamp head`。

## 空库

直接 `alembic upgrade head` 或启动 uvicorn，即可得到与 models 一致的 schema。

## 旧 SQLite / 已有数据

1. **备份** `ontometa.db`（及 `-wal`/`-shm`）。
2. 确认 schema 已包含 B1 字段（如 `external_apps.api_key_hash`）。若不确定，先启动一次旧版本或对照 models。
3. 启动应用，或运行：`python scripts/alembic_stamp_legacy.py`
4. 之后的 schema 变更只通过新的 Alembic revision 演进。

极旧且缺列的库：不要 stamp；应备份后用空库 upgrade，再按需导入数据，或手写过渡 revision。

## 开发 vs 生产

| 环境 | DATABASE_URL 示例 |
|------|-------------------|
| 开发 | `sqlite:///./ontometa.db` |
| 生产 | `postgresql+psycopg://user:pass@host:5432/ontometa` |

生产请安装对应驱动（如 `psycopg[binary]`），并在部署流水线中于启动前执行 `alembic upgrade head`。
