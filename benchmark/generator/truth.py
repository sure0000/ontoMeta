"""从台账算出真值，落 truth.json。

这份文件是 50 题答案的**第二条独立来源**：另一条是手写金标准 SQL。两者不一致就说明
其中一个错了，必须查清再往下走。没有这条交叉校验，B2 答错而金标准也错时会被判成
「答对」，整份验证报告失去意义。

**注意口径**：这里给的是「按台账定义」的期望值。真实系统里同一指标可能因为退货冲销、
跨期归属、部分回款的处理方式不同而略有出入——**差异本身就是要考的东西**，
不要为了对上而回头改这里的算法。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ledger import (
    MATCH_ATTR_ONLY,
    MATCH_ODOO_ONLY,
    Ledger,
    money,
)

ZERO = Decimal("0")


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(money(v))
    if isinstance(v, date):
        return v.isoformat()
    raise TypeError(f"不可序列化: {type(v)}")


def _paid_by_invoice(led: Ledger, as_of: date) -> dict[str, Decimal]:
    """按发票摊回款，**只算 as_of 当日及之前收到的**。

    账期最长 90 天加噪声，所以有相当一部分回款发生在窗口结束之后。把它们也算成
    已付，期末应收就被严重低估——DSO 会算成 1 天出头，账龄 90+ 档直接空掉。
    应收是时点存量，必须按时点截断。

    合并回款按各发票全额冲销（生成期即保证金额相等）；单张发票可能只收到部分，
    账龄里因此留下尾巴。
    """
    amount_of = {inv.key: inv.amount for inv in led.invoices if not inv.is_credit_note}
    paid: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in led.payments:
        if p.paid_on > as_of:
            continue
        if len(p.invoice_keys) == 1:
            paid[p.invoice_keys[0]] += p.amount
        else:
            for k in p.invoice_keys:
                paid[k] += amount_of.get(k, ZERO)
    return paid


def _aging_bucket(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def compute_metrics(led: Ledger, since: date | None = None) -> dict[str, Any]:
    """八个金标准指标。since 给定时只统计该日之后下单的订单（观察窗）。"""
    cfg = led.recipe
    orders = [o for o in led.orders if since is None or o.ordered_on >= since]
    active = [o for o in orders if o.status != "cancelled"]
    delivered = [o for o in active if o.first_delivery_on is not None]

    cycle = [(o.first_delivery_on - o.ordered_on).days for o in delivered]
    on_time = [o for o in delivered if o.first_delivery_on <= o.promised_on]

    revenue = sum((o.total for o in active), ZERO)
    cost = sum(
        (money(ln.qty * led.product(ln.sku).std_cost) for o in active for ln in o.lines), ZERO
    )
    returned_amt = sum(
        (abs(money(sum((ln.amount for ln in d.lines), ZERO)))
         for o in active for d in o.deliveries if d.is_return),
        ZERO,
    )

    # 应收与账龄：**时点存量，不随 since 切片**。
    # 账龄是「截至 cfg.end 还欠多少、欠了多久」，按下单日过滤会把窗口前开的、
    # 至今未收的老账排除掉——那恰恰是 90+ 这档的全部来源。
    paid = _paid_by_invoice(led, cfg.end)
    aging: dict[str, Decimal] = {b: ZERO for b in ("0-30", "31-60", "61-90", "90+")}
    ar_total = ZERO
    for inv in led.invoices:
        if inv.is_credit_note or inv.posted_on > cfg.end:
            continue
        outstanding = inv.amount - paid.get(inv.key, ZERO)
        if outstanding <= ZERO:
            continue
        ar_total += outstanding
        aging[_aging_bucket((cfg.end - inv.posted_on).days)] += outstanding

    # 回款周期（加权）：只算窗口内已实际收到的
    collect_days: list[tuple[int, Decimal]] = []
    inv_by_key = {inv.key: inv for inv in led.invoices}
    for p in led.payments:
        if p.paid_on > cfg.end:
            continue
        for k in p.invoice_keys:
            inv = inv_by_key.get(k)
            if inv is None:
                continue
            if since is not None and led.order(inv.order_key).ordered_on < since:
                continue
            collect_days.append(((p.paid_on - inv.posted_on).days, p.amount))
    weighted = sum((Decimal(d) * a for d, a in collect_days), ZERO)
    weight_sum = sum((a for _, a in collect_days), ZERO)

    span_days = (cfg.end - (since or cfg.start)).days + 1
    per_customer = Counter(o.customer_key for o in active)

    return {
        "window": {"from": (since or cfg.start), "to": cfg.end, "days": span_days},
        "order_count": len(orders),
        "active_order_count": len(active),
        # ① 订单履约周期（天）
        "fulfillment_cycle_days_avg": round(sum(cycle) / len(cycle), 4) if cycle else None,
        # ② 按时交付率
        "on_time_delivery_rate": round(len(on_time) / len(delivered), 6) if delivered else None,
        # ③ DSO —— 两种口径都给，题目问哪个都有据可依
        "dso_classic": round(float(ar_total / revenue) * span_days, 4) if revenue else None,
        "avg_collection_days_weighted": (
            round(float(weighted / weight_sum), 4) if weight_sum else None
        ),
        # ④ 应收账龄分布
        "ar_total": ar_total,
        "ar_aging": aging,
        # ⑤ 订单毛利率
        "revenue": revenue,
        "cost": cost,
        "gross_margin_rate": round(float((revenue - cost) / revenue), 6) if revenue else None,
        # ⑥ 缺货导致的延迟率
        "stockout_delay_rate": (
            round(len([o for o in delivered if o.stockout_delayed]) / len(delivered), 6)
            if delivered else None
        ),
        # ⑦ 退货率（金额口径）
        "return_amount": returned_amt,
        "return_rate": round(float(returned_amt / revenue), 6) if revenue else None,
        # ⑧ 客户复购率
        "repurchase_rate": (
            round(sum(1 for n in per_customer.values() if n >= 2) / len(per_customer), 6)
            if per_customer else None
        ),
    }


def _expected_erp(led: Ledger) -> dict[str, Any]:
    """ERPNext 侧应该落下多少行。编码投递器语义，是一致性校验的判据。"""
    erp = [o for o in led.orders if o.in_erp]
    amended = [o for o in erp if o.amended]  # 改单会留下「原单 + 改单」两行
    active = [o for o in erp if o.status != "cancelled"]
    inv_keys = {inv.key for o in active for inv in o.invoices}
    return {
        "sales_order_rows": len(erp) + len(amended),
        "sales_order_submitted": len(active),
        "sales_order_cancelled": len(erp) + len(amended) - len(active),
        "sales_order_item_rows": sum(len(o.lines) for o in erp)
        + sum(len(o.lines) for o in amended),
        "delivery_note_rows": sum(len(o.deliveries) for o in active),
        "sales_invoice_rows": sum(len(o.invoices) for o in active),
        "payment_entry_rows": len(
            [p for p in led.payments if any(k in inv_keys for k in p.invoice_keys)]
        ),
        # grand_total = 行合计 - 尾差折扣。若目标站点挂了默认税模板，这条会红——
        # 那是对的，说明数据里多了没预期的税额，别把判据放宽。
        "submitted_grand_total": sum((o.total for o in active), ZERO),
        "customer_rows": sum(1 for c in led.customers if c.erp_name),
        "item_rows": len(led.products),
    }


def _expected_odoo(led: Ledger) -> dict[str, Any]:
    """Odoo 侧应该落下多少行。它只收线上单，且不做改单——不对称本身就是题面。"""
    odoo = [o for o in led.orders if o.in_odoo]
    cancelled = [o for o in odoo if o.status == "cancelled"]
    return {
        "sale_order_rows": len(odoo),
        "sale_order_cancelled": len(cancelled),
        "sale_order_confirmed": len(odoo) - len(cancelled),
        "order_line_rows": sum(len(o.lines) for o in odoo),
        # 比未税额：装了 l10n 科目表后商品可能带默认税，含税额对不上不代表数据错
        "amount_untaxed_sum": sum((o.line_total for o in odoo), ZERO),
        "partner_rows": sum(1 for c in led.customers if c.odoo_name),
        "product_product_rows": len(led.products),
        "product_template_rows": len({p.spu_key for p in led.products}),
    }


def build_truth(led: Ledger) -> dict[str, Any]:
    cfg = led.recipe

    shared = [c for c in led.customers if c.erp_name and c.odoo_name]
    missing = [o for o in led.orders if o.channel == "online" and not o.in_erp]

    spu_sku: dict[str, list[str]] = defaultdict(list)
    for p in led.products:
        spu_sku[p.spu_key].append(p.key)

    # 逐月按「该月下单」切片。跨期发票故意落在下个月，所以「按下单月」与
    # 「按开票月」两种口径必然对不上——那正是要考的东西，不要在这里抹平。
    months: dict[str, Any] = {}
    cur = cfg.observe_from.replace(day=1)
    while cur <= cfg.end:
        nxt = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        sub = [o for o in led.orders if cur <= o.ordered_on < nxt]
        months[f"{cur:%Y-%m}"] = {
            "order_count": len(sub),
            "revenue": sum((o.total for o in sub if o.status != "cancelled"), ZERO),
            "cancelled": sum(1 for o in sub if o.status == "cancelled"),
            "invoiced_amount": sum(
                (inv.amount for o in led.orders for inv in o.invoices
                 if not inv.is_credit_note and cur <= inv.posted_on < nxt),
                ZERO,
            ),
        }
        cur = nxt

    return {
        "meta": {
            "seed": cfg.seed,
            "window": {"from": cfg.start, "to": cfg.end, "observe_from": cfg.observe_from},
            "recipe": {k: v for k, v in asdict(cfg).items() if not isinstance(v, (date, tuple))},
        },
        # ---- 跨系统冲突：这些是**设定值**，不是从系统里跑出来的现象 ----
        "conflicts": {
            "shared_customers": [
                {
                    "ledger_key": c.key,
                    "match_kind": c.match_kind,
                    "erp_name": c.erp_name,
                    "odoo_name": c.odoo_name,
                    "tax_id": c.tax_id,
                    "phone": c.phone,
                }
                for c in shared
            ],
            "shared_customer_count": len(shared),
            "hard_to_match_count": sum(1 for c in shared if c.match_kind == MATCH_ATTR_ONLY),
            "odoo_only_customers": [
                c.key for c in led.customers if c.match_kind == MATCH_ODOO_ONLY
            ],
            "orders_missing_in_erp": [
                {"ledger_key": o.key, "po_no": o.po_no, "ordered_on": o.ordered_on}
                for o in missing
            ],
            "orders_missing_in_erp_count": len(missing),
            "spu_sku_map": dict(spu_sku),
            # Odoo 与 ERPNext 的状态机不同，映射关系是答题依据
            "status_map": {
                "draft": "Draft",
                "sent": "Draft",
                "sale": "To Deliver and Bill",
                "done": "Completed",
                "cancel": "Cancelled",
            },
        },
        # ---- 脏案例的精确条数，供一致性校验逐项比对 ----
        "dirty_cases": {
            "cancelled": sum(1 for o in led.orders if o.status == "cancelled"),
            "amended": sum(1 for o in led.orders if o.amended),
            "partial_shipment": sum(1 for o in led.orders if o.partial_shipment),
            "over_under_shipment": sum(1 for o in led.orders if o.over_under),
            "returns": sum(1 for o in led.orders if o.returned),
            "bad_debt": sum(1 for o in led.orders if o.bad_debt),
            "cross_period": sum(1 for o in led.orders if o.cross_period),
            "rounding_residue": sum(1 for o in led.orders if o.rounding_residue),
            "stockout_delayed": sum(1 for o in led.orders if o.stockout_delayed),
            "dirty_names": sum(
                1 for c in led.customers if c.erp_name and c.erp_name != c.name
            ),
        },
        # ---- 两侧的**预期落地行数**：verify.py 拿它逐项比对 ----
        # 这里编码的是投递器的语义（改单会多出一张原单、取消单不产生下游单据、
        # Odoo 只收线上单且不做改单），所以改投递逻辑必须同步改这里，否则校验会假红。
        "expected_erp": _expected_erp(led),
        "expected_odoo": _expected_odoo(led),
        # ---- 台账口径行数 ----
        "counts": {
            "customers": len(led.customers),
            "customers_in_erp": sum(1 for c in led.customers if c.erp_name),
            "customers_in_odoo": sum(1 for c in led.customers if c.odoo_name),
            "products_sku": len(led.products),
            "products_spu": len(spu_sku),
            "orders": len(led.orders),
            "orders_in_erp": sum(1 for o in led.orders if o.in_erp),
            "orders_in_odoo": sum(1 for o in led.orders if o.in_odoo),
            "order_lines": sum(len(o.lines) for o in led.orders),
            "deliveries": sum(len(o.deliveries) for o in led.orders),
            "invoices": len(led.invoices),
            "payments": len(led.payments),
            "order_total_sum": sum((o.total for o in led.orders), ZERO),
        },
        # ---- 八个金标准指标 ----
        "metrics_full_window": compute_metrics(led),
        "metrics_observation_window": compute_metrics(led, since=cfg.observe_from),
        "metrics_by_month": months,
    }


def dump_truth(led: Ledger, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_truth(led), ensure_ascii=False, indent=2, default=_j), encoding="utf-8"
    )
    return out
