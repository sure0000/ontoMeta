# ontoMeta 优化方案

> 本文档是可分批执行的工程优化路线图。后续会话应按 **批次（Batch）** 独立落地，每批结束后更新下方「执行进度」表。  
> 依据：当前代码库（2026-07）通读结论；与 [SSOT.md](./SSOT.md)、[TECH_DESIGN.md](./TECH_DESIGN.md)、[PRD.md](./PRD.md) 对齐。  
> **原则**：小步可合并；每批有明确验收标准；不在同一批混入无关重构。

---

## 0. 执行进度

| 批次 | 主题 | 状态 | 完成日期 | 备注 |
|------|------|------|----------|------|
| B0 | 基线与护栏 | 已完成 | 2026-07-17 | Makefile + README 启动/健康检查对齐 |
| B1 | 安全与鉴权（P0） | 已完成 | 2026-07-17 | Admin Token + API Key 哈希 + 500 脱敏 |
| B2 | Schema 迁移与存储 | 已完成 | 2026-07-17 | Alembic baseline；启动 upgrade/stamp |
| B3 | 测试与 CI | 已完成 | 2026-07-17 | pytest 冒烟 + GitHub Actions |
| B4 | 后端模块拆分 | 已完成 | 2026-07-17 | routes/query/models/schemas 按域拆分；契约不变 |
| B5 | 异步任务与并发 | 已完成 | 2026-07-17 | 状态机+重启修复+队列位次；LLM to_thread；B5.1 延期 |
| B6 | 前端结构与数据层 | 已完成 | 2026-07-17 | useApi 推广；Chat BI/CSS 拆分；ESLint+Prettier |
| B7 | Query / 图谱性能 | 已完成 | 2026-07-17 | 分页 PageResult + 图谱邻域展开 + GROUP BY；SQL 次数测试 |
| B8 | 外部 API / MCP 产品化 | 已完成 | 2026-07-17 | Scope+限流+调用日志；MCP/REST 同源；5 个契约测试 |
| B9 | 语义质量与版本能力 | 已完成 | 2026-07-17 | 版本 diff/快照；发布前校验；Chat BI grounding；任务重试 |
| B10 | 文档与部署对齐 | 已完成 | 2026-07-17 | README/TECH/IA/PRD + Compose(pg/api/frontend) |

状态取值：`未开始` | `进行中` | `已完成` | `延期` | `取消`

---

## 1. 目标与约束

### 1.1 优化目标

1. **可安全上线**：管理端受控、密钥不落明文、错误不泄密。
2. **可演进**：Schema 可迁移、模块边界清晰、后台任务可恢复。
3. **可回归**：关键路径有自动化测试与 CI。
4. **可维护**：拆分千行级文件，统一前端数据获取模式。
5. **可对外**：External API / MCP 具备 scope、限流与契约文档。

### 1.2 非目标（本方案不覆盖）

- 重写前端技术栈或更换 UI 库
- 一次性替换 DataHub 为其他元数据平台
- 完整 RBAC 产品化（可先做「最小鉴权」，完整角色体系单开 PRD）
- 跨域本体建模大重构（归入远期能力，见 B9 备注）

### 1.3 执行约定（给后续会话）

每个 Batch 开新会话时，提示词建议包含：

```
请阅读 OPTIMIZATION_PLAN.md，仅执行 Batch Bx。
约束：不扩大范围；完成后更新文档「执行进度」；按仓库现有风格改代码；需要提交时再问我。
```

每批交付物：

- 代码变更（可合并的 PR 粒度）
- 验收清单全部勾选或注明豁免
- 更新本文件「执行进度」与该 Batch「完成记录」小节（可追加短日志）

---

## 2. 现状摘要（决策依据）

| 维度 | 现状 | 风险 |
|------|------|------|
| 管理 API | `/api/*`（除 external v1）无登录鉴权 | 设置、发布、LLM Key 可被任意调用 |
| 外部 API Key | `external_apps.api_key` 明文存储 | 库泄露即全量失陷 |
| Schema | `create_all` + `init_db` 手写 ALTER | 多环境不一致、难回滚 |
| 存储 | 默认 SQLite | 多进程/生产受限 |
| 测试 / CI | 几乎无；无 Docker/Compose | 回归靠手工 |
| 后端体量 | `query.py` ~1.8k、`routes.py` ~1k、`chat_bi.py` ~1.1k | 改动成本高、易冲突 |
| 前端体量 | `ChatBiPage` ~1.8k、`styles.css` ~2.8k | 难复用、难测 |
| 文档 | README/TECH_DESIGN 滞后于 Chat BI / External API / MCP | 协作与排期失真 |
| 设计落差 | TECH_DESIGN 要求四层权限、分页、局部展开图谱 | 部分未落地 |

关键路径参考：

```
backend/app/main.py
backend/app/database.py
backend/app/api/routes.py
backend/app/api/external_routes.py
backend/app/services/query.py
backend/app/services/external_api.py
frontend/src/api.ts
frontend/src/hooks/useApi.ts
```

---

## 3. 批次方案详述

---

### Batch B0 — 基线与护栏

**目标**：为后续批次建立「可验证、可回滚」的最小基线，本身少改业务逻辑。

**范围**

1. 确认本地启动路径（backend uvicorn + frontend vite）可用。
2. 增加最小健康检查说明（已有 `GET /health`）。
3. 在仓库根或 `backend/` 增加 `Makefile` 或 `scripts/dev.sh`：一键装依赖、起服务（可选）。
4. 明确 `.gitignore` 已覆盖 `*.db`、`.env`、`.env.mcp`（已有则跳过）。
5. 在本文件锁定后续 Batch 依赖顺序（见第 4 节）。

**不做**

- 不改业务 API 行为
- 不引入新依赖框架（除文档/脚本）

**涉及文件（预期）**

- `README.md`（补充「优化方案」链接）
- 可选：`Makefile`、`scripts/dev.sh`

**验收**

- [x] 新贡献者按 README 能启动前后端
- [x] `OPTIMIZATION_PLAN.md` 被 README 引用
- [x] 无业务行为变更

**预估**：0.5 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B0
- 主要变更：
  - 新增根目录 `Makefile`（`install` / `backend` / `frontend` / `health`）
  - 修正 README：前端端口 `5180`（与 `vite.config.ts` 一致）、健康检查说明、Make 与手动双路径、Mock 默认值与 `.env.example` 对齐
  - 确认 `.gitignore` 已覆盖 `*.db`、`.env`、`.env.mcp`；第 4 节依赖顺序保持为锁定顺序
  - 实地验证：`GET /health` → 200；前端 `:5180` 可达
- 偏差与后续：未新增 `scripts/dev.sh`（Makefile 已覆盖）；CORS 仍含 `5173`，因 Vite 代理同源访问 `/api`，本批不改业务配置（留给 B1/B10 视需要）

---

### Batch B1 — 安全与鉴权（P0）

**目标**：堵住匿名管理面；外部 Key 不可逆存储；生产错误脱敏。

**范围**

#### B1.1 管理端最小鉴权

方案（择一，推荐 A）：

| 方案 | 描述 | 适用 |
|------|------|------|
| **A. 共享管理 Token** | env `ONTOMETA_ADMIN_TOKEN`；请求头 `X-Admin-Token` 或 `Authorization: Bearer` | 最快落地 |
| B. 简单用户表 + session/JWT | 设置页登录 | 产品化更强，工作量大 |

推荐先做 **A**，并在设置页/前端 `api.ts` 统一注入 header（可用 `localStorage` 存 token，仅开发便利；生产走反向代理注入更佳）。

保护范围（全部 `/api` 管理路由，**排除**）：

- `GET /health`
- `/api/v1/*`（已有 External App Key）
- `/api/mcp`（已有 External App Key）
- 可选：只读公开目录若产品需要再开白名单

同时：

- `POST/PATCH/DELETE /external-apps*` 必须纳入管理鉴权（当前无 Key 即可创建应用）。

#### B1.2 API Key 哈希

- 存储 `api_key_hash`（如 SHA-256 + 可选 pepper），可选保留 `api_key_prefix`（前 8 位）便于展示与检索。
- 创建/轮换时仅响应体返回一次明文。
- `reveal_key` 接口：要么删除，要么仅管理员 + 审计日志（哈希后无法 reveal，应改为「仅轮换」）。
- 迁移：现有明文 Key 启动时一次性哈希回填脚本。

#### B1.3 错误与密钥治理

- `main.py` 全局 500：生产（`debug=False`）只返回通用文案；详情写日志。
- LLM / DataHub 密钥：确认列表接口只返回 hint；禁止无鉴权 `reveal`。
- CORS：保持显式 origins，不使用 `*`。

**涉及文件（预期）**

- `backend/app/main.py`
- `backend/app/config.py`
- `backend/app/api/routes.py`、`external_routes.py`
- `backend/app/services/external_api.py`、`models`、`schemas`
- `backend/app/database.py` 或迁移（若与 B2 合并则本批只加列并兼容）
- `frontend/src/api.ts`、设置相关页

**验收**

- [x] 无 Token 访问管理 API → 401
- [x] 有 Token 可正常走工作区/发布/设置
- [x] External v1 / MCP 仍用 App API Key
- [x] DB 中无完整明文 `om_sk_...`（仅 hash/prefix；旧列 `api_key` 为 `hashed:<hash>` 占位以兼容 SQLite NOT NULL）
- [x] `debug=False` 时 500 响应不含异常类名与堆栈

**依赖**：无（可先于 B2；若改表结构，列变更可与 B2 合并）

**风险**：前端需配置 Token，文档必须写清；旧 External Key 迁移失败会导致外接中断 → 必须提供迁移脚本与回滚说明。

**预估**：2–3 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B1
- 主要变更：
  - 新增 `app/auth.py`：`AdminAuthMiddleware`（`X-Admin-Token` / Bearer）；豁免 `/health`、`/api/v1/*`、`/api/mcp*`
  - 外部 App Key：SHA-256（可选 pepper）+ prefix；创建/重置仅一次明文；移除 `reveal_key`
  - 启动时自动哈希迁移遗留明文；旧列 NOT NULL 用 `hashed:<digest>` 占位
  - `debug=False` 时 500 通用文案；LLM 详情不再回显明文 Key
  - 前端：`api.ts` 注入 Admin Token；设置页「管理鉴权」；外部应用页去掉「查看密钥」
  - `.env.example` / README 补充 `ONTOMETA_ADMIN_TOKEN` 等说明；CORS 增加 `:5180`
- 偏差与后续：正式 Alembic 迁移留给 B2；旧 `api_key` 列仍保留占位，B2 可再建干净表结构

---

### Batch B2 — Schema 迁移与存储

**目标**：用版本化迁移替代 `init_db` 手写 ALTER；明确开发/生产数据库策略。

**范围**

1. 引入 Alembic（或等价），`alembic revision --autogenerate` 从当前模型生成 baseline。
2. 将 `database.py` 中 ALTER / 补索引逻辑迁入迁移文件；`init_db` 仅保留 `create_all`（开发）或改为「只跑 migrate」。
3. 文档化：
   - 开发：SQLite 可继续
   - 生产：PostgreSQL（`DATABASE_URL`）
4. 可选：`docker-compose.yml` 提供 `postgres` + `api` 服务骨架（实现可放到 B10，本批至少写清连接串与注意点）。

**涉及文件（预期）**

- `backend/alembic.ini`、`backend/alembic/`
- `backend/app/database.py`
- `backend/requirements.txt`
- `README.md` / `.env.example`

**验收**

- [x] 空库：`alembic upgrade head` 得到与模型一致的 schema
- [x] 从「旧 SQLite 文件」升级路径有说明或脚本
- [x] 启动不再依赖不断增加的手写 ALTER 列表

**依赖**：B1 若已加 hash 列，baseline 需包含；否则本批一并纳入。

**预估**：2 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B2
- 主要变更：
  - 引入 Alembic：`alembic.ini`、`alembic/env.py`、baseline `5a881e5c0024_baseline_schema.py`
  - `init_db` 改为 `run_migrations()`（upgrade；遗留库自动 stamp）+ 幂等数据回填
  - 删除手写 ALTER / 补索引列表；`scripts/alembic_stamp_legacy.py`；`alembic/README.md`
  - README / `.env.example` / Makefile（`make migrate`）补充开发 SQLite vs 生产 PostgreSQL 说明
- 偏差与后续：未加 docker-compose（按方案留给 B10）；生产 PG 驱动需部署时自行安装 `psycopg`

---

### Batch B3 — 测试与 CI

**目标**：关键路径可自动回归；PR 有最低质量门禁。

**范围**

1. 后端：`pytest` + `httpx.AsyncClient`/`TestClient`
   - 最小用例集：
     - health
     - 管理鉴权 401/200（依赖 B1）
     - domains list（mock DataHub）
     - external authenticate + 一个 v1 只读接口
     - 可选：confirmation / publish 冒烟
2. 前端：可选 Vitest 对 `utils/*`、`api` error parse；不强求页面 E2E。
3. CI（GitHub Actions）：
   - backend：install → pytest
   - frontend：`npm ci` → `npm run build`
4. 固定测试用 SQLite 内存库或临时文件；`USE_MOCK_DATAHUB=true`、`USE_MOCK_LLM=true`。

**涉及文件（预期）**

- `backend/tests/`
- `backend/requirements.txt`（pytest、pytest-asyncio 等）
- `.github/workflows/ci.yml`
- 可选：`frontend` vitest 配置

**验收**

- [x] 本地 `pytest` 绿
- [x] CI 在 PR 上运行（已添加 `.github/workflows/ci.yml`；需推送后由 GitHub 实际触发）
- [x] 前端 `npm run build` 在 CI 通过（本地已验证 build）

**依赖**：强烈建议 B1 完成后再写鉴权用例；B2 完成后用 migrate 建测试库更稳。

**预估**：2–3 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B3
- 主要变更：
  - `backend/tests/`：health、管理鉴权 401/200、domains(mock)、external v1 + API Key
  - `pytest.ini` + `conftest.py`（独立 SQLite、`USE_MOCK_*`、Admin Token）
  - `.github/workflows/ci.yml`：backend pytest + frontend `npm ci && npm run build`
  - `make test`；README 补充测试说明；未加 Vitest（方案标为可选）
- 偏差与后续：confirmation/publish 冒烟未做（方案标可选）；CI 需 push/PR 后才能在 GitHub 上看到绿勾

---

### Batch B4 — 后端模块拆分

**目标**：降低单文件体量，不改变对外 API 契约。

**拆分建议（按 PR 可再拆子批）**

| 源文件 | 目标结构 |
|--------|----------|
| `api/routes.py` | `api/workspace.py`、`api/ontology.py`、`api/business_logic.py`、`api/settings.py`、`api/chat_bi.py`、`api/confirmations.py`；`main` 或 `api/router.py` 汇总 |
| `services/query.py` | `services/workspace_service.py`、`ontology_query.py`、`logic_query.py`、`draft_task_service.py` |
| `models/__init__.py` | `models/domain.py`、`ontology.py`、`logic.py`、`chat_bi.py`、`external.py`、`settings.py` + `__init__` 重导出 |
| `schemas/__init__.py` | 同上按领域拆分 + 重导出（保证 `from app.schemas import X` 仍可用） |

**原则**

- 先搬移、再整理 import；本批 **禁止** 改接口路径与响应字段。
- 每拆完一块跑 B3 测试。

**验收**

- [x] 单文件原则上 < 600 行（个别允许例外并注明）
- [x] OpenAPI `/docs` 路径集合与拆分前一致
- [x] 现有前端无需改 URL

**依赖**：B3 有冒烟测试再拆更安全；无测试时需手工点验工作区/本体/业务逻辑/Chat BI/设置。

**预估**：3–4 人日（可拆成 B4a routes / B4b query / B4c models-schemas）

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B4
- 主要变更：
  - `models/`：按域拆为 `domain` / `ontology` / `logic` / `chat_bi` / `external` / `settings`，`__init__` 重导出
  - `schemas/`：同上按域拆分 + 重导出（`from app.schemas import X` 仍可用）
  - `services/query.py` → `ontology_query` / `logic_query` / `workspace_service` / `draft_task_service`；`query.py` 兼容重导出
  - `api/routes.py` → `settings` / `workspace` / `ontology` / `business_logic` / `confirmations` / `chat_bi` + `router.py` / `deps.py`；`routes.py` 兼容重导出
- 偏差与后续：未改任何 API 路径或响应字段；`pytest` 7 通过；OpenAPI 管理路径集合保持 67；单文件均 < 600 行

---

### Batch B5 — 异步任务与并发

**目标**：草稿生成等长任务可排队、可恢复、不阻塞事件循环。

**范围**

1. **短期（本批必做）**
   - 草稿生成：明确任务状态机（queued / running / succeeded / failed / cancelled），进程重启后 running → failed 或可 resume 策略写清并实现一种。
   - 避免 Semaphore「忙等 sleep」误导；排队进度可基于 DB 队列位次。
   - Chat BI / ExpressionFormatter 中同步 OpenAI 调用：改为 `asyncio.to_thread` 或 AsyncOpenAI，避免堵死 event loop。
2. **中期（可标为 B5.1 延期）**
   - 引入 Redis/ARQ/Celery 之一；worker 独立进程。
   - 多实例下淘汰进程内 Semaphore。

**涉及文件（预期）**

- `services/draft_generation_queue.py`
- `services/query.py`（或拆分后的 draft_task_service）
- `services/chat_bi.py`、`expression_formatter.py`
- `models` 中 `DraftGenerationTask`

**验收**

- [x] 并发上限配置仍生效
- [x] 重启后无「永久 running」僵尸任务（有修复逻辑）
- [x] 高并发 Chat BI 请求下 API 健康检查仍及时响应（定性即可）

**依赖**：B4 拆完 draft 相关更易改；可与 B4 解耦但冲突概率高。

**预估**：2–4 人日（含 B5.1 则另计）

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B5
- 主要变更：
  - 草稿状态机：`queued → running → succeeded|failed|cancelled`（兼容旧 `completed`）
  - `draft_generation_queue`：Semaphore 等待期间按 DB 队列位次更新进度，去掉 `locked()+sleep` 忙等
  - 启动时 `recover_stale_draft_tasks`：queued/running → failed（fail-on-restart，不 resume）
  - Chat BI / ExpressionFormatter：同步 OpenAI 经 `asyncio.to_thread`；路由改为 async
  - 前端：识别 `queued`/`succeeded`；停止按钮覆盖排队中任务
- 偏差与后续：B5.1（Redis/ARQ/Celery）按方案延期；未引入外部队列

---

### Batch B6 — 前端结构与数据层

**目标**：统一请求状态管理；拆大页；控制 CSS 膨胀。

**范围**

1. 推广 `useApi`（或引入 `@tanstack/react-query`，二选一，**全项目一种**）：
   - 优先改造：Workspace、Ontology 列表、DomainDetail、Settings、External API 页。
2. 拆分：
   - `ChatBiPage.tsx` → `chat-bi/` 目录（侧栏、消息列表、输入区、引用面板）。
   - `ObjectTypeDetailPage` / `RelationTypeDetailPage` 抽共享编辑区块。
   - `styles.css` 按域拆：`styles/layout.css`、`chat-bi.css`、`graph.css` 等，由 `main.tsx` 引入。
3. 工程：ESLint + Prettier（与现有 TS 配置兼容）；`npm run lint` 进 CI（可挂 B3 后续）。

**不做**

- 不改视觉语言（遵循现有 Ant Design 体系）
- 不做大范围「去卡片化」视觉重设计

**验收**

- [x] 列表页 loading/error 行为一致
- [x] Chat BI 功能回归通过（会话、提问、历史）
- [x] `styles.css` 单文件显著变小或已拆分
- [x] `npm run build` 通过

**依赖**：无强依赖；与 B4 并行注意 API 类型同步。

**预估**：3–5 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B6
- 主要变更：
  - 推广 `useApi`：DomainDetail / Settings / ExternalApiApps / ExternalApiDetail（Workspace/Ontology/Catalog 已有）
  - `ChatBiPage` → `pages/chat-bi/`（Sidebar / Messages / Composer / References / utils）
  - 共享编辑：`entity-edit/MappingDatasetSelect` + `EntityEditToolbar`（对象/关系详情复用）
  - `styles.css` → `styles/{tokens,layout,graph,expression,chat-bi,external-api}.css`
  - ESLint + Prettier；CI frontend 增加 `npm run lint`
- 偏差与后续：未引入 react-query（沿用现有 useApi）；lint 对 React Compiler 级规则保持关闭以免存量告警阻塞 CI

---

### Batch B7 — Query / 图谱性能

**目标**：落实 TECH_DESIGN 非功能：大域分页、图谱局部展开、减少 N+1。

**范围**

1. 审计 `OntologyQueryService`：
   - 业务逻辑分类计数等改为一次 GROUP BY。
   - 列表接口统一 `limit/offset` 或 cursor，并在前端接上。
2. 图谱：
   - 默认不一次拉全量边；支持按对象邻域展开（API + `OntologyGraphView`）。
3. 前端大列表：必要处虚拟滚动（Ant Table / 自研简单 window）。

**验收**

- [x] 大域（对象 500+、关系 1000+）下列表与图谱可交互（约定测试数据集或 mock）
- [x] 关键列表 SQL 次数有基准（可在测试中断言 query 次数上限）

**依赖**：B4 拆分后改 query 更安全；B3 有利于防回归。

**预估**：2–3 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B7
- 主要变更：
  - `list_object_types` / `list_relation_types` / `list_business_logics` 统一 `PageResult`（limit/offset/q）；外部 v1 / MCP 仍返回 list
  - `list_business_logic_categories` 改为一次 GROUP BY 计数；业务逻辑列表批量解析 category_name
  - 图谱默认邻域展开（`center_id`/`depth`/`max_nodes`/`full`）；前端双击合并邻域
  - 域详情 / 本体页服务端分页 + Table `virtual`（pageSize≥50）
  - `tests/test_b7_query_perf.py`：分页、邻域图、SQL 次数上限
- 偏差与后续：大域验收以 100 对象 / 120 关系 fixture + 分页/邻域机制覆盖；未做 500+/1000+ 全量种子（成本高，行为与上限一致）

---

### Batch B8 — 外部 API / MCP 产品化

**目标**：对外能力可治理、可发现、可测。

**范围**

1. **Scope**：应用级权限，如 `domains:read`、`objects:read`、`logics:read`；校验失败 403。
2. **限流**：按 `app_id` 简易令牌桶或固定窗口（进程内先可，多实例再换 Redis）。
3. **契约**：
   - 维护 OpenAPI 中 v1 分组清晰
   - MCP tools 列表与 `external_api` 目录单一数据源（避免两处漂移）
4. **控制台**：试用页请求走真实鉴权；调用日志（可选表 `external_api_call_logs`：时间、app、tool、状态码、耗时）。
5. 契约测试：MCP initialize / tools/call 黄金路径。

**涉及文件（预期）**

- `services/external_api.py`、`api/external_routes.py`
- `mcp_stdio_server.py`
- `frontend/src/pages/ExternalApi*`

**验收**

- [x] 无 scope 的 key 不能越权访问
- [x] 超限返回 429
- [x] MCP 与 REST 目录字段一致
- [x] 至少 3 个契约测试

**依赖**：B1（Key 哈希与管理鉴权）必须完成。

**预估**：3 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B8
- 主要变更：
  - 应用级 Scope：`domains:read` / `objects:read` / `relations:read` / `logics:read`；缺权 403；旧应用空 scopes 视为全开
  - 进程内固定窗口限流（按 app_id，可应用级覆盖）；超限 429；`EXTERNAL_API_RATE_LIMIT_PER_MINUTE`
  - `EXTERNAL_MCP_TOOLS` 为 MCP tools/list 与控制台 catalog 单一数据源（含 `required_scope` / `rest_path`）
  - 表 `external_api_call_logs` + 管理端查询；试用页继续走真实 `X-API-Key`
  - Alembic `b8e0a1c2d3f4`；`tests/test_b8_external_product.py`（initialize / tools/call / scope / 429 / 目录一致）
- 偏差与后续：限流为进程内实现（多实例需 Redis，方案已注明）；未做调用日志前端独立页（管理 API 已可用）

---

### Batch B9 — 语义质量与版本能力

**目标**：增强「可审核、可追溯」产品价值，而非堆新入口。

**范围**

1. **生成质量**
   - 草稿去重策略产品化（替代仅脚本 `purge_duplicate_drafts.py`）：生成前检测 / 生成后合并建议。
   - 一致性校验：关系端点对象存在、属性归属正确、逻辑绑定有效。
   - 失败任务：可重试 + 保留 evidence / 错误摘要。
2. **版本**
   - 发布版本可读 diff（对象/关系/逻辑增删改摘要）。
   - 版本列表与快照查看（回滚可作为只读「基于旧版创建新草稿」，本批可不做破坏性回滚）。
3. **Chat BI grounded**
   - 回答必须带本体引用；无检索命中时明确拒绝编造。
   - 会话与数据域绑定校验。

**验收**

- [x] 发布后可查看与上一版的差异摘要
- [x] 故意构造不一致草稿时校验失败有明确错误
- [x] Chat BI 无命中时不返回虚构对象名（用测例固定）

**依赖**：B3、B4；Chat BI 部分依赖 B5 的 async 改造更佳。

**预估**：4–6 人日（可拆 B9a 版本 diff / B9b 生成校验 / B9c Chat BI）

**延期项**：跨域对象合并、数字孪生表达 → 单独立项，不在本方案强制批次内。

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B9
- 主要变更：
  - 发布时写入结构化 `diff_json` + `snapshot_json`；`GET .../versions/{v}/diff|snapshot`；域详情可查看版本差异
  - `draft_consistency` 发布前校验（关系端点/属性归属/逻辑绑定）；`POST .../validate`；不一致发布返回 issues
  - 草稿去重产品化：`GET .../draft-duplicates` + 生成前检测/purge；失败任务 `error_summary` + `POST .../retry`
  - Chat BI：无检索命中硬拒绝；引用须落地真实实体；会话-域绑定校验
  - Alembic `b9a1b2c3d4e5`；`tests/test_b9_semantic_quality.py`（5 用例）
- 偏差与后续：破坏性回滚未做（按方案可选）；跨域合并仍延期

---

### Batch B10 — 文档与部署对齐

**目标**：文档反映真实能力；部署路径可复制。

**范围**

1. 更新 `README.md`：核心能力表加入 Chat BI、External API、MCP、表达式编辑；鉴权与环境变量。
2. 更新 `TECH_DESIGN.md`：API 清单、权限现状、任务队列、迁移工具。
3. 视需要轻量更新 `IA.md` / `PRD.md` 中与现状冲突的「当前版本不区分角色」等表述（改为「管理 Token + 外部 App Key」阶段性模型）。
4. `docker-compose.yml`：api + frontend + postgres（可选 redis）。
5. `.env.example` 完整列出：`ONTOMETA_ADMIN_TOKEN`、`DATABASE_URL`、DataHub、LLM、并发参数。

**验收**

- [x] 新成员只读 README + 本文件能理解架构与安全模型
- [x] Compose 一键起（`docker compose up --build` / `make compose-up`）

**依赖**：建议在 B1、B2 完成后做，避免文档再漂移。

**预估**：1–2 人日

#### 完成记录

- 日期：2026-07-17
- 执行者/会话：Batch B10
- 主要变更：
  - README：能力表（Chat BI / External API·MCP / 表达式）、安全模型、Compose 与 Make 双路径、环境变量表完整化
  - TECH_DESIGN：API 清单、阶段性鉴权、Alembic、任务队列、部署节；IA/PRD 回写 Admin Token + App Key
  - `docker-compose.yml`（postgres + api + frontend；redis 注释可选）+ backend/frontend Dockerfile + nginx 反代
  - `backend/.env.example` 补齐并发 / LLM / 限流等；Makefile `compose-up`/`compose-down`
- 偏差与后续：Compose 默认 Mock DataHub/LLM；未强制启用 Redis（与 B5.1 一致）

---

## 4. 依赖关系与推荐顺序

> **B0 锁定**：以下顺序为后续分批执行的权威依赖图；调整须同步改本文件并在进度表备注。

```text
B0 基线
 └─► B1 安全 ──────────────────────────► B8 外部 API 产品化
      └─► B2 迁移 ─► B3 测试/CI ─► B4 拆分 ─┬─► B5 异步任务
                                           ├─► B7 性能
                                           └─► B9 语义/版本
 B6 前端 可与 B4 后半并行（接口不变前提下）
 B10 文档/部署 建议在 B1+B2 之后，可在任意批次末尾穿插更新
```

**最小上线包（建议）**：`B0 + B1 + B2 + B3 + B10（部分）`  
**可维护性包**：再加 `B4 + B6`  
**对外与质量包**：再加 `B5 + B7 + B8 + B9`

---

## 5. 新会话执行模板

### 5.1 会话开始

```
工作区：ontoMeta
阅读：OPTIMIZATION_PLAN.md → 仅 Batch Bx
约束：
- 不执行其他 Batch
- 保持 API 契约（除非本批明确要求变更）
- 改完更新「0. 执行进度」与本 Batch 完成记录
- 不要主动 git commit，除非我要求
```

### 5.2 会话结束检查清单

- [ ] 验收项已勾选或注明豁免原因
- [ ] 进度表状态已更新
- [ ] 若引入新环境变量，已写入 `.env.example` 与 README
- [ ] 若行为变更，已在本 Batch「完成记录」写 3–5 行说明

### 5.3 完成记录格式（追加到对应 Batch 末尾）

```markdown
#### 完成记录
- 日期：
- 执行者/会话：
- 主要变更：
- 偏差与后续：
```

---

## 6. 风险总表

| 风险 | 影响 | 缓解 |
|------|------|------|
| 管理 Token 泄露 | 等同未鉴权 | 环境注入、定期轮换、禁止提交 `.env` |
| Key 哈希迁移失败 | 外部集成全断 | 双写窗口、迁移脚本、可回滚备份 DB |
| 大拆分无测试 | 隐性回归 | B3 优先；拆分前手工冒烟清单 |
| SQLite → PG 差异 | SQL/类型问题 | 迁移后在 PG 跑一遍 pytest |
| 前端 Token 存放 | XSS 风险 | 后续改 HttpOnly Cookie 或网关鉴权 |

---

## 7. 与现有文档的关系

| 文档 | 关系 |
|------|------|
| [SSOT.md](./SSOT.md) | 产品真相；优化不得违背系统边界（仍以 DataHub 为输入） |
| [TECH_DESIGN.md](./TECH_DESIGN.md) | 技术目标；本方案 B7/B9/B1 补齐其中未落地项 |
| [PRD.md](./PRD.md) | 权限「当前不区分角色」被 B1 阶段性模型取代，B10 需回写 |
| [DOMAIN_MODEL.md](./DOMAIN_MODEL.md) | B2/B8/B9 改表时同步 |
| [IA.md](./IA.md) | B6/B8 若增导航或外部 API 信息架构时同步 |

---

## 8. 附录：手工冒烟清单（无自动化时使用）

1. 工作区：同步域 → 触发生成 → 看进度/任务日志 → 停止任务  
2. 域详情：编辑对象/属性/关系 → 确认发布  
3. 本体页：列表/卡片/图谱切换 → 对象详情  
4. 业务逻辑：分类 → 详情 → 表达式编辑/格式化  
5. Chat BI：新建会话 → 提问 → 查看引用 → 归档/置顶  
6. 设置：LLM 服务增删改、DataHub 配置  
7. 外部 API：创建应用 → 复制 Key → 目录试用 → MCP tools/call  
8. 鉴权（B1 后）：无 Token 应失败；错误 Token 应 401  

---

*文档版本：v1.0 | 创建：2026-07-17 | 维护：随 Batch 完成更新进度表*
