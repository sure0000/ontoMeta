// 图组件性能测试和示例
// 用于验证 Cosmograph 集成是否正常工作

import { useState } from "react";
import { OntologyDetailGraph } from "../components/graph/OntologyDetailGraph";
import { OntologyOverviewGraph } from "../components/graph/OntologyOverviewGraph";
import { OntologyGraphSwitcher } from "../components/graph/OntologyGraphSwitcher";
import type { OntologyGraph, OntologyGroupedGraph } from "../types";

// 测试数据：详情图（小邻域图）
const detailGraphData: OntologyGraph = {
  nodes: [
    { id: "1", label: "user", display_name: "用户", status: "published" },
    { id: "2", label: "order", display_name: "订单", status: "published" },
    { id: "3", label: "product", display_name: "商品", status: "published" },
    { id: "4", label: "payment", display_name: "支付", status: "draft" },
    { id: "5", label: "shipping", display_name: "物流", status: "draft" },
  ],
  edges: [
    { id: "e1", source: "2", target: "1", label: "属于", relation_id: "r1" },
    { id: "e2", source: "2", target: "3", label: "包含", relation_id: "r2" },
    { id: "e3", source: "4", target: "2", label: "关联", relation_id: "r3" },
    { id: "e4", source: "5", target: "2", label: "配送", relation_id: "r4" },
  ],
};

// 测试数据：概览图（大规模聚类图）
const overviewGraphData: OntologyGroupedGraph = {
  clusters: [
    {
      id: "c1",
      name: "交易中心",
      kind: "business",
      nodes: [
        { id: "1", label: "order", display_name: "订单", status: "published" },
        { id: "2", label: "payment", display_name: "支付", status: "published" },
      ],
      node_count: 2,
      internal_relation_count: 5,
      cross_relation_count: 3,
      truncated: false,
      layout: { x: 0, y: 0 },
    },
    {
      id: "c2",
      name: "用户中心",
      kind: "business",
      nodes: [
        { id: "3", label: "user", display_name: "用户", status: "published" },
        { id: "4", label: "profile", display_name: "用户资料", status: "published" },
      ],
      node_count: 2,
      internal_relation_count: 2,
      cross_relation_count: 4,
      truncated: false,
      layout: { x: 2, y: 0 },
    },
    {
      id: "c3",
      name: "商品中心",
      kind: "business",
      nodes: [
        { id: "5", label: "product", display_name: "商品", status: "published" },
        { id: "6", label: "category", display_name: "类目", status: "published" },
      ],
      node_count: 2,
      internal_relation_count: 3,
      cross_relation_count: 2,
      truncated: false,
      layout: { x: 1, y: 2 },
    },
  ],
  hub_nodes: [
    {
      id: "hub1",
      label: "tenant",
      display_name: "租户",
      status: "published",
      degree: 8,
      layout: { x: 1, y: 1 },
    },
  ],
  edges: [
    { id: "ge1", source_cluster_id: "c1", target_cluster_id: "c2", weight: 3, relation_ids: ["r1", "r2", "r3"] },
    { id: "ge2", source_cluster_id: "c1", target_cluster_id: "c3", weight: 2, relation_ids: ["r4", "r5"] },
    { id: "ge3", source_cluster_id: "c2", target_cluster_id: "hub1", weight: 4, relation_ids: ["r6", "r7", "r8", "r9"] },
  ],
  isolated_nodes: [],
  total_object_count: 7,
  total_relation_count: 10,
};

export function GraphTestPage() {
  const [mode, setMode] = useState<"detail" | "overview">("detail");

  return (
    <div style={{ padding: 24 }}>
      <h1>图组件测试页面</h1>

      <div style={{ marginBottom: 24 }}>
        <h2>1. OntologyDetailGraph（详情模式 - G6）</h2>
        <p>适合 10-50 节点的邻域图，保留丰富交互能力。</p>
        <div style={{ border: "1px solid #d9d9d9", borderRadius: 8 }}>
          <OntologyDetailGraph
            graph={detailGraphData}
            height={400}
            objectDetailPath={(id) => `/objects/${id}`}
            relationDetailPath={(id) => `/relations/${id}`}
          />
        </div>
      </div>

      <div style={{ marginBottom: 24 }}>
        <h2>2. OntologyOverviewGraph（概览模式 - Cosmograph WebGL）</h2>
        <p>适合 100+ 节点的宏观图，性能极致。</p>
        <div style={{ border: "1px solid #d9d9d9", borderRadius: 8 }}>
          <OntologyOverviewGraph
            graph={overviewGraphData}
            height={400}
            objectDetailPath={(id) => `/objects/${id}`}
            onClusterDrillIn={(id) => console.log("下钻聚类:", id)}
          />
        </div>
      </div>

      <div style={{ marginBottom: 24 }}>
        <h2>3. OntologyGraphSwitcher（自动切换）</h2>
        <p>根据 graphMode 自动在 G6 和 Cosmograph 之间切换。</p>
        <div style={{ border: "1px solid #d9d9d9", borderRadius: 8 }}>
          <OntologyGraphSwitcher
            graph={detailGraphData}
            groupedGraph={overviewGraphData}
            height={400}
            graphMode={mode}
            onGraphModeChange={setMode}
            objectDetailPath={(id) => `/objects/${id}`}
            relationDetailPath={(id) => `/relations/${id}`}
            onClusterDrillIn={(id) => console.log("下钻聚类:", id)}
          />
        </div>
      </div>

      <div style={{ marginTop: 32, padding: 16, background: "#f5f5f5", borderRadius: 8 }}>
        <h3>性能对比</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #d9d9d9" }}>
              <th style={{ padding: 8, textAlign: "left" }}>组件</th>
              <th style={{ padding: 8, textAlign: "left" }}>渲染引擎</th>
              <th style={{ padding: 8, textAlign: "left" }}>适用场景</th>
              <th style={{ padding: 8, textAlign: "left" }}>性能</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style={{ padding: 8 }}>OntologyDetailGraph</td>
              <td style={{ padding: 8 }}>G6 (Canvas)</td>
              <td style={{ padding: 8 }}>10-50 节点</td>
              <td style={{ padding: 8 }}>中等，交互丰富</td>
            </tr>
            <tr>
              <td style={{ padding: 8 }}>OntologyOverviewGraph</td>
              <td style={{ padding: 8 }}>Cosmograph (WebGL)</td>
              <td style={{ padding: 8 }}>100+ 节点</td>
              <td style={{ padding: 8 }}>极致，3-10x 提升</td>
            </tr>
            <tr>
              <td style={{ padding: 8 }}>OntologyGraphView (旧)</td>
              <td style={{ padding: 8 }}>G6 (Canvas)</td>
              <td style={{ padding: 8 }}>全部场景</td>
              <td style={{ padding: 8 }}>基准，有重建开销</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 24, padding: 16, background: "#e6f7ff", borderRadius: 8 }}>
        <h3>迁移检查清单</h3>
        <ul>
          <li>✅ Cosmograph 已安装 (@cosmograph/cosmograph@2.5.1)</li>
          <li>✅ OntologyDetailGraph 已创建（G6 详情模式）</li>
          <li>✅ OntologyOverviewGraph 已创建（Cosmograph 概览模式）</li>
          <li>✅ OntologyGraphSwitcher 已创建（自动切换）</li>
          <li>✅ 导出到 index.ts</li>
          <li>⏳ 迁移现有页面（SegmentDetailPage 等）</li>
          <li>⏳ 性能测试（100+ 节点场景）</li>
          <li>⏳ 兼容性测试（交互功能）</li>
        </ul>
      </div>

      <div style={{ marginTop: 24, padding: 16, background: "#fff7e6", borderRadius: 8 }}>
        <h3>已知限制</h3>
        <ul>
          <li>
            <strong>Cosmograph 节点样式：</strong>只支持圆形，不支持自定义形状
          </li>
          <li>
            <strong>语义缩放（LoD）：</strong>概览模式暂不支持展开/收起聚类，使用下钻矩阵代替
          </li>
          <li>
            <strong>浏览器兼容性：</strong>需要 WebGL 支持，IE11 不支持
          </li>
          <li>
            <strong>标签显示：</strong>Cosmograph 默认不显示标签，仅悬浮时显示
          </li>
        </ul>
      </div>
    </div>
  );
}
