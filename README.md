# ontoMeta

建立在 DataHub 之上的企业级本体建模系统，实现「读取元数据 → 生成草稿 → 工作区编辑确认 → 发布本体」的完整闭环，并提供 Chat BI、对外 REST/MCP 与表达式编辑能力。

## 项目结构

```
ontoMeta/
├── backend/          # FastAPI 后端
│   ├── alembic/            # Schema 迁移
│   └── app/
│       ├── api/              # REST / MCP 路由
│       ├── connectors/       # DataHub 接入层
│       ├── models/           # SQLAlchemy 数据模型
│       ├── schemas/          # Pydantic 请求/响应模型
│       └── services/         # 核心业务服务
├── frontend/         # React 前端
├── docker-compose.yml        # Postgres + API + Frontend
├── Makefile          # 本地一键安装 / 启动 / 迁移 / 测试
└── *.md              # 产品与技术文档
```

## 安全模型（阶段性）

| 通道 | 鉴权 | 用途 |
|------|------|------|
| 管理 API `/api/*`（除下述） | `ONTOMETA_ADMIN_TOKEN`（`X-Admin-Token` 或 `Authorization: Bearer`） | 工作区、发布、设置、外部应用管理等 |
| 对外 REST `/api/v1/*` | 外部应用 API Key（`X-API-Key`） | 已发布本体只读查询 |
| MCP `/api/mcp*` | 同上 API Key | tools/list、tools/call |
| `GET /health` | 无 | 存活探针 |

完整 RBAC（读/编/审/发四层角色）尚未产品化；当前为 **管理 Token + 外部 App Key（含 scope / 限流）**。详见 [TECH_DESIGN.md](./TECH_DESIGN.md) §9、[OPTIMIZATION_PLAN.md](./OPTIMIZATION_PLAN.md)。

## 快速开始

### 方式一：Docker Compose（推荐体验完整栈）

```bash
# 可选：export ONTOMETA_ADMIN_TOKEN=your-token
docker compose up --build -d
```

- 前端：http://localhost:5180  
- API / OpenAPI：http://localhost:8000/docs  
- 健康检查：`GET http://localhost:8000/health`  
- 默认管理 Token：`dev-admin-token-change-me`（与 Compose 环境变量一致）  
- Compose 默认 `USE_MOCK_DATAHUB=true`、`USE_MOCK_LLM=true`，无需本机 DataHub/OpenAI

停止：`docker compose down`（数据卷 `ontometa_pg` 会保留）。

### 方式二：Make（本地开发）

```bash
make install    # 创建 backend/.venv、安装依赖、复制 .env；npm install
make backend    # 终端 1：后端 http://localhost:8000
make frontend   # 终端 2：前端 http://localhost:5180
make health     # 可选：确认后端存活
```

### 方式三：手动

#### 后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

本地无 DataHub / OpenAI 时，可在 `backend/.env` 中设置 `USE_MOCK_DATAHUB=true`（`.env.example` 默认 `false`）与 `USE_MOCK_LLM=true`（默认已是）。

**管理鉴权（必填）**：在 `backend/.env` 设置 `ONTOMETA_ADMIN_TOKEN`（见 `.env.example`）。前端打开「设置 → 管理鉴权」填入相同 Token，或设置 `VITE_ONTOMETA_ADMIN_TOKEN`。

- API 文档：http://localhost:8000/docs
- 健康检查：`GET http://localhost:8000/health` → `{"status":"ok","app":"ontoMeta"}`（无需 Token）

#### 前端

```bash
cd frontend
npm install
npm run dev
```

访问 http://localhost:5180（Vite 将 `/api` 与 `/health` 代理到 `:8000`）。

## 核心能力

| 模块 | 说明 |
|------|------|
| DataHub Connector | 读取数据域、数据集、字段、血缘、逻辑证据 |
| Evidence Builder | 整理 LLM 证据包 |
| Draft Generator | 分步生成本体草稿（对象、属性、关系、业务逻辑）；进程内排队与重启恢复 |
| Confirmation Service | 发布/删除等重要操作二次确认 |
| Publish Service | 草稿发布、版本 diff / 快照、发布前一致性校验 |
| Query Service | 本体、对象、业务逻辑查询（分页、图谱邻域展开） |
| Expression Formatter | 业务逻辑表达式富文本编辑与格式化 |
| Chat BI | 基于已发布本体的智能问数（须落地引用，无命中不编造） |
| External API / MCP | 应用 Key、scope、限流、调用日志；REST v1 与 MCP 目录同源 |

## 前端导航

- **本体浏览**：业务对象与关系（列表/卡片/图谱）
- **本体建模（工作区）**：按数据域组织建模任务，触发草稿生成与发布
- **业务逻辑**：指标、标签、规则；表达式编辑
- **智能问数（Chat BI）**：会话提问与本体引用
- **外部 API**：应用创建、MCP/REST 目录试用
- **设置**：管理鉴权、LLM 服务、DataHub 配置

## 配置

完整列表见 `backend/.env.example`。常用项：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `ONTOMETA_ADMIN_TOKEN` | 管理 API 共享 Token（必填） | — |
| `API_KEY_HASH_PEPPER` | 外部 API Key 哈希 pepper（可选） | — |
| `DATABASE_URL` | 开发 SQLite；生产 / Compose 用 PostgreSQL | `sqlite:///./ontometa.db` |
| `DEBUG` | `false` 时 500 响应脱敏 | `true` |
| `USE_MOCK_DATAHUB` | 使用 Mock 数据 | `false` |
| `USE_MOCK_LLM` | 使用规则引擎代替 LLM | `true` |
| `DATAHUB_GMS_URL` | DataHub GMS API 地址 | `http://localhost:8080` |
| `DATAHUB_FRONTEND_URL` | DataHub 前端地址 | `http://localhost:9002` |
| `DATAHUB_MAX_CONCURRENCY` | DataHub 拉取并发 | `5` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | LLM（也可在设置页配置） | — / `gpt-4o-mini` |
| `LLM_TIMEOUT_SECONDS` | LLM 超时 | `300` |
| `MAX_CONCURRENT_DRAFT_GENERATIONS` | 草稿生成并发上限 | `2` |
| `EXTERNAL_API_RATE_LIMIT_PER_MINUTE` | 外部 API/MCP 每应用默认每分钟上限 | `60` |

外部应用 API Key 仅在创建/重置时明文返回一次，库内只存 SHA-256 哈希与前缀。应用可配置 scope（`domains:read` / `objects:read` / `relations:read` / `logics:read`），缺权 403；超限 429。

### 数据库与迁移

- Schema 由 **Alembic** 管理（`backend/alembic/`）。启动时自动 `alembic upgrade head`。
- 开发：SQLite（`DATABASE_URL=sqlite:///./ontometa.db`）。
- 生产 / Compose：PostgreSQL（镜像内已装 `psycopg`）；本地连 PG 需自行 `pip install "psycopg[binary]"`。
- **旧库**（尚无 `alembic_version`）：启动时自动 `stamp head`，或 `python scripts/alembic_stamp_legacy.py`（先备份）。详见 [backend/alembic/README.md](./backend/alembic/README.md)。

### 测试与 CI

```bash
make test                 # 后端 pytest（Mock DataHub / LLM）
cd frontend && npm run lint && npm run build
```

GitHub Actions：`.github/workflows/ci.yml`（backend pytest + frontend lint/build）。

### MCP stdio（可选）

仓库另有 `backend/mcp_stdio_server.py`，与 HTTP `/api/mcp` 共用工具目录；配置见部署侧 MCP 客户端文档（`.env.mcp` 勿提交）。

## 文档

- [SSOT.md](./SSOT.md) — 项目 Single Source of Truth
- [PRD.md](./PRD.md) — 产品需求
- [TECH_DESIGN.md](./TECH_DESIGN.md) — 技术设计
- [DOMAIN_MODEL.md](./DOMAIN_MODEL.md) — 领域模型
- [IA.md](./IA.md) — 信息架构
- [OPTIMIZATION_PLAN.md](./OPTIMIZATION_PLAN.md) — 工程优化方案（可分批执行）
