import {
  HistoryOutlined,
  ProfileOutlined,
  FileTextOutlined,
  CopyOutlined,
} from "@ant-design/icons";
import { Alert, Button, Modal, Space, Spin, Table, Typography, message } from "antd";
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
  const [logs, setLogs] = useState<ChangeLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [logsLoading, setLogsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logModalOpen, setLogModalOpen] = useState(false);
  const [activeTask, setActiveTask] = useState<TaskRecord | null>(null);
  const [taskLogsMap, setTaskLogsMap] = useState<Record<string, ChangeLog[]>>({});

  useEffect(() => {
    if (!domainId) return;
    setLoading(true);
    api
      .listTasks(domainId)
      .then(async (items) => {
        setTasks(items);
        const entries = await Promise.all(
          items.map((t) =>
            api
              .getTaskLogs(domainId, t.id)
              .then((logs) => [t.id, logs] as const)
              .catch(() => [t.id, [] as ChangeLog[]] as const),
          ),
        );
        setTaskLogsMap(Object.fromEntries(entries));
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId]);

  const openLogModal = (task: TaskRecord) => {
    setActiveTask(task);
    setLogModalOpen(true);
    if (!domainId) return;
    setLogsLoading(true);
    setLogs([]);
    api
      .getTaskLogs(domainId, task.id)
      .then(setLogs)
      .catch((err) => message.error(err.message))
      .finally(() => setLogsLoading(false));
  };

  const closeLogModal = () => {
    setLogModalOpen(false);
    setLogs([]);
    setLogsLoading(false);
  };

  const buildLogText = (items: ChangeLog[]) =>
    items
      .map((log) => {
        const time = new Date(log.created_at).toLocaleString();
        const summary = log.change_summary?.trim() || "(无摘要)";
        return log.operator
          ? `[${time}] ${summary}  — ${log.operator}`
          : `[${time}] ${summary}`;
      })
      .join("\n");

  const handleCopyLogs = async () => {
    if (logs.length === 0) return;
    const text = buildLogText(logs);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      message.success(`已复制 ${logs.length} 条日志`);
    } catch {
      message.error("复制失败，请手动选择日志内容");
    }
  };

  const taskColumns: ColumnsType<TaskRecord> = [
    {
      title: "任务 ID",
      dataIndex: "id",
      key: "id",
      width: 130,
      render: (id: string) => <Text code>{id.slice(0, 8)}…</Text>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "进度",
      dataIndex: "progress",
      key: "progress",
      width: 80,
      align: "right",
      render: (v) => `${v}%`,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v) => new Date(v).toLocaleString(),
    },
    {
      title: "实体类型",
      key: "entity_type",
      width: 140,
      render: (_, record) => {
        const logs = taskLogsMap[record.id] ?? [];
        if (logs.length === 0)
          return <span className="om-muted">-</span>;
        return (
          <Space direction="vertical" size={2} style={{ display: "flex" }}>
            {logs.map((log) => (
              <Text key={log.id} style={{ fontSize: 12 }}>
                {log.entity_type}
              </Text>
            ))}
          </Space>
        );
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 96,
      render: (_, record) => (
        <Button
          type={activeTask?.id === record.id ? "primary" : "link"}
          size="small"
          onClick={(e) => {
            e.stopPropagation();
            openLogModal(record);
          }}
        >
          查看日志
        </Button>
      ),
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
                record.id === activeTask?.id ? "om-table-row-selected" : ""
              }
            />
          </SectionCard>
        </Space>
      )}

      <Modal
        title={
          <Space>
            <FileTextOutlined />
            <span>执行日志</span>
            {activeTask && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                任务 {activeTask.id.slice(0, 8)}…
              </Text>
            )}
          </Space>
        }
        open={logModalOpen}
        onCancel={closeLogModal}
        width={820}
        destroyOnClose
        styles={{ body: { maxHeight: "60vh", overflowY: "auto" } }}
        footer={
          <Space>
            <Button onClick={closeLogModal}>关闭</Button>
            <Button
              icon={<CopyOutlined />}
              disabled={logsLoading || logs.length === 0}
              onClick={handleCopyLogs}
            >
              复制日志
            </Button>
          </Space>
        }
      >
        <Spin spinning={logsLoading}>
          {logs.length === 0 ? (
            <div className="om-muted" style={{ padding: "24px 0", textAlign: "center" }}>
              暂无日志
            </div>
          ) : (
            <pre
              style={{
                margin: 0,
                padding: 12,
                background: "var(--om-bg-muted, #f5f5f5)",
                borderRadius: 6,
                fontSize: 12,
                lineHeight: 1.6,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
              }}
            >
              {buildLogText(logs)}
            </pre>
          )}
        </Spin>
      </Modal>
    </PageContainer>
  );
}
