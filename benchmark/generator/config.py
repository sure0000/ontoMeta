"""造数配方：所有量级、比例、窗口集中在这里，改这一个文件就能换规模。

配方来自 ``docs/BENCHMARK_DATA_PREP.md`` §3.2–§3.5。**脏案例数量是精确值不是概率**——
按配方精确抽样，truth.json 里的答案才是「设定值」而非「跑出来的现象」。这是跨系统题
能有可信答案的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Recipe:
    seed: int = 42

    # ---- 时间窗 ----
    # 生成 6 个月而指标观察窗取最后 3 个月：账龄要凑齐 0-30/31-60/61-90/90+ 四档，
    # 只造 3 个月的话 90+ 几乎没样本，坏账和 DSO 直接失去区分度。
    start: date = date(2026, 2, 1)
    end: date = date(2026, 7, 31)
    observe_from: date = date(2026, 5, 1)

    # ---- 主数据 ----
    customers: int = 500
    spus: int = 200
    skus: int = 800
    warehouses: int = 6
    sales_persons: int = 12

    # ---- 交易 ----
    orders: int = 6000
    online_orders: int = 1800  # 只有这部分进 Odoo（它扮演前端电商系统）

    # ---- 跨系统冲突（§3.3）----
    shared_customers: int = 120  # 两系统都有
    shared_exact: int = 45  # 名称完全一致，易匹配
    shared_variant: int = 45  # 后缀/拼写差异
    shared_attr_only: int = 30  # 名称完全不同，仅税号或电话相同（难匹配）
    # 电商系统独有的客户。配方原文未列，但一个「零独有客户」的前端系统不真实，
    # 且会让「两系统重复客户有多少」这道题退化成常数。显式参数化，别当默认值忘了。
    odoo_only_customers: int = 25
    orders_missing_in_erp: int = 60  # 线上下单但未下传 ERP —— 跨系统题 1 的答案
    foreign_currency_orders: int = 180

    # ---- 脏案例（§3.4）----
    partial_shipment: int = 800  # 一张 SO 分 2-3 次发
    over_under_shipment: int = 120  # 发货数量 ≠ 订单数量
    returns: int = 260
    partial_or_merged_payment: int = 640
    bad_debt: int = 95  # 超期 90 天未回款
    cross_period: int = 430  # 订单与发票分属不同月
    amended: int = 150  # cancel + amend，产生 amended_from 链
    cancelled: int = 210
    dirty_names: int = 40  # 前后空格 / 全半角混用 —— JOIN 陷阱
    rounding_residue: int = 300  # 行合计与表头差 0.01 —— 聚合口径陷阱
    stockout_delayed: int = 320  # 缺货导致的交付延迟

    # ---- 分布（§3.5）----
    zipf_s: float = 0.9  # 客户下单频次幂律；s=0.9 时 top20% 客户约占 70% 订单
    weekend_weight: float = 0.35
    month_end_boost: float = 1.8
    month_end_days: int = 3
    price_mu: float = 5.6  # 单价对数正态（median ≈ e^5.6 ≈ 270 元）
    price_sigma: float = 0.9
    credit_tiers: tuple[tuple[int, float], ...] = ((30, 0.50), (60, 0.35), (90, 0.15))
    payment_noise_days: float = 8.0
    promise_days: int = 4  # 承诺交付天数，按时交付率的基准（3 天会把按时率压到 54%，偏离真实分销商）

    # ---- 投递 ----
    concurrency: int = 8  # 提交是同步执行在 gunicorn worker 里的，worker 配几就开几

    # ---- ERPNext 侧的既有主数据名（Setup Wizard 建的，按实际改）----
    erp_company: str = ""
    erp_customer_group: str = "Commercial"
    erp_territory: str = "All Territories"
    erp_item_group: str = "Products"
    erp_uom: str = "Nos"
    erp_warehouse: str = ""  # 空则投递器自动取默认仓

    currencies: tuple[str, ...] = field(default=("CNY", "USD"))

    def __post_init__(self) -> None:
        if self.shared_exact + self.shared_variant + self.shared_attr_only != self.shared_customers:
            raise ValueError("shared_* 三档之和必须等于 shared_customers")
        if self.online_orders > self.orders:
            raise ValueError("online_orders 不能超过 orders")
        if self.orders_missing_in_erp > self.online_orders:
            raise ValueError("未下传的单必须是线上单的子集")
        if self.skus < self.spus:
            raise ValueError("SKU 数不能少于 SPU 数")


DEFAULT = Recipe()
