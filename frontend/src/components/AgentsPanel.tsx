import {
  CheckCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { SectionCard } from "./SectionCard";
import type {
  AgentKinds,
  AgentValidationIssue,
  GovernanceArtifact,
} from "../types";

const { Paragraph, Text } = Typography;

const PRE_STYLE: React.CSSProperties = {
  background: "rgba(0,0,0,0.03)",
  border: "1px solid rgba(0,0,0,0.06)",
  borderRadius: 6,
  padding: "8px 12px",
  margin: 0,
  maxHeight: 320,
  overflow: "auto",
  fontSize: 12,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const KIND_LABEL: Record<string, string> = {
  cluster: "集群拓扑 cluster",
  sync: "数据同步 sync",
  transform: "数据加工 transform",
  metric: "指标任务 metric",
};

const STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  validated: "blue",
  confirmed: "gold",
  executing: "processing",
  succeeded: "green",
  failed: "red",
};

// 与后端 validation._WARNING_CODES 对齐：这些是 warning 级，其余为阻断级。
const WARNING_CODES = new Set(["engine_unverified", "ontology_issue"]);

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** 治理智能体制品：草稿 → 校验 → 确认 → 执行。整个命名空间需 publisher 角色。 */
export function AgentsPanel() {
  const [rows, setRows] = useState<GovernanceArtifact[]>([]);
  const [kinds, setKinds] = useState<AgentKinds | null>(null);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm<{
    kind: string;
    intent: string;
    ontology_id?: string;
    context?: string;
  }>();

  const handleError = useCallback((err: unknown, fallback: string) => {
    if (err instanceof ApiError && err.status === 403) {
      setForbidden(true);
      return;
    }
    message.error(err instanceof Error ? err.message : fallback);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [artifacts, kindsOut] = await Promise.all([
        api.listArtifacts(),
        api.listAgentKinds(),
      ]);
      setRows(artifacts);
      setKinds(kindsOut);
      setForbidden(false);
    } catch (err) {
      handleError(err, "加载失败");
    } finally {
      setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshDetail = useCallback(async (id: string) => {
    try {
      setDetail(await api.getArtifact(id));
    } catch {
      /* 详情刷新失败不打断主流程 */
    }
  }, []);

  const create = async () => {
    const values = await form.validateFields();
    let context: Record<string, unknown> = {};
    if (values.context?.trim()) {
      try {
        context = JSON.parse(values.context) as Record<string, unknown>;
      } catch {
        message.error("上下文不是合法 JSON");
        return;
      }
    }
    if (values.ontology_id?.trim()) {
      context = { ontology_id: values.ontology_id.trim(), ...context };
    }
    try {
      const created = await api.draftArtifact({
        kind: values.kind,
        intent: values.intent,
        ontology_id: values.ontology_id?.trim() || null,
        context,
      });
      setCreateOpen(false);
      form.resetFields();
      await load();
      setDetail(created);
    } catch (err) {
      handleError(err, "起草失败");
    }
  };

  const runStep = async (
    step: "validate" | "confirm" | "execute",
    artifact: GovernanceArtifact,
  ) => {
    setBusy(true);
    try {
      let next: GovernanceArtifact;
      if (step === "validate") {
        next = await api.validateArtifact(artifact.id);
      } else if (step === "confirm") {
        next = await api.confirmArtifact(artifact.id);
      } else {
        next = await api.executeArtifact(artifact.id);
      }
      setDetail(next);
      await load();
      const label = { validate: "校验", confirm: "确认", execute: "执行" }[step];
      message.success(`${label}完成：${next.status}`);
    } catch (err) {
      handleError(err, "操作失败");
      void refreshDetail(artifact.id);
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnsType<GovernanceArtifact> = [
    {
      title: "类型",
      dataIndex: "kind",
      key: "kind",
      render: (kind: string) => KIND_LABEL[kind] ?? kind,
    },
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: string, row) => (
        <Space size={4}>
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
          {row.is_high_risk && <Tag color="volcano">高危</Tag>}
        </Space>
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
      render: (_, row) => (
        <Button size="small" onClick={() => setDetail(row)}>
          查看
        </Button>
      ),
    },
  ];

  return (
    <SectionCard
      title="治理智能体制品"
      icon={<RobotOutlined />}
      count={rows.length}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={forbidden}
            onClick={() => setCreateOpen(true)}
          >
            起草制品
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {forbidden ? (
          <Alert
            type="error"
            showIcon
            message="需要 publisher 角色"
            description="写侧智能体会改集群、建表、执行 SQL，/api/agents 整个命名空间仅 publisher 可访问。请用 publisher 或 ADMIN Token。"
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="流水线：草稿 → 校验（含 dry-run 差异）→ 人工确认 → 执行"
            description="LLM 只产声明式 Spec，不产命令；未经确认不得执行，高危制品须先看到 dry-run 差异。"
          />
        )}
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
        />
      </Space>

      <Modal
        open={createOpen}
        title="起草制品"
        onOk={create}
        onCancel={() => setCreateOpen(false)}
        okText="起草"
      >
        <Form form={form} layout="vertical" initialValues={{ kind: "metric" }}>
          <Form.Item name="kind" label="制品类型" rules={[{ required: true }]}>
            <Select
              options={(kinds?.all_kinds ?? Object.keys(KIND_LABEL)).map((k) => ({
                value: k,
                label:
                  (KIND_LABEL[k] ?? k) +
                  (kinds && !kinds.registered.includes(k) ? "（未实现）" : "") +
                  (kinds?.high_risk.includes(k) ? " · 高危" : ""),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="intent"
            label="自然语言意图"
            rules={[{ required: true, message: "请描述要做什么" }]}
          >
            <Input.TextArea rows={2} placeholder="如：统计成交额 / 部署三节点 HDFS" />
          </Form.Item>
          <Form.Item
            name="ontology_id"
            label="本体 ID"
            extra="指标/加工类制品需绑定本体；集群类可留空"
          >
            <Input placeholder="ontology_id（可选）" />
          </Form.Item>
          <Form.Item
            name="context"
            label="上下文 JSON"
            extra='可选。如集群制品填 {"hosts":["n1","n2"]}；凭据只能传 *_ref/*_alias'
          >
            <Input.TextArea rows={2} placeholder='{"hosts": ["n1"]}' />
          </Form.Item>
        </Form>
      </Modal>

      <ArtifactDetail
        artifact={detail}
        busy={busy}
        onClose={() => setDetail(null)}
        onStep={runStep}
      />
    </SectionCard>
  );
}

function IssueList({ issues }: { issues: AgentValidationIssue[] }) {
  if (!issues.length) return <Text type="success">无问题</Text>;
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {issues.map((issue, idx) => {
        const warning = WARNING_CODES.has(issue.code);
        return (
          <div key={`${issue.code}-${idx}`}>
            <Tag color={warning ? "gold" : "red"}>{warning ? "warning" : "阻断"}</Tag>
            <Text code>{issue.code}</Text> {issue.message}
            {issue.entity_name && <Text type="secondary">（{issue.entity_name}）</Text>}
          </div>
        );
      })}
    </Space>
  );
}

export function ArtifactDetail({
  artifact,
  busy,
  onClose,
  onStep,
}: {
  artifact: GovernanceArtifact | null;
  busy: boolean;
  onClose: () => void;
  onStep: (
    step: "validate" | "confirm" | "execute",
    artifact: GovernanceArtifact,
  ) => void;
}) {
  if (!artifact) return null;
  const report = artifact.validation_report;
  const status = artifact.status;

  return (
    <Drawer
      open={Boolean(artifact)}
      onClose={onClose}
      width={640}
      title={
        <Space>
          {KIND_LABEL[artifact.kind] ?? artifact.kind}
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
          {artifact.is_high_risk && <Tag color="volcano">高危</Tag>}
        </Space>
      }
      extra={
        <Space>
          {status === "drafted" && (
            <Button
              icon={<CheckCircleOutlined />}
              loading={busy}
              onClick={() => onStep("validate", artifact)}
            >
              校验
            </Button>
          )}
          {status === "validated" && (
            <>
              <Button loading={busy} onClick={() => onStep("validate", artifact)}>
                重新校验
              </Button>
              <Button
                type="primary"
                icon={<CheckCircleOutlined />}
                loading={busy}
                onClick={() => onStep("confirm", artifact)}
              >
                确认
              </Button>
            </>
          )}
          {status === "confirmed" && (
            <Button
              type="primary"
              danger
              icon={<ThunderboltOutlined />}
              loading={busy}
              onClick={() => onStep("execute", artifact)}
            >
              执行
            </Button>
          )}
        </Space>
      }
    >
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="名称">{artifact.name}</Descriptions.Item>
          <Descriptions.Item label="意图">{artifact.intent || "—"}</Descriptions.Item>
          <Descriptions.Item label="本体">
            {artifact.ontology_id || "—"}
          </Descriptions.Item>
          {artifact.confirmed_by && (
            <Descriptions.Item label="确认人">
              {artifact.confirmed_by}
            </Descriptions.Item>
          )}
        </Descriptions>

        <div>
          <Text strong>声明式 Spec</Text>
          <Paragraph>
            <pre style={PRE_STYLE}>{prettyJson(artifact.spec)}</pre>
          </Paragraph>
        </div>

        {report && (
          <div>
            <Text strong>
              校验报告 ·{" "}
              {report.blocking_count > 0 ? (
                <Text type="danger">{report.blocking_count} 项阻断</Text>
              ) : (
                <Text type="success">无阻断</Text>
              )}
            </Text>
            <div style={{ marginTop: 8 }}>
              <IssueList issues={report.issues ?? []} />
            </div>
            {report.dry_run_error && (
              <Alert
                style={{ marginTop: 8 }}
                type="warning"
                showIcon
                message="dry-run 失败"
                description={report.dry_run_error}
              />
            )}
            {report.dry_run && (
              <div style={{ marginTop: 8 }}>
                <Text type="secondary">dry-run 差异（将要发生什么）</Text>
                <pre style={PRE_STYLE}>{prettyJson(report.dry_run)}</pre>
              </div>
            )}
          </div>
        )}

        {artifact.execution_receipt && (
          <div>
            <Text strong>执行回执</Text>
            <pre style={PRE_STYLE}>
              {prettyJson(artifact.execution_receipt)}
            </pre>
          </div>
        )}
      </Space>
    </Drawer>
  );
}
