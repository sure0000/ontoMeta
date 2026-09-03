/**
 * 血缘补录页的**原型数据**。
 *
 * 这一页目前没有后端：`/api/lineage/supplement/*` 还没写，本文件是界面能长什么样的
 * 唯一事实源。表名、URN 形状照着真实 ERP 域（mysql / _d71df877e93eac81）造，
 * 数量与 SQL 是编的——页头挂着「原型 · 示例数据」说明这件事。
 *
 * 一条口径：表名在数据里一律写**短名**（`tabCustomer`），带点的（`ext_market.xxx`）
 * 视为已带库名。展示用短名，构造 URN 时才 `qualify()`。
 */

export const SOURCE_DB = "_d71df877e93eac81";

/** 域级事实。页头只讲一件事：**有多少张表是孤岛**。 */
export const DOMAIN_FACTS = {
  domain: "ERP",
  platform: "mysql",
  database: SOURCE_DB,
  fabric: "PROD",
  total: 734,
  withLineage: 302,
  /** 上下游皆空 → 本轮被判孤岛表。 */
  isolated: 47,
  /** 孤岛导致的降级。 */
  degraded: 31,
  brokenSegments: 2,
};

export function qualify(name: string) {
  return name.includes(".") ? name : `${SOURCE_DB}.${name}`;
}

/** 与 `connectors/datahub.build_dataset_urn` 同形。 */
export function buildDatasetUrn(name: string, platform = "mysql", fabric = "PROD") {
  return `urn:li:dataset:(urn:li:dataPlatform:${platform},${qualify(name)},${fabric})`;
}

/* ------------------------------------------------------------------ *
 * 表清单（左栏 / 画布取表）
 * ------------------------------------------------------------------ */

export interface TableRow {
  name: string;
  isolated: boolean;
  upstream: number;
  downstream: number;
}

export const TABLES: TableRow[] = [
  { name: "ext_customer_credit_daily", isolated: true, upstream: 0, downstream: 0 },
  { name: "dw_sales_order_wide", isolated: true, upstream: 0, downstream: 0 },
  { name: "stg_partner_shipment_daily", isolated: true, upstream: 0, downstream: 0 },
  { name: "ext_item_price_index", isolated: true, upstream: 0, downstream: 0 },
  { name: "ext_supplier_scorecard", isolated: false, upstream: 3, downstream: 0 },
  { name: "stg_gl_daily_balance", isolated: true, upstream: 0, downstream: 0 },
  { name: "stg_stock_movement", isolated: false, upstream: 3, downstream: 1 },
  { name: "ext_project_cost_daily", isolated: false, upstream: 3, downstream: 0 },
  { name: "dw_customer_360", isolated: true, upstream: 0, downstream: 0 },
  { name: "imp_channel_order_2026", isolated: true, upstream: 0, downstream: 0 },
  { name: "imp_offline_store_traffic", isolated: true, upstream: 0, downstream: 0 },
  { name: "imp_wms_pick_log", isolated: true, upstream: 0, downstream: 0 },
  { name: "tabSales Order", isolated: false, upstream: 2, downstream: 6 },
  { name: "tabSales Order Item", isolated: false, upstream: 1, downstream: 4 },
  { name: "tabCustomer", isolated: false, upstream: 0, downstream: 9 },
  { name: "tabSales Invoice", isolated: false, upstream: 3, downstream: 5 },
  { name: "tabPayment Entry", isolated: false, upstream: 2, downstream: 3 },
  { name: "tabItem", isolated: false, upstream: 0, downstream: 11 },
  { name: "tabItem Price", isolated: false, upstream: 1, downstream: 2 },
  { name: "tabDelivery Note", isolated: false, upstream: 2, downstream: 3 },
  { name: "tabDelivery Note Item", isolated: false, upstream: 1, downstream: 2 },
  { name: "tabWarehouse", isolated: false, upstream: 0, downstream: 7 },
  { name: "tabStock Ledger Entry", isolated: false, upstream: 4, downstream: 2 },
  { name: "tabGL Entry", isolated: false, upstream: 5, downstream: 1 },
  { name: "tabAccount", isolated: false, upstream: 0, downstream: 6 },
  { name: "tabSupplier", isolated: false, upstream: 0, downstream: 4 },
  { name: "tabPurchase Order", isolated: false, upstream: 2, downstream: 4 },
  { name: "tabEmployee", isolated: false, upstream: 0, downstream: 5 },
];

/* ------------------------------------------------------------------ *
 * 字段（画布上连线要用）
 * ------------------------------------------------------------------ */

export interface ColumnDef {
  name: string;
  type: string;
  pk?: boolean;
}

/** ERPNext 每张 doctype 表都有这几列，没单独写字段的表用它兜底。 */
const FRAPPE_STANDARD: ColumnDef[] = [
  { name: "name", type: "varchar(140)", pk: true },
  { name: "creation", type: "datetime" },
  { name: "modified", type: "datetime" },
  { name: "owner", type: "varchar(140)" },
  { name: "docstatus", type: "int(1)" },
];

const COLUMNS: Record<string, ColumnDef[]> = {
  "tabSales Order": [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "customer", type: "varchar(140)" },
    { name: "transaction_date", type: "date" },
    { name: "delivery_date", type: "date" },
    { name: "grand_total", type: "decimal(21,9)" },
    { name: "status", type: "varchar(30)" },
    { name: "docstatus", type: "int(1)" },
  ],
  "tabSales Order Item": [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "parent", type: "varchar(140)" },
    { name: "item_code", type: "varchar(140)" },
    { name: "qty", type: "decimal(21,9)" },
    { name: "rate", type: "decimal(21,9)" },
    { name: "warehouse", type: "varchar(140)" },
  ],
  tabCustomer: [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "customer_name", type: "varchar(140)" },
    { name: "customer_group", type: "varchar(140)" },
    { name: "territory", type: "varchar(140)" },
    { name: "tax_id", type: "varchar(140)" },
  ],
  "tabSales Invoice": [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "customer", type: "varchar(140)" },
    { name: "posting_date", type: "date" },
    { name: "grand_total", type: "decimal(21,9)" },
    { name: "outstanding_amount", type: "decimal(21,9)" },
    { name: "pos_profile", type: "varchar(140)" },
  ],
  "tabPayment Entry": [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "party_type", type: "varchar(140)" },
    { name: "party", type: "varchar(140)" },
    { name: "posting_date", type: "date" },
    { name: "paid_amount", type: "decimal(21,9)" },
  ],
  tabItem: [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "item_name", type: "varchar(140)" },
    { name: "item_group", type: "varchar(140)" },
    { name: "stock_uom", type: "varchar(140)" },
    { name: "is_stock_item", type: "int(1)" },
  ],
  "tabDelivery Note": [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "customer", type: "varchar(140)" },
    { name: "posting_date", type: "date" },
    { name: "status", type: "varchar(30)" },
  ],
  tabWarehouse: [
    { name: "name", type: "varchar(140)", pk: true },
    { name: "warehouse_name", type: "varchar(140)" },
    { name: "company", type: "varchar(140)" },
  ],
  imp_channel_order_2026: [
    { name: "so_no", type: "varchar(64)" },
    { name: "cust_code", type: "varchar(64)" },
    { name: "channel", type: "varchar(32)" },
    { name: "order_date", type: "date" },
    { name: "sku_code", type: "varchar(64)" },
    { name: "amount", type: "decimal(18,2)" },
  ],
  imp_offline_store_traffic: [
    { name: "invoice_no", type: "varchar(64)" },
    { name: "store_id", type: "varchar(32)" },
    { name: "visit_date", type: "date" },
    { name: "visitors", type: "int" },
  ],
  imp_wms_pick_log: [
    { name: "pick_id", type: "varchar(64)" },
    { name: "dn_no", type: "varchar(64)" },
    { name: "item_code", type: "varchar(64)" },
    { name: "picked_qty", type: "decimal(18,3)" },
    { name: "picked_at", type: "datetime" },
  ],
  ext_customer_credit_daily: [
    { name: "customer_id", type: "varchar(140)", pk: true },
    { name: "customer_name", type: "varchar(140)" },
    { name: "invoiced_amt", type: "decimal(18,2)" },
    { name: "paid_amt", type: "decimal(18,2)" },
    { name: "stat_date", type: "date" },
  ],
  dw_sales_order_wide: [
    { name: "so_no", type: "varchar(140)" },
    { name: "customer_id", type: "varchar(140)" },
    { name: "item_code", type: "varchar(140)" },
    { name: "qty", type: "decimal(18,3)" },
    { name: "amount", type: "decimal(18,2)" },
    { name: "order_date", type: "date" },
  ],
  stg_stock_movement: [
    { name: "entry_id", type: "varchar(140)" },
    { name: "item_code", type: "varchar(140)" },
    { name: "warehouse", type: "varchar(140)" },
    { name: "qty_change", type: "decimal(18,3)" },
    { name: "posting_date", type: "date" },
  ],
};

export function columnsOf(table: string): ColumnDef[] {
  return COLUMNS[table] ?? FRAPPE_STANDARD;
}

/* ------------------------------------------------------------------ *
 * 代码包扫描结果
 * ------------------------------------------------------------------ */

export type EdgeState = "ok" | "blocked" | "skipped";

/** [上游表, 关联键（可空）, 状态, 原因] */
type RawEdge = [string, string, EdgeState?, string?];

interface RawGroup {
  target: string;
  isolated: boolean;
  file: string;
  edges: RawEdge[];
}

/**
 * 代码包里能解析出的边，按**目标表**成组——扫描一个包动辄上百条边，
 * 逐条列人读不完；按落点分组才对得上「这张表的血缘补上了没有」。
 */
const RAW_GROUPS: RawGroup[] = [
  {
    target: "ext_customer_credit_daily",
    isolated: true,
    file: "credit/build_credit_daily.sql",
    edges: [
      ["tabCustomer", "tabCustomer.name = tabSales Invoice.customer"],
      ["tabSales Invoice", "tabSales Invoice.customer = tabCustomer.name"],
      ["tabPayment Entry", "tabPayment Entry.party = tabCustomer.name"],
    ],
  },
  {
    target: "v_customer_credit_flag",
    isolated: false,
    file: "credit/flag_view.sql",
    edges: [
      [
        "ext_customer_credit_daily",
        "ext_customer_credit_daily.customer_id = v_customer_credit_flag.customer_id",
        "blocked",
        "目标视图未在 DataHub 摄取",
      ],
    ],
  },
  {
    target: "dw_sales_order_wide",
    isolated: true,
    file: "sales/order_wide.sql",
    edges: [
      ["tabSales Order", "tabSales Order.name = tabSales Order Item.parent"],
      ["tabSales Order Item", "tabSales Order Item.item_code = tabItem.name"],
      ["tabCustomer", "tabSales Order.customer = tabCustomer.name"],
      ["tabItem", "tabSales Order Item.item_code = tabItem.name"],
    ],
  },
  {
    target: "stg_partner_shipment_daily",
    isolated: true,
    file: "logistics/partner_shipment.sql",
    edges: [
      ["tabDelivery Note", "tabDelivery Note Item.parent = tabDelivery Note.name"],
      ["tabDelivery Note Item", "tabDelivery Note Item.warehouse = tabWarehouse.name"],
      ["tabWarehouse", "tabWarehouse.name = tabDelivery Note Item.warehouse"],
    ],
  },
  {
    target: "ext_item_price_index",
    isolated: true,
    file: "pricing/price_index.sql",
    edges: [
      ["tabItem", "tabItem Price.item_code = tabItem.name"],
      ["tabItem Price", "tabItem Price.item_code = tabItem.name"],
      [
        "ext_market.commodity_index",
        "commodity_index.item_code = tabItem.name",
        "blocked",
        "源库 ext_market 未接入 DataHub",
      ],
    ],
  },
  {
    target: "stg_gl_daily_balance",
    isolated: true,
    file: "finance/gl_daily.sql",
    edges: [
      ["tabGL Entry", "tabGL Entry.account = tabAccount.name"],
      ["tabAccount", "tabAccount.name = tabGL Entry.account"],
    ],
  },
  {
    target: "ext_supplier_scorecard",
    isolated: false,
    file: "scm/supplier_score.sql",
    edges: [
      ["tabSupplier", "tabPurchase Order.supplier = tabSupplier.name"],
      ["tabPurchase Order", "tabPurchase Order.supplier = tabSupplier.name"],
      ["tabPurchase Receipt", "tabPurchase Receipt.supplier = tabSupplier.name"],
    ],
  },
  {
    target: "stg_stock_movement",
    isolated: false,
    file: "stock/movement.sql",
    edges: [
      ["tabStock Ledger Entry", "tabStock Ledger Entry.item_code = tabItem.name"],
      ["tabWarehouse", "tabStock Ledger Entry.warehouse = tabWarehouse.name"],
      ["tabItem", "tabStock Ledger Entry.item_code = tabItem.name"],
    ],
  },
  {
    target: "dw_invoice_payment_link",
    isolated: false,
    file: "finance/inv_pay.sql",
    edges: [
      ["tabSales Invoice", "tabSales Invoice.name = tabPayment Entry Reference.reference_name"],
      ["tabPayment Entry", "tabPayment Entry.name = tabPayment Entry Reference.parent"],
      [
        "tabPayment Entry Reference",
        "tabPayment Entry Reference.reference_name = tabSales Invoice.name",
      ],
    ],
  },
  {
    target: "ext_project_cost_daily",
    isolated: false,
    file: "pm/project_cost.sql",
    edges: [
      ["tabProject", "tabTimesheet.project = tabProject.name"],
      ["tabTimesheet", "tabTimesheet Detail.parent = tabTimesheet.name"],
      ["tabTimesheet Detail", "tabTimesheet Detail.parent = tabTimesheet.name"],
    ],
  },
  {
    target: "stg_employee_attendance",
    isolated: false,
    file: "hr/attendance.sql",
    edges: [
      ["tabEmployee", "tabAttendance.employee = tabEmployee.name"],
      ["tabAttendance", "tabAttendance.employee = tabEmployee.name"],
    ],
  },
  {
    target: "dw_customer_360",
    isolated: true,
    file: "marketing/customer360.sql",
    edges: [
      ["tabCustomer", "tabCustomer.name = ext_customer_credit_daily.customer_id"],
      ["ext_customer_credit_daily", "ext_customer_credit_daily.customer_id = tabCustomer.name"],
      ["dw_sales_order_wide", "dw_sales_order_wide.customer_id = tabCustomer.name"],
      ["tabAddress", ""],
      ["tmp_seg.customer_tag", "customer_tag.cust = tabCustomer.name", "skipped", "临时库不在本域"],
    ],
  },
];

export interface ScanEdge {
  id: string;
  src: string;
  dst: string;
  key: string;
  file: string;
  state: EdgeState;
  reason?: string;
}

export interface ScanGroup {
  target: string;
  /** 目标表当前是不是孤岛——补录后能不能脱离，全看这一列。 */
  isolated: boolean;
  file: string;
  edges: ScanEdge[];
}

export const SCAN_GROUPS: ScanGroup[] = RAW_GROUPS.map((group) => ({
  target: group.target,
  isolated: group.isolated,
  file: group.file,
  edges: group.edges.map(([src, key, state = "ok", reason], index) => ({
    id: `${group.target}#${index}`,
    src,
    dst: group.target,
    key,
    file: group.file,
    state,
    reason,
  })),
}));

/* ------------------------------------------------------------------ *
 * 代码包历史
 * ------------------------------------------------------------------ */

export interface ScanFailure {
  file: string;
  reason: string;
}

export interface SqlPackage {
  id: string;
  name: string;
  size: string;
  uploadedAt: string;
  sqlFiles: number;
  directories: number;
  statements: number;
  /** 这个包覆盖到的落点（对应 SCAN_GROUPS.target）。 */
  targets: string[];
  /** 解析失败的文件——野生代码包里存储过程和动态 SQL 一定有，藏起来会让人以为扫完就全了。 */
  failures: ScanFailure[];
  /** 已上报过的记录。没有＝还没上报。 */
  applied?: { edges: number; resolved: number; at: string };
}

/**
 * 代码包是**有历史的**：谁在什么时候投了哪个包、扫出多少边、上报了没有、
 * 当时让几张表脱离了孤岛。补录是长期活，同一个包会重投、会补投，
 * 没有历史就只能靠人记。
 */
export const PACKAGES: SqlPackage[] = [
  {
    id: "pkg-ext-bundle",
    name: "erp_ext_sql_bundle.zip",
    size: "4.7 MB",
    uploadedAt: "2026-09-03 14:02",
    sqlFiles: 128,
    directories: 17,
    statements: 407,
    targets: [
      "ext_customer_credit_daily",
      "v_customer_credit_flag",
      "dw_sales_order_wide",
      "stg_partner_shipment_daily",
      "ext_item_price_index",
      "stg_gl_daily_balance",
      "dw_customer_360",
    ],
    failures: [
      { file: "legacy/etl_dump.sql", reason: "存储过程语法（DELIMITER $$）超出解析范围" },
      { file: "adhoc/fix_20250917.sql", reason: "动态 SQL 字符串拼接，静态解析拿不到表名" },
      { file: "reports/monthly_close.sql", reason: "方言不符：Oracle CONNECT BY" },
      { file: "migrate/v2_backfill.sql", reason: "只有 UPDATE，没有可推的落点" },
      { file: "tmp/scratch.sql", reason: "文件为空" },
      { file: "legacy/proc_rebuild_idx.sql", reason: "存储过程语法（DELIMITER $$）超出解析范围" },
    ],
  },
  {
    id: "pkg-scm",
    name: "scm_stock_pack_v2.zip",
    size: "1.2 MB",
    uploadedAt: "2026-08-27 09:41",
    sqlFiles: 34,
    directories: 5,
    statements: 96,
    targets: ["ext_supplier_scorecard", "stg_stock_movement"],
    failures: [{ file: "wms/proc_sync.sql", reason: "存储过程语法（DELIMITER $$）超出解析范围" }],
    applied: { edges: 6, resolved: 2, at: "2026-08-27 10:05" },
  },
  {
    id: "pkg-fin-hr",
    name: "finance_hr_etl_2026Q3.tar.gz",
    size: "2.4 MB",
    uploadedAt: "2026-08-11 16:20",
    sqlFiles: 61,
    directories: 9,
    statements: 188,
    targets: ["dw_invoice_payment_link", "ext_project_cost_daily", "stg_employee_attendance"],
    failures: [
      { file: "hr/legacy_import.sql", reason: "动态 SQL 字符串拼接，静态解析拿不到表名" },
      { file: "ap/aging_report.sql", reason: "方言不符：Oracle CONNECT BY" },
    ],
    /** 当时只勾了一部分落点上报——历史里要看得出「没上全」。 */
    applied: { edges: 6, resolved: 1, at: "2026-08-11 17:02" },
  },
];

export function groupsOf(pkg: SqlPackage): ScanGroup[] {
  return SCAN_GROUPS.filter((group) => pkg.targets.includes(group.target));
}

/** 所有代码包都没提到的孤岛表——只能手工连的那批。 */
export function uncoveredIsolated(): string[] {
  const covered = new Set(PACKAGES.flatMap((pkg) => pkg.targets));
  return TABLES.filter((t) => t.isolated && !covered.has(t.name)).map((t) => t.name);
}
