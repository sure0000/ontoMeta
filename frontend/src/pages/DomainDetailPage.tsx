import {
  ApartmentOutlined,
  AppstoreOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  DeploymentUnitOutlined,
  DownOutlined,
  EditOutlined,
  ExportOutlined,
  FileSearchOutlined,
  HistoryOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Drawer,
  Dropdown,
  Modal,
  Progress,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { OntologyWorkspaceView } from "../components/OntologyWorkspaceView";
import { ConflictsPanel } from "../components/ConflictsPanel";
import { IncrementalModelingModal } from "../components/IncrementalModelingModal";
import { ManualCreateModal } from "../components/ManualCreateModal";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import { useApi } from "../hooks/useApi";
import type {
  DomainContextDetail,
  DraftGenerationScope,
  DraftProgress,
  MergeReport,
  MergeReportSummary,
  ObjectTypeSummary,
  PublishPreflight,
  RelationType,
  VersionDiff,
  VersionRecord,
} from "../types";

const DEFAULT_PAGE_SIZE = 20;

const GENERATION_SCOPES: DraftGenerationScope[] = ["full", "objects", "relations"];

const SCOPE_LABEL: Record<DraftGenerationScope, string> = {
  full: "本体草稿",
  objects: "业务对象",
  relations: "业务关系",
};

type DomainBundle = {
  domain: DomainContextDetail;
  objects: ObjectTypeSummary[];
  objectTotal: number;
  relations: RelationType[];
  relationTotal: number;
};

async function fetchOntologyLists(
  ontologyId: string,
  opts: {
    objectPage: number;
    relationPage: number;
    pageSize: number;
    q?: string;
    roleIn?: string[];
    needsReview?: boolean;
  },
): Promise<Omit<DomainBundle, "domain">> {
  const objectOffset = (opts.objectPage - 1) * opts.pageSize;
  const relationOffset = (opts.relationPage - 1) * opts.pageSize;
  const [objectsPage, relationsPage] = await Promise.all([
    api.listObjectTypes({
      ontologyId,
      q: opts.q || undefined,
      roleIn: opts.roleIn,
      needsReview: opts.needsReview,
      limit: opts.pageSize,
      offset: objectOffset,
    }),
    api.listRelationTypes({
      ontologyId,
      q: opts.q || undefined,
      limit: opts.pageSize,
      offset: relationOffset,
    }),
  ]);
  return {
    objects: objectsPage.items,
    objectTotal: objectsPage.total,
    relations: relationsPage.items,
    relationTotal: relationsPage.total,
  };
}

async function fetchDomainBundle(
  domainId: string,
  opts: {
    objectPage: number;
    relationPage: number;
    pageSize: number;
    q?: string;
    roleIn?: string[];
    needsReview?: boolean;
  },
): Promise<DomainBundle> {
  const domain = await api.getDomain(domainId);
  if (!domain.working_ontology_id) {
    return {
      domain,
      objects: [],
      objectTotal: 0,
      relations: [],
      relationTotal: 0,
    };
  }
  const lists = await fetchOntologyLists(domain.working_ontology_id, opts);
  return { domain, ...lists };
}

/** 发布前自检摘要：将发布什么、将跳过什么、为什么。 */
function PublishPreflightSummary({ preflight }: { preflight: PublishPreflight }) {
  const skipped = [
    preflight.skipped_needs_review > 0 && `待复核业务对象 ${preflight.skipped_needs_review}`,
    preflight.skipped_non_business > 0 && `非业务对象 ${preflight.skipped_non_business}`,
    preflight.skipped_relation_endpoint > 0 &&
      `端点未发布的关系 ${preflight.skipped_relation_endpoint}`,
  ].filter(Boolean) as string[];

  return (
    <div style={{ fontSize: 13, lineHeight: 1.9 }}>
      <div>
        版本 v{preflight.current_version} → <strong>v{preflight.next_version}</strong>
      </div>
      <div>
        将发布：业务对象 <strong>{preflight.object_count}</strong> · 属性 {preflight.property_count}{" "}
        · 业务关系 {preflight.relation_count}
      </div>
      {skipped.length > 0 && <div>将跳过：{skipped.join(" · ")}</div>}
      {preflight.unresolved_conflicts > 0 && (
        <div>未解决字段冲突：{preflight.unresolved_conflicts}（保持人工值，不阻断发布）</div>
      )}
      {preflight.object_count === 0 && (
        <Alert
          style={{ marginTop: 8 }}
          type="warning"
          showIcon
          message="本次不会提升任何业务对象"
          description="发布后本体浏览页仍会是空的。先在工作区把对象标为已确认（或改判角色）再发布。"
        />
      )}
    </div>
  );
}

/** 一次生成到底改了什么——机器动过的地方在这里一次性看完。 */
function MergeReportDrawer({
  report,
  open,
  onClose,
}: {
  report: MergeReport | null;
  open: boolean;
  onClose: () => void;
}) {
  if (!report) return null;
  const s = report.summary;
  const sections: { key: keyof MergeReportSummary; label: string; hint: string }[] = [
    { key: "added", label: "新增", hint: "上游新出现，已入库" },
    { key: "updated", label: "更新", hint: "人没动过，采纳了机器新值" },
    { key: "kept", label: "保留", hint: "人工值优先，机器未改动" },
    { key: "conflict", label: "冲突", hint: "双方都改，待你裁决" },
    { key: "removed", label: "上游消失", hint: "源表已不在" },
  ];
  return (
    <Drawer title="本次生成变更报告" open={open} onClose={onClose} width={560}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
          {sections.map((sec) => (
            <div
              key={sec.key}
              style={{
                flex: "1 1 90px",
                padding: "10px 12px",
                border: "1px solid var(--om-border)",
                borderRadius: 8,
              }}
            >
              <div style={{ fontSize: 20, fontWeight: 500 }}>{s[sec.key]}</div>
              <div style={{ fontSize: 12, color: "var(--om-text-secondary)" }}>{sec.label}</div>
            </div>
          ))}
        </div>
        {sections.map((sec) => (
          <div key={sec.key} style={{ fontSize: 13 }}>
            <div style={{ fontWeight: 500, marginBottom: 4 }}>
              {sec.label}
              <span style={{ color: "var(--om-text-secondary)", fontWeight: 400 }}>
                {" "}
                — {sec.hint}
              </span>
            </div>
            {(["object_types", "relation_types", "properties"] as const).map((entity) => {
              const items = report[entity]?.[sec.key] ?? [];
              if (!items.length) return null;
              const label =
                entity === "object_types" ? "对象" : entity === "relation_types" ? "关系" : "属性";
              return (
                <div key={entity} style={{ color: "var(--om-text-secondary)" }}>
                  {label}（{items.length}）：
                  {items
                    .slice(0, 8)
                    .map((i) => i.display_name || i.name)
                    .join("、")}
                  {items.length > 8 && ` 等 ${items.length} 项`}
                </div>
              );
            })}
          </div>
        ))}
      </Space>
    </Drawer>
  );
}

export function DomainDetailPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const [objectPage, setObjectPage] = useState(1);
  const [relationPage, setRelationPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  // 默认 Tab 为「业务对象」：初始就按该角色过滤，避免首帧拉到全部角色（含关系表）后再收窄。
  const [typeFilter, setTypeFilter] = useState<string[]>(["business_object"]);
  const [needsReviewOnly, setNeedsReviewOnly] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  useEffect(() => {
    setObjectPage(1);
    setRelationPage(1);
  }, [debouncedQ, domainId]);

  useEffect(() => {
    setObjectPage(1);
  }, [typeFilter, needsReviewOnly]);

  const {
    data: bundle,
    loading,
    error: loadError,
    setData: setBundle,
    reload: reloadBundle,
  } = useApi<DomainBundle>(async () => {
    if (!domainId) throw new Error("缺少数据域 ID");
    return fetchDomainBundle(domainId, {
      objectPage,
      relationPage,
      pageSize,
      q: debouncedQ,
      roleIn: typeFilter.length ? typeFilter : undefined,
      needsReview: needsReviewOnly || undefined,
    });
  }, [domainId, objectPage, relationPage, pageSize, debouncedQ, typeFilter, needsReviewOnly]);

  const domain = bundle?.domain ?? null;
  const objects = bundle?.objects ?? [];
  const relations = bundle?.relations ?? [];
  const objectTotal = bundle?.objectTotal ?? 0;
  const relationTotal = bundle?.relationTotal ?? 0;

  const [generating, setGenerating] = useState<Record<DraftGenerationScope, boolean>>({
    full: false,
    objects: false,
    relations: false,
  });
  const [draftProgress, setDraftProgress] = useState<
    Record<DraftGenerationScope, DraftProgress | null>
  >({ full: null, objects: null, relations: null });
  const [ontologyLoading, setOntologyLoading] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [incrementalOpen, setIncrementalOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [mergeReport, setMergeReport] = useState<MergeReport | null>(null);
  const [mergeReportOpen, setMergeReportOpen] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [selectedDiff, setSelectedDiff] = useState<VersionDiff | null>(null);
  const pollRefs = useRef<Record<DraftGenerationScope, ReturnType<typeof setTimeout> | null>>({
    full: null,
    objects: null,
    relations: null,
  });
  const completionHandledRefs = useRef<Record<DraftGenerationScope, string | null>>({
    full: null,
    objects: null,
    relations: null,
  });

  const error = actionError || loadError;

  const loadOntology = useCallback(
    async (ontologyId: string) => {
      setOntologyLoading(true);
      try {
        const lists = await fetchOntologyLists(ontologyId, {
          objectPage: 1,
          relationPage: 1,
          pageSize,
          q: debouncedQ,
        });
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

  const stopPolling = useCallback((scope: DraftGenerationScope) => {
    const ref = pollRefs.current[scope];
    if (ref) {
      clearTimeout(ref);
      pollRefs.current[scope] = null;
    }
  }, []);

  const pollProgress = useCallback(
    (scope: DraftGenerationScope, taskId: string) => {
      stopPolling(scope);
      completionHandledRefs.current[scope] = null;

      const pollOnce = async () => {
        try {
          const p = await api.getProgress(domainId!, scope);
          if (p.task_id !== taskId) return;
          setDraftProgress((prev) => ({ ...prev, [scope]: p }));

          if (
            p.status === "succeeded" ||
            p.status === "completed" ||
            p.status === "failed" ||
            p.status === "cancelled"
          ) {
            if (completionHandledRefs.current[scope] === taskId) return;
            completionHandledRefs.current[scope] = taskId;
            stopPolling(scope);
            setGenerating((prev) => ({ ...prev, [scope]: false }));

            if ((p.status === "succeeded" || p.status === "completed") && p.ontology_id) {
              const updated = await api.getDomain(domainId!);
              setBundle((prev) => (prev ? { ...prev, domain: updated } : prev));
              await loadOntology(p.ontology_id);
              message.success(`${SCOPE_LABEL[scope]}生成完成`);
              // 二次生成的核心价值是「告诉我上游变了什么」。后端每次都落了完整合并
              // 报告，此前前端从不读它——用户点完只得到一句 toast，然后自己去几百个
              // 对象里翻。生成一结束就把变更报告推到眼前。
              try {
                const report = await api.getMergeReport(domainId!, taskId);
                setMergeReport(report);
                setMergeReportOpen(true);
              } catch {
                /* 报告拿不到不影响生成本身 */
              }
            } else if (p.status === "failed") {
              setActionError(p.message || "生成失败");
            } else if (p.status === "cancelled") {
              message.info(p.message || "生成已停止");
            }
            return;
          }

          pollRefs.current[scope] = setTimeout(pollOnce, 2000);
        } catch {
          stopPolling(scope);
          setGenerating((prev) => ({ ...prev, [scope]: false }));
          setActionError("获取进度失败");
        }
      };

      void pollOnce();
    },
    [domainId, loadOntology, setBundle, stopPolling],
  );

  useEffect(() => () => GENERATION_SCOPES.forEach((scope) => stopPolling(scope)), [stopPolling]);

  const handleGenerate = (scope: DraftGenerationScope) => {
    if (!domainId) return;
    const config: Record<
      DraftGenerationScope,
      { title: string; content: string; run: () => Promise<DraftProgress> }
    > = {
      full: {
        title: "确认生成本体草稿",
        content:
          "将根据 DataHub 最新元数据重新生成并与现有草稿合并：未改动字段接受机器更新，你的人工修正会被保留，双改字段进入冲突复核。",
        run: () => api.generateDraft(domainId),
      },
      objects: {
        title: "确认生成业务对象",
        content: "将根据 DataHub 元数据重新生成业务对象与属性，不影响已有的业务关系。",
        run: () => api.generateObjects(domainId),
      },
      relations: {
        title: "确认生成业务关系",
        content:
          "将根据 DataHub 元数据重新生成业务关系，不影响已有的业务对象；需已先生成业务对象。",
        run: () => api.generateRelations(domainId),
      },
    };
    const { title, content, run } = config[scope];
    Modal.confirm({
      title,
      content,
      okText: "确认生成",
      cancelText: "取消",
      onOk: async () => {
        setGenerating((prev) => ({ ...prev, [scope]: true }));
        setActionError(null);
        setDraftProgress((prev) => ({ ...prev, [scope]: null }));
        try {
          const result = await run();
          setDraftProgress((prev) => ({ ...prev, [scope]: result }));
          pollProgress(scope, result.task_id);
        } catch (err) {
          setGenerating((prev) => ({ ...prev, [scope]: false }));
          setActionError(err instanceof Error ? err.message : "生成失败");
        }
      },
    });
  };

  const handlePublish = async () => {
    if (!domain?.working_ontology_id || !domainId) return;

    // 发布前自检：把「将发布多少、将跳过多少、为什么」在点之前算给用户看。
    // 真实故障模式是源库无主键导致对象 100% 待复核 → 提升 0 个、本体浏览页空白，
    // 而用户只看到一句「发布成功」。这里让原因在点之前就可见。
    const preflight: PublishPreflight | null = await api
      .publishPreflight(domain.working_ontology_id)
      .catch(() => null);

    Modal.confirm({
      title: "确认发布本体",
      width: 520,
      content: preflight ? (
        <PublishPreflightSummary preflight={preflight} />
      ) : (
        "发布将把当前已确认内容固化为新版本，对外在本体页与业务逻辑页展示。此操作需要二次确认。"
      ),
      okText: "确认发布",
      cancelText: "取消",
      onOk: async () => {
        try {
          setActionError(null);
          // 形式化不变式预检（F2）：error 级且 enforcement=error 时阻断（后端也会拦，
          // 这里提前给出可读提示，避免直接抛 400）。
          const formal = await api
            .formalValidateOntology(domain.working_ontology_id!)
            .catch(() => null);
          if (formal && formal.enforcement === "error" && !formal.ok) {
            const errs = formal.issues.filter((i) => i.severity === "error");
            const top = errs
              .slice(0, 3)
              .map((i) => i.message)
              .join("；");
            const extra = errs.length > 3 ? ` 等 ${errs.length} 项` : "";
            const msg = `本体未通过形式化校验，无法发布：${top}${extra}`;
            setActionError(msg);
            message.error(msg);
            throw new Error(msg);
          }
          // 一致性校验改为建议性：不再阻断发布。发布已确认的业务对象与业务关系，
          // 待复核/其它类型对象保持原状，冲突仅作简洁提示。
          const validation = await api
            .validateOntology(domain.working_ontology_id!)
            .catch(() => null);
          const confirmation = await api.createConfirmation({
            ontology_id: domain.working_ontology_id!,
            target_type: "ontology",
            action_type: "publish",
            reason: "工作区发布确认",
          });
          await api.confirmAction(confirmation.id);
          const updated = await api.getDomain(domainId);
          setBundle((prev) => (prev ? { ...prev, domain: updated } : prev));
          const issues = validation && !validation.ok ? validation.issues : [];
          const formalWarnings =
            formal && formal.warning_count > 0
              ? formal.issues.filter((i) => i.severity === "warning")
              : [];
          if (issues.length || formalWarnings.length) {
            const parts: string[] = [];
            if (issues.length) {
              const top = issues
                .slice(0, 2)
                .map((i) => i.message)
                .join("；");
              parts.push(
                `${issues.length} 处待复核/冲突保持原状：${top}${issues.length > 2 ? " 等" : ""}`,
              );
            }
            if (formalWarnings.length) {
              parts.push(`${formalWarnings.length} 项形式化提醒（基数/语义类型可完善）`);
            }
            message.warning(`已发布已确认的业务对象与关系；${parts.join("；")}`);
          } else {
            message.success("发布成功");
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : "发布失败";
          setActionError(msg);
          message.error(msg);
          throw err;
        }
      },
    });
  };

  const handleDiscardUnpublished = () => {
    if (!domainId) return;
    Modal.confirm({
      title: "丢弃未发布内容",
      okType: "danger",
      content:
        "将删除该数据域工作本体里从未发布过的对象与关系，已发布内容（含你对它们的修改）保持不变。此操作不可撤销。",
      okText: "确认丢弃",
      cancelText: "取消",
      onOk: async () => {
        try {
          const r = await api.discardUnpublished(domainId);
          setActionError(null);
          message.success(
            `已丢弃：对象 ${r.object_types} · 关系 ${r.relation_types} · 属性 ${r.properties}`,
          );
          void reloadBundle();
        } catch (err) {
          const msg = err instanceof Error ? err.message : "丢弃失败";
          setActionError(msg);
          message.error(msg);
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

  // 仅在「首屏尚无数据」时显示整页骨架屏。分页/搜索等再次请求时 loading 也会为 true，
  // 若此处仍整页返回骨架屏，会卸载并重建整棵子树（含 OntologyWorkspaceView），导致其内部
  // 的对象/关系视图切换状态被重置——表现为「关系视图下点击分页跳回对象视图」。
  // 保留已有内容、由下方 Spin 覆盖提示加载，即可让视图切换状态在再次请求间保持。
  if (loading && !bundle) return <PageSkeleton type="detail" />;

  if (!domain) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "数据域不存在"} showIcon />
      </PageContainer>
    );
  }

  const objectDetailPath = (objectId: string) => `/workspace/${domainId}/objects/${objectId}`;
  const relationDetailPath = (relationId: string) =>
    `/workspace/${domainId}/relations/${relationId}`;
  const relationGroupDetailPath = (displayName: string) =>
    `/workspace/${domainId}/relation-groups/${encodeURIComponent(displayName)}?oid=${domain.working_ontology_id}`;

  const publishedVersion = domain.published_ontology_version;

  // 一域一本体：工作本体永远可编辑，不论发没发布过。已发布实体被人工编辑后**立即
  // 生效且保持 published**（人工即权威），改动会计入下面的「待固化」提示条，点发布
  // 即固化成新版本。机器改动才需要过闸——那走三方合并的冲突通道。
  const workspaceEditable = domain.working_ontology_id != null;
  const pendingCount = domain.unpublished_change_count ?? 0;
  const pendingPublish = domain.pending_publish_count ?? 0;
  const needsReviewCount = domain.needs_review_count ?? 0;
  const conflictCount = domain.unresolved_conflict_count ?? 0;

  return (
    <PageContainer full>
      <PageHeader
        icon={<DeploymentUnitOutlined />}
        title={
          <Space size={10}>
            <span>{domain.name}</span>
            {domain.working_ontology_status && (
              <StatusBadge status={domain.working_ontology_status} />
            )}
          </Space>
        }
        description={domain.description || "暂无描述"}
        extra={
          <Space wrap>
            {domain.datahub_url && (
              <Tooltip title="在 DataHub 中打开">
                <Button
                  type="default"
                  href={domain.datahub_url}
                  target="_blank"
                  icon={<ExportOutlined />}
                  aria-label="在 DataHub 中打开"
                />
              </Tooltip>
            )}
            <Link to={`/workspace/${domainId}/executions`}>
              <Tooltip title="执行记录">
                <Button icon={<HistoryOutlined />} aria-label="执行记录" />
              </Tooltip>
            </Link>
            <Link to={`/workspace/${domainId}/segments`}>
              <Tooltip title="业务板块">
                <Button icon={<AppstoreOutlined />} aria-label="业务板块" />
              </Tooltip>
            </Link>
            {domain.published_ontology_id && (
              <>
                <Link to={`/ontology?domain=${domainId}`}>
                  <Tooltip title="查看已发布本体">
                    <Button icon={<ApartmentOutlined />} aria-label="查看已发布本体" />
                  </Tooltip>
                </Link>
                <Tooltip title={`版本历史${publishedVersion ? ` v${publishedVersion}` : ""}`}>
                  <Button
                    icon={<HistoryOutlined />}
                    onClick={openVersionHistory}
                    aria-label={`版本历史${publishedVersion ? ` v${publishedVersion}` : ""}`}
                  />
                </Tooltip>
              </>
            )}
            <Dropdown
              trigger={["hover", "click"]}
              disabled={generating.full || generating.objects || generating.relations}
              menu={{
                items: [
                  {
                    key: "full",
                    icon: <ThunderboltOutlined />,
                    label: "生成本体草稿",
                    onClick: () => handleGenerate("full"),
                  },
                  // 对象/关系两个范围后端一直支持，此前只有 Data Agent 的提案块能触发，
                  // 工作区点不到。
                  {
                    key: "objects",
                    icon: <ApartmentOutlined />,
                    label: "仅生成业务对象",
                    onClick: () => handleGenerate("objects"),
                  },
                  {
                    key: "relations",
                    icon: <DeploymentUnitOutlined />,
                    label: "仅生成业务关系",
                    disabled: !domain.working_ontology_id,
                    onClick: () => handleGenerate("relations"),
                  },
                  { type: "divider" as const },
                  // 增量建模：为几张新表重扫整个域，代价是几十万 token 且会把待复核
                  // 重新灌满。先看清楚哪几张表是新的，再只对它们生成。
                  {
                    key: "incremental",
                    icon: <FileSearchOutlined />,
                    label: "增量建模（只生成新表）",
                    onClick: () => setIncrementalOpen(true),
                  },
                  {
                    key: "manual",
                    icon: <EditOutlined />,
                    label: "人工生成",
                    onClick: () => setManualOpen(true),
                  },
                  {
                    key: "discard",
                    icon: <DeleteOutlined />,
                    danger: true,
                    disabled: !domain.working_ontology_id,
                    label: "丢弃未发布内容",
                    onClick: handleDiscardUnpublished,
                  },
                ],
              }}
            >
              <Button
                type="primary"
                loading={generating.full || generating.objects || generating.relations}
                icon={<ThunderboltOutlined />}
              >
                <Space size={4}>
                  生成
                  <DownOutlined style={{ fontSize: 10 }} />
                </Space>
              </Button>
            </Dropdown>
            {domain.working_ontology_id && workspaceEditable && (
              <ConflictsPanel
                ontologyId={domain.working_ontology_id}
                onChanged={() =>
                  domain.working_ontology_id && void loadOntology(domain.working_ontology_id)
                }
              />
            )}
            {domain.working_ontology_id && workspaceEditable && (
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

      {workspaceEditable && (pendingCount > 0 || pendingPublish > 0) && (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message={
            <Space size={8} wrap>
              {pendingCount > 0 && <Tag color="orange">{pendingCount} 项已发布内容被修改</Tag>}
              {pendingPublish > 0 && <Tag color="blue">{pendingPublish} 项待提升</Tag>}
              {needsReviewCount > 0 && <Tag>{needsReviewCount} 个待复核对象（发布会跳过）</Tag>}
              {conflictCount > 0 && <Tag color="red">{conflictCount} 处字段冲突</Tag>}
              <span style={{ color: "var(--om-text-secondary)", fontSize: 13 }}>
                尚未固化为新版本
              </span>
            </Space>
          }
          action={
            <Button size="small" type="primary" onClick={handlePublish}>
              发布
            </Button>
          }
        />
      )}

      {GENERATION_SCOPES.filter((scope) => generating[scope] && draftProgress[scope]).map(
        (scope) => {
          const progress = draftProgress[scope]!;
          return (
            <div
              key={scope}
              style={{
                margin: "16px 0",
                padding: "16px 24px",
                background: "var(--om-surface-muted)",
                border: "1px solid var(--om-border)",
                borderRadius: 8,
              }}
            >
              <div style={{ marginBottom: 4, fontWeight: 500 }}>{SCOPE_LABEL[scope]}生成中</div>
              <Progress
                percent={progress.progress}
                status={progress.status === "failed" ? "exception" : "active"}
                strokeColor={{ from: "#2563eb", to: "#16a34a" }}
              />
              <div style={{ marginTop: 4, color: "var(--om-text-secondary)", fontSize: 13 }}>
                {progress.message || "处理中..."}
              </div>
            </div>
          );
        },
      )}

      <Spin spinning={ontologyLoading || loading}>
        {!domain.working_ontology_id ? (
          <EmptyState
            title="尚未生成本体草稿"
            description="从 DataHub 拉取数据域元数据并生成本体草稿，作为后续编辑与发布的起点。"
            action={
              <Space>
                <Button
                  type="primary"
                  size="large"
                  loading={generating.full}
                  disabled={generating.objects}
                  onClick={() => handleGenerate("full")}
                  icon={<ThunderboltOutlined />}
                >
                  生成本体草稿
                </Button>
                <Button
                  size="large"
                  disabled={generating.full}
                  onClick={() => setManualOpen(true)}
                  icon={<EditOutlined />}
                >
                  人工生成
                </Button>
              </Space>
            }
          />
        ) : (
          <>
            <OntologyWorkspaceView
              objects={objects}
              relations={relations}
              objectDetailPath={objectDetailPath}
              relationDetailPath={relationDetailPath}
              relationScope={{ ontologyId: domain.working_ontology_id ?? undefined }}
              datasetOntologyId={domain.working_ontology_id}
              onDerivedObjectCreated={reloadBundle}
              relationGroupDetailPath={relationGroupDetailPath}
              workspaceMode
              searchQuery={searchQuery}
              onSearchChange={setSearchQuery}
              objectTypeFilter={typeFilter}
              onObjectTypeFilterChange={setTypeFilter}
              needsReviewOnly={needsReviewOnly}
              onNeedsReviewOnlyChange={setNeedsReviewOnly}
              onBatchUpdateObjects={async (ids, patch) => {
                const res = await api.batchUpdateObjectTypes({ ids, ...patch });
                message.success(`已更新 ${res.updated} 个对象`);
                reloadBundle();
              }}
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
            />
          </>
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
            className="om-table"
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
                    <div>
                      对象 新增 {selectedDiff.object_types.added.length} / 修改{" "}
                      {selectedDiff.object_types.modified.length} / 删除{" "}
                      {selectedDiff.object_types.removed.length}
                    </div>
                    <div>
                      关系 新增 {selectedDiff.relation_types.added.length} / 修改{" "}
                      {selectedDiff.relation_types.modified.length} / 删除{" "}
                      {selectedDiff.relation_types.removed.length}
                    </div>
                    <div>
                      逻辑 新增 {selectedDiff.business_logics.added.length} / 修改{" "}
                      {selectedDiff.business_logics.modified.length} / 删除{" "}
                      {selectedDiff.business_logics.removed.length}
                    </div>
                    {selectedDiff.object_types.added.length > 0 && (
                      <div style={{ marginTop: 8 }}>
                        新增对象：
                        {selectedDiff.object_types.added
                          .map((i) => i.display_name || i.name)
                          .join("、")}
                      </div>
                    )}
                    {selectedDiff.relation_types.added.length > 0 && (
                      <div>
                        新增关系：
                        {selectedDiff.relation_types.added
                          .map((i) => i.display_name || i.name)
                          .join("、")}
                      </div>
                    )}
                  </div>
                }
              />
            </div>
          )}
        </Spin>
      </Modal>

      <MergeReportDrawer
        report={mergeReport}
        open={mergeReportOpen}
        onClose={() => setMergeReportOpen(false)}
      />

      {domainId && (
        <IncrementalModelingModal
          open={incrementalOpen}
          domainId={domainId}
          onClose={() => setIncrementalOpen(false)}
          // 裁剪生成也是一次 objects 范围的任务，进度沿用同一条轮询链路。
          onStarted={(progress) => {
            setGenerating((prev) => ({ ...prev, objects: true }));
            setActionError(null);
            setDraftProgress((prev) => ({ ...prev, objects: progress }));
            pollProgress("objects", progress.task_id);
          }}
        />
      )}

      {domainId && (
        <ManualCreateModal
          open={manualOpen}
          onClose={() => setManualOpen(false)}
          domainId={domainId}
          ontologyId={domain?.working_ontology_id}
          objects={objects}
          onCreated={() => void reloadBundle()}
        />
      )}
    </PageContainer>
  );
}
