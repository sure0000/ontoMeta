import {
  HistoryOutlined,
  ProfileOutlined,
  FileTextOutlined,
} from "@ant-design/icons";
import { Alert, Button, Space, Spin, Table, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import type { ChangeLog, TaskRecord } from "../types";

const { Text } = Typography;

export function ExecutionRecordsPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [logs, setLogs] = useState<ChangeLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [logsLoading, setLogsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!domainId) return;
    setLoading(true);
    api
      .listTasks(domainId)
      .then((items) => {
        setTasks(items);
        if (items.length > 0) setSelectedTaskId(items[0].id);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId]);

  useEffect(() => {
    if (!domainId || !selectedTaskId) {
      setLogs([]);
      return;
    }
    setLogsLoading(true);
    api
      .getTaskLogs(domainId, selectedTaskId)
      .then(setLogs)
      .catch((err) => message.error(err.message))
      .finally(() => setLogsLoading(false));
  }, [domainId, selectedTaskId]);

  const taskColumns: ColumnsType<TaskRecord> = [
    {
      title: "任务 ID",
      dataIndex: "id",
      key: "id",
      render: (id: string) => <Text code>{id.slice(0, 8)}…</Text>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "进度",
      dataIndex: "progress",
      key: "progress",
      width: 100,
      align: "right",
      render: (v) => `${v}%`,
    },
    {
      title: "消息",
      dataIndex: "message",
      key: "message",
      ellipsis: true,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v) => new Date(v).toLocaleString(),
    },
    {
      title: "操作",
      key: "actions",
      width: 110,
      render: (_, record) => (
        <Button
          type={selectedTaskId === record.id ? "primary" : "link"}
          size="small"
          onClick={() => setSelectedTaskId(record.id)}
        >
          查看日志
        </Button>
      ),
    },
  ];

  const logColumns: ColumnsType<ChangeLog> = [
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v) => new Date(v).toLocaleString(),
    },
    {
      title: "动作",
      dataIndex: "action",
      key: "action",
      width: 130,
      render: (action) => <StatusBadge status={action} />,
    },
    {
      title: "实体类型",
      dataIndex: "entity_type",
      key: "entity_type",
      width: 130,
    },
    {
      title: "摘要",
      dataIndex: "change_summary",
      key: "change_summary",
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "操作人",
      dataIndex: "operator",
      key: "operator",
      width: 120,
      render: (v) => v || <span className="om-muted">-</span>,
    },
  ];

  if (loading) return <PageSkeleton type="list" />;

  return (
    <PageContainer full>
      <PageHeader
        icon={<HistoryOutlined />}
        title="任务执行记录"
        description="查看预处理任务的执行状态与变更日志，跟踪本体草稿生成全过程。"
      />

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setError(null)}
        />
      )}

      {tasks.length === 0 ? (
        <EmptyState
          title="暂无执行任务记录"
          description="当在工作区发起本体草稿生成时，相关任务会在此实时展示。"
        />
      ) : (
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          <SectionCard
            title="任务列表"
            count={tasks.length}
            countPrimary
            icon={<ProfileOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={taskColumns}
              dataSource={tasks}
              pagination={false}
              rowClassName={(record) =>
                record.id === selectedTaskId ? "om-table-row-selected" : ""
              }
              onRow={(record) => ({
                onClick: () => setSelectedTaskId(record.id),
                style: { cursor: "pointer" },
              })}
            />
          </SectionCard>

          <SectionCard
            title="执行日志"
            icon={<FileTextOutlined />}
            extra={
              selectedTaskId && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  任务 {selectedTaskId.slice(0, 8)}…
                </Text>
              )
            }
            bodyFlush
          >
            <Spin spinning={logsLoading}>
              <Table
                className="om-table"
                rowKey="id"
                size="middle"
                columns={logColumns}
                dataSource={logs}
                pagination={false}
                locale={{ emptyText: "暂无日志" }}
              />
            </Spin>
          </SectionCard>
        </Space>
      )}
    </PageContainer>
  );
}
