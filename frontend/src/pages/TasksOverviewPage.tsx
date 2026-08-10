import { ProfileOutlined, ReloadOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Space, Table, Tag, Tabs, Select, Input, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { SectionCard } from "../components/SectionCard";
import { ArtifactDetail } from "../components/AgentsPanel";
import type { GovernanceArtifact, OntologySummary, DomainContext } from "../types";

const { Search } = Input;

const KIND_LABEL: Record<string, string> = {
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

const STATUS_LABEL: Record<string, string> = {
  drafted: "草稿",
  validated: "已校验",
  confirmed: "已确认",
  executing: "执行中",
  succeeded: "成功",
  failed: "失败",
};

/**
 * 任务中心 - 统一入口
 *
 * 整合所有任务类型（物化/同步/加工/指标）到一个页面，通过 Tab、筛选器和搜索来查找。
 * 取代原来分散的 4 个独立类型页，提供更好的整体视图和操作体验。
 */
export function TasksOverviewPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [rows, setRows] = useState<GovernanceArtifact[]>([]);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);

  // 筛选状态
  const activeKind = searchParams.get("kind") || "all";
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [ontologyFilter, setOntologyFilter] = useState<string | undefined>(undefined);
  const [searchText, setSearchText] = useState<string>("");

  // 本体列表（用于筛选器）
  const [ontologies, setOntologies] = useState<OntologySummary[]>([]);
  const [domains, setDomains] = useState<DomainContext[]>([]);

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
      const filterKind = activeKind === "all" ? undefined : activeKind;
      const artifacts = await api.listArtifacts(filterKind ? { kind: filterKind } : undefined);
      setRows(artifacts);
      setForbidden(false);
    } catch (err) {
      handleError(err, "加载失败");
    } finally {
      setLoading(false);
    }
  }, [handleError, activeKind]);

  useEffect(() => {
    void load();
  }, [load]);

  // 加载本体列表（用于筛选）
  useEffect(() => {
    Promise.all([api.listOntologies(), api.listDomains()])
      .then(([onts, doms]) => {
        setOntologies(onts);
        setDomains(doms);
      })
      .catch(() => {
        /* 筛选器数据加载失败不阻断主流程 */
      });
  }, []);

  const domainName = useCallback(
    (domainContextId: string): string => {
      const d = domains.find((x) => x.id === domainContextId);
      return d?.name ?? domainContextId;
    },
    [domains],
  );

  const ontologyName = useCallback(
    (ontologyId: string): string => {
      const o = ontologies.find((x) => x.id === ontologyId);
      if (!o) return ontologyId;
      return `${domainName(o.domain_context_id)} v${o.version}`;
    },
    [ontologies, domainName],
  );

  // 客户端筛选
  const filteredRows = useMemo(() => {
    return rows.filter((row) => {
      // 状态筛选
      if (statusFilter && row.status !== statusFilter) return false;

      // 本体筛选
      if (ontologyFilter && row.ontology_id !== ontologyFilter) return false;

      // 搜索文本
      if (searchText) {
        const text = searchText.toLowerCase();
        const matchName = row.name?.toLowerCase().includes(text);
        const matchIntent = row.intent?.toLowerCase().includes(text);
        if (!matchName && !matchIntent) return false;
      }

      return true;
    });
  }, [rows, statusFilter, ontologyFilter, searchText]);

  const refreshDetail = useCallback(async (id: string) => {
    try {
      setDetail(await api.getArtifact(id));
    } catch {
      /* 详情刷新失败不打断主流程 */
    }
  }, []);

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
      width: 100,
      render: (k: string) => <Tag color="blue">{KIND_LABEL[k] ?? k}</Tag>,
    },
    {
      title: "任务名称",
      dataIndex: "name",
      key: "name",
      ellipsis: true,
    },
    {
      title: "本体",
      dataIndex: "ontology_id",
      key: "ontology_id",
      width: 180,
      ellipsis: true,
      render: (id: string | null) => (id ? ontologyName(id) : "—"),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string, row) => {
        const isLive = row.live_state?.live_state && !row.live_state?.terminal;
        return (
          <Space size={4}>
            <Tag color={STATUS_COLOR[status] ?? "default"}>{STATUS_LABEL[status] ?? status}</Tag>
            {isLive && <Tag color="blue">运行中</Tag>}
            {row.is_high_risk && <Tag color="volcano">高危</Tag>}
          </Space>
        );
      },
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (v: string) =>
        new Date(v).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        }),
    },
    {
      title: "操作",
      key: "actions",
      width: 88,
      fixed: "right",
      render: (_, row) => (
        <Button size="small" onClick={() => setDetail(row)}>
          查看
        </Button>
      ),
    },
  ];

  const tabItems = [
    { key: "all", label: `全部 (${rows.length})` },
    { key: "materialize", label: KIND_LABEL.materialize },
    { key: "sync", label: KIND_LABEL.sync },
    { key: "transform", label: KIND_LABEL.transform },
    { key: "metric", label: KIND_LABEL.metric },
  ];

  const handleTabChange = (key: string) => {
    setSearchParams(key === "all" ? {} : { kind: key });
    setStatusFilter(undefined);
    setOntologyFilter(undefined);
    setSearchText("");
  };

  const handleCreateTask = () => {
    // 根据当前 Tab 决定创建什么类型的任务
    const targetKind = activeKind === "all" ? "materialize" : activeKind;
    navigate(`/tasks/create?kind=${targetKind}`);
  };

  return (
    <PageContainer>
      <PageHeader
        icon={<ProfileOutlined />}
        title="我的任务"
        description="管理所有数据作业任务，包括物化、同步、加工和指标任务。"
      />

      <SectionCard
        title="任务列表"
        icon={<ProfileOutlined />}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()} />
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={forbidden}
              onClick={handleCreateTask}
            >
              创建任务
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Tabs activeKey={activeKind} items={tabItems} onChange={handleTabChange} />

          <Space wrap>
            <Search
              placeholder="搜索任务名称或描述"
              allowClear
              style={{ width: 240 }}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
            />

            <Select
              placeholder="筛选状态"
              allowClear
              style={{ width: 120 }}
              value={statusFilter}
              onChange={setStatusFilter}
              options={[
                { label: "草稿", value: "drafted" },
                { label: "已校验", value: "validated" },
                { label: "已确认", value: "confirmed" },
                { label: "执行中", value: "executing" },
                { label: "成功", value: "succeeded" },
                { label: "失败", value: "failed" },
              ]}
            />

            <Select
              placeholder="筛选本体"
              allowClear
              showSearch
              optionFilterProp="label"
              style={{ width: 200 }}
              value={ontologyFilter}
              onChange={setOntologyFilter}
              options={ontologies.map((o) => ({
                value: o.id,
                label: ontologyName(o.id),
              }))}
            />
          </Space>

          <Table
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={filteredRows}
            pagination={{
              showSizeChanger: true,
              showQuickJumper: true,
              showTotal: (total) => `共 ${total} 个任务`,
              defaultPageSize: 20,
              pageSizeOptions: ["10", "20", "50", "100"],
            }}
          />
        </Space>
      </SectionCard>

      <ArtifactDetail
        artifact={detail}
        busy={busy}
        onClose={() => setDetail(null)}
        onStep={runStep}
        onEdit={(a) => navigate(`/tasks/${a.id}/edit`)}
        ontologyName={ontologyName}
      />
    </PageContainer>
  );
}
