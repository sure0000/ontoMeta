import { Link } from "react-router-dom";
import { ArrowRightOutlined } from "@ant-design/icons";
import { Empty, Space, Tag } from "antd";

import type { RelationType } from "../types";

interface Props {
  /**
   * 关系列表：outgoing + incoming + implemented 已合并
   */
  relations: RelationType[];

  /**
   * 当前对象 ID，用于判断方向
   */
  currentObjectId: string;

  /**
   * 对象详情页路径生成函数
   */
  objectDetailPath?: (objectId: string) => string;

  /**
   * 关系详情页路径生成函数
   */
  relationDetailPath?: (relationId: string) => string;

  /**
   * 最多显示几条关系（默认 10）
   */
  limit?: number;
}

/**
 * 关系三元组列表：把关系写成"主语 → 谓语 → 宾语"的人话句子。
 *
 * 设计原则（docs/ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md §2）：
 * 「最小可读单元是三元组」——任何声称在展示本体的屏幕，都必须同时出现
 * 「对象 / 关系 / 对象」。只出现对象的屏叫表清单。
 */
export function RelationTriples({
  relations,
  currentObjectId,
  objectDetailPath = (id) => `/ontology/${id}`,
  relationDetailPath = (id) => `/relations/${id}`,
  limit = 10,
}: Props) {
  if (relations.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无关系"
        style={{ padding: "24px 0" }}
      />
    );
  }

  const displayRelations = limit > 0 ? relations.slice(0, limit) : relations;
  const hasMore = limit > 0 && relations.length > limit;

  return (
    <div className="relation-triples">
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        {displayRelations.map((rel) => {
          // 判断方向：当前对象是源还是目标
          const isOutgoing = rel.source_object_type_id === currentObjectId;
          const subjectId = isOutgoing ? rel.source_object_type_id : rel.target_object_type_id;
          const subjectName = isOutgoing ? rel.source_object_name : rel.target_object_name;
          const objectId = isOutgoing ? rel.target_object_type_id : rel.source_object_type_id;
          const objectName = isOutgoing ? rel.target_object_name : rel.source_object_name;

          return (
            <div key={rel.id} className="relation-triple">
              <Space size={8} align="center">
                <Link to={objectDetailPath(subjectId)} className="relation-triple__subject">
                  {subjectName || subjectId}
                </Link>

                <ArrowRightOutlined className="relation-triple__arrow" />

                <Link to={relationDetailPath(rel.id)} className="relation-triple__predicate">
                  {rel.display_name}
                </Link>

                <ArrowRightOutlined className="relation-triple__arrow" />

                <Link to={objectDetailPath(objectId)} className="relation-triple__object">
                  {objectName || objectId}
                </Link>

                {rel.structure_type && (
                  <Tag className="relation-triple__tag">
                    {rel.structure_type}
                  </Tag>
                )}
              </Space>
            </div>
          );
        })}
      </Space>

      {hasMore && (
        <div style={{ marginTop: 12, color: "var(--om-color-text-tertiary)", fontSize: 13 }}>
          还有 {relations.length - limit} 条关系，切换到「关系列表」Tab 查看全部
        </div>
      )}

      <style>{`
        .relation-triples {
          padding: 16px 0;
        }

        .relation-triple {
          padding: 8px 12px;
          border-radius: 6px;
          background: var(--om-color-fill-quaternary);
          transition: background 0.2s;
        }

        .relation-triple:hover {
          background: var(--om-color-fill-tertiary);
        }

        .relation-triple__subject,
        .relation-triple__object {
          font-weight: 500;
          color: var(--om-color-text);
        }

        .relation-triple__subject:hover,
        .relation-triple__object:hover {
          color: var(--om-color-primary);
        }

        .relation-triple__predicate {
          color: var(--om-color-primary);
          font-weight: 500;
        }

        .relation-triple__arrow {
          color: var(--om-color-text-tertiary);
          font-size: 12px;
        }

        .relation-triple__tag {
          margin-left: 4px;
          font-size: 12px;
        }
      `}</style>
    </div>
  );
}
