import {
  ApartmentOutlined,
  CodeOutlined,
  FunctionOutlined,
  HistoryOutlined,
  LinkOutlined,
  TableOutlined,
} from "@ant-design/icons";
import { Alert, Button, Col, Descriptions, Row, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SectionCard } from "../components/SectionCard";
import { StatusBadge } from "../components/StatusBadge";
import type {
  BusinessLogicDetail,
  BusinessLogicObjectBinding,
  BusinessLogicPropertyBinding,
  Property,
  VersionRecord,
} from "../types";

const { Text } = Typography;

const OBJECT_ROLE_LABEL: Record<string, string> = {
  subject: "主对象",
  dimension: "维度对象",
  output: "产出对象",
};

const PROPERTY_ROLE_LABEL: Record<string, string> = {
  input: "输入",
  output: "输出",
  filter: "过滤",
  group: "分组",
};

export function BusinessLogicDetailPage() {
  const { logicId } = useParams<{ logicId: string }>();
  const [logic, setLogic] = useState<BusinessLogicDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!logicId) return;
    setLoading(true);
    api
      .getBusinessLogic(logicId)
      .then(setLogic)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [logicId]);

  const propertyColumns: ColumnsType<Property> = [
    {
      title: "属性",
      dataIndex: "display_name",
      key: "display_name",
      render: (v, r) => (
        <span className="id-link">
          <span>{v}</span>
          <span className="id-link-sub">{r.name}</span>
        </span>
      ),
    },
    {
      title: "类型",
      dataIndex: "data_type",
      key: "data_type",
      width: 140,
      render: (v) => v || <span className="om-muted">-</span>,
    },
  ];

  const objectBindingColumns: ColumnsType<BusinessLogicObjectBinding> = [
    {
      title: "对象",
      dataIndex: "object_type_display_name",
      key: "object_type_display_name",
      render: (v, r) => (
        <Link to={`/ontology/${r.object_type_id}`}>
          {v || r.object_type_name || r.object_type_id}
        </Link>
      ),
    },
    {
      title: "标识名",
      dataIndex: "object_type_name",
      key: "object_type_name",
      render: (v) => <span className="id-link-sub">{v}</span>,
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: 120,
      render: (v) => OBJECT_ROLE_LABEL[v] || v,
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 110,
      render: (v) => (v === "manual" ? <Tag color="blue">人工</Tag> : <Tag>推断</Tag>),
    },
  ];

  const propertyBindingColumns: ColumnsType<BusinessLogicPropertyBinding> = [
    {
      title: "字段",
      dataIndex: "property_display_name",
      key: "property_display_name",
      render: (v, r) => v || r.property_name || r.property_id,
    },
    {
      title: "标识名",
      dataIndex: "property_name",
      key: "property_name",
      render: (v) => <span className="id-link-sub">{v}</span>,
    },
    {
      title: "所属对象",
      dataIndex: "object_type_name",
      key: "object_type_name",
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: 110,
      render: (v) => PROPERTY_ROLE_LABEL[v] || v,
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 110,
      render: (v) => (v === "manual" ? <Tag color="blue">人工</Tag> : <Tag>推断</Tag>),
    },
  ];

  const versionColumns: ColumnsType<VersionRecord> = [
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 90,
      render: (v) => `v${v}`,
    },
    {
      title: "类型",
      dataIndex: "entity_type",
      key: "entity_type",
      width: 130,
    },
    {
      title: "摘要",
      dataIndex: "diff_summary",
      key: "diff_summary",
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v) => new Date(v).toLocaleString(),
    },
  ];

  if (loading) return <PageSkeleton type="detail" />;

  if (!logic) {
    return (
      <PageContainer>
        <Alert type="error" message={error || "业务逻辑不存在"} showIcon />
      </PageContainer>
    );
  }

  const workspacePath = logic.domain_context_id
    ? `/workspace/${logic.domain_context_id}`
    : "/workspace";

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title={logic.display_name}
        description={logic.description || "暂无描述"}
        extra={<StatusBadge status={logic.status} />}
      />

      <Row gutter={[20, 20]}>
        <Col xs={24} lg={12}>
          <SectionCard title="逻辑定义" icon={<FunctionOutlined />}>
            <Descriptions column={1} size="small" labelStyle={{ width: 96 }}>
              <Descriptions.Item label="数据域">
                {logic.domain_name || "-"}
              </Descriptions.Item>
              <Descriptions.Item label="类型">
                {logic.logic_type}
              </Descriptions.Item>
              <Descriptions.Item label="来源类型">
                {logic.source_type || "-"}
              </Descriptions.Item>
              <Descriptions.Item label="来源引用">
                <Text code copyable={!!logic.source_ref}>
                  {logic.source_ref || "-"}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="置信度">
                {logic.source_confidence?.toFixed(2) ?? "-"}
              </Descriptions.Item>
            </Descriptions>
          </SectionCard>
        </Col>

        <Col xs={24} lg={12}>
          <SectionCard title="计算规则" icon={<CodeOutlined />}>
            <pre className="code-block">
              {logic.expression_summary || "暂无规则表达式"}
            </pre>
          </SectionCard>
        </Col>
      </Row>

      <SectionCard
        title="关联对象"
        count={logic.related_object_types.length}
        icon={<ApartmentOutlined />}
      >
        {logic.related_object_types.length === 0 ? (
          <EmptyState title="暂无关联对象" />
        ) : (
          <Row gutter={[16, 16]}>
            {logic.related_object_types.map((obj) => (
              <Col key={obj.id} xs={24} sm={12} md={8}>
                <Link to={`/ontology/${obj.id}`} className="om-card-link">
                  <div className="entity-card" style={{ padding: 14 }}>
                    <div className="entity-card-head">
                      <div style={{ minWidth: 0 }}>
                        <div className="entity-card-title">{obj.display_name}</div>
                        <div className="entity-card-subtitle">{obj.name}</div>
                      </div>
                      <StatusBadge status={obj.status} />
                    </div>
                    <div className="entity-card-desc">
                      {obj.description || "暂无描述"}
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
        )}
      </SectionCard>

      {(logic.object_bindings?.length ?? 0) > 0 && (
        <SectionCard
          title="绑定对象（表）"
          count={logic.object_bindings?.length ?? 0}
          icon={<TableOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={objectBindingColumns}
            dataSource={logic.object_bindings}
            pagination={false}
          />
        </SectionCard>
      )}

      {(logic.property_bindings?.length ?? 0) > 0 && (
        <SectionCard
          title="绑定字段"
          count={logic.property_bindings?.length ?? 0}
          icon={<TableOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={propertyBindingColumns}
            dataSource={logic.property_bindings}
            pagination={false}
          />
        </SectionCard>
      )}

      {(logic.related_properties?.length ?? 0) > 0 && (
        <SectionCard
          title="依赖字段"
          count={logic.related_properties?.length ?? 0}
          icon={<TableOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={propertyColumns}
            dataSource={logic.related_properties}
            pagination={false}
          />
        </SectionCard>
      )}

      {(logic.version_records?.length ?? 0) > 0 && (
        <SectionCard
          title="版本记录"
          count={logic.version_records?.length ?? 0}
          icon={<HistoryOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={versionColumns}
            dataSource={logic.version_records}
            pagination={false}
          />
        </SectionCard>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Link to={workspacePath}>
          <Button icon={<LinkOutlined />}>前往工作区编辑</Button>
        </Link>
      </div>
    </PageContainer>
  );
}
