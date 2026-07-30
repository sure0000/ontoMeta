import { ApartmentOutlined, DatabaseOutlined } from "@ant-design/icons";
import { Alert, Button, Spin } from "antd";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { MaterializeModal } from "../components/MaterializeModal";
import { OntologyWorkspaceView } from "../components/OntologyWorkspaceView";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useApi } from "../hooks/useApi";
import type {
  DomainContext,
  DomainContextDetail,
  ObjectTypeSummary,
  RelationType,
} from "../types";

const DEFAULT_PAGE_SIZE = 20;

interface OntologyBundle {
  domains: DomainContext[];
  domain: DomainContextDetail | null;
  objects: ObjectTypeSummary[];
  objectTotal: number;
  relations: RelationType[];
  relationTotal: number;
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
  const [roleFilter, setRoleFilter] = useState<string[]>([]);
  const [materializeOpen, setMaterializeOpen] = useState(false);

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
  }, [roleFilter]);

  const { data: bundle, loading, error } = useApi<OntologyBundle>(
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
          publishedOntologyId: null,
        };
      }
      const objectOffset = (objectPage - 1) * pageSize;
      const relationOffset = (relationPage - 1) * pageSize;
      const [objectsPage, relationsPage] = await Promise.all([
        api.listObjectTypes({
          ontologyId,
          publishedOnly: true,
          q: debouncedQ || undefined,
          roleIn: roleFilter.length ? roleFilter : undefined,
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
      ]);
      return {
        domains,
        domain,
        objects: objectsPage.items,
        objectTotal: objectsPage.total,
        relations: relationsPage.items,
        relationTotal: relationsPage.total,
        publishedOntologyId: ontologyId,
      };
    },
    [domainId, objectPage, relationPage, pageSize, debouncedQ, roleFilter],
  );

  const domains = bundle?.domains ?? [];
  const domain = bundle?.domain ?? null;
  const objects = bundle?.objects ?? [];
  const relations = bundle?.relations ?? [];
  const publishedOntologyId = bundle?.publishedOntologyId ?? null;
  const objectTotal = bundle?.objectTotal ?? 0;
  const relationTotal = bundle?.relationTotal ?? 0;

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
        extra={
          publishedOntologyId ? (
            <Button
              type="primary"
              icon={<DatabaseOutlined />}
              onClick={() => setMaterializeOpen(true)}
            >
              物化
            </Button>
          ) : undefined
        }
      />

      {publishedOntologyId && (
        <MaterializeModal
          ontologyId={publishedOntologyId}
          open={materializeOpen}
          onClose={() => setMaterializeOpen(false)}
        />
      )}

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
            showRoleClassification={false}
            relationDetailPath={(relationId) => `/ontology/relations/${relationId}`}
            relationScope={{ ontologyId: publishedOntologyId ?? undefined, publishedOnly: true }}
            relationGroupDetailPath={(displayName) =>
              `/ontology/relation-groups/${encodeURIComponent(displayName)}?oid=${publishedOntologyId}&pub=1`
            }
            objectTypeFilter={roleFilter}
            onObjectTypeFilterChange={setRoleFilter}
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
          />
        )}
      </Spin>
    </PageContainer>
  );
}
