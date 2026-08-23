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
  | { kind: "properties" }
  | { kind: "businessLogics" }
  | { kind: "engines" }
  | {
      kind: "dataSources";
      purpose?: "business_source" | "warehouse";
      engine?: string;
      defaultOnly?: boolean;
      executableOnly?: boolean;
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
}

// ---- 闭集常量（与后端同源，注释标注来源；变动极少，P0 前端内置避免多一次请求） ----

/** transform 清洗规则：后端 SUPPORTED_CLEANSING_RULES（transform.py:31）。 */
export const CLEANSING_RULES: { value: string; label: string }[] = [
  { value: "deduplicate", label: "去重" },
  { value: "drop_null", label: "空值过滤" },
  { value: "trim", label: "去除首尾空格" },
  { value: "uppercase", label: "转大写" },
  { value: "lowercase", label: "转小写" },
  { value: "normalize_code", label: "编码标准化" },
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
  },
  help: note,
});

/**
 * 任务级 Flink 执行参数。**设置页那份是默认值，这里是这一个任务的覆盖**——
 * 一条搬 300 张表的同步和一条小指标聚合，对并行度/队列/checkpoint 的要求不是一回事。
 * 留空 = 跟随设置页（后端 `flink_params.normalize` 直接丢弃空值，不落进 Spec）。
 *
 * 只给**真的经 Flink 跑**的三类任务（sync/transform/metric）。物化只建表
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
    help: "这个任务的 flink run -p；留空跟随设置页。大表搬运调高，小任务别占满集群",
  },
  {
    key: "flink_yarn_queue",
    label: "YARN 队列",
    control: "text",
    advanced: true,
    help: "提交到哪个队列（-Dyarn.application.queue）；留空跟随设置页",
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
    help: "流式/CDC 作业的读位点目录，file:///var/… 或 hdfs://…；批作业不需要，留空跟随设置页",
  },
  {
    key: "flink_extra_args",
    label: "额外 flink run 参数",
    control: "stringList",
    advanced: true,
    help: "如 -Dtaskmanager.memory.process.size=2g，输入后回车添加；不接受空白与 shell 元字符",
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
      help: "选一个已定义的业务逻辑，指标口径与绑定的对象/字段由它推导",
    },
    targetDatasourceField("固定使用默认 Doris；不选则只生成 Doris DDL+SQL，不落库"),
    {
      key: "target_layer",
      label: "目标层",
      control: "select",
      optionSource: { kind: "static", options: LAYER_OPTIONS },
      default: "ads",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    { key: "schedule", label: "Airflow 调度", control: "cron" },
  ],
  transform: [
    {
      key: "target_table",
      label: "目标表（本体对象）",
      control: "objectSingle",
      required: true,
      optionSource: { kind: "objectTypes" },
    },
    targetDatasourceField("固定使用默认 Doris；未配置时只生成 Doris SQL，不执行"),
    {
      key: "target_layer",
      label: "目标层",
      control: "select",
      optionSource: { kind: "static", options: LAYER_OPTIONS },
      default: "dim",
    },
    {
      key: "cleansing_rules",
      label: "清洗规则",
      control: "multiSelect",
      optionSource: { kind: "cleansingRules" },
      help: "可多选；每条对应一个确定性清洗算子",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    { key: "schedule", label: "Airflow 调度", control: "cron" },
    { key: "notes", label: "备注", control: "textarea" },
  ],
  sync: [
    {
      key: "object_type",
      label: "对象（本体）",
      control: "objectSingle",
      required: true,
      optionSource: { kind: "objectTypes" },
      help: "选它后由 drafter 自动带出 source/target，无需手填",
    },
    {
      key: "source_datasource_id",
      label: "业务源 DataSource",
      control: "select",
      required: true,
      optionSource: { kind: "dataSources", purpose: "business_source", executableOnly: true },
      help: "候选仅含启用且已配置连接的 business_source；最终还须与本体 source_ref 匹配",
    },
    targetDatasourceField("固定选择启用的默认 Doris；同步只写其 ODS 层"),
    { key: "target_ods_database", label: "ODS 数据库", control: "text", required: true, default: "ods" },
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
      optionSource: { kind: "properties" },
      help: "incremental/CDC 必填；用于 Doris Unique Key UPSERT",
    },
    {
      key: "incremental_column",
      label: "增量字段",
      control: "propertySingle",
      optionSource: { kind: "properties" },
      help: "incremental 模式必填",
    },
    { key: "initial_watermark", label: "初始水位", control: "text", help: "incremental 模式必填" },
    {
      key: "sequence_column",
      label: "CDC Sequence 列",
      control: "propertySingle",
      optionSource: { kind: "properties" },
      help: "CDC 必填，保证 UPDATE 顺序",
    },
    {
      key: "delete_policy",
      label: "DELETE 策略",
      control: "select",
      optionSource: { kind: "static", options: [
        { value: "ignore", label: "忽略删除" },
        { value: "soft_delete", label: "软删除" },
        { value: "hard_delete", label: "传播删除（仅 CDC）" },
      ] },
      default: "ignore",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    ...flinkFields(),
  ],
  materialize: [
    targetDatasourceField("固定使用启用、已配置连接的默认 Doris；物化只建结构"),
    {
      key: "target_database",
      label: "目标数据库",
      control: "select",
      required: true,
      optionSource: { kind: "databases", dependsOn: "target_datasource_id" },
      help: "选择 Doris 中已存在的数据库；系统不会自动创建数据库",
    },
  ],
};

/** 表单里标了 required 的字段——向导提交前据此做真校验（不只是画个星号）。 */
export function requiredSpecKeys(kind: string, skipKeys?: Set<string>): SpecFieldDef[] {
  return (SPEC_FIELDS[kind] ?? []).filter((f) => f.required && !(skipKeys?.has(f.key) ?? false));
}

/** metric 走 drafter+context 路径（非 spec 直填），其余 kind 走 spec 直填。 */
export const DRAFTER_CONTEXT_KINDS = new Set(["metric"]);
