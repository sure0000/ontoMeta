import { ApartmentOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Drawer, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { ArtifactDetail } from "../components/AgentsPanel";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { SectionCard } from "../components/SectionCard";
import type { GovernanceArtifact, TaskPipeline } from "../types";

const { Text } = Typography;

const PIPELINE_STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  running: "processing",
  succeeded: "green",
  failed: "red",
};

const STEP_STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  validated: "blue",
  confirmed: "gold",
  executing: "processing",
  succeeded: "green",
  failed: "red",
};

/** 任务链详情抽屉：逐步列出步骤，每步「查看」跳该步制品的 ArtifactDetail。 */
function PipelineDrawer({
  pipeline,
  onClose,
}: {
  pipeline: TaskPipeline | null;
  onClose: () => void;
}) {
  const [artifact, setArtifact] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);

  const openStep = async (id: string) => {
    try {
      setArtifact(await api.getArtifact(id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载制品失败");
    }
  };

  // 管理页只做只读追踪；校验/确认/执行仍在类型页或对话里发起。此处走一遍步骤后刷新详情。
  const runStep = async (
    step: "validate" | "confirm" | "execute",
    target: GovernanceArtifact,
  ) => {
    setBusy(true);
    try {
      const next =
        step === "validate"
          ? await api.validateArtifact(target.id)
          : step === "confirm"
            ? await api.confirmArtifact(target.id)
            : await api.executeArtifact(target.id);
      setArtifact(next);
      const label = { validate: "校验", confirm: "确认", execute: "执行" }[step];
      message.success(`${label}完成：${next.status}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  if (!pipeline) return null;

  const stepColumns: ColumnsType<TaskPipeline["steps"][number]> = [
    { title: "步序", dataIndex: "step_index", key: "step_index", width: 64 },
    { title: "类型", dataIndex: "kind", key: "kind" },
    { title: "意图", dataIndex: "intent", key: "intent" },
    {
      title: "状态",
      key: "status",
      render: (_, step) =>
        step.artifact_status ? (
          <Tag color={STEP_STATUS_COLOR[step.artifact_status] ?? "default"}>
            {step.artifact_status}
          </Tag>
        ) : (
          <Text type="secondary">未起草</Text>
        ),
    },
    {
      title: "操作",
      key: "actions",
      width: 88,
      render: (_, step) =>
        step.artifact_id ? (
          <Button size="small" onClick={() => void openStep(step.artifact_id!)}>
            查看
          </Button>
        ) : null,
    },
  ];

  return (
    <Drawer
      open={Boolean(pipeline)}
      onClose={onClose}
      width={720}
      title={
        <Space>
          {pipeline.name}
          <Tag color={PIPELINE_STATUS_COLOR[pipeline.status] ?? "default"}>
            {pipeline.status}
          </Tag>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="关于任务编排"
          description="任务编排管理多个任务的执行顺序和上下文传递。每一步仍是独立任务，需要分别进行校验、确认和执行。可在 Data Agent 中创建新的编排或设置调度。"
        />
        {pipeline.next_blocked_reason && (
          <Alert type="warning" showIcon message={pipeline.next_blocked_reason} />
        )}
        <Table
          rowKey="id"
          size="small"
          columns={stepColumns}
          dataSource={pipeline.steps}
          pagination={false}
        />
      </Space>
      <ArtifactDetail
        artifact={artifact}
        busy={busy}
        onClose={() => setArtifact(null)}
        onStep={runStep}
      />
    </Drawer>
  );
}

/** 任务链管理页：只读列出所有任务链，查看逐步进度。 */
export function PipelinesPage() {
  const [rows, setRows] = useState<TaskPipeline[]>([]);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [detail, setDetail] = useState<TaskPipeline | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api.listPipelines());
      setForbidden(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setForbidden(true);
      } else {
        message.error(err instanceof Error ? err.message : "加载失败");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ColumnsType<TaskPipeline> = [
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: string) => (
        <Tag color={PIPELINE_STATUS_COLOR[status] ?? "default"}>{status}</Tag>
      ),
    },
    {
      title: "步数",
      key: "steps",
      render: (_, row) => row.steps.length,
    },
    {
      title: "调度",
      key: "schedule",
      render: (_, row) =>
        row.schedule_cron ? (
          <Space size={4}>
            <Text code>{row.schedule_cron}</Text>
            {row.compiled_dag_id && <Tag color="blue">已编译</Tag>}
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      render: (v: string) => new Date(v).toLocaleString(),
    },
    {
      title: "操作",
      key: "actions",
      width: 88,
      render: (_, row) => (
        <Button size="small" onClick={() => setDetail(row)}>
          查看
        </Button>
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={<ApartmentOutlined />}
        title="任务编排"
        description="串联多个任务形成数据处理流水线，如「物化 → 清洗 → 聚合」等复杂作业链。"
      />
      <SectionCard
        title="任务编排列表"
        icon={<ApartmentOutlined />}
        count={rows.length}
        extra={<Button icon={<ReloadOutlined />} onClick={() => void load()} />}
      >
        {forbidden ? (
          <Alert
            type="error"
            showIcon
            message="需要 publisher 角色"
            description="任务编排需要 publisher 权限才能访问。请使用 publisher 或 ADMIN Token。"
          />
        ) : (
          <Table
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={rows}
            pagination={false}
          />
        )}
      </SectionCard>
      <PipelineDrawer pipeline={detail} onClose={() => setDetail(null)} />
    </PageContainer>
  );
}
