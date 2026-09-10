import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
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
  Typography,
} from "antd";
import {
  BarChartOutlined,
  DashboardOutlined,
  ExportOutlined,
  ReloadOutlined,
  SyncOutlined,
  TableOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import type { SupersetAsset, SupersetStatus } from "../types";

const { Text } = Typography;

const TYPE_META: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  dashboard: { label: "看板", icon: <DashboardOutlined />, color: "purple" },
  chart: { label: "图表", icon: <BarChartOutlined />, color: "blue" },
  dataset: { label: "数据集", icon: <TableOutlined />, color: "default" },
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

export function VizAssetsPage() {
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

  // 关键词先筛，类型后筛：这样分段控件上的计数与「切过去能看到几条」始终一致。
  const matched = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (!needle) return assets ?? [];
    return (assets ?? []).filter(
      (a) =>
        a.title.toLowerCase().includes(needle) ||
        a.landings.some(
          (l) =>
            l.entity_display_name.toLowerCase().includes(needle) ||
            l.physical.toLowerCase().includes(needle) ||
            (l.ontology_name ?? "").toLowerCase().includes(needle),
        ),
    );
  }, [assets, keyword]);

  const rows = useMemo(
    () => (typeFilter === "all" ? matched : matched.filter((a) => a.asset_type === typeFilter)),
    [matched, typeFilter],
  );

  const typeOptions = useMemo(() => {
    const count = (t: string) => matched.filter((a) => a.asset_type === t).length;
    return [
      { label: `全部 ${matched.length}`, value: "all" },
      { label: `看板 ${count("dashboard")}`, value: "dashboard" },
      { label: `图表 ${count("chart")}`, value: "chart" },
      { label: `数据集 ${count("dataset")}`, value: "dataset" },
    ];
  }, [matched]);

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
      width: "38%",
      // 主操作绑在主信息上：点名字就是打开它。这样操作列不必再摆一个长按钮。
      render: (title: string, row: SupersetAsset) => {
        const meta = TYPE_META[row.asset_type];
        return (
          <Space size={8} align="start">
            <span style={{ color: "var(--om-text-tertiary)", lineHeight: "22px" }}>
              {meta?.icon}
            </span>
            <Space orientation="vertical" size={0}>
              <a href={row.url} target="_blank" rel="noreferrer">
                {title}
                <ExportOutlined style={{ marginInlineStart: 6, fontSize: 11 }} />
              </a>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {meta?.label ?? row.asset_type}
                {row.viz_type ? ` · ${row.viz_type}` : ""}
              </Text>
            </Space>
          </Space>
        );
      },
    },
    {
      title: "落点",
      dataIndex: "landings",
      // 三态要分开说：有落点 / 引用断了 / 本来就没登记。合成一个「—」会把
      // 「口径断了」说成「本来就没有」。看板还有第四种：成员图表没在这儿登记过。
      render: (_: unknown, row: SupersetAsset) => {
        const [first, ...rest] = row.landings;
        if (first) {
          const tip = (
            <div style={{ fontSize: 12 }}>
              {row.landings.map((l) => (
                <div key={l.ref}>
                  {l.entity_display_name} · {l.physical}
                  <br />
                  <span style={{ opacity: 0.75 }}>{l.ref}</span>
                </div>
              ))}
            </div>
          );
          return (
            <Tooltip title={tip}>
              <Space orientation="vertical" size={0}>
                <span>
                  {first.entity_kind === "object_type" && first.domain_id ? (
                    <Link to={`/workspace/${first.domain_id}/objects/${first.entity_id}`}>
                      {first.entity_display_name}
                    </Link>
                  ) : (
                    first.entity_display_name
                  )}
                  {rest.length ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {` 等 ${row.landings.length} 个`}
                    </Text>
                  ) : null}
                </span>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {first.ontology_name ?? "（本体未知）"}
                </Text>
              </Space>
            </Tooltip>
          );
        }
        if (row.dataset_ref) {
          return (
            <Tooltip
              title={`引用 ${row.dataset_ref} 解析不到实体：可能已被删除或降级，这张图的口径已经断了`}
            >
              <Tag color="warning">落点已失效</Tag>
            </Tooltip>
          );
        }
        return (
          <Tooltip
            title={
              row.asset_type === "dashboard"
                ? "看板的落点由成员图表推导；这些图表没有一张在 ontoMeta 登记过落点"
                : "没有记录落点引用，口径无法回溯到本体"
            }
          >
            <Text type="secondary">—</Text>
          </Tooltip>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "state",
      width: 100,
      render: (state: string, row: SupersetAsset) => {
        const meta = STATE_META[state] ?? STATE_META.unknown;
        // 「上次对账」是什么时候，得说出来——只说"在"而不说何时看过，等于没说时效。
        const when = row.last_seen_at
          ? `（${new Date(row.last_seen_at).toLocaleString()}）`
          : "";
        return (
          <Tooltip title={`${meta.hint}${when}`}>
            <Tag color={meta.color}>{meta.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "",
      width: 64,
      align: "right" as const,
      // 解除是次要操作，不该比它旁边的资产名还显眼。危险性由 Popconfirm 承担，
      // 不必再用红色抢一遍注意力。
      render: (_: unknown, row: SupersetAsset) => (
        <Popconfirm
          title="解除登记"
          description="只从这里移除，不会删除 Superset 里的内容。"
          okText="解除"
          okButtonProps={{ danger: true }}
          onConfirm={() => handleUnlink(row)}
        >
          <Button size="small" type="text" style={{ color: "var(--om-text-tertiary)" }}>
            解除
          </Button>
        </Popconfirm>
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
              ：请到「设置 → 基础设施」填写 Superset 的地址与账号密码，拨测通过后启用。
              ontoMeta 不部署 Superset，只连接已经跑着的实例。
            </>
          }
        />
      ) : null}

      <Space style={{ marginBottom: 12 }}>
        <Segmented
          value={typeFilter}
          onChange={(v) => setTypeFilter(String(v))}
          options={typeOptions}
        />
        <Input.Search
          allowClear
          placeholder="按名称、落点实体、本体或物理表过滤"
          style={{ width: 280 }}
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
