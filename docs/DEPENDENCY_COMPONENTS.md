# 基础设施组件

> ontoMeta **不部署任何依赖**，一律连接已经跑着的服务。设置页「基础设施」里只做两件事：
> 填连接信息、拨测。没有新增、没有删除、没有部署方式。

## 固定的那几样

| 组件 | key | 连接内容 | 配在哪 |
|------|-----|----------|--------|
| Doris 统一数仓 | —— | FE 的 SQL 端点 + HTTP 节点 + 凭据 | 数据源（面板里做投影展示） |
| LLM / 嵌入服务 | `llm` | api_base_url / api_key / model | `dependency_components` |
| DataHub | `datahub` | gms_url / frontend_url / token / fabric | `dependency_components` |
| Airflow 调度 | `airflow` | 调度 API（endpoint+账密）、DAG 投递（SSH） | `dependency_components` |
| Superset BI | `superset` | base_url / 账密 / 数仓 database_id / 对外地址 | `dependency_components` |

组件目录写死在 `app/services/dependency_service.py:COMPONENT_CATALOG`，顺序即面板显示顺序。
表里缺的行由 `ensure_components()` 在列表接口里补齐——面板上没有「新增」，缺的行只能后端补。

Superset 有两处刻意**不推导**的字段：

- `database_id` —— Superset 里那条指向数仓（Doris）的 database 连接。由 Superset 侧建好并自测通过，
  ontoMeta 只认这个 id。否则就得把数仓凭据再复制一份推给 Superset，那是另一套密钥的事。
- `public_base_url` —— 用户浏览器可达的地址。后端从哪儿访问 Superset 与用户从哪儿访问它是两回事
  （内网 ip vs 对外域名），跳转链接与嵌入 SDK 只能用后者；留空则回落到 `base_url`。

不在这里纳管的：

- **业务源库 / 其他数据源**（ERPNext、MySQL…）→ 「数据源」标签页（`DataSource`，完整 CRUD + 测试）
- **MCP 服务** → 它是 ontoMeta 自己的一层，只是借同一张表存运行期开关（`INTERNAL_KEYS`），
  界面在「Agent 接入」页
- **ontoMeta 自身数据库** → bootstrap 配置，走环境变量（见 `DEVELOPMENT_PRINCIPLES.md`）
- **Flink** → 没有独立连接：提交参数挂在 Airflow 的编排参数里（`settings.extra` 的 `flink_*`）

## 数据形状

一个组件一行 `dependency_components`：

- `connection_json` —— 怎么连。字段形态由 `key` 决定（`CONNECTION_SCHEMAS`），机密字段
  「留空 = 保持原值」。
- `settings_json` —— 附加配置：`extra`（现只有 Airflow 编排/Flink 参数）与 `_probe`
  （逐条连接的拨测记账）。
- `connection_status` —— `unknown` / `connected` / `failed`，**只由拨测写入**。保存连接不等于
  连接可用：改了连接就退回 `unknown`，避免"填完地址就显示已连接"的假绿灯；同理，改对了配置会
  把上次的红叉一并作废（`_drop_probe_ledger`）。

## 一个组件可以有几条连接

Airflow 一个组件握着两条互不相干的连接：**调度 API**（触发 DagRun，走 8080）与
**DAG 投递**（rsync 产物，走 22，甚至不是同一台机器）。它们常常一通一断，所以：

- 连接字段按 `CONNECTION_GROUPS` 分组自描述，前端据此分节渲染；
- 拨测可以只测其中一条（`POST /api/settings/dependencies/{id}/probe?target=ssh`）；
- 行状态由**各条的最新记账**聚合：有一条失败即 `failed`，全部通过才 `connected`，还有没测过的
  就停在 `unknown`。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/settings/dependencies/schema` | 组件目录 + 连接字段/分组自描述（前端据此生成表单） |
| GET | `/api/settings/dependencies` | 列出组件（先 `ensure_components` 补齐缺的行） |
| GET | `/api/settings/dependencies/{id}` | 单个组件 |
| PUT | `/api/settings/dependencies/{id}` | 改展示名 / 启停 / 连接 / `settings` |
| POST | `/api/settings/dependencies/{id}/probe` | 拨测，`?target=` 只测其中一条 |

上层功能不直接读这张表，走 `SettingsService.get_*_runtime()` 投影。
