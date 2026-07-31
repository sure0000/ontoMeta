# Data Agent 改造设计文档

> 状态：**评审草案** · 目标：把「智能问数（ChatBI）」从单发问答升级为 Claude Code 式的**多步工具编排 Agent**
> 关联：[[chatbi-sends-full-ontology-413]]（P0 已治的 413）、`TECH_DESIGN.md`、`app/services/chat_bi.py`

---

## 1. 背景与目标

### 1.1 现状问题

当前 Data Agent（`ChatBiService.ask`）是**单发式问答**：一次 LLM 调用、固定 JSON schema、答案僵硬，无法追问、下钻或基于真实数据作答。用户反馈"回答过于死板，不够智能"。

### 1.2 改造目标

让 Data Agent 像 Claude Code 一样：

1. **会自己找信息** —— 按需多次检索本体（对象/关系/口径），而非一次性塞全量。
2. **会真正跑数** —— 不只"建议 SQL"，而是执行只读查询、拿到真实行、基于结果回答。
3. **过程可见** —— 用户能看到 Agent 每一步在调用什么工具、得到什么。
4. **多步推理** —— 一个问题可拆成"检索 → 建 SQL → 执行 → 解读"的链条，必要时反问澄清。

### 1.3 非目标（本次不做）

- 不做写操作（物化/回写 DataHub 仍走既有治理制品流程）。
- 不引入向量库/语义检索（现状全是 ILIKE，够用；留待后续）。
- 不替换会话管理、分类、生成数据应用等既有 UI。

---

## 2. 现状盘点（代码锚点）

| 维度 | 现状 | 锚点 |
|---|---|---|
| 入口 | `ask()` 单发，`asyncio.to_thread` 包一次阻塞 `_llm_answer` | `chat_bi.py` |
| 结构化 | `response_format=json_object` + 手工 normalize；**实测 GLM 此模式吐坏 JSON** | `chat_bi.py:_llm_answer` |
| 检索 | Python token 重叠打分取 top-3，一次性灌 prompt | `chat_bi.py:_match_objects/_match_logics` |
| 跑数 | 仅产出 `suggested_sql` 字符串；执行是**另一个手动动作** | `POST /chat-bi/messages/{id}/execute` |
| 工具调用 | **全仓零 `tools=`** | — |
| 传输 | 单 JSON，无流式；前端一个"思考中"气泡→终态替换 | `api.ts` / `ChatBiPage.tsx:submit()` |
| 超时 | `make_http_client()` httpx 默认 **5s**、无重试（潜在失败源） | `common.py:make_http_client` |
| 历史 | 来自请求体，非 DB 回灌 | `chat_bi.py:ask(history=…)` |

### 2.1 可复用资产（关键：多数已存在）

- **工具注册表 + 分发器已存在**：`app/services/external_api.py` 的 `EXTERNAL_MCP_TOOLS`（10 个只读工具，含 `input_schema`/`required_scope`/`output_fields`）+ `call_tool()` 分发 + `_validate_arguments()` 校验。
- **只读 SQL 执行原语已存在**：`data_app_executor.execute_sql(dsn, sql, limit, timeout) → (columns, rows)`，带 `is_read_only` 守卫 + 自动 LIMIT + 方言翻译 + 超时。
- **检索能力齐备**：`OntologyQueryService.list_object_types(q=…)` / `get_object_type` / `list_relation_types` / `get_ontology_grouped_graph` / `list_business_logics` / `get_business_logic`。
- **DataHub 信号**：`DataHubConnector.search_datasets` / `get_dataset_by_urn`（行数 + 样例值）。
- **前端步骤范式**：`AgentsPanel.tsx`（drafted→executing→succeeded 状态机 + 步骤抽屉）。
- **过程持久化位**：`ChatBiMessage.payload`（JSON Text），扩 `steps[]` 即可回放。

### 2.2 可行性实证

自建 GLM-5.1（`glm-local`）**支持原生 OpenAI tool-calling**，已实测 2 周闭环：

```
round1: finish_reason=tool_calls, tool_calls=[query_objects({"keyword":"客户"})]
   → 回灌 role:tool 结果
round2: finish_reason=stop, 基于工具结果输出接地答案（真实字段表格）
```

结论：**走 native tools，不走 json_object**（后者在该模型上输出畸形）。

---

## 3. 目标架构

### 3.1 总览

```
用户提问
   │
   ▼
ChatBiAgent.run(db, domain_id, question, history)
   │   system prompt（问答助手 + 工具纪律 + 只读约束）
   │   tools = 检索工具(⊂ EXTERNAL_MCP_TOOLS) + run_sql
   ▼
┌─────────────── Agent 循环（≤ MAX_STEPS） ───────────────┐
│  LLM.create(messages, tools=…, tool_choice=auto)         │
│     finish_reason == "tool_calls" ?                      │
│        ├─ 是：并发 dispatch 每个 tool_call               │
│        │      → 追加 role:assistant(tool_calls)           │
│        │      → 追加 role:tool(每个结果, 截断保护)         │
│        │      → emit step 事件 → 继续循环                  │
│        └─ 否："stop" → 最终 markdown 答案 → 跳出          │
└──────────────────────────────────────────────────────────┘
   │
   ▼
收尾 harvest：从工具轨迹收割结构化产物
   referenced_objects  ← 本轮 get_object/search_objects 命中
   suggested_sql + data_result(columns/rows) ← run_sql 入参与返回
   steps[]             ← 全过程工具调用轨迹（落 payload 持久化）
```

### 3.2 核心设计原则

1. **接地由构造保证**：工具只返回真实 id/name，答案引用天然可落地。弱化脆弱的 `_enforce_grounded_refs` 后过滤（保留为兜底）。
2. **结构从轨迹收割，不逼模型吐 JSON**：答案正文是自然 markdown（可流式）；`referenced_*`/`suggested_sql`/`data_result` 由工具调用记录派生。
3. **Token 预算天然受控**：工具分页/按需返回，**不再全量灌本体 → 永久根治 413**。
4. **只读优先**：`run_sql` 复用 `is_read_only` + LIMIT + 超时，仅对非 mock `DataSource` 生效。
5. **运行时对齐**：ChatBI 切**异步 OpenAI 客户端 + 显式 `llm_timeout_seconds`**（对齐 `draft_generator`），弃用 5s 默认超时。

---

## 4. 工具目录

按 `domain_id → get_published_ontology` 限定范围、`published_only=True`、全部只读。

| 工具 | 后端支撑 | 输入 | 输出 |
|---|---|---|---|
| `search_objects` | `list_object_types(q=…)` | `keyword, limit?` | 对象列表（id/name/display/desc） |
| `get_object` | `get_object_type` | `object_id` | 字段 + 进出关系 + 相关逻辑 |
| `search_relations` | `list_relation_types(q=…)` | `keyword` | 关系列表 |
| `get_relation` | `get_relation_type` | `relation_id` | 源/目标对象 + 基数 + 映射 |
| `search_logics` | `list_business_logics(q=…)` | `keyword` | 口径/指标列表 |
| `get_logic` | `get_business_logic` | `logic_id` | 表达式 + 绑定对象/字段 + 口径 |
| `get_domain_overview` | `get_ontology_grouped_graph` | — | 域级聚类概览（有哪些业务板块） |
| `get_object_graph` | `get_ontology_graph` | `center_id, depth` | 邻域下钻子图 |
| **`run_sql`** ⭐ | `execute_sql`（只读守卫） | `sql, limit?` | `columns[], rows[]`（真实数据） |
| `get_table_profile`（P3，可选） | `DataHubConnector.get_dataset_by_urn` | `dataset_urn` | 行数 + 样例值 |

**`run_sql` 安全边界**：① `is_read_only` 拒绝非 SELECT/多语句/危险函数；② 自动追加/收敛 LIMIT；③ 语句超时（postgres `statement_timeout`）；④ 仅解析到本域 `DataSource.dsn_secret_ref` 的真实连接串，mock/无 DSN 源直接拒绝；⑤ 表名/字段用本体物理映射校正。

---

## 5. 数据契约变更

### 5.1 `ChatBiAnswer` 扩展（新增字段，向后兼容）

```jsonc
{
  "answer": "…markdown…",
  "suggested_sql": "SELECT …",         // 保留
  "referenced_objects": [ … ],          // 保留（改由轨迹收割）
  "referenced_logics":  [ … ],          // 保留
  "caliber_decomposition": [ … ],       // 保留（可由步骤映射生成）
  "used_mock": false,

  // ── 新增 ──
  "steps": [                            // Agent 过程轨迹（持久化到 payload）
    { "index": 0, "tool": "search_objects",
      "arguments": { "keyword": "客户" },
      "status": "succeeded",
      "summary": "命中 3 个对象", "duration_ms": 42 },
    { "index": 1, "tool": "run_sql",
      "arguments": { "sql": "SELECT …", "limit": 100 },
      "status": "succeeded", "summary": "返回 100 行", "duration_ms": 210 }
  ],
  "data_result": {                      // run_sql 的真实结果（供前端表格）
    "columns": [ { "key": "customer_name", "title": "customer_name" } ],
    "rows":    [ { "customer_name": "…" } ],
    "truncated": false
  }
}
```

### 5.2 `steps[]` 语义

- 每个工具调用一条；`status ∈ {running, succeeded, failed}`；`summary` 是人类可读摘要（非原始大结果）。
- 原始工具结果**不落 `steps`**（避免膨胀），只保留摘要；`run_sql` 结果单独进 `data_result`。
- 历史回放：`getChatBiMessages` 读 `payload.steps` 原样渲染。

---

## 6. 传输层：两阶段

### P1（先非流式）

保留 `POST /chat-bi/ask` 单 JSON，但返回扩展后的 `ChatBiAnswer`（含 `steps[]`/`data_result`）。前端把 `steps` 渲染成**可折叠步骤轨迹** + 结果表格。**无需碰传输层即可上线"会查会算"。**

### P2（流式 SSE）

新增 `POST /chat-bi/ask/stream`（`text/event-stream`）：

| 事件 | 载荷 | 前端动作 |
|---|---|---|
| `step` | `{index, tool, arguments, status, summary}` | 追加/更新步骤条 |
| `token` | `{delta}` | 追加答案 markdown |
| `data` | `{columns, rows}` | 渲染结果表 |
| `done` | 完整 `ChatBiAnswer` | 定稿 + 落库 |
| `error` | `{message}` | 友好报错（复用 `_friendly_llm_error`） |

前端 `api.ts` 新增流式 reader（`ReadableStream`/`getReader`，全仓首例）；`submit()` 从"原子替换 pending 气泡"改为"随事件追加/patch"。旧 `/ask` 保留为降级。

---

## 7. 安全 · 可靠性 · 成本护栏

| 护栏 | 措施 |
|---|---|
| 步数上限 | `MAX_STEPS`（默认 6），超出则收尾强制作答 |
| 工具超时 | 每个工具单独超时；`run_sql` 用 `execute_sql` 的 `timeout_seconds` |
| LLM 超时 | 异步客户端 + `llm_timeout_seconds`（弃用 5s 默认） |
| 只读 | `run_sql` 走 `is_read_only`；无写工具暴露 |
| 结果截断 | 工具结果回灌前按字符上限截断（承接 P0 的 `_MAX_KNOWLEDGE_CHARS` 思路） |
| 上下文 | 分页工具 + 截断 → 天然不触 413 |
| 失败降级 | 循环级 try/except → `_friendly_llm_error`；硬失败回退规则摘要 |
| 幂等/审计 | `steps[]` 落 `payload`，可复盘每一步工具与入参 |

---

## 8. 分阶段落地

| 阶段 | 范围 | 主要改动文件 | 风险 |
|---|---|---|---|
| **P0** ✅ | 知识包裁剪治 413 | `chat_bi.py`（已被 P1 结构性取代） | — |
| **P1** ✅ **已交付** | Agent 循环 + `run_sql` + `steps[]`/`data_result` 契约（非流式） | 后端 `chat_bi.py`（`_llm_answer`→`_run_agent_loop`）+ 工具适配层；`schemas/chat_bi.py`；前端 `types.ts` + `ChatBiReferences.tsx` + `chat-bi.css` | 中 |
| **P2** | SSE 流式端点 + 前端流式 reader + 实时轨迹 | 后端新端点；前端 `api.ts` 流式、`ChatBiPage.submit()` 改增量 | 中大（前端绿地） |
| **P3** | DataHub profile 工具、澄清反问、结果一键生成图表（复用 `generate-widget`）、DB 历史回灌 | 增量 | 小-中 |

**推荐从 P1 起步**：复用已有工具目录 + 已有只读 SQL 原语，改动集中在 `chat_bi.py` 加一个薄适配层，不动传输层即可让 Agent"会查会算"，收益最大、风险最小。

---

## 9. 评审决策（已定稿）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | `run_sql` 是否 P1 即开放 | ✅ **P1 即开放（受控）**：仅非 mock `DataSource` + `is_read_only` + 自动 LIMIT + 语句超时 |
| 2 | 步骤轨迹默认展示 | ✅ **默认折叠，失败自动展开**（一行"已执行 N 步"，点击展开） |
| 3 | `caliber_decomposition` 处置 | ✅ **保留字段，由 `steps` 映射生成**（前端口径卡零改动） |
| 4 | P1 是否切异步 LLM 客户端 | ✅ **是，一并切**（异步 OpenAI + `llm_timeout_seconds`，顺带修 5s 默认超时潜因） |
| 5 | 运行阈值取档 | ✅ **均衡档**（见下方常量表） |

### 9.1 运行常量（均衡档）

```python
MAX_STEPS            = 6      # Agent 循环步数上限，超出强制收尾作答
RUN_SQL_LIMIT        = 100    # run_sql 默认返回行上限
TOOL_RESULT_MAX_CHARS = 8000  # 单个工具结果回灌前的截断阈值
SQL_TIMEOUT_SECONDS  = 15     # run_sql 语句超时（execute_sql 既有能力）
# LLM 调用超时沿用 settings.llm_timeout_seconds（默认 300s）
```

> 以上为 P1 起始值，上线后按真实问数分布调参。

---

## 10. 附录：与既有能力的关系

- **不替代治理制品流程**：物化/回写仍走 `agent_pipeline` 的 draft→confirm→execute，Data Agent 只读。
- **复用生成数据应用**：`run_sql` 结果可直接喂给既有 `generate-app`/`generate-widget`，形成"问 → 查 → 建看板"闭环。
- **本体是一级源**：Agent 的所有检索都基于**已发布本体**，延续"本体=一级源数据"的战略方向。

---

## 11. P1 实现说明（与设计的差异 · 已交付）

落地时对设计做了三处务实调整，均已在实机（自建 GLM-5.1 + ERP 全量域）验证：

1. **检索工具专用适配层，而非直接复用 `EXTERNAL_MCP_TOOLS.call_tool`。**
   原因：MCP 目录的 `list_object_types` 只按 `domain_id` 全量返回（ERP 会一次吐 734 对象，重蹈 413）。
   故 `search_*` 工具直呼 `OntologyQueryService`（带 `q` 关键词 + `limit`）并 compact 化结果；
   `get_*` 语义与 `call_tool` 对齐（`published_only` 校验）。上下文由分页工具按需拉取，**结构性根治 413**。

2. **`run_sql` 的数据源解析策略。** `DataSource` schema 层无数据域外键，故：唯一可用源直接用；
   多个可用源取最近更新的；**无可用源则优雅降级**为"仅建议 SQL、未实际执行"。dev 环境零数据源时
   Agent 仍能检索 + 产出建议 SQL，不阻塞体验。

3. **`suggested_sql` 双通道收割。** 优先取自 `run_sql` 入参；模型若把 SQL 写进正文围栏块却没调
   `run_sql`，用 `_extract_sql_from_text` 兜底抽取，避免前端（丢弃 markdown 内 SQL 围栏）丢 SQL。

**运行时修正**：ChatBI 已切到 `AsyncOpenAI` + `llm_timeout_seconds`（默认 300s），
弃用 `make_http_client()` 的 httpx 5s 默认超时（"LLM 调用失败"的另一潜因）。旧的单发
`_llm_answer(response_format=json_object)` 及知识包 `_build_knowledge_text/_scope_knowledge`
已随 P1 移除（工具编排取代）。

**验证**：`_dispatch_run_sql` 三态（执行/只读拒绝/无源降级）单测通过；ERP 全域多步工具编排跑通
（`used_mock=False`，19 次工具调用 ≤6 轮，真实引用）；临时 sqlite 源下 `data_result` 端到端收割
（3 行）；`ChatBiAnswer` schema 校验含 `steps`/`data_result`；后端 **517 测试全过**；前端 `tsc` 干净。
