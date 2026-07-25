import { useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Dropdown,
  Empty,
  Input,
  message,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EyeOutlined,
  FilterOutlined,
  AppstoreAddOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "../components/DataAppRenderer";
import { DatasetEditor } from "../components/DatasetEditor";
import { DataSourcesModal } from "../components/DataSourcesModal";
import {
  ParamBar,
  buildRuntimeFilters,
  type DrillFilter,
} from "../components/ParamBar";
import {
  ScreenCanvas,
  exportCsv,
  newWidget,
  type ScreenWidget,
} from "../components/ScreenCanvas";
import { DashboardGrid, newTile, type DashboardTile } from "../components/DashboardGrid";
import { WidgetLibraryModal } from "../components/WidgetLibraryModal";
import type {
  DataAppDataset,
  DataAppDetail,
  DataAppPreviewResult,
  DataSource,
  RuntimeFilter,
  ScreenParam,
} from "../types";

const { Text, Paragraph } = Typography;

export function DataAppEditorPage() {
  const { appId } = useParams<{ appId: string }>();
  const navigate = useNavigate();
  const [previews, setPreviews] = useState<Record<string, DataAppPreviewResult>>({});
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishComment, setPublishComment] = useState("");
  const [showPublish, setShowPublish] = useState(false);
  const [showDatasetEditor, setShowDatasetEditor] = useState(false);
  const [editingDataset, setEditingDataset] = useState<DataAppDataset | null>(null);
  const [showDataSources, setShowDataSources] = useState(false);
  const [selectedWidget, setSelectedWidget] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const widgetsRef = useRef<ScreenWidget[]>([]);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [drills, setDrills] = useState<DrillFilter[]>([]);
  const [showWidgetLib, setShowWidgetLib] = useState(false);
  const [widgetPreviews, setWidgetPreviews] = useState<Record<string, DataAppPreviewResult>>({});

  const { data: app, loading, reload, setData } = useApi<DataAppDetail>(
    async () => api.getDataApp(appId!),
    [appId],
  );
  const { data: dataSources } = useApi<DataSource[]>(
    async () => api.listDataSources(),
    [showDataSources],
  );

  const datasetIndexById = useMemo(() => {
    const map = new Map<string, number>();
    (app?.datasets ?? []).forEach((d, i) => map.set(d.id, i));
    return map;
  }, [app]);

  const runPreview = async (datasetId: string, filters?: RuntimeFilter[]) => {
    if (!appId) return;
    setPreviewing(datasetId);
    try {
      const res = await api.previewDataAppDataset(appId, datasetId, 50, filters);
      setPreviews((prev) => ({ ...prev, [datasetId]: res }));
      if (res.warnings?.length) message.warning(res.warnings.join("；"));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预览失败");
    } finally {
      setPreviewing(null);
    }
  };

  const previewByIndex = useMemo(() => {
    const out: Record<number, DataAppPreviewResult> = {};
    Object.entries(previews).forEach(([dsId, p]) => {
      const idx = datasetIndexById.get(dsId);
      if (idx !== undefined) out[idx] = p;
    });
    return out;
  }, [previews, datasetIndexById]);

  const handleRename = async (name: string) => {
    if (!appId || !name.trim()) return;
    try {
      const updated = await api.updateDataApp(appId, { name: name.trim() });
      setData(updated);
      message.success("已保存名称");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    }
  };

  const handleSaveDataset = async (payload: {
    id?: string;
    name: string;
    primary_object_type_id: string;
    binding: DataAppDataset["binding"];
    data_source_id?: string | null;
  }) => {
    if (!app || !appId) return;
    const others = app.datasets.filter((d) => d.id !== payload.id);
    const merged = [
      ...others.map((d) => ({
        id: d.id,
        name: d.name,
        primary_object_type_id: d.primary_object_type_id,
        binding: d.binding,
        data_source_id: d.data_source_id,
      })),
      {
        id: payload.id,
        name: payload.name,
        primary_object_type_id: payload.primary_object_type_id,
        binding: payload.binding,
        data_source_id: payload.data_source_id,
      },
    ];
    setSaving(true);
    try {
      const updated = await api.updateDataApp(appId, { datasets: merged });
      setData(updated);
      setShowDatasetEditor(false);
      setEditingDataset(null);
      message.success("已保存数据集");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteDataset = async (dsId: string) => {
    if (!app || !appId) return;
    const remaining = app.datasets
      .filter((d) => d.id !== dsId)
      .map((d) => ({
        id: d.id,
        name: d.name,
        primary_object_type_id: d.primary_object_type_id,
        binding: d.binding,
        data_source_id: d.data_source_id,
      }));
    const updated = await api.updateDataApp(appId, { datasets: remaining });
    setData(updated);
  };

  const updateSpec = async (spec: Record<string, unknown>) => {
    if (!appId) return;
    const updated = await api.updateDataApp(appId, { spec });
    setData(updated);
  };

  const handleWidgetsChange = (widgets: ScreenWidget[]) => {
    if (!app) return;
    widgetsRef.current = widgets;
    const spec = { ...(app.spec ?? {}), widgets };
    // 本地即时更新，避免拖拽卡顿；松手后持久化
    setData({ ...app, spec });
  };

  const commitWidgets = async () => {
    if (!app) return;
    await updateSpec({ ...(app.spec ?? {}), widgets: widgetsRef.current });
  };

  const addWidget = async (type: string) => {
    if (!app) return;
    const widgets = [...((app.spec?.widgets as ScreenWidget[]) ?? []), newWidget(type)];
    widgetsRef.current = widgets;
    await updateSpec({ ...(app.spec ?? {}), widgets });
  };

  // ---- dashboard tiles ----
  const addTile = async (type: string) => {
    if (!app) return;
    const tiles = [...((app.spec?.tiles as DashboardTile[]) ?? []), newTile(type, 0)];
    await updateSpec({ ...(app.spec ?? {}), tiles });
  };
  const patchTile = async (id: string, patch: Partial<DashboardTile>) => {
    if (!app) return;
    const tiles = ((app.spec?.tiles as DashboardTile[]) ?? []).map((t) =>
      t.id === id ? { ...t, ...patch } : t,
    );
    await updateSpec({ ...(app.spec ?? {}), tiles });
  };
  const removeTile = async (id: string) => {
    if (!app) return;
    const tiles = ((app.spec?.tiles as DashboardTile[]) ?? []).filter((t) => t.id !== id);
    await updateSpec({ ...(app.spec ?? {}), tiles });
  };
  const commitTiles = (tiles: DashboardTile[]) => {
    if (!app) return;
    setData({ ...app, spec: { ...(app.spec ?? {}), tiles } });
  };
  const persistTiles = async (tiles: DashboardTile[]) => {
    if (!app) return;
    await updateSpec({ ...(app.spec ?? {}), tiles });
  };

  const handlePublish = async () => {
    if (!appId) return;
    setPublishing(true);
    try {
      const updated = await api.publishDataApp(appId, publishComment || undefined);
      setData(updated);
      setShowPublish(false);
      setPublishComment("");
      message.success(`已发布 v${updated.published_version}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "发布失败");
    } finally {
      setPublishing(false);
    }
  };

  if (loading) {
    return (
      <PageContainer>
        <Spin />
      </PageContainer>
    );
  }
  if (!app) {
    return (
      <PageContainer>
        <Empty description="数据应用不存在" />
      </PageContainer>
    );
  }

  const isScreen = app.app_type === "screen";
  const isDashboard = app.app_type === "dashboard";
  const tiles = (app.spec?.tiles as DashboardTile[]) ?? [];
  const widgets = (app.spec?.widgets as ScreenWidget[]) ?? [];
  widgetsRef.current = widgets;
  const selected = widgets.find((w) => w.id === selectedWidget) ?? null;
  const params = (app.spec?.params as ScreenParam[]) ?? [];
  const runtimeFilters = buildRuntimeFilters(params, paramValues, drills);

  const previewAll = (filters = runtimeFilters) => {
    app.datasets.forEach((d) => runPreview(d.id, filters));
    // 看板：预览引用的图表资产 tile
    tiles.forEach((t) => {
      if (t.widget_id) void previewWidgetTile(t.widget_id, filters);
    });
  };

  const previewWidgetTile = async (widgetId: string, filters = runtimeFilters) => {
    try {
      const res = await api.previewWidget(widgetId, 50, filters);
      setWidgetPreviews((prev) => ({ ...prev, [widgetId]: res }));
    } catch {
      /* ignore individual widget preview failure */
    }
  };

  const handleAddWidget = async (widgetId: string) => {
    if (!appId) return;
    try {
      const updated = await api.addWidgetToDashboard(appId, widgetId);
      setData(updated);
      setShowWidgetLib(false);
      message.success("已加入看板");
      void previewWidgetTile(widgetId);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加入失败");
    }
  };

  const handleDrill = (widget: ScreenWidget, column: string, value: string) => {
    const nextDrills = [...drills.filter((d) => d.column !== column), { column, value }];
    setDrills(nextDrills);
    const filters = buildRuntimeFilters(params, paramValues, nextDrills);
    const ds = app.datasets[widget.datasetIndex ?? 0];
    if (ds) runPreview(ds.id, filters);
  };

  const addParam = async () => {
    const id = `p${Date.now()}`;
    const nextParams = [
      ...params,
      { id, label: "筛选", column: "", op: "eq" } as ScreenParam,
    ];
    await updateSpec({ ...(app.spec ?? {}), params: nextParams });
  };

  const updateParam = async (id: string, patch: Partial<ScreenParam>) => {
    const nextParams = params.map((p) => (p.id === id ? { ...p, ...patch } : p));
    await updateSpec({ ...(app.spec ?? {}), params: nextParams });
  };

  const removeParam = async (id: string) => {
    await updateSpec({
      ...(app.spec ?? {}),
      params: params.filter((p) => p.id !== id),
    });
  };

  const renderPreview = (datasetId: string, widgetType?: string) => {
    const p = previews[datasetId];
    if (!p) {
      return <Text type="secondary">点击「预览」拉取数据</Text>;
    }
    const props = { columns: p.columns, rows: p.rows };
    if (widgetType === "bar") return <BarChartRender {...props} />;
    if (widgetType === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <PageContainer>
      <PageHeader
        icon={<ArrowLeftOutlined onClick={() => navigate("/data-apps")} style={{ cursor: "pointer" }} />}
        title={
          <Space>
            <Input
              defaultValue={app.name}
              variant="borderless"
              style={{ fontSize: 20, fontWeight: 600, width: 280 }}
              onBlur={(e) => handleRename(e.target.value)}
            />
            <Tag color={app.status === "published" ? "success" : "default"}>
              {app.status === "published" ? `已发布 v${app.published_version}` : `草稿 v${app.current_version}`}
            </Tag>
            <Tag>{isScreen ? "可视化大屏" : "数据表格"}</Tag>
            {saving && <Tag color="processing">保存中…</Tag>}
          </Space>
        }
        description={app.description}
        extra={
          <Space wrap>
            <Button icon={<DatabaseOutlined />} onClick={() => setShowDataSources(true)}>
              数据源
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => reload()}>
              刷新
            </Button>
            {app.status === "published" && (
              <Button icon={<EyeOutlined />} onClick={() => navigate(`/apps/${app.id}`)}>
                查看已发布
              </Button>
            )}
            <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => setShowPublish(true)}>
              发布
            </Button>
          </Space>
        }
      />

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title={<Space><span>数据集</span><Tag>{app.datasets.length}</Tag></Space>}
        extra={
          <Button
            type="primary"
            size="small"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditingDataset(null);
              setShowDatasetEditor(true);
            }}
          >
            新建数据集
          </Button>
        }
      >
        {app.datasets.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无数据集。可在「Data Agent」提问后一键生成，或点此新建并绑定本体对象/字段。"
          />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }}>
            {app.datasets.map((ds) => (
              <div
                key={ds.id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "6px 8px",
                  borderBottom: "1px solid var(--om-border, #eee)",
                }}
              >
                <Space>
                  <Text strong>{ds.name}</Text>
                  {ds.data_source_id ? (
                    <Tag color="green">已接数据源</Tag>
                  ) : (
                    <Tag>Mock</Tag>
                  )}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {ds.compiled_sql ? "已编译" : "未落地"}
                  </Text>
                </Space>
                <Space>
                  <Button
                    size="small"
                    loading={previewing === ds.id}
                    onClick={() => runPreview(ds.id)}
                  >
                    预览
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditingDataset(ds);
                      setShowDatasetEditor(true);
                    }}
                  >
                    编辑
                  </Button>
                  <Button
                    size="small"
                    danger
                    type="text"
                    icon={<DeleteOutlined />}
                    onClick={() => handleDeleteDataset(ds.id)}
                  />
                </Space>
              </div>
            ))}
          </Space>
        )}
      </Card>

      {isDashboard ? (
        <Card
          title="看板布局"
          extra={
            <Space>
              <Dropdown
                menu={{
                  items: [
                    { key: "table", label: "表格" },
                    { key: "bar", label: "柱状图" },
                    { key: "kpi", label: "指标卡" },
                  ],
                  onClick: ({ key }) => addTile(key),
                }}
                disabled={app.datasets.length === 0}
              >
                <Button icon={<PlusOutlined />} disabled={app.datasets.length === 0}>
                  添加图表
                </Button>
              </Dropdown>
              <Button icon={<AppstoreAddOutlined />} onClick={() => setShowWidgetLib(true)}>
                图表库
              </Button>
              <Button icon={<FilterOutlined />} onClick={addParam}>
                添加筛选参数
              </Button>
              <Button onClick={() => previewAll()}>预览全部</Button>
            </Space>
          }
        >
          {app.datasets.length === 0 && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="请先在上方创建数据集（可多个），再添加图表并自由拖拽拼接成看板。"
            />
          )}
          {params.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                {params.map((p) => (
                  <Space key={p.id} wrap>
                    <Text type="secondary">参数</Text>
                    <Input
                      size="small"
                      style={{ width: 120 }}
                      value={p.label}
                      placeholder="标题"
                      onChange={(e) => updateParam(p.id, { label: e.target.value })}
                    />
                    <Input
                      size="small"
                      style={{ width: 160 }}
                      value={p.column}
                      placeholder="列名（如 channel）"
                      onChange={(e) => updateParam(p.id, { column: e.target.value })}
                    />
                    <Button
                      size="small"
                      danger
                      type="text"
                      icon={<DeleteOutlined />}
                      onClick={() => removeParam(p.id)}
                    />
                  </Space>
                ))}
              </Space>
            </div>
          )}
          <ParamBar
            params={params}
            values={paramValues}
            drills={drills}
            onChange={setParamValues}
            onClearDrill={(i) => {
              const next = drills.filter((_, xi) => xi !== i);
              setDrills(next);
              previewAll(buildRuntimeFilters(params, paramValues, next));
            }}
            onApply={() => previewAll()}
          />
          <DashboardGrid
            tiles={tiles}
            grid={app.spec?.grid as { cols?: number; rowHeight?: number; gap?: number }}
            datasets={app.datasets.map((d) => ({ id: d.id, name: d.name }))}
            previews={previewByIndex}
            widgetPreviews={widgetPreviews}
            editable
            onLayoutChange={commitTiles}
            onTilePatch={patchTile}
            onRemoveTile={removeTile}
            onDrill={(tile, column, value) => {
              const nextDrills = [
                ...drills.filter((d) => d.column !== column),
                { column, value },
              ];
              setDrills(nextDrills);
              const filters = buildRuntimeFilters(params, paramValues, nextDrills);
              if (tile.widget_id) {
                void previewWidgetTile(tile.widget_id, filters);
              } else {
                const ds = app.datasets[tile.datasetIndex ?? 0];
                if (ds) runPreview(ds.id, filters);
              }
            }}
          />
          <div style={{ textAlign: "right", marginTop: 8 }}>
            <Button size="small" onClick={() => persistTiles(tiles)}>
              保存布局
            </Button>
          </div>
        </Card>
      ) : isScreen ? (
        <Card
          title="大屏画布"
          extra={
            <Space>
              <Dropdown
                menu={{
                  items: [
                    { key: "bar", label: "柱状图" },
                    { key: "kpi", label: "指标卡" },
                    { key: "table", label: "表格" },
                  ],
                  onClick: ({ key }) => addWidget(key),
                }}
                disabled={app.datasets.length === 0}
              >
                <Button icon={<PlusOutlined />} disabled={app.datasets.length === 0}>
                  添加组件
                </Button>
              </Dropdown>
              <Button icon={<FilterOutlined />} onClick={addParam}>
                添加筛选参数
              </Button>
              <Button onClick={() => previewAll()}>预览全部</Button>
            </Space>
          }
        >
          {app.datasets.length === 0 && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="请先创建数据集，再添加大屏组件。"
            />
          )}

          {params.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                {params.map((p) => (
                  <Space key={p.id} wrap>
                    <Text type="secondary">参数</Text>
                    <Input
                      size="small"
                      style={{ width: 120 }}
                      value={p.label}
                      placeholder="标题"
                      onChange={(e) => updateParam(p.id, { label: e.target.value })}
                    />
                    <Input
                      size="small"
                      style={{ width: 160 }}
                      value={p.column}
                      placeholder="列名（如 channel）"
                      onChange={(e) => updateParam(p.id, { column: e.target.value })}
                    />
                    <Button
                      size="small"
                      danger
                      type="text"
                      icon={<DeleteOutlined />}
                      onClick={() => removeParam(p.id)}
                    />
                  </Space>
                ))}
              </Space>
            </div>
          )}

          <ParamBar
            params={params}
            values={paramValues}
            drills={drills}
            onChange={setParamValues}
            onClearDrill={(i) => {
              const next = drills.filter((_, xi) => xi !== i);
              setDrills(next);
              previewAll(buildRuntimeFilters(params, paramValues, next));
            }}
            onApply={() => previewAll()}
          />
          <div style={{ display: "flex", gap: 16 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <ScreenCanvas
                canvas={
                  (app.spec?.canvas as { width: number; height: number; bg?: string }) ?? {
                    width: 1920,
                    height: 1080,
                    bg: "#0b1a2e",
                  }
                }
                widgets={widgets}
                previews={previewByIndex}
                selectedId={selectedWidget}
                editable
                onSelect={setSelectedWidget}
                onChange={handleWidgetsChange}
                onCommit={commitWidgets}
                onDrill={handleDrill}
              />
            </div>
            <Card size="small" title="组件属性" style={{ width: 260, flexShrink: 0 }}>
              {!selected ? (
                <Text type="secondary">在画布中选中一个组件</Text>
              ) : (
                <Space direction="vertical" style={{ width: "100%" }}>
                  <div>
                    <Text type="secondary">标题</Text>
                    <Input
                      value={selected.title}
                      onChange={(e) =>
                        handleWidgetsChange(
                          widgets.map((w) =>
                            w.id === selected.id ? { ...w, title: e.target.value } : w,
                          ),
                        )
                      }
                      onBlur={commitWidgets}
                    />
                  </div>
                  <div>
                    <Text type="secondary">图表类型</Text>
                    <Segmented
                      block
                      value={selected.type}
                      options={[
                        { label: "柱状", value: "bar" },
                        { label: "指标", value: "kpi" },
                        { label: "表格", value: "table" },
                      ]}
                      onChange={(v) => {
                        handleWidgetsChange(
                          widgets.map((w) =>
                            w.id === selected.id ? { ...w, type: String(v) } : w,
                          ),
                        );
                        void commitWidgets();
                      }}
                    />
                  </div>
                  <div>
                    <Text type="secondary">绑定数据集</Text>
                    <Select
                      style={{ width: "100%" }}
                      value={selected.datasetIndex ?? 0}
                      options={app.datasets.map((d, i) => ({ label: d.name, value: i }))}
                      onChange={(v) => {
                        handleWidgetsChange(
                          widgets.map((w) =>
                            w.id === selected.id ? { ...w, datasetIndex: v } : w,
                          ),
                        );
                        void commitWidgets();
                      }}
                    />
                  </div>
                  <Button
                    danger
                    block
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      handleWidgetsChange(widgets.filter((w) => w.id !== selected.id));
                      setSelectedWidget(null);
                      void commitWidgets();
                    }}
                  >
                    删除组件
                  </Button>
                </Space>
              )}
            </Card>
          </div>
        </Card>
      ) : app.datasets.length > 0 ? (
        <Tabs
          items={app.datasets.map((ds) => ({
            key: ds.id,
            label: ds.name,
            children: (
              <Card
                extra={
                  <Space>
                    <Button
                      icon={<EyeOutlined />}
                      loading={previewing === ds.id}
                      onClick={() => runPreview(ds.id)}
                    >
                      预览
                    </Button>
                    <Button
                      icon={<DownloadOutlined />}
                      disabled={!previews[ds.id]}
                      onClick={() =>
                        previews[ds.id] &&
                        exportCsv(ds.name, previews[ds.id].columns, previews[ds.id].rows)
                      }
                    >
                      导出 CSV
                    </Button>
                  </Space>
                }
              >
                <Paragraph>
                  <Text type="secondary">
                    编译 SQL{previews[ds.id]?.used_mock === false ? "（真实数据源已执行）" : "（Mock 示例数据）"}：
                  </Text>
                </Paragraph>
                <pre
                  style={{
                    background: "#0f172a",
                    color: "#e2e8f0",
                    padding: 12,
                    borderRadius: 8,
                    overflowX: "auto",
                    fontSize: 12,
                  }}
                >
                  {ds.compiled_sql || "（未能编译，请检查数据集绑定是否落地到本体）"}
                </pre>
                <div style={{ marginTop: 12 }}>{renderPreview(ds.id)}</div>
              </Card>
            ),
          }))}
        />
      ) : null}

      <DatasetEditor
        open={showDatasetEditor}
        domainId={app.domain_id}
        dataSources={dataSources ?? []}
        dataset={editingDataset}
        onClose={() => {
          setShowDatasetEditor(false);
          setEditingDataset(null);
        }}
        onSave={handleSaveDataset}
      />

      <DataSourcesModal open={showDataSources} onClose={() => setShowDataSources(false)} />

      <WidgetLibraryModal
        open={showWidgetLib}
        domainId={app.domain_id}
        onClose={() => setShowWidgetLib(false)}
        onPick={handleAddWidget}
      />

      <Modal
        title="发布数据应用"
        open={showPublish}
        confirmLoading={publishing}
        onOk={handlePublish}
        onCancel={() => setShowPublish(false)}
        okText="确认发布"
      >
        <Paragraph type="secondary">
          发布将冻结当前配置与数据集绑定为一个只读版本快照，可在版本记录中回看，并可通过外部 API（scope: dataapps:read）访问。
        </Paragraph>
        <Input.TextArea
          rows={3}
          placeholder="版本备注（可选）"
          value={publishComment}
          onChange={(e) => setPublishComment(e.target.value)}
        />
      </Modal>
    </PageContainer>
  );
}
