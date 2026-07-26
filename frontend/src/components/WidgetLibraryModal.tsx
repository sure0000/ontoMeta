import { useEffect, useState } from "react";
import {
  Button,
  Empty,
  Input,
  List,
  message,
  Modal,
  Popconfirm,
  Segmented,
  Space,
  Tag,
} from "antd";
import { PlusOutlined, SearchOutlined } from "@ant-design/icons";
import { Switch } from "antd";
import { api } from "../api";
import { DatasetEditor } from "./DatasetEditor";
import type { DataAppWidget, DataSource } from "../types";

const TYPE_LABEL: Record<string, string> = {
  table: "表格",
  bar: "柱状图",
  kpi: "指标卡",
};

export function WidgetLibraryModal({
  open,
  domainId,
  onClose,
  onPick,
}: {
  open: boolean;
  domainId: string;
  onClose: () => void;
  onPick: (widgetId: string) => void;
}) {
  const [widgets, setWidgets] = useState<DataAppWidget[]>([]);
  const [loading, setLoading] = useState(false);
  const [q, setQ] = useState("");
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [showEditor, setShowEditor] = useState(false);
  const [newType, setNewType] = useState("bar");
  const [crossDomain, setCrossDomain] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .listWidgets({ domainId: crossDomain ? undefined : domainId, q: q || undefined })
      .then(setWidgets)
      .catch(() => setWidgets([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (!open) return;
    load();
    api.listDataSources().then(setDataSources).catch(() => setDataSources([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, q, domainId, crossDomain]);

  const handleCreate = async (payload: {
    name: string;
    primary_object_type_id: string;
    binding: DataAppWidget["binding"];
    data_source_id?: string | null;
  }) => {
    try {
      await api.createWidget({
        domain_id: domainId,
        name: payload.name,
        widget_type: newType,
        primary_object_type_id: payload.primary_object_type_id,
        binding: payload.binding,
        data_source_id: payload.data_source_id,
        source: "manual",
      });
      message.success("已创建面板，可在面板库中复用");
      setShowEditor(false);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteWidget(id);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <Modal
      title="面板库"
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
    >
      <Space style={{ width: "100%", marginBottom: 12 }} align="center">
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索面板名称"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          allowClear
          style={{ width: 240 }}
        />
        <Segmented
          value={newType}
          onChange={(v) => setNewType(String(v))}
          options={[
            { label: "柱状图", value: "bar" },
            { label: "指标卡", value: "kpi" },
            { label: "表格", value: "table" },
          ]}
        />
        <Button icon={<PlusOutlined />} type="primary" onClick={() => setShowEditor(true)}>
          新建面板
        </Button>
        <Space size={4}>
          <Switch size="small" checked={crossDomain} onChange={setCrossDomain} />
          <span style={{ fontSize: 12, color: "var(--om-text-tertiary)" }}>跨数据域</span>
        </Space>
      </Space>

      <List
        loading={loading}
        dataSource={widgets}
        locale={{ emptyText: <Empty description="面板库为空，点击「新建面板」或在 Data Agent 中生成" /> }}
        renderItem={(w) => (
          <List.Item
            actions={[
              <Button key="add" type="link" onClick={() => onPick(w.id)}>
                加入看板
              </Button>,
              <Popconfirm key="del" title="删除该面板？" onConfirm={() => handleDelete(w.id)}>
                <Button type="link" danger>
                  删除
                </Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  {w.name}
                  <Tag>{TYPE_LABEL[w.widget_type] ?? w.widget_type}</Tag>
                  {w.source === "chat_generated" && <Tag color="blue">问数生成</Tag>}
                </Space>
              }
              description={w.description}
            />
          </List.Item>
        )}
      />

      <DatasetEditor
        open={showEditor}
        domainId={domainId}
        dataSources={dataSources}
        dataset={null}
        onClose={() => setShowEditor(false)}
        onSave={(payload) =>
          handleCreate({
            name: payload.name,
            primary_object_type_id: payload.primary_object_type_id,
            binding: payload.binding,
            data_source_id: payload.data_source_id,
          })
        }
      />
    </Modal>
  );
}
