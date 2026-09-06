# 删除 Data Agent 的就绪度清单

目标形态：**通用 agent（dsh / Claude Desktop / …）+ ontoMeta MCP + skill**，
产品内不再自带对话页。本文只回答两件事：**现在还差什么**，以及**真要删时按什么顺序删**。

状态：2026-09-06。解耦与能力补齐已完成（见 `backend/app/mcp/STATUS.md` 顶部一节），
删不删是产品决定，不是技术阻塞。

---

## 一、已经不再是阻塞的（做完了）

### 1. MCP 不再依赖对话模块

此前 MCP 有五处 `from app.services.chat_bi...`，删对话模块会让建数流程静默哑掉。
共用的东西已搬到中性位置：

| 东西 | 新家 |
|---|---|
| 建数表单骨架 / 候选目录 / context 校验 | `backend/app/services/task_form.py` |
| 代跑 SQL 的整条闸门链 | `backend/app/services/agent_sql.py` |
| 表单传输结构 | `backend/app/schemas/task_form.py` |
| 草稿生成派发 | `backend/app/services/draft_launch.py` |
| `agent_pipeline` 单例 | `backend/app/services/agent_pipeline.py` |

**由测试把关**：`backend/tests/test_mcp_independence.py` 静态扫 `app/mcp/**` 的每一条
import，再在新解释器里把 `app.services.chat_bi` 从 meta_path 挡掉、断言 51 个工具照样
装配得出来——那等于一次删除演练，每次跑测试都在演练一遍。

### 2. 取数的闸门两侧合一

`execute_sql` 之前只做只读校验就直连 Doris：没有语义证明、没有就绪闸，也没有落点映射
（调用方只能自己拼 `ods_xxx`）。现在与对话侧共用 `agent_sql.run_agent_sql`。

### 3. 只在 Data Agent 里有过入口的能力，已经在 MCP 上有了

| 能力 | 原来 | 现在 |
|---|---|---|
| 落点目录 | `list_datasets` | `list_datasets`（MCP） |
| 口径创作（自然语言 → 表达式 → 编译自证 → 落草稿） | `propose_draft` / `propose_expression` | `compile_logic_expression` + `create_logic` + `update_logic_expression` |
| 规约自检 | `lint_against_standard` | `lint_spec` |
| 接数据 | `list_onboarding_targets` / `propose_datasource` / `propose_ontology_draft` | `list_onboarding_targets` / `create_datasource` / `start_ontology_draft` |
| 建模工单 + 维度模型 | 五个 `*_modeling_case` / `propose_dimensional_model` | `create_modeling_case` / `save_modeling_spec` / `confirm_modeling_spec` / `get_modeling_case` / `create_dimensional_model` |

skill 同步补了三份（`ontometa-onboarding` / `ontometa-authoring` / `ontometa-modeling`），
「每个注册工具都要有 skill 指引」那条测试把关。

### 4. 本次替代增强

- `analyze_query` 已加入 MCP：复用 `execute_sql` 的 SQL 闸门，在服务端计算分布、IQR 离群、趋势和突变，避免通用 Agent 把查询结果重新塞回上下文自行口算。
- 结果分析算法已移到 `services/result_analysis.py`，Data Agent 与 MCP 共用同一实现。
- `ChatBiService` 不再由共享 `api.deps` 在导入期实例化；旧对话路由通过懒加载兼容，未来移除路由不会拖垮其它 API。
- `MarkdownLite` 已移到 `frontend/src/components/MarkdownLite.tsx`，技能管理和 MCP 面板不再依赖 `pages/chat-bi/`。

---

## 二、删了就没有的（要先接受，或另作安排）

这几条**不是技术债，是删除的代价**。列在这里是为了让决定是明知的。

### 1. F4 断言级核验：原理上搬不过去

`agent_grounding.FactLedger` + `answer_verifier` 核的是**最终答复文本**里的具名实体和数字：
模型写了本体里没有的对象名、或一个没有工具证据支撑的数，当场拒答。

MCP 看不见通用 agent 的输出文本，这条**做不了**。替代只有 skill 里的出口契约——从
「服务端能拒答」退化为「提示词里请求它别编」。

**缓解**：工具侧的硬约束还在，而且比以前多（F3 语义证明、编译器、就绪闸、落点目录）。
真机上 dsh 遇到未发布对象时如实报「受阻」而不是编一个数——那是工具拒绝造成的，不是提示词。
但**散文式的推理编造**（不带具名实体、不带数字）仍然只有 skill 约束。

### 2. 产品内没有对话入口

前端 5,090 行 chat-bi 页（会话/分类/引用卡/提案卡/思考流/图表）一起没；结果分析能力已由 MCP `analyze_query` 覆盖，但富交互渲染仍不在 MCP。
用户要问数就必须装一个 MCP 客户端。这是产品定位决定。

### 3. 提案卡这套人机闸

「agent 只提案、人点卡片才写」长在前端卡片上。MCP 侧换成**角色门控**（见 STATUS 的 DR-2）：
发什么角色的令牌 = 允不允许这个 agent 自动写。对四类任务和口径创作都成立，但形态不同——
不再有「看着卡片点确认」那一下。

### 4. 数据应用面板/看板

`propose_panel` / `propose_dashboard` 没有 MCP 对应物。删掉后，"把这一轮口径做成一个面板"
只能在 Web 的数据应用编辑器里手工做。

### 5. 跨会话口径记忆

`propose_preference` + 域记忆卡（`chat_bi_domain_memory` 表）随对话模块一起没。
通用 agent 宿主各有自己的记忆机制，但那是宿主的，不是 ontoMeta 的。

---

## 三、真要删时的顺序

分三批，每批之间可以停下来跑一次全量测试与真机 dsh。

### 批次 1：前端 + REST 对话面（可逆，先做）

```
frontend/src/pages/chat-bi/          （5,090 行）
frontend/src/api.ts                  里的 chat-bi 段
frontend/src/App.tsx / Layout.tsx    里的路由与菜单项
backend/app/api/chat_bi.py           （566 行）
backend/app/api/router.py            去掉 chat_bi 的 include_router
backend/app/api/agents.py            删 /agents/draft-confirmed（只被对话页调用）
```

删除前先确认 `frontend/src/components/MarkdownLite.tsx` 已保留；它已被技能管理页和 MCP 面板共用。

`/agents/task-form` **留着**：它已经指向 `services/task_form`，与对话无关。

### 批次 2：服务层

```
backend/app/services/chat_bi.py                （6,354 行）
backend/app/services/chat_bi_tool_schemas.py   （1,937 行）
backend/app/services/chat_bi_skills.py         （301 行）
backend/app/services/chat_bi_blocks.py         （183 行）
backend/app/services/chat_bi_references.py     （168 行）
backend/app/services/agent_grounding.py        （232 行）
backend/app/services/answer_verifier.py        （294 行）
backend/app/services/agent_result_store.py     （93 行）
backend/app/services/agent_run_store.py        （347 行）
backend/app/services/agent_compaction.py       （180 行）
backend/app/services/ops_live_eval.py          （151 行，已无任何引用）
backend/app/api/deps.py                        去掉 chat_bi_service 单例
```

注意几处**不是**只服务对话的：

- `agent_telemetry` / `agent_trace` 被 `app/observability.py` 与 `app/config.py` 引用，
  删之前先确认可观测面是不是也要一起收。
- `app/ontology_types.py` 的注释提到 `answer_verifier`，只是文档引用。
- `services/warehouse_migration.py` 的 `runtime_compatibility_inventory` 用
  `path.exists()` 探 `chat_bi_tool_schemas.py`，文件没了会正确地返回「不再阻塞」，不必改。

### 批次 3：数据与测试

```
backend/app/models/chat_bi.py     （163 行）+ app/models/__init__.py 的再导出
backend/app/schemas/chat_bi.py    （283 行）+ app/schemas/__init__.py 的再导出
backend/tests/test_chat_bi*.py    （14 个文件、6,112 行）
迁移：chat_bi_conversations / chat_bi_messages / chat_bi_conversation_tasks /
      chat_bi_domain_memory 四张表 —— 建议先留表不删，观察一个发布周期
```

`app/schemas/chat_bi.py` 里的表单结构（`ChatBiFormField` / `ChatBiFormRequest` / `ChatBiFormOption`）
已经搬到 `app/schemas/task_form.py`，那边保留的只是别名再导出，删掉别名即可。

**删完的验收**：`pytest` 全绿 + `tests/test_mcp_independence.py` 的删除演练本来就在跑，
再用 dsh 真机跑一遍四条主路径（结构探索 / 取数 / 建口径 / 建任务）。

---

## 四、这次没做但值得单列的发现

1. **建模工单在对话侧是直接写表的平行实现**，写进去的需求规格字段名（`analysis_scope` /
   `primary_subject` / `grain` / `time_range` / `deliverables`）与 `RequirementSpec`
   （`extra: forbid`）对不上——从对话建的规格读得出来、按 schema 校验却过不去。
   MCP 版本接的是 `ModelingCaseService`（REST 走的同一条路），不带这个毛病。
   若暂时不删对话模块，这仍是一个真实缺陷。

2. **`/agents/task-form` 与 `taskConfirmationForm` 前端零调用**：REST 端点在、api.ts 里
   有封装、没有任何页面用它。不影响删除，但属于死代码。

3. **`services/data_app.py` 的两处「缺载荷就重新问一次 LLM」回退已删**。那条回退会生成一份
   与用户看过的口径未必相同的应用；现在缺载荷直接报错。若有直接调 REST 的外部调用方，
   它们需要改为带载荷调用。
