import { DatabaseOutlined, ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Input, Segmented, Space, Table, Tag, Tooltip, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import type { DatasetEntry } from "../types";
import { DeriveObjectModal } from "./DeriveObjectModal";
import { EmptyState } from "./EmptyState";
import { UnclaimedTablesModal } from "./UnclaimedTablesModal";
import { LandingStateBadge } from "./ObjectLanding";
import { SectionCard } from "./SectionCard";

const { Text } = Typography;

/**
 * 数据集目录：这个本体在数仓里落成了哪些物理表。
 *
 * 同步/加工**不产生新本体**——ODS、DWD 表都是既有实体的物理投影。此前这些表只在后端
 * 登记着、在对象详情里露一个徽标，没有一个地方能整体看见「数仓里现在有什么、哪张能用」，
 * 于是「同步完之后指不到那张表」。这里就是那个地方。
 *
 * 每行带一个稳定引用 `ref`（指存储槽位，不指分层），它是后续要写进任务配置的东西；
 * 物理表名只用于展示与复制，它会随契约变。
 */

const LAYER_LABEL: Record<string, string> = {
  ods: "ODS",
  dim: "DIM",
  dwd: "DWD",
  dws: "DWS",
  ads: "ADS",
  serving: "服务层",
};

const LAYER_COLOR: Record<string, string> = {
  ods: "default",
  dim: "cyan",
  dwd: "blue",
  dws: "geekblue",
  ads: "purple",
};

interface Props {
  ontologyId: string;
  /** 对象详情路径；给了就把「归属实体」做成链接。 */
  objectDetailPath?: (objectId: string) => string;
  /** 派生出新对象后通知上层刷新对象列表（新对象不刷新就要等下一次进页面才看得见）。 */
  onObjectCreated?: () => void;
}

export function DatasetCatalogPanel({
  ontologyId,
  objectDetailPath,
  onObjectCreated,
}: Props) {
  const [entries, setEntries] = useState<DatasetEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [layer, setLayer] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [selectedRefs, setSelectedRefs] = useState<string[]>([]);
  const [deriveOpen, setDeriveOpen] = useState(false);
  const [unclaimedOpen, setUnclaimedOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setEntries(await api.listDatasets(ontologyId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取数据集目录失败");
    } finally {
      setLoading(false);
    }
  }, [ontologyId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 过滤在前端做：目录是一个本体的落点全集（对象数以千计，落点只有已登记的那些，
  // 量级差一到两个数量级），一次取回再筛比每次改筛选条件都打一次接口更跟手。
  const layers = useMemo(() => {
    const seen: string[] = [];
    for (const entry of entries) if (!seen.includes(entry.layer)) seen.push(entry.layer);
    return seen;
  }, [entries]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return entries.filter((entry) => {
      if (layer !== "all" && entry.layer !== layer) return false;
      if (!q) return true;
      return [entry.physical, entry.entity_display_name, entry.entity_name].some((v) =>
        (v || "").toLowerCase().includes(q),
      );
    });
  }, [entries, layer, query]);

  const columns: ColumnsType<DatasetEntry> = useMemo(
    () => [
      {
        title: "物理表",
        dataIndex: "physical",
        render: (physical: string, row) => (
          // 稳定引用只放进悬浮提示：它是给任务配置与 Agent 用的句柄，摆在表面上只是
          // 一行 UUID 噪声，而人要复制的永远是表名。
          <Tooltip title={`引用：${row.ref}`}>
            <Text code copyable={{ text: physical }}>
              {physical}
            </Text>
          </Tooltip>
        ),
      },
      {
        title: "分层",
        dataIndex: "layer",
        width: 110,
        render: (value: string, row) => (
          <Space size={4}>
            <Tag color={LAYER_COLOR[value] || "default"}>{LAYER_LABEL[value] || value}</Tag>
            {row.mode && <Tag>{row.mode}</Tag>}
          </Space>
        ),
      },
      {
        title: "归属实体",
        dataIndex: "entity_display_name",
        render: (name: string, row) => {
          const label = (
            <Space orientation="vertical" size={0}>
              <span>{name}</span>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {row.entity_kind === "business_logic" ? "业务逻辑" : "业务对象"} · {row.entity_name}
              </Text>
            </Space>
          );
          return row.entity_kind === "object_type" && objectDetailPath ? (
            <Link to={objectDetailPath(row.entity_id)}>{label}</Link>
          ) : (
            label
          );
        },
      },
      {
        title: "状态",
        dataIndex: "state",
        width: 170,
        render: (_state: DatasetEntry["state"], row) => (
          <Space size={4}>
            <LandingStateBadge state={row.state} />
            {/* 「能不能当源」「能不能直接查」是两件事：前者只要表在，后者要口径跑完。
                合成一个绿灯会让人以为 ODS 也能直接查。 */}
            {row.source_ready && <Tag color="green">可作源表</Tag>}
            {row.queryable && <Tag color="blue">可查询</Tag>}
          </Space>
        ),
      },
      {
        title: "最近成功",
        dataIndex: "last_success_at",
        width: 170,
        render: (value?: string) =>
          value ? (
            new Date(value).toLocaleString()
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
    ],
    [objectDetailPath],
  );

  // 选中顺序就是上游顺序，**第一个是主表**——antd 的 selectedRowKeys 不保序，故自己记。
  const selectedUpstreams = useMemo(
    () =>
      selectedRefs
        .map((ref) => entries.find((e) => e.ref === ref))
        .filter((e): e is DatasetEntry => Boolean(e)),
    [selectedRefs, entries],
  );

  const extra = (
    <Space size={8}>
      <Button
        size="small"
        type="primary"
        disabled={selectedUpstreams.length === 0}
        onClick={() => setDeriveOpen(true)}
      >
        派生业务对象{selectedUpstreams.length ? `（${selectedUpstreams.length}）` : ""}
      </Button>
      {/* 反向入口：数仓里有、本体里没人认领的表。这张表列的是「已认领」的那一面，
          无主的那一面要有地方看得见，否则它们对治理就是不存在的。 */}
      <Button size="small" onClick={() => setUnclaimedOpen(true)}>
        无主表
      </Button>
      <Input
        allowClear
        size="small"
        style={{ width: 200 }}
        prefix={<SearchOutlined style={{ color: "var(--om-text-secondary)" }} />}
        placeholder="搜索表名或实体"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {layers.length > 1 && (
        <Segmented
          size="small"
          value={layer}
          onChange={(value) => setLayer(String(value))}
          options={[
            { label: "全部", value: "all" },
            ...layers.map((l) => ({ label: LAYER_LABEL[l] || l, value: l })),
          ]}
        />
      )}
      <Tooltip title="重新读取落点">
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()} />
      </Tooltip>
    </Space>
  );

  return (
    <SectionCard
      title="数仓落点"
      count={entries.length}
      icon={<DatabaseOutlined />}
      extra={extra}
      bodyFlush
    >
      {error ? (
        <EmptyState title="读取失败" description={error} />
      ) : entries.length === 0 && !loading ? (
        <EmptyState
          title="还没有任何落点"
          description="同步 / 物化 / 清洗任务跑起来之后，这个本体的对象会在这里显示它们落在数仓的哪张表上。任务产出的表不会变成新的业务对象。"
        />
      ) : (
        <Table
          className="om-table"
          rowKey="ref"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={filtered}
          scroll={{ x: "max-content" }}
          rowSelection={{
            selectedRowKeys: selectedRefs,
            // 勾选顺序即上游顺序，所以按增量维护而不是直接取 antd 给的 keys。
            onSelect: (row, checked) =>
              setSelectedRefs((prev) =>
                checked ? [...prev, row.ref] : prev.filter((r) => r !== row.ref),
              ),
            onSelectAll: (checked, _rows, changed) =>
              setSelectedRefs((prev) =>
                checked
                  ? [...prev, ...changed.map((r) => r.ref).filter((r) => !prev.includes(r))]
                  : prev.filter((r) => !changed.some((c) => c.ref === r)),
              ),
            getCheckboxProps: (row) => ({
              // 口径的 ADS 表列由口径定义决定，本体里没有属性可挑——不给选，
              // 而不是选了以后发现字段列表是空的。
              disabled: row.entity_kind !== "object_type",
            }),
          }}
          pagination={{
            pageSize: 20,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
          }}
        />
      )}
      {unclaimedOpen && (
        <UnclaimedTablesModal
          open={unclaimedOpen}
          ontologyId={ontologyId}
          onClose={() => setUnclaimedOpen(false)}
          onClaimed={() => {
            void load();
            onObjectCreated?.();
          }}
        />
      )}
      {deriveOpen && selectedUpstreams.length > 0 && (
        <DeriveObjectModal
          open={deriveOpen}
          ontologyId={ontologyId}
          upstreams={selectedUpstreams}
          onClose={() => setDeriveOpen(false)}
          onCreated={(created) => {
            message.success(`已创建派生对象「${created.display_name}」（${created.layer.toUpperCase()} 层）`);
            setDeriveOpen(false);
            setSelectedRefs([]);
            void load();
            onObjectCreated?.();
          }}
        />
      )}
    </SectionCard>
  );
}
