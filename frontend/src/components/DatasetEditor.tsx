import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { api } from "../api";
import type {
  DataAppBinding,
  DataAppDataset,
  DataSource,
  ObjectTypeSummary,
  Property,
} from "../types";

const { Text } = Typography;

const AGG_OPTIONS = ["sum", "count", "avg", "max", "min"].map((v) => ({
  label: v.toUpperCase(),
  value: v,
}));

const TIME_WINDOWS = [
  { label: "不限", value: "" },
  { label: "近 7 天", value: "last_7d" },
  { label: "近 30 天", value: "last_30d" },
  { label: "近 90 天", value: "last_90d" },
  { label: "本月", value: "this_month" },
  { label: "今日", value: "today" },
];

export interface DatasetEditorProps {
  open: boolean;
  domainId: string;
  dataSources: DataSource[];
  dataset: DataAppDataset | null; // null = 新建
  onClose: () => void;
  onSave: (payload: {
    id?: string;
    name: string;
    primary_object_type_id: string;
    binding: DataAppBinding;
    data_source_id?: string | null;
  }) => void;
}

export function DatasetEditor({
  open,
  domainId,
  dataSources,
  dataset,
  onClose,
  onSave,
}: DatasetEditorProps) {
  const [name, setName] = useState("数据集");
  const [objectId, setObjectId] = useState<string | undefined>();
  const [dimensionIds, setDimensionIds] = useState<string[]>([]);
  const [measures, setMeasures] = useState<{ propId: string; agg: string }[]>([]);
  const [timeProp, setTimeProp] = useState<string | undefined>();
  const [timeWindow, setTimeWindow] = useState<string>("");
  const [rowLimit, setRowLimit] = useState<number>(100);
  const [dataSourceId, setDataSourceId] = useState<string | undefined>();

  const [objects, setObjects] = useState<ObjectTypeSummary[]>([]);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [properties, setProperties] = useState<Property[]>([]);
  const [loadingProps, setLoadingProps] = useState(false);

  // 载入数据域下已发布对象
  useEffect(() => {
    if (!open || !domainId) return;
    setLoadingObjects(true);
    api
      .listObjectTypes({ domainId, publishedOnly: true, limit: 200 })
      .then((res) => setObjects(res.items))
      .catch(() => setObjects([]))
      .finally(() => setLoadingObjects(false));
  }, [open, domainId]);

  // 初始化表单（编辑态）
  useEffect(() => {
    if (!open) return;
    const b = dataset?.binding;
    setName(dataset?.name ?? "数据集");
    setObjectId(dataset?.primary_object_type_id ?? b?.primary_object_type_id ?? undefined);
    setDimensionIds((b?.dimensions ?? []).map((d) => d.id!).filter(Boolean));
    setMeasures(
      (b?.measures ?? [])
        .filter((m) => m.ref?.kind === "property" && m.ref.id)
        .map((m) => ({ propId: m.ref.id!, agg: m.agg || "sum" })),
    );
    setTimeProp(b?.time_range?.ref?.id ?? undefined);
    setTimeWindow(b?.time_range?.window ?? "");
    setRowLimit(b?.row_limit ?? 100);
    setDataSourceId(dataset?.data_source_id ?? undefined);
  }, [open, dataset]);

  // 载入所选对象的字段
  useEffect(() => {
    if (!objectId) {
      setProperties([]);
      return;
    }
    setLoadingProps(true);
    api
      .getObjectType(objectId)
      .then((detail) => setProperties(detail.properties ?? []))
      .catch(() => setProperties([]))
      .finally(() => setLoadingProps(false));
  }, [objectId]);

  const propMap = useMemo(
    () => new Map(properties.map((p) => [p.id, p])),
    [properties],
  );
  const propOptions = properties.map((p) => ({
    label: `${p.display_name}（${p.name}）`,
    value: p.id,
  }));

  const handleSave = () => {
    if (!objectId) {
      message.warning("请选择主对象");
      return;
    }
    const binding: DataAppBinding = {
      primary_object_type_id: objectId,
      dimensions: dimensionIds.map((id) => {
        const p = propMap.get(id);
        return { kind: "property", id, name: p?.name, display_name: p?.display_name };
      }),
      measures: measures
        .filter((m) => m.propId)
        .map((m) => {
          const p = propMap.get(m.propId);
          return {
            ref: { kind: "property", id: m.propId, name: p?.name, display_name: p?.display_name },
            agg: m.agg,
          };
        }),
      filters: [],
      time_range: timeProp
        ? {
            ref: {
              kind: "property",
              id: timeProp,
              name: propMap.get(timeProp)?.name,
              display_name: propMap.get(timeProp)?.display_name,
            },
            window: timeWindow || null,
          }
        : null,
      row_limit: rowLimit,
    };
    onSave({
      id: dataset?.id,
      name: name.trim() || "数据集",
      primary_object_type_id: objectId,
      binding,
      data_source_id: dataSourceId ?? null,
    });
  };

  return (
    <Modal
      title={dataset ? "编辑数据集" : "新建数据集"}
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      okText="保存数据集"
      width={640}
      destroyOnClose
    >
      <Form layout="vertical">
        <Form.Item label="数据集名称">
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Form.Item>

        <Form.Item label="主对象" required>
          {loadingObjects ? (
            <Spin size="small" />
          ) : objects.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="该数据域暂无已发布对象"
            />
          ) : (
            <Select
              placeholder="选择业务对象"
              value={objectId}
              onChange={setObjectId}
              options={objects.map((o) => ({
                label: `${o.display_name}（${o.name}）`,
                value: o.id,
              }))}
              showSearch
              optionFilterProp="label"
            />
          )}
        </Form.Item>

        {objectId && (
          <>
            <Form.Item label="维度（分组字段）">
              <Select
                mode="multiple"
                placeholder="选择维度字段"
                value={dimensionIds}
                onChange={setDimensionIds}
                options={propOptions}
                loading={loadingProps}
                optionFilterProp="label"
              />
            </Form.Item>

            <Divider plain>
              <Text type="secondary">度量（聚合字段）</Text>
            </Divider>
            {measures.map((m, i) => (
              <Space key={i} style={{ display: "flex", marginBottom: 8 }} align="baseline">
                <Select
                  style={{ width: 260 }}
                  placeholder="字段"
                  value={m.propId || undefined}
                  onChange={(v) =>
                    setMeasures((prev) =>
                      prev.map((x, xi) => (xi === i ? { ...x, propId: v } : x)),
                    )
                  }
                  options={propOptions}
                  optionFilterProp="label"
                  showSearch
                />
                <Select
                  style={{ width: 110 }}
                  value={m.agg}
                  onChange={(v) =>
                    setMeasures((prev) =>
                      prev.map((x, xi) => (xi === i ? { ...x, agg: v } : x)),
                    )
                  }
                  options={AGG_OPTIONS}
                />
                <Button
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setMeasures((prev) => prev.filter((_, xi) => xi !== i))}
                />
              </Space>
            ))}
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() => setMeasures((prev) => [...prev, { propId: "", agg: "sum" }])}
              style={{ marginBottom: 16 }}
            >
              添加度量
            </Button>

            <Form.Item label="时间范围">
              <Space wrap>
                <Select
                  style={{ width: 240 }}
                  allowClear
                  placeholder="时间字段（可选）"
                  value={timeProp}
                  onChange={(v) => setTimeProp(v)}
                  options={propOptions}
                  optionFilterProp="label"
                />
                <Select
                  style={{ width: 140 }}
                  value={timeWindow}
                  onChange={setTimeWindow}
                  options={TIME_WINDOWS}
                  disabled={!timeProp}
                />
              </Space>
            </Form.Item>

            <Space size="large" wrap>
              <Form.Item label="返回行数上限" style={{ marginBottom: 0 }}>
                <InputNumber
                  min={1}
                  max={1000}
                  value={rowLimit}
                  onChange={(v) => setRowLimit(v ?? 100)}
                />
              </Form.Item>
              <Form.Item label="执行数据源" style={{ marginBottom: 0 }}>
                <Select
                  style={{ width: 220 }}
                  allowClear
                  placeholder="Mock（示例数据）"
                  value={dataSourceId}
                  onChange={(v) => setDataSourceId(v)}
                  options={dataSources.map((d) => ({
                    label: (
                      <span>
                        {d.name} <Tag>{d.kind}</Tag>
                      </span>
                    ),
                    value: d.id,
                  }))}
                />
              </Form.Item>
            </Space>
          </>
        )}
      </Form>
    </Modal>
  );
}
