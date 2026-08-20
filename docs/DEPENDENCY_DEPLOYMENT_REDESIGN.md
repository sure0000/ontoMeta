# 依赖组件统一部署管理 · 重新设计

> 状态：**设计**。本文重新设计 ontoMeta 对「除自身前后端外所有依赖组件」的部署与连接管理，
> 统一收进设置页，与现有设置（LLM / DataHub / Airflow / 数据源）一并考虑。
>
> 目标一句话：**每个依赖组件在设置页选一种部署方式（物理机 / Docker / K8s / 已有外部服务），
> 选「部署」则由 ontoMeta 拉起并自动回收连接信息，选「已有」则手填连接信息——之后对上层功能而言无差别。**
>
> **现状更新（2026-08）**：本设计落地后经架构演进裁剪，以下组件已从基础设施面板移除，由其他机制替代：
> - `sync_runner` / `seatunnel`：搬运统一走 Flink SQL on YARN（见统一执行重构），不再作为独立组件
> - `cube`：非核心依赖，Cube 模型生成接口保留，配置需手动部署
> - `postgres`：ontoMeta 自身数据库由环境变量配置，不属于「依赖组件」
> - `warehouse` / `source_db`：目标数仓/源库连接由「数据源」标签页（DataSourcesPanel）统一管理，完整 CRUD + 测试
>
> 当前实际保留的组件：`llm` / `datahub` / `airflow`。下表及后文仍保留全量设计作为历史参考。

---

## 0. 为什么现在要改

当前每个依赖各自一套 bespoke 模型 + 服务 + 路由 + 面板，且只支持「手填一个已在跑的服务的连接信息」：

| 组件 | 现有模型 | 现状 |
|------|----------|------|
| LLM | `LlmServiceConfig`（多行） | 手填 api_base/key/model，有拨测 |
| DataHub | `DatahubSetting`（单例） | 手填 gms_url/frontend_url/token |
| Airflow | `AirflowSetting`（单例） | 手填 endpoint/auth + 投递目录 + 搬运通道参数 |
| Cube | `CubeSetting`（单例） | 手填 api_url/secret |
| 数据源（目标仓/源库） | `DataSource`（多行） | 手填 SQLAlchemy 连接串，有测试 |
| sync-runner | 凭据代填到 runner | 只配 endpoint + 凭据，不部署 |
| SeaTunnel / Doris / ERPNext / Bigtop | **无** | 完全在 compose / 外部，设置页够不着 |

问题：
1. **部署方式不可选**——只有「填连接」，没有「帮我起一个」。DataHub / ERPNext / Doris / SeaTunnel 在设置页根本不可见，全靠人去 compose 里起、再回来填地址，割裂。
2. **三态缺失**——物理机 / Docker / K8s 三种部署形态没有统一抽象；现在是「compose 一把梭」或「外部假设在跑」，没有 K8s 路径。
3. **连接信息靠人抄**——起完容器要自己去 `docker ps` 抄端口、拼连接串，易错。
4. **模型碎片化**——五个 bespoke 表，加字段要改五处；新增一个组件要从头再写一套。

---

## 1. 设计原则

1. **统一注册表，组件特化靠 schema**——一张 `dependency_components` 表管所有组件的部署与连接；组件差异用「组件类型 → 连接 schema + 部署模板」描述，不另起表。
2. **部署与连接分离**——`deploy_mode` 决定「怎么来的」，`connection` 记「怎么连」。部署成功自动回写 connection；选「已有」则跳过部署、直接填 connection。
3. **四种模式同构**——`external`（已有）/ `docker` / `k8s` / `bare_metal` 各一个 Deployer 适配器，输入部署参数、输出连接信息，对上层同一接口。
4. **不破坏现有功能**——现有 `LlmServiceConfig` 等表的读取侧（`get_*_runtime`）改为从注册表投影，旧表数据迁移进注册表；上层服务（chat_bi / materialization / data_app）调用不变。
5. **凭据不双写**——连接凭据只存注册表一处；sync-runner 凭据仍归 runner（ontoMeta 代填不落库）这一既有原则保留。
6. **部署是可选增强，不是前提**——`external` 模式零改造即等价于今天的行为；部署能力按组件逐步上，不一次性全开。
7. **【法则】所有配置走设置页，不写配置文件**——任何运行期参数（连接信息、通道选择、投递方式等）必须在 Web 设置页填写并存入数据库，**绝不**硬编码在 `config.py` / `.env` / compose 环境变量里。环境变量只作为**初始默认值**播种到数据库，之后由设置页统一管理。这是用户体验的铁律：配置散在文件里，部署方根本不知道去哪改、改了要不要重启，troubleshooting 时也找不到真实生效值。

---

## 2. 组件目录（统一管理的范围）

| key | 组件 | 连接信息形态 | 可部署模式 | 现有归属 |
|-----|------|--------------|------------|----------|
| `llm` | LLM / 嵌入服务 | api_base_url, api_key, model | external, docker, k8s, bare_metal | `LlmServiceConfig` |
| `datahub` | DataHub GMS(+前端) | gms_url, frontend_url, token, fabric | external, docker, k8s | `DatahubSetting` |
| `airflow` | Airflow 调度 | 调度 API：endpoint, username/password；DAG 投递：ssh_password（主机/端口/目录在 `deploy_spec.extra`） | external, docker, k8s | `AirflowSetting` |
| `seatunnel` | SeaTunnel 搬运 | rest_endpoint | external, docker, k8s | 无（散在 compose） |
| `warehouse` | 目标数仓(Doris/Hive/MySQL/…) | sqlalchemy_url, dialect | external, docker, k8s, bare_metal | `DataSource` |
| `source_db` | 源库(ERPNext MariaDB/…) | sqlalchemy_url, dialect | external, bare_metal | `DataSource` |
| `sync_runner` | sync-runner | endpoint, token | external, docker, k8s | AirflowSetting 内嵌 |
| `cube` | Cube 语义层 | api_url, api_secret | external, docker, k8s | `CubeSetting` |
| `postgres` | ontoMeta 自身 PG | sqlalchemy_url | external, docker, k8s | compose 内置 |
| `bigtop` | Bigtop Manager(Hive 备选) | api_url | external, bare_metal | 无 |

> `llm` / `warehouse` / `source_db` 支持多实例（多行，一个 default）；其余单例（每 key 一行）。
> `sync_runner` 从 `AirflowSetting` 拆出独立成行——它本就是独立服务，只是历史上塞在 Airflow 配置里。

---

## 3. 数据模型

### 3.1 统一注册表 `dependency_components`

```python
class DependencyComponent(Base):
    __tablename__ = "dependency_components"

    id: Mapped[str]                          # uuid
    key: Mapped[str]                         # 组件类型：llm/datahub/airflow/...（见 §2）
    name: Mapped[str]                        # 展示名，如 "DeepSeek 默认" / "Doris 目标仓"
    # —— 部署：怎么来的 ——
    deploy_mode: Mapped[str]                 # external | docker | k8s | bare_metal
    deploy_spec: Mapped[dict]                # JSON：模式参数（见 §4），external 时为空
    deploy_status: Mapped[str]               # not_deployed|deploying|deployed|failed|connected
    deploy_error: Mapped[str | None]         # 失败原因（供排错）
    # —— 连接：怎么连 ——
    connection: Mapped[dict]                 # JSON：连接信息（见 §3.2），部署成功自动回写或手填
    # —— 状态 ——
    enabled: Mapped[bool]
    is_default: Mapped[bool]                 # 多实例组件的默认行
    # —— 审计 ——
    created_at / updated_at
```

- `deploy_spec` 与 `connection` 用 JSON 列；结构由 `key` 决定的 schema 约束（见 §3.2），在服务层校验，不靠 DB 约束。
- 单例组件（datahub/airflow/…）在应用层保证每 `key` 至多一行；多实例组件（llm/warehouse/source_db）允许多行。

### 3.2 连接 schema（按 key）

每种 `key` 的 `connection` JSON 形态固定，由 `CONNECTION_SCHEMAS` 描述（服务层校验 + 前端表单生成）：

```python
CONNECTION_SCHEMAS = {
    "llm":       {"api_base_url": str, "api_key": secret, "model": str},
    "datahub":   {"gms_url": str, "frontend_url": str, "token": secret, "fabric": str},
    "airflow":   {"endpoint": str, "username": str?, "password": secret?, "ssh_password": secret?},
    "seatunnel": {"rest_endpoint": str},
    "warehouse": {"sqlalchemy_url": secret, "dialect": str},      # doris/hive/mysql/clickhouse/...
    "source_db": {"sqlalchemy_url": secret, "dialect": str},
    "sync_runner": {"endpoint": str, "token": secret?},
    "cube":      {"api_url": str, "api_secret": secret},
    "postgres":  {"sqlalchemy_url": secret},
    "bigtop":    {"api_url": str},
}
```

`secret` 字段：写时落库，读时只回 `*_set: bool` + `*_hint`（掩码），与现有 `mask_secret` 一致。

### 3.3 部署参数 schema（按 mode，跨组件通用 + 组件特化）

```python
DEPLOY_SPEC_SCHEMAS = {
    "external":   {},                         # 不部署，connection 手填
    "docker":     {"compose_file": str?, "image": str?, "env": dict?, "ports": dict?, "network": str?},
    "k8s":        {"manifest": str?, "helm_chart": str?, "helm_values": dict?, "namespace": str?},
    "bare_metal": {"host": str, "ssh_user": str?, "install_script": str?, "ports": dict?},
}
```

- `docker` 模式：优先用仓库自带的 compose 片段（`docker/components/<key>.yml`），可覆盖 image/env/ports。
- `k8s` 模式：给 manifest 或 Helm chart + values；Deployer apply 后等 Service 就绪、回采端口。
- `bare_metal` 模式：SSH 到 host 跑安装脚本（或只登记一台已装好的机器）。
- 组件可追加特化字段（如 `airflow` 的 docker 模式要 `dags_dir`/`jobs_dir` 挂载），在 `CONNECTION_SCHEMAS` 之外用 `extra` 子对象承载，不污染通用结构。

### 3.4 与现有表的关系（迁移策略）

**不并存两套事实源。** 现有五张表迁移进 `dependency_components`，之后读取侧只认注册表：

| 现有表 | 迁移目标 |
|--------|----------|
| `LlmServiceConfig` | → 多行 `key='llm'`，`connection={api_base_url,api_key,model}` |
| `DatahubSetting` | → 单行 `key='datahub'` |
| `AirflowSetting` | → 拆成 `key='airflow'`（连接+编排参数）+ `key='sync_runner'`（连接）；Airflow 的编排参数（dags_dir/sync_channel/max_tasks_per_dag/…）放 `extra` |
| `CubeSetting` | → 单行 `key='cube'` |
| `DataSource` | → 多行 `key='warehouse'` 或 `key='source_db'`（按用途分） |

迁移由 Alembic revision + `ensure_defaults` 一次性回填：旧表有数据则搬进注册表并标记 `deploy_mode='external'`（因为旧数据都是手填的已有服务）；空库则按环境变量播种（与今天 `ensure_defaults` 同逻辑）。

> Airflow 的「编排参数」（投递目录、DAG 形状、staging_swap 等）不是连接信息，保留在 `extra` 里，由 `get_airflow_runtime` 一并投影——这些参数的语义不变，只是换了个存放处。

---

## 4. 部署适配器（Deployer）

四模式同构接口：

```python
class Deployer(Protocol):
    def deploy(self, key: str, spec: dict) -> "DeployResult": ...
    def teardown(self, key: str, spec: dict) -> None: ...
    def status(self, key: str, spec: dict) -> str: ...

@dataclass
class DeployResult:
    connection: dict          # 自动回收的连接信息，回写 dependency_components.connection
    logs: list[str]           # 部署日志摘要（存 deploy_error/审计）
```

### 4.1 `ExternalDeployer`（已有服务）
- `deploy` 是 no-op，`connection` 由用户手填（即今天的行为）。
- 拨测：复用现有 `test_llm_connection` / `test_airflow_connection` / 数据源测试，按 key 分派到 `PROBE` 表（§5）。

### 4.2 `DockerDeployer`
- 读 `docker/components/<key>.yml`（本仓库新增目录，每个组件一份 compose 片段），用 `deploy_spec` 覆盖 image/env/ports/network。
- `docker compose -f <fragment> up -d`，轮询容器健康检查。
- 回收连接：`docker inspect` 取映射端口 → 按 key 模板拼 `connection`（如 datahub → `gms_url=http://localhost:<port>`）。
- 凭据：compose 片段里用 `${VAR}` 占位，Deployer 从 `deploy_spec.env` 注入；不把凭据写进产物。
- 依赖 `docker` CLI（后端容器需挂 docker.sock，或后端跑在宿主机上）。K8s 部署的后端不适用 docker 模式部署组件——此时应选 k8s 模式。

### 4.3 `K8sDeployer`
- `deploy_spec.manifest`（多文档 YAML）或 `helm_chart + helm_values`。
- 用 `kubernetes` python 客户端 apply；等 Service 就绪（LoadBalancer 外部 IP / NodePort / ClusterIP）。
- 回收连接：读 Service 端口 → `connection.endpoint = http://<svc>.<ns>:<port>`（集群内）或外部 IP（集群外）。
- 凭据经 Secret 注入，不进 manifest 明文。
- 需后端有 in-cluster kubeconfig 或挂 kubeconfig。

### 4.4 `BareMetalDeployer`
- `deploy_spec.host` + `ssh_user` + 可选 `install_script`。
- 有 install_script 则 Paramiko/SSH 执行；无则纯登记（「这台机器已装好 Doris，端口 9030」）。
- 回收连接：用 `deploy_spec.ports` + host 拼 `connection`。
- 适用：企业内已有物理机数仓、不允许容器化的源库。

---

## 5. 连接拨测（PROBE）

部署完成或手填连接后，统一拨测一次，结果写 `deploy_status`：

```python
PROBES = {
    "llm":         lambda c: ping_openai(c["api_base_url"], c["api_key"], c["model"]),
    "datahub":     lambda c: http_get(f"{c['gms_url']}/config"),
    "airflow":     lambda c: airflow_health_then_api(c),   # 复用现有两步拨测
    "seatunnel":   lambda c: http_get(f"{c['rest_endpoint']}/api/v1/info"),
    "warehouse":   lambda c: sqlalchemy_test(c["sqlalchemy_url"]),
    "source_db":   lambda c: sqlalchemy_test(c["sqlalchemy_url"]),
    "sync_runner": lambda c: http_get(f"{c['endpoint']}/capabilities"),
    "cube":        lambda c: http_get(f"{c['api_url']}/livez"),
    "postgres":    lambda c: sqlalchemy_test(c["sqlalchemy_url"]),
    "bigtop":      lambda c: http_get(c["api_url"]),
}
```

- 拨测复用现有实现（`test_llm_connection` / `test_airflow_connection` / 数据源测试），只做分派收口。
- 拨测只读、幂等、带超时；失败不阻断保存，只标 `deploy_status='failed'` + `deploy_error`，UI 显红。

---

## 6. 上层集成（读取侧改造）

现有 `SettingsService.get_*_runtime` 改为从注册表投影，**返回结构不变**，上层服务零改动：

```python
def get_datahub_runtime(self, db) -> DatahubRuntimeConfig:
    row = self._get_component(db, "datahub")           # 从 dependency_components 取
    c = row.connection
    return DatahubRuntimeConfig(gms_url=c["gms_url"], frontend_url=c["frontend_url"],
                                token=c.get("token"), fabric=c.get("fabric", "PROD"))

def get_airflow_runtime(self, db) -> AirflowRuntimeConfig:
    row = self._get_component(db, "airflow")
    c = row.connection; extra = row.deploy_spec.get("extra", {})   # 编排参数在 extra
    runner = self._get_component(db, "sync_runner")
    return AirflowRuntimeConfig(endpoint=c["endpoint"], ...,
                                sync_runner_endpoint=runner.connection["endpoint"], ...)
```

- `get_llm_runtime` / `get_cube_runtime` 同理。
- 数据源（warehouse/source_db）：`DataSource` 的消费侧（物化、数据应用取数）改为查 `key in ('warehouse','source_db')` 的行。

---

## 7. API 设计

统一 REST，按组件 key 分派；保留现有 `/settings/llm-services` 等路径作兼容薄层（转发到新接口，避免前端一次性全改）。

```
GET    /api/settings/dependencies                 # 列全部组件（按 key 分组）
POST   /api/settings/dependencies                 # 新建（多实例组件）
GET    /api/settings/dependencies/{id}
PUT    /api/settings/dependencies/{id}            # 改 deploy_mode/deploy_spec/connection
DELETE /api/settings/dependencies/{id}

POST   /api/settings/dependencies/{id}/deploy     # 执行部署（docker/k8s/bare_metal）→ 回写 connection
POST   /api/settings/dependencies/{id}/probe      # 拨测当前 connection
POST   /api/settings/dependencies/{id}/teardown   # 卸载（仅自部署的）

GET    /api/settings/dependencies/schema          # 返回 CONNECTION_SCHEMAS + DEPLOY_SPEC_SCHEMAS（前端表单生成）
```

- `deploy` 是异步长任务（K8s/Docker 起服务要秒级~分钟级）：走现有 `agent_pipeline` 的任务模式或一个轻量后台线程，前端轮询 `deploy_status`。
- 凭据字段写时落库、读时掩码，与今天一致。

---

## 8. UI 设计

设置页新增一个一级 Tab「**基础设施**」（或把现有「数据连接」「调度与语义」合并进来），内为统一的组件列表 + 详情抽屉：

```
┌─ 基础设施 ────────────────────────────────────────────────┐
│  [+ 新增]                          [刷新]                  │
│  ┌──────────┬─────────┬──────────┬────────┬─────────┐    │
│  │ 组件     │ 模式    │ 状态      │ 连接   │ 操作    │    │
│  ├──────────┼─────────┼──────────┼────────┼─────────┤    │
│  │ DataHub  │ Docker  │ ● 已连接  │ :8080  │ 编辑/拨测│    │
│  │ Airflow  │ 已有    │ ● 已连接  │ :8081  │ 编辑/拨测│    │
│  │ Doris 仓 │ K8s     │ ◐ 部署中  │ —      │ 日志    │    │
│  │ DeepSeek │ 已有    │ ● 已连接  │ api…   │ 编辑/拨测│    │
│  │ SeaTunnel│ 未配置  │ ○ 未部署  │ —      │ 部署/连接│    │
│  └──────────┴─────────┴──────────┴────────┴─────────┘    │
└──────────────────────────────────────────────────────────┘
```

详情抽屉（编辑/新建）：
1. **组件类型** select（llm/datahub/airflow/…，决定 connection 表单字段）。
2. **部署方式** radio：已有服务 / Docker / Kubernetes / 物理机。
   - 选「已有」→ 直接渲染 connection 表单（按 schema），底部「保存 + 拨测」。
   - 选「Docker/K8s/物理机」→ 渲染 deploy_spec 表单（image/compose 或 manifest/host），底部「部署」按钮 → 部署完自动填 connection（只读，可「重新部署」「卸载」）。
3. **连接信息** 区：部署成功后自动填入只读；「已有」模式可手填；secret 字段掩码。
4. **拨测** 按钮 + 结果（绿/红 + 延迟）。

现有 Tab 整合：
- 「LLM 与生成」→ LLM 行迁到基础设施；草稿并发度保留在此 Tab。
- 「数据连接」→ DataHub + 数据源迁到基础设施；本 Tab 删除或仅留说明。
- 「调度与语义」→ Airflow + Cube + SeaTunnel + sync-runner 迁到基础设施；本 Tab 删除。
- 「治理智能体 / 安全与鉴权」不动（不是部署型依赖）。

---

## 9. 实施计划（分阶段，可独立合并）

### Phase 0 — 地基（不破坏现状）
- 新增 `dependency_components` 表 + Alembic revision。
- 新增 `DependencyComponentService`（CRUD + schema 分发 + probe 分派）+ 路由 `/settings/dependencies/*`。
- `CONNECTION_SCHEMAS` / `DEPLOY_SPEC_SCHEMAS` / `PROBES` 三张表落地。
- **不接读取侧**：现有五表仍是事实源，新表并行存在。前端新增「基础设施」Tab 只读展示新表（可空）。

### Phase 1 — 迁移读取侧
- `ensure_defaults` 一次性把旧五表数据搬进 `dependency_components`（`deploy_mode='external'`）。
- `get_datahub_runtime` / `get_airflow_runtime` / `get_cube_runtime` / `get_llm_runtime` / 数据源读取改为从注册表投影，返回结构不变。
- 旧兼容路由 `/settings/llm-services` 等转发到新接口。
- 全量后端测试应全绿（读取侧契约不变）。

### Phase 2 — 前端整合
- 新「基础设施」Tab 上线，组件列表 + 详情抽屉 + connection 表单（按 schema 生成）+ 拨测。
- 旧 Tab（数据连接 / 调度与语义）的对应面板改为读新接口；最终删除旧面板。
- LLM 多实例、数据源多实例的 CRUD 在新 Tab 完成。

### Phase 3 — 部署能力（按组件逐步开）
- `ExternalDeployer`（Phase 1 已隐含可用）。
- `DockerDeployer` + `docker/components/<key>.yml` 片段：先 datahub / doris / seatunnel / sync_runner / cube（有官方镜像、compose 友好的先上）。
- `K8sDeployer`：manifest/Helm，先 airflow / doris / cube。
- `BareMetalDeployer`：登记模式先行（不跑安装脚本，只收 host+port）；执行模式按需。
- 每开一个组件的部署，补其 `docker/components/<key>.yml` 或 K8s manifest + 拨测 + 回收连接模板。

### Phase 4 — 收尾
- 旧五表在下个 major 删除（Phase 1 后已是投影视图，可安全删）。
- `DEPLOYMENT.md` 更新：部署方式从「compose 一把梭 + 外部假设在跑」改为「设置页统一管理」。

---

## 10. 不变量与边界

1. **上层服务零感知部署方式**——`get_*_runtime` 返回同一结构，chat_bi / materialization / data_app 不关心组件是 docker 还是 k8s 起的。
2. **凭据单点存储**——注册表一处（sync-runner 凭据仍归 runner，ontoMeta 代填不落库的既有原则保留）。
3. **external 模式 = 今天的行为**——不选部署、只填连接，零回归。
4. **部署不替代 compose 主栈**——ontoMeta 自身的 API/Frontend/PG 仍由 `docker-compose.yml` 起；本设计只管「依赖组件」。`postgres` key 仅当 ontoMeta 想把自身 PG 也纳管时才用（默认 external，由 compose 管）。
5. **K8s 模式下不用 docker 模式**——部署后端在 K8s 里时，组件部署也走 K8s（Deployer 按运行环境可用性收窄可选项，UI 上不可达的模式置灰并说明）。
6. **拨测不阻断**——连接存得上、拨测失败只标红，不挡保存（与今天一致）。

---

## 11. 与现有代码的接缝（落地对照）

| 现有文件 | 改动 |
|----------|------|
| `app/models/settings.py` | 新增 `DependencyComponent`；旧五表保留至 Phase 4 |
| `app/services/settings_service.py` | `get_*_runtime` 改投影；新增 `DependencyComponentService`；`ensure_defaults` 加迁移 |
| `app/api/settings.py` | 新增 `/settings/dependencies/*` 路由；旧路由转薄层 |
| `app/connectors/` | 新增 `deployers/{docker,k8s,bare_metal,external}.py`；`PROBES` 复用现有 `airflow.py`/`sync_runner.py`/`datahub.py` 客户端 |
| `frontend/src/pages/SettingsPage.tsx` | 新增「基础设施」Tab；旧 Tab 整合（Phase 2） |
| `frontend/src/components/` | 新增 `DependencyPanel.tsx` + `DependencyDrawer.tsx`（按 schema 生成表单） |
| `docker/components/*.yml` | 新增：每组件一份 compose 片段（Phase 3） |
| `alembic/versions/` | 新增 revision：建 `dependency_components` + 数据迁移 |

---

## 12. 风险与对策

| 风险 | 对策 |
|------|------|
| 后端在容器里，docker 模式要 docker.sock | 文档标注：docker 模式部署组件需后端能访问 Docker 引擎（挂 sock 或宿主机跑）；K8s 部署的后端用 k8s 模式 |
| K8s 凭据/kubeconfig 敏感 | kubeconfig 不落库，用 in-cluster 或挂载；Secret 注入凭据 |
| 部署长任务阻塞请求 | 异步执行 + `deploy_status` 轮询，复用现有任务模式 |
| 旧表迁移丢字段 | Phase 1 迁移在 `ensure_defaults` 里幂等做，先写再切读取侧；保留旧表至 Phase 4 可回退 |
| 组件版本/镜像差异 | `deploy_spec.image` 可覆盖；compose 片段用 ARG/`${VAR}` 占位，与 orchestration 栈的 `IMG_*` 同套路 |
| 拨测假绿灯 | 复用现有两步拨测（Airflow health+API、LLM 真 chat），不退回单步 |
