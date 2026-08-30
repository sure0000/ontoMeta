/**
 * 每个 kind 的结构化 Spec 字段定义。**这是手动起草表单与对话提案表单的唯一字段依据。**
 *
 * 字段清单逐字对齐后端各 drafter 的 return（spec_json 是无结构 dict，无 Pydantic 模型）：
 *   metric     → drafters/metric.py:95-115
 *   transform  → drafters/transform.py:69-79
 *   sync       → drafters/sync.py:84-98
 *   materialize→ drafters/materialize.py:42-62
 *
 * 校验闸门（backend validation.py）会拦：对象/字段/引擎必须是本体真实值（下拉限定）、
 * 禁明文凭据（不设凭据输入框）、各 kind 必填。故本表把这些字段标为下拉 + required。
 */

export type SpecControlType =
  | "text"
  | "number"
  | "textarea"
  | "select"
  | "multiSelect"
  | "stringList" // tags 自由列表（hosts）
  | "objectSingle" // 本体对象单选，value=对象 name
  | "objectMultiSelect" // 本体对象多选，value=对象 name
  | "propertySingle" // 本体字段单选，value=字段 name
  | "propertyMultiSelect" // 本体字段多选，value=字段 name
  | "businessLogicSelect" // 业务逻辑单选，value=logic id
  | "engineSelect"
  | "cron";

export type OptionSource =
  | { kind: "static"; options: { value: string; label: string }[] }
  | { kind: "objectTypes" }
  /**
   * 本体字段候选。`scopeField` 指向表单里选定对象的那个字段（sync 是 object_type、
   * transform 是 target_table）：主键 / 增量字段 / sequence 列必须是**那张表**上的列，
   * 全本体混列会让人从几百张表的字段里选到一个根本不在目标表上的名字。
   */
  | { kind: "properties"; scopeField?: string }
  | { kind: "businessLogics" }
  | { kind: "engines" }
  | {
      kind: "dataSources";
      purpose?: "business_source" | "warehouse";
      engine?: string;
      defaultOnly?: boolean;
      executableOnly?: boolean;
      /**
       * 这个候选表所服务的字段自己的 key。
       *
       * 上面几个过滤条件说的是「现在**可以选**哪些」，而 Spec 里存着的可能是一条已经
       * 掉出候选集的数据源（源库被停用、目标仓不再是默认仓）。候选里找不到时下拉和
       * 只读预览都会退回裸 uuid——恰恰是最该看清名字的时候。给了 selfField，
       * `useSpecOptions` 就能把当前值从未过滤的清单里捞回来补进候选。
       */
      selfField?: string;
    }
  | { kind: "databases"; dependsOn: string }
  | { kind: "cleansingRules" };

export interface SpecFieldDef {
  key: string;
  label: string;
  control: SpecControlType;
  required?: boolean;
  optionSource?: OptionSource;
  default?: unknown;
  help?: string;
  /** number 控件的取值范围（与后端 flink_params 的校验同界，免得填完才被闸门拦）。 */
  min?: number;
  max?: number;
  /** 折进「高级」折叠面板：调优项，不填也能跑（留空 = 跟随设置页默认）。 */
  advanced?: boolean;
  /**
   * 条件可见：`{ field: "mode", in: ["incremental", "cdc"] }`。不满足时不渲染、不校验、
   * 提交前也会被剔除——先选 CDC 填了 sequence 列、又改回全量，那个值若留在 spec 里会
   * 真的进建表语句（Doris 的 sequence 列），「确认的是全量、建出来的是 CDC 表」。
   */
  visibleWhen?: { field: string; in: string[] };
}

const INCREMENTAL_OR_CDC = { field: "mode", in: ["incremental", "cdc"] };
const INCREMENTAL_ONLY = { field: "mode", in: ["incremental"] };
const CDC_ONLY = { field: "mode", in: ["cdc"] };

// ---- 闭集常量（与后端同源，注释标注来源；变动极少，P0 前端内置避免多一次请求） ----

/**
 * transform 清洗规则：后端 SUPPORTED_CLEANSING_RULES（drafters/transform.py）。
 * 闭集的意义是「说得出的都做得到」——曾经的 `normalize_code` 没有实现也说不出标准化成
 * 什么，选了它的任务照常"成功"而 SQL 一个字符没改，已从两端同时下线。
 */
export const CLEANSING_RULES: { value: string; label: string }[] = [
  { value: "deduplicate", label: "去重" },
  { value: "drop_null", label: "空值过滤" },
  { value: "trim", label: "字符串列去首尾空格" },
  { value: "uppercase", label: "字符串列转大写" },
  { value: "lowercase", label: "字符串列转小写" },
];

const LOAD_STRATEGY_OPTIONS = [
  { value: "full", label: "全量覆盖" },
  { value: "incremental", label: "增量追加" },
  { value: "cdc", label: "CDC 变更捕获" },
];

const LAYER_OPTIONS = [
  { value: "dim", label: "维度层 DIM" },
  { value: "dwd", label: "明细层 DWD" },
  { value: "dws", label: "汇总层 DWS" },
  { value: "ads", label: "应用层 ADS" },
];

// Transform 是 Doris 内加工，产物落在 DIM/DWD/DWS；ADS 由 metric 任务负责。
const TRANSFORM_LAYER_OPTIONS = LAYER_OPTIONS.filter((option) => option.value !== "ads");
const METRIC_LAYER_OPTIONS = LAYER_OPTIONS.filter((option) => option.value === "ads");

/** flink run -t 的取值闭集：与后端 `flink_params.DEPLOY_TARGETS`、设置页下拉同源。 */
const DEPLOY_TARGET_OPTIONS = [
  { value: "yarn-per-job", label: "yarn-per-job" },
  { value: "yarn-session", label: "yarn-session" },
  { value: "remote", label: "remote" },
  { value: "local", label: "local" },
];

/**
 * 目标数据源。**四种任务都要**——执行器一律 `spec.get("target_datasource_id") or
 * context.get(...)`，缺它时 materialize 连起草都过不去（drafter 的 required_context），
 * 而 sync/transform/metric 会「成功」但只渲染作业配置/SQL，一行数据都不动。
 * 只存数据源 id，DSN 由执行侧按 id 取（凭据不进 Spec）。
 */
const targetDatasourceField = (note: string): SpecFieldDef => ({
  key: "target_datasource_id",
  label: "目标数仓",
  control: "select",
  required: true,
  optionSource: {
    kind: "dataSources",
    purpose: "warehouse",
    engine: "doris",
    defaultOnly: true,
    executableOnly: true,
    selfField: "target_datasource_id",
  },
  help: note,
});

/**
 * 任务级 Flink 执行参数。**设置页那份是默认值，这里是这一个任务的覆盖**——
 * 一条搬 300 张表的同步和一条小指标聚合，对并行度/队列/checkpoint 的要求不是一回事。
 * 留空 = 跟随设置页（后端 `flink_params.normalize` 直接丢弃空值，不落进 Spec）。
 *
 * 只给**真的经 Flink 跑**的同步任务。物化只建表
 * （Airflow SQLExecuteQueryOperator 直连目标仓），给它摆一组 Flink 参数等于让人白填。
 *
 * 不含 SqlRunner JAR / main class / flink 命令路径：那是「Flink 装在哪」的部署事实
 * （jar 由 ontoMeta 随包投递），逐任务改只会让投递的 jar 与命令行对不上，仍只在设置页配。
 */
const flinkFields = (): SpecFieldDef[] => [
  {
    key: "flink_parallelism",
    label: "并行度",
    control: "number",
    min: 1,
    max: 512,
    advanced: true,
    help: "flink run -p；留空跟随设置页",
  },
  {
    key: "flink_yarn_queue",
    label: "YARN 队列",
    control: "text",
    advanced: true,
    help: "-Dyarn.application.queue；留空跟随设置页",
  },
  {
    key: "flink_deploy_target",
    label: "提交目标",
    control: "select",
    optionSource: { kind: "static", options: DEPLOY_TARGET_OPTIONS },
    advanced: true,
    help: "flink run -t；留空跟随设置页",
  },
  {
    key: "flink_checkpoint_dir",
    label: "Checkpoint 目录",
    control: "text",
    advanced: true,
    help: "流式 / CDC 作业的读位点目录（file:/// 或 hdfs://）；批作业不需要",
  },
  {
    key: "flink_extra_args",
    label: "额外 flink run 参数",
    control: "stringList",
    advanced: true,
    help: "如 -Dtaskmanager.memory.process.size=2g，回车添加",
  },
];

/**
 * 各 kind 的字段集。metric 是特例：手填只暴露「选一个业务逻辑」+ 少量覆盖，
 * 其余字段由 MetricDrafter 从该 logic 推导（走 context 路径，非 spec 直填）。
 */
export const SPEC_FIELDS: Record<string, SpecFieldDef[]> = {
  metric: [
    {
      key: "business_logic_id",
      label: "业务逻辑（口径）",
      control: "businessLogicSelect",
      required: true,
      optionSource: { kind: "businessLogics" },
      help: "口径与绑定的对象/字段由它推导",
    },
    targetDatasourceField("聚合结果写入默认 Doris 的 ads 层"),
    {
      key: "target_layer",
      label: "目标层",
      control: "select",
      optionSource: { kind: "static", options: METRIC_LAYER_OPTIONS },
      default: "ads",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    { key: "refresh_cron", label: "调度频率", control: "cron", help: "留空 = 仅手动触发" },
  ],
  transform: [
    {
      key: "target_table",
      label: "目标表（本体对象）",
      control: "objectSingle",
      required: true,
      optionSource: { kind: "objectTypes" },
    },
    targetDatasourceField("加工结果写入默认 Doris"),
    {
      key: "target_layer",
      label: "目标层",
      control: "select",
      optionSource: { kind: "static", options: TRANSFORM_LAYER_OPTIONS },
      default: "dim",
    },
    {
      key: "cleansing_rules",
      label: "清洗规则",
      control: "multiSelect",
      optionSource: { kind: "cleansingRules" },
      help: "每条对应一个确定性清洗算子",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    { key: "refresh_cron", label: "调度频率", control: "cron", help: "留空 = 仅手动触发" },
    { key: "notes", label: "备注", control: "textarea" },
  ],
  sync: [
    {
      key: "object_type",
      label: "对象（本体）",
      control: "objectSingle",
      required: true,
      optionSource: { kind: "objectTypes" },
    },
    // ── 连接步骤 ──────────────────────────────────────────────────────────────
    {
      key: "source_datasource_id",
      label: "业务源",
      control: "select",
      required: true,
      optionSource: {
        kind: "dataSources",
        purpose: "business_source",
        executableOnly: true,
        selfField: "source_datasource_id",
      },
      help: "须与所选对象的来源匹配",
    },
    // 同步的落点不给选：库恒为 ods、表名恒为 ods_{数据域}_{原始表名}（后端 ods_naming）。
    // 分层（dim/dwd/dws/ads）是加工与聚合任务的事，同步表单里不该出现层或库名前缀。
    targetDatasourceField("数据落到默认 Doris 的 ods 库"),
    // ── 策略步骤 ──────────────────────────────────────────────────────────────
    {
      key: "mode",
      label: "装载方式",
      control: "select",
      optionSource: { kind: "static", options: LOAD_STRATEGY_OPTIONS },
      default: "full",
    },
    {
      key: "primary_keys",
      label: "业务主键",
      control: "propertyMultiSelect",
      required: true,
      visibleWhen: INCREMENTAL_OR_CDC,
      optionSource: { kind: "properties", scopeField: "object_type" },
      help: "靠它做 UPSERT 去重，猜错会让重跑插出重复行",
    },
    {
      key: "incremental_column",
      label: "增量字段",
      control: "propertySingle",
      required: true,
      visibleWhen: INCREMENTAL_ONLY,
      optionSource: { kind: "properties", scopeField: "object_type" },
      help: "每轮只搬该字段 ≥ 上次成功水位的行；通常是更新时间列",
    },
    {
      key: "initial_watermark",
      label: "初始水位",
      control: "text",
      required: true,
      visibleWhen: INCREMENTAL_ONLY,
      help: "第一次从这里开始，之后由每轮成功水位自动推进",
    },
    {
      key: "sequence_column",
      label: "CDC Sequence 列",
      control: "propertySingle",
      required: true,
      visibleWhen: CDC_ONLY,
      optionSource: { kind: "properties", scopeField: "object_type" },
      help: "同一主键的多条变更按它定新旧，避免乱序回放把旧值覆盖成最新",
    },
    {
      key: "delete_policy",
      label: "DELETE 策略",
      control: "select",
      visibleWhen: CDC_ONLY,
      optionSource: {
        kind: "static",
        options: [
          { value: "ignore", label: "忽略删除（源删了 ODS 保留）" },
          { value: "soft_delete", label: "软删除（打标记）" },
          { value: "hard_delete", label: "传播删除（ODS 同步删除）" },
        ],
      },
      default: "ignore",
    },
    /**
     * 调度频率。**同步最该有的一个参数**：入仓作业跑一次不叫管道。此前 Spec 里没有这个
     * 键，产出的 DAG 一律 schedule=None，只能手动点；想定时只能绕到物化弹窗里逐实体改
     * 契约的 refresh_cron，没人找得到。留空 = 仅手动触发。
     */
    { key: "refresh_cron", label: "调度频率", control: "cron", help: "留空 = 仅手动触发" },
    ...flinkFields(),
  ],
  materialize: [
    targetDatasourceField("物化只建结构，不搬数据"),
    {
      key: "target_database",
      label: "目标数据库",
      control: "select",
      required: true,
      optionSource: { kind: "databases", dependsOn: "target_datasource_id" },
      help: "只能选已存在的库，物化不会自动建库",
    },
  ],
};

/** sync 连接步骤字段键（第 2 步：选数据源）。 */
export const SYNC_CONN_KEYS = new Set(["source_datasource_id", "target_datasource_id"]);

/** sync 策略步骤需要跳过的键（已在连接步骤展示）。 */
export const SYNC_STRATEGY_SKIP_KEYS: Set<string> = SYNC_CONN_KEYS;

/**
 * 声明了 `default` 的字段的初值。**表单上写着「默认 X」，提交的就必须是 X。**
 *
 * 此前 `default` 只当占位文案用（Select 的 placeholder「默认 full」），一个值都不进
 * specData：于是新建同步任务时装载方式那格显示「默认 full」，提交的 context 里却没有
 * `mode`，后端 SyncDrafter 退回**物化契约的 load_strategy**——契约是 incremental 的对象
 * 就建成了增量任务。而增量要的业务主键/初始水位在向导里是 `visibleWhen: mode=incremental`
 * 的隐藏字段（当时 mode 为空，没渲染），任务一建出来就被校验闸门以
 * `sync_primary_key_missing` / `sync_initial_watermark_missing` 双阻断卡死，且无处可填。
 */
export function specDefaults(kind: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const def of SPEC_FIELDS[kind] ?? []) {
    if (def.default !== undefined) out[def.key] = def.default;
  }
  return out;
}

/** 条件可见判定。没声明 `visibleWhen` 的字段恒可见。 */
export function isFieldVisible(def: SpecFieldDef, values: Record<string, unknown>): boolean {
  const cond = def.visibleWhen;
  if (!cond) return true;
  return cond.in.includes(String(values[cond.field] ?? ""));
}

/**
 * 表单里标了 required 的字段——向导提交前据此做真校验（不只是画个星号）。
 * 给了 `values` 就跳过当前不可见的字段：全量同步不该被一个看不见的 sequence 列卡住。
 */
export function requiredSpecKeys(
  kind: string,
  skipKeys?: Set<string>,
  values?: Record<string, unknown>,
): SpecFieldDef[] {
  return (SPEC_FIELDS[kind] ?? []).filter(
    (f) => f.required && !(skipKeys?.has(f.key) ?? false) && (!values || isFieldVisible(f, values)),
  );
}

/**
 * 提交前剔除当前不可见字段的取值。改回全量后，先前填的 CDC 参数不能悄悄留在 Spec 里
 * ——它们会真的生效（sequence 列进建表语句、主键进 Unique Key 模型）。
 */
export function pruneHiddenSpecValues(
  kind: string,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const out = { ...values };
  for (const def of SPEC_FIELDS[kind] ?? []) {
    if (!isFieldVisible(def, values)) delete out[def.key];
  }
  return out;
}

/** metric 走 drafter+context 路径（非 spec 直填），其余 kind 走 spec 直填。 */
export const DRAFTER_CONTEXT_KINDS = new Set(["metric"]);
