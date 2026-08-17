"""台账生成器：可复现性、配方精确性、分布形状、真值自洽。

台账不依赖任何外部系统，所以这层能在没有 ERPNext/Odoo 的机器上完全验证——
真正跑造数前，先让这些测试全绿。

钉住的是那些「错了会让分数失真」的性质：
- 同 seed 必须逐字节可复现（数据一变，前面跑的对照组分数全废）
- 脏案例是精确条数而非概率（truth.json 的答案必须是设定值）
- 未下传的单、难匹配客户等冲突数量必须与配方一致（跨系统题的答案）
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from generator.config import Recipe
from generator.ledger import MATCH_ATTR_ONLY, MATCH_ODOO_ONLY, build_ledger
from generator.truth import build_truth, compute_metrics
from generator.truth import _j as json_default

# 全量 6000 单跑一次约数秒；测试用缩小配方，比例关系不变。
SMALL = Recipe(
    seed=7,
    customers=120,
    spus=40,
    skus=160,
    orders=900,
    online_orders=300,
    shared_customers=36,
    shared_exact=14,
    shared_variant=14,
    shared_attr_only=8,
    odoo_only_customers=6,
    orders_missing_in_erp=12,
    foreign_currency_orders=30,
    partial_shipment=120,
    over_under_shipment=20,
    returns=40,
    partial_or_merged_payment=100,
    bad_debt=15,
    cross_period=70,
    amended=25,
    cancelled=35,
    dirty_names=8,
    rounding_residue=50,
    stockout_delayed=50,
)


@pytest.fixture(scope="module")
def led():
    return build_ledger(SMALL)


# ---------------------------------------------------------------- 可复现性

def test_same_seed_is_byte_identical():
    a = json.dumps(build_truth(build_ledger(SMALL)), default=json_default, sort_keys=True)
    b = json.dumps(build_truth(build_ledger(SMALL)), default=json_default, sort_keys=True)
    assert a == b


def test_different_seed_diverges():
    other = Recipe(**{**SMALL.__dict__, "seed": SMALL.seed + 1})
    a = json.dumps(build_truth(build_ledger(SMALL)), default=json_default, sort_keys=True)
    b = json.dumps(build_truth(build_ledger(other)), default=json_default, sort_keys=True)
    assert a != b


# ---------------------------------------------------------------- 配方精确性

def test_dirty_case_counts_are_exact(led):
    dirty = build_truth(led)["dirty_cases"]
    assert dirty["cancelled"] == SMALL.cancelled
    assert dirty["amended"] == SMALL.amended
    assert dirty["partial_shipment"] == SMALL.partial_shipment
    assert dirty["over_under_shipment"] == SMALL.over_under_shipment
    assert dirty["returns"] == SMALL.returns
    assert dirty["cross_period"] == SMALL.cross_period
    assert dirty["rounding_residue"] == SMALL.rounding_residue
    assert dirty["stockout_delayed"] == SMALL.stockout_delayed
    assert dirty["dirty_names"] == SMALL.dirty_names


def test_bad_debt_only_from_aged_invoices(led):
    """坏账必须挑账龄够 90 天的发票，否则「90+」这档凑不出样本，账龄题失去区分度。"""
    assert build_truth(led)["dirty_cases"]["bad_debt"] == SMALL.bad_debt
    for o in led.orders:
        if not o.bad_debt:
            continue
        aged = [inv for inv in o.invoices if (SMALL.end - inv.posted_on).days >= 90]
        assert aged, f"{o.key} 标了坏账却没有超期 90 天的发票"


def test_cancelled_orders_have_no_fulfillment(led):
    for o in led.orders:
        if o.status == "cancelled":
            assert not o.deliveries and not o.invoices


# ---------------------------------------------------------------- 跨系统冲突

def test_cross_system_conflicts_match_recipe(led):
    c = build_truth(led)["conflicts"]
    assert c["shared_customer_count"] == SMALL.shared_customers
    assert c["hard_to_match_count"] == SMALL.shared_attr_only
    assert c["orders_missing_in_erp_count"] == SMALL.orders_missing_in_erp
    assert len(c["odoo_only_customers"]) == SMALL.odoo_only_customers
    assert len(c["spu_sku_map"]) == SMALL.spus


def test_hard_match_customers_share_only_attributes(led):
    """难匹配那档：名称完全不同，只有税号/电话能对上——B1（无本体）应该在这里翻车。"""
    for c in led.customers:
        if c.match_kind != MATCH_ATTR_ONLY:
            continue
        assert c.erp_name and c.odoo_name
        assert c.erp_name != c.odoo_name
        assert c.odoo_name not in c.erp_name and c.erp_name not in c.odoo_name


def test_odoo_only_customers_absent_from_erp(led):
    for c in led.customers:
        if c.match_kind == MATCH_ODOO_ONLY:
            assert c.erp_name is None and c.odoo_name is not None


def test_online_orders_only_reference_odoo_visible_customers(led):
    """线上单必须挂在 Odoo 里存在的客户上，否则单子在电商侧没处落。"""
    for o in led.orders:
        if o.channel == "online":
            assert led.customer(o.customer_key).odoo_name is not None
            assert o.po_no  # 跨系统关联键
        else:
            assert o.po_no is None
            assert not o.in_odoo


def test_missing_orders_are_subset_of_online(led):
    # 「故意未下传」只算共享客户的线上单；odoo_only 客户的单是自然缺失，不算在内
    missing = [
        o for o in led.orders
        if not o.in_erp and led.customer(o.customer_key).match_kind != MATCH_ODOO_ONLY
    ]
    assert len(missing) == SMALL.orders_missing_in_erp
    assert all(o.channel == "online" and o.in_odoo for o in missing)


def test_odoo_only_customer_orders_never_in_erp(led):
    """odoo_only 客户只存在于 Odoo，其线上单天然不进 ERP（in_erp=False）。"""
    odoo_only = {c.key for c in led.customers if c.match_kind == MATCH_ODOO_ONLY}
    orders = [o for o in led.orders if o.customer_key in odoo_only]
    assert orders, "odoo_only 客户应有线上单"
    assert all(o.channel == "online" and o.in_odoo and not o.in_erp for o in orders)


# ---------------------------------------------------------------- 分布形状

def test_order_dates_within_window(led):
    assert all(SMALL.start <= o.ordered_on <= SMALL.end for o in led.orders)


def test_customer_frequency_follows_power_law(led):
    """20% 客户应贡献约 70% 订单。分布压平则 DSO/复购率失去区分度，B1 也能蒙对。"""
    from collections import Counter

    counts = sorted(Counter(o.customer_key for o in led.orders).values(), reverse=True)
    top = counts[: max(1, len(counts) // 5)]
    share = sum(top) / sum(counts)
    assert 0.45 < share < 0.85, f"top20% 占比 {share:.2%}，分布不对"


def test_weekend_volume_is_lower(led):
    weekday = sum(1 for o in led.orders if o.ordered_on.weekday() < 5)
    weekend = sum(1 for o in led.orders if o.ordered_on.weekday() >= 5)
    assert weekend / max(weekday, 1) < 0.5


# ---------------------------------------------------------------- 真值自洽

def test_rounding_residue_breaks_header_vs_lines(led):
    """行合计与表头差 0.01 —— 专门给「不看语义只按字面聚合」设的坎。"""
    off = [o for o in led.orders if o.total != o.line_total]
    assert len(off) == SMALL.rounding_residue
    assert all(abs(o.total - o.line_total) == Decimal("0.01") for o in off)


def test_metrics_are_populated_and_in_range(led):
    m = compute_metrics(led)
    assert m["fulfillment_cycle_days_avg"] > 0
    assert 0 < m["on_time_delivery_rate"] < 1
    assert 0 < m["gross_margin_rate"] < 1
    assert 0 < m["repurchase_rate"] <= 1
    assert m["revenue"] > 0
    assert m["dso_classic"] > 0


def test_ar_aging_buckets_cover_all_four(led):
    """四档都要有钱，否则账龄题只有一个答案，问不出东西。"""
    aging = compute_metrics(led)["ar_aging"]
    assert set(aging) == {"0-30", "31-60", "61-90", "90+"}
    assert all(v > 0 for v in aging.values()), aging


def test_ar_total_equals_aging_sum(led):
    m = compute_metrics(led)
    assert m["ar_total"] == sum(m["ar_aging"].values())


def test_ar_ignores_payments_after_window_end(led):
    """应收是时点存量：窗口结束后才收到的钱不能算已付。

    回归：曾经不做时点截断，账期 90 天的回款落在窗口外也被算成已收，
    期末应收严重低估——DSO 算出 1.4 天、账龄 90+ 档直接空掉。
    """
    from generator.truth import _paid_by_invoice

    at_end = _paid_by_invoice(led, SMALL.end)
    forever = _paid_by_invoice(led, date(2099, 1, 1))
    assert sum(at_end.values()) < sum(forever.values()), "窗口外确实该有未截断的回款"
    assert all(p.paid_on > SMALL.end for p in led.payments if p.paid_on > SMALL.end)


def test_dso_in_plausible_business_range(led):
    """信用档位是 30/60/90 天，DSO 该落在这个量级。
    数量级不对说明真值算法错了，而不是数据有意思。"""
    m = compute_metrics(led)
    assert 20 < m["dso_classic"] < 100, m["dso_classic"]
    assert 15 < m["avg_collection_days_weighted"] < 90


def test_aging_is_a_stock_not_a_period_slice(led):
    """账龄不随观察窗切片——按下单日过滤会把窗口前开的老账排除掉，
    而那恰恰是 90+ 这档的全部来源。"""
    full = compute_metrics(led)
    obs = compute_metrics(led, since=SMALL.observe_from)
    assert full["ar_total"] == obs["ar_total"]
    assert full["ar_aging"] == obs["ar_aging"]


def test_stockout_orders_deliver_later_than_promised(led):
    """缺货延迟的单绝大多数应该迟于承诺日，否则「按时交付率」这条指标测不到东西。"""
    late = [
        o for o in led.orders
        if o.stockout_delayed and o.first_delivery_on and o.first_delivery_on > o.promised_on
    ]
    total = [o for o in led.orders if o.stockout_delayed and o.first_delivery_on]
    assert len(late) / len(total) > 0.95


def test_truth_json_serializes(led, tmp_path):
    from generator.truth import dump_truth

    out = dump_truth(led, tmp_path / "truth.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["meta"]["seed"] == SMALL.seed
    assert data["counts"]["orders"] == SMALL.orders
    # 金额一律以字符串落盘，避免 float 精度在下游比对时引入假差异
    assert isinstance(data["counts"]["order_total_sum"], str)


def test_observation_window_is_a_subset(led):
    full = compute_metrics(led)
    obs = compute_metrics(led, since=SMALL.observe_from)
    assert obs["order_count"] < full["order_count"]
    assert obs["revenue"] < full["revenue"]


def test_recipe_validates_inconsistent_shares():
    with pytest.raises(ValueError):
        Recipe(shared_customers=100, shared_exact=1, shared_variant=1, shared_attr_only=1)
    with pytest.raises(ValueError):
        Recipe(orders=10, online_orders=20)


def test_window_matches_config(led):
    assert SMALL.start == date(2026, 2, 1) and SMALL.end == date(2026, 7, 31)
