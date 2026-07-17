import {
  ApartmentOutlined,
  CheckCircleOutlined,
  DeploymentUnitOutlined,
  ExportOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Alert, Button, Modal, Progress, Space, Spin, Table, message } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { OntologyWorkspaceView } from "../components/OntologyWorkspaceView";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { useApi } from "../hooks/useApi";
import { getOntologyDomainStatusVisual } from "../utils/statusVisual";
import type {
  DomainContextDetail,
  DraftProgress,
  ObjectTypeSummary,
  OntologyGraph,
  RelationType,
  VersionDiff,
  VersionRecord,
} from "../types";

const DEFAULT_PAGE_SIZE = 20;

function mergeOntologyGraph(base: OntologyGraph | null, extra: OntologyGraph): OntologyGraph {
  const nodeMap = new Map((base?.nodes ?? []).map((n) => [n.id, n]));
  for (const n of extra.nodes) nodeMap.set(n.id, n);
  const edgeMap = new Map((base?.edges ?? []).map((e) => [e.id, e]));
  for (const e of extra.edges) edgeMap.set(e.id, e);
  return {
    nodes: [...nodeMap.values()],
    edges: [...edgeMap.values()],
    center_id: extra.center_id ?? base?.center_id,
    depth: extra.depth ?? base?.depth,
    truncated: Boolean(extra.truncated || base?.truncated),
    total_object_count: extra.total_object_count ?? base?.total_object_count ?? nodeMap.size,
    total_relation_count: extra.total_relation_count ?? base?.total_relation_count,
  };
}

type DomainBundle = {
  domain: DomainContextDetail;
  objects: ObjectTypeSummary[];
  objectTotal: number;
  relations: RelationType[];
  relationTotal: number;
  graph: OntologyGraph | null;
};

async function fetchOntologyLists(
  ontologyId: string,
  opts: {
    objectPage: number;
    relationPage: number;
    pageSize: number;
    q?: string;
    /** 传入已有图谱时跳过重新拉取，保留邻域展开结果 */
    existingGraph?: OntologyGraph | null;
  },
): Promise<Omit<DomainBundle, "domain">> {
  const objectOffset = (opts.objectPage - 1) * opts.pageSize;
  const relationOffset = (opts.relationPage - 1) * opts.pageSize;
  const listPromises = [
    api.listObjectTypes({
      ontologyId,
      q: opts.q || undefined,
      limit: opts.pageSize,
      offset: objectOffset,
    }),
    api.listRelationTypes({
      ontologyId,
      q: opts.q || undefined,
      limit: opts.pageSize,
      offset: relationOffset,
    }),
  ] as const;
  if (opts.existingGraph) {
    const [objectsPage, relationsPage] = await Promise.all(listPromises);
    return {
      objects: objectsPage.items,
      objectTotal: objectsPage.total,
      relations: relationsPage.items,
      relationTotal: relationsPage.total,
      graph: opts.existingGraph,
    };
  }
  const [objectsPage, relationsPage, graph] = await Promise.all([
    ...listPromises,
    api.getOntologyGraph(ontologyId, { depth: 1 }),
  ]);
  return {
    objects: objectsPage.items,
    objectTotal: objectsPage.total,
    relations: relationsPage.items,
    relationTotal: relationsPage.total,
    graph,
  };
}

async function fetchDomainBundle(
  domainId: string,
  opts: {
    objectPage: number;
    relationPage: number;
    pageSize: number;
    q?: string;
    existingGraph?: OntologyGraph | null;
    existingOntologyId?: string | null;
  },
): Promise<DomainBundle> {
  const domain = await api.getDomain(domainId);
  if (!domain.latest_ontology_id) {
    return {
      domain,
      objects: [],
      objectTotal: 0,
      relations: [],
      relationTotal: 0,
      graph: null,
    };
  }
  const reuseGraph =
    opts.existingOntologyId === domain.latest_ontology_id ? opts.existingGraph : null;
  const lists = await fetchOntologyLists(domain.latest_ontology_id, {
    ...opts,
    existingGraph: reuseGraph,
  });
  return { domain, ...lists };
}

export function DomainDetailPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const [objectPage, setObjectPage] = useState(1);
  const [relationPage, setRelationPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [graphExpanding, setGraphExpanding] = useState(false);
  const graphCacheRef = useRef<{ ontologyId: string; graph: OntologyGraph } | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  useEffect(() => {
    setObjectPage(1);
    setRelationPage(1);
  }, [debouncedQ, domainId]);

  const {
    data: bundle,
    loading,
    error: loadError,
    setData: setBundle,
  } = useApi<DomainBundle>(
    async () => {
      if (!domainId) throw new Error("缺少数据域 ID");
      const cached = graphCacheRef.current;
      const result = await fetchDomainBundle(domainId, {
        objectPage,
        relationPage,
        pageSize,
        q: debouncedQ,
        existingGraph: cached?.graph ?? null,
        existingOntologyId: cached?.ontologyId ?? null,
      });
      if (result.domain.latest_ontology_id && result.graph) {
        graphCacheRef.current = {
          ontologyId: result.domain.latest_ontology_id,
          graph: result.graph,
        };
      }
      return result;
    },
    [domainId, objectPage, relationPage, pageSize, debouncedQ],
  );

  const domain = bundle?.domain ?? null;
  const objects = bundle?.objects ?? [];
  const relations = bundle?.relations ?? [];
  const graph = bundle?.graph ?? null;
  const objectTotal = bundle?.objectTotal ?? 0;
  const relationTotal = bundle?.relationTotal ?? 0;

  const [generating, setGenerating] = useState(false);
  const [draftProgress, setDraftProgress] = useState<DraftProgress | null>(null);
  const [ontologyLoading, setOntologyLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [selectedDiff, setSelectedDiff] = useState<VersionDiff | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completionHandledRef = useRef<string | null>(null);

  const error = actionError || loadError;

  const loadOntology = useCallback(
    async (ontologyId: string) => {
      setOntologyLoading(true);
      try {
        graphCacheRef.current = null;
        const lists = await fetchOntologyLists(ontologyId, {
          objectPage: 1,
          relationPage: 1,
          pageSize,
          q: debouncedQ,
        });
        if (lists.graph) {
          graphCacheRef.current = { ontologyId, graph: lists.graph };
        }
        setObjectPage(1);
        setRelationPage(1);
        setBundle((prev) => (prev ? { ...prev, ...lists } : prev));
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "加载本体失败");
      } finally {
        setOntologyLoading(false);
      }
    },
    [setBundle, pageSize, debouncedQ],
  );

  const handleExpandGraphNode = useCallback(
    async (objectId: string) => {
      const ontologyId = domain?.latest_ontology_id;
      if (!ontologyId) return;
      setGraphExpanding(true);
      try {
        const neighborhood = await api.getOntologyGraph(ontologyId, {
          centerId: objectId,
          depth: 1,
        });
        setBundle((prev) => {
          if (!prev) return prev;
          const merged = mergeOntologyGraph(prev.graph, neighborhood);
          graphCacheRef.current = { ontologyId, graph: merged };
          return { ...prev, graph: merged };
        });
      } catch (err) {
        message.error(err instanceof Error ? err.message : "展开邻域失败");
      } finally {
        setGraphExpanding(false);
      }
    },
    [domain?.latest_ontology_id, setBundle],
  );

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const pollProgress = useCallback(
    (taskId: string) => {
      stopPolling();
      completionHandledRef.current = null;

      const pollOnce = async () => {
        try {
          const p = await api.getProgress(domainId!);
          if (p.task_id !== taskId) return;
          setDraftProgress(p);

          if (
            p.status === "succeeded" ||
            p.status === "completed" ||
            p.status === "failed" ||
            p.status === "cancelled"
          ) {
            if (completionHandledRef.current === taskId) return;
            completionHandledRef.current = taskId;
            stopPolling();
            setGenerating(false);

            if (
              (p.status === "succeeded" || p.status === "completed") &&
              p.ontology_id
            ) {
              const updated = await api.getDomain(domainId!);
              setBundle((prev) => (prev ? { ...prev, domain: updated } : prev));
              await loadOntology(p.ontology_id);
              message.success("本体草稿生成完成");
            } else if (p.status === "failed") {
              setActionError(p.message || "生成失败");
            } else if (p.status === "cancelled") {
              message.info(p.message || "草稿生成已停止");
            }
            return;
          }

          pollRef.current = setTimeout(pollOnce, 2000);
        } catch {
          stopPolling();
          setGenerating(false);
          setActionError("获取进度失败");
        }
      };

      void pollOnce();
    },
    [domainId, loadOntology, setBundle, stopPolling],
  );

  useEffect(() => () => stopPolling(), [stopPolling]);

  const handleGenerate = () => {
    if (!domainId) return;
    Modal.confirm({
      title: "确认生成本体草稿",
      content: "将根据 DataHub 元数据重新生成本体草稿，已有草稿内容将被覆盖。",
      okText: "确认生成",
      cancelText: "取消",
      onOk: async () => {
        setGenerating(true);
        setActionError(null);
        setDraftProgress(null);
        try {
          const result = await api.generateDraft(domainId);
          setDraftProgress(result);
          pollProgress(result.task_id);
        } catch (err) {
          setGenerating(false);
          setActionError(err instanceof Error ? err.message : "生成失败");
        }
      },
    });
  };

  const handlePublish = () => {
    if (!domain?.latest_ontology_id || !domainId) return;

    Modal.confirm({
      title: "确认发布本体",
      content:
        "发布后将把当前草稿固化为正式版本，对外在本体页与业务逻辑页展示。此操作需要二次确认。",
      okText: "确认发布",
      cancelText: "取消",
      onOk: async () => {
        try {
          setActionError(null);
          const validation = await api.validateOntology(domain.latest_ontology_id!);
          if (!validation.ok) {
            const msg = validation.issues.map((i) => i.message).join("；");
            setActionError(msg || "一致性校验失败");
            message.error(msg || "一致性校验失败");
            throw new Error(msg || "一致性校验失败");
          }
          const confirmation = await api.createConfirmation({
            ontology_id: domain.latest_ontology_id!,
            target_type: "ontology",
            action_type: "publish",
            reason: "工作区发布确认",
          });
          await api.confirmAction(confirmation.id);
          const updated = await api.getDomain(domainId);
          setBundle((prev) => (prev ? { ...prev, domain: updated } : prev));
          message.success("发布成功");
        } catch (err) {
          const msg = err instanceof Error ? err.message : "发布失败";
          setActionError(msg);
          message.error(msg);
          throw err;
        }
      },
    });
  };

  const openVersionHistory = async () => {
    if (!domain?.published_ontology_id) return;
    setVersionsOpen(true);
    setVersionsLoading(true);
    setSelectedDiff(null);
    try {
      const items = await api.listOntologyVersions(domain.published_ontology_id);
      setVersions(items);
      if (items[0]?.has_diff) {
        const diff = await api.getOntologyVersionDiff(
          domain.published_ontology_id,
          items[0].version,
        );
        setSelectedDiff(diff);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载版本失败");
    } finally {
      setVersionsLoading(false);
    }
  };

  if (loading) return <PageSkeleton type="detail" />;

  if (!domain) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "数据域不存在"} showIcon />
      </PageContainer>
    );
  }

  const objectDetailPath = (objectId: string) =>
    `/workspace/${domainId}/objects/${objectId}`;
  const relationDetailPath = (relationId: string) =>
    `/workspace/${domainId}/relations/${relationId}`;

  const publishedVersion = domain.published_ontology_version;
  const ontologyStatusVisual = getOntologyDomainStatusVisual(
    domain.latest_ontology_status ?? (domain.latest_ontology_id ? "draft" : "active"),
  );

  return (
    <PageContainer full>
      <PageHeader
        icon={<DeploymentUnitOutlined />}
        title={
          <Space size={10}>
            <span>{domain.name}</span>
            {domain.latest_ontology_status && (
              <StatusBadge status={domain.latest_ontology_status} />
            )}
          </Space>
        }
        description={domain.description || "暂无描述"}
        extra={
          <Space wrap>
            {domain.datahub_url && (
              <Button
                type="default"
                href={domain.datahub_url}
                target="_blank"
                icon={<ExportOutlined />}
              >
                DataHub
              </Button>
            )}
            <Link to={`/workspace/${domainId}/executions`}>
              <Button icon={<HistoryOutlined />}>执行记录</Button>
            </Link>
            {domain.published_ontology_id && (
              <>
                <Link to={`/ontology?domain=${domainId}`}>
                  <Button icon={<ApartmentOutlined />}>查看已发布本体</Button>
                </Link>
                <Link to={`/business-logic?domain=${domainId}`}>
                  <Button>业务逻辑</Button>
                </Link>
              </>
            )}
            <Button
              type="primary"
              loading={generating}
              onClick={handleGenerate}
              icon={<ThunderboltOutlined />}
            >
              生成本体草稿
            </Button>
            {domain.latest_ontology_id && domain.latest_ontology_status === "draft" && (
              <Button onClick={handlePublish} icon={<CheckCircleOutlined />}>
                确认发布
              </Button>
            )}
          </Space>
        }
      />

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setActionError(null)}
        />
      )}

      {generating && draftProgress && (
        <div style={{ margin: "16px 0", padding: "16px 24px", background: "#f6f8fa", borderRadius: 8 }}>
          <Progress
            percent={draftProgress.progress}
            status={draftProgress.status === "failed" ? "exception" : "active"}
            strokeColor={{ from: "#108ee9", to: "#87d068" }}
          />
          <div style={{ marginTop: 4, color: "#666", fontSize: 13 }}>
            {draftProgress.message || "处理中..."}
          </div>
        </div>
      )}

      <div className="stat-row">
        <StatCard
          tone="primary"
          icon={<ApartmentOutlined />}
          label="业务对象"
          value={objectTotal}
        />
        <StatCard
          tone="success"
          icon={<DeploymentUnitOutlined />}
          label="关系数量"
          value={relationTotal}
        />
        <StatCard
          tone={ontologyStatusVisual.tone}
          icon={ontologyStatusVisual.icon}
          label="本体状态"
          value={
            <span style={{ fontSize: 16 }}>
              {domain.latest_ontology_status
                ? statusLabel(domain.latest_ontology_status)
                : "未生成"}
            </span>
          }
        />
        <StatCard
          tone="neutral"
          icon={<PlayCircleOutlined />}
          label="发布版本"
          value={
            publishedVersion ? (
              <Button type="link" style={{ padding: 0, height: "auto", fontSize: 20 }} onClick={openVersionHistory}>
                v{publishedVersion}
              </Button>
            ) : (
              "—"
            )
          }
        />
      </div>

      <Spin spinning={ontologyLoading}>
        {!domain.latest_ontology_id ? (
          <EmptyState
            title="尚未生成本体草稿"
            description="从 DataHub 拉取数据域元数据并生成本体草稿，作为后续编辑与发布的起点。"
            action={
              <Button
                type="primary"
                size="large"
                loading={generating}
                onClick={handleGenerate}
                icon={<ThunderboltOutlined />}
              >
                生成本体草稿
              </Button>
            }
          />
        ) : (
          <OntologyWorkspaceView
            objects={objects}
            relations={relations}
            graph={graph}
            objectDetailPath={objectDetailPath}
            relationDetailPath={relationDetailPath}
            workspaceMode
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            objectPaging={{
              total: objectTotal,
              page: objectPage,
              pageSize,
              onChange: (page, size) => {
                setObjectPage(page);
                setPageSize(size);
              },
            }}
            relationPaging={{
              total: relationTotal,
              page: relationPage,
              pageSize,
              onChange: (page, size) => {
                setRelationPage(page);
                setPageSize(size);
              },
            }}
            onExpandGraphNode={handleExpandGraphNode}
            graphExpanding={graphExpanding}
          />
        )}
      </Spin>

      <Modal
        title="发布版本与差异"
        open={versionsOpen}
        onCancel={() => setVersionsOpen(false)}
        footer={null}
        width={720}
      >
        <Spin spinning={versionsLoading}>
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={versions}
            columns={[
              { title: "版本", dataIndex: "version", width: 80, render: (v: number) => `v${v}` },
              { title: "摘要", dataIndex: "diff_summary", ellipsis: true },
              {
                title: "操作",
                width: 100,
                render: (_, record: VersionRecord) => (
                  <Button
                    type="link"
                    size="small"
                    disabled={!record.has_diff || !domain?.published_ontology_id}
                    onClick={async () => {
                      if (!domain?.published_ontology_id) return;
                      try {
                        const diff = await api.getOntologyVersionDiff(
                          domain.published_ontology_id,
                          record.version,
                        );
                        setSelectedDiff(diff);
                      } catch (err) {
                        message.error(err instanceof Error ? err.message : "加载差异失败");
                      }
                    }}
                  >
                    查看差异
                  </Button>
                ),
              },
            ]}
          />
          {selectedDiff && (
            <div style={{ marginTop: 16 }}>
              <Alert
                type="info"
                showIcon
                message={selectedDiff.diff_summary || `v${selectedDiff.version} 差异`}
                description={
                  <div style={{ fontSize: 13 }}>
                    <div>对象 新增 {selectedDiff.object_types.added.length} / 修改 {selectedDiff.object_types.modified.length} / 删除 {selectedDiff.object_types.removed.length}</div>
                    <div>关系 新增 {selectedDiff.relation_types.added.length} / 修改 {selectedDiff.relation_types.modified.length} / 删除 {selectedDiff.relation_types.removed.length}</div>
                    <div>逻辑 新增 {selectedDiff.business_logics.added.length} / 修改 {selectedDiff.business_logics.modified.length} / 删除 {selectedDiff.business_logics.removed.length}</div>
                    {selectedDiff.object_types.added.length > 0 && (
                      <div style={{ marginTop: 8 }}>
                        新增对象：{selectedDiff.object_types.added.map((i) => i.display_name || i.name).join("、")}
                      </div>
                    )}
                    {selectedDiff.relation_types.added.length > 0 && (
                      <div>
                        新增关系：{selectedDiff.relation_types.added.map((i) => i.display_name || i.name).join("、")}
                      </div>
                    )}
                  </div>
                }
              />
            </div>
          )}
        </Spin>
      </Modal>
    </PageContainer>
  );
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: "草稿",
    in_review: "待审",
    published: "已发布",
    pre_published: "预发布",
    archived: "已归档",
  };
  return map[status] || status;
}
