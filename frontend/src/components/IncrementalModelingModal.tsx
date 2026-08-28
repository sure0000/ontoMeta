import { Alert, Button, Input, Modal, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import type { DraftProgress, UnmodeledTable } from "../types";
import { EmptyState } from "./EmptyState";

const { Text } = Typography;

/**
 * 增量建模：挑出域里还没进本体的表，只对选中的那几张跑生成。
 *
 * 为什么要有这一步，而不是「发现新表就自动建模」：
 * - 全域重扫的代价是几十万 token，且会把 `needs_review` 重新灌满、把部分发布的门闸打回去；
 * - 「没建模」有多种成因（真新表 / 上游改了标识 / 人工删过），哪一种该建模只有人知道。
 *
 * 清单接口实时拉 DataHub（分钟级），并顺手回填证据缓存——紧接着的生成因此用的是
 * 本清单所依据的同一份元数据，「我选的和我生成的是同一批」由此保证。
 */

interface Props {
  open: boolean;
  domainId: string;
  onClose: () => void;
  /** 生成任务已排队：交回给页面接管进度轮询。 */
  onStarted: (progress: DraftProgress) => void;
}

export function IncrementalModelingModal({ open, domainId, onClose, onStarted }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tables, setTables] = useState<UnmodeledTable[]>([]);
  const [domainTableCount, setDomainTableCount] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.listUnmodeledTables(domainId);
      setTables(result.items);
      setDomainTableCount(result.domain_table_count);
      setSelected([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取未建模表失败");
    } finally {
      setLoading(false);
    }
  }, [domainId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tables;
    return tables.filter((t) =>
      [t.name, t.display_name, t.description].some((v) => (v || "").toLowerCase().includes(q)),
    );
  }, [tables, query]);

  const columns: ColumnsType<UnmodeledTable> = useMemo(
    () => [
      {
        title: "物理表",
        dataIndex: "name",
        render: (name: string, row) => (
          <Space direction="vertical" size={0}>
            <Text code>{name}</Text>
            {row.display_name && row.display_name !== name && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {row.display_name}
              </Text>
            )}
          </Space>
        ),
      },
      {
        title: "来源",
        dataIndex: "platform",
        width: 110,
        render: (platform?: string) => (platform ? <Tag>{platform}</Tag> : "-"),
      },
      { title: "字段数", dataIndex: "field_count", width: 90 },
      {
        title: "行数",
        dataIndex: "row_count",
        width: 110,
        render: (rows?: number) => (rows == null ? "-" : rows.toLocaleString()),
      },
    ],
    [],
  );

  const submit = async () => {
    if (selected.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const progress = await api.generateObjects(domainId, selected);
      onStarted(progress);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="增量建模：只生成未建模的表"
      open={open}
      onCancel={onClose}
      width={860}
      footer={[
        <Button key="refresh" onClick={() => void load()} loading={loading}>
          重新扫描
        </Button>,
        <Button key="cancel" onClick={onClose}>
          取消
        </Button>,
        <Button
          key="ok"
          type="primary"
          disabled={selected.length === 0}
          loading={submitting}
          onClick={() => void submit()}
        >
          {selected.length > 0 ? `只生成选中的 ${selected.length} 张表` : "请先选择表"}
        </Button>,
      ]}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="只对选中的表跑生成，不重扫整个数据域"
          description={
            <>
              已建模的表、人工删除过的表，以及平台自己建的物化/同步落点表都不会出现在这里。
              生成结果按源表合并进现有本体，不影响其它对象。
              <br />
              扫描需要实时读取 DataHub，表多时可能要等一两分钟。
            </>
          }
        />
        {error && <Alert type="error" showIcon message={error} />}
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Input.Search
            allowClear
            placeholder="搜索表名 / 描述"
            style={{ width: 280 }}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Text type="secondary">
            {loading
              ? "正在扫描…"
              : `域内共 ${domainTableCount} 张表，其中 ${tables.length} 张未建模`}
          </Text>
        </Space>
        {!loading && tables.length === 0 && !error ? (
          <EmptyState title="没有未建模的表" description="这个数据域里的表都已经在本体中了。" />
        ) : (
          <Table<UnmodeledTable>
            rowKey="urn"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={filtered}
            pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
            scroll={{ y: 320 }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: (keys) => setSelected(keys as string[]),
            }}
          />
        )}
      </Space>
    </Modal>
  );
}
