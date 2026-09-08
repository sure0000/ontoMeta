import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Empty,
  Input,
  message,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  ExportOutlined,
  EyeOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import type { SupersetAsset, SupersetStatus } from "../types";

const TYPE_LABEL: Record<string, string> = {
  dataset: "数据集",
  chart: "图表",
  dashboard: "看板",
};

/** 对账状态。`unknown` 是「还没对过账」——它**不等于**不存在，措辞不能含糊。 */
const STATE_META: Record<string, { color: string; label: string; hint: string }> = {
  active: { color: "success", label: "在", hint: "上次对账时 Superset 里还在" },
  missing: {
    color: "error",
    label: "已不在",
    hint: "上次对账时 Superset 那边已经没有了；登记保留，便于追溯当时建过什么",
  },
  unknown: {
    color: "default",
    label: "未对账",
    hint: "还没和 Superset 对过账，不代表不存在；点「与 Superset 对账」刷新",
  },
};

const VIA_LABEL: Record<string, string> = { mcp: "Agent", web: "手工" };

export function VizAssetsPage() {
  const navigate = useNavigate();
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [keyword, setKeyword] = useState("");
  const [reconciling, setReconciling] = useState(false);

  const { data: status } = useApi<SupersetStatus>(async () => api.getSupersetStatus(), []);
  const {
    data: assets,
    loading,
    reload,
  } = useApi<SupersetAsset[]>(
    async () => (await api.listSupersetAssets()).items,
    [],
  );

  const rows = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return (assets ?? []).filter(
      (a) =>
        (typeFilter === "all" || a.asset_type === typeFilter) &&
        (!needle || a.title.toLowerCase().includes(needle)),
    );
  }, [assets, typeFilter, keyword]);

  const handleReconcile = async () => {
    setReconciling(true);
    try {
      const counts = await api.reconcileSupersetAssets();
      message.success(`对账完成：在 ${counts.active} 个，已不在 ${counts.missing} 个`);
      reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "对账失败");
    } finally {
      setReconciling(false);
    }
  };

  const handleUnlink = async (asset: SupersetAsset) => {
    try {
      await api.unlinkSupersetAsset(asset.id);
      message.success("已解除登记（Superset 里的内容未删除）");
      reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "解除登记失败");
    }
  };

  const columns = [
    {
      title: "名称",
      dataIndex: "title",
      render: (title: string, row: SupersetAsset) =>
        row.asset_type === "dashboard" && row.embedded_uuid ? (
          <Link to={`/data-apps/${row.id}`}>{title}</Link>
        ) : (
          <span>{title}</span>
        ),
    },
    {
      title: "类型",
      dataIndex: "asset_type",
      width: 110,
      render: (t: string, row: SupersetAsset) => (
        <Space size={4}>
          <Tag>{TYPE_LABEL[t] ?? t}</Tag>
          {row.viz_type ? <Tag color="blue">{row.viz_type}</Tag> : null}
        </Space>
      ),
    },
    {
      title: "落点",
      dataIndex: "dataset_ref",
      render: (ref: string | null) =>
        ref ? (
          <code>{ref}</code>
        ) : (
          <Tooltip title="没有记录落点引用，口径无法回溯到本体">
            <span style={{ color: "var(--om-text-tertiary)" }}>—</span>
          </Tooltip>
        ),
    },
    {
      title: "建者",
      dataIndex: "created_by",
      width: 150,
      render: (by: string | null, row: SupersetAsset) => (
        <Space size={4}>
          <span>{by ?? "—"}</span>
          <Tag>{VIA_LABEL[row.created_via] ?? row.created_via}</Tag>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "state",
      width: 110,
      render: (state: string) => {
        const meta = STATE_META[state] ?? STATE_META.unknown;
        return (
          <Tooltip title={meta.hint}>
            <Tag color={meta.color}>{meta.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "操作",
      width: 210,
      render: (_: unknown, row: SupersetAsset) => (
        <Space size={4}>
          {row.asset_type === "dashboard" && row.embedded_uuid ? (
            <Button
              size="small"
              icon={<EyeOutlined />}
              onClick={() => navigate(`/data-apps/${row.id}`)}
            >
              预览
            </Button>
          ) : null}
          <Button
            size="small"
            icon={<ExportOutlined />}
            href={row.url}
            target="_blank"
            rel="noreferrer"
          >
            在 Superset 打开
          </Button>
          <Popconfirm
            title="解除登记"
            description="只从这里移除，不会删除 Superset 里的内容。"
            onConfirm={() => handleUnlink(row)}
          >
            <Button size="small" danger type="text">
              解除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        title="数据应用"
        description="图表与看板建在 Apache Superset 里；这里登记 ontoMeta 建过哪些、口径来自哪个落点。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={reload}>
              刷新
            </Button>
            <Button
              icon={<SyncOutlined />}
              loading={reconciling}
              onClick={handleReconcile}
              disabled={!status?.configured}
            >
              与 Superset 对账
            </Button>
            {status?.base_url ? (
              <Button
                type="primary"
                icon={<ExportOutlined />}
                href={status.base_url}
                target="_blank"
                rel="noreferrer"
              >
                打开 Superset
              </Button>
            ) : null}
          </Space>
        }
      />

      {status && !status.configured ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="还没接入 Superset"
          description={
            <>
              {status.reason}
              ：请到「设置 → 基础设施」填写 Superset 的地址、账号密码与数仓 database
              编号，拨测通过后启用。ontoMeta 不部署 Superset，只连接已经跑着的实例。
            </>
          }
        />
      ) : null}

      <Space style={{ marginBottom: 12 }}>
        <Segmented
          value={typeFilter}
          onChange={(v) => setTypeFilter(String(v))}
          options={[
            { label: "全部", value: "all" },
            { label: "看板", value: "dashboard" },
            { label: "图表", value: "chart" },
            { label: "数据集", value: "dataset" },
          ]}
        />
        <Input.Search
          allowClear
          placeholder="按名称过滤"
          style={{ width: 240 }}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
      </Space>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        locale={{
          emptyText: (
            <Empty
              description={
                status?.configured
                  ? "还没有建过图表或看板。让 Agent 用 ontometa-viz 技能建一张试试。"
                  : "接入 Superset 后，Agent 建的图表和看板会出现在这里。"
              }
            />
          ),
        }}
      />
    </PageContainer>
  );
}
