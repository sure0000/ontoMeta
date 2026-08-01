import { DatabaseOutlined, WarningOutlined } from "@ant-design/icons";
import {
  Alert,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Spin,
  Table,
  Tabs,
  Tag,
  Transfer,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type {
  DataSource,
  MaterializationContract,
  MaterializationLoadStrategy,
  MaterializationRun,
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

// 同步方式：复用后端 LoadStrategy，作为本次物化运行期的一次性选择（不写回契约）。
const LOAD_STRATEGY_OPTIONS: { value: MaterializationLoadStrategy; label: string }[] =
  [
    { value: "full", label: "全量覆盖（INSERT OVERWRITE）" },
    { value: "incremental", label: "增量追加（INSERT INTO，按分区键）" },
    { value: "cdc", label: "CDC 变更捕获（物化内不承载，请用同步作业）" },
  ];

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
  load_strategy: MaterializationLoadStrategy;
  database_prefix?: string;
  partition_key?: string;
}

/** 由数据源类型推导物化引擎（DDL/ETL 方言）。仅仓库类型可作物化目标，否则返回 null。 */
function engineOfKind(kind?: string | null): string | null {
  const k = kind?.toLowerCase();
  return k && ENGINE_VALUES.has(k) ? k : null;
}

/** 生成的目标表名（治理辅助信息）：库名=层[_前缀]，表名=实体目标名。 */
function tableNameOf(c: MaterializationContract, prefix?: string | null): string {
  const p = prefix?.trim();
  const db = p ? `${c.target_layer}_${p}` : c.target_layer;
  return `${db}.${c.target_name ?? c.target_id}`;
}

/** 本体一键物化弹窗：选目标存储 / 引擎 / 库前缀 / 待物化实体 → 真正落库执行。
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
  const [result, setResult] = useState<MaterializationRun | null>(null);
  const [runs, setRuns] = useState<MaterializationRun[]>([]);
  const [activeTab, setActiveTab] = useState<"run" | "history">("run");
  // 单实体物化：命中契约不可物化 / 尚未生成时的原因，禁用执行并提示。
  const [scopeError, setScopeError] = useState<string | null>(null);

  const scoped = Boolean(scopeTargetId);
  const isDraft = Boolean(ontologyStatus && ontologyStatus !== "published");
  // 表名预览与分区项随库前缀/同步方式实时变化。
  const dbPrefix = Form.useWatch("database_prefix", form);
  const loadStrategy = Form.useWatch("load_strategy", form);

  useEffect(() => {
    if (!open) return;
    setResult(null);
    setActiveTab("run");
    setScopeError(null);
    setLoading(true);
    void (async () => {
      try {
        const [ds, runList] = await Promise.all([
          api.listDataSources(),
          api.listMaterializationRuns(ontologyId),
        ]);
        setDataSources(ds);
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
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, [open, ontologyId, scopeTargetId]);

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
    // 增量 + 指定分区键：按契约 id 写入 overrides（写回并钉住所选实体的分区键）。
    const partitionKey = values.partition_key?.trim();
    const overrides: Record<string, { partition_key: string }> = {};
    if (values.load_strategy === "incremental" && partitionKey) {
      for (const t of allTargets) {
        if (targetKeys.includes(t.key)) {
          overrides[t.contract.id] = { partition_key: partitionKey };
        }
      }
    }
    setRunning(true);
    setResult(null);
    try {
      const run = await api.materializeOntology(ontologyId, {
        target_datasource_id: values.target_datasource_id,
        engine,
        database_prefix: values.database_prefix?.trim() || null,
        load_strategy: values.load_strategy || null,
        // 单实体锁定其名；整体物化时全选传 null（不裁剪），否则传勾选的实体名。
        selected_targets: scoped
          ? targetKeys
          : targetKeys.length === allTargets.length
            ? null
            : targetKeys,
        ...(Object.keys(overrides).length ? { overrides } : {}),
      });
      setResult(run);
      if (run.ok) message.success("物化完成：建表与数据装载均成功");
      else message.warning("物化已执行，但存在失败项，请查看回执");
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

  const runTab = (
    <>
      <div
        className="muted"
        style={{ marginBottom: 12, fontSize: 12, lineHeight: 1.6 }}
      >
        <WarningOutlined style={{ color: "#faad14", marginRight: 6 }} />
        破坏性操作：对目标库建表并覆盖写入（INSERT OVERWRITE），会重写整表/分区。
        {isDraft && (
          <span style={{ color: "#cf1322", marginLeft: 6 }}>
            当前本体未发布（{LABELS[ontologyStatus!] ?? ontologyStatus}），将落未发布内容。
          </span>
        )}
      </div>
      <Form form={form} layout="vertical" initialValues={{ load_strategy: "full" }}>
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
            options={dataSources.map((d) => ({
              value: d.id,
              title:
                d.status === "ok"
                  ? undefined
                  : "该连接未测试或状态异常，建议先到设置里测试",
              label: `${d.name} · ${d.kind} · ${d.status === "ok" ? "已连通" : d.status}`,
            }))}
            notFoundContent={
              <Empty description="尚无数据源，请到 系统设置 → 数据源 添加" />
            }
          />
        </Form.Item>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <Form.Item
            name="load_strategy"
            label="同步方式"
            style={{ flex: 1, minWidth: 260 }}
            extra="全量覆盖（默认）/ 增量按分区键追加 / CDC 请改用同步作业"
          >
            <Select options={LOAD_STRATEGY_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="database_prefix"
            label="库名前缀（可选）"
            style={{ flex: 1, minWidth: 200 }}
            extra="如 erp → dim_erp；留空则用 dim / dwd / ads"
          >
            <Input placeholder="留空则用 dim / dwd / ads" allowClear />
          </Form.Item>
        </div>
        {loadStrategy === "incremental" && (
          <Form.Item
            name="partition_key"
            label="分区键"
            extra="增量装载按此列分区（水位由调度器注入）；留空则沿用各实体契约既定分区键"
          >
            <Input placeholder="如 dt / ds / stat_date" allowClear />
          </Form.Item>
        )}
        <Form.Item
          label={scoped ? "物化实体 → 目标表" : "待物化实体 → 目标表（默认全选）"}
          extra="目标表名按「层[_库前缀].实体名」规则生成，供后续治理对照"
        >
          {scoped ? (
            scopeError ? (
              <Alert type="warning" showIcon message={scopeError} />
            ) : allTargets[0] ? (
              <span>
                <Tag color="blue">{scopeLabel ?? allTargets[0].contract.target_display_name ?? "—"}</Tag>
                <code style={{ fontSize: 12 }}>{tableNameOf(allTargets[0].contract, dbPrefix)}</code>
              </span>
            ) : (
              <Tag>—</Tag>
            )
          ) : (
            <Transfer
              dataSource={allTargets}
              titles={["不物化", "物化"]}
              targetKeys={targetKeys}
              onChange={(keys) => setTargetKeys(keys as string[])}
              render={(item) => (
                <span>
                  {item.contract.target_display_name ??
                    item.contract.target_name ??
                    item.contract.target_id}
                  <code className="muted" style={{ marginLeft: 6, fontSize: 11 }}>
                    {tableNameOf(item.contract, dbPrefix)}
                  </code>
                </span>
              )}
              listStyle={{ width: 320, height: 240 }}
            />
          )}
        </Form.Item>
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
      okText={result ? "再次物化" : "执行物化"}
      okButtonProps={{
        loading: running,
        danger: true,
        disabled: scoped && (Boolean(scopeError) || targetKeys.length === 0),
        // 「历史」页只读，隐藏执行按钮。
        style: activeTab === "history" ? { display: "none" } : undefined,
      }}
      cancelText="关闭"
      width={720}
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

/** 历次物化任务列表：时间 / 操作人 / 目标 / 状态 / 分阶段成败，行内展开完整回执。 */
function MaterializeRunsTable({ runs }: { runs: MaterializationRun[] }) {
  if (runs.length === 0) {
    return <Empty description="尚无物化执行记录" />;
  }
  const phaseText = (p?: { executed: number; failed: number; total: number }) =>
    p ? `${p.executed}/${p.total}${p.failed ? ` 失${p.failed}` : ""}` : "—";
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
      render: (_, r) =>
        r.receipt
          ? `${r.receipt.target_datasource.name}（${r.receipt.engine}）`
          : "—",
    },
    {
      title: "状态",
      key: "status",
      width: 130,
      render: (_, r) => (
        <span>
          <StatusBadge status={r.status} />
          {r.receipt && !r.ok && (
            <Tag color="orange" style={{ marginLeft: 6 }}>
              有失败项
            </Tag>
          )}
        </span>
      ),
    },
    {
      title: "建表 / 装载",
      key: "phases",
      width: 130,
      render: (_, r) =>
        r.receipt ? (
          <span className="muted">
            建 {phaseText(r.receipt.ddl)} · 装 {phaseText(r.receipt.etl)}
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
      pagination={runs.length > 8 ? { pageSize: 8 } : false}
      expandable={{
        expandedRowRender: (r) => <MaterializeReceiptView run={r} />,
        rowExpandable: (r) => Boolean(r.receipt),
      }}
    />
  );
}

/** 执行回执：分 DDL/ETL 两阶段展示成功/失败与错误。 */
function MaterializeReceiptView({ run }: { run: MaterializationRun }) {
  const r = run.receipt;
  if (!r) {
    return (
      <Alert type="error" showIcon message={`执行失败（${run.status}）`} />
    );
  }
  const phase = (label: string, p: typeof r.ddl) => {
    if (p.skipped) {
      return (
        <Descriptions.Item label={label}>
          <Tag>已跳过</Tag>
          <span className="muted">{p.skip_reason}</span>
        </Descriptions.Item>
      );
    }
    const failed = p.failed > 0 || p.error;
    return (
      <Descriptions.Item label={label}>
        <Tag color={failed ? "red" : "green"}>
          成功 {p.executed} / 失败 {p.failed} / 共 {p.total}
        </Tag>
        {p.error && <div style={{ color: "#cf1322", marginTop: 4 }}>{p.error}</div>}
      </Descriptions.Item>
    );
  };
  return (
    <>
      <Divider style={{ margin: "12px 0" }} />
      <Alert
        type={run.ok ? "success" : "warning"}
        showIcon
        style={{ marginBottom: 12 }}
        message={run.ok ? "物化成功" : "物化已执行，存在失败项"}
      />
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="目标存储">
          {r.target_datasource.name}（{r.engine}）
        </Descriptions.Item>
        {phase("建表 DDL", r.ddl)}
        {phase("数据装载 ETL", r.etl)}
        {r.warnings && r.warnings.length > 0 && (
          <Descriptions.Item label="提示">
            {r.warnings.map((w, i) => (
              <div key={i} style={{ color: "#d46b08" }}>
                {w.target}（{w.feature}）：{w.detail}
              </div>
            ))}
          </Descriptions.Item>
        )}
        {r.unsupported && r.unsupported.length > 0 && (
          <Descriptions.Item label="未生成">
            {r.unsupported.map((u, i) => (
              <div key={i} className="muted">
                {u.target}：{u.reason}
              </div>
            ))}
          </Descriptions.Item>
        )}
      </Descriptions>
    </>
  );
}
