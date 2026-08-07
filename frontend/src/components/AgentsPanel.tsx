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
import { SpecForm } from "./artifact-spec/SpecForm";
import { SPEC_FIELDS } from "./artifact-spec/specFields";
import type {
  AgentKinds,
  AgentValidationIssue,
  DomainContext,
  GovernanceArtifact,
  OntologySummary,
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
  materialize: "物化 materialize",
};

/** 中文短名，用于 kind 锁定时的面板标题（不带英文后缀）。 */
const KIND_SHORT_LABEL: Record<string, string> = {
  cluster: "集群拓扑",
  sync: "数据同步",
  transform: "数据加工",
  metric: "指标任务",
  materialize: "物化任务",
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

/**
 * 治理智能体制品：草稿 → 校验 → 确认 → 执行。整个命名空间需 publisher 角色。
 *
 * 传入 `kind` 时化身为「某一类型任务」的专属面板（列表按该 kind 过滤、起草弹窗锁定该
 * 类型）；不传则是覆盖全部类型的通用面板。任务管理菜单的 5 个类型页正是靠此 prop 复用
 * 同一份组件。
 */
export function AgentsPanel({ kind }: { kind?: string } = {}) {
  const [rows, setRows] = useState<GovernanceArtifact[]>([]);
  const [kinds, setKinds] = useState<AgentKinds | null>(null);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 起草表单的结构化状态（取代原 intent/context JSON textarea）
  const [draftKind, setDraftKind] = useState<string>(kind ?? "metric");
  const [draftName, setDraftName] = useState("");
  const [draftOntologyId, setDraftOntologyId] = useState<string | undefined>();
  const [specDraft, setSpecDraft] = useState<Record<string, unknown>>({});

  // 本体下拉数据（OntologySummary 自身无名字，靠 domain_context_id 关联域名做 label）
  const [ontologies, setOntologies] = useState<OntologySummary[]>([]);
  const [domains, setDomains] = useState<DomainContext[]>([]);

  const effectiveKind = kind ?? draftKind;

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
        api.listArtifacts(kind ? { kind } : undefined),
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
  }, [handleError, kind]);

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

  const openCreate = useCallback(() => {
    // 重置表单到初始态，并按需拉本体下拉数据
    setDraftKind(kind ?? "metric");
    setDraftName("");
    setDraftOntologyId(undefined);
    setSpecDraft({});
    setCreateOpen(true);
    if (!ontologies.length) {
      Promise.all([api.listOntologies(), api.listDomains()])
        .then(([onts, doms]) => {
          setOntologies(onts);
          setDomains(doms);
        })
        .catch(() => {
          /* 下拉数据拉取失败不阻断，用户仍可手填其它字段 */
        });
    }
  }, [kind, ontologies.length]);

  /** 各 kind 必填字段的前端非空校验（对齐后端闸门 missing_required_field）。 */
  const missingRequired = (): string | null => {
    for (const f of SPEC_FIELDS[effectiveKind] ?? []) {
      if (!f.required) continue;
      const v = specDraft[f.key];
      const empty =
        v == null ||
        (typeof v === "string" && !v.trim()) ||
        (Array.isArray(v) && v.length === 0);
      if (empty) return f.label;
    }
    return null;
  };

  const create = async () => {
    const missing = missingRequired();
    if (missing) {
      message.error(`请填写：${missing}`);
      return;
    }
    // 非 cluster 制品必须绑定本体
    if (effectiveKind !== "cluster" && !draftOntologyId) {
      message.error("请选择本体");
      return;
    }
    setSubmitting(true);
    try {
      // 所有类型统一走 context+drafter 派生路径：表单收的是 drafter 输入（对象名/业务
      // 逻辑/目标源），真正落库的 spec（sync 的 source/target、transform 的结构化清洗规则、
      // materialize/transform 的 ontology_id 等）由 drafter 派生补全。此前 sync/transform/
      // materialize 把表单原样当 spec 直填，缺 drafter 派生的必填字段，一律过不了校验闸门。
      // user_created=true：表单是用户发起，溯源标 user（区别于对话/机器起草的 machine）。
      const created = await api.draftArtifact({
        kind: effectiveKind,
        name: draftName || undefined,
        intent: draftName || undefined,
        ontology_id: draftOntologyId ?? null,
        context: { ontology_id: draftOntologyId, ...specDraft },
        user_created: true,
      });
      setCreateOpen(false);
      await load();
      setDetail(created);
    } catch (err) {
      handleError(err, "起草失败");
    } finally {
      setSubmitting(false);
    }
  };

  const domainName = (domainContextId: string): string => {
    const d = domains.find((x) => x.id === domainContextId);
    return d?.name ?? domainContextId;
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
    // kind 固定时（类型专属页）隐藏冗余的「类型」列
    ...(kind
      ? []
      : [
          {
            title: "类型",
            dataIndex: "kind",
            key: "kind",
            render: (k: string) => KIND_LABEL[k] ?? k,
          } as ColumnsType<GovernanceArtifact>[number],
        ]),
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
      title={kind ? `${KIND_SHORT_LABEL[kind] ?? kind}制品` : "治理智能体制品"}
      icon={<RobotOutlined />}
      count={rows.length}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={forbidden}
            onClick={openCreate}
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
        onOk={() => void create()}
        confirmLoading={submitting}
        onCancel={() => setCreateOpen(false)}
        okText="起草"
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              制品类型
            </Text>
            <Select
              style={{ width: "100%", marginTop: 4 }}
              value={effectiveKind}
              disabled={Boolean(kind)}
              onChange={(v) => {
                setDraftKind(v);
                setSpecDraft({});
              }}
              options={(kinds?.all_kinds ?? Object.keys(KIND_LABEL)).map((k) => ({
                value: k,
                label:
                  (KIND_LABEL[k] ?? k) +
                  (kinds && !kinds.registered.includes(k) ? "（未实现）" : "") +
                  (kinds?.high_risk.includes(k) ? " · 高危" : ""),
              }))}
            />
          </div>

          {effectiveKind !== "cluster" && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                本体
              </Text>
              <Select
                style={{ width: "100%", marginTop: 4 }}
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择要绑定的本体"
                value={draftOntologyId}
                onChange={(v) => setDraftOntologyId(v)}
                options={ontologies.map((o) => ({
                  value: o.id,
                  label: `${domainName(o.domain_context_id)} · v${o.version}（${o.status}）`,
                }))}
              />
            </div>
          )}

          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              名称（可选，留空自动命名）
            </Text>
            <Input
              style={{ marginTop: 4 }}
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              placeholder="制品名称"
            />
          </div>

          <SpecForm
            kind={effectiveKind}
            mode="manual"
            value={specDraft}
            ontologyId={draftOntologyId}
            onChange={(k, v) =>
              setSpecDraft((prev) => ({ ...prev, [k]: v }))
            }
          />
        </Space>
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
