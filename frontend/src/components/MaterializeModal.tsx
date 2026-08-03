import { DatabaseOutlined, ExportOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Collapse,
  Empty,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Spin,
  Steps,
  Table,
  Tabs,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { CronPicker } from "./CronPicker";
import { DataSourceStatusTag } from "./DataSourcesModal";
import type {
  DataSource,
  MaterializationContract,
  MaterializationContractUpdateInput,
  MaterializationLoadStrategy,
  MaterializationRun,
  MaterializeStatus,
  LineageEmitResult,
  SyncTool,
  SyncToolInfo,
} from "../types";
import { LABELS, StatusBadge } from "./StatusBadge";

// 目标数仓引擎（决定 DDL/ETL 方言）——与后端 app/warehouse/adapters 对齐。
const ENGINE_OPTIONS = ["hive", "doris", "starrocks", "clickhouse", "iceberg"].map(
  (e) => ({ value: e, label: e }),
);
// 引擎名集合：选中数据源时其 kind 命中则据此推导引擎。
const ENGINE_VALUES = new Set(ENGINE_OPTIONS.map((e) => e.value));
// 推导不出引擎时的回退（与后端 DEFAULT_ENGINE 对齐）。
const DEFAULT_ENGINE = "hive";

// 数仓分层（阶段）顺序与展示名。
const LAYER_ORDER = ["dim", "dwd", "ads"];
const LAYER_LABEL: Record<string, string> = {
  dim: "维度层 DIM",
  dwd: "明细层 DWD",
  ads: "应用层 ADS",
};

// 同步方式：复用后端 LoadStrategy，逐实体选，随 overrides 写回各自的物化契约。
const LOAD_STRATEGY_OPTIONS: { value: MaterializationLoadStrategy; label: string }[] =
  [
    { value: "full", label: "全量覆盖" },
    { value: "incremental", label: "增量追加" },
    { value: "cdc", label: "CDC 变更捕获" },
  ];

// 各同步方式的实际语义（列窄放不下，挂在下拉的 Tooltip 上，尤其 CDC 的限制不能丢）。
const STRATEGY_HINT: Record<string, string> = {
  full: "INSERT OVERWRITE：重写整表/分区",
  incremental: "INSERT INTO：按分区键追加，水位由调度器注入；未配分区键会退化为无谓词追加",
  cdc: "物化内不承载变更捕获，本次按全量覆盖执行；要 CDC 请改用同步作业",
};

// 定时策略：写回契约 refresh_cron，交由调度作业执行（本次物化仍是立即执行一次）。
// 编辑与回读见 {@link CronPicker}。

// DagRun / 任务状态 → 中文标签与色。弹窗通篇中文，直接吐 Airflow 的 `upstream_failed`
// 对使用者没有意义；但**未知状态一律原样显示**，不猜、不归到「其他」——状态是判断
// 要不要重跑的依据，假装认识比看不懂更糟。
const RUN_STATE: Record<string, { label: string; color: string }> = {
  success: { label: "成功", color: "success" },
  running: { label: "运行中", color: "processing" },
  queued: { label: "排队中", color: "default" },
  scheduled: { label: "已调度", color: "default" },
  up_for_retry: { label: "待重试", color: "warning" },
  up_for_reschedule: { label: "待重排", color: "warning" },
  failed: { label: "失败", color: "error" },
  upstream_failed: { label: "上游失败", color: "error" },
  skipped: { label: "已跳过", color: "warning" },
  removed: { label: "已移除", color: "default" },
};
// 判定「这次运行是不是砸了」用的状态集合。
const FAILED_STATES = new Set(["failed", "upstream_failed"]);

function RunStateTag({ state }: { state?: string | null }) {
  if (!state) return <Tag>未知</Tag>;
  const hit = RUN_STATE[state];
  // 未收录的状态原样显示，并把原始值挂在 title 上便于对照 Airflow。
  return (
    <Tag color={hit?.color ?? "default"} title={state} style={{ marginInlineEnd: 0 }}>
      {hit?.label ?? state}
    </Tag>
  );
}

/** 任务在 Airflow UI 里的地址：从 DagRun 的 grid 链接派生，多带一个 task_id。 */
function taskUrl(runUrl: string | undefined, taskId: string): string | null {
  if (!runUrl) return null;
  return `${runUrl}&task_id=${encodeURIComponent(taskId)}`;
}

interface Props {
  ontologyId: string;
  open: boolean;
  onClose: () => void;
  /** 工作本体的发布状态（draft/published/…）；非 published 时弹窗给出落库警示。 */
  ontologyStatus?: string;
  /** 限定只物化某个实体（对象/关系）。传其 id（= 契约 target_id）即锁定为单实体物化。 */
  scopeTargetId?: string;
  /** 单实体物化时的展示名（对象/关系的业务名）。 */
  scopeLabel?: string;
}

interface FormValues {
  target_datasource_id: string;
  target_database: string;
  sync_tool: SyncTool;
}

/** 由数据源类型推导物化引擎（DDL/ETL 方言）。仅仓库类型可作物化目标，否则返回 null。 */
function engineOfKind(kind?: string | null): string | null {
  const k = kind?.toLowerCase();
  return k && ENGINE_VALUES.has(k) ? k : null;
}

/** 实体的技术名（= 后端不加覆盖时生成的表名）。 */
function entityNameOf(c: MaterializationContract): string {
  return c.target_name ?? c.target_id;
}

/** 推荐表名。
 *
 * 各层共处一个目标库，同名风险来自跨层，故推荐「层_实体名」。若目标库里已有同名
 * 或同实体名的表，优先推荐那张——物化是覆盖写，另建一张近义表只会制造两份事实。
 */
function recommendTableName(
  c: MaterializationContract,
  existing: string[],
): string {
  const base = entityNameOf(c);
  const layer = c.target_layer;
  const suggested = base.startsWith(`${layer}_`) ? base : `${layer}_${base}`;
  const lower = existing.map((t) => t.toLowerCase());
  for (const candidate of [suggested, base]) {
    const i = lower.indexOf(candidate.toLowerCase());
    if (i >= 0) return existing[i];
  }
  return suggested;
}

/** 本体一键物化弹窗：第 1 步选连接（数据源 + 目标库），第 2 步逐实体配存储策略 → 落库执行。
 *
 * 物化会对目标库执行 CREATE TABLE 与 INSERT OVERWRITE（覆盖写），属破坏性操作，
 * 故弹窗里明确提示，执行后就地呈现回执（分 DDL/ETL 两阶段的成功/失败）。
 * 「历史」页读已存的物化制品回执，供查看历次任务与执行状态。
 */
export function MaterializeModal({
  ontologyId,
  open,
  onClose,
  ontologyStatus,
  scopeTargetId,
  scopeLabel,
}: Props) {
  const [form] = Form.useForm<FormValues>();
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [targetKeys, setTargetKeys] = useState<string[]>([]); // 勾选要物化的实体名
  const [allTargets, setAllTargets] = useState<
    { key: string; contract: MaterializationContract }[]
  >([]);
  // 逐实体（契约 id）的存储策略编辑值：分区键 / 同步方式 / 定时策略，默认取契约现值。
  const [rowPk, setRowPk] = useState<Record<string, string>>({});
  const [rowStrategy, setRowStrategy] = useState<
    Record<string, MaterializationLoadStrategy>
  >({});
  const [rowCron, setRowCron] = useState<Record<string, string>>({});
  // 人工指定的表名（契约 id → 表名）。只存「改过的」：未改的跟随推荐值实时变化。
  const [rowTableEdit, setRowTableEdit] = useState<Record<string, string>>({});
  // 目标源上已有的库；取不到（mock/无连接/缺驱动）时记原因，供第 1 步显式报错。
  const [databases, setDatabases] = useState<string[]>([]);
  const [dbError, setDbError] = useState<string | null>(null);
  const [dbLoading, setDbLoading] = useState(false);
  // 目标库已有的表，键为 `${数据源id}::${库名}`，用于推荐表名与「已存在」提示。
  const [tablesByDb, setTablesByDb] = useState<Record<string, string[]>>({});
  // 多步向导：0=目标与策略，1=按层配置；activeLayer=当前查看的分层。
  const [step, setStep] = useState(0);
  const [activeLayer, setActiveLayer] = useState<string | null>(null);
  const [result, setResult] = useState<MaterializationRun | null>(null);
  const [runs, setRuns] = useState<MaterializationRun[]>([]);
  const [syncTools, setSyncTools] = useState<SyncToolInfo[]>([]);
  const [activeTab, setActiveTab] = useState<"run" | "history">("run");
  // 单实体物化：命中契约不可物化 / 尚未生成时的原因，禁用执行并提示。
  const [scopeError, setScopeError] = useState<string | null>(null);

  const scoped = Boolean(scopeTargetId);
  const isDraft = Boolean(ontologyStatus && ontologyStatus !== "published");
  const dsId = Form.useWatch("target_datasource_id", form);
  const targetDb = Form.useWatch("target_database", form);
  const syncToolName = Form.useWatch("sync_tool", form);
  const selectedToolInfo = useMemo(
    () => syncTools.find((t) => t.name === syncToolName) ?? null,
    [syncTools, syncToolName],
  );
  /** 所选工具支持的装载方式。工具信息还没到时不设限，避免把选项误锁死。 */
  const supportedModes = useMemo(
    () => (selectedToolInfo ? new Set(selectedToolInfo.modes) : null),
    [selectedToolInfo],
  );

  // 目标库里已有的表：推荐表名与「已存在」提示都据此。
  const existingTables = useMemo(
    () => tablesByDb[`${dsId}::${targetDb}`] ?? [],
    [tablesByDb, dsId, targetDb],
  );
  /** 某实体的目标表名：人工改过用改的，否则用推荐值。 */
  const tableOf = useCallback(
    (c: MaterializationContract) =>
      (rowTableEdit[c.id] ?? "").trim() || recommendTableName(c, existingTables),
    [rowTableEdit, existingTables],
  );
  const tableExists = useCallback(
    (table: string) =>
      existingTables.some((t) => t.toLowerCase() === table.toLowerCase()),
    [existingTables],
  );

  /** 逐实体的存储策略以各自契约的现值起步——弹窗是在编辑既有契约，不是从零填。 */
  const seedRowState = (cs: MaterializationContract[]) => {
    setRowPk(Object.fromEntries(cs.map((c) => [c.id, c.partition_key ?? ""])));
    setRowStrategy(
      Object.fromEntries(
        cs.map((c) => [c.id, c.load_strategy as MaterializationLoadStrategy]),
      ),
    );
    setRowCron(Object.fromEntries(cs.map((c) => [c.id, c.refresh_cron ?? ""])));
  };

  useEffect(() => {
    if (!open) return;
    setResult(null);
    setActiveTab("run");
    setScopeError(null);
    setStep(0);
    setActiveLayer(null);
    setRowTableEdit({});
    setDatabases([]);
    setDbError(null);
    setTablesByDb({});
    setLoading(true);
    void (async () => {
      try {
        const [ds, runList, toolInfo] = await Promise.all([
          api.listDataSources(),
          api.listMaterializationRuns(ontologyId),
          api.getSyncTools().catch(() => null),
        ]);
        setDataSources(ds);
        if (toolInfo) {
          setSyncTools(toolInfo.tools);
          // 搬运工具默认取后端 default（通常 seatunnel），可在弹窗改。默认工具在本
          // 部署里没有可用镜像时退到第一个可用的——默认值本来就该是能跑的那个。
          if (!form.getFieldValue("sync_tool")) {
            const fallback = toolInfo.tools.find((t) => t.available)?.name;
            const preferred = toolInfo.tools.find((t) => t.name === toolInfo.default);
            form.setFieldValue(
              "sync_tool",
              preferred?.available ? toolInfo.default : (fallback ?? toolInfo.default),
            );
          }
        }
        setRuns(runList);
        if (scopeTargetId) {
          // 单实体：取全部契约按 target_id 命中，校验其是否配置为物化。
          let all = await api.listMaterializationContracts(ontologyId);
          let hit = all.find((c) => c.target_id === scopeTargetId);
          if (!hit) {
            try {
              await api.syncMaterializationContracts(ontologyId);
              all = await api.listMaterializationContracts(ontologyId);
              hit = all.find((c) => c.target_id === scopeTargetId);
            } catch {
              // 无权同步则保持未命中。
            }
          }
          if (!hit) {
            setScopeError("尚未生成该实体的物化契约，无法物化。");
            setAllTargets([]);
            setTargetKeys([]);
          } else if (!hit.materialized) {
            setScopeError("该实体的物化契约标记为「不物化」，无法物化。");
            setAllTargets([]);
            setTargetKeys([]);
          } else {
            const key = hit.target_name ?? hit.target_id;
            setAllTargets([{ key, contract: hit }]);
            setTargetKeys([key]);
            seedRowState([hit]);
          }
          return;
        }
        // 整体物化：列出全部可物化实体；草稿若未同步过契约则补一次推导。
        let cs = await api.listMaterializationContracts(ontologyId, {
          materialized_only: true,
        });
        if (cs.length === 0) {
          try {
            await api.syncMaterializationContracts(ontologyId);
            cs = await api.listMaterializationContracts(ontologyId, {
              materialized_only: true,
            });
          } catch {
            // 无权同步或推导失败时静默降级：待物化实体保持为空。
          }
        }
        const targets = cs
          .map((c) => ({ key: c.target_name ?? c.target_id, contract: c }))
          .filter((t) => t.key);
        setAllTargets(targets);
        setTargetKeys(targets.map((t) => t.key)); // 默认全选
        seedRowState(cs);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, [open, ontologyId, scopeTargetId]);

  // 选定数据源 → 读它上面的库列表。目标库只能从中选，故读不到就得显式报错。
  useEffect(() => {
    if (!open || !dsId) return;
    let stale = false;
    setDatabases([]);
    setDbError(null);
    setDbLoading(true);
    form.setFieldValue("target_database", undefined); // 换了源，上一个源上的库不再有意义
    api
      .listDataSourceDatabases(dsId)
      .then((r) => {
        if (!stale) setDatabases(r.databases);
      })
      .catch((err: unknown) => {
        if (!stale)
          setDbError(err instanceof Error ? err.message : "无法读取该数据源的库列表");
      })
      .finally(() => {
        if (!stale) setDbLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [open, dsId, form]);

  // 目标库 → 读该库已有的表（推荐表名 + 「已存在」提示）。按「数据源::库」缓存，只读一次。
  useEffect(() => {
    if (!open || !dsId || !targetDb) return;
    const key = `${dsId}::${targetDb}`;
    if (key in tablesByDb) return;
    // 先占位，避免同一渲染周期重复请求。
    setTablesByDb((m) => ({ ...m, [key]: [] }));
    api
      .listDataSourceTables(dsId, targetDb)
      .then((r) => setTablesByDb((m) => ({ ...m, [key]: r.tables })))
      .catch(() => undefined); // 读不到表：当作空列表，推荐值退回约定名
    // 不设 stale 守卫：结果按「数据源::库」入缓存，迟到的响应只会填自己那一格；
    // 而占位写入本身会触发本 effect 重跑，守卫反而会把在途请求作废。
  }, [open, dsId, targetDb, tablesByDb]);

  const submit = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    if (scopeError) {
      message.warning(scopeError);
      return;
    }
    if (targetKeys.length === 0) {
      message.warning(scoped ? "该实体不可物化" : "请至少勾选一个要物化的实体");
      return;
    }
    // 引擎由所选数据源类型推导；推导不出则回退默认引擎（不拦截物化）。
    const ds = dataSources.find((d) => d.id === values.target_datasource_id);
    const engine = engineOfKind(ds?.kind) ?? DEFAULT_ENGINE;
    // 逐实体的存储策略（分区键 / 同步方式 / 定时策略）按差异写入 overrides，
    // 写回并钉住各自的物化契约；目标库/表名只作用于本次落库，另走 *_overrides。
    const overrides: Record<string, MaterializationContractUpdateInput> = {};
    const databaseOverrides: Record<string, string> = {};
    const tableOverrides: Record<string, string> = {};
    const selected = allTargets.filter((t) => targetKeys.includes(t.key));
    for (const t of selected) {
      const c = t.contract;
      const patch: MaterializationContractUpdateInput = {};
      const pk = (rowPk[c.id] ?? "").trim() || null;
      if (pk !== (c.partition_key ?? null)) patch.partition_key = pk;
      const strategy = rowStrategy[c.id];
      if (strategy && strategy !== c.load_strategy) patch.load_strategy = strategy;
      const cron = (rowCron[c.id] ?? "").trim() || null;
      if (cron !== (c.refresh_cron ?? null)) patch.refresh_cron = cron;
      if (Object.keys(patch).length) overrides[c.id] = patch;

      // 各层都落到同一个目标库；database_overrides 按层给，故逐层填同一个值。
      databaseOverrides[c.target_layer] = values.target_database;
      const table = tableOf(c);
      if (table !== entityNameOf(c)) tableOverrides[c.id] = table;
    }
    // 同库同名会互相覆盖：两个实体落到同一张表，先建的会被后写的冲掉。
    const byTable = new Map<string, string>();
    for (const t of selected) {
      const table = tableOf(t.contract).toLowerCase();
      const prev = byTable.get(table);
      if (prev) {
        message.error(`表名冲突：${prev} 与 ${t.key} 都要落到 ${values.target_database}.${table}`);
        return;
      }
      byTable.set(table, t.key);
    }
    setRunning(true);
    setResult(null);
    try {
      const run = await api.materializeOntology(ontologyId, {
        target_datasource_id: values.target_datasource_id,
        engine,
        sync_tool: values.sync_tool,
        // 同步方式逐实体来自各自契约（上面已写回），故不传全局覆盖。
        ...(Object.keys(databaseOverrides).length
          ? { database_overrides: databaseOverrides }
          : {}),
        ...(Object.keys(tableOverrides).length
          ? { table_overrides: tableOverrides }
          : {}),
        // 单实体锁定其名；整体物化时全选传 null（不裁剪），否则传勾选的实体名。
        selected_targets: scoped
          ? targetKeys
          : targetKeys.length === allTargets.length
            ? null
            : targetKeys,
        ...(Object.keys(overrides).length ? { overrides } : {}),
      });
      setResult(run);
      if (!run.ok) {
        message.warning("作业已生成，但触发失败，请查看回执");
      } else {
        message.success("作业已提交，正在按调度执行");
      }
      // 刷新历史，让本次执行进入记录列表。
      api
        .listMaterializationRuns(ontologyId)
        .then(setRuns)
        .catch(() => undefined);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "物化失败");
    } finally {
      setRunning(false);
    }
  };

  // 待物化实体按分层（阶段）分组，供多步向导逐层配置。
  const groups = LAYER_ORDER.map((L) => ({
    layer: L,
    rows: allTargets.filter((t) => t.contract.target_layer === L),
  })).filter((g) => g.rows.length);
  const curLayer = activeLayer ?? groups[0]?.layer ?? null;
  const curRows = groups.find((g) => g.layer === curLayer)?.rows ?? [];

  const goNext = async () => {
    try {
      await form.validateFields(["target_datasource_id", "target_database"]);
    } catch {
      return;
    }
    if (selectedToolInfo && !selectedToolInfo.available) {
      message.warning(`「${selectedToolInfo.name}」没有可用的执行镜像，请换一个搬运工具`);
      return;
    }
    setStep(1);
  };

  type Row = { key: string; contract: MaterializationContract };

  /** 表名输入：默认填推荐值，改动即以人工值为准；标注该表在目标库里是否已存在。 */
  const tableInput = (c: MaterializationContract) => {
    const table = tableOf(c);
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Input
          size="small"
          style={{ width: 150 }}
          value={table}
          onChange={(e) => setRowTableEdit((m) => ({ ...m, [c.id]: e.target.value }))}
        />
        {existingTables.length > 0 &&
          (tableExists(table) ? (
            <Tooltip title={`${targetDb}.${table} 已存在，本次物化将覆盖写`}>
              <Tag color="orange">已存在</Tag>
            </Tooltip>
          ) : (
            <Tag color="green">将新建</Tag>
          ))}
      </div>
    );
  };

  const strategySelect = (c: MaterializationContract) => (
    <Tooltip title={STRATEGY_HINT[rowStrategy[c.id]]}>
      <Select<MaterializationLoadStrategy>
        size="small"
        style={{ width: 120 }}
        value={rowStrategy[c.id]}
        // 所选工具不支持的装载方式置灰：选了也只会被 planner 记进「未生成作业」，
        // 让人以为配好了却没搬（如 DataX 没有 CDC）。
        options={LOAD_STRATEGY_OPTIONS.map((o) => ({
          ...o,
          disabled: Boolean(supportedModes && !supportedModes.has(o.value)),
          label:
            supportedModes && !supportedModes.has(o.value)
              ? `${o.label}（${syncToolName} 不支持）`
              : o.label,
        }))}
        onChange={(v) => setRowStrategy((m) => ({ ...m, [c.id]: v }))}
      />
    </Tooltip>
  );

  const pkInput = (c: MaterializationContract) => (
    <Input
      size="small"
      placeholder={c.partition_key ?? "如 dt"}
      value={rowPk[c.id] ?? ""}
      onChange={(e) => {
        const v = e.target.value;
        setRowPk((m) => ({ ...m, [c.id]: v }));
      }}
    />
  );

  const cronSelect = (c: MaterializationContract) => (
    <CronPicker
      value={rowCron[c.id] ?? ""}
      onChange={(v) => setRowCron((m) => ({ ...m, [c.id]: v }))}
    />
  );
  const entityTable = (rows: Row[]) => (
    <Table
      size="small"
      rowKey={(r) => r.key}
      dataSource={rows}
      pagination={false}
      scroll={{ y: 220 }}
      rowSelection={{
        selectedRowKeys: targetKeys.filter((k) => rows.some((r) => r.key === k)),
        onChange: (keys) => {
          const layerKeys = new Set(rows.map((r) => r.key));
          setTargetKeys((prev) => [
            ...prev.filter((k) => !layerKeys.has(k)),
            ...(keys as string[]),
          ]);
        },
      }}
      columns={[
        {
          title: "实体",
          key: "entity",
          render: (_, r) =>
            r.contract.target_display_name ??
            r.contract.target_name ??
            r.contract.target_id,
        },
        {
          title: "表名",
          key: "table",
          width: 230,
          render: (_, r) => tableInput(r.contract),
        },
        {
          title: "同步方式",
          key: "strategy",
          width: 120,
          render: (_, r) => strategySelect(r.contract),
        },
        {
          title: "分区键",
          key: "partition",
          width: 120,
          render: (_, r) => pkInput(r.contract),
        },
        {
          title: "定时策略",
          key: "cron",
          width: 150,
          render: (_, r) => cronSelect(r.contract),
        },
      ]}
    />
  );

  const runTab = (
    <>
      {/* 破坏性操作的告知放 Alert 而不是一行小字：它是这个弹窗里后果最重的一句话，
          此前排在最不显眼的位置。草稿警示并入同一条，避免两处措辞抢注意力。 */}
      <Alert
        type={isDraft ? "warning" : "info"}
        showIcon
        style={{ marginBottom: 16 }}
        message="物化会对目标库建表并覆盖写入（INSERT OVERWRITE），重写整表或整分区。"
        description={
          isDraft ? (
            <span>
              当前本体为
              <b>{LABELS[ontologyStatus!] ?? ontologyStatus}</b>
              状态，本次将把<b>未发布内容</b>落到目标库。
            </span>
          ) : undefined
        }
      />
      <Steps
        size="small"
        current={step}
        items={[
          { title: "连接信息", description: "目标存储与搬运工具" },
          { title: "存储策略", description: "逐实体的表名与同步方式" },
        ]}
        style={{ marginBottom: 20 }}
      />
      <Form form={form} layout="vertical">
        {/* 第 1 步字段常驻挂载（display 切换），保证表名预览的 watch 稳定 */}
        <div style={{ display: step === 0 ? "block" : "none" }}>
          <Form.Item
            name="target_datasource_id"
            label="目标数据源"
            rules={[{ required: true, message: "请选择目标数据源" }]}
            extra={
              <>
                引擎由数据源类型自动决定。新增 / 编辑 / 测试请到{" "}
                <Link to="/settings">系统设置 → 数据源</Link>。
              </>
            }
          >
            <Select
              placeholder="选择目标数据源"
              optionFilterProp="title"
              options={dataSources.map((d) => ({
                value: d.id,
                title: `${d.name} ${d.kind}`,
                label: (
                  <span
                    style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    {d.name}
                    <Tag style={{ marginInlineEnd: 0 }}>{d.kind}</Tag>
                    <DataSourceStatusTag status={d.status} />
                  </span>
                ),
              }))}
              notFoundContent={
                <Empty description="尚无数据源，请到 系统设置 → 数据源 添加" />
              }
            />
          </Form.Item>
          {/* 读不到库列表不该拦住物化：落库由 Airflow 侧执行，后端只是「顺带」
              列个库供选，故降级为手填。 */}
          <Form.Item
            name="target_database"
            label="目标库"
            normalize={(v) => (typeof v === "string" ? v.trim() : v)}
            rules={[{ required: true, message: dbError ? "请填写目标库" : "请选择目标库" }]}
            extra={
              dbError
                ? "列不出库，请手动填写；各分层的表都建在这个库里，物化不会自动建库"
                : "读取自所选数据源；各分层的表都建在这个库里，物化不会自动建库"
            }
          >
            {dbError ? (
              <Input placeholder="手动填写目标库名，如 dw" allowClear />
            ) : (
              <Select
                placeholder={dsId ? "选择目标库" : "请先选择目标数据源"}
                showSearch
                disabled={!dsId}
                loading={dbLoading}
                options={databases.map((d) => ({ value: d, label: d }))}
                notFoundContent={
                  dbLoading ? null : <Empty description="该数据源上没有可用的库" />
                }
              />
            )}
          </Form.Item>
          {dbError && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: -12, marginBottom: 16 }}
              message="无法读取该数据源的库列表，已切为手动填写"
              description={dbError}
            />
          )}
          <Form.Item
            name="sync_tool"
            label="搬运工具"
            extra={
              // 收起时只显示工具名（下拉里才展开镜像/能力），但「这次到底会用哪个镜像」
              // 是排查时第一个要问的，所以在这行常驻。
              selectedToolInfo ? (
                <>
                  执行镜像 <code>{selectedToolInfo.image}</code>；镜像不可用的工具不能选。
                </>
              ) : (
                "生成对应工具的 Airflow 作业交给调度器执行；镜像不可用的工具不能选。"
              )
            }
          >
            <Select<SyncTool>
              placeholder="选择搬运工具"
              optionLabelProp="title"
              options={syncTools.map((t) => ({
                value: t.name,
                title: t.name,
                // 镜像拿不到的工具置灰而非隐藏：「有这个工具但没配」与「没有这个工具」
                // 是两回事，后者会让人以为得改代码。
                disabled: !t.available,
                label: (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      minWidth: 0,
                    }}
                  >
                    <span style={{ fontWeight: 500 }}>{t.name}</span>
                    {t.cdc && (
                      <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                        CDC
                      </Tag>
                    )}
                    {!t.available && (
                      <Tooltip title={t.reason}>
                        <Tag color="default" style={{ marginInlineEnd: 0 }}>
                          镜像不可用
                        </Tag>
                      </Tooltip>
                    )}
                    <code
                      className="om-muted"
                      style={{
                        fontSize: 12,
                        marginLeft: "auto",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {t.image}
                    </code>
                  </div>
                ),
              }))}
            />
          </Form.Item>
          {/* 选中的工具没有可用镜像时，后端会在提交前拒绝。与其让人点了「提交」
              才看到报错，不如在这里就说清怎么修。 */}
          {selectedToolInfo && !selectedToolInfo.available && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: -12, marginBottom: 16 }}
              message={`「${selectedToolInfo.name}」在本部署里没有可用的执行镜像`}
              description={selectedToolInfo.reason}
            />
          )}
        </div>

        {step === 1 &&
          (scoped ? (
            scopeError ? (
              <Alert type="warning" showIcon message={scopeError} />
            ) : allTargets[0] ? (
              <div>
                <div className="om-muted" style={{ marginBottom: 10, fontSize: 12 }}>
                  <Tag color="blue">
                    {scopeLabel ?? allTargets[0].contract.target_display_name ?? "—"}
                  </Tag>
                  落库到 <code>{targetDb}</code>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 12, width: 56 }}>表名</span>
                  {tableInput(allTargets[0].contract)}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                  <span style={{ fontSize: 12, width: 56 }}>同步方式</span>
                  {strategySelect(allTargets[0].contract)}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                  <span style={{ fontSize: 12, width: 56 }}>分区键</span>
                  <span style={{ display: "inline-block", width: 150 }}>
                    {pkInput(allTargets[0].contract)}
                  </span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                  <span style={{ fontSize: 12, width: 56 }}>定时策略</span>
                  {cronSelect(allTargets[0].contract)}
                </div>
              </div>
            ) : (
              <Tag>—</Tag>
            )
          ) : (
            <>
              <div className="om-muted" style={{ marginBottom: 8, fontSize: 12 }}>
                已选 {targetKeys.length}/{allTargets.length} 个实体，全部落到{" "}
                <code>{targetDb}</code>。表名给的是推荐值（库里已有同名表时直接推荐那张，
                物化为覆盖写），可逐行改。
              </div>
              <Segmented
                value={curLayer ?? undefined}
                onChange={(v) => setActiveLayer(v as string)}
                options={groups.map((g) => ({
                  label: `${LAYER_LABEL[g.layer] ?? g.layer}（${
                    g.rows.filter((r) => targetKeys.includes(r.key)).length
                  }/${g.rows.length}）`,
                  value: g.layer,
                }))}
                style={{ marginBottom: 12 }}
              />
              {entityTable(curRows)}
            </>
          ))}
      </Form>

      {result && <MaterializeReceiptView run={result} />}
    </>
  );

  return (
    <Modal
      open={open}
      title={
        <span>
          <DatabaseOutlined />{" "}
          {scoped ? `物化实体：${scopeLabel ?? ""}` : "物化本体到目标存储"}
        </span>
      }
      onOk={submit}
      onCancel={onClose}
      footer={
        activeTab === "history"
          ? null
          : [
              <Button key="close" onClick={onClose}>
                关闭
              </Button>,
              ...(step === 1 && !result
                ? [
                    <Button key="prev" onClick={() => setStep(0)}>
                      上一步
                    </Button>,
                  ]
                : []),
              step === 0 ? (
                <Button key="next" type="primary" onClick={goNext}>
                  下一步
                </Button>
              ) : (
                <Button
                  key="run"
                  type="primary"
                  danger
                  loading={running}
                  disabled={targetKeys.length === 0 || (scoped && Boolean(scopeError))}
                  onClick={submit}
                >
                  {result ? "再次提交" : "提交并运行"}
                </Button>
              ),
            ]
      }
      width={860}
      destroyOnClose
    >
      <Spin spinning={loading}>
        <Tabs
          activeKey={activeTab}
          onChange={(k) => setActiveTab(k as "run" | "history")}
          items={[
            { key: "run", label: "执行", children: runTab },
            {
              key: "history",
              label: `历史${runs.length ? `（${runs.length}）` : ""}`,
              children: <MaterializeRunsTable runs={runs} />,
            },
          ]}
        />
      </Spin>
    </Modal>
  );
}

const RUNS_PAGE_SIZE = 8;

/** 历次物化任务列表：时间 / 操作人 / 目标 / 状态 / 分阶段成败，行内展开完整回执。 */
function MaterializeRunsTable({ runs }: { runs: MaterializationRun[] }) {
  const [page, setPage] = useState(1);
  // artifact_id → DagRun 的真实状态。
  const [runStates, setRunStates] = useState<Record<string, string | null>>({});
  // 已到终态的不再回读——终态不会变，继续问只是空耗。
  const settled = useRef<Set<string>>(new Set());

  const visible = useMemo(
    () => runs.slice((page - 1) * RUNS_PAGE_SIZE, page * RUNS_PAGE_SIZE),
    [runs, page],
  );

  // 列表里的「状态」必须是**运行**状态，不能是「提交成功了没有」。制品状态永远是
  // succeeded（提交成功即落库一条记录），照着渲染的结果是：DagRun 明明 failed，
  // 列表却整列绿色「已完成」——这次排查同步失败时就是被它误导的。
  // 权威在 Airflow，故逐行回读；只读当前页，未终结的每 5 秒再读一次。
  useEffect(() => {
    const pending = visible.filter(
      (r) => r.artifact_id && r.receipt?.dag_run_id && !settled.current.has(r.artifact_id),
    );
    if (pending.length === 0) return;
    let stopped = false;
    const tick = async () => {
      const results = await Promise.all(
        pending.map((r) =>
          api
            .getMaterializeStatus(r.artifact_id)
            .then((s) => [r.artifact_id, s] as const)
            .catch(() => null),
        ),
      );
      if (stopped) return;
      const next: Record<string, string | null> = {};
      for (const item of results) {
        if (!item) continue;
        const [id, s] = item;
        next[id] = s.state;
        if (s.terminal) settled.current.add(id);
      }
      if (Object.keys(next).length) setRunStates((m) => ({ ...m, ...next }));
      // 还有没跑完的才继续轮询
      if (!stopped && pending.some((r) => !settled.current.has(r.artifact_id))) {
        timer = window.setTimeout(() => void tick(), 5000);
      }
    };
    let timer = window.setTimeout(() => void tick(), 0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [visible]);

  if (runs.length === 0) {
    return <Empty description="尚无物化执行记录" />;
  }
  const columns: ColumnsType<MaterializationRun> = [
    {
      title: "时间",
      key: "time",
      width: 170,
      render: (_, r) => {
        const t = r.executed_at ?? r.created_at;
        return t ? new Date(t).toLocaleString() : "—";
      },
    },
    {
      title: "操作人",
      dataIndex: "operator",
      key: "operator",
      width: 100,
      render: (v: string | null) => v || "—",
    },
    {
      title: "目标",
      key: "target",
      // 提交在选目标之前就被拒的（如所选搬运工具没有可用镜像），回执里只有 error，
      // 没有 target_datasource——这里必须容忍，否则整个弹窗白屏。
      render: (_, r) =>
        r.receipt?.target_datasource
          ? `${r.receipt.target_datasource.name}（${r.receipt.engine}）`
          : "—",
    },
    {
      title: "运行状态",
      key: "status",
      width: 130,
      render: (_, r) => {
        // 触发就没成功的（DAG 落了盘但 Airflow 没收下）没有 DagRun 可读，
        // 此时制品状态才是唯一事实。
        if (!r.ok || !r.receipt?.dag_run_id) {
          return (
            <span>
              <StatusBadge status={r.status} />
              {r.receipt && !r.ok && (
                <Tag color="orange" style={{ marginLeft: 6 }}>
                  未触发
                </Tag>
              )}
            </span>
          );
        }
        const live = runStates[r.artifact_id];
        // 还没读到就先显示提交时的状态，不留白也不假装已知结果。
        return <RunStateTag state={live ?? r.receipt.state} />;
      },
    },
    {
      title: "作业 / 调度",
      key: "jobs",
      width: 150,
      render: (_, r) =>
        r.receipt ? (
          <span className="om-muted">
            建表 {r.receipt.tables?.length ?? 0} · 搬运 {r.receipt.jobs?.length ?? 0}
            {r.receipt.schedule ? " · 定时" : ""}
          </span>
        ) : (
          "—"
        ),
    },
  ];
  return (
    <Table
      size="small"
      rowKey="artifact_id"
      columns={columns}
      dataSource={runs}
      pagination={
        runs.length > RUNS_PAGE_SIZE
          ? {
              pageSize: RUNS_PAGE_SIZE,
              current: page,
              onChange: setPage,
              showSizeChanger: false,
            }
          : false
      }
      expandable={{
        expandedRowRender: (r) => <MaterializeReceiptView run={r} />,
        rowExpandable: (r) => Boolean(r.receipt),
      }}
    />
  );
}

/** 编排物化回执：产物在哪、DagRun 到哪一步。状态权威在 Airflow，故轮询而不缓存。 */
function OrchestratedReceiptView({ run }: { run: MaterializationRun }) {
  const r = run.receipt!;
  const [status, setStatus] = useState<MaterializeStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [lineageBusy, setLineageBusy] = useState(false);
  const [lineageResult, setLineageResult] = useState<LineageEmitResult | null>(null);

  useEffect(() => {
    if (!run.artifact_id || !r.dag_run_id) return;
    let stopped = false;
    const tick = () => {
      api
        .getMaterializeStatus(run.artifact_id)
        .then((s) => {
          if (stopped) return;
          setStatus(s);
          setStatusError(null);
          // 跑完就停轮询——终态不会再变，继续问只是空耗。
          if (!s.terminal) timer = window.setTimeout(tick, 5000);
        })
        .catch((err: unknown) => {
          if (stopped) return;
          setStatusError(err instanceof Error ? err.message : "无法读取运行状态");
        });
    };
    let timer = window.setTimeout(tick, 0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [run.artifact_id, r.dag_run_id]);

  const state = status?.state ?? r.state;
  const succeeded = state === "success";

  // 各状态的任务计数，供状态行给出「2 成功 / 1 失败」这种一眼可读的分布。
  const taskCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of status?.tasks ?? []) {
      const key = t.state ?? "unknown";
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()];
  }, [status]);

  // 回执的头条说的是**这次运行怎么样了**，不是「提交成功了」。
  // 原先无论 DagRun 是 failed 还是 success，顶上都挂一条绿色的「作业已提交给调度器」，
  // 于是失败的运行在界面上看着是成功的——排查同步失败时正是被这条挡住的。
  const headline = useMemo((): {
    type: "success" | "info" | "warning" | "error";
    message: string;
    description: string;
  } => {
    if (r.error) {
      return {
        type: "warning",
        message: "作业已生成，但触发失败",
        description: r.error,
      };
    }
    if (state && FAILED_STATES.has(state)) {
      return {
        type: "error",
        message: "运行失败",
        description:
          "建表或搬运有任务未通过。点开失败任务的日志定位原因；改完在 Airflow 侧重跑该任务即可，不必从头再提交一次。",
      };
    }
    if (succeeded) {
      return {
        type: "success",
        message: "运行成功",
        description: "建表与搬运均已完成，数据已落到目标库。",
      };
    }
    if (state === "running" || state === "queued") {
      return {
        type: "info",
        message: state === "queued" ? "已排队，等待调度" : "正在运行",
        description: "建表与搬运由 Airflow 执行；本弹窗每 5 秒回读一次状态。",
      };
    }
    return {
      type: "info",
      message: "作业已提交给调度器",
      description:
        "建表与搬运由 Airflow 执行；本弹窗只回读状态，重试与补数在 Airflow 侧完成。",
    };
  }, [r.error, state, succeeded]);

  const emitLineage = () => {
    if (!run.artifact_id) return;
    setLineageBusy(true);
    api
      .emitMaterializeLineage(run.artifact_id)
      .then((res) => {
        setLineageResult(res);
        if (res.failed && res.failed > 0) {
          message.warning(`血缘上报：${res.applied ?? 0} 成功 / ${res.failed} 失败`);
        } else {
          message.success(`血缘已上报 ${res.applied ?? 0} 条（重复上报幂等）`);
        }
      })
      .catch((err: unknown) =>
        message.error(err instanceof Error ? err.message : "血缘上报失败"),
      )
      .finally(() => setLineageBusy(false));
  };
  const failedTasks = (status?.tasks ?? []).filter((t) =>
    FAILED_STATES.has(t.state ?? ""),
  );
  return (
    <div className="mtz-receipt">
      <Alert
        type={headline.type}
        showIcon
        style={{ marginBottom: 14 }}
        message={headline.message}
        description={headline.description}
        action={
          r.run_url ? (
            <a href={r.run_url} target="_blank" rel="noreferrer" className="om-link">
              Airflow <ExportOutlined style={{ fontSize: 11 }} />
            </a>
          ) : undefined
        }
      />

      {/* 失败任务单独列出来并直达日志：这是运行失败时唯一有用的下一步动作，
          此前它被埋在一列任务里，得自己认出哪个是红的。 */}
      {failedTasks.length > 0 && (
        <div className="mtz-failures">
          <div className="mtz-failures-title">失败任务（{failedTasks.length}）</div>
          {failedTasks.map((t) => (
            <div key={t.task_id} className="mtz-failure-row">
              <RunStateTag state={t.state} />
              <code>{t.task_id}</code>
              {taskUrl(r.run_url, t.task_id) && (
                <a
                  href={taskUrl(r.run_url, t.task_id)!}
                  target="_blank"
                  rel="noreferrer"
                  className="om-link"
                >
                  查看日志
                </a>
              )}
            </div>
          ))}
        </div>
      )}

      <dl className="mtz-facts">
        <dt>目标存储</dt>
        <dd>
          {/* 调用方（MaterializeReceiptView）已挡掉没有 target_datasource 的回执，
              这里仍按可选取值：判空的责任留在用到它的地方，别指望上游永远记得挡。 */}
          {r.target_datasource?.name ?? "—"}
          <span className="om-muted">（{r.engine ?? "—"}）</span>
        </dd>

        <dt>运行状态</dt>
        <dd>
          <RunStateTag state={state} />
          {taskCounts.length > 0 && (
            <span className="mtz-task-counts">
              {taskCounts.map(([s, n]) => (
                <span key={s}>
                  {RUN_STATE[s]?.label ?? s} {n}
                </span>
              ))}
            </span>
          )}
          {statusError && <div className="mtz-warn">{statusError}</div>}
        </dd>

        <dt>调度</dt>
        <dd>
          {r.schedule ? (
            <code>{r.schedule}</code>
          ) : (
            <span className="om-muted">不定时（仅手动触发）</span>
          )}
        </dd>

        <dt>作业</dt>
        <dd className="om-muted">
          建表 {r.tables?.length ?? 0} 张 · 搬运作业 {r.jobs?.length ?? 0} 个
        </dd>

        {r.unsupported && r.unsupported.length > 0 && (
          <>
            <dt>未生成作业</dt>
            <dd>
              {r.unsupported.map((u, i) => (
                <div key={i} className="om-muted">
                  <code>{u.target}</code>：{u.reason}
                </div>
              ))}
            </dd>
          </>
        )}

        <dt>血缘</dt>
        <dd>
          <Button
            size="small"
            loading={lineageBusy}
            disabled={!succeeded}
            onClick={emitLineage}
          >
            兜底回补血缘
          </Button>
          <div className="om-muted" style={{ fontSize: 12, marginTop: 6 }}>
            {succeeded
              ? "主路径由 Airflow 的 DataHub 插件自动上报；插件缺位时在此兜底回补（表级，重复幂等）。"
              : "运行成功后可回补。"}
            {lineageResult && (
              <span style={{ marginLeft: 6 }}>
                已上报 {lineageResult.applied ?? 0} / 共 {lineageResult.applicable} 条
                {lineageResult.failed ? `，失败 ${lineageResult.failed}` : ""}
              </span>
            )}
          </div>
        </dd>
      </dl>

      {/* 任务明细与产物路径是排查时才看的东西，收进折叠里，不占默认视线。 */}
      {(status?.tasks.length || r.artifacts) && (
        <Collapse
          ghost
          size="small"
          className="mtz-detail"
          items={[
            ...(status && status.tasks.length > 0
              ? [
                  {
                    key: "tasks",
                    label: `任务明细（${status.tasks.length}）`,
                    children: (
                      <div className="mtz-tasks">
                        {status.tasks.map((t) => (
                          <div key={t.task_id} className="mtz-task-row">
                            <RunStateTag state={t.state} />
                            <code>{t.task_id}</code>
                            {taskUrl(r.run_url, t.task_id) && (
                              <a
                                href={taskUrl(r.run_url, t.task_id)!}
                                target="_blank"
                                rel="noreferrer"
                                className="om-link"
                              >
                                日志
                              </a>
                            )}
                          </div>
                        ))}
                      </div>
                    ),
                  },
                ]
              : []),
            ...(r.artifacts
              ? [
                  {
                    key: "artifacts",
                    label: "产物路径",
                    children: (
                      <div className="mtz-artifacts">
                        {Object.entries(r.artifacts).map(([key, path]) => (
                          <div key={key}>
                            <span className="om-muted">{key}</span>
                            <code>{path}</code>
                          </div>
                        ))}
                      </div>
                    ),
                  },
                ]
              : []),
          ]}
        />
      )}
    </div>
  );
}

/** 执行回执。物化总是交 Airflow 编排，看 DagRun；无 target 的纯错误回执直接报错。 */
function MaterializeReceiptView({ run }: { run: MaterializationRun }) {
  const r = run.receipt;
  if (!r || !r.target_datasource) {
    return (
      <Alert
        type="error"
        showIcon
        message={`执行失败（${run.status}）`}
        description={r?.error ?? undefined}
      />
    );
  }
  return <OrchestratedReceiptView run={run} />;
}
