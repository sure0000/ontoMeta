/**
 * 每个 kind 的结构化 Spec 字段定义。**这是手动起草表单与对话提案表单的唯一字段依据。**
 *
 * 字段清单逐字对齐后端各 drafter 的 return（spec_json 是无结构 dict，无 Pydantic 模型）：
 *   metric     → drafters/metric.py:95-115
 *   transform  → drafters/transform.py:69-79
 *   sync       → drafters/sync.py:84-98
 *   cluster    → drafters/cluster.py:82-89
 *   materialize→ drafters/materialize.py:42-62
 *
 * 校验闸门（backend validation.py）会拦：对象/字段/引擎必须是本体真实值（下拉限定）、
 * 禁明文凭据（不设凭据输入框）、各 kind 必填。故本表把这些字段标为下拉 + required。
 */

export type SpecControlType =
  | "text"
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
  | { kind: "dataSources" }
  | { kind: "databases"; dependsOn: string }
  | { kind: "cleansingRules" }
  | { kind: "bmServices" };

export interface SpecFieldDef {
  key: string;
  label: string;
  control: SpecControlType;
  required?: boolean;
  optionSource?: OptionSource;
  default?: unknown;
  help?: string;
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

/** cluster 可部署组件白名单：后端 BM_MANAGED_SERVICES（cluster.py:22）。 */
export const BM_MANAGED_SERVICES: { value: string; label: string }[] = [
  "zookeeper",
  "hdfs",
  "yarn",
  "mapreduce",
  "hive",
  "spark",
  "flink",
  "tez",
  "hbase",
  "kafka",
  "solr",
  "mysql",
  "prometheus",
  "grafana",
  "seatunnel",
].map((s) => ({ value: s, label: s }));

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

const EXEC_MODE_OPTIONS = [
  { value: "batch", label: "批处理" },
  { value: "streaming", label: "流处理" },
];

const engineField = (): SpecFieldDef => ({
  key: "engine",
  label: "引擎",
  control: "engineSelect",
  optionSource: { kind: "engines" },
  default: "hive",
});

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
    engineField(),
    {
      key: "target_layer",
      label: "目标层",
      control: "select",
      optionSource: { kind: "static", options: LAYER_OPTIONS },
      default: "ads",
    },
    { key: "database_prefix", label: "库名前缀", control: "text" },
    {
      key: "execution_mode",
      label: "执行模式",
      control: "select",
      optionSource: { kind: "static", options: EXEC_MODE_OPTIONS },
      default: "batch",
    },
  ],
  transform: [
    {
      key: "target_table",
      label: "目标表（本体对象）",
      control: "objectSingle",
      required: true,
      optionSource: { kind: "objectTypes" },
    },
    engineField(),
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
    {
      key: "execution_mode",
      label: "执行模式",
      control: "select",
      optionSource: { kind: "static", options: EXEC_MODE_OPTIONS },
      default: "batch",
    },
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
      key: "mode",
      label: "装载方式",
      control: "select",
      optionSource: { kind: "static", options: LOAD_STRATEGY_OPTIONS },
      default: "full",
    },
    {
      key: "partition_key",
      label: "分区键",
      control: "propertySingle",
      optionSource: { kind: "properties" },
      help: "增量/CDC 装载时的分区列；从本体字段中选",
    },
    engineField(),
    {
      key: "source_ref_alias",
      label: "源连接别名",
      control: "text",
      help: "连接别名（=Airflow conn_id），不是凭据本身",
    },
  ],
  cluster: [
    {
      key: "hosts",
      label: "主机",
      control: "stringList",
      required: true,
      help: "只填主机别名/地址，禁止填入任何密码或密钥",
    },
    {
      key: "services",
      label: "服务组件",
      control: "multiSelect",
      required: true,
      optionSource: { kind: "bmServices" },
      help: "仅支持 Bigtop Manager 托管的组件白名单",
    },
    { key: "stack", label: "技术栈", control: "text", default: "bigtop-3.3.0" },
    {
      key: "credential_ref",
      label: "凭据引用",
      control: "text",
      default: "cluster_ssh_default",
      help: "SSH 凭据的引用别名；密钥由执行器按别名从独立存储取",
    },
  ],
  materialize: [
    {
      key: "target_datasource_id",
      label: "目标数据源",
      control: "select",
      required: true,
      optionSource: { kind: "dataSources" },
    },
    {
      key: "target_database",
      label: "目标库",
      control: "select",
      optionSource: { kind: "databases", dependsOn: "target_datasource_id" },
    },
    engineField(),
    {
      key: "load_strategy",
      label: "装载方式",
      control: "select",
      optionSource: { kind: "static", options: LOAD_STRATEGY_OPTIONS },
    },
    { key: "refresh_cron", label: "调度频率", control: "cron" },
  ],
};

/** metric 走 drafter+context 路径（非 spec 直填），其余 kind 走 spec 直填。 */
export const DRAFTER_CONTEXT_KINDS = new Set(["metric"]);
