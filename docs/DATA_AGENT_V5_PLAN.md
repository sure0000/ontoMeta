# Data Agent V5 计划：V4 实测调参 + 收尾拆模块 + 能力延伸

> 状态：**P0 已交付 + P0.5–P0.9 真实会话实测完成 + P1 收官 + P2 已交付 + P3 部分交付**（T3/T3.2 交付；T2 两半均已实测定案——history 维持 6000、sample_rows 维持 5；T4 拆模块完成；T5 scout 多步链交付）。F1/F2/F4/F5 已修，F3 已解（ERP 域可查）。仅 T6（trace 回放 eval）有设计缺口待定。trace 已关回。
> 后续：承接 [DATA_AGENT_V4_HARNESS_PLAN.md](./DATA_AGENT_V4_HARNESS_PLAN.md)（S0–S3 全部交付）的「后续可选」。
> 本计划做三件事：**① 用真实会话把 V4 的收益量出来并据实调参；② 收尾未拆完的模块化；③ 在已验证的骨架上做能力延伸。**
> 主战场：`backend/app/services/chat_bi.py`、`chat_bi_tool_schemas.py`、`agent_compaction.py`、
> `agent_result_store.py`、`agent_subagent.py`、`query_scout_agent.py`、`agent_trace.py`、`agent_telemetry.py`。

---

## 0. 一句话结论

V4 交付的六项优化（O1–O6）都带了**开关与遥测**，但收益值目前只有**离线单测**支撑，缺**生产实测**：
`agent_trace_enabled` 默认关、各预算参数（`agent_history_char_budget=6000`、`agent_result_sample_rows=5`）
是**拍脑袋的初值**。V5 先把「量」建在真实会话上，再据实调参；顺带收掉 V4 明确留下的两处尾巴
（`_ReferenceResolver`/`_ObjectSnapshot` 未拆、scout 只单步），最后在已验证的骨架上延伸能力。

> **总纲**：先测后调、先收尾后延伸。**不碰守卫**（只读 SQL + RBAC + 治理闸门），
> `ask_stream` done 契约不变，每阶段跑 golden 20 例 + 全套件（当前 1131 passed）作护栏。

---

## 1. V4 现状盘点（代码锚点）

| 维度 | 现状 | 锚点 |
| --- | --- | --- |
| 轨迹落地 | JSONL，**默认关闭** | `agent_trace.py`；`config.py: agent_trace_enabled=False` |
| compaction | 抽取式摘要，预算 `agent_history_char_budget=6000`（初值） | `agent_compaction.py`；`config.py` |
| 大结果离场 | 样例 `agent_result_sample_rows=5`（初值）+ `read_result` 分页 | `agent_result_store.py` |
| 子 agent | 通用骨架 + 检索 + 取数探路（scout，**单步**） | `agent_subagent.py`、`query_scout_agent.py`、`retrieval_agent.py` |
| 模块化 | 工具声明已拆；引用归一已拆（T4）；只剩 `ChatBiService` 在 `chat_bi.py`（4229 行） | `chat_bi.py`、`chat_bi_tool_schemas.py`、`chat_bi_references.py` |
| 遥测指标 | 快照含 `context_chars_per_call`、`offloaded_chars`、`subagent_isolated_chars`、`skill_misroute_rate`、`compaction_runs` | `agent_telemetry.py`；`GET /chat-bi/telemetry` |

**当前基线**（V2 §8.12 + V4）：`avg_llm_calls=2.6`、`avg_steps=1.45`、golden 20 例、全库 1131 passed。

---

## 2. 优化项与前后对比

| # | 优化项 | 前 | 后 | 预期提升 |
| --- | --- | --- | --- | --- |
| **T1** | trace 实测 + 指标看板 | 遥测有字段但默认不落盘、无聚合视图 | 开 trace 采一段真实会话；出「V4 收益」对照表 | 收益从「单测推断」变「生产实测」 |
| **T2** | 预算参数据实调优 | `history_char_budget=6000`、`sample_rows=5` 为拍脑袋初值 | 按实测的 `context_chars_per_call`/摘要触发率/`read_result` 命中率调 | 上下文再降而不丢连续性/保真 |
| **T3** | compaction 关键段保留 | 抽取式摘要（引号实体 + 首句），未显式保关键 SQL/口径 | 摘要显式保 `key-SQL`/`compiled-metric` 段（对齐 pi Critical Context） | 长会话「延续口径」更稳，少重算 |
| **T4** | 收尾拆模块 | `_ReferenceResolver`+`_ObjectSnapshot`（~200 行）仍在 `chat_bi.py` | 拆到 `chat_bi_references.py`，re-export 保契约 | `chat_bi.py` 再瘦身；引用归一可独立测 |
| **T5** | scout 扩多步取数链 | scout 单步产一条候选 SQL | 支持「探路→取样→按样例改写→再取」链，仍不执行 | 复杂取数一次探到位，主上下文更省 |
| **T6** | trace 回放型 eval | golden 用 LLM stub（固定序列） | 把实测 trace 脱敏后转成 golden 用例 | golden 覆盖真实分布，回归更真 |

---

## 3. 实施阶段与进度

> 图例：⬜ 未开始 · 🟡 进行中 · ✅ 已交付

| 阶段 | 内容 | 风险 | 状态 |
| --- | --- | --- | --- |
| **P0** | T1 trace 实测 + 指标看板（先建「量」，后面调参有依据） | 低 | ✅ |
| **P1** | T2 预算据实调优 + T3 compaction 关键段保留 | 低 | ✅ **T3/T3.2 ✅；T2 两半均实测定案——history 维持 6000、sample_rows 维持 5（F3 已解）** |
| **P2** | T4 收尾拆 `chat_bi_references.py`（纯结构、零行为变化） | 中 | ✅ |
| **P3** | T5 scout 多步取数链 + T6 trace 回放型 eval | 中 | 🟡 **T5 ✅；T6 有设计缺口待定** |

### P0 详细任务

- [x] T1.1 轨迹记录扩字段：`write_trace` 新增 `offloaded_chars`/`offload_count`/`subagent_*`（汇总脚本直读）。开 `agent_trace_enabled` 即可采真实会话
- [x] T1.2 写 trace 汇总脚本 `scripts/summarize_agent_traces.py`：从 JSONL 算 `context_chars_per_call`、`offloaded_chars`、`compaction` 触发率、`skill_misroute_rate`、子 agent 隔离字符，按 skill/intent 分组
- [x] T1.3 端到端验证：驱 golden stub（trace 开）产 19 条真轨迹 → 汇总脉出基线 `avg_llm_calls=2.6`、拒答 21.1%（与文档一致）；另驱离场场景验证 O2 收益入表（移出 1334 字符）

> **P0 交付**：`scripts/summarize_agent_traces.py`（只读聚合）+ `test_trace_summary.py`（7 例）+ `write_trace` 字段扩。全库 **1138 passed**。
> **实测基线**（golden 19 条，单轮小结果故 O1/O2/O4 未触发=0，符预期）：`avg_llm_calls=2.6`、`avg_steps=1.4`、`avg_context_chars_per_call≈3812`。
> **使用**：`python scripts/summarize_agent_traces.py [--dir DIR] [--day YYYY-MM-DD] [--json]`。

### P0.5 真实会话实测（已采 14 条，域=数据域-ERP-全量 80 对象纯主数据、无绑定可用数据源）

打开 `AGENT_TRACE_ENABLED=true` 后用 curl 驱八类真实问答 + 五条结构性，实测汇总：

| 指标 | 实测 | 解读 |
| --- | --- | --- |
| avg_llm_calls | **5.4** | 高于基线 2.6——**因该域缺对象**，模型反复搜索无果才拒答（非回归，是场景） |
| avg_steps | 7.9 | 同上，拒答前多步搜索 |
| avg_context_chars_per_call | 10057（峰值 16079） | 含常驻域语义卡/阶梯；单轮会话history 短，故 O1 未触发 |
| O4 子 agent | **7 次，隔离 34838 字符，隔离比 995×** | ✅ 真实场景下隔离收益显著（代价 35 次子 LLM 调用） |
| O1/O2 | 0 | 单轮会话 + 无可执行数据源，未触发（O2 已由 e2e 测证） |
| skill_misroute_rate | 100%（2/2） | ⭕ **度量缺陷**（见下） |

**实测抢出三个真问题（非本期计划内，已处理 F1/F2）：**

- **F1（度量缺陷）✅ 已修**：`skill_matched` 把「选对了技能但实体不存在、未及调解锁工具就拒答」也计作 misroute。
  实例：选 lineage 后反复 search 找不到对象、未及 `get_lineage` 就拒；create 同理。
  **修法**：新增 `skill_no_entity` 态（拒答 + 真 search 过但未及解锁工具 = 路对但无实体），真 misroute 率分母排除它。
- **F2（0 步拒答）✅ 已修**：结构性/取数意图下，首轮无工具且譍气像拒答时，**逆一次“先 search 再判”**（只逆一次）。
  **验证**：进程内集成测讁实（turn1 拒→逆→turn2 search→turn3 作答，LLM 3 调、steps=1）。真实模型对「存在的对象」本就会搜（实测“商机” 8 步）；F2 只兵底拦「真未搜就拒」。
- **F3（真实大结果难采）**：当前库无已验证的可执行数据源，真 `run_sql` 不可行——O2 实测需接一个真数据源的域。

> **F1/F2 交付**：`agent_telemetry` 新增 `skill_no_entity`/`skill_no_entity_runs` + misroute 分母排除；
> `chat_bi` 新增 `_looks_like_refusal` + 首轮逆搜守卫；`summarize_agent_traces` 区分“路对但无实体”。
> 新增 2 单测 + 3 集成测（`test_f2_search_before_refuse.py`）；全库 **1146 passed**。
> **环境坑**：uvicorn `--reload` 会留子进程占端口（service.sh 不追踪），导致旧代码进程残留；重启前需 `lsof -tiTCP:8000 | xargs kill -9` 清干净。

> **对 T2 的启示**：单轮会话history 短，compaction 自然不触发；要调 `agent_history_char_budget` 必须先采到**长会话**（同一 conversation_id 多轮）。
> 本轮已验证 O4 真实收益（995× 隔离比）。

### P0.6 长会话实测（同一 conversation_id 跑 10 轮，history 累至 10746 字符）

用同一会话连续 10 轮下钻商机对象群，逐轮累积 history → **首次在真实会话中观测到 O1 触发**：

| 指标 | 实测 | 解读 |
| --- | --- | --- |
| O1 compaction | **第 8–10 轮触发（history 超 6000 后），各摘 2 轮** | ✅ 真实会话首次观到触发 |
| 上下文抑涨 | 触发轮 ctx/call 均值 **10371 < 未触发轮 10941** | history 从 6400→10746 但摘要把旧轮压掉，ctx/call 未继续爆涨 |
| F1 修后 misroute | 30 次路由，真 misroute **10%**（路对但无实体 0） | ✅ F1 生效（从先前虚高的 100% 降下来） |
| O4 子 agent | 11 次、隔离 51027 字符、隔离比 928× | ✅ 持续验证 |

**T3 验证**：本次长会话无 run_sql（域无可执行数据源），答案不含真 SQL 围栏，故 `key_sql` 为空。
另用「真实 compaction 流程 + 注入一轮带口径 SQL 的答案」验证：compaction 触发时那条
`SELECT … GROUP BY opportunity_type` **完整保住进摘要（含 GROUP BY 尾巴、未被首句截断）**——T3 在真实流程里正确。

> **小结**：O1/T3/F1 均已在真实（或真实化）会话中验证。O2/T2 的实测仍待一个「有可查数据 + 能长会话」的域（F3）。

### P0.7 T2 复采（2026-08-07）：换域实测，抢出 F4

P0.6 之后接入「数据域-ERP-全量」（724 已发布对象 / 4113 关系）复采，**第一轮就撞上一条
比调参更要紧的缺陷**——长会话从第 2 轮起系统性误拒，history 根本涨不起来，T2 无从谈起：

| 轮 | 问句 | 结果 |
| --- | --- | --- |
| 1 | 这个域里和采购相关的对象有哪些？ | ✅ 819 字符（10 次 search，接地） |
| 2 | 采购订单这个对象有哪些字段？ | ❌ 拒答（llm_calls=1、steps=0、unverified=[]） |
| 3 | 采购发票呢？它和采购订单是什么关系？ | ❌ 拒答 |

- **F4（自信作答被判未接地）✅ 已修**：多轮会话里模型会**照着上一轮的上下文直接作答**、
  本轮一次工具都不调。这类文本**不像拒答**，于是 F2 的逆搜守卫（只逆「像拒答」的）逆不到；
  可 `grounded = (grounded_hit or intent=="general") and verify_ok` 要求**本轮**真有工具命中，
  于是那条**答对了的**答案被整段换成「未检索到匹配的对象类型或业务逻辑」。
  用户看到的是拒答，模型其实答对了；而 `unverified=[]` 说明 F4 校验根本没有异议。
  **修法**：把逆的触发条件从「像拒答」放宽到「本轮零工具」——两种都是「未经核实就收尾」，
  同一处逆、只是话术分开（像拒答→「先搜再判」；自信作答→「本轮结论要有本轮凭证」）。
  **守卫不放宽**：`grounded` 仍要求本轮工具命中，逆只给一次补救机会，逆完仍不查照旧拒答。
  逆的额度与 F2 共用，一轮最多一次。
  验证：`test_f2_search_before_refuse.py` 新增 2 例（自信作答被逆后正常落地 / 额度只有一次），
  并把原「正常答案不该被逆」改写为「general 意图不该被逆」——那才是真正该豁免的一类。
  全库 **1156 passed**。

> 顺带记下另一条**未修**的观察：用**英文标识符**提问（`purchase_order 这个对象有哪些字段？`）
> 会 0 步拒答，换中文显示名（`采购订单…`）才走搜索。域里对象的 `name` 确实是英文标识符，
> 说明检索侧对标识符式问法的召回不如中文名。**不在本期动**，记录待评估。

#### 修完之后的 12 轮长会话（`scripts/drive_long_session.py`，采购链路下钻）

| 轮 | llm | 步 | ctx/call | compaction | 结果 | history 入参 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 6 | 11 | 50713 | — | ✅ | 0 |
| 2 | 4 | 2 | 65243 | — | ✅ | 630 |
| 3 | 3 | 2 | 49162 | — | ✅ | 1341 |
| 4 | 5 | 4 | **10824** | — | ✅ | 2251 |
| 5 | 3 | 3 | **12730** | — | ✅ | 2611 |
| 6 | 3 | 2 | 65050 | — | ✅ | 2784 |
| 7 | 4 | 9 | **79376** | — | ❌ F4 校验 | 3748 |
| 8 | 4 | 5 | 16042 | — | ✅ | 3951 |
| 9 | 4 | 2 | 68993 | — | ❌ F4 校验 | 4605 |
| 10 | 3 | 4 | 52083 | — | ✅ | 4804 |
| 11 | 2 | 0 | 50757 | — | ✅（general 豁免） | 5297 |
| 12 | 4 | 2 | 52480 | **触发/摘 2 轮** | ✅ | 6049 |

汇总：拒答率 16.7%（2/12）、`avg_llm_calls=3.8`、`avg_steps=3.8`、`avg ctx/call=47788`
（中位数 51420）、O4 隔离比 3995×、最终 history 6562 字符。

两处拒答都**不是** F4 那类 0 步拒答，而是 F4 校验（answer_verifier）把箭头连写的路径串
当成了未证实实体：`「供应商 → 采购订单 → 采购收货 → 采购发票 → 采购发票明细」`、`「生成」`。
**记录待评估**：校验器抽实体时应把箭头/分隔符连写的串拆开逐个核，而不是整串当一个名字。

### T2.1 结论（2026-08-07）：`agent_history_char_budget` **维持 6000，不调**

不是「没测所以不动」，是**测完了，数据说不该动**：

| 事实 | 数值 |
| --- | --- |
| history 峰值（12 轮累计） | 6562 字符 |
| ctx/call 均值 | 47788 字符 |
| **history 占上下文的比重（峰值口径）** | **≤ 13.7%** |
| ctx/call 实际波动区间 | 10824 ~ 79376 |

ctx/call 在 1 万到 8 万之间来回甩，而 history 在同期只是从 0 单调涨到 6562——
**上下文的大头不是历史，是常驻的域语义卡 + 阶梯深加载 + 当轮工具结果**（该域 724 对象、
4113 关系，比 P0.5 的域大一个量级，ctx/call 也从 10057 涨到 47788）。
把预算调到 0 也最多省下 13.7%，而代价是丢掉 T3 刚建起来的「延续上一轮口径」保证。
**真正的杠杆在别处**：常驻卡/阶梯的规模控制（大域下按需裁剪），那是另一件事，不是调这个数。

`agent_result_sample_rows` 当时还定不了（F3 未解）。**F3 已于同日解开**，见下节。

### P0.8 F3 解除（2026-08-07）：ERP 域真正可查

F3 一直被记成「没有可查数据源」，实际核下来是**三处配错叠在一起**，数据一直都在：

| 症状 | 实情 |
| --- | --- |
| ERP 库「表被删了」 | **没有**。ERPNext 的 MariaDB（`erpnext-db-1`，**3308**）完好：739 表、201 张有数据、`tabGL Entry` 43.8 万行。此前看的是 `mysql80`（3307）上的同名残留副本（1 张表） |
| 数据源连错库 | DataSource「测试」指着 3307。改指 3308（原值 `mysql+pymysql://root:root@192.168.31.53:3307`，回退用它） |
| 连对了仍查不了 | agent 按本体标识符写 `FROM sales_order`，物理表叫 `` `tabSales Order` ``；改写物理名又被语义证明器以 `unknown_table` 拒——两道闸夹死。这正是 `mapping_json` 的用途，而它是空的 |

**`_apply_mapping` 必须先改**：原实现 tables/columns 混在一起整词替换，而 ERP 的 724 个对象里
**203 个同时是子表的外键列名**（`sales_order` / `customer` / `item` …）——整词替换会把
`SELECT customer FROM sales_order` 里的**列** `customer` 也换成表名，产出一条语法合法、
语义全错的 SQL。**报错能被看见，静默错答不能**。故改为：tables 只在表位置
（FROM / JOIN / INTO / UPDATE / TABLE 之后）替换，columns 保持整词。+7 单测。
随后由 724 个对象的 `source_ref` 机械生成 `mapping_json`（零条解析失败）。

**顺带修掉一条一撞就死的 bug**：MySQL 金额列返回 `decimal.Decimal`，一路带到 `ask` 响应体
即 `TypeError: Object of type Decimal is not JSON serializable` → HTTP 500。
即「域一旦真能查，第一个问金额的问题就挂」。修在**执行器边界**（物理库的值进入应用的唯一入口）：
Decimal→float、date/datetime→isoformat、bytes→解码。Decimal 特意转 float 而非 str——
下游图表与 `analyze_result` 的统计要的是数，转字符串虽精确却会让每个金额列的图表和均值
悄悄失效，那比末位精度贵。+2 单测。

**端到端验证**：agent 自写 `SELECT COUNT(*) FROM sales_order` → 真执行 → 答 **73,912 条**，无拒答。

### P0.9 T2.1 sample_rows：测了 5 vs 20，**维持 5**（算术预测被实测推翻）

F3 一解开就把 `agent_result_sample_rows` 补测了：同一问句序列（6 个递增行数的取数问题，
最后一问专门逼它翻页）跑两遍，只换这一个参数。

调大的理由本来很硬：**结果一超过 5 行，模型下一步必定把余下的全部翻回来**——
20 行的那轮 `read_result(offset=5, limit=15)` 取走 15 行，8 行的那轮取走 3 行，无一例外。
那两次离场因此一分钱没省，只是多花了一次 LLM 往返。而多带 15 行按实测是 46~87 字符/行
≈1000 字符，多一次往返要重付一整轮 prefill ≈30800 字符——账面差 30 倍。

**对照结果**：

| 指标 | sample_rows=5 | sample_rows=20 |
| --- | --- | --- |
| `read_result` 往返 | 2 次 | **0 次** ✅ 机制如预测 |
| 离场字符 | 2449 | **0** ✅ |
| avg_llm_calls | 6.2 | **8.0** ❌ |
| avg_steps | 7.0 | **9.5** ❌ |
| 拒答率 | 0%（0/6） | 33.3%（2/6） |
| ctx/call | 30843 | 30691（基本没动） |

逐轮配对的 llm_calls：`2,6,6,10,7,6` → `3,7,8,10,10,10`，**5 涨 1 平 0 降**——
不是个别轮次的偏差，是一致的信号。样例行变多之后，模型在结果上兜圈子的轮次也变多，
省下的那一次往返被这个盖过去了。「`avg_llm_calls` 不回涨」是 V5 的验收护栏，
故**按实测维持 5**，不按算术预测改。

> 两次拒答与 sample_rows 无关，都是 **F4 校验的误判**：把问句自身的复述
> 「按客户统计销售订单总金额、取前 30 名」、以及一个数值「50个」当成未证实实体。
> 与 P0.7 记的箭头串误判同一类——**校验器的实体抽取需要单独收拾一次**，已累计三例。
> → 已收拾，见 P1.0（F5）。

### P1.0 F5（2026-08-07）：校验器抽取精度——ERP 域实测里的拒答**全部**是它

24 轮实测出现 4 次拒答，逐条查下来**无一是真幻觉**，全是抽取误判。追到底，
根因不在这几个个案，而在一条隐含假设：

> **`_BOOKNAME` 把每一段 「」 都当成「断言某实体存在」去核。**
> 可中文的「」本来就是引用与强调**任意短语**的括号，不只用来标实体名。

于是模型只要用「」强调一句话，就会被要求「这句话必须是本体里的实体」，核不到即拒答，
用户看到的是「你问的东西本体里没有」——问的明明是他自己刚打的字。四类实测原文：

| 实测原文（取自真实拒答文案） | 是什么 | 修法 |
| --- | --- | --- |
| 「文档状态、序号、成品物料、物料编码」 | 字段清单挤在一对括号里 | 按 `、，,;；/／→->\|` **拆开逐段核，全段可证才算证** |
| 「供应商 → 采购订单 → 采购收货 → 采购发票」 | 血缘路径 | 同上 |
| 「SELECT COUNT(*) FROM sales_order」 | SQL 片段 | 不当实体核——SQL 归 F3 语义证明器管，那里判「表存不存在」比字符串比对准 |
| 「各订单状态分别有多少条记录」「按客户汇总销售订单金额」 | **模型用自己的话转述任务** | 从句判别（疑问词 / 领起介词 / 「分别」 / >24 字）→ 不是实体名 |
| 「销售订单一共有多少条记录」、数值 `50个` | 逐字复述用户问句 | 复述豁免：出现在**用户说过的话**里的字眼与数字不是模型编的 |

**守卫没有放宽**，四条反向测试钉住：
① 复合串里只要有一段不可证，照旧拒答；
② 短片段（<6 字符）不吃复述豁免；
③ 问句里没有的数字照样要 run_sql 凭证；
④ 只豁免**用户**说过的话——模型自己上一轮编的实体，这一轮复读照样拦。
从句判别刻意留窄（只用最强语法信号），**宁可漏豁免多拒一次，也不误豁免放过一次幻觉**。

复述豁免覆盖**多轮**（`_echo_corpus` = 本轮问句 + 全部历史用户消息）：第 6 轮复述第 5 轮的
问句同样不是编造，只看本轮会漏——长会话越往后越容易被误拒，实测里就漏了一次。

**真实会话验证**（同一 6 轮取数序列，逐版重跑）：

| 版本 | 拒答 |
| --- | --- |
| 修复前 | 2/6 |
| v1 拆复合串 + 逐字复述豁免 | 1/6 |
| v2 复述豁免覆盖多轮 | 2/6（转述够不着子串，暴露根因） |
| **v3 从句判别** | **0/6，且答案都是实的**（145/264/1550/612/411/449 字符） |

+11 例回归，全部取自真实拒答原文。全库 **1184 passed**。

**未修、记录待评估**：模型会把**系统自己的报错文案**引回正文再被当实体核，实测原文
「按字段『account』（语义类型 textual）分组通常是口径错误，拒绝执行」——那是语义证明器的
拒绝消息。修法应在账本侧（工具错误文案入 `add_context_name`），不在校验器，属另一处改动。
另有 「生成」 这类**光杆动词**加强调，从句判别够不着（长度 2、无疑问词），暂留。

### P1 详细任务

- [x] T2.1（history 半边）按实测调 `agent_history_char_budget`：**实测后决定维持 6000**——
  ERP 域 12 轮长会话实测 history 峰值仅占 ctx/call 的 13.7%，调它最多省一成三，却要拿 T3 的口径延续性去换；
  上下文大头是常驻域语义卡/阶梯（见 P0.7 §T2.1 结论）。golden 不变、全库全绿。
- [x] T2.1（sample_rows 半边）`agent_result_sample_rows` **实测后决定维持 5**——F3 已于 2026-08-07 解开（ERP 域接通 3308 库、生成 `mapping_json`、修 `_apply_mapping`/Decimal 序列化，见 P0.8）。
  同一 6 问序列跑 5 vs 20：20 虽消掉 `read_result` 往返（离场省 2449 字符），但 `avg_llm_calls` 6.2→8.0、`avg_steps` 7.0→9.5、拒答率 0%→33.3%，触碰「llm_calls 不回涨」护栏，故**按实测维持 5**（见 P0.9）。
- [x] T3.1 `agent_compaction.py` 抽取摘要时显式识别并**完整保留**旧轮的 ```sql 围栏块（只留 SELECT/WITH、去重、保最后 3 条），附在摘要末尾不进首句截断；`chat_bi` 把保留 SQL 的表/列标识符入 FactLedger（防误拒答）；+3 单测
- [x] T3.2 golden 加长会话用例：摘要后「延续上一轮口径继续下钻」不重算、不误拒（`test_t3_2_caliber_continuity.py` 3 例）：长会话（history 7528 字符 > 6000）进真实 ask → compaction 触发 → GMV 口径 SQL 完整保留进摘要并入模型 system 上下文 → 延续轮复用口径作答而非重推/误拒

### P2 详细任务

- [x] T4.1 `_ObjectSnapshot` + `_ReferenceResolver` + `_loads_payload` 拆到 `chat_bi_references.py`（新，168 行）。**前引用未成为问题**：这三个符号只依赖 ORM 模型（`Property`/`RelationType`/`BusinessLogic`），不引用 `ChatBiService`，故无需 forward-ref 字符串、也无循环 import
- [x] T4.2 `chat_bi.py` 全量 re-export（比照 V4 O5 拆工具 schema 的做法），`chat_bi._ObjectSnapshot`/`_ReferenceResolver`/`_loads_payload` 的**对象 identity 不变**（`is` 断言钉住）；顺带清掉已不再使用的 `dataclass` import
- [x] T4.3 行为零变化：全套件 **1154 passed, 1 skipped**（= 基线 1149 + 新增 5 例），无用例改动。新增 `tests/test_chat_bi_references.py`（5 例）——拆出来的收益就是这层能脱离推理循环单测：re-export identity、伪 id 按 display_name 校回、校不回的引用被丢弃、caliber 按 kind 分派、`_loads_payload` 容错

### P3 详细任务

- [x] T5.1 `query_scout_agent` 增多步预算：`MAX_STEPS` 5 → **8**。5 只够走一趟直线
  （定位 1 + join 1 + profile 1 + 起草复查 1 + 收尾 1 正好用光），中间对不上一次就没余量改写重查，
  被强制收尾交回**没验完的草稿**——而那恰恰是最贵的产物：看着能跑，主 agent 一执行才发现口径错了。
  系统提示同步改成显式五步链（定位→取样→起草→校验改写→收尾），并写死「没 profile 过的取值不许进 SQL，
  探不出就交白卷并说明卡在哪」。仍**不含 run_sql**，仍封顶（超出强制收尾），子 agent 的调用仍单独计入
  `subagent_llm_calls`，不污染主 `avg_llm_calls`。
- [x] T5.2 scout 返回结构不变：`to_dict()` 仍是 `candidate_sql/brief/objects/logics/note` 五个键，
  主 agent 执行链路一行未动。+3 单测（6 步链跑得完 / 预算仍封顶 / 返回结构不变）
- [ ] T6.1 写 trace→golden 转换器 —— **卡在一处设计缺口，需先定方案，见下**
- [ ] T6.2 用转换出的用例扩 golden，跑通 CI 确定性

> **T6 的缺口（动手前必须先定）**：golden 用例需要**有序的工具调用序列 + 每次调用的参数 + 收尾文本**
> （`ToolTurn([(name, args)…]) + FinalTurn(text)`），而当前 `write_trace` 只落**工具名的计数**
> （`tools: {"search_objects": 10}`），顺序和参数都没有——直接转不出用例。两处要定：
> 1. **序列从哪来**：扩 `write_trace` 落有序调用（参数需脱敏），还是改从 `chat_bi_messages.payload.steps`
>    读（那里已有 index/tool/arguments/status，但那是库不是轨迹，与「trace 回放」的说法不一致）。
> 2. **实体怎么落地**：真实域的对象 id 在 golden 的种子域里不存在，照搬转出来的用例只会全部拒答。
>    要么转换器把实体映射到种子域的 `@order`/`@customer` 别名（有损，且需人工确认映射），
>    要么连同实体快照一起转、另起一套「按轨迹重建种子」的回放套件（更真，但工作量大得多）。
>
> 这两个选择会改变 T6 的形态和工作量，故不擅自定；其余 P3 内容（T5）已交付。

### 验收护栏（每阶段必过）

1. golden 20 例断言行为不变（工具序列、拒答、run_sql 三态、拒绝码）。
2. `avg_llm_calls` 不回涨（护住 2.6）；全套件保持全绿（当前 1184 passed）。
3. 调参/延伸看**实测**指标：`context_chars_per_call`、`offloaded_chars`、`skill_misroute_rate`、`subagent_isolated_chars`。
4. 不碰守卫：只读 SQL + RBAC + 治理闸门不动；`ask_stream` done 契约不变；scout 仍不执行 SQL。

---

## 4. 风险与对策

| 风险 | 对策 |
| --- | --- |
| trace 采样含敏感值（取数结果/域名） | trace 默认关；转 golden 前**强制脱敏**（去 rows 真实值、替换域名/表名为占位） |
| T2 调小预算 → 丢上文/丢列 → 误拒答或答不全 | 金标准是 golden 不变 + `avg_llm_calls` 不涨；调参走灰度、可一键回初值 |
| T3 摘要保 SQL 引入实体 → FactLedger 漏登记 → 误拒答 | 保留段里的表/列名同步 `ledger.add_context_name`（沿用 V4 O1 不变式） |
| ~~T4 拆模块触发循环 import~~ | **已消解**：`_ObjectSnapshot` 一并搬走后，references 模块只依赖 `app.models`，不反向引用 `ChatBiService`，forward-ref 未用上 |
| T5 scout 多步 → LLM 调用涨 | 子 agent 调用**单独计**（`subagent_llm_calls`），不污染主 `avg_llm_calls`；设步数预算封顶 |

---

## 5. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| （本次） | P0 | 交付 T1：`write_trace` 扩离场/子agent 字段 + `summarize_agent_traces.py` 汇总脚本（+7 单测）；端到端验证基线 `avg_llm_calls=2.6`；全库 1138 passed |
| （本次） | P0.5 | 真实会话实测采 14 条（ERP 域）：验证 O4 隔离比 995×；抢出 F1/F2/F3 三个真问题 |
| （本次） | P1（部分） | 交付 T3：compaction 完整保留旧轮关键 SQL（不截断）+ 其表/列标识符入 FactLedger（+3 单测）；T2 待长会话 trace |
| （本次） | T3.2 | 交付合成长会话 golden（`test_t3_2_caliber_continuity.py` 3 例）：真实 ask 中 compaction 触发后口径 SQL 完整保留入模型上下文、延续轮复用不重推/不误拒；全库 1149 passed |
| （本次） | P0.6 | 长会话实测（10 轮/history 10746 字符）：**首次真实会话观到 O1 触发**（第 8-10 轮各摘 2 轮，抑住上下文爆涨）；F1 修后 misroute 100%→10%；T3 在真实化 compaction 流程验证保住口径 SQL |
| （本次） | F1/F2 | 处理实测抢出的两个真问题：misroute 度量分出“路对但无实体”（F1）；结构性/取数首轮拒答逆一次“先 search 再判”（F2）；+5 测；全库 1146 passed |
| （本次） | P1 收官 | T3/T3.2 已交付；T2 延后至可查数据域（已标注）；关回 `AGENT_TRACE_ENABLED`（观测非常驻）；干净重启后端。全库 1149 passed |
| （本次） | P2 | 交付 T4：`chat_bi_references.py`（`_ObjectSnapshot`/`_ReferenceResolver`/`_loads_payload`，168 行）从 `chat_bi.py` 拆出并 re-export，identity 与 import 契约不变；`chat_bi.py` 4364 → 4229 行。零行为变化，全库 **1154 passed**（基线 1149 + 新增 5 例） |
| 2026-08-07 | P0.7 / F4 | ERP 域复采撞出 F4（自信作答被判未接地 → 好答案被换成拒答）：逆搜守卫从「像拒答才逆」放宽到「本轮零工具就逆」，守卫本身不放宽；+2 测，全库 **1156 passed**。真实会话验证：修前第 2/3 轮连拒，修后 1–3 轮全接地 |
| 2026-08-07 | P1 T2.1 | 12 轮长会话实测（`scripts/drive_long_session.py` 固化驱动）：history 峰值仅占 ctx/call 的 13.7% → **`agent_history_char_budget` 维持 6000**；`agent_result_sample_rows` 仍卡 F3（有数据的库上没有已发布本体） |
| 2026-08-07 | P3 T5 | scout `MAX_STEPS` 5 → 8 + 系统提示改成显式五步链（定位→取样→起草→校验改写→收尾），返回结构不变；+3 单测，全库 **1159 passed** |
| 2026-08-07 | P0.8 / F3 | **F3 解除**：ERP 域真正可查——数据源改指 3308（ERPNext MariaDB，739 表/`tabGL Entry` 43.8 万行）；改 `_apply_mapping` 为表位置才替换（避免列名误替，+7 单测）；由 724 对象 `source_ref` 机械生成 `mapping_json`；修执行器边界 Decimal/date/bytes 序列化（+2 单测）。端到端：`SELECT COUNT(*) FROM sales_order` → 73,912 条 |
| 2026-08-07 | P0.9 / T2.1 | `agent_result_sample_rows` 实测 5 vs 20：20 虽零离场往返但 `avg_llm_calls` 6.2→8.0、拒答率 0%→33.3%，触碰护栏→**维持 5**（算术预测被实测推翻） |
| 2026-08-07 | P1.0 / F5 | 校验器抽取精度：拆复合串逐段核 + 多轮复述豁免 + 从句判别（疑问词/领起介词/>24 字→非实体名），SQL 片段不当实体核；真实会话拒答 2/6→0/6且答案皆实；+11 例回归，全库 **1184 passed** |

---

## 6. 收官：V5 P0–P3 基本交付（仅 T6 待定）

V5 承接 V4，把「收益从单测推断变生产实测」落地，并修掉实测抢出的真问题：

| 项 | 交付物 | 状态 |
| --- | --- | --- |
| T1 trace 实测 + 看板 | `agent_trace` 字段扩 + `summarize_agent_traces.py` | ✅ |
| T3 compaction 保关键 SQL | `agent_compaction` key_sql 保留 + 入账 | ✅（单测 + 真实化验证） |
| T3.2 长会话 golden | `test_t3_2_caliber_continuity.py` | ✅（真实 ask 端到端） |
| F1 misroute 度量修正 | `skill_no_entity` 态 | ✅（实测 100%→10%） |
| F2 首轮逆搜守卫 | `_looks_like_refusal` + nudge | ✅（集成测） |
| T4 收尾拆模块 | `chat_bi_references.py` + `test_chat_bi_references.py` | ✅（identity 不变、零行为变化） |
| F4 零工具作答被误拒 | 逆搜守卫覆盖「自信作答」那半 | ✅（实测复现 + 修后真实会话验证） |
| T2 history 预算 | 实测后**维持 6000**（history 仅占上下文 13.7%） | ✅（12 轮长会话实测） |
| T5 scout 多步链 | `MAX_STEPS`=8 + 五步链提示，返回结构不变 | ✅（+3 单测） |
| F3 ERP 域可查 | 数据源改指 3308 + `_apply_mapping` 修正 + `mapping_json` 生成 + 执行器边界序列化 | ✅（端到端 73,912 条） |
| T2 sample_rows | 实测 5 vs 20 后**维持 5**（20 触碰 llm_calls 回涨护栏） | ✅（P0.9 实测） |
| F5 校验器抽取精度 | 拆复合串/复述豁免/从句判别 + SQL 不当实体 | ✅（拒答 2/6→0/6，+11 例） |
| T6 trace 回放 eval | — | ⏳ 有设计缺口待定（轨迹缺有序调用序列 + 实体落地方案） |

**真实会话实测硬数据**（P0.5/P0.6）：O4 隔离比 928–995×；O1 在 10 轮长会话第 8 轮触发、抑住上下文爆涨；
基线 `avg_llm_calls=2.6` 未回涨。全程不碰守卫，全库 **1184 passed**。

**重新采样方法（已固化）**：`backend/.env` 设 `AGENT_TRACE_ENABLED=true` → 清端口重启
（`lsof -tiTCP:8000 | xargs kill -9`）→ `python scripts/drive_long_session.py --domain-id <id>`
→ `python scripts/summarize_agent_traces.py` → **采完把 `AGENT_TRACE_ENABLED` 关回**（观测非常驻）。

**遗留（待方案）**：仅剩 **T6 trace 回放 eval** 未开工（有设计缺口：轨迹缺有序调用序列 + 实体落地方案，见 P3 详细任务）。
另有若干「记录待评估」观察未改：英文标识符提问召回弱（P0.7）、系统报错文案被引回当实体核（P1.0，宜在账本侧改）、「生成」光杆动词加强调（P1.0 暂留）。
