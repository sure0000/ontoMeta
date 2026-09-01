import { ApartmentOutlined, ArrowRightOutlined, AppstoreOutlined } from "@ant-design/icons";
import { Alert, Empty, List, Row, Col, Spin, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import type { OntologyGroupedGraph, OntologyGraph, SegmentDetail } from "../types";
import { useApi } from "../hooks/useApi";
import { OntologyGraphView } from "./graph";
import { ISOLATED_CLUSTER_ID } from "./graph/g6/buildG6Data";
import { SectionCard } from "./SectionCard";

interface Props {
  ontologyId: string;
  publishedOnly?: boolean;
  objectDetailPath?: (objectId: string) => string;
  segmentPath?: (segmentId: string) => string;
  height?: number;
}

const EMPTY_GRAPH: OntologyGraph = { nodes: [], edges: [] };

/** L1 business map: directory, macro graph, and a compact selected-segment summary. */
export function OntologyOverviewPanel({
  ontologyId,
  publishedOnly = false,
  objectDetailPath = (id) => `/ontology/${id}`,
  segmentPath = (id) => `/segments/${id}`,
  height = 560,
}: Props) {
  const navigate = useNavigate();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SegmentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const { data: groupedGraph, loading, error } = useApi<OntologyGroupedGraph>(
    () => api.getOntologyGroupedGraph(ontologyId, publishedOnly),
    [ontologyId, publishedOnly],
  );

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    api
      .getSegment(selectedId, publishedOnly)
      .then((next) => {
        if (!cancelled) setDetail(next);
      })
      .catch((err) => {
        if (!cancelled) setDetailError(err instanceof Error ? err.message : "加载板块详情失败");
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, publishedOnly]);

  const clusters = useMemo(
    () => [...(groupedGraph?.clusters ?? [])].sort((a, b) => b.node_count - a.node_count),
    [groupedGraph],
  );
  const relationSentences = detail?.relation_sentences ?? [];

  if (loading && !groupedGraph) return <Spin spinning />;
  if (error) return <Alert type="error" message="业务地图加载失败" description={error} showIcon />;
  if (!groupedGraph || (clusters.length === 0 && groupedGraph.hub_nodes.length === 0)) {
    return <Empty description="暂无可展示的业务结构" />;
  }

  return (
    <div className="ontology-overview-panel">
      <Row gutter={[16, 16]} align="stretch">
        <Col xs={24} lg={7}>
          <SectionCard title="板块目录" icon={<AppstoreOutlined />} bodyFlush>
            <List
              size="small"
              dataSource={clusters}
              locale={{ emptyText: "暂无已分组对象" }}
              renderItem={(cluster) => (
                <List.Item
                  style={{
                    cursor: "pointer",
                    padding: "10px 14px",
                    background: selectedId === cluster.id ? "var(--om-bg-soft)" : undefined,
                  }}
                  onClick={() => setSelectedId(cluster.id)}
                >
                  <div style={{ minWidth: 0, width: "100%" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {cluster.name}
                      </span>
                      <Tag>{cluster.node_count}</Tag>
                    </div>
                    {cluster.truncated && (
                      <span style={{ color: "var(--om-text-tertiary)", fontSize: 12 }}>
                        仅显示核心成员
                      </span>
                    )}
                  </div>
                </List.Item>
              )}
            />
            {groupedGraph.isolated_nodes.length > 0 && (
              <div style={{ padding: "10px 14px", borderTop: "1px solid var(--om-border)" }}>
                <span>未接入对象</span>
                <Tag style={{ marginInlineStart: 8 }}>{groupedGraph.isolated_nodes.length}</Tag>
              </div>
            )}
          </SectionCard>
        </Col>
        <Col xs={24} lg={17}>
          <SectionCard title="业务地图" icon={<ApartmentOutlined />} bodyFlush>
            <OntologyGraphView
              graph={EMPTY_GRAPH}
              groupedGraph={groupedGraph}
              graphMode="overview"
              height={height}
              onClusterDrillIn={(id) => {
                if (id !== ISOLATED_CLUSTER_ID) navigate(segmentPath(id));
              }}
              objectDetailPath={objectDetailPath}
              embedded
            />
          </SectionCard>
        </Col>
      </Row>

      {selectedId && (
        <div style={{ marginTop: 16 }}>
          <SectionCard title={detail?.display_name ?? "板块摘要"} icon={<AppstoreOutlined />}>
            {detailLoading ? (
              <Spin />
            ) : detailError ? (
              <Alert type="error" message={detailError} showIcon />
            ) : detail ? (
              <>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                  <Tag>{detail.member_count} 个成员</Tag>
                  <Tag>{detail.internal_relation_count} 条内部关系</Tag>
                  <Tag>{detail.cross_relation_count} 条跨板块关系</Tag>
                  <Link to={segmentPath(detail.id)}>
                    查看板块档案 <ArrowRightOutlined />
                  </Link>
                </div>
                {relationSentences.length > 0 ? (
                  <List
                    size="small"
                    dataSource={relationSentences.slice(0, 2)}
                    renderItem={(sentence) => <List.Item>{sentence}</List.Item>}
                  />
                ) : (
                  <span style={{ color: "var(--om-text-tertiary)" }}>暂无内部关系句子</span>
                )}
              </>
            ) : null}
          </SectionCard>
        </div>
      )}
    </div>
  );
}
