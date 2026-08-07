# ontoMeta 部署文档

本文件是**面向运维/部署的单一权威**：从零起一份完整可用的 ontoMeta，并保证每一项功能在
部署后真正可用。每项功能都给出「需要配什么 → 怎么验 → 不配会怎样」。

> 阅读顺序：§1 架构 → §2 前置 → §3 一键起栈 → §4 功能×依赖矩阵 → §5 逐功能配置与验收 →
> §6 数据库与迁移 → §7 生产加固 → §8 排错。

---

## 1. 架构与组件

```
┌──────────────┐    /api/*    ┌────────────────────┐   SQLAlchemy    ┌────────────┐
│  Frontend    │ ───────────▶ │  Backend (FastAPI) │ ──────────────▶ │ PostgreSQL │
│  React+Vite  │   /health    │  :8000             │   Alembic       │  (或 SQLite)│
│  :5180 / :80 │ ◀─────────── │  /docs /api/mcp    │ ◀────────────── │            │
└──────────────┘              └─────────┬──────────┘                 └────────────┘
                                         │  外部依赖（按功能按需启用）
              ┌──────────────┬───────────┼───────────┬──────────────┐
              ▼              ▼           ▼           ▼              ▼
         DataHub GMS     LLM/OpenAI   Cube 语义层   Airflow      sync-runner
         (元数据/血缘)   (草稿/问数)   (语义层/预聚合) (物化调度)   (数据搬运)
                              │                        │
                              ▼                        ▼
                         SeaTunnel / Doris / Hive（目标数仓与搬运工具）
```

| 组件 | 镜像/来源 | 端口 | 必需性 |
|------|-----------|------|--------|
| Frontend | `frontend/Dockerfile`（node:22 构建 → nginx:127） | 5180（宿）/ 80（容器） | 必需 |
| Backend | `backend/Dockerfile`（python:3.12-slim） | 8000 | 必需 |
| PostgreSQL | `postgres:16-alpine` | 5432 | Compose 必需；本地可用 SQLite |
| DataHub GMS | 外部已部署 | 8080 / 9002 | 本体导入/血缘功能需要 |
| LLM | OpenAI 兼容（含 DeepSeek） | — | 草稿命名/问数/语义检索需要 |
| Cube | `cubejs/cube` | 4000 | 语义层/预聚合（可选） |
| Airflow | `apache/airflow:2.10.5`（本地构建含 DataHub 插件） | 8081 | 物化编排需要 |
| SeaTunnel | `apache/seatunnel:2.3.11` | 5801 | 数据搬运需要 |
| Doris | `apache/doris:doris-all-in-one-2.1.0` | 8030/9030 | 目标数仓（可选，可换 Hive/MySQL） |
| sync-runner | `docker/sync-runner/Dockerfile` | 8098 | runner 通道搬运需要 |

---

## 2. 前置要求

### 2.1 机器
- **Docker Compose 全栈**：Docker Engine ≥ 24 + Compose v2、≥ 4 GB 可用内存、≥ 5 GB 磁盘（编排栈镜像另需 ~6 GB）。
- **本地开发**：Python 3.12+、Node 20+（CI 用 20，22 亦可）、npm。

### 2.2 端口占用
默认占用：`5180`（前端）、`8000`（API）、`5432`（PG）。编排栈另占 `8081/5801/8030/9030`。
部署前确认这些端口空闲，或改映射（见 §7.4）。

### 2.3 镜像源（国内环境必读）
本机实测 Docker Hub 直连极慢（alpine 4MB 耗时 6 分钟）。**起编排栈前务必配镜像源**，
否则 airflow/seatunnel/doris 拉不下来。两种方式任选：
- Docker Desktop → Settings → Docker Engine 加 `registry-mirrors`（全局生效，推荐）；
- 或在 `docker/orchestration/.env` 给 `IMG_AIRFLOW`/`IMG_SEATUNNEL`/`IMG_DORIS`/`IMG_POSTGRES` 加源前缀，如 `IMG_AIRFLOW=docker.m.daocloud.io/apache/airflow:2.10.5`。

---

## 3. 一键起栈（Docker Compose，推荐）

最短路径，起出「本体浏览 + 工作区 + Chat BI + 外部 API」全可用（外部数据源按 §5 接）。

```bash
git clone <repo> && cd ontoMeta

# ① 管理令牌（强烈建议改掉默认值）
export ONTOMETA_ADMIN_TOKEN=$(openssl rand -hex 24)

# ② LLM（不配则草稿走确定性命名兽、问数显式报错）
export OPENAI_API_KEY=sk-...

# ③ 起栈
docker compose up --build -d

# ④ 等 API 就绪
curl -sf http://localhost:8000/health   # → {"status":"ok","app":"ontoMeta"}
```

访问：
- 前端：http://localhost:5180
- API 文档：http://localhost:8000/docs
- 健康检查：`GET /health`（无需鉴权）

打开前端 → **设置 → 管理鉴权**，填入与 `ONTOMETA_ADMIN_TOKEN` 相同的值（或构建期注入
`VITE_ONTOMETA_ADMIN_TOKEN`，见 §7.2），即可使用全部管理功能。

停止 / 清理：
```bash
docker compose down              # 保留数据卷 ontometa_pg
docker compose down -v           # 连数据卷一起删（清空所有数据）
```

> Compose 默认用 PostgreSQL。`api` 容器启动时自动 `alembic upgrade head`，空库即建表，
> 旧库自动 `stamp head`（见 §6）。无需手动迁移。

---

## 4. 功能 × 依赖矩阵

| 功能 | 后端 | 前端 | DB | DataHub | LLM | Cube | Airflow+搬运 | 验收方式 |
|------|:----:|:----:|:--:|:-------:|:---:|:----:|:------------:|----------|
| 本体浏览/图谱 | ✅ | ✅ | ✅ | — | — | — | — | §5.1 |
| 本体建模（草稿生成） | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | §5.2 |
| 发布与一致性校验 | ✅ | ✅ | ✅ | — | — | — | — | §5.3 |
| 业务逻辑/表达式 | ✅ | ✅ | ✅ | — | ✅ | — | — | §5.4 |
| Data Agent（Chat BI 问数） | ✅ | ✅ | ✅ | — | ✅ | — | — | §5.5 |
| Data Agent 执行 SQL/取值画像 | ✅ | ✅ | ✅ | — | ✅ | — | — | §5.5 |
| 语义检索（同义词召回） | ✅ | ✅ | ✅ | — | ✅(嵌入) | — | — | §5.5 |
| 外部 REST / MCP | ✅ | ✅ | ✅ | — | — | — | — | §5.6 |
| 数据应用（表格/大屏/看板） | ✅ | ✅ | ✅ | — | — | — | — | §5.7 |
| Cube 语义层/预聚合 | ✅ | ✅ | ✅ | — | — | ✅ | — | §5.8 |
| 物化编排（生成 DAG/搬运/血缘） | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | §5.9 |
| DataHub 元数据导入 | ✅ | ✅ | ✅ | ✅ | — | — | — | §5.10 |

> 「—」表示该功能不依赖此项。**未配置的依赖走确定性路径或显式报错，不会静默错乱**：
> 无 LLM → 草稿以证据确定性命名兽底（不报错），但问数/表达式等纯 LLM 能力显式报错；
> 无 DataHub → 导入/血缘相关接口报错；无 Cube → 语义层接口报 503/降级。

---

## 5. 逐功能配置与验收

### 5.1 本体浏览与图谱
**需要**：后端 + 前端 + DB + 管理令牌（§3 已满足）。
**验收**：前端「本体浏览」页能列出已发布对象；图谱视图可展开邻域。
**无数据时**：先走 §5.2 建模发布，或 §5.10 从 DataHub 导入。

### 5.2 本体建模（草稿生成）
**需要**：§5.1 + DataHub GMS 可达 + LLM。
**配置**：
```bash
# backend/.env 或 Compose environment
DATAHUB_GMS_URL=http://<datahub-gms>:8080
DATAHUB_FRONTEND_URL=http://<datahub-frontend>:9002
DATAHUB_TOKEN=                     # DataHub 鉴权 token（若 GMS 开启了鉴权）
OPENAI_API_KEY=sk-...              # 或在「设置 → LLM 服务」配 DeepSeek 等兼容服务
OPENAI_MODEL=gpt-4o-mini
LLM_TIMEOUT_SECONDS=300
MAX_CONCURRENT_DRAFT_GENERATIONS=2 # 草稿生成并发上限
```
**验收**：前端「本体建模」选数据域 → 触发草稿生成 → 对象/属性/关系/业务逻辑逐块产出，
状态从 running → succeeded；发布后出现在「本体浏览」。
**无 LLM**：草稿以确定性命名兽底生成（可用但命名质量低）；无 DataHub：无法拉取源数据域，报错。

### 5.3 发布与一致性校验
**需要**：§5.1。**配置**：
```bash
FORMAL_ENFORCE=warn   # off=不检查 / warn=检查不阻断(默认) / error=error 级违反阻断发布
```
**验收**：发布时返回一致性报告；`error` 模式下存在 error 级不变式违反时发布被拒。

### 5.4 业务逻辑与表达式
**需要**：§5.1 + LLM（表达式导入/命名用）。**验收**：新建业务逻辑 → 表达式富文本编辑器
可格式化、保存；发布后可被 Chat BI 引用。

### 5.5 Data Agent（Chat BI 问数）
**需要**：§5.1 + LLM + 已发布本体。**配置**：
```bash
AGENT_SOUNDNESS=on                 # off/warn/on：SQL 语义证明与断言核验闸门
AGENT_RUN_SQL_MIN_ROLE=publisher   # run_sql 工具最低角色（与 /execute 同价）
AGENT_PROFILE_CACHE_SECONDS=900    # 取值画像缓存
AGENT_PROFILE_TOP_N=20
AGENT_COMPACTION=on                # 长会话上下文压缩
AGENT_HISTORY_CHAR_BUDGET=6000
AGENT_RESULT_OFFLOAD=on            # 大结果离场存储
AGENT_RESULT_SAMPLE_ROWS=5
# 语义检索（可选，留空即关闭，退回纯 ILIKE）
AGENT_EMBEDDING_MODEL=text-embedding-3-small   # 走已配的 OpenAI 兼容服务
AGENT_EMBEDDING_DIM=256
AGENT_EMBEDDING_MIN_SCORE=0.0
```
**直连数仓执行 SQL**：Data Agent 的 `run_sql` / `profile_values` 需要后端能连到目标数仓。
在「设置 → 数据源」或本体 `source_ref` 配置数仓连接串；按目标引擎装驱动（见 §5.9.4）。
**验收**：
1. 「Data Agent」页提问 → 回答含口径映射 + 可执行 SQL + （有数据时）结果表。
2. `AGENT_SOUNDNESS=on` 时，语义不可证的 SQL 被拒答。
3. 启用嵌入后，同义词（客户/往来单位/Customer）能召回。
**无 LLM**：问数显式报错（不 Mock）。

### 5.6 外部 REST / MCP
**需要**：§5.1。**配置**：
```bash
API_KEY_HASH_PEPPER=...            # 可选；变更后须重新生成全部 App Key
EXTERNAL_API_RATE_LIMIT_PER_MINUTE=60   # 每应用每分钟默认上限（<=0 关闭）
```
**验收**：
1. 前端「外部 API」创建应用 → 获得 API Key（仅创建/重置时明文返回一次）。
2. `curl -H "X-API-Key: <key>" http://localhost:8000/api/v1/objects` 返回已发布对象。
3. MCP：`ONTOMETA_MCP_URL=http://<host>:8000/api/mcp`，用同一 API Key 调 `tools/list`。
4. 超限返回 429，缺 scope 返回 403。
**MCP stdio（可选）**：`backend/mcp_stdio_server.py` 与 HTTP `/api/mcp` 共用工具目录，
按 MCP 客户端文档配置（`.env.mcp` 勿提交）。

### 5.7 数据应用（表格 / 大屏 / 看板）
**需要**：§5.1。**验收**：Data Agent 回答后「生成数据表格 / 生成看板 / 加入看板」可用；
「数据应用」页可编辑/预览/发布；公开页可嵌入。
**直连数仓渲染**：若数据应用直连数仓取数，需配数据源连接 + 装对应驱动（§5.9.4）。

### 5.8 Cube 语义层（可选）
**需要**：§5.1 + Cube。**启用**：取消 `docker-compose.yml` 里 `cube` / `cube_refresh_worker`
两段注释，填 `CUBE_API_SECRET`，`docker compose up -d cube cube_refresh_worker`。
```bash
CUBE_API_URL=http://cube:4000
CUBE_API_SECRET=<强随机>
CUBE_PREAGG_REFRESH=1 hour
CUBE_TENANT_DIMENSION=tenant_id     # 行级权限列名（为空不启用 RLS）
CUBE_TIMEOUT_SECONDS=30
```
> Cube 配置现已改为在「设置 → Cube 语义层」管理（存 DB），环境变量仅作首次播种默认值。
**验收**：发布本体后 `GET /api/ontologies/{id}/cube-model` 返回 model；Cube 加载成功；
数据应用走 Cube 取数返回结果。

### 5.9 物化编排（Airflow + 搬运 + 目标数仓）
**需要**：§5.2 + Airflow + 搬运工具 + 目标数仓。这是最重的一块，分步来。

#### 5.9.1 起编排验证栈
```bash
cd docker/orchestration
cp .env.example .env               # 改镜像源、DataHub/ERP 连接、ONTOMETA_ADMIN_TOKEN
cd ../..
make orch-preflight                # 镜像/网络/端口/已有服务检查，全过再往下
make orch-up-airflow               # Airflow + 元数据库（含 DataHub 插件）
make orch-up-sync                  # SeaTunnel
make orch-up-warehouse             # Doris（可选，可换 Hive/MySQL）
make orch-logs                     # 跟日志
```
- Airflow Web：http://localhost:8081 （admin/admin，仅本地验证）。
- `docker/orchestration/.env` 关键项：
  ```
  DATAHUB_GMS_URL=http://<datahub-gms容器名>:8080
  DATAHUB_NETWORK=datahub_network
  ERPNEXT_NETWORK=erpnext_frappe_network
  ONTOMETA_API=http://host.docker.internal:8000
  ONTOMETA_ADMIN_TOKEN=<同后端>
  ORCH_DAGS_DIR=./dags
  ORCH_JOBS_DIR=./seatunnel/jobs
  ```

#### 5.9.2 后端侧编排配置（部署基础设施，不进设置页）
```bash
AIRFLOW_DAGS_DIR=/path/to/dags      # 与 Airflow 容器挂载点对齐
AIRFLOW_JOBS_DIR=/path/to/jobs
AIRFLOW_DOCKER_NETWORK=bridge       # 源库/目标仓互访的 Docker 网络（非 bridge 时必改）
AIRFLOW_SYNC_DRIVERS_DIR=/path/to/jdbc  # JDBC 驱动 jar 目录（空=不挂）
SYNC_TOOL_IMAGES=datax=registry/datax:3.0  # 工具执行镜像覆盖（DataX 无官方镜像）
SYNC_CHANNEL=runner                # runner(推荐) / docker(旧通道)
SYNC_RUNNER_ENDPOINT=http://sync-runner:8098
ONTOMETA_STAGING_SWAP=true         # 全量装载走 staging+原子切换
ONTOMETA_MAX_TASKS_PER_DAG=50
ONTOMETA_MAX_ACTIVE_TASKS_PER_DAG=16
```

#### 5.9.3 sync-runner（runner 通道推荐）
```bash
docker build -f docker/sync-runner/Dockerfile -t ontometa/sync-runner:3 .
docker run -d -p 8098:8098 \
  -e SYNC_CONN_ERP_READONLY_URL='mysql+pymysql://ro:pw@erp-db:3306/erp' \
  -e SYNC_CONN_WAREHOUSE_DEFAULT_URL='mysql+pymysql://root:pw@doris-fe:9030/dw' \
  ontometa/sync-runner:3
# 写 Hive 时再加：-e SEATUNNEL_REST_ENDPOINT=http://<zeta>:8080
#                -e SYNC_CONN_<别名>_METASTORE_URI=thrift://<host>:9083
```
驱动构建期烘进镜像、运行期零挂载（消 ClassNotFoundException）。`GET /capabilities` 声明装到哪些驱动。

#### 5.9.4 后端数仓驱动（直连数仓取数/执行 SQL 用）
按目标引擎在 `backend/.venv` 内安装（`requirements.txt` 默认不带，避免版本耦合）：
```bash
# Doris / StarRocks / MySQL 线协议
pip install pymysql
# Hive / Kyuubi（别用 pyhive[hive]，其 sasl 在 Python≥3.12 编译不过）
pip install pyhive thrift thrift-sasl pure-sasl
# ClickHouse
pip install clickhouse-sqlalchemy
```
> Compose 的 `api` 镜像默认只装了 `psycopg`。直连其他数仓需在 `backend/Dockerfile`
> 追加对应 `pip install` 后重新构建。

#### 5.9.5 Flink on YARN（计算任务，可选）
```bash
FLINK_SQL_RUNNER_JAR=/path/to/sql-runner.jar   # 空=只产 SQL 不执行（仅产出模式）
FLINK_SQL_RUNNER_CLASS=com.ontometa.flink.SqlRunner
FLINK_BIN=flink
FLINK_DEPLOY_TARGET=yarn-per-job
FLINK_PARALLELISM=1
FLINK_YARN_QUEUE=
```

#### 5.9.6 验收
1. 本体发布后触发物化 → `docker/orchestration/dags/` 出现新 DAG。
2. Airflow Web 能看到该 DAG 并成功跑通；目标数仓里出现物化表且有数据。
3. DataHub 出现对应 DataFlow/DataJob 血缘（`curl localhost:8081/openapi.json | grep dagRuns` 确认 REST 版本）。
4. 端到端冒烟：`make smoke`（需后端 + Airflow + 目标仓可达；换表 `SMOKE_ENTITY=item make smoke`）。
5. DAG 解析校验：`make dag-parse`（用真 Airflow DagBag 解析，`DAG_PARSE_IMAGE=` 指实际镜像）。

### 5.10 DataHub 元数据导入
**需要**：§5.2 的 DataHub 配置。**验收**：前端「本体建模」选数据域从 DataHub 拉取数据集/
字段/血缘/逻辑证据；`DATAHUB_MAX_CONCURRENCY` 控制拉取并发（隧道不稳时调低，默认 3）。

---

## 6. 数据库与迁移

- Schema 由 **Alembic** 管理（`backend/alembic/`）。**启动时自动 `alembic upgrade head`**。
- 旧库（有业务表无 `alembic_version`）启动时自动 `stamp head`（前提：schema 已与模型一致）。
- 极旧缺列库：**先备份**，勿直接 stamp；用空库 upgrade 后导数据，或手写过渡 revision。

| 环境 | `DATABASE_URL` | 驱动 |
|------|----------------|------|
| 开发 | `sqlite:///./ontometa.db` | 内置（WAL 模式） |
| Compose / 生产 | `postgresql+psycopg://user:pass@host:5432/ontometa` | 镜像内已装 `psycopg[binary]`；本地连 PG 需 `pip install "psycopg[binary]"` |

手动迁移（已激活 venv，`backend/` 下）：
```bash
alembic upgrade head          # 应用到最新
alembic current               # 当前版本
alembic history               # 历史
alembic revision --autogenerate -m "描述"   # 生成新迁移（须审阅）
```
生产流水线建议在**启动前**显式执行一次 `alembic upgrade head`，再起 uvicorn。

---

## 7. 生产加固

### 7.1 必改项
- `ONTOMETA_ADMIN_TOKEN`：改强随机（`openssl rand -hex 24`），勿用默认 `dev-admin-token-change-me`。
- `DEBUG=false`：避免 500 响应泄露异常细节。
- `DATABASE_URL`：用 PostgreSQL，勿用 SQLite。
- `API_KEY_HASH_PEPPER`：设强随机；**变更后须重新生成全部外部 App Key**。
- Airflow `admin/admin` 仅限本地验证，生产改强密码并接 LDAP/OIDC。

### 7.2 前端管理令牌注入
前端需拿到管理令牌才能调管理 API。两种方式：
- **运行时**：用户在「设置 → 管理鉴权」手填（令牌存浏览器）。
- **构建时**：`VITE_ONTOMETA_ADMIN_TOKEN=<token> npm run build`（编进产物，适合内部部署）。
> Compose 的 `frontend` 镜像默认不注入；如需编入，在 `frontend/Dockerfile` 加 `ARG VITE_ONTOMETA_ADMIN_TOKEN` + `ENV` 后 `--build-arg` 传入。

### 7.3 CORS
```bash
CORS_ORIGINS=["https://ontometa.yourcorp.com","https://app.yourcorp.com"]
```
JSON 数组。Compose 默认放行 `localhost:5180/127.0.0.1:5180`，生产改成实际域名。

### 7.4 改端口
- 前端：改 `docker-compose.yml` 的 `frontend.ports` 与 `CORS_ORIGINS`。
- API：改 `api.ports` 与前端 `nginx.conf` 的 `proxy_pass`。
- PG：改 `postgres.ports`（仅宿主机映射，容器间互访不变）。

### 7.5 持久化与备份
- Compose 数据卷 `ontometa_pg` 保留 PG 数据；`docker compose down` 不删，`-v` 才删。
- 生产 PG 按常规做物理备份（pg_basebackup / WAL 归档）。
- 外部 App Key 库内只存 SHA-256 哈希 + 前缀，备份 DB 即备份凭据；`API_KEY_HASH_PEPPER` 务必另行安全保管。

### 7.6 资源与并发
- `MAX_CONCURRENT_DRAFT_GENERATIONS=2`：草稿生成并发，按 LLM 配额与机器调。
- `DATAHUB_MAX_CONCURRENCY=3`：DataHub 拉取并发，隧道不稳（如 ngrok）调低。
- `ONTOMETA_MAX_ACTIVE_TASKS_PER_DAG=16`：单 DAG 并发搬运任务，按 worker 容量调。
- 草稿队列为进程内；高可用场景可启用 Redis（取消 `docker-compose.yml` redis 注释，当前不依赖）。

### 7.7 反向代理 / TLS
生产前置 nginx/网关做 TLS 终止，转发到 `frontend:80` 与 `api:8000`；
`X-Forwarded-For`/`X-Forwarded-Proto` 已由 `frontend/nginx.conf` 透传。注意 `CORS_ORIGINS` 与
`X-Forwarded-Proto` 一致，避免 cookie/重定向协议错配。

---

## 8. 排错

| 现象 | 排查 |
|------|------|
| 前端打开空白 / 接口 404 | 后端没起或端口不对；`curl localhost:8000/health`；检查 `nginx.conf` `proxy_pass` |
| 管理 API 返回 503 | `ONTOMETA_ADMIN_TOKEN` 未配置；前端「管理鉴权」令牌不一致 |
| 草稿生成卡住/失败 | 看 `.logs/backend.log`；无 LLM 时走确定性命名兽（非故障）；`MAX_CONCURRENT_DRAFT_GENERATIONS` 是否打满 |
| Chat BI 报「未配置 LLM」 | `OPENAI_API_KEY` 未设且设置页未配 LLM 服务 |
| Chat BI 拒答 | `AGENT_SOUNDNESS=on` 且语义不可证；改 `warn` 观测，或修正本体口径 |
| 语义检索同义词召回不到 | `AGENT_EMBEDDING_MODEL` 留空即关闭；填模型名并发布本体重建索引 |
| 物化 DAG 没出现在 Airflow | `AIRFLOW_DAGS_DIR` 与 Airflow 容器挂载点不一致；`dag_dir_list_interval` 默认 300s，等解析或调 `ONTOMETA_DAG_PARSE_TIMEOUT` |
| 搬运任务 ClassNotFoundException | runner 通道用 `ontometa/sync-runner` 镜像（驱动烘进）；docker 通道需 `AIRFLOW_SYNC_DRIVERS_DIR` 挂 JDBC jar |
| DataX 任务 pull 404 | DataX 无官方镜像，须 `SYNC_TOOL_IMAGES=datax=<自建镜像>` |
| 连 Doris/Hive 报缺驱动 | 见 §5.9.4 装驱动；Compose `api` 镜像需重建 |
| 镜像拉取超慢/失败 | 配镜像源（§2.3）；`make orch-preflight` 会测速并拦下 |
| 旧库启动报 schema 错 | 极旧缺列库勿 stamp；备份后空库 upgrade 再导数据（§6） |

### 日志位置
- Docker Compose：`docker compose logs -f api frontend postgres`。
- 本地 `service.sh`：`.logs/backend.log`、`.logs/frontend.log`。
- Agent 运行轨迹：`AGENT_TRACE_ENABLED=true` 时写 `backend/.logs/agent_traces/`（JSONL）。

### 健康检查
- `GET /health` → `{"status":"ok","app":"ontoMeta"}`（无需鉴权，K8s/Compose 探针用）。
- `api` 容器内置 HEALTHCHECK（`curl /health`）；`postgres` 内置 `pg_isready`。

---

## 附录 A：环境变量速查

| 变量 | 默认 | 说明 |
|------|------|------|
| `ONTOMETA_ADMIN_TOKEN` | — | 管理 API 共享 Token（必填，否则管理接口 503） |
| `DEBUG` | `true` | `false` 时 500 脱敏 |
| `DATABASE_URL` | `sqlite:///./ontometa.db` | 生产用 PG |
| `API_KEY_HASH_PEPPER` | — | 外部 Key 哈希 pepper |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub GMS |
| `DATAHUB_FRONTEND_URL` | `http://localhost:9002` | DataHub 前端 |
| `DATAHUB_TOKEN` | — | DataHub 鉴权 |
| `DATAHUB_MAX_CONCURRENCY` | `3` | DataHub 拉取并发 |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4o-mini` | LLM（也可在设置页配） |
| `LLM_TIMEOUT_SECONDS` | `300` | LLM 超时 |
| `LLM_CONTEXT_BUDGET_CHARS` | `48000` | 草稿分块字符预算 |
| `MAX_CONCURRENT_DRAFT_GENERATIONS` | `2` | 草稿并发 |
| `CORS_ORIGINS` | localhost 三端口 | JSON 数组 |
| `EXTERNAL_API_RATE_LIMIT_PER_MINUTE` | `60` | 外部 API 限流 |
| `CUBE_API_URL` / `CUBE_API_SECRET` | `http://localhost:4000` / — | Cube 语义层 |
| `CUBE_PREAGG_REFRESH` / `CUBE_TENANT_DIMENSION` | `1 hour` / — | 预聚合 / RLS 列 |
| `AGENT_SOUNDNESS` | `on` | 形式化闸门 off/warn/on |
| `AGENT_RUN_SQL_MIN_ROLE` | `publisher` | run_sql 最低角色 |
| `AGENT_EMBEDDING_MODEL` | — (关闭) | 语义检索嵌入模型 |
| `AGENT_EMBEDDING_DIM` / `AGENT_EMBEDDING_MIN_SCORE` | `256` / `0.0` | 截断维度 / 相似度下限 |
| `AGENT_COMPACTION` / `AGENT_HISTORY_CHAR_BUDGET` | `on` / `6000` | 上下文压缩 |
| `AGENT_RESULT_OFFLOAD` / `AGENT_RESULT_SAMPLE_ROWS` | `on` / `5` | 大结果离场 |
| `AGENT_TRACE_ENABLED` / `AGENT_TRACE_DIR` | `false` / `.logs/agent_traces` | 运行轨迹 |
| `FORMAL_ENFORCE` | `warn` | 发布不变式校验 off/warn/error |
| `SYNC_CHANNEL` / `SYNC_RUNNER_ENDPOINT` | `runner` / — | 搬运通道 |
| `AIRFLOW_DAGS_DIR` / `AIRFLOW_JOBS_DIR` | — | 编排产物落盘 |
| `AIRFLOW_DOCKER_NETWORK` | `bridge` | 搬运容器网络 |
| `AIRFLOW_SYNC_DRIVERS_DIR` | — | JDBC 驱动目录 |
| `SYNC_TOOL_IMAGES` | — | 工具镜像覆盖 `名=镜像,...` |
| `ONTOMETA_STAGING_SWAP` | `true` | 全量装载原子切换 |
| `FLINK_SQL_RUNNER_JAR` | — | Flink SqlRunner JAR（空=仅产 SQL） |

> 完整列表与逐项语义见 `backend/app/config.py` 与 `backend/.env.example`（两者为权威）。

## 附录 B：部署前自检清单

- [ ] `ONTOMETA_ADMIN_TOKEN` 已改强随机
- [ ] `DEBUG=false`（生产）
- [ ] `DATABASE_URL` 指向 PostgreSQL
- [ ] `CORS_ORIGINS` 改成实际域名
- [ ] `OPENAI_API_KEY` 或设置页 LLM 已配（否则问数/草稿命名受限）
- [ ] `DATAHUB_GMS_URL` 可达（否则导入/血缘不可用）
- [ ] 端口 5180/8000/5432 空闲
- [ ] `curl /health` 返回 ok
- [ ] 前端「管理鉴权」令牌与后端一致
- [ ] （如用物化）编排栈已起、`AIRFLOW_DAGS_DIR` 与挂载点对齐、驱动/镜像就位
- [ ] （如用 Cube）`CUBE_API_SECRET` 已设、cube 容器已起
- [ ] 旧库已备份再升级（§6）
