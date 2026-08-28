import { Alert, Button, Modal, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { ObjectTypeSummary, UnclaimedTable } from "../types";
import { EmptyState } from "./EmptyState";

const { Text } = Typography;

/**
 * 无主表：数仓里存在、本体里没人认领的表。
 *
 * 成因有三类——对象被人工删掉而表还在（登记成了孤儿）、ontoMeta 接管之前就有的表、
 * 别处手工建的表。对它们只给**两个**出路：认领为某个已有对象的落点，或者不管它。
 *
 * **没有「照着这张表建个对象」**：照物理表反推出来的对象正是重复对象的来源。要建模就
 * 走建模（未建模表清单 / 派生对象），不要让扫描结果直接变成本体成员。
 */

interface Props {
  open: boolean;
  ontologyId: string;
  onClose: () => void;
  /** 认领成功后通知上层刷新数据集目录。 */
  onClaimed: () => void;
}

export function UnclaimedTablesModal({ open, ontologyId, onClose, onClaimed }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tables, setTables] = useState<UnclaimedTable[]>([]);
  const [scanned, setScanned] = useState<string[]>([]);
  const [objects, setObjects] = useState<ObjectTypeSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [ownerId, setOwnerId] = useState<string | undefined>();
  const [claiming, setClaiming] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [result, page] = await Promise.all([
        api.listUnclaimedTables(ontologyId),
        api.listObjectTypes({ ontologyId, publishedOnly: false, limit: 500 }),
      ]);
      setTables(result.items);
      setScanned(result.scanned_databases);
      setObjects(page.items);
      setSelected(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "清点无主表失败");
    } finally {
      setLoading(false);
    }
  }, [ontologyId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const handleClaim = async () => {
    const row = tables.find((t) => t.physical === selected);
    if (!row || !ownerId) return;
    setClaiming(true);
    setError(null);
    try {
      await api.claimTable(ontologyId, {
        object_type_id: ownerId,
        database: row.database,
        table: row.table,
      });
      const owner = objects.find((o) => o.id === ownerId);
      message.success(`${row.physical} 已登记为「${owner?.display_name ?? "对象"}」的落点`);
      setTables((prev) => prev.filter((t) => t.physical !== row.physical));
      setSelected(null);
      setOwnerId(undefined);
      onClaimed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "认领失败");
    } finally {
      setClaiming(false);
    }
  };

  const columns: ColumnsType<UnclaimedTable> = [
    {
      title: "物理表",
      dataIndex: "physical",
      render: (physical: string) => <Text code>{physical}</Text>,
    },
    {
      title: "分层",
      dataIndex: "layer",
      width: 110,
      render: (layer?: string) =>
        layer ? <Tag>{layer.toUpperCase()}</Tag> : <Text type="secondary">未知</Text>,
    },
  ];

  return (
    <Modal
      title="无主表"
      open={open}
      onCancel={onClose}
      width={720}
      footer={null}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="认领只登记归属，不代表平台搬过这张表的数据"
        description="认领后它会出现在「数仓落点」里、可以作为下游加工的源；但最近成功时间会留空，也不会因此放行查询——那要等一次真实执行成功后的对账。"
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      {tables.length === 0 && !loading ? (
        <EmptyState
          title="没有无主表"
          description={
            scanned.length
              ? `已扫描：${scanned.join("、")}。这些库里的表都有本体实体认领。`
              : "没有扫到任何库——目标数仓里还没有本体写过的库。"
          }
        />
      ) : (
        <>
          <Table
            className="om-table"
            rowKey="physical"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={tables}
            scroll={{ x: "max-content", y: 320 }}
            pagination={false}
            rowSelection={{
              type: "radio",
              selectedRowKeys: selected ? [selected] : [],
              onChange: (keys) => setSelected(keys[0] ? String(keys[0]) : null),
            }}
          />
          <Space style={{ marginTop: 16 }} wrap>
            <Select
              style={{ width: 320 }}
              placeholder="选择这张表归属的业务对象"
              value={ownerId}
              onChange={setOwnerId}
              disabled={!selected}
              showSearch
              optionFilterProp="label"
              options={objects.map((o) => ({
                value: o.id,
                label: `${o.display_name}（${o.name}）`,
              }))}
            />
            <Button
              type="primary"
              disabled={!selected || !ownerId}
              loading={claiming}
              onClick={handleClaim}
            >
              认领为落点
            </Button>
            {/* 唯一的另一个出路就是「不管它」——这里不提供「照着这张表建对象」。 */}
            <Text type="secondary">要为这张表建模，请走未建模表清单或派生对象</Text>
          </Space>
        </>
      )}
      {scanned.length > 0 && tables.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已扫描：{scanned.join("、")}
          </Text>
        </div>
      )}
    </Modal>
  );
}
