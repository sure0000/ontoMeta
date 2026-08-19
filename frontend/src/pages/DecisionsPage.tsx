/**
 * 决策追踪页（P2）。
 *
 * Data Agent 的确认散落在一次次对话里，看完就翻不回去了。这一页把它们摊平成一张
 * 可筛可查的表：谁、在哪一环、机器提了什么、人最终定了什么。
 *
 * **只读**。账本是观察层——执行门槛的唯一权威仍是 `GovernanceArtifact.status`，
 * 这一页不提供任何改判、补记、删除的入口，免得它被当成第二个审批台。
 */
import { HistoryOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Drawer, Empty, Select, Space, Table, Tag, Tooltip, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { SectionCard } from "../components/SectionCard";
import { ClosureCard } from "./chat-bi/ClosureCard";
import {
  NODE_LABEL,
  NODE_SEQUENCE,
  OUTCOME_COLOR,
  OUTCOME_LABEL,
} from "./chat-bi/decisionMeta";
import type { ChatBiDecision, ChatBiDecisionClosure } from "../types";

/** 产物类型 → 中文名。软引用，后端不设枚举，故未知值原样显示。 */
const REF_KIND_LABEL: Record<string, string> = {
  artifact: "数据任务",
  business_logic: "业务口径",
  data_app: "数据应用",
  datasource: "数据源",
  domain: "数据域",
  ontology: "本体",
  object_type: "对象",
  pipeline: "任务链",
  preference: "本域约定",
};

function fmtTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/** JSON 值转成一段可读文本，供抽屉里对照「机器提的 vs 人定的」。 */
function jsonText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function DecisionsPage() {
  const [rows, setRows] = useState<ChatBiDecision[]>([]);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [nodeFilter, setNodeFilter] = useState<string | undefined>();
  const [outcomeFilter, setOutcomeFilter] = useState<string | undefined>();
  const [subjectFilter, setSubjectFilter] = useState<string | undefined>();

  // 抽屉：某条记录所属会话的完整闭环。列表回答「发生过什么」，抽屉回答「这一次走完没有」。
  const [openConv, setOpenConv] = useState<ChatBiDecision | null>(null);
  const [closure, setClosure] = useState<ChatBiDecisionClosure | null>(null);
  const [closureLoading, setClosureLoading] = useState(false);

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
      // 筛选走服务端：账本是长期累积的，全量拉回来在前端过滤迟早拉爆。
      const data = await api.searchChatBiDecisions({
        node: nodeFilter,
        outcome: outcomeFilter,
        subject_id: subjectFilter,
        limit: 500,
      });
      setRows(data);
      setForbidden(false);
    } catch (err) {
      handleError(err, "加载决策记录失败");
    } finally {
      setLoading(false);
    }
  }, [nodeFilter, outcomeFilter, subjectFilter, handleError]);

  useEffect(() => {
    void load();
  }, [load]);

  const openClosure = useCallback(
    async (record: ChatBiDecision) => {
      setOpenConv(record);
      setClosure(null);
      setClosureLoading(true);
      try {
        setClosure(await api.getChatBiClosure(record.conversation_id));
      } catch (err) {
        handleError(err, "加载会话闭环失败");
      } finally {
        setClosureLoading(false);
      }
    },
    [handleError],
  );

  // 责任人下拉从**当前结果集**归纳而来：没有用户目录接口可查，硬编码一份又会立刻过期。
  // 代价是筛掉某人后下拉里只剩他自己——故恒插一个「全部」清空项。
  const subjectOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of rows) {
      if (r.subject_id) seen.set(r.subject_id, r.subject_role ?? r.subject_id);
    }
    return [...seen].map(([value, role]) => ({ value, label: `${value}（${role}）` }));
  }, [rows]);

  const columns: ColumnsType<ChatBiDecision> = [
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (v: string | null) => fmtTime(v),
    },
    {
      title: "会话",
      key: "conversation",
      ellipsis: true,
      // **刻意不做成跳回对话的链接**：Data Agent 页的会话列表按 URL 里的 domains 作用域
      // 拉取，而决策记录不带域信息——链过去多半落进一个看不到该会话的作用域，比不给链接更糟。
      // 「看这次对话定了什么」由右侧抽屉的闭环时间线回答。
      render: (_, r) => (
        <Tooltip title={r.conversation_id}>{r.conversation_title || r.conversation_id}</Tooltip>
      ),
    },
    {
      title: "环节",
      dataIndex: "node",
      key: "node",
      width: 120,
      render: (v: string) => NODE_LABEL[v] ?? v,
    },
    {
      title: "结果",
      dataIndex: "outcome",
      key: "outcome",
      width: 110,
      render: (v: string) => <Tag color={OUTCOME_COLOR[v] ?? "default"}>{OUTCOME_LABEL[v] ?? v}</Tag>,
    },
    { title: "摘要", dataIndex: "summary", key: "summary", ellipsis: true },
    {
      title: "改动字段",
      dataIndex: "overridden_fields",
      key: "overridden_fields",
      width: 160,
      render: (fields: string[]) =>
        fields?.length ? (
          <Tooltip title={fields.join("、")}>
            <span className="om-muted">{fields.length} 项</span>
          </Tooltip>
        ) : (
          <span className="om-muted">原样接受</span>
        ),
    },
    {
      title: "产物",
      key: "ref",
      width: 130,
      render: (_, r) =>
        r.ref_kind ? (
          <Tooltip title={r.ref_id ?? ""}>{REF_KIND_LABEL[r.ref_kind] ?? r.ref_kind}</Tooltip>
        ) : (
          <span className="om-muted">—</span>
        ),
    },
    {
      title: "责任人",
      key: "subject",
      width: 150,
      // subject_id 为空是**共享 admin token 的正常结果**（resolve_principal 不查库），
      // 不是没记上——写「未具名」而不是「—」，免得被当成 bug 追。
      render: (_, r) => r.subject_id ?? <span className="om-muted">未具名（{r.subject_role ?? "?"}）</span>,
    },
    {
      title: "操作",
      key: "actions",
      width: 96,
      fixed: "right",
      render: (_, r) => (
        <Button size="small" onClick={() => void openClosure(r)}>
          看闭环
        </Button>
      ),
    },
  ];

  if (forbidden) {
    return (
      <PageContainer>
        <Alert type="warning" showIcon message="当前账号无权查看决策记录" />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        icon={<HistoryOutlined />}
        title="决策追踪"
        description="Data Agent 对话里人在需求 / 本体 / 数据 / 执行方案 / 执行任务 / 结果六个环节拍的板。只读留痕，不改变任何执行权限。"
      />
      <SectionCard
        title="决策记录"
        icon={<HistoryOutlined />}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Space wrap>
            <Select
              placeholder="全部环节"
              allowClear
              style={{ width: 160 }}
              value={nodeFilter}
              onChange={setNodeFilter}
              options={NODE_SEQUENCE.map((n) => ({ value: n.value, label: n.label }))}
            />
            <Select
              placeholder="全部结果"
              allowClear
              style={{ width: 150 }}
              value={outcomeFilter}
              onChange={setOutcomeFilter}
              options={Object.entries(OUTCOME_LABEL).map(([value, label]) => ({ value, label }))}
            />
            <Select
              placeholder="全部责任人"
              allowClear
              showSearch
              style={{ width: 220 }}
              value={subjectFilter}
              onChange={setSubjectFilter}
              options={subjectOptions}
            />
          </Space>
          <Table
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={rows}
            scroll={{ x: 1200 }}
            locale={{ emptyText: <Empty description="还没有决策留痕" /> }}
            pagination={{
              showSizeChanger: true,
              defaultPageSize: 20,
              pageSizeOptions: ["10", "20", "50", "100"],
              showTotal: (total) => `共 ${total} 条`,
            }}
          />
        </Space>
      </SectionCard>

      <Drawer
        width={720}
        open={!!openConv}
        onClose={() => setOpenConv(null)}
        title={openConv?.conversation_title || "会话确认闭环"}
      >
        {closureLoading ? (
          <div className="om-muted">加载中…</div>
        ) : closure ? (
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            <ClosureCard closure={closure} />
            <div>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>决策时间线</div>
              {closure.records.map((rec) => (
                <div key={rec.id} className="om-decision-item">
                  <div className="om-decision-item-head">
                    <Tag color={OUTCOME_COLOR[rec.outcome] ?? "default"}>
                      {OUTCOME_LABEL[rec.outcome] ?? rec.outcome}
                    </Tag>
                    <span>{NODE_LABEL[rec.node] ?? rec.node}</span>
                    <span className="om-muted">{fmtTime(rec.created_at)}</span>
                  </div>
                  {rec.summary && <div>{rec.summary}</div>}
                  {rec.overridden_fields.length > 0 && (
                    <div className="om-muted">改动字段：{rec.overridden_fields.join("、")}</div>
                  )}
                  {/* 只在人改过时才并排展示两份——原样接受时贴两段一样的 JSON 是纯噪声 */}
                  {rec.overridden_fields.length > 0 && (
                    <div className="om-decision-diff">
                      <pre className="code-block--light code-block--bounded">
                        机器提案{"\n"}
                        {jsonText(rec.proposed)}
                      </pre>
                      <pre className="code-block--light code-block--bounded">
                        人最终定的{"\n"}
                        {jsonText(rec.chosen)}
                      </pre>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Space>
        ) : (
          <Empty description="无法加载该会话的闭环" />
        )}
      </Drawer>
    </PageContainer>
  );
}
