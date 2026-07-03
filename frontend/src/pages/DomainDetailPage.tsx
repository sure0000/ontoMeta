import {
  ApartmentOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DeploymentUnitOutlined,
  ExportOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Alert, Button, Modal, Progress, Space, Spin, message } from "antd";
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
import type {
  DomainContextDetail,
  DraftProgress,
  ObjectTypeSummary,
  OntologyGraph,
  RelationType,
} from "../types";

export function DomainDetailPage() {
  const { domainId } = useParams<{ domainId: string }>();
  const [domain, setDomain] = useState<DomainContextDetail | null>(null);
  const [objects, setObjects] = useState<ObjectTypeSummary[]>([]);
  const [relations, setRelations] = useState<RelationType[]>([]);
  const [graph, setGraph] = useState<OntologyGraph | null>(null);
  const [generating, setGenerating] = useState(false);
  const [draftProgress, setDraftProgress] = useState<DraftProgress | null>(null);
  const [loading, setLoading] = useState(true);
  const [ontologyLoading, setOntologyLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadOntology = useCallback(async (ontologyId: string) => {
    setOntologyLoading(true);
    try {
      const [objectList, graphData, relationList] = await Promise.all([
        api.listObjectTypes({ ontologyId }),
        api.getOntologyGraph(ontologyId),
        api.listRelationTypes({ ontologyId }),
      ]);
      setObjects(objectList);
      setRelations(relationList);
      setGraph(graphData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载本体失败");
    } finally {
      setOntologyLoading(false);
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const pollProgress = useCallback(
    (taskId: string) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const p = await api.getProgress(domainId!);
          if (p.task_id !== taskId) return;
          setDraftProgress(p);
          if (p.status === "completed" || p.status === "failed") {
            stopPolling();
            setGenerating(false);
            if (p.status === "completed" && p.ontology_id) {
              const updated = await api.getDomain(domainId!);
              setDomain(updated);
              await loadOntology(p.ontology_id);
              message.success("本体草稿生成完成");
            } else if (p.status === "failed") {
              setError(p.message || "生成失败");
            }
          }
        } catch {
          stopPolling();
          setGenerating(false);
          setError("获取进度失败");
        }
      }, 2000);
    },
    [domainId, loadOntology, stopPolling],
  );

  useEffect(() => {
    if (!domainId) return;
    setLoading(true);
    api
      .getDomain(domainId)
      .then((detail) => {
        setDomain(detail);
        if (detail.latest_ontology_id) {
          return loadOntology(detail.latest_ontology_id);
        }
        setObjects([]);
        setRelations([]);
        setGraph(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId, loadOntology]);

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
        setError(null);
        setDraftProgress(null);
        try {
          const result = await api.generateDraft(domainId);
          setDraftProgress(result);
          pollProgress(result.task_id);
        } catch (err) {
          setGenerating(false);
          setError(err instanceof Error ? err.message : "生成失败");
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
          setError(null);
          const confirmation = await api.createConfirmation({
            ontology_id: domain.latest_ontology_id!,
            target_type: "ontology",
            action_type: "publish",
            reason: "工作区发布确认",
          });
          await api.confirmAction(confirmation.id);
          const updated = await api.getDomain(domainId);
          setDomain(updated);
          message.success("发布成功");
        } catch (err) {
          const msg = err instanceof Error ? err.message : "发布失败";
          setError(msg);
          message.error(msg);
          throw err;
        }
      },
    });
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
                  <Button>查看业务逻辑</Button>
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
          onClose={() => setError(null)}
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
          value={objects.length}
          hint="当前草稿"
        />
        <StatCard
          tone="success"
          icon={<DeploymentUnitOutlined />}
          label="关系数量"
          value={relations.length}
          hint="显式关系"
        />
        <StatCard
          tone={domain.latest_ontology_status === "published" ? "success" : "warning"}
          icon={<ClockCircleOutlined />}
          label="本体状态"
          value={
            <span style={{ fontSize: 16 }}>
              {domain.latest_ontology_status
                ? statusLabel(domain.latest_ontology_status)
                : "未生成"}
            </span>
          }
          hint={publishedVersion ? `已发布版本 v${publishedVersion}` : "尚未发布"}
        />
        <StatCard
          tone="neutral"
          icon={<PlayCircleOutlined />}
          label="发布版本"
          value={publishedVersion ? `v${publishedVersion}` : "—"}
          hint={domain.published_ontology_id ? "对外可见" : "尚未对外发布"}
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
          />
        )}
      </Spin>
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
