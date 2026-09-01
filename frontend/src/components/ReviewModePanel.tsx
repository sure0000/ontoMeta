import { Progress, Space, Tag, Tooltip } from "antd";
import { CheckCircleOutlined, ClockCircleOutlined } from "@ant-design/icons";

interface SegmentProgress {
  segment_id: string;
  segment_name: string;
  total_count: number;
  needs_review_count: number;
  reviewed_count: number;
  progress_ratio: number;
}

interface ReviewStats {
  total_objects: number;
  needs_review_count: number;
  reviewed_count: number;
  progress_ratio: number;
  segment_progress: SegmentProgress[];
}

interface Props {
  stats: ReviewStats | null;
  loading?: boolean;
  onSegmentClick?: (segmentId: string) => void;
}

/**
 * 审核模式进度面板：板块级进度地形 + 全局统计。
 *
 * 设计要求（docs/ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md §5.1）：
 * - 进度地形：哪块判完了、哪块还全红
 * - 板块按待审核数降序排序（未完成的排在前面）
 */
export function ReviewModePanel({ stats, loading, onSegmentClick }: Props) {
  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: "center", color: "var(--om-color-text-tertiary)" }}>
        加载中...
      </div>
    );
  }

  if (!stats) {
    return null;
  }

  const globalProgress = Math.round(stats.progress_ratio * 100);

  return (
    <div className="review-mode-panel">
      {/* 全局进度 */}
      <div className="review-global-stats">
        <div className="review-stat-row">
          <Space size={16}>
            <div className="review-stat-item">
              <span className="review-stat-label">总对象</span>
              <span className="review-stat-value">{stats.total_objects}</span>
            </div>
            <div className="review-stat-item review-stat-item--pending">
              <ClockCircleOutlined />
              <span className="review-stat-label">待审核</span>
              <span className="review-stat-value">{stats.needs_review_count}</span>
            </div>
            <div className="review-stat-item review-stat-item--completed">
              <CheckCircleOutlined />
              <span className="review-stat-label">已审核</span>
              <span className="review-stat-value">{stats.reviewed_count}</span>
            </div>
          </Space>
        </div>
        <Progress
          percent={globalProgress}
          strokeColor="var(--om-color-success)"
          trailColor="var(--om-color-fill-tertiary)"
          showInfo={false}
          style={{ marginTop: 12 }}
        />
        <div style={{ marginTop: 4, fontSize: 12, color: "var(--om-color-text-tertiary)" }}>
          审核进度 {globalProgress}%
        </div>
      </div>

      {/* 板块级进度地形 */}
      <div className="review-segment-progress">
        <h4 style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>板块进度</h4>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {stats.segment_progress.map((seg) => {
            const segProgress = Math.round(seg.progress_ratio * 100);
            const isDone = seg.needs_review_count === 0;

            return (
              <div
                key={seg.segment_id}
                className={`review-segment-item ${onSegmentClick ? "review-segment-item--clickable" : ""}`}
                onClick={() => onSegmentClick?.(seg.segment_id)}
              >
                <div className="review-segment-header">
                  <span className="review-segment-name">{seg.segment_name}</span>
                  <Space size={8}>
                    {isDone ? (
                      <Tag color="success" icon={<CheckCircleOutlined />}>
                        已完成
                      </Tag>
                    ) : (
                      <Tooltip title={`待审核 ${seg.needs_review_count} / ${seg.total_count}`}>
                        <Tag color="orange">{seg.needs_review_count} 待审</Tag>
                      </Tooltip>
                    )}
                  </Space>
                </div>
                <Progress
                  percent={segProgress}
                  strokeColor={isDone ? "var(--om-color-success)" : "var(--om-color-warning)"}
                  trailColor="var(--om-color-fill-tertiary)"
                  size="small"
                  showInfo={false}
                  style={{ marginTop: 6 }}
                />
                <div className="review-segment-stats">
                  <span>{segProgress}%</span>
                  <span className="review-segment-count">
                    {seg.reviewed_count} / {seg.total_count}
                  </span>
                </div>
              </div>
            );
          })}
        </Space>
      </div>

      <style>{`
        .review-mode-panel {
          padding: 16px;
          background: var(--om-color-bg-container);
          border-radius: 8px;
        }

        .review-global-stats {
          padding-bottom: 16px;
          border-bottom: 1px solid var(--om-color-border);
        }

        .review-stat-row {
          margin-bottom: 8px;
        }

        .review-stat-item {
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }

        .review-stat-item--pending {
          color: var(--om-color-warning);
        }

        .review-stat-item--completed {
          color: var(--om-color-success);
        }

        .review-stat-label {
          font-size: 12px;
          color: var(--om-color-text-tertiary);
        }

        .review-stat-value {
          font-size: 18px;
          font-weight: 600;
        }

        .review-segment-progress {
          margin-top: 16px;
        }

        .review-segment-item {
          padding: 10px 12px;
          border-radius: 6px;
          background: var(--om-color-fill-quaternary);
          transition: background 0.2s;
        }

        .review-segment-item--clickable {
          cursor: pointer;
        }

        .review-segment-item--clickable:hover {
          background: var(--om-color-fill-tertiary);
        }

        .review-segment-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 4px;
        }

        .review-segment-name {
          font-weight: 500;
          font-size: 13px;
        }

        .review-segment-stats {
          display: flex;
          justify-content: space-between;
          margin-top: 4px;
          font-size: 12px;
          color: var(--om-color-text-tertiary);
        }

        .review-segment-count {
          font-variant-numeric: tabular-nums;
        }
      `}</style>
    </div>
  );
}
