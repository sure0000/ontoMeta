import { ApartmentOutlined } from "@ant-design/icons";
import { Alert, Select, Space, Spin, Typography } from "antd";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { OntologyWorkspaceView } from "../components/OntologyWorkspaceView";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import type {
  DomainContext,
  ObjectTypeSummary,
  OntologyGraph,
  RelationType,
} from "../types";

const { Text } = Typography;

export function OntologyPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [objects, setObjects] = useState<ObjectTypeSummary[]>([]);
  const [relations, setRelations] = useState<RelationType[]>([]);
  const [graph, setGraph] = useState<OntologyGraph | null>(null);
  const [publishedOntologyId, setPublishedOntologyId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listDomains().then(setDomains).catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (domains.length === 0) {
      setLoading(false);
      return;
    }

    const targetDomainId = domainId ?? domains[0]?.id;
    if (!targetDomainId) {
      setLoading(false);
      return;
    }

    if (!domainId && targetDomainId) {
      setSearchParams({ domain: targetDomainId }, { replace: true });
      return;
    }

    setLoading(true);
    setError(null);

    api
      .getDomain(targetDomainId)
      .then(async (domain) => {
        const ontologyId = domain.published_ontology_id;
        setPublishedOntologyId(ontologyId ?? null);

        if (!ontologyId) {
          setObjects([]);
          setRelations([]);
          setGraph(null);
          return;
        }

        const [objectList, graphData, relationList] = await Promise.all([
          api.listObjectTypes({ ontologyId, publishedOnly: true }),
          api.getOntologyGraph(ontologyId),
          api.listRelationTypes({ ontologyId, publishedOnly: true }),
        ]);
        setObjects(objectList);
        setRelations(relationList);
        setGraph(graphData);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId, domains, setSearchParams]);

  const handleDomainChange = (value: string) => {
    setSearchParams({ domain: value });
  };

  if (loading && domains.length === 0) return <PageSkeleton type="cards" />;

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
        title="本体"
        description="已发布本体的语义结果展示（只读），可切换数据域浏览对象、关系与图谱。"
        extra={
          domains.length > 0 ? (
            <Space>
              <Text type="secondary" style={{ fontSize: 13 }}>
                数据域
              </Text>
              <Select
                style={{ minWidth: 220 }}
                value={domainId ?? domains[0]?.id}
                onChange={handleDomainChange}
                options={domains.map((d) => ({ label: d.name, value: d.id }))}
              />
            </Space>
          ) : undefined
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
