import { ApartmentOutlined } from "@ant-design/icons";
import { Alert, Spin } from "antd";
import { useEffect, useLayoutEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { OntologyWorkspaceView } from "../components/OntologyWorkspaceView";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import type {
  DomainContext,
  DomainContextDetail,
  ObjectTypeSummary,
  OntologyGraph,
  RelationType,
} from "../types";

interface OntologyBundle {
  domains: DomainContext[];
  domain: DomainContextDetail | null;
  objects: ObjectTypeSummary[];
  relations: RelationType[];
  graph: OntologyGraph | null;
  publishedOntologyId: string | null;
}

export function OntologyPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;

  const { data: bundle, loading, error } = useApi<OntologyBundle>(
    async () => {
      const domains = await api.listDomains();
      if (domains.length === 0) {
        return {
          domains,
          domain: null,
          objects: [],
          relations: [],
          graph: null,
          publishedOntologyId: null,
        };
      }
      const targetDomainId = domainId ?? domains[0]?.id;
      if (!targetDomainId) {
        return {
          domains,
          domain: null,
          objects: [],
          relations: [],
          graph: null,
          publishedOntologyId: null,
        };
      }
      const domain = await api.getDomain(targetDomainId);
      const ontologyId = domain.published_ontology_id;
      if (!ontologyId) {
        return {
          domains,
          domain,
          objects: [],
          relations: [],
          graph: null,
          publishedOntologyId: null,
        };
      }
      const [objectList, graphData, relationList] = await Promise.all([
        api.listObjectTypes({ ontologyId, publishedOnly: true }),
        api.getOntologyGraph(ontologyId),
        api.listRelationTypes({ ontologyId, publishedOnly: true }),
      ]);
      return {
        domains,
        domain,
        objects: objectList,
        relations: relationList,
        graph: graphData,
        publishedOntologyId: ontologyId,
      };
    },
    [domainId],
  );

  const domains = bundle?.domains ?? [];
  const domain = bundle?.domain ?? null;
  const objects = bundle?.objects ?? [];
  const relations = bundle?.relations ?? [];
  const graph = bundle?.graph ?? null;
  const publishedOntologyId = bundle?.publishedOntologyId ?? null;

  // 首次进入且未在 URL 中带 domain 参数时，把默认域写入 URL（replace，不污染历史）。
  // 必须放在 effect 中，不能在 render 期间调用 setSearchParams。
  const syncedRef = useRef(false);
  useLayoutEffect(() => {
    if (syncedRef.current) return;
    if (!domainId && domains.length > 0 && domains[0]?.id) {
      syncedRef.current = true;
      setSearchParams({ domain: domains[0].id }, { replace: true });
    }
  }, [domainId, domains, setSearchParams]);

  // domainId 变化时重置 sync 标记
  useEffect(() => {
    syncedRef.current = Boolean(domainId);
  }, [domainId]);

  if (loading && domains.length === 0) return <PageSkeleton type="cards" full />;

  if (error && domains.length === 0) {
    return (
      <PageContainer>
        <Alert type="error" message="加载失败" description={error} showIcon />
      </PageContainer>
    );
  }

  return (
    <PageContainer full>
      <PageHeader
        icon={<ApartmentOutlined />}
        title={domain?.name ?? "本体浏览"}
      />

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
        />
      )}

      <Spin spinning={loading}>
        {!publishedOntologyId ? (
          <EmptyState
            title="该数据域尚无已发布本体"
            description="请在工作区完成草稿编辑并发布后，回到此页查看已固化的本体语义。"
          />
        ) : (
          <OntologyWorkspaceView
            objects={objects}
            relations={relations}
            graph={graph}
            relationDetailPath={(relationId) => `/ontology/relations/${relationId}`}
          />
        )}
      </Spin>
    </PageContainer>
  );
}
