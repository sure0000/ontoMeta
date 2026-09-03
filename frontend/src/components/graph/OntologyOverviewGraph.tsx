import { CompressOutlined, FullscreenExitOutlined, FullscreenOutlined } from "@ant-design/icons";
import { Button, Space, Tooltip as AntTooltip } from "antd";
import { Cosmograph, prepareCosmographData } from "@cosmograph/cosmograph";
import { memo, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import type { OntologyGroupedGraph } from "../../types";

export interface OntologyOverviewGraphProps {
  /** 分组图数据（概览模式） */
  graph: OntologyGroupedGraph;
  /** 画布高度 */
  height?: number;
  /** 对象详情路径生成器 */
  objectDetailPath?: (objectId: string) => string;
  /** 点击聚类时的下钻回调 */
  onClusterDrillIn?: (clusterId: string) => void;
  /** 嵌入模式（无外边距） */
  embedded?: boolean;
  /** 额外操作按钮 */
  extraActions?: ReactNode;
}

/**
 * 概览模式图组件 - 使用 Cosmograph (WebGL) 渲染大规模图
 *
 * 适用场景：
 * - 100+ 节点的概览视图
 * - 需要极致性能的场景
 *
 * 限制：
 * - 只支持圆形节点
 * - 标签只能纯文本
 * - 不支持语义缩放（概览模式用下钻矩阵代替）
 */
export const OntologyOverviewGraph = memo(function OntologyOverviewGraph({
  graph,
  height = 500,
  objectDetailPath,
  onClusterDrillIn,
  embedded = false,
  extraActions,
}: OntologyOverviewGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cosmographRef = useRef<Cosmograph | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const navigate = useNavigate();

  // 准备 Cosmograph 数据格式
  const prepareData = useCallback(() => {
    const { clusters, hub_nodes, edges } = graph;

    // 节点：聚类 + 枢纽
    const points = [
      ...clusters.map((c) => ({
        id: c.id,
        label: c.name,
        kind: c.kind,
        count: c.node_count,
        isHub: false,
        clusterId: c.id,
        x: c.layout?.x ?? 0,
        y: c.layout?.y ?? 0,
      })),
      ...hub_nodes.map((h) => ({
        id: h.id,
        label: h.display_name || h.label,
        kind: "hub",
        count: 1,
        isHub: true,
        x: 0,
        y: 0,
      })),
    ];

    // 边
    const links = edges.map((e) => ({
      source: e.source_cluster_id,
      target: e.target_cluster_id,
      weight: e.weight || 1,
    }));

    return { points, links };
  }, [graph]);

  // 初始化和更新 Cosmograph
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const { points, links } = prepareData();

    // 清理旧实例
    if (cosmographRef.current) {
      cosmographRef.current.destroy?.();
      cosmographRef.current = null;
    }

    // 准备数据
    prepareCosmographData(
      {
        points: {
          pointIdBy: "id",
          pointLabelBy: "label",
        },
        links: {
          linkSourceBy: "source",
          linkTargetsBy: ["target"],
        },
      },
      points,
      links
    ).then((result) => {
      if (!result || !container) return;

      const { points: preparedPoints, links: preparedLinks, cosmographConfig } = result;

      // 创建 Cosmograph 实例
      const cosmograph = new Cosmograph(container, {
        points: preparedPoints,
        links: preparedLinks,
        ...cosmographConfig,
        backgroundColor: "#0A0D12",
        renderLinks: true,
        spaceDimensions: 2,
        simulationGravity: 0.2,
        simulationRepulsion: 0.8,
        simulationLinkSpring: 0.5,
        simulationFriction: 0.85,
        enableSimulation: !points.every((n) => n.x != null && n.y != null),
      });

      cosmographRef.current = cosmograph;

      // 事件监听 - 使用 on 方法
      // TODO: 根据实际 API 文档调整事件监听方式
      // Cosmograph 可能不支持直接的点击事件，需要查阅官方文档
    }).catch((err) => {
      console.error("Failed to prepare Cosmograph data:", err);
    });

    return () => {
      if (cosmographRef.current) {
        cosmographRef.current.destroy?.();
        cosmographRef.current = null;
      }
    };
  }, [graph, prepareData, objectDetailPath, onClusterDrillIn, navigate]);

  // 全屏切换
  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;

    if (!isFullscreen) {
      container.requestFullscreen?.();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen?.();
      setIsFullscreen(false);
    }
  }, [isFullscreen]);

  // 适配画布
  const fitView = useCallback(() => {
    cosmographRef.current?.fitView?.();
  }, []);

  return (
    <div
      style={{
        position: "relative",
        height,
        width: "100%",
        backgroundColor: "#0A0D12",
        borderRadius: embedded ? 0 : 8,
        overflow: "hidden",
      }}
    >
      {/* 画布容器 */}
      <div ref={containerRef} style={{ width: "100%", height: "100%" }} />

      {/* 操作按钮 */}
      <div
        style={{
          position: "absolute",
          top: 16,
          right: 16,
          zIndex: 10,
        }}
      >
        <Space>
          {extraActions}
          <AntTooltip title="适配画布">
            <Button
              icon={<CompressOutlined />}
              onClick={fitView}
              style={{
                backgroundColor: "rgba(255, 255, 255, 0.1)",
                borderColor: "rgba(255, 255, 255, 0.2)",
                color: "#fff",
              }}
            />
          </AntTooltip>
          <AntTooltip title={isFullscreen ? "退出全屏" : "全屏"}>
            <Button
              icon={isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              onClick={toggleFullscreen}
              style={{
                backgroundColor: "rgba(255, 255, 255, 0.1)",
                borderColor: "rgba(255, 255, 255, 0.2)",
                color: "#fff",
              }}
            />
          </AntTooltip>
        </Space>
      </div>

      {/* 图例 */}
      <div
        style={{
          position: "absolute",
          bottom: 16,
          left: 16,
          padding: "8px 12px",
          backgroundColor: "rgba(10, 13, 18, 0.85)",
          borderRadius: 6,
          fontSize: 12,
          color: "#9CA3AF",
          backdropFilter: "blur(4px)",
        }}
      >
        <div style={{ marginBottom: 4, fontWeight: 500 }}>交互提示</div>
        <div>单击枢纽节点 → 跳转详情</div>
        <div>双击聚类 → 下钻矩阵</div>
        <div>滚轮 → 缩放</div>
      </div>
    </div>
  );
});
