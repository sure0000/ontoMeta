# Agent 接入控制台 —— MCP / Skill / Token 统一管理实施方案

> 面向**冷启动的执行 agent**：你没有产生本方案的对话上下文。本文自包含，照此执行即可。
> 先读「背景」「现状地图」两节建立坐标，再按 P0 → P1 → P2 逐项实施。
> 本文所有关于现状的陈述都核对过真实代码（2026-09-04，分支 `v3`）。
> 注意：`docs/MCP_*.md` 里的**设计稿是想象稿**，引用了本仓不存在的模型；以真实代码为准。

## 需要先拍板的两处

实施前请与用户确认这两条；其余决策本文已定。

1. **菜单命名**：本文按「**Agent 接入**」写（路由 `/agent-access`）。备选：「MCP 接入」（更窄，
   但 skill/token 不只服务 MCP）、「智能体接入」。改名只影响 `Layout.tsx` 的 label 与路由常量。
2. **Skill 的权威源**：本文选「**代码内置 pack 为默认 + DB 覆写**」（见 P1 决策 D1，附被否方案）。
   若用户要求「纯 DB 编辑」，P1 的测试策略要跟着改——`test_dsh_skill.py` 的工具覆盖断言
   将失去静态可测性，需要改成对种子数据断言。

---

## 背景（为什么做）

三样东西属于同一件事——**让一个外部 agent 安全、正确地用上 ontoMeta**——却散落在三个地方，
其中一样根本没有管理面：

| 能力 | 今天在哪 | 能做什么 | 问题 |
|---|---|---|---|
| MCP 服务 | 设置页 → 「MCP 服务」Tab（`McpPanel.tsx`，461 行） | **只读**：服务形态、工具清单、审计、统计（3 个 GET，没有任何写操作） | 埋在设置页第 5 个 Tab；看得见改不了——限流、HTTP 开关、默认角色全在 `app/config.py` 读 env |
| Token | 设置页 → 「安全与鉴权」Tab（`PrincipalsPanel.tsx`，233 行） | 完整 CRUD + 轮换 | 与 MCP 完全不交叉引用。可 MCP 的整个安全模型就是「发什么角色的令牌 = 授权这个 agent 能做什么」 |
| Skill | `.dsh/skills/`（6 份 SKILL.md） | **没有任何管理面** | 只靠 dsh profile 里一条**绝对路径**挂载才被发现；换台机器、换个客户端就没了 |

**Skill 那一格是这次的战略核心**。MCP 只提供工具，「什么时候用哪个、结果怎么解读、什么话不能说」
全在 skill 里。今天：

- dsh 之外的任何 MCP 客户端（Claude Desktop、Cursor、远程 HTTP agent）连上来拿到 **29 个工具、
  零指引**；
- 而这个领域最危险的错误恰恰**不报错**——照口径文字重写 SQL、按字段名猜 JOIN、按命名规则拼
  `ods_xxx` 表名，产出的都是语法合法、看起来合理的**错答案**。

换句话说：**我们把最容易出错的部分写成了指引，却只在一台机器的一个客户端上生效。**

## 目标 / 非目标

**目标**
1. 把 MCP、Skill、Token 提升为一级菜单下的三个页面，形成「起服务 → 发令牌 → 给指引」的完整闭环。
2. 让 skill 成为**服务端交付的一等资产**：任何 MCP 客户端连上来都能拿到指引，不再依赖本地目录挂载。
3. 让 MCP 从「只读看板」变成「可管理的服务」：运行期配置落库、可在页面上改。
4. 令牌与 MCP 打通：发一个令牌时看得见它能调哪些工具，并能一键生成客户端配置。

**非目标**
- 不改 MCP 工具本身的语义、角色门槛或审计口径（那套已稳定，2265 passed）。
- 不做资源级权限（某主体只能看某数据域）——那是独立议题，见 `backend/app/mcp/STATUS.md` 待办。
- 不做 skill 的可视化编辑器；纯 Markdown 文本编辑 + 预览即可。
- 不改 Data Agent（Chat BI）。

---

## 现状地图（执行前必读）

### 后端

- **MCP 服务器**：`backend/app/mcp/server.py`。`build_server()` 目前只注册
  `on_list_tools` / `on_call_tool`。SDK 是 `mcp==2.1.1`，`Server.__init__` **已支持**
  `on_list_prompts` / `on_get_prompt` / `on_list_resources` / `on_read_resource`
  以及服务器级 `instructions`——P1 要用的原语无需升级依赖。
- **工具**：`backend/app/mcp/tools/*.py`，`@register_tool` 注册进 `TOOL_REGISTRY`（**29 个**）。
  新增模块必须加进 `tools/__init__.py` 末尾的 import 清单，否则静默不注册。
- **授权**：`server.handle_call_tool` 调用前按 `tool_required_role(tool)` 集中 fail-closed 拦；
  四层角色 `reader < editor < reviewer < publisher`（`app/models/principal.py`）。
- **审计**：`app/mcp/audit.py` → `mcp_audit_logs`（append-only、脱敏）。
- **共享自省层**：`app/mcp/introspection.py`——`service_status()` / `tool_catalog()` /
  统计与审计聚合，MCP 工具与 REST `/api/mcp/*` 共用一份，**勿在别处另写聚合**。
- **REST**：`app/api/mcp.py` 提供 `GET /api/mcp/info|stats|audit`（后两个要 publisher）。
  **没有任何写接口**。
- **Token**：`app/api/principals.py` —— `GET/POST /api/principals`、
  `PATCH /api/principals/{id}`、`POST /api/principals/{id}/rotate-token`、
  `DELETE /api/principals/{id}`、`GET /api/principals-policy`。
  模型 `Principal`：`name / role / token_hash / token_prefix / active / last_used_at`。
  **明文令牌只在创建与轮换时返回一次**，库里只有 SHA-256 哈希与前缀。
- **MCP 运行期配置**：全部在 `app/config.py`（读 env / `.env`）——
  `mcp_default_role`、`mcp_rate_limit_per_minute`、`mcp_execute_sql_rate_limit_per_minute`、
  `mcp_http_enabled`、`mcp_http_allow_anonymous`。
  **这违反 `docs/DEVELOPMENT_PRINCIPLES.md` P1**（运行期配置必须走 Web 设置页、DB 为唯一权威源，
  不得新增运行期读 `settings.<x>` 的配置项）。P2 修它。
- 落库配置的既有落地方式：`dependency_service.py::CONNECTION_SCHEMAS` 自描述 schema +
  `DependencyComponent.connection_json`（**加字段不需要 DB 迁移**）+
  `SettingsService.get_*_runtime(db)` 暴露给运行期。

### 前端

- 路由在 `frontend/src/App.tsx`（`react-router-dom` 的 `<Routes>`），菜单在
  `frontend/src/components/Layout.tsx` 的 `menuItems`（antd `Menu`，`/ontology` 与 `/tasks`
  已有二级 children，照抄即可）。
- 设置页 `pages/SettingsPage.tsx`（333 行）用 antd `Tabs`，5 个 key：
  `infra` / `generation` / `data-sources` / `security` / `mcp`。
- API 客户端集中在 `frontend/src/api`（`api.getMcpInfo/getMcpAudit/getMcpStats`、
  `api.listPrincipals/createPrincipal/updatePrincipal/rotatePrincipalToken/deletePrincipal`）。

### Skill

- 6 份：`ontometa-discovery` / `ontometa-query` / `ontometa-task-plan` /
  `ontometa-task-execute` / `ontometa-admin` / `ontometa-mcp`（总入口，
  `disable-model-invocation: true`，只做路由 + 共同底线 + 输出契约）。
- frontmatter：`name` / `description` / `whenToUse` / `disable-model-invocation` / `user-invocable`。
- `backend/tests/test_dsh_skill.py`（14 条）已把「齐备」变成被检查的属性：
  **`TOOL_REGISTRY` 里每个工具都必须在某份 skill 里被提到**（已负向自检过会拦下），
  且各 skill 的关键指引断言在自己身上而非并集。**这套测试必须保留并跟着迁移。**
- 发现方式：`/Users/me/.dsh/profiles/web/cordis.patch.yml` 把
  `/Users/me/Documents/ontoMeta/.dsh/skills` 挂成自定义 skill 根（绝对路径，一台机器有效）。

---

## 信息架构：新增一级菜单

`Layout.tsx` 的 `menuItems` 里，在「数据应用」与「设置」之间插入：

```
{ key: "/agent-access", icon: <ApiOutlined />, label: "Agent 接入", children: [
    { key: "/agent-access/service", label: "服务与工具" },
    { key: "/agent-access/skills",  label: "技能" },
    { key: "/agent-access/tokens",  label: "令牌" },
]}
```

`App.tsx` 加三条路由 + 一条 `/agent-access` → `/agent-access/service` 的 `Navigate`。

**设置页的处置**（重要，别留两份入口）：
- 删掉 `mcp` Tab，其内容整体搬到「服务与工具」页。
- 「安全与鉴权」Tab **保留**，但只留「管理 Token（本机 localStorage 的 Admin Token）」那一节——
  那是本机开发便利，不是 agent 接入。把 `<PrincipalsPanel />` 从这里移到「令牌」页。
- 两处都加一句指路文案（设置页 →「Agent 接入」），避免用户在旧位置找不到。

---

## P0 · 菜单与页面骨架（搬家，不改语义）

只做位置迁移，**不引入新行为**，先让三件事在一个菜单下齐活。

1. 新建 `frontend/src/pages/agent-access/`：`ServicePage.tsx` / `SkillsPage.tsx` / `TokensPage.tsx`
   （外加一个 `index.ts` 统一导出，与 `pages/chat-bi` 的组织方式一致）。
2. `ServicePage` = 现有 `<McpPanel />` 原样搬入（它已经包含服务形态、工具清单、审计、统计四块）。
3. `TokensPage` = 现有 `<PrincipalsPanel />` 原样搬入。
4. `SkillsPage` = P1 的落点，P0 先放一个「即将上线」的占位说明，或直接并入 P1 一起做。
5. 菜单、路由、设置页 Tab 删除与指路文案。

**验收**：`/agent-access/service` 与 `/agent-access/tokens` 功能与搬家前完全一致；
设置页不再有 MCP Tab；旧路径 `/settings` 仍可访问且不报错。

---

## P1 · Skill 成为一等资产（最大增量）

### 决策 D1：权威源 = 代码内置 pack + DB 覆写

**选定**：
- **默认 pack 随后端代码走**：把 6 份 skill 从仓库根 `.dsh/skills/` 迁到
  `backend/app/mcp/skills/<skill-name>/SKILL.md`。理由：它们与工具同生共死（工具改了指引就得改，
  `test_dsh_skill.py` 正是在钉这条），必须能随后端一起打包、部署、测试；放在仓库根意味着
  后端在没有仓库根的环境里读不到自己的指引。
- **DB 只存覆写与开关**：新表 `mcp_skills`（见下），有覆写就用覆写，没有就用内置 pack。
- 运行期解析顺序：`DB 覆写(active) → 内置 pack`。

**为什么不是纯 DB**（被否）：`test_dsh_skill.py` 的「每个工具都必须有 skill 指引」是这套体系
最有价值的一条保险；内容全进 DB 后它只能对种子数据断言，而种子和真实运行内容会分叉。
**为什么不是纯文件只读**（被否）：不同部署确实需要在不改代码的前提下调指引（换措辞、加本地约定），
这正是 `docs/DEVELOPMENT_PRINCIPLES.md` P1 的精神。

**先例**：与 `GovernanceStandardRecord`（`app/models/governance.py`）同构——代码里的 pack 是权威，
DB 记录版本戳与快照。照那个模式做，别另起炉灶。

### 数据模型

新表 `mcp_skills`（一次 Alembic 迁移）：

| 列 | 说明 |
|---|---|
| `id` | uuid 主键 |
| `name` | skill 标识名，唯一（如 `ontometa-query`） |
| `body_md` | 覆写正文（含 frontmatter）；为空表示不覆写 |
| `enabled` | 是否对外交付（默认 true）。关掉的 skill 不进 `list_prompts` |
| `source` | `builtin`（跟随内置 pack）/ `override`（已改写） |
| `builtin_digest` | 覆写发生时内置 pack 的摘要——内置 pack 后续升级时据此提示「上游已更新」 |
| `updated_by` / `updated_at` | 谁在什么时候改的 |

> **迁移前先取号**：本仓修订号是手写顺序 hex，极易撞号；撞号的症状是整套 pytest 炸成几百个
> error、完全看不出跟迁移有关。加迁移前先用脚本扫一遍现有 `down_revision` 链取号。

### 后端：服务层 + MCP 原语 + REST

**服务层** `backend/app/mcp/skills.py`（新）：
- `list_skills(db) -> list[SkillView]`：合并内置 pack 与 DB 覆写，带 `enabled` / `source` /
  `frontmatter` / `body`。
- `get_skill(db, name)`；`resolve_body(db, name)`。
- `builtin_pack()`：只读内置 pack（不碰 DB），供测试与「恢复默认」。
- frontmatter 解析复用 `yaml.safe_load`，与 `test_dsh_skill.py` 同一套解析口径。

**MCP 原语**（`server.py` 的 `build_server()`）：
1. `on_list_prompts` / `on_get_prompt`：**一份 skill = 一个 prompt**。
   `list_prompts` 返回 `name` + `description`（用 frontmatter 的 `description`）
   + `title`（`whenToUse` 摘要）；`get_prompt(name)` 返回单条消息，内容是 skill 正文。
   这样任何 MCP 客户端都能拿到指引，不再依赖本地目录挂载。
2. `instructions=`：服务器级指引，客户端在 `initialize` 时就拿到，很多客户端会自动并入系统提示。
   **只放最小集**：三条会安静出错的红线 + 五个专用 skill 的路由表（即
   `ontometa-mcp/SKILL.md` 的前两节）。**不要**把 6 份正文全塞进去——那会挤占每一次会话的上下文。
3. 与工具一样，prompt 的读取也要进审计（复用 `audit.record_call` 或平行的轻量记录），
   否则「这个 agent 到底有没有拿到指引」在事后无从判断。

**REST**（`app/api/mcp.py` 追加，前端用）：
- `GET  /api/mcp/skills`（reader）：清单 + 是否被覆写 + 上游是否更新。
- `GET  /api/mcp/skills/{name}`（reader）：内置正文与当前生效正文，供 diff。
- `PUT  /api/mcp/skills/{name}`（publisher）：写覆写。**必须过校验**（见下）。
- `DELETE /api/mcp/skills/{name}/override`（publisher）：恢复默认。
- `PATCH /api/mcp/skills/{name}`（publisher）：启用/停用。

**保存前校验（服务端强制，不能只在前端拦）**：
- frontmatter 必须能解析，且 `name` 与路径一致、`user-invocable` 为 bool、
  `disable-model-invocation` 为 bool；
- 正文必须含输出契约（现有测试的口径：出现「结论」）；
- 专用 skill 必须含「通用底线」段；
- **覆写后仍要满足「每个注册工具都被某份 skill 提到」**——把 `test_dsh_skill.py` 的那条覆盖
  判定抽成服务层函数 `skill_coverage_gaps(db)`，测试与保存校验共用一份，保存时缺工具就拒绝并
  列出缺哪些。这条是整套体系的保险，不能因为「从 UI 改」就绕过去。

### 前端：技能页

- 左侧 skill 列表（名称、`whenToUse` 摘要、`builtin`/`已改写` 标签、启用开关、覆盖工具数）；
- 右侧 Markdown 正文查看/编辑 + 预览；「保存覆写」「恢复默认」「上游已更新，查看 diff」；
- 顶部一块**覆盖度**面板：29 个工具 × 是否有 skill 指引，缺的高亮——这是这一页存在的意义，
  让「新加了工具没写指引」在界面上就看得见，而不是等 CI 报错；
- 保存失败时把服务端返回的缺失工具列表原样显示，别只弹一句「保存失败」。

### dsh 侧的迁移

- 改 `/Users/me/.dsh/profiles/web/cordis.patch.yml` 的自定义 skill 根：
  `/Users/me/Documents/ontoMeta/.dsh/skills` → `/Users/me/Documents/ontoMeta/backend/app/mcp/skills`。
  **profile 改完要重启 dsh**（改正文只需 watcher 刷新，改配置必须重启）。
- 删除仓库根 `.dsh/`，避免两份并存后漂移（这次会话里总入口 skill 就是因为和专用 skill 并存
  两份内容而漂过一次）。
- 同步更新 `docs/DSH_ONTOMETA_SKILL.md` 里的路径与「启用配置」一节。

---

## P2 · MCP 可管理 + 令牌与 MCP 打通

### P2.1 运行期配置落库（修 P1 原则违规）

把这 5 项从 `app/config.py` 迁到 Web 设置（DB 权威源），在「服务与工具」页可改：

| 配置 | 含义 |
|---|---|
| `mcp_http_enabled` | 是否挂 `/mcp` 远程 HTTP 传输 |
| `mcp_http_allow_anonymous` | 无令牌请求是否回落默认角色（**公网必须为否**） |
| `mcp_default_role` | 匿名/未匹配令牌的角色 |
| `mcp_rate_limit_per_minute` | 全局每工具每分钟上限 |
| `mcp_execute_sql_rate_limit_per_minute` | `execute_sql` 单独更严的上限 |

- 落地方式照 `CONNECTION_SCHEMAS` 的自描述 schema + `connection_json`（加字段免迁移），
  运行期通过 `SettingsService.get_*_runtime(db)` 取，调用方接 `runtime_config` 而不是读 `settings.<x>`。
- **`ONTOMETA_MCP_TOKEN` 不迁**：它是客户端拉起 MCP 子进程时注入的**身份**，属于引导期变量，
  不是服务端配置。迁了反而说不通。
- 改「HTTP 开关 / 匿名」这类会立刻改变对外暴露面的项时，前端要二次确认。

### P2.2 令牌页与 MCP 打通

在「令牌」页，每个 Principal 除现有字段外补三样（数据都已存在，只是没连起来）：

1. **这个角色能调哪些工具**：拿 `introspection.tool_catalog()` 的 `required_role` 与该主体角色
   做 `role_satisfies` 比对，给「可调 N / 共 29」并可展开明细。发令牌的人当场看得见后果。
2. **最近调用**：`mcp_audit_logs` 按 principal 过滤的近 N 条（成功/被拒/被限流分色）。
   `last_used_at` 已在模型里，但「拿它做了什么」只有审计表知道。
3. **一键生成客户端配置**：按仓库根 `mcp-config.json` 的形状产出 stdio 配置片段
   （`command` / `args` / `cwd` / `env.ONTOMETA_MCP_TOKEN`），以及远程 HTTP 的
   `Authorization: Bearer` 示例。**明文令牌只在创建/轮换那一次可填入**，之后只显示
   `token_prefix` 占位——今天 `mcp-config.json` 里那行是手写占位符，正是因为没有这个入口。

### P2.3 建议默认（写进页面文案，不是硬编码）

给外部 agent **默认发 editor 令牌**（只到 `draft_task`/`validate_task`，看得见「将发生什么」
但动不了数仓）；确需自动执行再发 publisher。**发什么角色的令牌，就是「允不允许这个 agent
自动执行」的人为决定**——这是 MCP 侧替代「人在六环逐环确认」的机制，页面上要说清。

---

## 安全红线

1. **不要在任何页面回显令牌明文**。模型只存哈希与前缀，明文仅创建/轮换返回一次——保持这个
   语义，别为了「方便查看」加一个解密接口。
2. **不要把 Admin Token 交给外部 agent**。`backend/.env` 目前**确实存在**
   （`docs/DEVELOPMENT_PRINCIPLES.md` 称已删除，与事实不符——顺手核实并纠正文档）。
   dsh 那次事故就是 agent 读 `.env` 抓 admin token 绕过了 MCP 的全部门控。
   令牌页要显式引导「给 agent 发最小权限 Principal 令牌」。
3. **skill 覆写要 publisher**，且写操作进审计。skill 是 agent 的行为契约，能改它等于能改
   agent 的行为边界。
4. **`mcp_http_allow_anonymous` 打开 = 对外裸奔**。页面上要给明确警示，且默认关闭。
5. 新增的 REST 写接口沿用现有 `require_role` 依赖，别自己写一套鉴权。

---

## 测试要求

新增/改动的测试，全量必须绿（当前基线 **2265 passed, 1 skipped**）：

- `backend/tests/test_dsh_skill.py`：**改路径**到新 pack 位置，其余断言原样保留。
  特别是「每个注册工具都必须有 skill 指引」那条——它已负向自检过会拦下未写指引的工具。
- 新 `backend/tests/test_mcp_skills.py`：
  - `list_skills` 合并内置与覆写；覆写后 `source=override`、`resolve_body` 返回覆写正文；
  - 「恢复默认」后回到内置正文；
  - `enabled=false` 的 skill 不出现在 `list_prompts`；
  - **保存校验**：frontmatter 不合法 / 缺输出契约 / 覆写后有工具失去指引 → 拒绝并列出缺哪些；
  - REST：读要 reader、写要 publisher（reader 被拒）。
- 新 `backend/tests/test_mcp_prompts.py`：
  - `list_prompts` 返回 6 份（停用后减少）、`get_prompt` 返回正文；
  - 未知 prompt 名报错而不是空正文；
  - `instructions` 非空且**只含**红线与路由表（用长度上限断言，防止有人把 6 份正文塞进去）。
- P2.1 的配置迁移：新增「运行期从 DB 读、改了立刻生效」的测试，并**删掉**对
  `settings.mcp_*` 的直接依赖。
- 前端：按仓库现有前端测试惯例补页面渲染与路由跳转即可，不必追求覆盖率。

---

## 交付检查清单

- [ ] P0：一级菜单「Agent 接入」+ 三条路由；`McpPanel` / `PrincipalsPanel` 搬家；设置页删 MCP Tab、留指路
- [ ] P1：skill pack 迁到 `backend/app/mcp/skills/`；`mcp_skills` 表 + 迁移（先取号）
- [ ] P1：`app/mcp/skills.py` 服务层；`skill_coverage_gaps` 与测试共用一份判定
- [ ] P1：`on_list_prompts` / `on_get_prompt` + 服务器级 `instructions`；prompt 读取进审计
- [ ] P1：`/api/mcp/skills*` 五个端点（读 reader / 写 publisher）
- [ ] P1：技能页（列表 + 编辑 + 恢复默认 + **工具覆盖度面板**）
- [ ] P1：dsh profile 路径切换、删除仓库根 `.dsh/`、更新 `docs/DSH_ONTOMETA_SKILL.md`
- [ ] P2.1：5 项 MCP 运行期配置落库、页面可改、去掉运行期读 `settings.mcp_*`
- [ ] P2.2：令牌页展示可调工具数/明细、最近调用、一键生成客户端配置
- [ ] P2.3：最小权限令牌的引导文案
- [ ] `backend/app/mcp/STATUS.md` 追加本阶段记录
- [ ] 全量 pytest 绿

## 已知陷阱

- **不要提交** `backend/data/`（血缘补录运行产物）与 `frontend/tsconfig.tsbuildinfo`。**只在 `v3` 分支做**。
- **Alembic 撞号**：见上，加迁移前先取号。
- **antd v6**：`Spin` 的包裹类已从 `.ant-spin-nested-loading` 改名 `.ant-spin-section`，
  写死旧类名的整页滚动 CSS 会**静默失效**。
- **样式 token 真名是 `--om-surface*` 族**，不是 `--om-color-*`；写错会静默丢样式，跑 `lint:tokens`。
- **Tab 内联表单要 `forceRender`**，否则未激活 Tab 里的表单拿不到实例。
- **机密回显约定**：非机密全回显；机密走明文回显 + `Input.Password` 眼睛切换，「清空 = 保持原值」。
  但 Principal 令牌是哈希存储，**没有明文可回显**——只显示 `token_prefix`，别套用这条。
- **别在别处另写自省聚合**：`app/mcp/introspection.py` 是 MCP 工具与 REST 共用的那一份。
- **总入口 skill 是 `disable-model-invocation: true`**：模型自动路由时不加载它，所以安全底线
  必须每份专用 skill 自带。改 skill 结构时别把这条破坏掉（有测试钉着）。

## 参考

- 真实工具体系与各阶段记录：`backend/app/mcp/README.md`、`backend/app/mcp/STATUS.md`
- 配置原则：`docs/DEVELOPMENT_PRINCIPLES.md` P1
- skill 现状与约定：`docs/DSH_ONTOMETA_SKILL.md`、`backend/tests/test_dsh_skill.py`
- 上一阶段方案的写法与验收风格：`docs/MCP_DATA_AGENT_PARITY_PLAN.md`
