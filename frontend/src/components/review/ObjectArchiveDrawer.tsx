import { Alert, Descriptions, Drawer, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";
import { api } from "../../api";
import { useApi } from "../../hooks/useApi";
import { RelationTriples } from "../RelationTriples";
import { DecisionEvidencePanel } from "./DecisionEvidence";
import type { ObjectTypeDetail, Property } from "../../types";
import { getRoleMeta } from "../../utils/role";

const { Text } = Typography;

interface Props {
  objectId: string | null;
  open: boolean;
  onClose: () => void;
  /** 工作区数据域 id，用于「去编辑」的深链。 */
  domainId?: string;
}

const PROPERTY_COLUMNS: ColumnsType<Property> = [
  { title: "字段", dataIndex: "display_name", key: "display_name" },
  { title: "标识名", dataIndex: "name", key: "name" },
  { title: "类型", dataIndex: "data_type", key: "data_type", width: 120 },
  { title: "语义", dataIndex: "semantic_type", key: "semantic_type", width: 110 },
];

/**
 * 对象完整档案抽屉。
 *
 * 审核时要看细节（字段构成、连了谁）是常事，但**不能因此离开队列**——跳出去再回来，
 * 位置、选择集、刚判到哪一组全得重建。抽屉让「看一眼」保持在同一屏内。
 * 真要编辑再去工作区详情页，那是另一件事。
 */
export function ObjectArchiveDrawer({ objectId, open, onClose, domainId }: Props) {
  const { data, loading, error } = useApi<ObjectTypeDetail | null>(
    () => (objectId && open ? api.getObjectType(objectId) : Promise.resolve(null)),
    [objectId, open],
  );

  return (
    <Drawer
      title={data?.display_name ?? "对象档案"}
      open={open}
      onClose={onClose}
      width={720}
      extra={
        data && domainId ? (
          <Link to={`/workspace/${domainId}/objects/${data.id}`} target="_blank" rel="noreferrer">
            去工作区编辑 →
          </Link>
        ) : null
      }
    >
      {error && <Alert type="error" message={error} showIcon />}
      <Spin spinning={loading}>
        {data && (
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <Descriptions column={{ xs: 1, md: 2 }} size="small">
              <Descriptions.Item label="标识名">{data.name}</Descriptions.Item>
              <Descriptions.Item label="角色">
                <Tag color={getRoleMeta(data.table_role).color}>
                  {getRoleMeta(data.table_role).label}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="所属板块">{data.segment_name || "未接入"}</Descriptions.Item>
              <Descriptions.Item label="行数">{data.row_count ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="描述" span={2}>
                {data.description || "暂无描述"}
              </Descriptions.Item>
            </Descriptions>

            <section>
              <Text strong>字段（{data.properties.length}）</Text>
              <Table
                className="om-table"
                style={{ marginTop: 8 }}
                rowKey="id"
                size="small"
                columns={PROPERTY_COLUMNS}
                dataSource={data.properties}
                pagination={data.properties.length > 12 ? { pageSize: 12 } : false}
              />
            </section>

            <section>
              <Text strong>
                外键关系（{data.outgoing_relations.length + data.incoming_relations.length}）
              </Text>
              <div style={{ marginTop: 8 }}>
                <RelationTriples
                  relations={[...data.outgoing_relations, ...data.incoming_relations]}
                  currentObjectId={data.id}
                  limit={0}
                />
              </div>
            </section>

            <section>
              <Text strong>判定依据</Text>
              <div style={{ marginTop: 8 }}>
                <DecisionEvidencePanel obj={data} />
              </div>
            </section>
          </div>
        )}
      </Spin>
    </Drawer>
  );
}
