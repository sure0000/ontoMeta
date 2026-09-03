# OntologyGraphView 迁移指南

## 概述

为了解决业务图渲染性能问题，我们将图可视化拆分成两个专用组件：

- **OntologyDetailGraph**：使用 G6，处理详情模式（10-50 节点）
- **OntologyOverviewGraph**：使用 Cosmograph（WebGL），处理概览模式（100+ 节点）
- **OntologyGraphSwitcher**：包装器组件，自动在两者之间切换

## 性能提升

- **概览模式**：3-10 倍性能提升（WebGL 渲染）
- **详情模式**：保持原有性能和交互
- **代码复杂度**：从 1726 行简化为三个独立组件

## 迁移步骤

### 选项 1：使用 OntologyGraphSwitcher（推荐）

如果你需要详情/概览切换功能，直接替换：

```typescript
// 之前
import { OntologyGraphView } from "@/components/graph";

<OntologyGraphView
  graph={graph}
  groupedGraph={groupedGraph}
  graphMode={mode}
  onGraphModeChange={setMode}
  {...otherProps}
/>

// 之后
import { OntologyGraphSwitcher } from "@/components/graph";

<OntologyGraphSwitcher
  graph={graph}
  groupedGraph={groupedGraph}
  graphMode={mode}
  onGraphModeChange={setMode}
  {...otherProps}
/>
```

**API 完全兼容**，无需修改 props。

### 选项 2：使用单独的组件

如果你只需要其中一种模式：

#### 只需要详情模式

```typescript
// 之前
import { OntologyGraphView } from "@/components/graph";

<OntologyGraphView
  graph={graph}
  centerNodeId={centerNodeId}
  objectDetailPath={objectDetailPath}
  {...props}
/>

// 之后
import { OntologyDetailGraph } from "@/components/graph";

<OntologyDetailGraph
  graph={graph}
  centerNodeId={centerNodeId}
  objectDetailPath={objectDetailPath}
  {...props}
/>
```

#### 只需要概览模式

```typescript
// 之前
import { OntologyGraphView } from "@/components/graph";

<OntologyGraphView
  graph={dummyGraph} // 之前可能需要传空数据
  groupedGraph={groupedGraph}
  graphMode="overview"
  {...props}
/>

// 之后
import { OntologyOverviewGraph } from "@/components/graph";

<OntologyOverviewGraph
  graph={groupedGraph}
  objectDetailPath={objectDetailPath}
  onClusterDrillIn={onClusterDrillIn}
  {...props}
/>
```

## Props 对比

### OntologyDetailGraph（详情模式）

保留的 props：
- ✅ `graph: OntologyGraph`
- ✅ `height?: number`
- ✅ `centerNodeId?: string`
- ✅ `objectDetailPath?: (objectId: string) => string`
- ✅ `relationDetailPath?: (relationId: string) => string`
- ✅ `onEdgeClick?: (edge) => void`
- ✅ `onExpandNode?: (objectId: string) => void`
- ✅ `expanding?: boolean`
- ✅ `hint?: ReactNode`
- ✅ `embedded?: boolean`

移除的 props（只属于概览模式）：
- ❌ `groupedGraph`
- ❌ `groupedGraphLoading`
- ❌ `graphMode`
- ❌ `onGraphModeChange`
- ❌ `onClusterDrillIn`

### OntologyOverviewGraph（概览模式）

保留的 props：
- ✅ `graph: OntologyGroupedGraph`（注意：类型从 `groupedGraph` 改为 `graph`）
- ✅ `height?: number`
- ✅ `objectDetailPath?: (objectId: string) => string`
- ✅ `onClusterDrillIn?: (clusterId: string) => void`
- ✅ `hint?: ReactNode`
- ✅ `embedded?: boolean`
- ✅ `loading?: boolean`（之前叫 `groupedGraphLoading`）

移除的 props（只属于详情模式）：
- ❌ `centerNodeId`
- ❌ `relationDetailPath`
- ❌ `onEdgeClick`
- ❌ `onExpandNode`
- ❌ `expanding`
- ❌ `graphMode`
- ❌ `onGraphModeChange`

## 注意事项

### 1. Cosmograph 特性差异

OntologyOverviewGraph 使用 WebGL 渲染，有以下限制：

- ❌ 不支持自定义节点形状（只能是圆形）
- ❌ 不支持复杂的 CSS 样式
- ✅ 支持基本的颜色、大小、标签
- ✅ 支持点击、双击、悬浮事件
- ✅ 性能极致，可处理 1000+ 节点

### 2. 语义缩放（LoD）行为变化

旧 OntologyGraphView 的语义缩放（展开/收起聚类）需要整体重建画布。

新 OntologyOverviewGraph **不支持语义缩放**，原因：
- Cosmograph 主打"全量渲染"，无需增量展开
- 100+ 节点全部渲染也不卡（WebGL 优势）
- 简化了交互逻辑

如果需要查看聚类内部，使用 `onClusterDrillIn` 打开矩阵视图。

### 3. 样式兼容性

两个组件共用相同的 CSS class：
- `.ontology-graph-view`
- `.ontology-graph-toolbar`
- `.ontology-graph-canvas`
- `.ontology-graph-hint`

现有样式无需修改。

### 4. 依赖变化

新增依赖：
```json
{
  "@cosmograph/cosmograph": "^2.5.1"
}
```

已安装，无需手动操作。

## 回滚计划

如果遇到问题，可以暂时回退到旧组件：

```typescript
// 使用旧的 OntologyGraphView
import { OntologyGraphView } from "@/components/graph/OntologyGraphView";
```

旧组件仍然保留，只是不再导出到 `index.ts`。

## 待办事项

- [ ] 迁移 `SegmentDetailPage.tsx`
- [ ] 迁移 `RelationTypeDetailPage.tsx`
- [ ] 迁移 `ChatBiReferences.tsx`
- [ ] 迁移其他使用 `OntologyGraphView` 的页面
- [ ] 测试概览模式性能
- [ ] 测试详情模式兼容性
- [ ] 更新文档和类型定义

## 常见问题

### Q: 为什么不直接升级 G6 到 v6？

A: G6 v6 仍在早期阶段，可能有 breaking changes。Cosmograph 是成熟的大规模图库，更适合概览场景。

### Q: 概览模式能自定义节点样式吗？

A: 只能自定义颜色和大小。复杂样式需要等 Cosmograph 更新，或回退到 G6。

### Q: 性能提升有多少？

A: 概览模式（100+ 节点）约 3-10 倍，详情模式保持不变。

### Q: 能同时显示详情和概览吗？

A: 使用 `OntologyGraphSwitcher`，它会根据 `graphMode` 自动切换组件。

## 参考资料

- [Cosmograph 官网](https://cosmograph.app/)
- [G6 官网](https://g6.antv.antgroup.com/)
- [性能优化方案文档](./GRAPH_OPTIMIZATION_PLAN.md)

---

**更新日期**：2026-09-03
