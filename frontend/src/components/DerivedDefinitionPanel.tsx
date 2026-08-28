import { Descriptions, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";

import { api } from "../api";
import type { DatasetEntry, DerivedDefinition } from "../types";
import { LandingStateBadge } from "./ObjectLanding";

const { Text } = Typography;

/**
 * 派生对象的定义：它是由数仓里哪几张表、按什么粒度算出来的。
 *
 * 只对派生对象显示。普通对象的数据来自它自己的源表，那件事由「物理落点」讲；派生对象
 * 的上游是**别的**对象的落点，所以要单独摆出来——否则没人说得清这张宽表的行是怎么来的。
 */

interface Props {
  objectId: string;
  /** 对象的来源；非 `derived` 不请求（省掉一次注定 404 的往返）。 */
  provenance?: string;
}

export function DerivedDefinitionPanel({ objectId, provenance }: Props) {
  const [definition, setDefinition] = useState<DerivedDefinition | null>(null);

  useEffect(() => {
    if (provenance !== "derived") {
      setDefinition(null);
      return;
    }
    let alive = true;
    api
      .getDerivedDefinition(objectId)
      .then((d) => alive && setDefinition(d))
      .catch(() => alive && setDefinition(null));
    return () => {
      alive = false;
    };
  }, [objectId, provenance]);

  if (!definition) return null;

  const columns: ColumnsType<DatasetEntry> = [
    {
      title: "上游表",
      dataIndex: "physical",
      render: (physical: string) => <Text code>{physical}</Text>,
    },
    { title: "分层", dataIndex: "layer", width: 90 },
    { title: "归属实体", dataIndex: "entity_display_name" },
    {
      title: "状态",
      dataIndex: "state",
      width: 120,
      render: (_v, row) => <LandingStateBadge state={row.state} />,
    },
  ];

  return (
    <div style={{ marginTop: 16 }}>
      <Descriptions column={{ xs: 1, md: 2 }} size="small" title="派生定义">
        <Descriptions.Item label="粒度">{definition.grain}</Descriptions.Item>
        <Descriptions.Item label="落层">
          {definition.layer ? definition.layer.toUpperCase() : "-"}
        </Descriptions.Item>
        {definition.joins.length > 0 && (
          <Descriptions.Item label="连接条件" span={2}>
            <Space orientation="vertical" size={2}>
              {definition.joins.map((j, i) => (
                <Text key={i} code>
                  {j.how || "inner"} join ·{" "}
                  {j.on.map((c) => `${c.left} = ${c.right}`).join(" AND ")}
                </Text>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {definition.notes && (
          <Descriptions.Item label="备注" span={2}>
            {definition.notes}
          </Descriptions.Item>
        )}
      </Descriptions>
      {/* 解析不到的上游要显式列出来：少列一个，定义看起来照样成立，跑起来才发现少一张表。 */}
      {definition.dangling_refs.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <Space size={6} wrap>
            <Tag color="red">上游已失效</Tag>
            {definition.dangling_refs.map((ref) => (
              <Text key={ref} code>
                {ref}
              </Text>
            ))}
          </Space>
        </div>
      )}
      <Table
        className="om-table"
        rowKey="ref"
        size="small"
        columns={columns}
        dataSource={definition.upstreams}
        pagination={false}
        scroll={{ x: "max-content" }}
      />
    </div>
  );
}
