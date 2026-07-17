import { Space, Tag, Select, Typography } from "antd";
import type { DataHubDatasetOption } from "../../types";

const { Text } = Typography;

type Props = {
  options: DataHubDatasetOption[];
  searching?: boolean;
  ensuring?: boolean;
  placeholder?: string;
  onSearch: (keyword: string) => void;
  onSelectUnmapped: (dataset: DataHubDatasetOption) => void;
  /** 是否在选项中展示 platform / urn 等扩展信息 */
  detailed?: boolean;
};

/**
 * 对象/关系编辑共用的 DataHub 承载表选择器。
 * 已映射表直接选 object_type_id；未映射表触发 onSelectUnmapped 创建对象。
 */
export function MappingDatasetSelect({
  options,
  searching = false,
  ensuring = false,
  placeholder = "输入表名搜索 DataHub 表",
  onSearch,
  onSelectUnmapped,
  detailed = false,
}: Props) {
  return (
    <Select
      showSearch
      allowClear
      loading={searching || ensuring}
      placeholder={placeholder}
      optionFilterProp="label"
      optionLabelProp="label"
      filterOption={false}
      onSearch={onSearch}
      notFoundContent={searching ? "搜索中..." : "输入关键字搜索 DataHub 表"}
      options={options.map((ds) => ({
        label: ds.display_name || ds.name,
        value: ds.object_type_id ?? `dataset:${ds.urn}`,
        dataset: ds,
      }))}
      onSelect={(_value, option) => {
        const ds = (option as { dataset?: DataHubDatasetOption }).dataset;
        if (ds && !ds.object_type_id) {
          onSelectUnmapped(ds);
        }
      }}
      optionRender={(option) => {
        const ds = (option as { dataset?: DataHubDatasetOption }).dataset;
        if (!ds) return option.label;
        if (!detailed) {
          return (
            <Space size={6}>
              <Text strong>{ds.display_name || ds.name}</Text>
              {ds.object_type_id ? (
                <Tag color="green">已映射</Tag>
              ) : (
                <Tag color="blue">将创建</Tag>
              )}
            </Space>
          );
        }
        return (
          <Space direction="vertical" size={0}>
            <Space size={6}>
              <Text strong>{ds.display_name || ds.name}</Text>
              {ds.platform ? <Tag>{ds.platform}</Tag> : null}
              {ds.object_type_id ? (
                <Tag color="green">已映射</Tag>
              ) : (
                <Tag color="blue">将创建</Tag>
              )}
            </Space>
            {ds.description ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {ds.description}
              </Text>
            ) : null}
            <Text type="secondary" code style={{ fontSize: 11 }}>
              {ds.urn}
            </Text>
          </Space>
        );
      }}
    />
  );
}
