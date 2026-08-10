import { Card, Row, Col, Typography, Space } from "antd";
import {
  ThunderboltOutlined,
  SwapOutlined,
  LineChartOutlined,
  ToolOutlined,
} from "@ant-design/icons";

const { Title, Text } = Typography;

interface TaskType {
  key: string;
  title: string;
  description: string;
  icon: React.ReactNode;
  example: string;
}

const TASK_TYPES: TaskType[] = [
  {
    key: "materialize",
    title: "物化任务",
    icon: <ThunderboltOutlined style={{ fontSize: 32, color: "#1890ff" }} />,
    description: "把本体正向生成物理表结构和数据",
    example:
      "适用场景：本体设计完成后，需要在数据仓库中创建实际的表。系统会自动生成 DDL 和数据同步作业。",
  },
  {
    key: "sync",
    title: "数据同步",
    icon: <SwapOutlined style={{ fontSize: 32, color: "#52c41a" }} />,
    description: "从源数据库同步数据到目标数据库",
    example: "适用场景：将业务系统的数据定期同步到数据仓库，保持数据实时性。",
  },
  {
    key: "transform",
    title: "数据加工",
    icon: <ToolOutlined style={{ fontSize: 32, color: "#faad14" }} />,
    description: "对已入仓的数据进行清洗、转换和加工",
    example: "适用场景：清洗脏数据、标准化字段格式、关联多表生成宽表等数据处理操作。",
  },
  {
    key: "metric",
    title: "指标任务",
    icon: <LineChartOutlined style={{ fontSize: 32, color: "#722ed1" }} />,
    description: "基于业务逻辑定义生成指标聚合表",
    example: "适用场景：根据业务规则计算 KPI、生成报表数据、构建指标体系。",
  },
];

interface Props {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function TaskTypeSelector({ value, onChange, disabled }: Props) {
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <div>
        <Title level={4}>选择任务类型</Title>
        <Text type="secondary">根据您的需求选择合适的任务类型</Text>
      </div>

      <Row gutter={[16, 16]}>
        {TASK_TYPES.map((type) => (
          <Col key={type.key} xs={24} sm={12} lg={12}>
            <Card
              hoverable={!disabled}
              style={{
                borderColor: value === type.key ? "#1890ff" : undefined,
                borderWidth: value === type.key ? 2 : 1,
                cursor: disabled ? "not-allowed" : "pointer",
                opacity: disabled ? 0.6 : 1,
              }}
              onClick={() => !disabled && onChange(type.key)}
            >
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <Space>
                  {type.icon}
                  <Title level={5} style={{ margin: 0 }}>
                    {type.title}
                  </Title>
                </Space>

                <Text>{type.description}</Text>

                <div
                  style={{
                    background: "rgba(0, 0, 0, 0.02)",
                    padding: "8px 12px",
                    borderRadius: 4,
                    fontSize: 12,
                  }}
                >
                  <Text type="secondary">{type.example}</Text>
                </div>
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
    </Space>
  );
}
