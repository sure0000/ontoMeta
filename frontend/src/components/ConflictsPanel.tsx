import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { WarningOutlined } from "@ant-design/icons";

import { api } from "../api";
import type { ConflictItem, OntologyConflicts } from "../types";

const ENTITY_LABEL: Record<string, string> = {
  object_type: "对象",
  property: "属性",
  relation_type: "关系",
  business_logic: "业务逻辑",
};

const ENTITY_COLOR: Record<string, string> = {
  object_type: "blue",
  property: "green",
  relation_type: "purple",
  business_logic: "gold",
};

const FIELD_LABEL: Record<string, string> = {
  name: "标识名",
  display_name: "业务名",
  description: "描述",
  table_role: "对象角色",
  role_reason: "角色依据",
  data_type: "数据类型",
  semantic_type: "语义类型",
  cardinality: "基数",
  structure_type: "结构类型",
  expression_summary: "口径摘要",
  logic_type: "逻辑类型",
};

type Resolution = "accept_theirs" | "keep_ours";

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "（空）";
  return String(v);
}

const rowKeyOf = (item: ConflictItem) => `${item.entity_id}:${item.field}`;

/** 展示单个「基线 / 你的值 / 上游值」，超长自动省略并以 Tooltip 显示全文。 */
function ValueCell({ label, value, variant }: { label: string; value: unknown; variant: "base" | "ours" | "theirs" }) {
  const text = fmt(value);
  const inner =
    variant === "ours" ? (
      <Typography.Text mark>{text}</Typography.Text>
    ) : variant === "theirs" ? (
      <Typography.Text code>{text}</Typography.Text>
    ) : (
      <Typography.Text type="secondary">{text}</Typography.Text>
    );
  return (
    <Tooltip title={`${label}：${text}`} placement="topLeft">
      <div style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {inner}
      </div>
    </Tooltip>
  );
}

interface Props {
  ontologyId: string;
  onChanged?: () => void;
}

/**
 * 字段级冲突复核：再生成后人工与上游双改的字段。
 *
 * 冲突量可能很大，故不再内联平铺，而是以「带角标的按钮 + 抽屉 + 可筛选/分页表格」
 * 承载：按实体类型分段筛选、按名称搜索、多选批量处置，避免一次性铺满页面。
 * 无冲突时组件自身不渲染任何内容。
 */
export function ConflictsPanel({ ontologyId, onChanged }: Props) {
  const [data, setData] = useState<OntologyConflicts | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [entityFilter, setEntityFilter] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.listOntologyConflicts(ontologyId));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载冲突失败");
    } finally {
      setLoading(false);
    }
  }, [ontologyId]);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // 各实体类型计数，用于分段筛选标签上的角标。
  const countsByType = useMemo(() => {
    const m: Record<string, number> = {};
    for (const it of items) m[it.entity_type] = (m[it.entity_type] ?? 0) + 1;
    return m;
  }, [items]);

  const filtered = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return items.filter((it) => {
      if (entityFilter !== "all" && it.entity_type !== entityFilter) return false;
      if (!kw) return true;
      return (
        (it.display_name || "").toLowerCase().includes(kw) ||
        (it.name || "").toLowerCase().includes(kw) ||
        (FIELD_LABEL[it.field] ?? it.field).toLowerCase().includes(kw)
      );
    });
  }, [items, entityFilter, search]);

  // 筛选变化后剔除不在当前视图内的已选项，避免批量处置误伤被筛掉的条目。
  useEffect(() => {
    const visible = new Set(filtered.map(rowKeyOf));
    setSelectedKeys((prev) => prev.filter((k) => visible.has(k)));
  }, [filtered]);

  const resolve = async (item: ConflictItem, resolution: Resolution) => {
    const key = rowKeyOf(item);
    setBusy(key);
    try {
      await api.resolveConflict({
        entity_type: item.entity_type,
        entity_id: item.entity_id,
        field: item.field,
        resolution,
      });
      message.success(resolution === "accept_theirs" ? "已采纳上游" : "已保留你的修改");
      await load();
      onChanged?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const resolveSelected = async (resolution: Resolution) => {
    const targets = filtered.filter((it) => selectedKeys.includes(rowKeyOf(it)));
    if (targets.length === 0) return;
    setBatchBusy(true);
    try {
      await Promise.all(
        targets.map((it) =>
          api.resolveConflict({
            entity_type: it.entity_type,
            entity_id: it.entity_id,
            field: it.field,
            resolution,
          }),
        ),
      );
      message.success(`已处理选中的 ${targets.length} 处冲突`);
      setSelectedKeys([]);
      await load();
      onChanged?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "批量处理失败");
    } finally {
      setBatchBusy(false);
    }
  };

  const resolveAll = async (resolution: Resolution) => {
    setBatchBusy(true);
    try {
      const r = await api.resolveAllConflicts(ontologyId, resolution);
      message.success(`已处理 ${r.resolved} 处冲突`);
      setSelectedKeys([]);
      await load();
      onChanged?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "批量处理失败");
    } finally {
      setBatchBusy(false);
    }
  };

  const columns: ColumnsType<ConflictItem> = [
    {
      title: "类型",
      dataIndex: "entity_type",
      width: 84,
      render: (t: string) => <Tag color={ENTITY_COLOR[t]}>{ENTITY_LABEL[t] ?? t}</Tag>,
    },
    {
      title: "实体",
      dataIndex: "display_name",
      width: 180,
      render: (_: unknown, it) => (
        <Tooltip title={it.name} placement="topLeft">
          <Typography.Text strong ellipsis style={{ maxWidth: 168 }}>
            {it.display_name || it.name}
          </Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: "字段",
      dataIndex: "field",
      width: 96,
      render: (f: string) => <Tag>{FIELD_LABEL[f] ?? f}</Tag>,
    },
    {
      title: "基线",
      dataIndex: "base",
      render: (v: unknown) => <ValueCell label="基线" value={v} variant="base" />,
    },
    {
      title: "你的值",
      dataIndex: "ours",
      render: (v: unknown) => <ValueCell label="你的值" value={v} variant="ours" />,
    },
    {
      title: "上游值",
      dataIndex: "theirs",
      render: (v: unknown) => <ValueCell label="上游值" value={v} variant="theirs" />,
    },
    {
      title: "操作",
      key: "actions",
      width: 148,
      fixed: "right",
      render: (_: unknown, it) => {
        const key = rowKeyOf(it);
        return (
          <Space size={4}>
            <Button size="small" loading={busy === key} onClick={() => resolve(it, "accept_theirs")}>
              采纳上游
            </Button>
            <Button
              size="small"
              type="primary"
              ghost
              loading={busy === key}
              onClick={() => resolve(it, "keep_ours")}
            >
              保留我的
            </Button>
          </Space>
        );
      },
    },
  ];

  // 无冲突：不占用任何页面空间。
  if (total === 0 && !loading) return null;

  const segmentOptions = [
    { label: `全部 ${total}`, value: "all" },
    ...Object.keys(ENTITY_LABEL)
      .filter((t) => countsByType[t])
      .map((t) => ({ label: `${ENTITY_LABEL[t]} ${countsByType[t]}`, value: t })),
  ];

  return (
    <>
      <Badge count={total} size="small" overflowCount={999}>
        <Button icon={<WarningOutlined />} onClick={() => setOpen(true)}>
          字段冲突复核
        </Button>
      </Badge>

      <Drawer
        title="字段冲突复核"
        width={960}
        open={open}
        onClose={() => setOpen(false)}
        destroyOnClose
        extra={
          <Space>
            <Popconfirm
              title="全部采纳上游？"
              description={`将把 ${total} 处冲突全部采纳为上游最新值。`}
              okText="确认"
              cancelText="取消"
              onConfirm={() => resolveAll("accept_theirs")}
            >
              <Button size="small" loading={batchBusy}>
                全部采纳上游
              </Button>
            </Popconfirm>
            <Popconfirm
              title="全部保留我的？"
              description={`将把 ${total} 处冲突全部保留为你的修改。`}
              okText="确认"
              cancelText="取消"
              onConfirm={() => resolveAll("keep_ours")}
            >
              <Button size="small" type="primary" ghost loading={batchBusy}>
                全部保留我的
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginTop: -4 }}>
          再生成后，人工与上游同时改动过的字段需你确认保留哪一方。逐条处置，或多选后批量处置。
        </Typography.Paragraph>

        <Space wrap style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
          <Segmented
            options={segmentOptions}
            value={entityFilter}
            onChange={(v) => setEntityFilter(v as string)}
          />
          <Input.Search
            allowClear
            placeholder="按实体名 / 字段搜索"
            style={{ width: 220 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </Space>

        {selectedKeys.length > 0 && (
          <Space style={{ marginBottom: 12 }}>
            <Typography.Text type="secondary">已选 {selectedKeys.length} 项：</Typography.Text>
            <Button size="small" loading={batchBusy} onClick={() => resolveSelected("accept_theirs")}>
              采纳上游
            </Button>
            <Button
              size="small"
              type="primary"
              ghost
              loading={batchBusy}
              onClick={() => resolveSelected("keep_ours")}
            >
              保留我的
            </Button>
            <Button size="small" type="text" onClick={() => setSelectedKeys([])}>
              取消选择
            </Button>
          </Space>
        )}

        <Table<ConflictItem>
          className="om-table"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={filtered}
          rowKey={rowKeyOf}
          scroll={{ x: 900 }}
          locale={{ emptyText: <Empty description="没有匹配的冲突" /> }}
          rowSelection={{
            selectedRowKeys: selectedKeys,
            onChange: (keys) => setSelectedKeys(keys as string[]),
          }}
          pagination={{
            defaultPageSize: 10,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${t} 条`,
          }}
        />
      </Drawer>
    </>
  );
}
