import { ApartmentOutlined } from "@ant-design/icons";
import { Alert, Spin, message } from "antd";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
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

interface OntologyBundle {
  domains: DomainContext[];
  domain: DomainContextDetail | null;
  objects: ObjectTypeSummary[];
  objectTotal: number;
  relations: RelationType[];
  relationTotal: number;
  graph: OntologyGraph | null;
  publishedOntologyId: string | null;
}

export function OntologyPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;
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
    graphCacheRef.current = null;
  }, [debouncedQ, domainId]);

  const { data: bundle, loading, error, setData: setBundle } = useApi<OntologyBundle>(
    async () => {
      const domains = await api.listDomains();
      if (domains.length === 0) {
        return {
          domains,
          domain: null,
          objects: [],
          objectTotal: 0,
          relations: [],
          relationTotal: 0,
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
          objectTotal: 0,
          relations: [],
          relationTotal: 0,
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
          objectTotal: 0,
          relations: [],
          relationTotal: 0,
          graph: null,
          publishedOntologyId: null,
        };
      }
      const cached = graphCacheRef.current;
      const reuseGraph = cached?.ontologyId === ontologyId ? cached.graph : null;
      const objectOffset = (objectPage - 1) * pageSize;
      const relationOffset = (relationPage - 1) * pageSize;
      const listReqs = [
        api.listObjectTypes({
          ontologyId,
          publishedOnly: true,
          q: debouncedQ || undefined,
          limit: pageSize,
          offset: objectOffset,
        }),
        api.listRelationTypes({
          ontologyId,
          publishedOnly: true,
          q: debouncedQ || undefined,
          limit: pageSize,
          offset: relationOffset,
        }),
      ] as const;
      let graph: OntologyGraph | null = reuseGraph;
      let objectsPage;
      let relationsPage;
      if (reuseGraph) {
        [objectsPage, relationsPage] = await Promise.all(listReqs);
      } else {
        [objectsPage, relationsPage, graph] = await Promise.all([
          ...listReqs,
          api.getOntologyGraph(ontologyId, { depth: 1 }),
        ]);
      }
      if (graph) graphCacheRef.current = { ontologyId, graph };
      return {
        domains,
        domain,
        objects: objectsPage.items,
        objectTotal: objectsPage.total,
        relations: relationsPage.items,
        relationTotal: relationsPage.total,
        graph,
        publishedOntologyId: ontologyId,
      };
    },
    [domainId, objectPage, relationPage, pageSize, debouncedQ],
  );

  const domains = bundle?.domains ?? [];
  const domain = bundle?.domain ?? null;
  const objects = bundle?.objects ?? [];
  const relations = bundle?.relations ?? [];
  const graph = bundle?.graph ?? null;
  const publishedOntologyId = bundle?.publishedOntologyId ?? null;
  const objectTotal = bundle?.objectTotal ?? 0;
  const relationTotal = bundle?.relationTotal ?? 0;

  const handleExpandGraphNode = useCallback(
    async (objectId: string) => {
      if (!publishedOntologyId) return;
      setGraphExpanding(true);
      try {
        const neighborhood = await api.getOntologyGraph(publishedOntologyId, {
          centerId: objectId,
          depth: 1,
        });
        setBundle((prev) => {
          if (!prev) return prev;
          const merged = mergeOntologyGraph(prev.graph, neighborhood);
          graphCacheRef.current = { ontologyId: publishedOntologyId, graph: merged };
          return { ...prev, graph: merged };
        });
      } catch (err) {
        message.error(err instanceof Error ? err.message : "展开邻域失败");
      } finally {
        setGraphExpanding(false);
      }
    },
    [publishedOntologyId, setBundle],
  );

  // 首次进入且未在 URL 中带 domain 参数时，把默认域写入 URL（replace，不污染历史）。
  const syncedRef = useRef(false);
  useLayoutEffect(() => {
    if (syncedRef.current) return;
    if (!domainId && domains.length > 0 && domains[0]?.id) {
      syncedRef.current = true;
      setSearchParams({ domain: domains[0].id }, { replace: true });
    }
  }, [domainId, domains, setSearchParams]);

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
    </PageContainer>
  );
}
