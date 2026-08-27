# Data Agent V5 优化：工具收窄 + Prompt 增强 + ReAct 模式

**日期**: 2026-08-27  
**目标**: 解决 Agent 效果差、死板的问题  
**方案**: 优化现有架构（不换框架）

---

## 问题诊断

### 根本原因
1. **工具过载**: 38 个工具同时暴露给模型 → 选择困难（tool confusion）
2. **Skill 系统名存实亡**: "只解锁不收窄" → 选 skill 后工具更多（12 基础 + 6 额外）
3. **Prompt 过简**: 只有 5 句话，无工作流程指导，无 few-shot 示例
4. **完全依赖模型自觉**: 不主动路由 skill，模型要自己调 select_skill
5. **缺少思考过程**: 模型直接调用工具，无法追踪决策逻辑

### 为什么不换框架？
- **现有架构良好**: 状态机流水线、完整可观测性、接地验证、答案核验
- **框架带不来本质改进**: LangGraph 本质也是工具循环，问题在工具设计而非循环实现
- **迁移成本巨大**: 1847 行 tool_schemas + 6600+ 行 chat_bi + 配套基础设施

---

## 优化方案

### 1. 工具白名单 + 真收窄（立竿见影）

#### 前：只解锁不收窄
```python
# 旧逻辑
base_tools = 12 个  # 永远可用
skill.extra_tools = 6 个  # 叠加
→ 总计 18-20 个工具（未选 skill 时也是 12 个）
```

#### 后：每个 skill 独立白名单
```python
# 新逻辑
SKILL_TOOL_ALLOWLIST = {
    "overview": 8 个精选工具,
    "query": 17 个（最多，因为涉及取数+分析+可视化）,
    "lineage": 7 个,
    "create": 10 个,
    "task": 9 个,
    "onboard": 4 个,
}
DEFAULT_TOOL_ALLOWLIST = 9 个（未选 skill）
```

#### 效果
- 未选 skill: **38 → 9 个**工具
- 选 skill 后: **38 → 4-17 个**工具（根据场景）
- 减少工具选择困难，提升准确率

---

### 2. 增强 System Prompt

#### 前：5 句话
```
你是企业数据助手，请用中文简洁回答。
使用当前已发布本体和工具结果获取信息。
数据查询使用默认 Doris。元数据问题使用本体工具。
数据任务使用任务工具和确认表单。
请清楚标注建议、草稿、查询结果和任务状态。
```

#### 后：结构化工作流程
```
# 工作方式
1. 理解意图 → 2. 选择技能（重要）→ 3. 逐步执行 → 4. 基于事实

# 工具选择原则（6 种场景 × 工具链）
- 查数据 → select_skill('query') → search → run_sql → analyze
- 看结构 → select_skill('overview') → 使用语义卡 + search
- 看血缘 → select_skill('lineage') → search → get_lineage
- ...

# 重要约束
- 数据查询默认 Doris
- 任务需六环确认，不能跳过
- 同步不需要先物化
- 清楚标注状态
```

---

### 3. 增强 Skill Overlay

#### 前：每个 skill 2-3 句
```python
"【取数分析】先定位对象或业务口径。已有口径使用 compile_metric..."
```

#### 后：详细工作流程 + 示例
```python
"【取数分析模式】
当前任务：查询实际数据并分析。

标准工作流程：
1. **定位对象** - search_objects → get_object
2. **处理跨对象查询** - find_join_path / compile_metric
3. **处理筛选条件** - profile_values
4. **执行查询** - run_sql / scout_query / update_plan
5. **分析和呈现** - analyze_result / render_chart

示例：「本月销售额多少？」
→ search_objects('销售') → 找到「销售订单」
→ get_object(销售订单_id) → 看到 amount、order_date 字段
→ run_sql('SELECT SUM(amount) FROM sales_order...')
→ analyze_result() → 提炼洞察

注意事项：
- 不要编造对象名或字段名
- SQL 使用本体标识符（name），不是显示名
- 深加载的对象已包含字段、关系、取值样例
"
```

---

### 4. 自动 Skill 路由

#### 新增功能
```python
def _auto_select_skill(question: str) -> str | None:
    """基于关键词自动选择 Skill"""
    # 优先级：task > onboard > lineage > create > query > overview
    if "物化" in q or "同步" in q or "任务" in q:
        return "task"
    if "接入" in q or "连接" in q:
        return "onboard"
    if "血缘" in q or "上游" in q:
        return "lineage"
    # ...
    return None  # 未命中让模型选
```

#### 应用时机
在 agent 循环开始前自动预选，并发送 `skill_selected` 事件通知前端。

---

### 5. ReAct 模式（V5.1 新增）

#### 什么是 ReAct？
ReAct = **Reasoning and Acting**（思考再行动）。要求模型在调用工具前先输出思考过程：
- 为什么选这个工具
- 期望获得什么信息
- 当前目标是什么

#### 实现方式
1. **Prompt 引导**：要求模型在调用工具前先在 `<thinking>` 标签中说明理由
   ```
   <thinking>用户问销售额，需要先找到销售相关对象，用 search_objects</thinking>
   ```

2. **思考提取**：自动提取 `<thinking>` 标签内容，作为 `thought` 步骤展示给用户

3. **遥测统计**：记录思考次数和字符数，用于评估 ReAct 采用率

#### 代码实现
```python
# chat_bi.py
def _extract_thinking(text: str | None) -> tuple[str, str]:
    """提取 ReAct 思考内容。返回 (思考内容, 去除思考标签后的文本)"""
    thinking_match = re.search(r"<thinking>(.*?)</thinking>", text, flags=re.S | re.I)
    if thinking_match:
        thinking = thinking_match.group(1).strip()
        text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.S | re.I).strip()
        return thinking, text
    return "", text

# agent_telemetry.py
thinking_count: int = 0  # 有多少次调用前有 thinking
thinking_chars: int = 0  # 思考内容总字符数
```

#### 效果
- **提高准确性**：思考过程帮助模型做出更合理的工具选择
- **便于调试**：可以看到模型为什么选择某个工具，快速定位问题
- **增强可解释性**：用户能理解 Agent 的决策逻辑

---

## 验证结果

### 测试通过
```bash
======================== 84 passed, 1 warning in 5.83s =========================
```

### 自动分类准确率
```
✓ '本月销售额多少？' -> query
✓ '销售订单有哪些字段？' -> overview
✓ '建个复购率指标' -> create
✓ '同步 ERP 数据到 ODS' -> task
✓ '订单表的血缘关系' -> lineage
✓ '接入 MySQL 数据库' -> onboard
✓ '你好' -> None (一般问题不预选)
```

### 工具收窄效果
| 场景 | 优化前 | 优化后 | 减少比例 |
|------|--------|--------|----------|
| 未选 skill | 12 个 | 9 个 | -25% |
| overview | 18 个 | 8 个 | -56% |
| query | 18 个 | 17 个 | -6% (本就需要多工具) |
| lineage | 18 个 | 7 个 | -61% |
| create | 18 个 | 10 个 | -44% |
| task | 18 个 | 9 个 | -50% |
| onboard | 18 个 | 4 个 | -78% |

---

## 代码变更

### 修改的文件
1. `backend/app/services/chat_bi_tool_schemas.py`
   - 新增 `SKILL_TOOL_ALLOWLIST` 和 `DEFAULT_TOOL_ALLOWLIST`
   - 重写 `_tools_for_skill()` 实现真收窄
   - 增强 `_AGENT_SYSTEM_PROMPT`（V5.1：加入 ReAct 引导）

2. `backend/app/services/chat_bi_skills.py`
   - 增强每个 skill 的 `prompt_overlay`（加工作流程 + 示例）

3. `backend/app/services/chat_bi.py`
   - 新增 `_auto_select_skill()` 方法
   - 在 `_stream_agent_events()` 中应用自动路由
   - V5.1：新增 `_extract_thinking()` 方法提取思考内容
   - V5.1：在工具调用前后提取并记录思考过程

4. `backend/app/services/agent_telemetry.py`
   - V5.1：新增 `thinking_count` 和 `thinking_chars` 统计字段
   - V5.1：新增 `thinking()` 方法记录思考
   - V5.1：在 `snapshot()` 中导出思考统计

5. `backend/tests/test_chat_bi_skills.py`
   - 更新测试以匹配新的"真收窄"行为
   - V5.1：新增 `test_react_thinking_extraction()` 测试思考提取功能

### 行为变更
- **破坏性变更**: 从"只解锁不收窄"变为"真收窄"
- **影响**: 每个 skill 的可用工具集变小，模型选择更简单
- **测试覆盖**: 84 个测试全部更新并通过
- **V5.1 新增**: ReAct 模式，模型在调用工具前输出思考过程

---

## 下一步优化建议

### 短期（1-2 周）
1. **观测和迭代**
   - 加 telemetry 记录每次的 skill 选择 + 工具调用序列
   - 分析日志，识别高频失败模式
   - 调整工具白名单（可能某些工具需要加回默认集）
   - **V5.1**：监控 ReAct 思考内容的质量和采用率

2. **Few-shot 示例库**
   - 为每个 skill 收集 5-10 个典型问答
   - 动态注入最相似的 1-2 个示例到 prompt

### 中期（1 个月）
3. **ReAct 思维链优化**（V5.1 已实现基础版）
   - ✅ 要求模型在调用工具前先输出 `<thinking>` 标签
   - ✅ 帮助调试为什么选错工具
   - 🔄 进一步优化：根据思考内容质量调整 prompt

4. **工具描述优化**
   - 每个工具的 `description` 加"何时用、何时不用"
   - 减少语义相近工具的混淆（如 search_objects vs locate_entities）

### 长期（按需）
5. **考虑 LangGraph（如果仍不满意）**
   - 前提：上述优化都做了，仍然效果差
   - 优势：状态机可视化、checkpoint 调试、LangSmith 追踪
   - 成本：重写 1847 + 6600 行代码

---

## 总结

### 本次优化
- ✅ 工具从 38 个减少到 4-17 个（根据场景）
- ✅ Prompt 从 5 句话扩展到结构化工作流程
- ✅ 自动 Skill 路由，减少依赖模型自觉
- ✅ 每个 skill 有详细工作流程 + 示例
- ✅ 84 个测试全部通过
- ✅ **V5.1 新增**：ReAct 模式，思考再行动

### 核心理念
**架构优于框架，设计优于工具**。问题在于工具设计（38 个全暴露）和引导缺失（5 句 prompt），不在于没用 LangGraph。优化现有架构，成本更低、风险更小、效果更直接。

### 预期效果
- 工具选择准确率提升（减少混乱）
- 响应速度加快（更少工具 = 更小 prefill）
- 回答质量提升（有工作流程指导）
- 调试更容易（skill 选择可追踪）
- **V5.1**：决策过程可见（ReAct 思考内容）

### V5.1 ReAct 模式的价值
1. **提高准确性**：思考过程帮助模型选择正确的工具
2. **便于调试**：看到决策逻辑，快速定位问题
3. **增强可解释性**：用户理解 Agent 的工作方式
4. **遥测支持**：记录思考统计，量化 ReAct 效果
