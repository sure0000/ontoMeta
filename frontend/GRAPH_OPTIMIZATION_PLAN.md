# 业务图渲染性能优化方案

## 问题诊断

### 当前性能瓶颈

1. **G6 v5.1 架构限制**
   - 增量渲染会卡死管线（代码注释已确认）
   - 被迫采用"销毁+重建"策略
   - 概览模式每次展开/收起都要重建整个 Graph 实例

2. **代码复杂度**
   - 图组件总计 1726 行代码
   - 详情/概览两种模式逻辑混杂在同一组件
   - 语义缩放（LoD）逻辑与重建机制耦合

3. **数据规模**
   - ERP 域：734 张表
   - 大型业务模块：30-40+ 节点
   - 概览模式最多同时展开 12 个聚类

### 性能关键参数

```typescript
// 当前配置
const DETAIL_FORCE_NODE_THRESHOLD = 12;  // 详情图切换力导向的阈值
const LOD_OPEN_ZOOM = 0.42;              // 语义缩放展开阈值
const LOD_MAX_OPEN_CLUSTERS = 12;        // 同时展开的版块上限
const LOD_DEBOUNCE_MS = 200;             // 缩放防抖延迟
const OVERVIEW_MEMBER_CAP = 24;          // 单个版块最多展示成员数
```

## 优化方案对比

### 方案 1：升级 G6 到 v6.x（推荐）⭐⭐⭐⭐⭐

**描述**：G6 在 2026-05-08 发布了 v6.0 版本，可能修复了 v5.1 的管线问题。

**实施步骤**：
1. 升级依赖：`@antv/g6: ^5.1.1` → `^6.0.0`
2. 查看 [迁移指南](https://g6.antv.antgroup.com/manual/upgrade)
3. 测试增量渲染是否稳定（`addNodeData/removeNodeData` + `draw()`）
4. 如果增量渲染可用，重构 LoD 逻辑，移除"整体重建"机制

**预期收益**：
- 消除"销毁+重建"开销，性能提升 3-5 倍
- 可恢复动画（用户体验更平滑）
- 代码简化约 20%

**风险评估**：
- 🟡 中等风险：可能有 breaking changes
- 🟢 可回滚：保留 v5 分支

**工作量**：2-3 天

---

### 方案 2：切换到高性能图库

#### 2A. Cosmograph（适合大规模图）

**描述**：WebGL 驱动的图库，专为大规模图（1000+ 节点）优化。

**npm**：`@cosmograph/cosmograph`

**特点**：
- ✅ 性能极致（WebGL 渲染）
- ✅ 力导向布局高度优化
- ✅ 支持百万级边的渲染
- ❌ API 较简单，需重写交互逻辑
- ❌ 自定义节点样式受限

**适用场景**：
- 全域概览（734 张表全图）
- 大型业务模块（100+ 节点）

**工作量**：5-7 天（需重写大部分交互）

#### 2B. Graphmother（WebGPU 前沿方案）

**描述**：基于 WebGPU 的下一代图库。

**npm**：`@graphmother/core`

**特点**：
- ✅ WebGPU 性能最优
- ❌ 生态不成熟（2026-08 发布）
- ❌ 浏览器兼容性问题（需 WebGPU 支持）

**结论**：⛔ 暂不推荐（太新，风险高）

#### 2C. React Flow（如果图是 DAG）

**描述**：React 原生的流程图库，专为 DAG 和流程图优化。

**npm**：`reactflow` 或 `@xyflow/react`

**特点**：
- ✅ React 原生，API 友好
- ✅ 层级布局性能优秀
- ✅ 丰富的交互能力
- ❌ **不适合网状关系图**（ERP 血缘是环状，见 memory）

**结论**：⛔ 不适合本项目（数据结构不匹配）

---

### 方案 3：当前架构优化（不换库）

保持 G6 v5.1，通过工程优化提升性能。

#### 3.1 调整 LoD 参数

```typescript
// 优化后的参数
const LOD_OPEN_ZOOM = 0.42;          // 保持不变
const LOD_MAX_OPEN_CLUSTERS = 8;     // 12 → 8，减少同时展开数
const LOD_DEBOUNCE_MS = 400;         // 200 → 400ms，减少重建频率
const OVERVIEW_MEMBER_CAP = 20;      // 24 → 20，单个版块节点数下限
```

**预期收益**：性能提升 20-30%

**工作量**：0.5 天

#### 3.2 拆分详情/概览组件

将 `OntologyGraphView` 拆成两个独立组件：
- `OntologyDetailGraph.tsx`（详情模式）
- `OntologyOverviewGraph.tsx`（概览模式）

**收益**：
- 代码复杂度降低
- 避免模式切换时的逻辑冲突
- 便于独立优化

**工作量**：2 天

#### 3.3 Web Worker 计算布局

将力导向布局计算移到 Worker：

```typescript
// 主线程
const worker = new Worker('./layout-worker.ts');
worker.postMessage({ nodes, edges });
worker.onmessage = (e) => {
  const { positions } = e.data;
  g.updateNodePositions(positions);
};
```

**收益**：
- 主线程不阻塞，UI 流畅
- 大图布局计算不卡顿

**工作量**：3 天

#### 3.4 Canvas 分层渲染

```typescript
// 背景层：静态边
const bgCanvas = document.createElement('canvas');
const bgCtx = bgCanvas.getContext('2d');
renderEdges(bgCtx, edges); // 一次性绘制，不重绘

// 前景层：动态节点
const fgCanvas = document.createElement('canvas');
// 只重绘节点和交互高亮
```

**收益**：
- 减少全局重绘
- hover/focus 时只重绘前景层

**工作量**：3-4 天

---

## 推荐方案

### 短期方案（1 周内）

**组合：方案 1 + 方案 3.1**

1. 尝试升级 G6 到 v6.0（2-3 天）
   - 如果增量渲染稳定 → 重构 LoD，移除重建机制
   - 如果仍有问题 → 回滚到 v5.1

2. 无论升级是否成功，都调整 LoD 参数（0.5 天）
   - 立即见效，风险极低

**预期收益**：
- 最佳情况：性能提升 3-5 倍（v6 稳定）
- 最差情况：性能提升 20-30%（仅参数优化）

### 中期方案（2-3 周）

如果 G6 v6 仍不理想，考虑：

**方案 2A（Cosmograph）+ 方案 3.2（组件拆分）**

- 概览模式切换到 Cosmograph（大规模性能优先）
- 详情模式保留 G6（交互丰富度优先）
- 两个组件独立实现，互不干扰

**工作量**：7-10 天

---

## 性能测试计划

### 测试场景

1. **小图**（10-20 节点）
   - 邻域图：对象详情页
   - 预期：<100ms 首次渲染

2. **中图**（30-50 节点）
   - 业务模块关系图
   - 预期：<300ms 首次渲染

3. **大图**（100-200 节点）
   - 域概览展开多个版块
   - 预期：<800ms 首次渲染

4. **极端场景**（500+ 节点）
   - 全域概览
   - 预期：<2s 首次渲染

### 测试指标

- **FCP**（First Contentful Paint）
- **Interaction Ready**：可交互时间
- **LoD 响应时间**：缩放后重建延迟
- **内存占用**：长时间使用后的内存

---

## 实施建议

### 立即执行（本周）

1. 调整 LoD 参数（方案 3.1）
   - 风险低，收益确定
   - 可立即上线

2. 调研 G6 v6 迁移指南
   - 评估 breaking changes
   - 准备测试环境

### 下周执行

3. 在测试分支升级 G6 到 v6
   - 重点测试增量渲染稳定性
   - 验证动画是否可恢复

4. 根据升级结果决定：
   - ✅ v6 稳定 → 重构 LoD，移除重建
   - ❌ v6 仍有问题 → 评估 Cosmograph

---

## 参考资料

- [G6 官网](https://g6.antv.antgroup.com/)
- [Cosmograph](https://cosmograph.app/)
- [WebGL vs Canvas 性能对比](https://github.com/anvaka/graph-drawing-libraries)
- [大规模图可视化最佳实践](https://observablehq.com/@d3/force-directed-graph)

---

**最后更新**：2026-09-03
**作者**：Claude (Kiro)
