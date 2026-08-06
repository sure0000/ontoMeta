# Data Agent V4 改造方案：从「成熟推理层」到「专用 data-agent harness」

> 状态：**S0–S3 全部交付**（O6 trace + O1 compaction + O2 大结果离场 + O3 渐进披露 + O4 子 agent 框架化 + O5 循环模块化）。本方案收尾。
> 前序：[DATA_AGENT_REDESIGN.md](./DATA_AGENT_REDESIGN.md)（V1：单发问答 → 多步工具编排）、
> [DATA_AGENT_V2_PLAN.md](./DATA_AGENT_V2_PLAN.md)（P0–P4：语义层从「否决者」变「生成器」）、
> [DATA_AGENT_V3_SKILLS_PLAN.md](./DATA_AGENT_V3_SKILLS_PLAN.md)（skill + 渲染块，S0–S3 已交付）、
> [FORMAL_VALIDATION_PLAN.md](./FORMAL_VALIDATION_PLAN.md)（宁可拒答不可错答）
> 主战场：后端 `backend/app/services/chat_bi.py`（5120 行单文件）+ `chat_bi_skills.py` + `retrieval_agent.py` + `agent_telemetry.py`

---

## 0. 一句话结论

V1–V3 已把**推理层与展示层**做成熟：多步工具编排、12 工具、域语义卡、接地/自证、检索子 agent、skill + 块渲染。
新的瓶颈**不在推理层，而在 harness 骨架**——对标 pi（极小内核 + 渐进披露技能 + 结构化 compaction + 子 agent 隔离 + JSONL 会话可回放）与业界 data-agent 专用 harness，当前有 5 个结构性差距：

1. **单文件 5120 行扁平 loop**：工具分派/收割/校验/自愈全内联，难测难扩。
2. **无上下文 compaction**：`history[-6:]` 硬截断（`chat_bi.py:4339`），多步探索丢上文。
3. **大结果塞满上下文**：SQL 结果表/profile 内联再按字符截断（`_TOOL_RESULT_MAX_CHARS=8000`，`chat_bi.py:80`），data-agent 特有的大结构化结果污染后续每一轮。
4. **只有一个子 agent 被隔离**：仅 `retrieval_agent.locate_entities`，取数/血缘的重活仍在主上下文试错。
5. **遥测进程内、重启清零**：`agent_telemetry.py` 明写「刻意不落库」，无法做生产 eval / 回放 / `skill_misroute_rate`。

> **改造总纲**：不碰守卫（只读 SQL + RBAC `agent_run_sql_min_role` + 治理闸门），不改 `ask_stream` 对外 done 契约，只重构 **harness 骨架**。每一步先建「量」再改「形」，用 golden 20 例逐字节比对护栏。

---

## 1. 现状盘点（代码锚点）

| 维度 | 现状 | 锚点 |
| --- | --- | --- |
| 主循环 | 单文件 5120 行，`ask_stream` 巨函数内联全流程 | `chat_bi.py:1258`（入口）、`:4392`（工具 loop） |
| 上下文—历史 | `history[-6:]` 硬截断，超窗直接丢旧轮 | `chat_bi.py:4339` |
| 上下文—工具结果 | 单结果按字符截断内联，`_TOOL_RESULT_MAX_CHARS=8000` | `chat_bi.py:80`、`tool_result_compaction.py:31` |
| 技能 | overlay + 解锁工具，`select_skill` opt-in；12 工具 schema + 卡 + 记忆 + 阶梯全程常驻 | `chat_bi_skills.py`、`chat_bi.py:4337` |
| 子 agent | 仅检索隔离，回报紧凑结论 | `retrieval_agent.py` |
| 接地账本 | FactLedger 登记工具真实返回的事实，防幻觉误拒答 | `agent_grounding.py:75` |
| 遥测 | 进程内 Counter，重启清零；golden 用 LLM stub | `agent_telemetry.py`、`api/chat_bi.py:413` |
| 写侧 | pipeline 状态机完备，agent 经 `propose_*` 出提案 | `agent_pipeline.py` |

**当前基线**（V2 §8.12，golden 20 例）：`avg_llm_calls=2.6`、`avg_steps=1.45`、`refusal_rate≈0.2`。

---

## 2. 对标：pi 范式 → data-agent 差距

| pi 范式 | pi 文档 | 当前 data agent | 差距 |
| --- | --- | --- | --- |
| 结构化 compaction（goal/constraints/progress/decisions + 阈值触发 + 保留近轮 + 文件轨迹累积） | `docs/compaction.md` | `history[-6:]` 硬截断 | **O1** |
| 渐进披露技能（描述常驻、正文/工具 on-demand 加载） | `docs/skills.md` | 全量工具 schema + 卡 + 记忆常驻 | **O3** |
| 子 agent 隔离（重活关进隔离上下文，只回结论） | Task 模式 | 仅检索 | **O4** |
| JSONL 会话（可回放、可分支、可 eval） | `docs/session-format.md` | 进程内遥测 | **O6** |
| 极小内核 + 扩展注册 | `docs/extensions.md` | 5120 行单文件 | **O5** |
| （data-agent 专属）大结构化结果离场存储 | — | 内联 + 字符截断 | **O2** |

---

## 3. 优化项与前后对比

| # | 优化项 | 前 | 后 | 预期提升 |
| --- | --- | --- | --- | --- |
| **O1** | 结构化 compaction | `history[-6:]` 硬截断 | 超阈值摘要旧轮 + 保留近轮 + 关键 SQL/口径入摘要 | 长会话每问 prefill token −30~50%；多轮探索连续性↑ |
| **O2** | 大结果离场存储 ✅ | 大结果内联 + 字符截断 | store 存全量，上下文留 schema+样例+句柄（read_result 分页） | query 技能上下文字符 −40~60%；不再截断丢列 |
| **O3** | 渐进披露技能 ✅ | 全量 schema 常驻 | 基础工具集瘦身；技能/工具命中才加载（read_result 取到数才解锁） | 首轮 prefill 工具↓；misroute 可测 |
| **O4** | 子 agent 框架化 ✅ | 仅检索隔离 | 通用 sub-agent 骨架 + 取数探路（scout）子 agent | `isolated_chars` 收益扩到取数探路 |
| **O5** | 循环模块化 ✅ | 5309 行单文件 | 工具 schema/常量/工具集构建拆到 `chat_bi_tool_schemas.py`（re-export 保契约） | chat_bi.py 5309→4291 行；工具改动面↓ |
| **O6** | trace 落地 + 生产 eval | 进程内遥测清零 | 持久 trace，可回放/算 misroute/回归 | 改造可量化；golden 从 stub 扩到真实回放 |

---

## 4. 实施阶段与进度

> 图例：⬜ 未开始 · 🟡 进行中 · ✅ 已交付

| 阶段 | 内容 | 风险 | 状态 |
| --- | --- | --- | --- |
| **S0** | O6 trace 落地 + O1 compaction（先建「量」，compaction 收益立等可取） | 低 | ✅ |
| **S1** | O2 大结果离场（data-agent 最大痛点，收益最直观） | 中 | ✅ |
| **S2** | O3 渐进披露 + O4 子 agent 框架化 | 中 | ✅ |
| **S3** | O5 模块化重构（纯结构、零行为变化，golden 逐字节护栏） | 高 | ✅ |

### S0 详细任务

- [x] O6.1 运行轨迹落地：**pi JSONL session 风格**（`agent_trace.py`，非 DB 表、零 schema 债，默认关闭，开关开写一行/问到 `.logs/agent_traces/`）
- [x] O6.2 `skill_misroute_rate` 度量：路由技能 vs 实际命中工具族的一致性（`agent_telemetry.route`/`route_outcome`）
- [x] O6.3 `GET /chat-bi/telemetry` 扩 `context_chars_per_call` 快照（+ `compaction_runs`、`skill_routed`）
- [x] O1.1 新增 `agent_compaction.py`：**抽取式结构化摘要**（不额外调 LLM，护住 avg_llm_calls=2.6）+ 字符预算触发
- [x] O1.2 `ask_stream` 用 compaction 替换 `history[-6:]` 硬截断，摘要内实体名同步入 FactLedger（防误拒答）
- [x] O1.3 golden 断言：短会话逐字节不变（golden 84 例全绿）；`test_agent_harness_v4.py` 验证长会话摘要触发且不丢关键实体

> **S0 实测回归**：`test_agent_harness_v4.py` 7 例全绿；golden 20 例 + agent 套件（pipeline/subagent/implementations）共 84 例全过；chat_bi 相关 175 例全过。**行为零变化**。
> **新增设置**（`config.py`）：`agent_compaction=on`、`agent_history_char_budget=6000`、`agent_trace_enabled=False`、`agent_trace_dir=.logs/agent_traces`。

### S1 详细任务

- [x] O2.1 新增 `agent_result_store.py`：`RunResultStore`（per-run、随问答生灭、不跨请求缓存）+ `project_run_sql_for_model`（列名+样例 N 行+句柄）
- [x] O2.2 `run_sql` 大结果回灌模型前**离场**：全量行寄存 store，上下文只留样例 + `result_handle`（接在通用回灌点，不影响收割/入账拿全量）
- [x] O2.3 新增基础工具 `read_result(handle, offset, limit)`：模型按需分页取行，只把那几行调进上下文（对齐 pi 句柄引用）
- [x] O2.4 遥测新增 `offloaded_chars`/`offload_count`；前端 `data_result` 仍拿全量，渲染/`analyze_result`/`render_chart` 保真不变
- [x] O2.5 集成测：驱动 agent 循环验证回模型消息含句柄+样例而非全量行、`read_result` 分页取回尾部行、前端仍拿 100 行

> **S1 实测回归**：`test_agent_result_offload.py` 6 例 + `test_agent_offload_e2e.py` 1 例全绿；全库 1124 passed。**行为零变化**（golden `expect_sql_executed=False` 不受影响；真执行路径由新集成测覆盖）。
> **新增设置**（`config.py`）：`agent_result_offload=on`、`agent_result_sample_rows=5`。

### S2 详细任务

- [x] O4.1 新增 `agent_subagent.py`：通用 `SubAgentSpec` + `run_subagent`（隔离上下文循环骨架：工具子集/越权回绝/步数预算/字符预算/度量）
- [x] O4.2 重构 `retrieval_agent.locate_entities` 复用通用骨架（行为保持，9 例回归全绿），不再维两套循环
- [x] O4.3 新增第二个隔离子 agent `query_scout_agent.py`（取数探路：定位/profile/find_join/编口径 → 只回候选 SQL），**不含 run_sql**（不执行，守卫不变）
- [x] O4.4 `scout_query` 工具由 query 技能解锁 + inline dispatch + `tel.subagent` 单独计成本
- [x] O3.1 渐进披露：`read_result` 移出基础工具集，**首次 run_sql 取到结果后才动态解锁**（首轮不暴露、省 prefill；语义也更干净）
- [x] O3.2 记录：基础工具集的渐进披露在 V3 已大部分实现（写侧/图表/血缘工具均由技能 `extra_tool_names` 延迟解锁）；S2 补齐 read_result 这一漏网

> **S2 实测回归**：`test_query_scout_subagent.py`（通用骨架 + scout）+ `test_retrieval_subagent.py`（重构后 9 例）+ `test_agent_offload_e2e.py`（新增 O3 解锁断言）全绿；全库 **1131 passed**。**行为零变化**（只解锁不收窄；scout 不执行 SQL）。
> **新增工具**：`scout_query`（query 技能解锁）；`read_result` 改为 run_sql 后动态解锁。

### S3 详细任务

- [x] O5.1 把工具 schema + 编排常量 + 系统提示 + 工具集构建/检索小工具（~1080 行，纯声明、无运行态）拆到新模块 `chat_bi_tool_schemas.py`
- [x] O5.2 `chat_bi.py` 全量 re-export（显式 `__all__` 54 个符号），保持 `chat_bi._AGENT_TOOL_SCHEMAS`/`_TOOL_BY_NAME`/`_tools_for_skill`/… **对外符号与对象 identity 不变**（测试/其它模块 import 契约不动）
- [x] O5.3 清理 chat_bi.py 因拆分而闲置的 import（`sqlparse`、`skill_choices_text`）
- [x] O5.4 拆分护栏：全库 **1131 passed** 逐项不变（golden 20 + 全套件），**行为零变化**

> **S3 取舍**：`_ReferenceResolver` + `_loads_payload`（尾块 ~130 行）与 `_ObjectSnapshot` 强耦合、且被 `ChatBiService`
> 内部前引用，拆出会连带 `_ObjectSnapshot` 一同搬、风险大收益小，**本期不拆**。已把 ~1080 行纯声明的大头拆完（主目标）。

### 验收护栏（每阶段必过）

1. golden 20 例断言行为不变（工具序列、拒答、run_sql 三态、拒绝码）。
2. `avg_llm_calls` 不回涨（护住 2.6）。
3. 新增指标看收益：`context_tokens_per_call`、`skill_misroute_rate`、`isolated_chars`。
4. 不碰守卫：只读 SQL + RBAC + 治理闸门不动；`ask_stream` done 契约不变。

---

## 5. 风险与对策

| 风险 | 对策 |
| --- | --- |
| O1/O2 改上下文构造 → FactLedger 漏登记摘要/句柄里的实体 → 误拒答 | 摘要/句柄产出时同步 `ledger.add_context_name`；golden 加「摘要后引用旧实体不误拒」用例 |
| O5 重构面最大 | 放最后；保持 `ask_stream` 对外契约；golden 逐字节比对 |
| compaction 摘要漏关键 SQL/口径 | 摘要格式显式保留 `key-SQL` / `compiled-metric` 段（对齐 pi 的 Critical Context） |
| trace 落地带来 schema 债 | 已采用 **JSONL 文件**（pi session 风格）而非 DB 表：默认关、可开关、改造收尾删目录即可，零 schema 债、零迁移 |

---

## 6. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| （本次） | S0 | 交付 O6（trace JSONL + 遥测扩）+ O1（跨轮 compaction）；新增 3 模块 + 7 单测；golden/agent 套件全绿，行为零变化 |
| （本次） | S1 | 交付 O2（大结果离场 store + `read_result` 分页工具 + 离场遥测）；新增 `agent_result_store.py` + 7 测（含 1 集成测）；全库 1124 passed |
| （本次） | S2 | 交付 O4（`agent_subagent.py` 通用骨架 + retrieval 重构 + `query_scout_agent.py` 取数探路）+ O3（read_result run_sql 后动态解锁）；全库 1131 passed |
| （本次） | S3 | 交付 O5（工具 schema/常量/工具集构建拆到 `chat_bi_tool_schemas.py` + re-export）；chat_bi.py 5309→4291 行；全库 1131 passed，行为零变化 |

---

## 7. 收尾：S0–S3 全部交付

V4 把 Data Agent 从「成熟推理层」升级为「专用 data-agent harness」，六项优化全部落地：

| 项 | 交付物 | 核心收益 |
| --- | --- | --- |
| O1 | `agent_compaction.py` | 跨轮结构化摘要取代 `history[-6:]` 硬截断（不额外调 LLM） |
| O2 | `agent_result_store.py` + `read_result` | 大结果离场，上下文只留样例 + 句柄 |
| O3 | 基础工具集瘦身 | `read_result` run_sql 后才动态解锁，首轮省 prefill |
| O4 | `agent_subagent.py` + `query_scout_agent.py` | 子 agent 框架化；取数探路入隔离上下文 |
| O5 | `chat_bi_tool_schemas.py` | 工具声明拆出，chat_bi.py 5309→4291 行 |
| O6 | `agent_trace.py` + 遥测扩 | JSONL 轨迹可回放 + `skill_misroute_rate`/`context_chars_per_call`/`offloaded_chars` |

**全程守住**：不碰守卫（只读 SQL + RBAC + 治理闸门）；`ask_stream` done 契约不变；
`avg_llm_calls` 不回涨；每阶段 golden 20 例 + 全套件 1131 例全绿，**行为零变化**。

**新增模块**（5）：`agent_compaction.py`、`agent_trace.py`、`agent_result_store.py`、
`agent_subagent.py`、`query_scout_agent.py` + 拆分出的 `chat_bi_tool_schemas.py`。

**后续可选（未纳入 V4）**：`_ReferenceResolver`/`_ObjectSnapshot` 进一步拆模块；compaction
接真实长会话实测调参 `agent_history_char_budget`；scout 扩到多步取数链；trace 接回放型 eval 驱动 golden 扩充。
> → 已落成独立计划：[DATA_AGENT_V5_PLAN.md](./DATA_AGENT_V5_PLAN.md)（P0 先行：trace 实测）。
