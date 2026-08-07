# Data Agent V3 改造方案：从「一套模板答所有」到「skill + 渲染块」

> 状态：**S0–S3 全部交付**（§11–§14）。§10 四项决策全部拍板。V3 五 skill、块协议、写侧桥接闭环完成。
> 前序：DATA_AGENT_REDESIGN（V1 评审草案，已并入 V2）、
> [DATA_AGENT_V2_PLAN.md](./DATA_AGENT_V2_PLAN.md)（P0–P4：语义层从「否决者」变「生成器」，五期已交付）、
> [FORMAL_VALIDATION_PLAN.md](./FORMAL_VALIDATION_PLAN.md)（宁可拒答不可错答）
> 主战场：后端 `backend/app/services/chat_bi.py` + `backend/app/schemas/chat_bi.py`；
> 前端 `frontend/src/pages/chat-bi/ChatBiReferences.tsx`

---

## 0. 一句话结论

V1/V2 已把**推理层**做成成熟的多步工具编排 agent（12 工具 + 域语义卡 + 接地/自证 + 遥测/golden，见 V2 §8.1）。
用户新反馈的「本体映射展示过于死板」——**问题已不在推理层，而在两个结构性位置**：

1. **前端是一条写死的 JSX 阶梯**（`ChatBiReferences.tsx:223-336`）。无论问什么，渲染顺序恒为
   `steps → 拒答/澄清 → 答案 → 口径卡 → SQL → 结果表 → 标签 → mock → 建应用`，
   每段只是对 `payload` 某字段的 `&&` 开关。口径卡（`CaliberDecomposition`，`ChatBiReferences.tsx:488-519`）
   永远是同一个标题「口径拆解·本体映射」+ 同一个编号列表 + 4 种颜色的 antd `Tag`。
   **任何问题都套同一张报表模板**——这是「死板」的主因。
2. **后端只有一套 system prompt + 一个扁平循环服务所有任务类型**。取数、分析、建数、看血缘
   走同一条 prompt、同一组工具、同一个扁平 DTO（`ChatBiAnswer`）。**没有「任务类型」这个维度，
   自然没有任务专属的行为与展示。**

> **改造总纲**：给 Data Agent 引入**任务类型**这个一等维度。
> 一个 skill = 面向某类数据工作的能力包（触发 + prompt 片段 + 工具子集 + **渲染契约** + 可选子 agent）。
> 展示从「前端写死」变成「skill 声明、块渲染」。
>
> **进度指标**：前端渲染主干的「写死分支数」。启动时 9 段硬编码 `&&` 阶梯 → 目标 0（全部走块注册表）。
> 凡是能由块协议承载的，不写进 JSX 阶梯。

---

## 1. 现状盘点（代码锚点）

| 维度 | 现状 | 锚点 |
| --- | --- | --- |
| 后端循环 | 单套 `_AGENT_SYSTEM_PROMPT`（741 字符）+ 12 工具扁平循环，服务所有任务 | `chat_bi.py:88-110`、`_AGENT_TOOL_SCHEMAS:114-375` |
| 输出契约 | 扁平 DTO `ChatBiAnswer`：`answer` + 若干可选字段，**无 chart 字段** | `schemas/chat_bi.py:62-80` |
| 口径映射 | `ChatBiCaliberItem = {label, description, references[]}`，4 种 kind 冻结映射 | `schemas/chat_bi.py:12-26`、`ChatBiReferences.tsx:166-178` |
| 前端渲染 | `ChatBubble` 写死 9 段 `&&` 阶梯；`CaliberDecomposition` 固定编号列表 | `ChatBiReferences.tsx:223-336`、`488-519` |
| 图表 | **零**。结果只有纯 `<table>`（`ResultTable`，50 行封顶） | `ChatBiReferences.tsx:447-486` |
| 流式 | SSE 已就绪：`meta/step_start/step_done/thought/repair/token/done/error` | `api/chat_bi.py:208-287` |
| 写侧骨架 | `agent_pipeline`（draft→validate→confirm→execute）完备，**但 Data Agent 未接入** | V2 §1.2 关键观察 |

### 1.1 三个具体病灶

**A. 口径卡对每个答案一视同仁。** `CaliberDecomposition` 永远同一张脸：同标题、同编号列表、同 chip 样式。
平凡的「客户表有多少行」和复杂的「GMV 按区域月度拆解」得到**完全一样的展示 chrome**。
死板是结构性的——扁平 DTO 投影到写死阶梯，调 CSS 治不了。

**B. 只有 4 种 reference kind，且冻结映射到 label+color。**
`object_type/property/relation_type/business_logic` 之外的东西（血缘、join 路径、图表、公式、条件）
**没有任何展示出口**，只能塞进 markdown 正文或被丢弃。

**C. 后端无任务类型概念。** 「建个客户分层表」和「查客户数」进同一条 prompt、同一组工具、
产同一个 DTO。建数类请求要么被引导去手动走别的页面，要么模型硬答——
**全项目唯一没接进写侧治理骨架的模块**（V2 §1.2）。

---

## 2. 核心思想：把 skill 映射到本项目

Claude Code 的 skill = **按需加载的、面向某类任务的能力包**。搬到 Data Agent：

```
Skill = {
  name,                    # 概览 / 查询 / 分析 / 建数 / 血缘
  when_to_use,             # 触发描述(给模型选,像 CC 的 skill 描述)
  prompt_overlay,          # 该任务类型专属的工具选择策略(叠在瘦身基座上)
  tool_allowlist,          # 该 skill 解锁/收窄的工具子集
  render_contract,         # ★该 skill 产出哪些「渲染块」——治死板的关键
  subagent?                # 可选:重活外包(如血缘子图定位)
}
```

**关键区别于「再加几个工具」**：skill 不只换 prompt，它还**声明输出长什么样**（`render_contract`）。
这把「展示」从前端写死变成 skill 驱动——这是本方案与 V2「加语义工具」的本质不同。

> **形态判断（承接 V2 §7.1）**：不照抄 CC 的 skill 全量机制。CC 的 skill 面向长程、有副作用的
> 工程任务；Data Agent 的 skill 面向**一次问答内的任务类型分派 + 展示契约**。
> 要的是「这类问题该用哪组能力、答案该长什么样」，不是「加载一份多步操作手册」。

---

## 3. 治本第一刀：渲染块协议（blocks[]）

**这一步单独就解决死板，且不碰推理层、不碰守卫。**

把 `ChatBiAnswer` 从「扁平字段 + 前端写死顺序」改成 **`blocks: RenderBlock[]`**——
一个有类型的块序列，skill 决定发哪些块、什么顺序；前端变成**块渲染器 + 组件注册表**。

### 3.1 块协议

```jsonc
"blocks": [
  { "id": "b0", "type": "markdown", "content": "…" },
  { "id": "b1", "type": "sql",      "sql": "…", "compiled_from": "metric:GMV" },
  { "id": "b2", "type": "table",    "columns": [...], "rows": [...], "truncated": false },
  { "id": "b3", "type": "chart",    "spec": { "kind": "line", "x": "月", "y": "GMV" } },
  { "id": "b4", "type": "mapping",  "variant": "inline|caliber", "items": [...] },
  { "id": "b5", "type": "lineage",  "graph": { "nodes": [...], "edges": [...] } },
  { "id": "b6", "type": "draft_proposal", "pipeline_id": "…", "diff": {...} },
  { "id": "b7", "type": "steps",    "steps": [...] }   // 工具轨迹也是一种块
]
```

**块类型起步集**：`markdown / sql / table / chart / mapping / lineage / draft_proposal / steps / clarify / notice`。

### 3.2 前端：块渲染器 + 注册表

`ChatBubble` 的 9 段 `&&` 阶梯（`ChatBiReferences.tsx:223-336`）替换为：

```tsx
{message.payload?.blocks?.map(b => {
  const Comp = BLOCK_REGISTRY[b.type] ?? UnknownBlock;   // 未知类型优雅跳过
  return <Comp key={b.id} block={b} />;
})}
```

- 现有 `CaliberDecomposition / SqlBlock / ResultTable / StepTrace` **原样复用**，只是被登记进 `BLOCK_REGISTRY`
  而非写死调用。**加新块类型不用改渲染主干。**
- **口径「本体映射」从常驻报表段降级为一个可选块**：
  - 平凡单表查询 → `mapping{variant:"inline"}`：一行 chip，不占整块。
  - 编译指标 → `mapping{variant:"caliber"}`：完整口径卡，数据来自 P3 已有的 `caliber_trace`。
  - 不相关 → **不发这个块**。同一张口径卡面板不再套在每个答案上。

### 3.3 流式增量

SSE 已有 `token/step_*/done`（`api/chat_bi.py:208-287`）。加一个 `block` 事件增量推块；
前端 `ChatBiPage.tsx:867-969` 的 `done` **整体覆盖**逻辑（956-959）改为**按 `block.id` patch/append**。
`markdown` 块的正文仍走 `token` 事件流式追加（挂到对应块），其余块在完成时以 `block` 事件落定。

### 3.4 向后兼容

`ChatBiAnswer` 保留 `answer/suggested_sql/caliber_decomposition/data_result` 旧字段（不删），
后端**同时**产出 `blocks[]`。前端切到块渲染后，旧字段仅供历史消息/降级；
一个转换器 `answer_to_blocks(payload)` 把老 payload 投影成块，保证历史会话不空白。

**S0 验收**：`ChatBubble` 的写死 `&&` 分支数 9 → 0；口径卡在「单表查询/编译指标/无关」三种场景
渲染出三种形态（inline / caliber / 不出现）；历史消息经 `answer_to_blocks` 正常回放。

---

## 4. Skill 目录（建议 5 个）

| Skill | 触发（when_to_use） | 解锁/收窄工具 | 产出块（render_contract） | 复用锚点 |
| --- | --- | --- | --- | --- |
| **概览** overview | 「有哪些 / 介绍下这个域 / 都能查什么」 | search_* / get_domain_overview | markdown + mapping(inline) | 域语义卡（P2.1） |
| **查询/取数** query | 「查 / 取 / 多少 / 列出 / 明细」 | compile_metric + run_sql + profile_values + find_join_path | sql + table + **chart** + mapping(caliber 仅编译指标时) | 全部现成，只新增 chart |
| **分析** analysis | 「趋势 / 对比 / 为什么 / 拆解 / 环比同比」 | 同 query，多轮 run_sql | markdown(叙述) + 多 chart + table | run_sql 多次；`dataviz` skill |
| **建数** create-data | 「建个表 / 生成指标 / 加字段 / 派生一列」 | ★新 `propose_draft`（接写侧，只产草稿不写库） | draft_proposal（带 confirm 按钮） | **`agent_pipeline` draft→confirm→execute**（V2 §1.2 唯一缺口） |
| **血缘** lineage | 「血缘 / 上下游 / 影响面 / 这张表从哪来」 | ★新 `get_lineage(center,depth)` + locate_entities 子 agent | lineage 图 + markdown | 已有血缘图数据 + P4.2 子 agent 隔离范式 |

> **不选 skill = 现有通用行为**。skill 是**特化**，不是**必经**——保证零回归（见 §5）。

### 4.1 chart 块与 dataviz

`chart` 块的 `spec` 采用与图表库无关的中间描述（kind/x/y/series/agg），前端用 `dataviz` skill 的
配色/构图规范渲染。**图表选择由 query/analysis skill 的 prompt_overlay 建议、由数据形状决定**
（时间序列→line，类别对比→bar，占比→有意义时才 pie），不硬塞。

### 4.2 建数 skill 的边界（最需要守住的）

- Data Agent 侧只产 **draft_proposal 块**——一个指向 `agent_pipeline` 草稿的提案 + diff 预览 + 「去确认」按钮。
- **真正写库仍由用户在既有 draft→confirm→execute 流程里确认后执行**。Data Agent **不新增任何写侧出口**。
- 延续 V2 「只读问答 + 写侧走治理制品」的边界（V2 §10）：建数 skill 是**桥**，不是**后门**。

### 4.3 血缘 skill 的复用

ERP 血缘是含大环的非 DAG（见记忆 `erp-lineage-is-cyclic-tangle`）。lineage 块**默认按中心节点邻域下钻
（center + depth）**，不试图全图分层；子图定位这类高 token 活外包给 P4.2 的 `locate_entities` 子 agent，
主上下文不被污染（隔离范式见 V2 §8.11）。

---

## 5. 路由：用工具选 skill，不加强制分类轮

沿用 V2 §8.8 第 4 条已定的取向（**澄清做成工具而非强制前置轮**，避免每问多付一次 LLM）：

- 新增工具 `select_skill(skill_name)`，`when_to_use` 描述写进 schema 的 description，
  模型在明确匹配时自己调——与 CC 用 Skill 工具选 skill 同构。
- 调用后：后端换上该 skill 的 `prompt_overlay`、按 `tool_allowlist` 收窄/解锁工具、置 `render_contract`。
- **不调 = 现有通用行为**（当前 12 工具全开、通用 prompt、`answer_to_blocks` 投影）。
  简单问题零开销，复杂/写侧任务才进 skill。

> **为什么不用前置分类器**：强制分类轮给每问固定加一次 LLM 调用，把 V2 好不容易压到的
> `avg_llm_calls=2.6` 又推回去（V2 §8.8）。工具形态让分派**opt-in**——这与 CC 的 skill
> 也是描述驱动、模型自选、非强制一致。

---

## 6. 不可动的不变式

1. **skill 只收窄/解锁工具与改渲染，绝不绕守卫。** run_sql 仍过只读 + RBAC（`agent_run_sql_min_role`）
   + `sql_soundness` 自证；答案仍过 `FactLedger`/`answer_verifier` 接地闸门（V2 §1.2）。
   skill 决定「用哪组能力、答案长什么样」，**不决定「正确性放松多少」**。
2. **建数 skill 严格走 draft→confirm→execute。** Data Agent 只产草稿提案块，写库由用户确认后
   由 `agent_pipeline` 执行。不开写侧后门（§4.2）。
3. **每个 skill 进 golden set。** 复用 P0 的 LLM stub + 遥测（V2 §3）：断言每类问题**选对 skill**、
   **发对块**、**守卫行为不变**。新增遥测量 `skill_distribution` 与 `skill_misroute_rate`。
   绝对拒答数、拒绝码分布**不得因 skill 引入而变化**（沿用 V2 §8.12 的盯法）。
4. **块协议是破坏性契约变更，一次性做对。** 参照 V2 §8.6 第 5 条 `_search_envelope` 的经验：
   契约变更要有测试钉住（`answer_to_blocks` 对每种老 payload 形态各验一次投影结果）。

---

## 7. 分期

四期递进，每期独立可上线、可回滚。

| 期 | 主题 | 范围 | 独立价值 | 前置 |
| --- | --- | --- | --- | --- |
| **S0** ✅ | 渲染块协议 | `blocks[]` schema + 前端 `BlockRenderer` 注册表 + `answer_to_blocks` 投影；口径卡改自适应 `mapping` 块。**先不加 skill** | **单这一步就拆掉写死报表模板**，口径卡三态自适应 | 无 |
| **S1** ✅ | Skill 骨架 + 取数图表 | `Skill` 抽象 + `select_skill` 工具 + overview/query 两 skill（纯现有能力重组）+ **chart 块**（接 dataviz） | 取数类有图表、有自适应展示；skill 维度落地 | S0 |
| **S2** ✅ | 血缘 skill | lineage 块 + `get_lineage` 工具 + 复用 P4.2 子 agent 定位子图 | 「看血缘」进入对话 | S1 |
| **S3** ✅ | 建数 skill | `propose_draft` 工具 + draft_proposal 块，桥接 `agent_pipeline` | 打通「问→查→建看板/建模」写侧闭环，补 V2 唯一缺口 | S1 |

### 依赖图

```
S0 渲染块协议 ──┬──► S1 skill 骨架 + chart ──┬──► S2 血缘 skill
               │                            └──► S3 建数 skill（接写侧骨架）
               └──► 历史消息 answer_to_blocks（兼容）
```

**推荐从 S0 起步**：不碰推理层、不碰守卫，纯前端块化 + schema 扩展，风险最低，
却直接消掉用户看到的「死板」。S1 之后 skill 才真正让不同任务类型分道。

---

## 8. 各期验收

| 期 | 验收 |
| --- | --- |
| **S0** | `ChatBubble` 写死 `&&` 分支 9→0；口径卡在「单表查询 / 编译指标 / 无关」渲染 inline / caliber / 不出现三态；历史消息经 `answer_to_blocks` 正常回放；golden 全绿（块投影不改变既有断言的语义结论） |
| **S1** | overview/query 两 skill 各有 golden 用例断言选对 skill + 发对块；`skill_misroute_rate` 有基线；chart 块在时间序列/类别对比两类问题各出一次合适图型；不选 skill 时行为与 S0 逐字节一致 |
| **S2** | 血缘类问题选中 lineage skill；子图定位走子 agent（主上下文增量 < 阈值，参照 V2 §8.11 隔离比）；大环数据下 lineage 块不崩（邻域下钻而非全图） |
| **S3** | 建数类只产 draft_proposal 块、**零写库**；点「去确认」跳既有 `agent_pipeline` 确认流；越权/无写权限时优雅降级为「仅建议，不可提交」 |

---

## 9. 与既有能力的关系

- **不替代 V2 的任何交付**：12 工具、语义卡、接地/自证、遥测/golden 全部保留；skill 是在其上加
  「任务类型 + 展示契约」两个维度。
- **复用生成数据应用**：query/analysis 的 chart 块可直接喂给既有 `generate-app`/`generate-widget`，
  形成「问→查→建看板」闭环（承接 V1 §10）。
- **本体仍是一级源**：所有检索基于已发布本体；建数 skill 的草稿也进本体治理骨架，
  延续「本体=一级源数据」战略方向（记忆 `ontology-is-primary-source`）。

---

## 10. 评审决策（已定稿）

| # | 决策点 | 结论 | 理由 |
| --- | --- | --- | --- |
| 1 | 块协议上线时旧字段处置 | ✅ **保留双写 + `answer_to_blocks` 兜底** | 零迁移、历史不空白、旧字段作降级通道；后续单开一次清理期删旧字段，不与 S0 耦合 |
| 2 | skill 路由 | ✅ **`select_skill` 工具、模型自选（opt-in）** | 与 CC skill 同构、零回归、不加强制 LLM 轮（护住 V2 的 `avg_llm_calls=2.6`）；不选=现有通用行为 |
| 3 | chart 中间描述 | ✅ **自研精简 `spec`**（`kind` 枚举 + x/y/series/agg） | 幻觉面坍缩成一组枚举（承接 V2 主题）、易校验、库无关、前端用 `dataviz` 规范渲染；复杂图再扩 schema |
| 4 | S3 建数 skill 与 M6 口径物化 | ✅ **分开推进，共用 `compile_metric` 接缝** | S3 只做只读草稿提案、不碰物化写库，守住 V2「只读问答 + 写侧走治理制品」边界；但设计成复用同一编译器，不另写翻译（对齐 V2 §6 第三条留的指针） |

### 10.1 决策对分期的影响

- **决策 1** → S0 增一个 `answer_to_blocks(payload)` 投影器 + 对每种老 payload 形态各一条投影测试（§6 不变式 4）。
- **决策 2** → S1 的 `select_skill` 工具 schema 里，`skill_name` 用 `enum`，`description` 承载各 skill 的 `when_to_use`。
- **决策 3** → `chart` 块的 `spec.kind` 定为闭合枚举 `line|bar|area|pie|scatter`；`x/y` 必须是 `data_result` 的真实列名（否则块被丢弃，不渲染臆造列）。
- **决策 4** → S3 的 `propose_draft` 与未来 M6 都调 `metric_compiler.compile_metric`；S3 不进 `MATERIALIZE_*` 计划的范围，两条线各自独立可回滚。

---

## 11. S0 实现说明（与设计的差异 · 已交付）

**交付物**

| 层 | 产出 |
| --- | --- |
| 后端 schema | `ChatBiBlock`（`extra=allow`）+ `ChatBiAnswer.blocks`（`schemas/chat_bi.py`） |
| 后端投影 | `services/chat_bi_blocks.py::answer_to_blocks` + 自适应 `_mapping_variant` |
| 后端接线 | `api/chat_bi.py` 两处终态 funnel 各调一次投影（非流式返回前、SSE `done` 下发前） |
| 前端类型 | `ChatBiBlock` 判别联合 + `ChatBiAnswer.blocks?`（`types.ts`） |
| 前端兜底 | `utils.ts::answerToBlocks`（旧消息 / 流式，规则与后端一致） |
| 前端渲染 | `ChatBiReferences.tsx`：`BlockRenderer` 注册表 + `MappingBlock`（inline/caliber）；`ChatBubble` 的 9 段 `&&` 阶梯 → `blocks.map` |
| 样式 | `.chatbi-mapping-inline`（`styles/chat-bi.css`） |
| 测试 | `test_chat_bi_blocks.py`（11 例）+ `test_b9_semantic_quality` 真实 `/chat-bi/ask` funnel 断言 |

**落地时对设计做的务实调整**

1. **投影点选在 API funnel（两处），不是服务层。** 服务层产出的是 **dict** payload，且终态 `done`
   分散在多条分支（无本体 / mock / 拒答 / 正常各一条）。API 层是这些 payload 的**唯一收口**
   （非流式 `ask()` 返回、流式 `done` 事件），两行 `payload["blocks"] = answer_to_blocks(payload)`
   即覆盖全部路径，且落库自然带上 blocks。逐个改服务层 done 站点既啰嗦又易漏。

2. **块模型故意「宽」以求前向兼容。** 后端 `ChatBiBlock` 用 `extra=allow`、前端用「判别联合 +
   `default` 跳过」，于是 S1 的 `chart / lineage / draft_proposal` 新块**新增字段不改本模型、
   未知类型运行时优雅跳过**——兑现 §3.2「加新块类型不用改渲染主干」。

3. **投影规则有两份实现（Python + TS），一致性靠纪律 + 测试。** 后端 `answer_to_blocks` 是双写正源；
   前端 `answerToBlocks` 只兜底「没有 blocks 的旧消息」与「流式中」（此时 `payload.answer` 仍空，
   正文取实时 `content`）。两份各有测试钉住，但**改投影规则（尤其 `mapping_variant` 阈值）必须同步两处**
   ——已在两处源码互相留注释。

4. **动作条不入块，是有意的。** 「生成数据应用」按钮与错误态是**交互 / 页面态**，不是回答内容，
   故留在 `blocks.map` 之外。「9 段 `&&` → 0」指的是**内容阶梯**清零；动作 footer 是刻意保留的独立区，
   对应 §3.2 说的「内容 vs 动作分离」。

5. **mock 提示也是一个 notice 块——funnel 断言当场确认。** 无 LLM 环境下的拒答，投影结果是
   `[notice(refused), markdown, notice(mock)]`（三块，非两块）。第三块来自 `used_mock`，是**正确行为**
   而非多余——`test_chat_bi_no_hit_refuses_fiction` 的断言写实了这一点，避免后人误当 bug 删掉。

**验收对照（§8 S0）**：ChatBubble 内容型 `&&` 分支 9 → 0；口径卡在单表查询 / 编译指标 / 无关
三场景渲染 inline / caliber / 不出现；旧消息经 `answerToBlocks` 回放。后端 **878 通过 / 1 skip(live)**，
前端 **`tsc` 干净**。

> **留给 S1 的接缝**：`select_skill` 工具选中某 skill 后，其 `render_contract` 决定发哪些块——
> 届时后端 `answer_to_blocks` 从「无条件按字段投影」升级为「按 skill 的块清单投影 + skill 自产的
> chart/lineage/draft 块」。块协议与渲染注册表本期已就位，S1 只加块类型与 skill 分派，不动渲染主干。

---

## 12. S1 实现说明（与设计的差异 · 已交付）

**交付物**

| 层 | 产出 |
| --- | --- |
| 技能注册表 | `services/chat_bi_skills.py`：`Skill` dataclass + overview/query 两技能 + `skill_choices_text()` |
| 技能层工具 | `chat_bi.py`：`_SELECT_SKILL_TOOL`、`_RENDER_CHART_TOOL`、`_BASE_TOOL_SCHEMAS`、`_TOOL_BY_NAME`、`_tools_for_skill()` |
| 循环接线 | `_stream_agent_events`：`active_skill`/`active_tools`/`charts` 运行态；`create(tools=active_tools)`；`select_skill`/`render_chart` 特判分派；done payload 加 `skill`+`charts` |
| 处理器 | `_apply_select_skill`（叠 overlay + 解锁工具）、`_dispatch_render_chart`（图型枚举 + x/y 接地校验） |
| 块投影 | `chat_bi_blocks.py`：`charts` → chart 块（自带数据行），紧随结果表 |
| 前端 | `ChatBiBlock` 加 chart 成员；`ChatBiChart`（SVG bar/line/area）+ 注册进 `BlockRenderer`；步骤图标 |
| 测试 | `test_chat_bi_skills.py`（10 例，含 select_skill→run_sql→render_chart 端到端穿真实循环）+ `test_chat_bi_blocks.py` chart 投影 |

**落地时对设计做的务实调整**

1. **「只解锁不收窄」（已拍板）。** skill 只做两件事：叠 prompt overlay + 解锁额外工具
   （query 解锁 render_chart）。基础 12 工具永远可用，**从不移除**——故零回归，且天然为
   S2/S3 的 get_lineage / propose_draft「解锁」铺好机制。文档 §4 的「收窄工具」推到 S1.x。
   不变式测试 `test_tools_only_unlock_never_shrink`：任何技能的工具集都 ⊇ 基础集。

2. **路由是 opt-in 工具，不是强制分类轮。** `select_skill` 由模型按 `when_to_use` 自己调；
   不选=现有通用行为。护住 V2 的 `avg_llm_calls`——不给每问硬加一轮（沿用 V2 §8.8 第 4 条）。

3. **chart 是「先取数、再作图」的两段式，x/y 强制接地。** `render_chart` 要求已有 run_sql
   执行结果，且 x/y 必须是**真实结果列**，否则拒绝并回可用列清单——图表不许对臆造列作图
   （承接决策 3 与 V2 的接地主题）。chart 块**自带数据行**，前端自足渲染，不回看兄弟表格。

4. **chart 图型 S1 只做 bar/line/area。** 决策 3 的全枚举是 `line|bar|area|pie|scatter`；
   S1 先落三型（覆盖类别对比 + 时间序列的绝大多数问数场景），`render_chart` 的 `kind` enum
   当期即锁这三个。pie/scatter 留 S1.x。前端沿用 `DataAppRenderer` 的手绘 SVG 风格，**不引图表库**
   （项目现状无图表依赖，保持一致）。

5. **`select_skill` overlay 重选即替换，不叠加。** 循环里捕获 `base_system` 基线，每次选技能
   重建 `messages[0] = base_system + overlay`——多次切换不会层层堆叠污染 system。

**验收对照（§8 S1）**：overview/query 两技能落地；`select_skill` opt-in、不选零回归；
`render_chart` 在时间序列/类别对比出图且拒臆造列；**端到端测试**证明
`select_skill(query)→run_sql→render_chart` 穿过真实 agent 循环、payload 带 `skill`+`charts`、
chart 块紧随结果表。前端 `tsc` 干净。

> **测试环境说明**：S1 落地期间仓库内有一条并行的「治理规约 G0+G1」工作流
> （`app/governance/`、`app/agents/validation.py`、`test_governance_standard.py`），其未完成状态
> 会在 `pytest-randomly` 全量随机序下引发跨用例级联失败。**S1 自身的完整爆炸半径**
> （chat_bi golden/skills/blocks/no_refuse/overview/text_tool_calls + retrieval_subagent + b9 +
> warehouse_generator，共 98 例）合并运行全绿；全量失败与本期无关，不在此修他人未完成的改动。

> **留给 S2 的接缝**：血缘 skill 解锁 `get_lineage` + 复用 P4.2 `locate_entities` 子 agent；
> 新增 `lineage` 块（前端注册表加一个 case，渲染主干不动）。skill 解锁机制、块协议本期已就位。

---

## 13. S2 实现说明（与设计的差异 · 已交付）

**交付物**

| 层 | 产出 |
| --- | --- |
| 技能注册表 | `chat_bi_skills.py`：新增 **lineage**（血缘影响）技能，解锁 `get_lineage` |
| 血缘工具 | `chat_bi.py`：`_GET_LINEAGE_TOOL` schema；`_dispatch_get_lineage`（走 `_dispatch_agent_tool` 通用分派，纯 DB 读）；循环 harvest `lineage` + `grounded_hit` + done payload 加 `lineage` |
| 接地 | `_ledger_register` 加 `get_lineage` 分支：邻域节点名/关系名入账，答案解读上下游对象名不被 F4 判幻觉 |
| 块投影 | `chat_bi_blocks.py`：`lineage` → lineage 块（center + nodes + edges），紧随图表块 |
| 前端 | `ChatBiBlock` 加 lineage 成员（复用既有 `GraphNode`/`GraphEdge`）；`ChatBiLineage` 手绘 SVG（三列上下游）+ 注册进 `BlockRenderer`；步骤图标 |
| 测试 | `test_chat_bi_skills.py`：get_lineage 邻域 + 拒未知中心 + **select_skill(lineage)→get_lineage 端到端**；`test_chat_bi_blocks.py`：lineage 块投影 |

**落地时对设计做的务实调整**

1. **血缘 = 已发布关系图，不是 DataHub。** 排查确认 DataHub 表级血缘在 ingest 时已被
   `evidence_builder` 落成 `structure_type="derivation"` 的 `RelationType` 边（`{src}_feeds_{tgt}`），
   ingest 后 DataHub 原始血缘不可查。故 `get_lineage` 直接包 **`OntologyQueryService.get_ontology_graph`**
   （中心 + depth BFS，`published_only=True`），天然按域/发布收敛；`derivation` 边即数据加工血缘，
   与外键/引用等业务关系同图共存、由 `structure_type` 区分。

2. **`get_lineage` 走通用分派，不占循环特判。** 它是纯 DB 读、无循环态依赖（不像 select_skill 要改
   messages、render_chart 要读 data_result），故放进 `_dispatch_agent_tool`（已持 db+ontology_id），
   只在 harvest 段捕获 `lineage`。center_id 须是已发布对象 id，缺失/未命中带 hint 拒绝
   （「先 search_objects 拿 id」）——lineage skill 的 overlay 也这么教。

3. **前端沿用手绘 SVG，不引 g6。** 项目虽有 `@antv/g6` + `OntologyGraphView`（LR dagre），
   但那是重量级 canvas（g6 生命周期 + useNavigate + 全屏/LoD），塞进每条聊天气泡不相称，
   且与聊天既有「纯内联 SVG 小可视化」惯例（`ChatBiChart`/`ClusterMatrixView`）冲突。
   `ChatBiLineage` 手绘三列布局（上游 | 中心 | 下游），1 跳邻域最贴切；边按 `structure_type` 着色
   （derivation=紫）。多跳/大图再升级到复用 `OntologyGraphView`（`embedded` + 固定 height）。

4. **子 agent 复用是「可用而非强制」。** 设计写「复用 P4.2 `locate_entities` 子 agent 定位子图」——
   `locate_entities` 本就在基础工具集里，模型可先用它在大域里定位中心对象、再 get_lineage，
   无需为血缘另造一条子 agent 路径。S2 未新增隔离逻辑，是复用既有隔离范式。

**验收对照（§8 S2）**：血缘类问题选中 lineage 技能；`get_lineage` 邻域下钻（非全图）；
大环数据下 lineage 块按 center+depth 邻域渲染不崩（截断带提示）；**端到端测试**证明
`select_skill(lineage)→get_lineage` 穿真实循环、payload 带 `lineage`、投影出 lineage 块。前端 `tsc` 干净。
S2 爆炸半径（chat_bi 全组 + retrieval_subagent + b9 + ontology 图）83 例合并全绿；并行治理工作流的全量
级联失败与本期无关（说明见 §12）。

> **留给 S3 的接缝**：建数 skill 解锁 `propose_draft`，桥接既有 `agent_pipeline`
> （draft→confirm→execute）；新增 `draft_proposal` 块（带「去确认」按钮，只读提案不写库）。
> skill 解锁机制、块协议、接地范式本期已全部就位。

---

## 14. S3 实现说明（与设计的差异 · 已交付）

**范围决策（§10 决策 4 的落地选择）**：建数 skill 提「**新口径定义草稿**」，不是「物化已有指标到表」。
用户描述想要的指标/标签/规则 → agent 出一份 **BusinessLogic 提案**（名字 + 类型 + 口径说明），
「去确认」→ `POST /api/business-logics` 建 SUGGESTED 草稿口径 → 用户补全表达式并走发布。

**交付物**

| 层 | 产出 |
| --- | --- |
| 技能注册表 | `chat_bi_skills.py`：新增 **create**（建数·口径提案）技能，解锁 `propose_draft` |
| 提案工具 | `chat_bi.py`：`_PROPOSE_DRAFT_TOOL`；`_dispatch_propose_draft`（走通用分派，纯 spec 无 DB 写）；循环 harvest `draft_proposals` + `grounded_hit` + done payload；`_ledger_register` 登记提案名 |
| 块投影 | `chat_bi_blocks.py`：`draft_proposals` → draft_proposal 块 |
| 前端 | `ChatBiBlock` 加 draft_proposal 成员；`DraftProposalBlock` 卡片 + 「去确认创建」按钮（`api.createBusinessLogic` → 跳 `/business-logic/:id`）+ 注册进 `BlockRenderer`；步骤图标 |
| 测试 | `test_chat_bi_skills.py`：propose_draft 载荷/校验/派生名 + **端到端 + 只读不变式**（ask() 建 0 条 BusinessLogic）；`test_chat_bi_blocks.py`：draft_proposal 块投影 |

**落地时对设计做的务实调整**

1. **agent 只出提案，写在用户点击时——`ask()` 严格只读。** `_dispatch_propose_draft` **零 DB 写**，
   只组装一份 `create_payload`（`POST /api/business-logics` 的 body）。真正建草稿由前端「去确认」按钮
   触发（用户显式动作），随后跳口径详情页补全表达式、再走既有发布确认流。**Data Agent 不新增任何
   写侧出口**（§6 不变式 2）；端到端测试 `test_create_skill_flow_stays_read_only` 钉死「ask() 前后
   BusinessLogic 计数不变」。

2. **不替用户编口径表达式。** 项目既有立场是「口径是人定的，不该由模型编」（`MetricDrafter` 的原话）。
   故提案只含中文名 + 类型（metric/tag/rule）+ 自然语言说明，`expression` 留空由人补——技能 overlay
   也明确这么教，并要求先 `search_logics` 查重、避免重复建。

3. **两个 create 语义里选了「建新定义」而非「物化已有」。** 排查发现两条独立写侧骨架：
   Skeleton B（`POST /api/business-logics` 建定义 → 发布确认）与 Skeleton A（`POST /api/agents/draft`
   的 `GovernanceArtifact`，metric drafter 只选已发布口径去物化）。「建数/创建数据」取**建新定义**语义
   （Skeleton B），最贴合用户本意与「本体=一级源、正向生成」方向；物化已有指标（Skeleton A）与既有
   Materialize 功能重叠，未纳入本期。

4. **grounded_hit + 接地登记。** 建数答案常不引用已有数据，若不算 grounded 会被误拒——故
   `propose_draft` 计入 `grounded_hit`；提案的口径名入事实账本，答案复述「建议新建指标 X」不被 F4 判幻觉。

**验收对照（§8 S3）**：建数类只产 draft_proposal 块、**零写库**；「去确认」跳既有 `POST /api/business-logics`
→ 口径详情补全/发布流；类型非法/缺名带 hint 拒绝。**端到端测试**证明 `select_skill(create)→propose_draft`
穿真实循环、payload 带 `draft_proposals`、投影出块、且 ask() 零落库。前端 `tsc` 干净。
S3 爆炸半径 89 例合并全绿；并行治理工作流的全量级联失败与本期无关（说明见 §12）。

---

## 15. V3 收官小结

五 skill 全部落地（overview / query / analysis 归入 query / lineage / create），一条渲染块协议
（markdown / sql / table / chart / mapping / lineage / draft_proposal / steps / notice / clarify / refs），
一套 opt-in 的 `select_skill` 路由（**只解锁不收窄**，零回归）。「本体映射死板」由「扁平 DTO + 写死 JSX 阶梯」
变成「skill 声明 + 块注册表渲染」；取数有图、看血缘有图、建数有提案闭环。守卫（只读 / soundness /
FactLedger 接地）全程未松动，写侧仍走既有 draft→confirm→execute 治理骨架。

**后续可选（未纳入 V3）**：chart 的 pie/scatter（S1.x）；lineage 多跳升级复用 `OntologyGraphView`；
建数扩到对象/字段提案；skill 的「render_contract 收窄块清单」（当前投影仍是字段驱动、advisory）。
