"""系统中立的业务事实台账 —— 真值的唯一来源。

**先造台账，再分别投递到 ERPNext 与 Odoo**，不要反过来。先在 A 造再同步到 B，会让真值
依赖同步逻辑本身的正确性——等于用被验证的对象去生成金标准，跨系统档的分数就没意义了。

台账不含任何 ERPNext / Odoo 的概念：这里只有客户、商品、订单、发货、发票、回款。
两个投递器各自把它翻译成目标系统的单据，并在翻译时注入约定好的差异（名称变体、
未下传的单、SPU/SKU 粒度差、状态机映射）。

全部脏案例按配方**精确抽样**而非按概率撒——truth.json 里的答案才是设定值。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .config import Recipe

CENT = Decimal("0.01")


def money(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 实体

MATCH_EXACT = "exact"  # 两系统名称完全一致
MATCH_VARIANT = "variant"  # 后缀/拼写差异
MATCH_ATTR_ONLY = "attr_only"  # 名称完全不同，仅税号/电话可匹配
MATCH_ERP_ONLY = "erp_only"
MATCH_ODOO_ONLY = "odoo_only"


@dataclass
class Customer:
    key: str
    name: str  # 台账口径的规范名
    tax_id: str
    phone: str
    credit_days: int
    match_kind: str
    erp_name: str | None  # None 表示 ERP 侧没有这个客户
    odoo_name: str | None


@dataclass
class Product:
    key: str  # SKU
    spu_key: str
    name: str
    spec: str
    list_price: Decimal
    std_cost: Decimal


@dataclass
class OrderLine:
    line_no: int
    sku: str
    qty: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return money(self.qty * self.unit_price)


@dataclass
class Delivery:
    key: str
    order_key: str
    delivered_on: date
    lines: list[OrderLine]
    is_return: bool = False


@dataclass
class Invoice:
    key: str
    order_key: str
    delivery_key: str | None
    posted_on: date
    amount: Decimal
    is_credit_note: bool = False


@dataclass
class Payment:
    key: str
    customer_key: str
    paid_on: date
    amount: Decimal
    invoice_keys: list[str]  # 一笔回款可覆盖多张发票（合并回款）


@dataclass
class Order:
    key: str
    customer_key: str
    ordered_on: date
    promised_on: date
    channel: str  # online | offline
    currency: str
    lines: list[OrderLine]
    po_no: str | None  # 线上单的跨系统关联键
    in_erp: bool
    in_odoo: bool
    status: str = "open"  # open | completed | cancelled
    # 脏案例标记
    amended: bool = False
    partial_shipment: bool = False
    over_under: bool = False
    stockout_delayed: bool = False
    cross_period: bool = False
    rounding_residue: bool = False
    returned: bool = False
    bad_debt: bool = False
    deliveries: list[Delivery] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    # 投递后回填的两侧单号，供一致性校验
    erp_name: str | None = None
    odoo_name: str | None = None

    @property
    def line_total(self) -> Decimal:
        return money(sum((ln.amount for ln in self.lines), Decimal("0")))

    @property
    def total(self) -> Decimal:
        """表头金额。rounding_residue 的单故意比行合计**少** 0.01，制造聚合口径陷阱。

        方向是「少」而不是「多」，因为它要能在真实系统里表达出来：ERPNext 的表头金额
        由行算出，投递器改不了；只有挂一笔 ``discount_amount=0.01`` 才能让
        ``grand_total != sum(行金额)``。若台账定义成「多 0.01」，投递时无处安放，
        这条脏案例就只存在于 truth.json 里，真实系统查不到——等于没造。
        """
        base = self.line_total
        return money(base - Decimal("0.01")) if self.rounding_residue else base

    @property
    def first_delivery_on(self) -> date | None:
        real = [d for d in self.deliveries if not d.is_return]
        return min((d.delivered_on for d in real), default=None)


@dataclass
class Ledger:
    recipe: Recipe
    customers: list[Customer]
    products: list[Product]
    orders: list[Order]
    payments: list[Payment]

    def customer(self, key: str) -> Customer:
        return self._cust_idx[key]

    def product(self, key: str) -> Product:
        return self._prod_idx[key]

    def order(self, key: str) -> Order:
        return self._ord_idx[key]

    def __post_init__(self) -> None:
        self._cust_idx = {c.key: c for c in self.customers}
        self._prod_idx = {p.key: p for p in self.products}
        self._ord_idx = {o.key: o for o in self.orders}

    @property
    def invoices(self) -> list[Invoice]:
        return [inv for o in self.orders for inv in o.invoices]


# ---------------------------------------------------------------- 主数据

_REGIONS = ("上海", "北京", "广州", "深圳", "杭州", "成都", "武汉", "南京", "苏州", "青岛")
_STEMS = (
    "远洋", "华泰", "鼎盛", "长风", "恒通", "汇智", "金桥", "天成", "隆昌", "康达",
    "瑞丰", "宏图", "东方", "德立", "嘉禾", "同利", "万顺", "启明", "锦华", "拓维",
)
_TRADES = ("贸易", "商贸", "食品", "日化", "供应链", "百货", "物流", "实业")
_CATEGORIES = ("洗护", "饮品", "休闲食品", "厨房清洁", "纸品", "个护", "调味", "乳品")
_SPECS = ("500ml", "1L", "2L", "150g", "300g", "袋装", "盒装", "六联包", "整箱")


def _company_name(rng: random.Random, seq: int) -> str:
    return (
        f"{rng.choice(_REGIONS)}{rng.choice(_STEMS)}{rng.choice(_TRADES)}有限公司"
        if seq % 7
        else f"{rng.choice(_REGIONS)}{rng.choice(_STEMS)}{rng.choice(_TRADES)}股份有限公司"
    )


def _dirty(rng: random.Random, name: str) -> str:
    """制造 JOIN 陷阱：前后空格 / 全角括号 / 全角空格。字面相等失败，语义上是同一个。

    每种脏化都必须**真的改变字符串**——否则 dirty_names 的实际条数少于配方，
    truth.json 里的答案就不再是设定值。故末尾兜底：没变就退回加空格那种。
    """
    style = rng.randrange(3)
    if style == 0:
        dirty = f"  {name} "
    elif style == 1:
        dirty = name.replace("有限公司", "（有限公司）")
    else:
        dirty = f"{name[0]}　{name[1:]}"  # 全角空格混入
    return dirty if dirty != name else f"  {name} "


def _drop_suffix(name: str) -> str:
    for suffix in ("股份有限公司", "有限公司"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _gen_customers(rng: random.Random, cfg: Recipe) -> list[Customer]:
    tiers, weights = zip(*cfg.credit_tiers)
    out: list[Customer] = []

    # ERP 是主系统，全部 500 个客户都在；其中 shared_customers 个同时出现在 Odoo。
    kinds = (
        [MATCH_EXACT] * cfg.shared_exact
        + [MATCH_VARIANT] * cfg.shared_variant
        + [MATCH_ATTR_ONLY] * cfg.shared_attr_only
        + [MATCH_ERP_ONLY] * (cfg.customers - cfg.shared_customers)
    )
    rng.shuffle(kinds)

    dirty_idx = set(rng.sample(range(cfg.customers), cfg.dirty_names))

    for i, kind in enumerate(kinds, start=1):
        name = _company_name(rng, i)
        erp_name = _dirty(rng, name) if (i - 1) in dirty_idx else name
        if kind == MATCH_EXACT:
            odoo_name: str | None = name
        elif kind == MATCH_VARIANT:
            odoo_name = _drop_suffix(name)
        elif kind == MATCH_ATTR_ONLY:
            # 完全不同的名字（电商侧用品牌名/门店名），只有税号与电话能对上
            odoo_name = f"{rng.choice(_STEMS)}{rng.choice(_CATEGORIES)}旗舰店"
        else:
            odoo_name = None
        out.append(
            Customer(
                key=f"CUST-{i:04d}",
                name=name,
                tax_id=f"91{rng.randrange(10**15, 10**16)}",
                phone=f"1{rng.choice('3578')}{rng.randrange(10**8, 10**9)}",
                credit_days=rng.choices(tiers, weights=weights, k=1)[0],
                match_kind=kind,
                erp_name=erp_name,
                odoo_name=odoo_name,
            )
        )

    # 电商独有客户：只在 Odoo 侧存在，ERP 里查无此人
    for j in range(1, cfg.odoo_only_customers + 1):
        name = _company_name(rng, j)
        out.append(
            Customer(
                key=f"CUST-W{j:03d}",
                name=name,
                tax_id=f"91{rng.randrange(10**15, 10**16)}",
                phone=f"1{rng.choice('3578')}{rng.randrange(10**8, 10**9)}",
                credit_days=rng.choices(tiers, weights=weights, k=1)[0],
                match_kind=MATCH_ODOO_ONLY,
                erp_name=None,
                odoo_name=name,
            )
        )
    return out


def _gen_products(rng: random.Random, cfg: Recipe) -> list[Product]:
    """SPU/SKU 两级 —— Odoo 有 product.template + product.product 两层，ERP 只有 Item 一层。
    粒度差就在这里埋下，是跨系统题「电商 SKU 销量 Top10 在 ERP 里的周转天数」的靶子。"""
    spu_names = [
        f"{rng.choice(_STEMS)}{rng.choice(_CATEGORIES)}{i:03d}" for i in range(1, cfg.spus + 1)
    ]
    out: list[Product] = []
    for i in range(1, cfg.skus + 1):
        spu_no = (i - 1) % cfg.spus
        price = money(min(rng.lognormvariate(cfg.price_mu, cfg.price_sigma), 20000))
        out.append(
            Product(
                key=f"SKU-{i:04d}",
                spu_key=f"SPU-{spu_no + 1:03d}",
                name=spu_names[spu_no],
                spec=rng.choice(_SPECS),
                list_price=price,
                std_cost=money(price * Decimal(str(rng.uniform(0.55, 0.78)))),
            )
        )
    return out


# ---------------------------------------------------------------- 订单

def _first_of_next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def _date_weights(cfg: Recipe) -> tuple[list[date], list[float]]:
    """周末下降、月末冲量。分布不像真的，DSO/账龄/复购率就接近均匀，题目失去区分度。"""
    days: list[date] = []
    weights: list[float] = []
    cur = cfg.start
    while cur <= cfg.end:
        w = cfg.weekend_weight if cur.weekday() >= 5 else 1.0
        if (_first_of_next_month(cur) - cur).days <= cfg.month_end_days:
            w *= cfg.month_end_boost
        days.append(cur)
        weights.append(w)
        cur += timedelta(days=1)
    return days, weights


def _gen_orders(
    rng: random.Random, cfg: Recipe, customers: list[Customer], products: list[Product]
) -> list[Order]:
    days, day_weights = _date_weights(cfg)

    # 幂律：少数客户贡献多数订单
    erp_customers = [c for c in customers if c.match_kind != MATCH_ODOO_ONLY]
    cust_weights = [(i + 1) ** -cfg.zipf_s for i in range(len(erp_customers))]

    online_flags = [True] * cfg.online_orders + [False] * (cfg.orders - cfg.online_orders)
    rng.shuffle(online_flags)
    fx_idx = set(rng.sample(range(cfg.orders), cfg.foreign_currency_orders))

    # 电商独有客户只会出现在线上单里
    odoo_only = [c for c in customers if c.match_kind == MATCH_ODOO_ONLY]
    shared = [c for c in erp_customers if c.odoo_name is not None]

    orders: list[Order] = []
    for i in range(cfg.orders):
        online = online_flags[i]
        if online:
            # 线上单必须落在 Odoo 里有的客户上，否则单子没处挂
            pool = shared + odoo_only
            cust = rng.choice(pool)
        else:
            cust = rng.choices(erp_customers, weights=cust_weights, k=1)[0]

        ordered_on = rng.choices(days, weights=day_weights, k=1)[0]
        # 行数不能超过 SKU 总数——缩规模冒烟时 SKU 可能只剩几个，
        # 不钳制的话 rng.sample 直接抛 "Sample larger than population"
        n_lines = rng.choices((1, 2, 3, 4, 5), weights=(0.18, 0.32, 0.27, 0.15, 0.08), k=1)[0]
        n_lines = min(n_lines, len(products))
        lines = []
        for no, sku in enumerate(rng.sample(products, n_lines), start=1):
            qty = Decimal(rng.choices((1, 2, 5, 10, 20, 50), weights=(0.3, 0.25, 0.2, 0.15, 0.07, 0.03), k=1)[0])
            discount = Decimal(str(rng.uniform(0.82, 1.0)))
            lines.append(OrderLine(no, sku.key, qty, money(sku.list_price * discount)))

        orders.append(
            Order(
                key=f"ORD-{i + 1:06d}",
                customer_key=cust.key,
                ordered_on=ordered_on,
                promised_on=ordered_on + timedelta(days=cfg.promise_days),
                channel="online" if online else "offline",
                currency="USD" if i in fx_idx else "CNY",
                lines=lines,
                po_no=f"WEB-{ordered_on:%Y%m}-{i + 1:06d}" if online else None,
                in_erp=cust.match_kind != MATCH_ODOO_ONLY,
                in_odoo=online,
            )
        )

    orders.sort(key=lambda o: (o.ordered_on, o.key))
    return orders


# ---------------------------------------------------------------- 生命周期与脏案例

def _pick(rng: random.Random, pool: list[Order], n: int) -> set[str]:
    return {o.key for o in rng.sample(pool, min(n, len(pool)))}


def _inject(
    rng: random.Random, cfg: Recipe, orders: list[Order], customers: list[Customer]
) -> list[Payment]:
    by_key = {o.key: o for o in orders}
    credit_of = {c.key: c.credit_days for c in customers}

    cancelled = _pick(rng, orders, cfg.cancelled)
    for o in orders:
        if o.key in cancelled:
            o.status = "cancelled"

    active = [o for o in orders if o.status != "cancelled"]
    for key in _pick(rng, active, cfg.amended):
        by_key[key].amended = True

    flags = {
        "partial_shipment": cfg.partial_shipment,
        "over_under": cfg.over_under_shipment,
        "stockout_delayed": cfg.stockout_delayed,
        "cross_period": cfg.cross_period,
    }
    for attr, n in flags.items():
        for key in _pick(rng, active, n):
            setattr(by_key[key], attr, True)
    for key in _pick(rng, orders, cfg.rounding_residue):
        by_key[key].rounding_residue = True

    # ---- 发货 ----
    dn_seq = inv_seq = 0
    for o in active:
        lag = rng.randint(1, 5)
        if o.stockout_delayed:
            lag += rng.randint(5, 20)  # 缺货导致延迟，按时交付率因此掉下来
        base = o.ordered_on + timedelta(days=lag)

        if o.partial_shipment:
            batches = rng.choice((2, 3))
            for b in range(batches):
                dn_seq += 1
                part = [
                    OrderLine(ln.line_no, ln.sku, money(ln.qty / batches), ln.unit_price)
                    for ln in o.lines
                ]
                o.deliveries.append(
                    Delivery(f"DN-{dn_seq:06d}", o.key, base + timedelta(days=b * rng.randint(3, 12)), part)
                )
        else:
            dn_seq += 1
            lines = list(o.lines)
            if o.over_under:
                # 超发/短发：数量与订单对不上，履约率口径要能反映出来
                factor = Decimal(str(rng.choice((0.8, 0.9, 1.1, 1.2))))
                lines = [OrderLine(ln.line_no, ln.sku, money(ln.qty * factor), ln.unit_price) for ln in o.lines]
            o.deliveries.append(Delivery(f"DN-{dn_seq:06d}", o.key, base, lines))
        o.status = "completed"

    # ---- 退货 ----
    for key in _pick(rng, active, cfg.returns):
        o = next(x for x in active if x.key == key)
        o.returned = True
        dn_seq += 1
        src = o.deliveries[0]
        ret_lines = [OrderLine(ln.line_no, ln.sku, money(-ln.qty * Decimal("0.5")), ln.unit_price) for ln in src.lines]
        o.deliveries.append(
            Delivery(f"DN-{dn_seq:06d}", o.key, src.delivered_on + timedelta(days=rng.randint(5, 30)), ret_lines, is_return=True)
        )

    # ---- 开票 ----
    for o in active:
        for d in o.deliveries:
            inv_seq += 1
            posted = d.delivered_on + timedelta(days=rng.randint(0, 3))
            if o.cross_period and not d.is_return:
                # 跨期：把发票推进下个月，制造「订单归哪个月」的口径分歧
                nxt_month = (posted.replace(day=1) + timedelta(days=32)).replace(day=1)
                posted = nxt_month + timedelta(days=rng.randint(0, 5))
            amount = money(sum((ln.amount for ln in d.lines), Decimal("0")))
            o.invoices.append(
                Invoice(f"INV-{inv_seq:06d}", o.key, d.key, posted, amount, is_credit_note=d.is_return)
            )

    # ---- 回款 ----
    payables = [
        (o, inv) for o in active for inv in o.invoices if not inv.is_credit_note and inv.posted_on <= cfg.end
    ]
    # 坏账只能从「账龄已够 90 天」的发票里选，否则超期 90 天这档根本凑不出样本
    aged = [(o, inv) for o, inv in payables if (cfg.end - inv.posted_on).days >= 90]
    bad_keys = {inv.key for _, inv in rng.sample(aged, min(cfg.bad_debt, len(aged)))}
    for o, inv in payables:
        if inv.key in bad_keys:
            o.bad_debt = True

    remaining = [(o, inv) for o, inv in payables if inv.key not in bad_keys]
    special = {inv.key for _, inv in rng.sample(remaining, min(cfg.partial_or_merged_payment, len(remaining)))}

    payments: list[Payment] = []
    pay_seq = 0
    merged_bucket: dict[str, list[tuple[Order, Invoice]]] = {}

    def _delay(customer_key: str) -> int:
        """账期按客户信用等级分档，叠正态噪声——不这样做的话账龄分布会挤成一根柱子。"""
        return max(1, int(rng.gauss(credit_of[customer_key], cfg.payment_noise_days)))

    for o, inv in remaining:
        cust = o.customer_key
        if inv.key in special:
            if rng.random() < 0.5:
                # 部分回款：金额少于应收，账龄里留一条尾巴
                pay_seq += 1
                payments.append(
                    Payment(
                        f"PAY-{pay_seq:06d}", cust, inv.posted_on + timedelta(days=_delay(cust)),
                        money(inv.amount * Decimal(str(rng.uniform(0.3, 0.8)))), [inv.key],
                    )
                )
            else:
                merged_bucket.setdefault(cust, []).append((o, inv))
            continue
        pay_seq += 1
        payments.append(
            Payment(
                f"PAY-{pay_seq:06d}", cust, inv.posted_on + timedelta(days=_delay(cust)),
                inv.amount, [inv.key],
            )
        )

    # 合并回款：同一客户 2-3 张发票一笔付清
    for cust, items in merged_bucket.items():
        for i in range(0, len(items), 3):
            chunk = items[i : i + 3]
            pay_seq += 1
            last = max(inv.posted_on for _, inv in chunk)
            payments.append(
                Payment(
                    f"PAY-{pay_seq:06d}", cust, last + timedelta(days=_delay(cust)),
                    money(sum((inv.amount for _, inv in chunk), Decimal("0"))),
                    [inv.key for _, inv in chunk],
                )
            )

    return payments


# ---------------------------------------------------------------- 入口

def build_ledger(cfg: Recipe) -> Ledger:
    rng = random.Random(cfg.seed)
    customers = _gen_customers(rng, cfg)
    products = _gen_products(rng, cfg)
    orders = _gen_orders(rng, cfg, customers, products)

    # 未下传 ERP 的线上单 —— 跨系统题「线上下单但 ERP 里没有」的答案，
    # 是配方参数不是跑出来的现象
    online = [o for o in orders if o.channel == "online"]
    # 只从共享客户的线上单里抽「故意未下传」——odoo_only 客户的单在上面的循环里
    # 已经因 match_kind 被置 in_erp=False（自然缺失），不该占这 60 张的配额
    for o in rng.sample([o for o in online if o.in_erp], cfg.orders_missing_in_erp):
        o.in_erp = False

    payments = _inject(rng, cfg, orders, customers)
    return Ledger(recipe=cfg, customers=customers, products=products, orders=orders, payments=payments)
