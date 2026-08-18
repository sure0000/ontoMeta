# ontoMeta 开发原则（Development Principles）

> 本文件收录跨模块、长期有效的工程约束。与具体功能方案（`docs/*_PLAN.md`）不同，这里的每一条
> 都是「默认遵守、违反要有明确理由」的团队约定。改动涉及以下任一主题前，先读对应条目。

---

## P1. 配置只在 Web 端（数据库）配置，不读环境变量

**原则**：所有**运行期业务/连接配置**都由 Web 设置页管理、落库（DB 为唯一权威源），运行期一律
从 DB 读取。**不得**新增「运行期读 `os.environ` / `app.config.settings.<x>`」的配置项。

代码里早已把这条写成法则（`settings_service.py::get_airflow_runtime`：
*"配置只在设置页，不读环境变量"*）；DataHub / LLM / Airflow 连接均已走
`SettingsService.get_*_runtime(db)` 从 DB 读取，`config.py` 里的同名字段仅作**首启播种默认值**。

**怎么落地**（新增一个可配置项时）：
1. 加到自描述 schema（`dependency_service.py::CONNECTION_SCHEMAS` 或对应设置表）——前端设置页
   由 `/api/settings/dependencies/schema` 自动生成表单，通常**无需改前端**。
2. 连接类配置以 JSON 存 `DependencyComponent.connection_json`，**加字段不需要 DB 迁移**。
3. 通过 `SettingsService.get_*_runtime(db)` 暴露给运行期；调用方传 `runtime_config` 而非读 env。
4. 机密字段 `secret=True`：读侧只回 `*_set/*_hint`，留空保持不变（见记忆
   `settings-form-echo-convention`）。

**唯一例外——引导期（bootstrap）**，这些天然无法「存进它自己要连的库/由设置页配」：
- `DATABASE_URL`：设置页数据存在 DB 里，连 DB 的地址不可能存在 DB 里（先有鸡问题）。
- `ONTOMETA_ADMIN_TOKEN` / `API_KEY_HASH_PEPPER`：访问设置页本身要先鉴权；鉴权根不能由被它
  保护的页面来配。
- `DEBUG`、`CORS_ORIGINS`：进程启动时即需，早于任何请求。

**部署事实也要 Web 化**：Airflow 投递目录、Flink 执行参数（SqlRunner JAR / flink_bin /
deploy_target / parallelism / yarn_queue / checkpoint_dir）等「这套部署长什么样」的配置，
过去放 env，现已一律迁到【设置页 → Airflow/Flink】（落库，见 `AirflowRuntimeConfig` 与
`_AIRFLOW_EXTRA_FIELDS`）。判断标准不是「是不是部署相关」，而是「能不能 Web 化」——只要运行期
能从 DB 读到、且不造成引导期先有鸡问题，就必须走设置页。

例外项应尽量少、集中在引导期（`.env` 或部署编排的环境注入）里。**除此之外的任何配置都必须走
Web 端。** 本仓库已删除 `backend/.env`，引导期变量由 `service.sh` / `Makefile` 注入默认值。

---
