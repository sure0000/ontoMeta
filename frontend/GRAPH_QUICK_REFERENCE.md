# 图组件快速参考

## 何时使用哪个组件？

```
┌─────────────────────────────────────────────────────────────┐
│  节点数量          推荐组件              性能              │
├─────────────────────────────────────────────────────────────┤
│  < 10 节点     OntologyDetailGraph    ★★★★☆  交互丰富    │
│  10-50 节点    OntologyDetailGraph    ★★★★☆  交互丰富    │
│  50-100 节点   OntologyDetailGraph    ★★★☆☆  可接受      │
│  100+ 节点     OntologyOverviewGraph  ★★★★★  极致        │
│  需要切换      OntologyGraphSwitcher  ★★★★☆  自动选择    │
└─────────────────────────────────────────────────────────────┘
```

## 三个组件对比

| 特性 | DetailGraph | OverviewGraph | Switcher |
|-----|-------------|---------------|----------|
| 引擎 | G6 (Canvas) | Cosmograph (WebGL) | 混合 |
| 节点上限 | 50 | 无限制 | 自动 |
| 自定义节点 | ✅ | ❌ (仅圆形) | ✅/❌ |
| 邻域聚焦 | ✅ | ❌ | ✅/❌ |
| 点击边 | ✅ | ❌ | ✅/❌ |
| 双击展开 | ✅ | ✅ (下钻) | ✅ |
| 性能 | 中 | 极致 | 自动 |
| 使用场景 | 邻域图 | 概览图 | 通用 |

## 代码示例

### 详情模式（单模式页面）

```typescript
import { OntologyDetailGraph } from "@/components/graph";

<OntologyDetailGraph
  graph={graph}
  height={420}
  objectDetailPath={(id) => `/objects/${id}`}
  relationDetailPath={(id) => `/relations/${id}`}
  onExpandNode={(id) => handleExpand(id)}
  embedded
/>
```

### 概览模式（单模式页面）

```typescript
import { OntologyOverviewGraph } from "@/components/graph";

<OntologyOverviewGraph
  graph={groupedGraph}
  height={600}
  objectDetailPath={(id) => `/objects/${id}`}
  onClusterDrillIn={(id) => openMatrix(id)}
  loading={loading}
  embedded
/>
```

### 自动切换（需要两种模式）

```typescript
import { OntologyGraphSwitcher } from "@/components/graph";
import { useState } from "react";

const [mode, setMode] = useState<"detail" | "overview">("detail");

<OntologyGraphSwitcher
  graph={detailGraph}
  groupedGraph={overviewGraph}
  graphMode={mode}
  onGraphModeChange={setMode}
  height={500}
  objectDetailPath={(id) => `/objects/${id}`}
  relationDetailPath={(id) => `/relations/${id}`}
  onClusterDrillIn={(id) => openMatrix(id)}
/>
```

## 常用 Props

### 共同 Props

| Prop | 类型 | 说明 |
|------|------|------|
| `height` | `number?` | 固定高度（px），省略则填满父容器 |
| `objectDetailPath` | `(id: string) => string` | 对象详情路由生成器 |
| `hint` | `ReactNode?` | 工具栏左侧提示文本 |
| `embedded` | `boolean?` | 嵌入模式，去除外层边框 |

### DetailGraph 独有

| Prop | 类型 | 说明 |
|------|------|------|
| `graph` | `OntologyGraph` | 详情图数据（节点+边） |
| `centerNodeId` | `string?` | 中心节点 ID（高亮） |
| `relationDetailPath` | `(id: string) => string` | 关系详情路由 |
| `onEdgeClick` | `(edge) => void` | 点击边回调 |
| `onExpandNode` | `(id: string) => void` | 双击展开邻域 |
| `expanding` | `boolean?` | 展开中状态 |

### OverviewGraph 独有

| Prop | 类型 | 说明 |
|------|------|------|
| `graph` | `OntologyGroupedGraph` | 概览图数据（聚类+枢纽） |
| `onClusterDrillIn` | `(id: string) => void` | 双击聚类下钻 |
| `loading` | `boolean?` | 加载中状态 |

### Switcher 独有

| Prop | 类型 | 说明 |
|------|------|------|
| `graph` | `OntologyGraph` | 详情图数据 |
| `groupedGraph` | `OntologyGroupedGraph?` | 概览图数据 |
| `graphMode` | `"detail" \| "overview"` | 当前模式 |
| `onGraphModeChange` | `(mode) => void` | 模式切换回调 |

## 迁移步骤

### 从 OntologyGraphView 迁移到单组件

```diff
- import { OntologyGraphView } from "@/components/graph";
+ import { OntologyDetailGraph } from "@/components/graph";

- <OntologyGraphView
+ <OntologyDetailGraph
    graph={graph}
-   graphMode="detail"
-   groupedGraph={null}
    {...props}
  />
```

### 从 OntologyGraphView 迁移到 Switcher

```diff
- import { OntologyGraphView } from "@/components/graph";
+ import { OntologyGraphSwitcher } from "@/components/graph";

- <OntologyGraphView
+ <OntologyGraphSwitcher
    graph={graph}
    groupedGraph={groupedGraph}
    graphMode={mode}
    onGraphModeChange={setMode}
    {...props}
  />
```

无需修改其他代码，API 完全兼容！

## 性能调优

### 详情模式

```typescript
// 节点数 > 12 时自动切换到力导向布局
// 节点数 ≤ 12 时使用层级布局（dagre）

// 最小可读缩放：0.8
// 边标签隐藏阈值：24 条边
```

### 概览模式

```typescript
// 节点大小 = √(成员数) × 8
// 边宽度 = 1 + 权重 × 0.05
// 只渲染业务模块（过滤系统/技术/待归类）
// 坐标间距 = 300px
```

## 故障排查

### Cosmograph 不渲染

```typescript
// 检查 WebGL 支持
const canvas = document.createElement('canvas');
const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
if (!gl) {
  console.error('WebGL not supported');
}
```

### 图显示空白

```typescript
// 检查数据格式
console.log('nodes:', graph.nodes.length);
console.log('edges:', graph.edges.length);

// 检查容器高度
console.log('container height:', containerRef.current?.offsetHeight);
```

### 交互无响应

```typescript
// 检查事件处理器
<OntologyDetailGraph
  objectDetailPath={(id) => {
    console.log('clicked node:', id);
    return `/objects/${id}`;
  }}
/>
```

## 性能基准

```
测试环境：MacBook Pro M1, Chrome 120

┌────────────────────┬───────────┬───────────┬──────────┐
│ 场景               │ 旧组件    │ 新组件    │ 提升     │
├────────────────────┼───────────┼───────────┼──────────┤
│ 10 节点详情        │ ~80ms     │ ~80ms     │ 持平     │
│ 50 节点详情        │ ~250ms    │ ~240ms    │ 1.04x    │
│ 100 节点概览       │ ~800ms    │ ~150ms    │ 5.3x     │
│ 500 节点概览       │ ~3200ms   │ ~400ms    │ 8.0x     │
│ 展开/收起聚类      │ ~500ms    │ N/A       │ 不支持   │
│ 滚轮缩放延迟       │ 200ms     │ 0ms       │ ∞        │
└────────────────────┴───────────┴───────────┴──────────┘

注：概览模式不支持语义缩放（展开/收起），但全量渲染性能足够。
```

## 相关文档

- **完整优化方案**：`GRAPH_OPTIMIZATION_PLAN.md`
- **迁移指南**：`GRAPH_MIGRATION_GUIDE.md`
- **实施总结**：`GRAPH_OPTIMIZATION_SUMMARY.md`
- **测试页面**：`src/pages/GraphTestPage.tsx`
- **迁移示例**：`src/pages/SegmentDetailPage.example.tsx`

---

**提示**：遇到问题先查 `GRAPH_MIGRATION_GUIDE.md` 的常见问题部分。
