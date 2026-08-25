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
  /** 一句话说清「这类任务把什么搬到哪」。四张卡摆在一起，靠这一行区分，不再另配一段场景说明。 */
  description: string;
  icon: React.ReactNode;
}

const TASK_TYPES: TaskType[] = [
  {
    key: "materialize",
    title: "物化任务",
    icon: <ThunderboltOutlined style={{ fontSize: 32, color: "#1890ff" }} />,
    description: "按本体在数仓里建出物理表结构（只建表，不搬数据）",
  },
  {
    key: "sync",
    title: "数据同步",
    icon: <SwapOutlined style={{ fontSize: 32, color: "#52c41a" }} />,
    description: "把本体对象的源头数据同步进数仓 ODS",
  },
  {
    key: "transform",
    title: "数据加工",
    icon: <ToolOutlined style={{ fontSize: 32, color: "#faad14" }} />,
    description: "清洗加工已入仓的 ODS 数据，写入 dim / dwd / dws",
  },
  {
    key: "metric",
    title: "指标任务",
    icon: <LineChartOutlined style={{ fontSize: 32, color: "#722ed1" }} />,
    description: "按已发布的业务口径生成指标聚合表（ADS）",
  },
];

interface Props {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function TaskTypeSelector({ value, onChange, disabled }: Props) {
  return (
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
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Space>
                {type.icon}
                <Title level={5} style={{ margin: 0 }}>
                  {type.title}
                </Title>
              </Space>

              <Text type="secondary">{type.description}</Text>
            </Space>
          </Card>
        </Col>
      ))}
    </Row>
  );
}
