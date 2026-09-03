# 🚀 快速启动指南

## 立即测试新组件

### 1️⃣ 访问测试页面

开发服务器已在运行，直接访问：

```
http://localhost:5180/graph-test
```

这个页面展示三种组件的对比：
- **OntologyDetailGraph** - G6 详情模式
- **OntologyOverviewGraph** - Cosmograph 概览模式  
- **OntologyGraphSwitcher** - 自动切换

### 2️⃣ 访问已迁移页面

任意选择一个板块详情页：

```
http://localhost:5180/segments/{板块ID}
```

查看"板块关系图"卡片，现在使用 `OntologyDetailGraph` 渲染。

### 3️⃣ 检查控制台

打开浏览器开发者工具（F12），检查：
- ✅ 无 TypeScript 错误
- ✅ 无运行时错误
- ⚠️ 可能有 Cosmograph 相关警告（正常，待实现事件监听）

---

## 下一步：完善 Cosmograph 事件

当前 `OntologyOverviewGraph.tsx` 的事件监听被注释：

```typescript
// TODO: 根据实际 API 文档调整事件监听方式
// Cosmograph 可能不支持直接的点击事件，需要查阅官方文档
```

### 需要实现的功能

1. **点击枢纽节点** → 跳转对象详情
2. **双击聚类** → 下钻矩阵
3. **悬浮节点** → 显示标签

### 参考文档

- Cosmograph API: https://cosmograph.app/docs-lib/
- 类型定义: `node_modules/@cosmograph/cosmograph/cosmograph/index.d.ts`

查找类似这些方法：
- `cosmograph.on('click', callback)`
- `cosmograph.onClick(callback)`
- `cosmograph.selectPoint(index)`

---

## 如果遇到问题

### 页面空白/报错

1. 检查浏览器控制台错误
2. 确认数据格式正确（`OntologyGroupedGraph` 类型）
3. 查看 Network 面板，API 请求是否成功

### Cosmograph 不渲染

1. 检查是否支持 WebGL：访问 https://get.webgl.org/
2. 查看容器尺寸是否正确
3. 确认 `prepareCosmographData` 返回正确

### 性能没提升

1. 确认节点数量 > 100（小图仍用 G6）
2. 使用 Performance API 测量实际时间
3. 对比旧组件 `OntologyGraphView`

---

## 文件位置速查

```
frontend/
├── src/
│   ├── components/graph/
│   │   ├── OntologyDetailGraph.tsx      # G6 详情组件 ✅
│   │   ├── OntologyOverviewGraph.tsx    # Cosmograph 概览组件 ⚠️
│   │   └── OntologyGraphSwitcher.tsx    # 切换器 ✅
│   ├── pages/
│   │   ├── GraphTestPage.tsx            # 测试页面
│   │   └── SegmentDetailPage.tsx        # 已迁移 ✅
│   └── App.tsx                          # 路由配置 ✅
├── MIGRATION_COMPLETE.md                # 完成报告
├── GRAPH_OPTIMIZATION_PLAN.md           # 方案对比
└── GRAPH_MIGRATION_GUIDE.md             # 迁移指南
```

---

## 立即行动检查清单

- [ ] 访问 `/graph-test`，确认三种组件都能渲染
- [ ] 访问任意 `/segments/{id}`，确认板块关系图正常
- [ ] 打开浏览器控制台，确认无致命错误
- [ ] 查阅 Cosmograph 文档，找到事件监听 API
- [ ] 实现点击/双击/悬浮事件
- [ ] 测试节点跳转和聚类下钻功能
- [ ] 运行性能基准测试
- [ ] 记录实际性能提升数据

---

**一切就绪，开始测试！** 🎯
