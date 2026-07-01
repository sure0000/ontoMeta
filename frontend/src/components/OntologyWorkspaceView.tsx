import {
  AppstoreOutlined,
  BarsOutlined,
  NodeIndexOutlined,
  ApartmentOutlined,
} from "@ant-design/icons";
import { Row, Col, Segmented, Space, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { Link } from "react-router-dom";
import { OntologyGraphView } from "./graph";
import { SectionCard } from "./SectionCard";
import { StatusBadge } from "./StatusBadge";
import { EmptyState } from "./EmptyState";
import {
  getRelationStructureLabel,
  inferRelationEvidenceType,
  inferRelationStructureType,
} from "../utils/relation";
import type { ObjectTypeSummary, OntologyGraph, RelationType } from "../types";

type EntityTab = "objects" | "relations";
type ObjectViewMode = "list" | "cards" | "graph";

interface Props {
  objects: ObjectTypeSummary[];
  relations: RelationType[];
  graph: OntologyGraph | null;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  workspaceMode?: boolean;
}

export function OntologyWorkspaceView({
  objects,
  relations,
  graph,
  objectDetailPath = (id) => `/ontology/${id}`,
  relationDetailPath,
  workspaceMode = false,
}: Props) {
  const [entityTab, setEntityTab] = useState<EntityTab>("objects");
  const [objectView, setObjectView] = useState<ObjectViewMode>("cards");

  const objectColumns: ColumnsType<ObjectTypeSummary> = [
    {
      title: "对象名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <Link to={objectDetailPath(record.id)} className="id-link">
          <span>{record.display_name}</span>
          <span className="id-link-sub">{record.name}</span>
        </Link>
      ),
    },
    {
      title: "属性",
      dataIndex: "property_count",
      key: "property_count",
      width: 80,
      align: "right",
    },
    {
      title: "关系",
      dataIndex: "relation_count",
      key: "relation_count",
      width: 80,
      align: "right",
    },
    {
      title: "绑定逻辑",
      dataIndex: "bound_logic_count",
      key: "bound_logic_count",
      width: 110,
      render: (v?: number) =>
        v ? <Tag color="blue">{v}</Tag> : <span className="om-muted">0</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "置信度",
      dataIndex: "source_confidence",
      key: "source_confidence",
      width: 100,
      align: "right",
      render: (value?: number) => value?.toFixed(2) ?? <span className="om-muted">-</span>,
    },
  ];

  const relationColumns: ColumnsType<RelationType> = [
    {
      title: "关系语义",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) =>
        relationDetailPath ? (
          <Link to={relationDetailPath(record.id)} className="id-link">
            <span>{record.display_name}</span>
            <span className="id-link-sub">{record.name}</span>
          </Link>
        ) : (
          <span className="id-link">
            <span>{record.display_name}</span>
            <span className="id-link-sub">{record.name}</span>
          </span>
        ),
    },
    {
      title: "源对象 → 目标对象",
      key: "objects",
      render: (_, record) => (
        <Space size={6} wrap>
          {record.source_object_name ? (
            <Link to={objectDetailPath(record.source_object_type_id)}>
              {record.source_object_name}
            </Link>
          ) : (
            <span className="om-muted">-</span>
          )}
          <span className="om-muted">→</span>
          {record.target_object_name ? (
            <Link to={objectDetailPath(record.target_object_type_id)}>
              {record.target_object_name}
            </Link>
          ) : (
            <span className="om-muted">-</span>
          )}
        </Space>
      ),
    },
    {
      title: "结构类型",
      dataIndex: "structure_type",
      key: "structure_type",
      width: 110,
      render: (value, record) =>
        getRelationStructureLabel(
          value || inferRelationStructureType(record.description, record.source_evidence),
        ),
    },
    {
      title: "基数",
      dataIndex: "cardinality",
      key: "cardinality",
      width: 90,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "证据来源",
      key: "evidence",
      width: 120,
      render: (_, record) => (
        <Tag>{inferRelationEvidenceType(record.source_evidence || record.description)}</Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "置信度",
      dataIndex: "source_confidence",
      key: "source_confidence",
      width: 90,
      align: "right",
      render: (value?: number) => value?.toFixed(2) ?? <span className="om-muted">-</span>,
    },
  ];

  if (objects.length === 0 && relations.length === 0) {
    return (
      <SectionCard title="本体草稿" bodyFlush>
        <EmptyState
          title="暂无本体草稿"
          description="尚未生成本体草稿，请在工作区发起生成后查看对象与关系。"
        />
      </SectionCard>
    );
  }

  const entitySwitcher = workspaceMode ? (
    <Segmented
      value={entityTab}
      onChange={(value) => setEntityTab(value as EntityTab)}
      options={[
        { label: `对象 ${objects.length}`, value: "objects" },
        { label: `关系 ${relations.length}`, value: "relations" },
      ]}
    />
  ) : null;

  const objectViewSwitcher =
    entityTab === "objects" ? (
      <Segmented
        value={objectView}
        onChange={(value) => setObjectView(value as ObjectViewMode)}
        options={[
          {
            label: (
              <Tooltip title="列表视图">
                <BarsOutlined />
              </Tooltip>
            ),
            value: "list",
          },
          {
            label: (
              <Tooltip title="卡片视图">
                <AppstoreOutlined />
              </Tooltip>
            ),
            value: "cards",
          },
          {
            label: (
              <Tooltip title="图谱视图">
                <NodeIndexOutlined />
              </Tooltip>
            ),
            value: "graph",
          },
        ]}
      />
    ) : null;

  return (
    <div className="om-stack">
      <div className="toolbar">
        <div className="toolbar-left">{entitySwitcher}</div>
        <div className="toolbar-right">
          {objectViewSwitcher}
          {entityTab === "objects" && objectView === "graph" && (
            <span className="toolbar-text">
              {graph ? `${graph.nodes.length} 节点 · ${graph.edges.length} 关系` : "图谱生成中"}
            </span>
          )}
        </div>
      </div>

      {entityTab === "relations" ? (
        relations.length === 0 ? (
          <SectionCard title="关系列表" bodyFlush>
            <EmptyState
              title="暂无关系类型"
              description="生成草稿后将自动识别外键与血缘关系，也可在对象详情中手动补充。"
            />
          </SectionCard>
        ) : (
          <SectionCard
            title="关系列表"
            count={relations.length}
            countPrimary
            icon={<ApartmentOutlined />}
            bodyFlush
          >
            <Table
              className="om-table"
              rowKey="id"
              size="middle"
              columns={relationColumns}
              dataSource={relations}
              pagination={false}
            />
          </SectionCard>
        )
      ) : objects.length === 0 ? (
        <SectionCard title="对象列表" bodyFlush>
          <EmptyState title="暂无业务对象" />
        </SectionCard>
      ) : objectView === "graph" && graph ? (
        <SectionCard title="对象图谱" count={objects.length} countPrimary bodyFlush>
          <OntologyGraphView
            graph={graph}
            objectDetailPath={objectDetailPath}
            relationDetailPath={relationDetailPath}
          />
        </SectionCard>
      ) : objectView === "list" ? (
        <SectionCard
          title="对象列表"
          count={objects.length}
          countPrimary
          icon={<ApartmentOutlined />}
          bodyFlush
        >
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={objectColumns}
            dataSource={objects}
            pagination={false}
          />
        </SectionCard>
      ) : (
        <div>
          <Row gutter={[16, 16]}>
            {objects.map((obj) => (
              <Col key={obj.id} xs={24} sm={12} lg={8} xl={6}>
                <Link to={objectDetailPath(obj.id)} className="om-card-link">
                  <div className="entity-card">
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
                    <div className="entity-card-foot">
                      <span className="entity-card-foot-item">
                        <strong>{obj.property_count}</strong> 属性
                      </span>
                      <span className="entity-card-foot-item">
                        <strong>{obj.relation_count}</strong> 关系
                      </span>
                      <span className="entity-card-foot-item">
                        绑定 <strong>{obj.bound_logic_count ?? 0}</strong> 逻辑
                      </span>
                    </div>
                  </div>
                </Link>
              </Col>
            ))}
          </Row>
        </div>
      )}
    </div>
  );
}
