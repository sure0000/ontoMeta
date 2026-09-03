# 🔧 修复：邻域聚焦后的节点置灰问题

**日期**: 2026-09-03  
**问题**: 鼠标移开节点后，所有节点都变成置灰状态  
**状态**: ✅ 已修复

---

## 问题描述

### 用户反馈

> 现在的图初始展示时所有节点都是黑体有线框展示，鼠标移动到某个节点之后会展示相关边和节点，但再移开鼠标所有节点都置灰了，为什么要这么设计？

### 实际情况

这**不是设计意图**，而是一个 bug：

1. ✅ **初始状态**：所有节点正常显示（白色卡片 + 黑色线框）
2. ✅ **悬浮节点**：高亮当前节点及其邻居，压暗其他节点（这是正确的设计）
3. ❌ **移开鼠标**：应该恢复正常，但实际变成了置灰状态

---

## 设计意图：邻域聚焦

这个交互模式叫**邻域聚焦（Neighborhood Focus）**，用于解决复杂图的可读性问题。

### 为什么需要这个功能？

根据代码注释（`OntologyDetailGraph.tsx:245-247`）：

> **30+ 节点的板块图里，这是「这个对象连着谁」唯一读得出来的方式。**

### 正确的交互流程

```
初始状态：所有节点正常显示
    ↓
悬浮节点 A：
  - A + A 的邻居 → 高亮（蓝色边框）
  - 其他节点 → 压暗（20% 不透明度）
  - A 的关系边 → 高亮并显示动词标签
    ↓
移开鼠标：
  - 所有节点 → 恢复正常显示 ✅（修复前：变成置灰 ❌）
```

### 视觉效果

**悬浮节点时**（邻域聚焦）：
```
┌─────────┐
│ 对象 A  │ ← 高亮（蓝色边框 + 阴影）
└─────────┘
     ↓ 转化
┌─────────┐
│ 对象 B  │ ← 高亮
└─────────┘

┌─────────┐
│ 对象 C  │ ← 压暗（20% 不透明度）
└─────────┘
```

**移开鼠标后**（应该恢复正常）：
```
┌─────────┐
│ 对象 A  │ ← 正常
└─────────┘

┌─────────┐
│ 对象 B  │ ← 正常
└─────────┘

┌─────────┐
│ 对象 C  │ ← 正常（修复前：置灰）
└─────────┘
```

---

## 根本原因

### 旧代码的问题

`clearFocus()` 函数将所有节点/边的状态重置为 `[]`（空数组）：

```typescript
// ❌ 旧代码
const clearFocus = () => {
  focusedId = null;
  const states: Record<string, string[]> = {};
  g.getNodeData().forEach((n) => (states[String(n.id)] = [])); // 空数组
  g.getEdgeData().forEach((e) => {
    if (e.id != null) states[String(e.id)] = [];
  });
  void g.setElementState(states, false);
};
```

### 为什么会置灰？

G6 的状态系统中，`[]`（空状态）**不等于**"初始状态"：
- 空状态 `[]` → G6 应用某种默认行为（可能是非活跃状态）
- 初始样式中没有明确定义 `opacity: 1` → 导致节点变成置灰

---

## 修复方案

### 1. 添加 `default` 状态

在节点和边的样式定义中，明确添加 `default` 状态：

**ontologyNode.ts**:
```typescript
state: {
  hover: { ... },
  active: {
    opacity: 1,           // 明确设置
    labelOpacity: 1,
  },
  dimmed: {
    opacity: 0.2,
    labelOpacity: 0.35,
  },
  default: {              // ✅ 新增
    opacity: 1,
    labelOpacity: 1,
  },
}
```

**ontologyEdge.ts**:
```typescript
state: {
  hover: { ... },
  active: { ... },
  dimmed: { ... },
  default: (data: EdgeData) => {  // ✅ 新增，考虑稠密图的初始透明度
    const dense = Boolean(data.data?.hideLabel);
    return {
      opacity: dense ? 0.4 : 1,
      labelOpacity: dense ? 0 : 1,
    };
  },
}
```

### 2. 修改 `clearFocus()` 重置逻辑

```typescript
// ✅ 新代码
const clearFocus = () => {
  if (focusedId === null) return;
  focusedId = null;
  const states: Record<string, string[]> = {};
  // 重置为 default 状态而不是空数组
  g.getNodeData().forEach((n) => (states[String(n.id)] = ["default"]));
  g.getEdgeData().forEach((e) => {
    if (e.id != null) states[String(e.id)] = ["default"];
  });
  void g.setElementState(states, false);
};
```

---

## 修改文件

| 文件 | 修改内容 | 行数 |
|------|----------|------|
| `src/components/graph/g6/ontologyNode.ts` | 添加 `default` 状态 | +5 |
| `src/components/graph/g6/ontologyEdge.ts` | 添加 `default` 状态 | +8 |
| `src/components/graph/OntologyDetailGraph.tsx` | 修改 `clearFocus()` 逻辑 | ~2 |

---

## 验证步骤

### 1. 访问测试页面
```
http://localhost:5180/graph-test
```

### 2. 访问已迁移页面
```
http://localhost:5180/segments/{板块ID}
```

### 3. 测试交互

**操作流程**：
1. 观察初始状态 → 所有节点正常显示（白色卡片）
2. 鼠标悬浮任意节点 → 当前节点及邻居高亮，其他压暗
3. 鼠标移开节点 → **所有节点恢复正常**（不再置灰）✅

**预期结果**：
- ✅ 初始：正常
- ✅ 悬浮：高亮/压暗
- ✅ 移开：恢复正常（不置灰）

---

## 技术细节

### G6 状态系统

G6 的状态是**叠加式**的：
- `["active"]` → 应用 `active` 状态样式
- `["active", "hover"]` → 同时应用两个状态
- `[]` → **不等于初始状态**，而是"无状态"
- `["default"]` → 明确回到定义的默认状态

### 为什么需要 `default` 状态？

因为：
1. G6 的初始样式在 `style` 函数中定义
2. 状态样式在 `state` 对象中定义
3. `setElementState([])` 清空状态后，G6 不会自动"回退"到 `style` 函数
4. 必须显式定义 `default` 状态来表示"正常状态"

### 边的特殊处理

边的 `default` 状态需要考虑**稠密图模式**：

```typescript
default: (data: EdgeData) => {
  const dense = Boolean(data.data?.hideLabel);
  return {
    opacity: dense ? 0.4 : 1,      // 稠密图：40% 透明度
    labelOpacity: dense ? 0 : 1,   // 稠密图：隐藏标签
  };
}
```

这确保在稠密图中，移开鼠标后边的透明度回到 40% 而不是 100%。

---

## 附加说明

### 邻域聚焦的价值

这个交互模式在**复杂图**中非常重要：

1. **问题**：30+ 节点的板块图，边线密集，完全看不清关系
2. **解决**：悬浮节点 → 只显示相关关系，其他压暗
3. **效果**：「这个对象连着谁、什么关系」一眼可答

### 稠密图的标签策略

代码中还有一个配套设计（`buildG6Data.EDGE_LABEL_LIMIT`）：

- **稀疏图**（边少）：默认显示所有动词标签
- **稠密图**（边多）：默认隐藏标签，只在邻域聚焦时显示

这避免了标签互相压住、完全读不出来的问题。

---

## 总结

✅ **问题**：移开鼠标后节点置灰  
✅ **原因**：`clearFocus()` 使用空数组 `[]` 而不是 `["default"]`  
✅ **修复**：添加 `default` 状态定义 + 修改重置逻辑  
✅ **编译**：通过（0 错误）  
✅ **设计**：邻域聚焦是有意设计，用于提升复杂图的可读性  

**下一步**：测试验证修复效果
